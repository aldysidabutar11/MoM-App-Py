"""Python-side enrollment capture. Raw audio never reaches the browser.

**Why this exists at all.** :meth:`EnrollmentService.add_sample` takes PCM bytes,
which could in principle be supplied by anything -- including JavaScript. It must
not be. Capturing voice in the page would mean `getUserMedia`, audio in browser
memory, and a biometric sample travelling over HTTP as base64 or multipart. Each of
those is a place a voice recording can be cached, logged or intercepted, for no
benefit: the microphone is already reachable from Python through the Phase 2
backend.

So this controller owns the stream. The UI asks it to record a sample and later
receives levels, duration and a quality verdict. **Audio stays in this process, in
memory, for as long as it takes to embed it -- and no longer.** See ADR-0012.

**The callback does one thing.** It copies bytes into a bounded buffer and returns.
No embedding, no encryption, no database access, no logging, and no exception may
reach PortAudio -- the same discipline Phase 2 imposes on its recording callback,
for the same reason: a blocked callback is lost audio.

**Every ceiling is enforced in bytes.** A device that misreports its sample rate
cannot grow the buffer past the limit by claiming a short duration.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Final

from mom_igd.audio.backend import CallbackStatus
from mom_igd.enrollment.service import (
    MAX_SAMPLE_SECONDS,
    EnrollmentError,
    EnrollmentService,
    ReasonCode,
)
from mom_igd.logging_setup import get_logger

__all__ = ["MAX_SAMPLE_BYTES", "EnrollmentCaptureController"]

_LOG = get_logger("enrollment.capture")

MAX_SAMPLE_BYTES: Final[int] = 48_000 * 2 * 2 * int(MAX_SAMPLE_SECONDS) + 8192
"""Hard ceiling for ONE sample: 48 kHz stereo 16-bit for the maximum duration.

