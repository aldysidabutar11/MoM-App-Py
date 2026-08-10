"""Microphone calibration: a short, explicit level test before recording.

Runs for 10-15 seconds with the microphone in its meeting position and reports
whether the level is usable: RMS and peak in dBFS, hard-clipping percentage,
silence share, an approximate noise floor, per-channel activity, and the driver's
own xrun count.

Two boundaries matter.

**Audio is not kept.** The point is the numbers, not the recording. Nothing is
written to disk unless the caller explicitly asks, because a calibration clip is
still a recording of whoever was in the room.

**Nothing on the system is changed.** No gain, no AGC, no microphone
enhancement, no Windows setting, no registry value. When the level is wrong the
operator is told what to adjust; the application does not adjust it for them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from mom_igd.audio.backend import AudioBackend, CaptureProfile, StreamError
from mom_igd.audio.devices import DeviceInfo
from mom_igd.audio.manifest import utc_now_iso
from mom_igd.audio.quality import LevelVerdict, QualityMeter, QualitySnapshot
from mom_igd.logging_setup import get_logger

__all__ = ["CalibrationResult", "run_calibration"]

_LOG = get_logger("audio.calibration")
_POLL_SECONDS = 0.02


@dataclass(slots=True)
class CalibrationResult:
    """Outcome of one calibration run."""

    device: dict[str, Any]
    profile: dict[str, Any]
    seconds: float
    frames: int
    snapshot: QualitySnapshot
    callbacks: int = 0
    xrun_callbacks: int = 0
    dropped_frames: int = 0
    saved_to: str | None = None
    error: str | None = None
    gaps: list[dict[str, Any]] = field(default_factory=list)

    @property
    def verdict(self) -> LevelVerdict:
        return self.snapshot.verdict

    @property
    def advice(self) -> str:
        """The most specific guidance available for this run.

        Delegates to :attr:`QualitySnapshot.diagnosis`, which asks Windows why the level
        is wrong rather than listing the three things it could be. A muted microphone and
        an empty room are byte-identical here; only the endpoint can tell them apart.
        """
        return self.snapshot.diagnosis

    @property
    def ok(self) -> bool:
        return self.error is None and self.frames > 0 and self.verdict.is_acceptable

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "verdict": self.verdict.value,
            "advice": self.advice,
            "device": self.device,
            "profile": self.profile,
            "seconds": round(self.seconds, 2),
            "frames": self.frames,
            "callbacks": self.callbacks,
            "xrun_callbacks": self.xrun_callbacks,
            "dropped_frames": self.dropped_frames,
            "levels": self.snapshot.to_dict(),
            "inactive_channels": list(self.snapshot.inactive_channels),
            "audio_saved": self.saved_to is not None,
            "error": self.error,
            "utc": utc_now_iso(),
        }

    def evidence(self) -> dict[str, Any]:
        """Compact record for ``app_settings``. Carries no audio and no path."""
        return {
            "utc": utc_now_iso(),
            "device": self.device.get("name", "unknown"),
            "device_fingerprint": self.device.get("fingerprint"),
            "transport": self.device.get("transport"),
            "verdict": self.verdict.value,
            "rms_dbfs": round(self.snapshot.rms_dbfs, 2),
            "peak_dbfs": round(self.snapshot.peak_dbfs, 2),
            "clipping_percent": round(self.snapshot.clipping_percent, 4),
            "silence_percent": round(self.snapshot.silence_percent, 2),
            "noise_floor_dbfs": round(self.snapshot.noise_floor_dbfs, 2),
            "inactive_channels": list(self.snapshot.inactive_channels),
            "seconds": round(self.seconds, 2),
            "xrun_callbacks": self.xrun_callbacks,
        }


def run_calibration(
    backend: AudioBackend,
    device: DeviceInfo,
    profile: CaptureProfile,
    *,
    seconds: float = 12.0,
    save_to: Path | None = None,
    meter_stride: int = 1,
    on_level: Callable[[QualitySnapshot], None] | None = None,
    on_audio: Callable[[bytes], None] | None = None,
) -> CalibrationResult:
    """Open the microphone briefly and measure the level.

    **Engages the hardware**, so it must only be reached from an explicit user
    action.

    Args:
        seconds: Measurement duration. The product rule that a real calibration
            lasts 10-15 s lives in ``AudioConfig.calibration_seconds`` and in
            :meth:`mom_igd.audio.service.RecordingService.calibrate`; keeping it
            out of here lets tests exercise the real code path in a fraction of a
            second instead of stalling the suite for ten.
        save_to: When given, the raw PCM is written there. Off by default: a
            calibration clip records whoever is in the room, and keeping it would
            be a retention decision this phase has not made.
    """
    if seconds <= 0:
        raise ValueError(f"calibration seconds={seconds} must be positive.")

    meter = QualityMeter(profile, rolling_seconds=min(3.0, seconds), stride=meter_stride)
    collected: list[bytes] = []
    frames = 0
    callbacks = 0
    xruns = 0
    error: str | None = None

    def _sink(pcm: bytes, count: int, status) -> None:
        nonlocal frames, callbacks, xruns
        frames += count
        callbacks += 1
        if not status.is_clean:
            xruns += 1
        collected.append(pcm)
        # A second consumer, for the voice check that transcribes while it measures.
        # Same discipline as the capture path: it must not raise, and it must not block
        # -- this runs on the device callback, where anything slow is a dropped frame.
        if on_audio is not None:
            try:
                on_audio(pcm)
            except Exception:  # noqa: BLE001 - a listener must not break the measurement
                pass

    try:
        stream = backend.open_input_stream(device.index, profile, _sink)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return CalibrationResult(
            device=device.to_dict(),
            profile=profile.describe(),
            seconds=0.0,
            frames=0,
            snapshot=meter.cumulative_snapshot(),
            error=f"Could not open the microphone: {exc}",
        )

    started = time.monotonic()
    try:
        stream.start()
        deadline = started + seconds
        while time.monotonic() < deadline:
            time.sleep(_POLL_SECONDS)
            # Analyse as we go so a long run does not hold every block in memory
            # any longer than necessary.
            consumed = False
            while collected:
                meter.add(collected.pop(0))
                consumed = True
            # Publish the rolling level while the test runs. Without this the operator
            # watches a still bar for ten to fifteen seconds and concludes the
            # microphone is dead -- which is exactly what happened, on a microphone
            # that was working. A meter that does not move is indistinguishable from
            # one that has nothing to show.
            if consumed and on_level is not None:
                try:
                    on_level(meter.rolling_snapshot())
                except Exception:  # noqa: BLE001 - a listener must not fail the test
                    pass
    except StreamError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            stream.stop()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("Calibration could not stop the stream: %s", exc)
        try:
            stream.close()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("Calibration could not close the stream: %s", exc)

    elapsed = time.monotonic() - started
    tail = b"".join(collected)
    if tail:
        meter.add(tail)

    saved: str | None = None
    if save_to is not None and frames:
        # Explicit opt-in only.
        save_to.parent.mkdir(parents=True, exist_ok=True)
        save_to.write_bytes(tail)
        saved = save_to.name
        _LOG.warning(
            "Calibration audio was saved to %s at the caller's request; delete it "
            "when it is no longer needed.",
            save_to.name,
        )
    collected.clear()

    snapshot = meter.cumulative_snapshot()
    if frames == 0 and error is None:
        error = (
            "The microphone opened but produced no audio. Check that it is not muted "
            "and that Windows has granted microphone access."
        )
    return CalibrationResult(
        device=device.to_dict(),
        profile=profile.describe(),
        seconds=elapsed,
        frames=frames,
        snapshot=snapshot,
        callbacks=callbacks,
        xrun_callbacks=xruns,
        saved_to=saved,
        error=error,
    )
