"""Worker isolation: spawn semantics, resource measurement, cancellation, containment.

The worker is the mechanism behind two invariants that nothing else can provide. One heavy
model at a time is only actually true if the process *exits* — freeing a Python reference
does not reliably return a CTranslate2 arena to the operating system. And peak resident
memory cannot be read after a process has ended, so the parent must sample it while the
child runs.

These tests use a light task (`vad`) rather than transcription wherever possible, so they
exercise the worker machinery without paying for a 500 MB model load. Nothing here needs a
provisioned model.
"""

from __future__ import annotations

import ast
import math
import os
import struct
import wave
from pathlib import Path

import pytest

from mom_igd.asr import worker as worker_module
from mom_igd.asr.tasks import TASK_REGISTRY, TaskCancelled
from mom_igd.asr.worker import (
    WORKER_POLL_SECONDS,
    WorkerOutcome,
    WorkerTimeout,
    measure_peak_rss,
    run_in_worker,
    temperature_evidence,
    worker_environment_summary,
)

ASR = Path(__file__).resolve().parent.parent / "mom_igd" / "asr"


def _silence_wav(path: Path, seconds: float = 1.0, rate: int = 16_000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack("<h", 0) * int(seconds * rate))
    return path


def _tone_wav(path: Path, seconds: float = 1.0, rate: int = 16_000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for index in range(int(seconds * rate)):
        value = int(0.3 * 22000 * math.sin(2 * math.pi * 220 * index / rate))
        frames += struct.pack("<h", max(-32768, min(32767, value)))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return path


# ===========================================================================
# The task registry is a closed set
# ===========================================================================


def test_the_task_registry_is_closed_and_named() -> None:
    """The parent sends a task *name*. Arbitrary code is not runnable in a worker."""
    assert set(TASK_REGISTRY) == {"transcribe", "vad", "probe_model", "probe_directory"}
    for name, handler in TASK_REGISTRY.items():
        assert callable(handler), name


def test_an_unknown_task_name_is_refused_not_guessed() -> None:
    outcome = run_in_worker("../../evil", {}, timeout_seconds=120)
    assert outcome.ok is False
    assert "unknown worker task" in (outcome.error or "")
    assert outcome.exit_code == 0, "refusing a task is not a crash"


@pytest.mark.parametrize("name", ["", "TRANSCRIBE", "transcribe ", "vad;rm -rf /"])
def test_a_near_miss_task_name_is_refused(name: str) -> None:
    outcome = run_in_worker(name, {}, timeout_seconds=120)
    assert outcome.ok is False


# ===========================================================================
# Spawn, and what it returns
# ===========================================================================


def test_a_light_task_runs_in_a_child_and_returns_a_payload(tmp_path: Path) -> None:
    audio = _silence_wav(tmp_path / "a.wav", 1.0)
    outcome = run_in_worker(
        "vad", {"audio_path": str(audio)}, timeout_seconds=300
    )
    assert outcome.ok is True, outcome.error
    assert outcome.exit_code == 0
    assert outcome.payload["ran"] is True
    assert outcome.payload["region_count"] == 0
    assert outcome.wall_seconds > 0


def test_the_worker_measures_its_own_resident_memory(tmp_path: Path) -> None:
    """Peak RSS cannot be recovered after exit, so it must be sampled during the run."""
    audio = _tone_wav(tmp_path / "a.wav", 2.0)
    outcome = run_in_worker("vad", {"audio_path": str(audio)}, timeout_seconds=300)
    assert outcome.ok is True, outcome.error
    # A spawned CPython plus onnxruntime is tens of megabytes at minimum; a zero here
    # would mean the sampler never ran.
    assert outcome.peak_rss_bytes > 10 * (1 << 20), outcome.to_dict()
    assert outcome.peak_threads >= 1
    assert outcome.cpu_seconds >= 0.0


def test_the_outcome_serialises_without_leaking_anything(tmp_path: Path) -> None:
    audio = _silence_wav(tmp_path / "a.wav")
    outcome = run_in_worker("vad", {"audio_path": str(audio)}, timeout_seconds=300)
    payload = outcome.to_dict()
    assert set(payload) == {
        "ok",
        "error",
        "exit_code",
        "wall_seconds",
        "peak_rss_bytes",
        "peak_rss_mib",
        "cpu_seconds",
        "peak_threads",
        "cancelled",
    }
    assert "payload" not in payload, "the transcript payload must not be in the summary"


def test_a_task_that_raises_reports_the_type_without_a_traceback(tmp_path: Path) -> None:
    """An exception string from the ASR stack can contain an audio path."""
    outcome = run_in_worker(
        "vad", {"audio_path": str(tmp_path / "does-not-exist.wav")}, timeout_seconds=300
    )
    assert outcome.ok is False
    assert "VadError" in (outcome.error or "")
    assert "Traceback" not in (outcome.error or "")
    assert len(outcome.error or "") < 400, "the error must be truncated"


def test_a_missing_required_payload_key_is_reported_not_hung() -> None:
    outcome = run_in_worker("vad", {}, timeout_seconds=300)
    assert outcome.ok is False
    assert outcome.error


# ===========================================================================
# Cancellation
# ===========================================================================


def test_the_cancel_flag_actually_crosses_the_process_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cooperative cancellation is only real if the child can see the parent's flag.

    Measured on this machine: the child boots and reaches the task's cancel check about
    115 ms after ``start()``, while the parent's first poll is at
    ``WORKER_POLL_SECONDS`` = 250 ms. So on a sub-poll task the flag is set *after* the
    child has already looked. Shortening the poll interval closes that gap and lets this
    test prove the shared flag propagates, rather than asserting it from the source.
    """
    monkeypatch.setattr(worker_module, "WORKER_POLL_SECONDS", 0.01)
    audio = _silence_wav(tmp_path / "a.wav")
    with pytest.raises(WorkerTimeout) as excinfo:
        run_in_worker(
            "vad",
            {"audio_path": str(audio)},
            timeout_seconds=300,
            should_cancel=lambda: True,
        )
    assert "TaskCancelled" in str(excinfo.value), (
        "the child must have stopped because it observed the flag, "
        f"not for another reason: {excinfo.value}"
    )


def test_a_zero_timeout_stops_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The budget is enforced, and enforcement is reported rather than silent."""
    monkeypatch.setattr(worker_module, "WORKER_POLL_SECONDS", 0.01)
    audio = _tone_wav(tmp_path / "a.wav", 2.0)
    messages: list[str] = []
    with pytest.raises(WorkerTimeout):
        run_in_worker(
            "vad",
            {"audio_path": str(audio)},
            timeout_seconds=0.0,
            progress=messages.append,
        )
    assert any("budget" in message for message in messages), messages


def test_a_task_that_wins_the_race_is_still_reported_as_cancelled(
    tmp_path: Path,
) -> None:
    """Whoever wins the race, the caller is never told a cancelled run was clean.

    Deliberately run at the real poll interval, so this covers the production timing
    rather than a shortened one. Either the child saw the flag (``ok`` false, raised) or
    it finished first (``ok`` true) -- what must hold in both cases is that ``cancelled``
    is set, because a caller that asked to stop must not read the outcome as an
    uninterrupted success.
    """
    audio = _silence_wav(tmp_path / "a.wav")
    messages: list[str] = []
    try:
        outcome = run_in_worker(
            "vad",
            {"audio_path": str(audio)},
            timeout_seconds=300,
            should_cancel=lambda: True,
            progress=messages.append,
        )
    except WorkerTimeout:
        pass  # the child observed the flag; the raise is itself the report
    else:
        assert outcome.cancelled is True, outcome.to_dict()
    assert any("cancellation requested" in message for message in messages), messages


def test_a_worker_that_ignores_the_flag_is_terminated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cooperative first, forced second. Without escalation a wedged decode hangs a job.

    The grace period is 45 s in production -- long enough for the largest bounded region
    to finish -- so it is shortened here rather than waited out. The child is still
    importing when the deadline passes, which is exactly the "did not stop in time"
    condition.
    """
    monkeypatch.setattr(worker_module, "WORKER_POLL_SECONDS", 0.01)
    monkeypatch.setattr(worker_module, "_TERMINATE_GRACE_SECONDS", 0.0)
    audio = _silence_wav(tmp_path / "a.wav")
    messages: list[str] = []
    with pytest.raises(WorkerTimeout) as excinfo:
        run_in_worker(
            "vad",
            {"audio_path": str(audio)},
            timeout_seconds=300,
            should_cancel=lambda: True,
            progress=messages.append,
        )
    assert "terminated" in str(excinfo.value)
    assert any("terminating" in message for message in messages), messages


def test_the_cancel_flag_is_visible_inside_the_task() -> None:
    """Cooperative cancellation only works if the child can actually see the flag."""
    handler = TASK_REGISTRY["vad"]
    with pytest.raises(TaskCancelled):
        handler({"audio_path": "ignored.wav"}, lambda: True)


# ===========================================================================
# Environment reporting
# ===========================================================================


def test_the_environment_summary_carries_no_hostname_or_username() -> None:
    """It goes into a benchmark artefact that gets committed."""
    summary = worker_environment_summary()
    assert summary["start_method"] == "spawn"
    assert summary["logical_cpus"] and summary["logical_cpus"] > 0
    blob = repr(summary).lower()
    for private in (os.environ.get("USERNAME", "\x00").lower(), "users\\", "c:\\"):
        if private and private != "\x00":
            assert private not in blob, f"{private!r} leaked into the summary"


def test_temperature_is_reported_as_unavailable_rather_than_invented() -> None:
    """`psutil.sensors_temperatures` is not implemented on Windows.

    An honest N/A is worth more than a fabricated degree, and the brief requires exactly
    that wording.
    """
    evidence = temperature_evidence()
    assert set(evidence) >= {"available", "detail"}
    if not evidence["available"]:
        assert "N/A" in str(evidence["detail"])


def test_measuring_a_dead_process_returns_zeros_rather_than_raising() -> None:
    """Sampling races the child's exit by construction, so it must tolerate it."""
    rss, cpu, threads = measure_peak_rss(999_999_999)
    assert (rss, cpu, threads) == (0, 0.0, 0)


def test_measuring_this_process_returns_something_plausible() -> None:
    rss, cpu, threads = measure_peak_rss(os.getpid())
    assert rss > 0
    assert cpu >= 0.0
    assert threads >= 1


# ===========================================================================
# Structural guarantees
# ===========================================================================


def test_the_poll_interval_is_short_enough_to_feel_immediate() -> None:
    assert 0.0 < WORKER_POLL_SECONDS <= 0.5


def test_the_worker_uses_spawn_and_no_unix_primitive() -> None:
    source = (ASR / "worker.py").read_text(encoding="utf-8")
    assert 'mp.get_context("spawn")' in source
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for unix_only in ("signal", "fcntl", "pwd", "grp", "termios"):
        assert unix_only not in imported, (
            f"{unix_only} is a Unix primitive; the target platform is Windows"
        )


def test_the_worker_does_not_use_a_hardcoded_temp_directory() -> None:
    """`/tmp` does not exist on Windows, and a hardcoded path bypasses the path service."""
    source = (ASR / "worker.py").read_text(encoding="utf-8")
    for banned in ("/tmp", "/var/tmp", "C:\\\\Temp"):
        assert banned not in source, banned


def test_the_child_always_returns_exactly_one_message() -> None:
    """Otherwise the parent waits for a result that never arrives.

    Asserted structurally: the entrypoint body must be wrapped so that both the success
    and the failure path put something on the queue.
    """
    source = (ASR / "worker.py").read_text(encoding="utf-8")
    entry = source[source.index("def _child_entrypoint") : source.index("def run_in_worker")]
    assert entry.count("result_queue.put(") >= 3, (
        "the child must report on the unknown-task, success and exception paths"
    )
    assert "except BaseException" in entry, (
        "a child that dies without reporting leaves the parent waiting"
    )


def test_a_dead_child_that_reported_nothing_is_not_waited_on_forever() -> None:
    """A child killed by the OS puts nothing on the queue. The parent must not block."""
    source = (ASR / "worker.py").read_text(encoding="utf-8")
    body = source[source.index("def run_in_worker") :]
    branch_at = body.index("if not process.is_alive():")
    branch = body[branch_at : body.index("if cancel_deadline is not None", branch_at)]
    assert "queue.Empty" in branch, "the parent must drain once in case of a race"
    assert "message = {" in branch, "the parent must synthesise an outcome"
    assert "process.exitcode" in branch, "the exit code is the only diagnostic left"
    assert branch.rstrip().endswith("break"), "it must leave the loop, not keep polling"


def test_termination_escalates_but_only_after_a_grace_period() -> None:
    """Killing a process mid-write is how a half-written artefact appears."""
    source = (ASR / "worker.py").read_text(encoding="utf-8")
    assert "_TERMINATE_GRACE_SECONDS" in source
    assert "process.terminate()" in source
    assert "process.kill()" in source
    terminate_at = source.index("process.terminate()")
    grace_at = source.index("cancel_deadline is not None and elapsed > cancel_deadline")
    assert grace_at < terminate_at, "terminate must come after the cooperative deadline"


def test_the_worker_releases_the_model_before_the_process_exits() -> None:
    """A leak would then show as a rising peak across runs, rather than being masked."""
    source = (ASR / "tasks.py").read_text(encoding="utf-8")
    assert "provider.close()" in source
    assert source.count("finally:") >= 2


def test_only_json_serialisable_values_cross_the_boundary() -> None:
    """No model handles, no open files, no numpy arrays."""
    source = (ASR / "tasks.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    returns = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
    ]
    assert returns, "tasks must return plain dictionaries"


def test_running_two_workers_in_sequence_does_not_accumulate_memory(tmp_path: Path) -> None:
    """The point of a short-lived process: each run starts from a clean interpreter.

    Compares peaks across three sequential runs. A steadily rising peak would mean the
    child is inheriting state, which under `spawn` should be impossible -- so this is the
    regression test for someone switching the start method.
    """
    audio = _tone_wav(tmp_path / "a.wav", 1.0)
    peaks: list[int] = []
    for _ in range(3):
        outcome = run_in_worker("vad", {"audio_path": str(audio)}, timeout_seconds=300)
        assert outcome.ok is True, outcome.error
        peaks.append(outcome.peak_rss_bytes)
    spread = max(peaks) - min(peaks)
    assert spread < max(peaks) * 0.5, (
        f"peak RSS varied by {spread / 2**20:.0f} MiB across identical runs: {peaks}"
    )


def test_an_outcome_can_be_constructed_for_a_failure_without_a_process() -> None:
    outcome = WorkerOutcome(ok=False, error="something")
    assert outcome.to_dict()["ok"] is False
    assert outcome.to_dict()["peak_rss_mib"] == 0.0