Plus a small block-alignment allowance. Enforced in bytes, not seconds, so a device
reporting an unexpected rate cannot exceed it.
"""

_POLL_SECONDS: Final[float] = 0.005


@dataclass(slots=True)
class _Buffer:
    """A bounded byte sink written only by the device callback."""

    limit: int
    chunks: list[bytes] = field(default_factory=list)
    size: int = 0
    overflowed: bool = False
    dropped_frames: int = 0
    xrun_callbacks: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, data: bytes) -> None:
        """Copy and store. Called from the audio callback, so it must not block."""
        with self.lock:
            if self.size + len(data) > self.limit:
                self.overflowed = True
                return
            self.chunks.append(data)
            self.size += len(data)

    def take(self) -> bytes:
        with self.lock:
            joined = b"".join(self.chunks)
            self.chunks.clear()
            self.size = 0
            return joined

    def release(self) -> None:
        with self.lock:
            self.chunks.clear()
            self.size = 0


class EnrollmentCaptureController:
    """Records enrollment samples through the Phase 2 device path."""

    def __init__(
        self, *, recording_service: Any, enrollment_service: EnrollmentService
    ) -> None:
        self._recording = recording_service
        self._enrollment = enrollment_service
        # Guards only the *claim*, never the capture itself. Holding a lock for the
        # seven seconds a sample takes would make a concurrent request queue behind
        # it and then record an extra sample nobody asked for -- and it would let a
        # cancel arriving from the UI thread block on it.
        self._claim = threading.Lock()
        self._capturing = False
        self._buffer: _Buffer | None = None
        self._stream: Any = None

    @property
    def capturing(self) -> bool:
        return self._capturing

    def _claim_capture(self) -> bool:
        """Take the single-capture slot, or report that it is taken. Never blocks."""
        with self._claim:
            if self._capturing:
                return False
            self._capturing = True
            return True

    def _release_capture(self) -> None:
        with self._claim:
            self._capturing = False

    def capture_sample(self, *, seconds: float = 10.0) -> dict[str, Any]:
        """Record one sample and hand it to the enrollment service.

        Synchronous by design. A sample is 8-12 seconds and the operator is watching
        a wizard step; an asynchronous handle would add a second lifecycle to get
        wrong, and the request already has an enrollment session to belong to.
        """
        if not 1.0 <= seconds <= MAX_SAMPLE_SECONDS:
            raise EnrollmentError(
                ReasonCode.INTERNAL_ERROR,
                f"Sample length must be between 1 and {MAX_SAMPLE_SECONDS:.0f} "
                f"seconds, got {seconds}.",
            )
        if not self._claim_capture():
            raise EnrollmentError(
                ReasonCode.CAPTURE_LOCK_HELD,
                "A sample is already being recorded. Wait for it to finish before "
                "starting another.",
            )
        try:

            # The enrollment session must already exist: this is what proves consent,
            # participant status, model availability, device and calibration were all
            # checked *before* the microphone opens.
            status = self._enrollment.status()
            if not status.get("active"):
                raise EnrollmentError(
                    ReasonCode.INTERNAL_ERROR,
                    "No enrollment is in progress. Start one before recording a "
                    "sample; that is where consent and model availability are "
                    "verified.",
                )

            device, error = self._recording.resolve_device()
            if device is None:
                raise EnrollmentError(
                    ReasonCode.DEVICE_DISCONNECTED,
                    error or "The capture device is no longer available.",
                )
            expected = status.get("device", {}).get("fingerprint")
            if expected and device.fingerprint != expected:
                raise EnrollmentError(
                    ReasonCode.DEVICE_CHANGED,
                    "The capture device changed since this enrollment started. A "
                    "template must be tied to one microphone.",
                )
            profile = self._recording.profile_for(device)

            buffer = _Buffer(limit=MAX_SAMPLE_BYTES)
            self._buffer = buffer

            def _on_audio(data: bytes, frames: int, cb_status: CallbackStatus) -> None:
                # Copy and enqueue. Nothing else, ever.
                #
                # A driver overflow is counted as an xrun and NOT as a frame count:
                # the driver does not tell us how many frames it discarded, and
                # inventing a number would be fabricated evidence. The quality gate
                # rejects on any xrun at all, so the sample is refused either way --
                # which is the correct outcome and needs no invented figure.
                if cb_status.input_overflow:
                    buffer.xrun_callbacks += 1
                buffer.append(bytes(data))

            target_bytes = min(
                MAX_SAMPLE_BYTES,
                int(seconds * profile.sample_rate) * profile.bytes_per_frame,
            )
            started = time.monotonic()
            try:
                self._stream = self._recording._backend.open_input_stream(  # noqa: SLF001
                    device.index, profile, _on_audio
                )
                self._stream.start()
                deadline = started + (seconds * 4) + 5.0
                while True:
                    with buffer.lock:
                        enough = buffer.size >= target_bytes
                        overflowed = buffer.overflowed
                    if enough or overflowed:
                        break
                    if time.monotonic() > deadline:
                        raise EnrollmentError(
                            ReasonCode.DEVICE_DISCONNECTED,
                            "The microphone stopped delivering audio before the "
                            "sample was complete.",
                        )
                    time.sleep(_POLL_SECONDS)
            except EnrollmentError:
                self._teardown(release_buffer=True)
                raise
            except Exception as exc:
                self._teardown(release_buffer=True)
                raise EnrollmentError(
                    ReasonCode.DEVICE_DISCONNECTED,
                    "The microphone could not be recorded from. Check that the "
                    f"device is still connected. [{type(exc).__name__}]",
                ) from None

            # Stop before reading, so no callback can run while we drain.
            self._teardown(release_buffer=False)

            if buffer.overflowed:
                buffer.release()
                self._buffer = None
                raise EnrollmentError(
                    ReasonCode.BUFFER_LIMIT_EXCEEDED,
                    "The sample exceeded the in-memory ceiling and was discarded. "
                    "Enrollment audio is never written to disk, so it cannot grow "
                    "without bound.",
                )

            pcm = buffer.take()
            self._buffer = None
            try:
                # Hand over in-process. This is the only place enrollment PCM is
                # passed anywhere, and it never leaves the interpreter.
                return self._enrollment.add_sample(
                    pcm,
                    dropped_frames=buffer.dropped_frames,
                    xrun_callbacks=buffer.xrun_callbacks,
                    device_fingerprint=device.fingerprint,
                )
            finally:
                # Release the reference promptly whatever happened.
                del pcm
        finally:
            # Whatever happened -- success, rejection, device failure, overflow --
            # the stream is closed, the buffer is gone and the slot is free.
            self._teardown(release_buffer=True)
            self._release_capture()

    def abort(self) -> dict[str, Any]:
        """Stop any in-flight capture and drop its audio. Idempotent.

        Deliberately does **not** take the claim lock. A cancel arriving from the UI
        thread while a seven-second sample is recording must take effect promptly; if
        it waited for the slot it would block for the rest of the capture, and the
        operator's Cancel would appear to do nothing.
        """
        had_stream = self._stream is not None
        self._teardown(release_buffer=True)
        return {"aborted": had_stream}

    def _teardown(self, *, release_buffer: bool) -> None:
        """Close the stream and optionally drop the buffer. Never raises."""
        stream = self._stream
        self._stream = None
        if stream is not None:
            for step in ("stop", "close"):
                try:
                    getattr(stream, step)()
                except Exception:  # noqa: BLE001 - cleanup must not mask the cause
                    _LOG.warning("Enrollment stream %s() did not complete cleanly.", step)
        if release_buffer and self._buffer is not None:
            self._buffer.release()
            self._buffer = None

    def shutdown(self) -> None:
        """Called from application shutdown. Leaves no stream or buffer behind."""
        self.abort()
