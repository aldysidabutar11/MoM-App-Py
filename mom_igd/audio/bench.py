"""Fake-backend capture smoke test and accelerated benchmark.

Both drive :class:`FakeAudioBackend` against a temporary data root, so neither
needs a microphone, a GUI or the real runtime directory.

What the benchmark can and cannot tell you, stated plainly: it measures the
capture pipeline -- queue depth, writer latency, chunk integrity, duration drift --
against a deterministic source produced faster than real time. It **cannot** tell
you the CPU and memory cost of recording from a real microphone on the target
laptop, because PortAudio's callback, the driver and the actual clock are not in
the loop. Those figures stay ``NOT MEASURED`` until a real-time soak is run by
hand.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any

from mom_igd.audio.backend import CaptureProfile, SampleFormat
from mom_igd.audio.fake_backend import CounterSource, FakeAudioBackend, SineSource
from mom_igd.audio.manifest import ManifestWriter, verify_manifest, write_manifest_summary
from mom_igd.audio.recovery import recover_recording
from mom_igd.audio.session import CaptureSession, SessionState
from mom_igd.audio.writer import partial_path, write_partial_meta
from mom_igd.config import AppConfig

__all__ = ["run_capture_benchmark", "run_capture_smoke"]

_NOT_MEASURED = "NOT MEASURED (requires a real-time microphone soak)"


def _profile(config: AppConfig, *, sample_rate: int = 8_000, channels: int = 1) -> CaptureProfile:
    """A cheap profile: a low rate keeps a legal 10 s chunk small."""
    return CaptureProfile(
        sample_rate=sample_rate,
        channels=channels,
        sample_format=SampleFormat.INT16,
        chunk_seconds=max(10, min(config.audio.chunk_seconds, 10)),
    )


def _step(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def run_capture_smoke(config: AppConfig) -> dict[str, Any]:
    """Capture, verify, then crash and recover -- entirely on the fake backend."""
    steps: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="mom_audio_smoke_") as temporary:
        root = Path(temporary)
        profile = _profile(config)
        backend = FakeAudioBackend(blocksize=800, source=CounterSource())

        # -- 1. capture -----------------------------------------------------
        directory = root / "meeting-uuid" / "recording-uuid"
        directory.mkdir(parents=True)
        manifest = ManifestWriter(directory)
        session = CaptureSession(
            backend,
            device_index=0,
            profile=profile,
            directory=directory,
            recording_uuid="smoke",
            queue_seconds=config.audio.queue_seconds,
            manifest=manifest,
        )
        session.start()
        steps.append(
            _step("session_start", session.state is SessionState.RUNNING, "capture started")
        )
        stream = backend.streams[0]
        blocks = 10  # 10 x 800 frames = exactly one second at 8 kHz
        target = blocks * 800
        deadline = time.monotonic() + 30.0
        sent = 0
        while sent < blocks and time.monotonic() < deadline:
            # Paced in small batches: pumping is synchronous while the writer
            # consumes asynchronously, so a single burst would arrive faster than
            # real time and overflow the bounded queue by design.
            batch = min(4, blocks - sent)
            stream.pump(batch)
            sent += batch
            while session.frames_written < sent * 800 and time.monotonic() < deadline:
                time.sleep(0.001)
        result = session.stop()
        steps.append(
            _step(
                "capture_no_loss",
                result.dropped_frames == 0 and result.frames_written == target,
                f"{result.frames_written} frames written, {result.dropped_frames} dropped",
            )
        )
        steps.append(
            _step(
                "clean_shutdown",
                result.state is SessionState.STOPPED and not session.writer_alive(),
                f"state={result.state.value}, writer thread exited",
            )
        )

        # -- 2. integrity ---------------------------------------------------
        write_manifest_summary(
            directory,
            recording_uuid="smoke",
            meeting_uuid="meeting-uuid",
            profile=profile,
            records=result.chunks,
        )
        report = verify_manifest(directory)
        steps.append(
            _step(
                "manifest_verified",
                report.ok and report.verified_chunks == len(result.chunks),
                f"{report.verified_chunks} chunk(s) verified, chain "
                f"{report.chain_sha256[:12]}",
            )
        )
        expected = CounterSource().read(0, target, profile)
        import wave

        recovered = bytearray()
        for record in sorted(result.chunks, key=lambda r: r.seq):
            with wave.open(str(directory / record.filename), "rb") as handle:
                recovered += handle.readframes(handle.getnframes())
        steps.append(
            _step(
                "audio_byte_exact",
                bytes(recovered) == expected,
                f"{len(recovered)} bytes match the deterministic source exactly",
            )
        )

        # -- 3. tampering ---------------------------------------------------
        victim = directory / sorted(result.chunks, key=lambda r: r.seq)[0].filename
        original = victim.read_bytes()
        mutated = bytearray(original)
        mutated[64] ^= 0xFF
        victim.write_bytes(bytes(mutated))
        tampered = verify_manifest(directory)
        victim.write_bytes(original)
        steps.append(
            _step(
                "tampering_detected",
                not tampered.ok and bool(tampered.checksum_mismatches),
                "a single flipped byte was detected by checksum",
            )
        )

        # -- 4. crash recovery ----------------------------------------------
        crashed = root / "meeting-uuid" / "crashed-uuid"
        crashed.mkdir(parents=True)
        write_partial_meta(
            crashed,
            0,
            profile,
            start_frame=0,
            utc_start="2026-01-01T00:00:00.000Z",
            monotonic_start_ns=1,
            recording_uuid="crashed",
        )
        # Whole frames plus a fragment that does not complete one.
        partial_path(crashed, 0).write_bytes(
            CounterSource().read(0, 4_321, profile) + b"\x7f"
        )
        recovery = recover_recording(crashed, profile=profile)
        steps.append(
            _step(
                "recovery_rebuilt_partial",
                recovery.chunks_recovered == 1 and recovery.frames_recovered == 4_321,
                f"{recovery.frames_recovered} frames recovered, "
                f"{recovery.bytes_discarded} trailing byte(s) discarded",
            )
        )
        second = recover_recording(crashed, profile=profile)
        steps.append(
            _step(
                "recovery_idempotent",
                second.chunks_recovered == 0 and not second.changed,
                "a second recovery pass changed nothing",
            )
        )
        recovered_report = verify_manifest(crashed)
        steps.append(
            _step(
                "recovered_audio_verifies",
                recovered_report.verified_chunks == 1,
                f"{recovered_report.verified_chunks} recovered chunk verified",
            )
        )

    passed = sum(1 for step in steps if step["ok"])
    return {
        "ok": passed == len(steps),
        "passed": passed,
        "total": len(steps),
        "steps": steps,
    }


def run_capture_benchmark(
    config: AppConfig, *, audio_minutes: float = 10.0, speed: float = 60.0
) -> dict[str, Any]:
    """Push simulated audio through the real pipeline faster than real time."""
    try:
        import psutil

        process = psutil.Process()
    except Exception:  # pragma: no cover - psutil is a required dependency
        process = None

    profile = _profile(config)
    total_frames = int(profile.sample_rate * audio_minutes * 60)
    blocksize = max(256, profile.sample_rate // 20)  # ~50 ms blocks
    backend = FakeAudioBackend(
        blocksize=blocksize,
        source=SineSource(frequency_hz=440.0, level_dbfs=-18.0),
        realtime=True,
        speed=speed,
        total_frames=total_frames,
    )

    rss_samples: list[int] = []
    cpu_samples: list[float] = []

    with tempfile.TemporaryDirectory(prefix="mom_audio_bench_") as temporary:
        directory = Path(temporary) / "m" / "r"
        directory.mkdir(parents=True)
        manifest = ManifestWriter(directory)
        session = CaptureSession(
            backend,
            device_index=0,
            profile=profile,
            directory=directory,
            recording_uuid="bench",
            queue_seconds=config.audio.queue_seconds,
            manifest=manifest,
            meter_stride=config.audio.meter_stride,
        )
        if process is not None:
            process.cpu_percent(None)
        wall_start = time.monotonic()
        session.start()
        stream = backend.streams[0]
        deadline = wall_start + max(30.0, audio_minutes * 60.0 / speed * 4)
        while stream.frames_produced < total_frames and time.monotonic() < deadline:
            time.sleep(0.05)
            if process is not None:
                rss_samples.append(process.memory_info().rss)
                cpu_samples.append(process.cpu_percent(None))
        frames_produced = stream.frames_produced
        result = session.stop()
        wall_seconds = time.monotonic() - wall_start
        write_manifest_summary(
            directory,
            recording_uuid="bench",
            meeting_uuid="m",
            profile=profile,
            records=result.chunks,
        )
        verification = verify_manifest(directory)

    queue = session.queue_stats
    writer = session.writer_stats
    cpu_sorted = sorted(cpu_samples)
    cpu_avg = sum(cpu_samples) / len(cpu_samples) if cpu_samples else 0.0
    cpu_p95 = cpu_sorted[int(0.95 * (len(cpu_sorted) - 1))] if cpu_sorted else 0.0
    rss_peak = max(rss_samples) if rss_samples else 0
    rss_avg = sum(rss_samples) / len(rss_samples) if rss_samples else 0

    measured = {
        "simulated_audio_minutes": round(audio_minutes, 2),
        "speed_multiplier": speed,
        "wall_seconds": round(wall_seconds, 2),
        "audio_seconds": round(result.audio_seconds, 2),
        "frames_written": result.frames_written,
        "frames_produced": frames_produced,
        "frames_requested": total_frames,
        # Capture fidelity: did the writer persist every frame the device produced?
        # This is the target that means "no frame lost". Comparing against
        # `frames_requested` instead would blame the capture path for a fake
        # generator that simply ran out of wall clock at a high speed multiplier.
        "capture_drift_percent": round(
            100.0
            * abs(result.frames_written - frames_produced)
            / max(frames_produced, 1),
            4,
        ),
        # Harness completion: how much of the requested audio the generator managed
        # to synthesise. Informational -- it measures this machine's spare CPU, not
        # the capture path.
        "requested_audio_delivered_percent": round(
            100.0 * frames_produced / max(total_frames, 1), 2
        ),
        "dropped_frames": result.dropped_frames,
        "xrun_callbacks": result.xrun_callbacks,
        "chunks": len(result.chunks),
        "bytes_written": writer["bytes_written"],
        "queue_high_water_frames": queue["high_water_frames"],
        "queue_high_water_percent": queue["high_water_percent"],
        "writer_mean_write_ms": writer["mean_write_ms"],
        "writer_finalise_max_s": writer["finalise_seconds_max"],
        "checksum_mismatches": len(verification.checksum_mismatches),
        "corrupt_chunks": len(verification.header_mismatches),
        "leaked_writer_threads": 0 if not session.writer_alive() else 1,
        "process_cpu_percent_avg": round(cpu_avg, 2),
        "process_cpu_percent_p95": round(cpu_p95, 2),
        "process_rss_mb_avg": round(rss_avg / (1024 * 1024), 1),
        "process_rss_mb_peak": round(rss_peak / (1024 * 1024), 1),
    }

    def verdict(ok: bool, actual: Any, target: str) -> str:
        return f"{'PASS' if ok else 'FAIL'} (actual {actual}, target {target})"

    targets = {
        "dropped_frames": verdict(result.dropped_frames == 0, result.dropped_frames, "0"),
        "xrun_callbacks": verdict(result.xrun_callbacks == 0, result.xrun_callbacks, "0"),
        "corrupt_chunks": verdict(
            not verification.header_mismatches, len(verification.header_mismatches), "0"
        ),
        "checksum_mismatches": verdict(
            not verification.checksum_mismatches,
            len(verification.checksum_mismatches),
            "0",
        ),
        "leaked_threads": verdict(not session.writer_alive(), 0, "0"),
        "capture_drift": verdict(
            measured["capture_drift_percent"] <= 0.1,
            f"{measured['capture_drift_percent']}%",
            "<= 0.1%",
        ),
        # The accelerated run deliberately saturates the pipeline, so its CPU and
        # memory figures say nothing about a real 1x recording.
        "capture_cpu_avg_le_5pct": _NOT_MEASURED,
        "capture_cpu_p95_le_10pct": _NOT_MEASURED,
        "capture_rss_le_250mb": _NOT_MEASURED,
    }
    hard_targets = [
        v for k, v in targets.items() if not str(v).startswith("NOT MEASURED")
    ]
    delivered = measured["requested_audio_delivered_percent"]
    note = (
        "Fake-backend figures. CPU, p95 CPU and RSS for the production device "
        "are NOT MEASURED until a real-time microphone soak is run."
    )
    if delivered < 99.0:
        # Say so loudly. A run that quietly covered a fraction of the requested
        # audio would otherwise read as a full-length soak.
        note += (
            f" INCOMPLETE COVERAGE: the fake generator synthesised only "
            f"{delivered}% of the requested {round(audio_minutes, 2)} audio minutes "
            f"before the wall-clock deadline -- this machine cannot sustain "
            f"{speed}x for that length. Lower --speed or --minutes for full "
            "coverage. The capture-fidelity targets above still apply to the audio "
            "that was delivered."
        )
    return {
        "ok": all(v.startswith("PASS") for v in hard_targets) and verification.ok,
        "measured": measured,
        "targets": targets,
        "manifest_ok": verification.ok,
        "coverage_complete": delivered >= 99.0,
        "note": note,
    }
