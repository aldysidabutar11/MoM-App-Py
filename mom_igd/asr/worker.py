"""Heavy ASR execution in a short-lived, isolated worker process.

**Why a separate process and not a thread.** A CTranslate2 model holds hundreds of
megabytes of weights plus its own arena allocators. Freeing the Python reference does
not reliably return that to the operating system, and on a 16 GB machine that matters:
the backend, the desktop shell and a second model must all still fit. A process that
*exits* returns everything, with no reliance on allocator behaviour. This is the
mechanism behind ADR-0004's one-heavy-worker policy, and it is what allows a pass-1
model to be fully released before a pass-2 model is loaded.

**Windows-safe by construction.** ``multiprocessing`` uses ``spawn`` here, explicitly,
rather than inheriting the platform default: the child re-imports the module and gets a
clean interpreter, so nothing depends on ``fork`` semantics, no Unix signal is used, and
no inherited file handle or lock is assumed. Cancellation is cooperative first -- the
child polls a flag and stops at the next region boundary -- and only escalates to
``terminate()`` after a timeout, because killing a process mid-write is how a
half-written artefact appears.

**What crosses the boundary.** Plain JSON-serialisable dictionaries, over a
``multiprocessing.Queue``. No model handles, no open files, no numpy arrays. The child
writes nothing to stdout or stderr on the success path: worker output ends up in logs,
and a transcript in a log is a privacy incident.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Final

from mom_igd.logging_setup import get_logger

__all__ = [
    "WORKER_POLL_SECONDS",
    "WorkerError",
    "WorkerOutcome",
    "WorkerTimeout",
    "measure_peak_rss",
    "run_in_worker",
]

_LOG = get_logger("asr.worker")

#: How often the parent checks for a result or a cancellation. Short enough that a
#: cancel feels immediate, long enough that polling costs nothing measurable.
WORKER_POLL_SECONDS: Final[float] = 0.25

#: Grace period between asking a worker to stop and terminating it. A region decode is
#: the unit of cooperative cancellation, and the largest region is bounded to 30 s.
_TERMINATE_GRACE_SECONDS: Final[float] = 45.0

#: How long to wait for a terminated process to actually die before giving up on a
#: clean join. Beyond this the parent stops waiting and reports it.
_KILL_GRACE_SECONDS: Final[float] = 10.0


class WorkerError(RuntimeError):
    """The worker failed. The message never contains transcript text."""


class WorkerTimeout(WorkerError):
    """The worker exceeded its wall-clock budget and was stopped."""


@dataclass(slots=True)
class WorkerOutcome:
    """What the parent learned from one worker run."""

    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    exit_code: int | None = None
    wall_seconds: float = 0.0
    peak_rss_bytes: int = 0
    cancelled: bool = False
    cpu_seconds: float = 0.0
    peak_threads: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "exit_code": self.exit_code,
            "wall_seconds": round(self.wall_seconds, 3),
            "peak_rss_bytes": self.peak_rss_bytes,
            "peak_rss_mib": round(self.peak_rss_bytes / (1 << 20), 1),
            "cpu_seconds": round(self.cpu_seconds, 2),
            "peak_threads": self.peak_threads,
            "cancelled": self.cancelled,
        }


def measure_peak_rss(pid: int) -> tuple[int, float, int]:
    """Sample one process **and its children**: RSS, CPU seconds, thread count.

    Children matter: CTranslate2 and the ONNX runtime both spawn thread pools, and on
    some configurations helper processes. A measurement that ignored them would report
    a resource ceiling the machine does not actually have.
    """
    try:
        import psutil
    except Exception:  # noqa: BLE001 - psutil is a declared dependency, but be safe
        return 0, 0.0, 0
    try:
        process = psutil.Process(pid)
        with process.oneshot():
            rss = int(process.memory_info().rss)
            cpu_times = process.cpu_times()
            cpu = float(cpu_times.user + cpu_times.system)
            threads = int(process.num_threads())
        for child in process.children(recursive=True):
            try:
                with child.oneshot():
                    rss += int(child.memory_info().rss)
                    child_times = child.cpu_times()
                    cpu += float(child_times.user + child_times.system)
                    threads += int(child.num_threads())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return rss, cpu, threads
    except Exception:  # noqa: BLE001 - a dead process is normal at the end of a run
        return 0, 0.0, 0


def _child_entrypoint(
    task_name: str,
    task_payload: dict[str, Any],
    result_queue: "mp.Queue[dict[str, Any]]",
    cancel_flag: Any,
) -> None:
    """Run inside the spawned child. Imports the heavy stack here, not at module load.

    Everything is wrapped: the child must always put exactly one message on the queue,
    or the parent would wait for a result that never arrives.
    """
    try:
        # Offline flags first, before any library that might consult them is imported.
        from mom_igd.asr.faster_whisper_provider import assert_offline_environment

        assert_offline_environment()

        from mom_igd.asr.tasks import TASK_REGISTRY

        handler = TASK_REGISTRY.get(task_name)
        if handler is None:
            result_queue.put(
                {"ok": False, "error": f"unknown worker task {task_name!r}"}
            )
            return

        def cancelled() -> bool:
            try:
                return bool(cancel_flag.value)
            except Exception:  # noqa: BLE001
                return False

        payload = handler(task_payload, cancelled)
        result_queue.put({"ok": True, "payload": payload})
    except BaseException as exc:  # noqa: BLE001 - the child must never die silently
        # Deliberately only the type and a short message: an exception string from the
        # ASR stack can contain audio paths, and a traceback can contain decoded text.
        result_queue.put(
            {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        )


def run_in_worker(
    task_name: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float = 3 * 60 * 60,
    should_cancel: Callable[[], bool] | None = None,
    progress: Callable[[str], None] | None = None,
) -> WorkerOutcome:
    """Run one heavy task in a spawned worker and return what happened.

    Samples resident memory while the child runs, because peak RSS cannot be recovered
    after the process has exited -- and peak, not final, is what the 2.5 GB budget is
    about.
    """
    context = mp.get_context("spawn")
    result_queue: "mp.Queue[dict[str, Any]]" = context.Queue(maxsize=1)
    cancel_flag = context.Value("i", 0)

    process = context.Process(
        target=_child_entrypoint,
        args=(task_name, payload, result_queue, cancel_flag),
        daemon=False,
        name=f"mom-igd-asr-{task_name}",
    )
    started = time.perf_counter()
    process.start()
    _LOG.info("asr.worker.started", extra={"task": task_name, "pid": process.pid})

    peak_rss = 0
    cpu_seconds = 0.0
    peak_threads = 0
    message: dict[str, Any] | None = None
    cancel_requested = False
    cancel_deadline: float | None = None

    try:
        while True:
            try:
                message = result_queue.get(timeout=WORKER_POLL_SECONDS)
                break
            except queue.Empty:
                pass

            if process.pid is not None:
                rss, cpu, threads = measure_peak_rss(process.pid)
                peak_rss = max(peak_rss, rss)
                cpu_seconds = max(cpu_seconds, cpu)
                peak_threads = max(peak_threads, threads)

            elapsed = time.perf_counter() - started

            if not cancel_requested:
                if should_cancel is not None and should_cancel():
                    cancel_requested = True
                    cancel_flag.value = 1
                    cancel_deadline = elapsed + _TERMINATE_GRACE_SECONDS
                    if progress:
                        progress("cancellation requested; asking the worker to stop")
                elif elapsed > timeout_seconds:
                    cancel_requested = True
                    cancel_flag.value = 1
                    cancel_deadline = elapsed + _TERMINATE_GRACE_SECONDS
                    if progress:
                        progress("wall-clock budget exceeded; stopping the worker")

            if not process.is_alive():
                # The child died without putting a message on the queue. Drain once in
                # case of a race, then give up rather than waiting forever.
                try:
                    message = result_queue.get(timeout=1.0)
                except queue.Empty:
                    message = {
                        "ok": False,
                        "error": (
                            f"worker exited with code {process.exitcode} without "
                            "returning a result"
                        ),
                    }
                break

            if cancel_deadline is not None and elapsed > cancel_deadline:
                # Cooperative cancellation did not land in time. Escalate.
                if progress:
                    progress("worker did not stop cooperatively; terminating")
                process.terminate()
                process.join(timeout=_KILL_GRACE_SECONDS)
                if process.is_alive():  # pragma: no cover - needs a wedged child
                    process.kill()
                    process.join(timeout=_KILL_GRACE_SECONDS)
                message = {"ok": False, "error": "worker was terminated", "cancelled": True}
                break
    finally:
        wall = time.perf_counter() - started
        if process.is_alive():
            process.join(timeout=_KILL_GRACE_SECONDS)
            if process.is_alive():  # pragma: no cover
                process.terminate()
                process.join(timeout=_KILL_GRACE_SECONDS)
        try:
            result_queue.close()
            result_queue.join_thread()
        except Exception:  # noqa: BLE001 - teardown must not mask the outcome
            pass

    outcome = WorkerOutcome(
        ok=bool(message and message.get("ok")),
        payload=dict((message or {}).get("payload") or {}),
        error=(message or {}).get("error"),
        exit_code=process.exitcode,
        wall_seconds=wall,
        peak_rss_bytes=peak_rss,
        cancelled=bool(cancel_requested or (message or {}).get("cancelled")),
        cpu_seconds=cpu_seconds,
        peak_threads=peak_threads,
    )
    _LOG.info(
        "asr.worker.finished",
        extra={
            "task": task_name,
            "ok": outcome.ok,
            "exit_code": outcome.exit_code,
            "wall_s": round(wall, 2),
            "peak_rss_mib": round(peak_rss / (1 << 20), 1),
        },
    )
    if outcome.cancelled and not outcome.ok:
        raise WorkerTimeout(outcome.error or "worker cancelled")
    return outcome


def worker_environment_summary() -> dict[str, Any]:
    """Facts about this machine, for a benchmark record. No hostname, no username."""
    summary: dict[str, Any] = {
        "logical_cpus": os.cpu_count(),
        "start_method": "spawn",
    }
    try:
        import psutil

        summary["physical_cpus"] = psutil.cpu_count(logical=False)
        memory = psutil.virtual_memory()
        summary["total_ram_bytes"] = int(memory.total)
        summary["available_ram_bytes"] = int(memory.available)
    except Exception:  # noqa: BLE001
        pass
    try:
        import ctranslate2

        summary["ctranslate2_version"] = ctranslate2.__version__
        summary["cpu_compute_types"] = sorted(
            ctranslate2.get_supported_compute_types("cpu")
        )
    except Exception:  # noqa: BLE001
        pass
    return summary


def temperature_evidence() -> dict[str, Any]:
    """Thermal data if Windows exposes anything trustworthy, else an explicit N/A.

    ``psutil.sensors_temperatures`` is not implemented on Windows, and the WMI
    ``MSAcpi_ThermalZoneTemperature`` class is absent or stale on most consumer
    laptops. Rather than report a number that might be a placeholder, this reports that
    no sensor was available -- an honest N/A is worth more than a fabricated degree.
    """
    try:
        import psutil

        getter = getattr(psutil, "sensors_temperatures", None)
        if getter is None:
            return {"available": False, "detail": "N/A -- sensor unavailable on Windows"}
        readings = getter()
        if not readings:
            return {"available": False, "detail": "N/A -- sensor unavailable on Windows"}
        return {
            "available": True,
            "detail": {
                name: [round(float(e.current), 1) for e in entries if e.current is not None]
                for name, entries in readings.items()
            },
        }
    except Exception:  # noqa: BLE001
        return {"available": False, "detail": "N/A -- sensor unavailable on Windows"}
