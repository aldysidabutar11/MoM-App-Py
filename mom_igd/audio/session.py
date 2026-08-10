"""Capture session: the audio callback, the bounded queue and the writer thread.

Division of labour, and the reason for it:

* **The audio callback** (PortAudio's real-time thread) copies the frames and
  enqueues them. That is all. No file I/O, no hashing, no database, no metering,
  no allocation beyond the copy, and it never waits for the writer. Blocking here
  makes the driver miss its deadline, and the operating system responds by
  throwing input away.
* **One writer thread** consumes the queue, appends to the current chunk, rotates
  at chunk boundaries and feeds the quality meter. Being single means frames are
  written in exactly the order they arrived, with no interleaving to reason about.
* **The bounded queue** between them makes back-pressure explicit: a disk hiccup
  costs queue depth, and only a sustained stall costs audio -- which is then
  counted rather than hidden.

Pause is a real boundary, not a flag: the stream is stopped so the microphone is
released, the open chunk is finalised, and the gap is recorded in the manifest.
Resume opens a fresh chunk. The result is that a paused interval is visibly absent
from the timeline instead of being silently stitched over.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Final

from mom_igd.audio.backend import (
    AudioBackend,
    CallbackStatus,
    CaptureProfile,
    InputStreamHandle,
    StreamError,
    StreamStats,
)
from mom_igd.audio.frame_queue import DEFAULT_QUEUE_SECONDS, BoundedFrameQueue
from mom_igd.audio.manifest import ChunkRecord, ManifestWriter, utc_now_iso
from mom_igd.audio.quality import QualityMeter, QualitySnapshot
from mom_igd.audio.writer import ChunkWriter, FinalisedChunk, WriterError
from mom_igd.logging_setup import get_logger

__all__ = ["CaptureSession", "SessionResult", "SessionState", "WRITER_THREAD_NAME"]

_LOG = get_logger("audio.session")

WRITER_THREAD_NAME: Final[str] = "mom-igd-audio-writer"
_QUEUE_POLL_SECONDS: Final[float] = 0.05
_JOIN_TIMEOUT_SECONDS: Final[float] = 15.0


class SessionState(StrEnum):
    """Where the capture engine is. Distinct from the recording lifecycle."""

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


@dataclass(slots=True)
class SessionResult:
    """What a finished session produced."""

    state: SessionState
    frames_captured: int = 0
    frames_written: int = 0
    dropped_frames: int = 0
    xrun_callbacks: int = 0
    chunks: list[ChunkRecord] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    degraded: bool = False
    wall_seconds: float = 0.0
    audio_seconds: float = 0.0

    @property
    def drift_percent(self) -> float:
        """Difference between wall-clock and captured audio duration.

        Measured against a monotonic clock. A healthy capture stays well under
        0.1%; a larger value means frames went missing somewhere.
        """
        if self.wall_seconds <= 0:
            return 0.0
        return 100.0 * abs(self.wall_seconds - self.audio_seconds) / self.wall_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "frames_captured": self.frames_captured,
            "frames_written": self.frames_written,
            "dropped_frames": self.dropped_frames,
            "xrun_callbacks": self.xrun_callbacks,
            "chunk_count": len(self.chunks),
            "gaps": list(self.gaps),
            "error": self.error,
            "degraded": self.degraded,
            "wall_seconds": round(self.wall_seconds, 3),
            "audio_seconds": round(self.audio_seconds, 3),
            "drift_percent": round(self.drift_percent, 4),
        }


class CaptureSession:
    """Runs one capture from stream open to final chunk.

    Opening the stream is the only point at which the microphone is engaged, and
    it happens in :meth:`start` -- never on construction, so building a session
    object is side-effect free.
    """

    def __init__(
        self,
        backend: AudioBackend,
        *,
        device_index: int,
        profile: CaptureProfile,
        directory: Path,
        recording_uuid: str = "",
        queue_seconds: float = DEFAULT_QUEUE_SECONDS,
        manifest: ManifestWriter | None = None,
        on_chunk: Callable[[FinalisedChunk], None] | None = None,
        meter_stride: int = 1,
        start_seq: int = 0,
        live_tap: Callable[[bytes], None] | None = None,
    ) -> None:
        self._backend = backend
        self._device_index = device_index
        self._profile = profile
        self._directory = directory
        self._recording_uuid = recording_uuid
        self._manifest = manifest
        self._on_chunk = on_chunk
        self._live_tap = live_tap
        self._tap_failures = 0

        self._queue = BoundedFrameQueue(profile, capacity_seconds=queue_seconds)
        self._meter = QualityMeter(profile, stride=meter_stride)
        self._writer = ChunkWriter(
            directory,
            profile,
            recording_uuid=recording_uuid,
            start_seq=start_seq,
            on_finalised=self._handle_finalised,
        )
        self._stream_stats = StreamStats()

        self._state = SessionState.IDLE
        self._state_lock = threading.Lock()
        # Guards every ChunkWriter and QualityMeter access. Neither is
        # thread-safe, and both are reached from two threads: the writer thread
        # while capturing, and the controlling thread when pausing or stopping.
        # Without this, a pause could interleave PCM from two threads into the
        # same chunk. The audio callback never takes this lock, so the real-time
        # path is untouched; the writer holds it only for the duration of one
        # block write.
        self._writer_lock = threading.Lock()
        self._stream: InputStreamHandle | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Set while the writer thread has nothing in flight. "Queue is empty" is
        # not sufficient to know the writer has finished: it pops an item and only
        # then writes it, so a pause that trusted queue depth alone could finalise
        # a chunk while the last block was still on its way into it.
        self._writer_idle = threading.Event()
        self._writer_idle.set()
        self._error: str | None = None
        self._degraded = False
        self._pending_xruns = 0
        self._pending_dropped = 0

        self._monotonic_start_ns = 0
        self._monotonic_end_ns = 0
        self._paused_ns_total = 0
        self._pause_started_ns = 0
        self._gaps: list[dict[str, Any]] = []
        self._chunks: list[ChunkRecord] = []

    # -- introspection ------------------------------------------------------

    @property
    def state(self) -> SessionState:
        with self._state_lock:
            return self._state

    @property
    def profile(self) -> CaptureProfile:
        return self._profile

    @property
    def degraded(self) -> bool:
        return self._degraded or self._queue.dropped_frames > 0

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def frames_captured(self) -> int:
        return self._stream_stats.frames_delivered

    @property
    def frames_written(self) -> int:
        return self._writer.total_frames

    @property
    def chunks(self) -> list[ChunkRecord]:
        return list(self._chunks)

    def elapsed_seconds(self) -> float:
        """Wall time excluding paused intervals, from a monotonic clock."""
        if self._monotonic_start_ns == 0:
            return 0.0
        end = self._monotonic_end_ns or time.monotonic_ns()
        paused = self._paused_ns_total
        if self._pause_started_ns:
            paused += time.monotonic_ns() - self._pause_started_ns
        return max(0.0, (end - self._monotonic_start_ns - paused) / 1_000_000_000.0)

    def writer_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def quality(self) -> QualitySnapshot:
        with self._writer_lock:
            return self._meter.rolling_snapshot()

    def cumulative_quality(self) -> QualitySnapshot:
        with self._writer_lock:
            return self._meter.cumulative_snapshot()

    def status(self) -> dict[str, Any]:
        """A cheap snapshot, safe to poll at a few hertz. Exposes no path."""
        queue_stats = self._queue.stats()
        return {
            "state": self.state.value,
            "degraded": self.degraded,
            "error": self._error,
            "elapsed_seconds": round(self.elapsed_seconds(), 2),
            "frames_captured": self.frames_captured,
            "frames_written": self.frames_written,
            "audio_seconds": round(self.frames_written / self._profile.sample_rate, 2),
            "current_chunk_seq": self._writer.current_seq,
            "current_chunk_progress": round(self._writer.chunk_progress(), 3),
            "chunks_finalised": self._writer.stats.chunks_finalised,
            "bytes_written": self._writer.stats.bytes_written,
            "writer_alive": self.writer_alive(),
            "queue": queue_stats.to_dict(),
            "stream": self._stream_stats.snapshot(),
            "profile": self._profile.describe(),
            "gaps": list(self._gaps),
            "paused_seconds": round(self._paused_ns_total / 1_000_000_000.0, 2),
        }

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Open the stream, start the writer thread, begin capturing.

        This is the moment the microphone is engaged.
        """
        with self._state_lock:
            if self._state is SessionState.RUNNING:
                return  # idempotent: a second Start must not open a second stream
            if self._state is not SessionState.IDLE:
                raise StreamError(
                    f"Cannot start a capture session from state {self._state.value}."
                )
            self._state = SessionState.RUNNING

        self._directory.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        thread = threading.Thread(target=self._writer_loop, name=WRITER_THREAD_NAME, daemon=True)
        self._thread = thread
        thread.start()

        try:
            self._stream = self._backend.open_input_stream(
                self._device_index, self._profile, self._on_audio
            )
            self._stream.start()
        except Exception as exc:
            self._fail(f"could not start capture: {exc}")
            self._shutdown_writer()
            raise

        self._monotonic_start_ns = time.monotonic_ns()
        if self._manifest is not None:
            self._manifest.append_event(
                "recording_opened",
                recording_uuid=self._recording_uuid,
                profile=self._profile.describe(),
                device_index=self._device_index,
            )
        _LOG.info(
            "Capture started: %d Hz, %d ch, %d s chunks",
            self._profile.sample_rate,
            self._profile.channels,
            self._profile.chunk_seconds,
        )

    def pause(self) -> None:
        """Stop the stream, finalise the open chunk, and record the gap."""
        with self._state_lock:
            if self._state is SessionState.PAUSED:
                return
            if self._state is not SessionState.RUNNING:
                raise StreamError(f"Cannot pause from state {self._state.value}.")
            self._state = SessionState.PAUSED

        self._stop_stream()
        # The stream is stopped, so no more audio can arrive. Let the writer
        # thread finish what is already queued rather than writing from this
        # thread alongside it.
        self._await_queue_drain()
        with self._writer_lock:
            self._writer.finalise_current(quality=self._chunk_quality_locked())
        self._pause_started_ns = time.monotonic_ns()
        if self._manifest is not None:
            self._manifest.append_event(
                "paused", frames_written=self._writer.total_frames
            )
        _LOG.info("Capture paused after %d frames.", self._writer.total_frames)

    def resume(self) -> None:
        """Reopen the stream and continue into a new chunk."""
        with self._state_lock:
            if self._state is SessionState.RUNNING:
                return
            if self._state is not SessionState.PAUSED:
                raise StreamError(f"Cannot resume from state {self._state.value}.")
            self._state = SessionState.RUNNING

        paused_ns = time.monotonic_ns() - self._pause_started_ns if self._pause_started_ns else 0
        self._paused_ns_total += paused_ns
        self._pause_started_ns = 0
        gap = {
            "type": "gap",
            "reason": "paused",
            "at_frame": self._writer.total_frames,
            "duration_seconds": round(paused_ns / 1_000_000_000.0, 3),
            "intentional": True,
            "utc": utc_now_iso(),
        }
        self._gaps.append(gap)
        if self._manifest is not None:
            self._manifest.append(gap)

        try:
            self._stream = self._backend.open_input_stream(
                self._device_index, self._profile, self._on_audio
            )
            self._stream.start()
        except Exception as exc:
            self._fail(f"could not resume capture: {exc}")
            raise
        _LOG.info("Capture resumed after %.1f s paused.", paused_ns / 1e9)

    def stop(self) -> SessionResult:
        """Stop capturing and finalise everything. Idempotent.

        The early-exit decision is taken inside the lock and the result is built
        after releasing it. Building it inside would re-enter ``_state_lock``
        through the ``state`` property, and ``threading.Lock`` is not reentrant --
        that deadlocked the second call to ``stop()``.
        """
        with self._state_lock:
            already_finished = self._state in {SessionState.STOPPED, SessionState.FAILED}
            never_started = self._state is SessionState.IDLE
            if never_started:
                self._state = SessionState.STOPPED
            elif not already_finished:
                self._state = SessionState.STOPPING
        if already_finished or never_started:
            return self._result()

        self._stop_stream()
        self._await_queue_drain()
        self._shutdown_writer()

        try:
            with self._writer_lock:
                self._writer.close(quality=self._chunk_quality_locked())
        except WriterError as exc:
            self._error = self._error or str(exc)
            self._degraded = True
            _LOG.error("Finalising the last chunk failed: %s", exc)

        self._monotonic_end_ns = time.monotonic_ns()
        if self._pause_started_ns:
            self._paused_ns_total += self._monotonic_end_ns - self._pause_started_ns
            self._pause_started_ns = 0

        with self._state_lock:
            if self._error is not None:
                self._state = SessionState.FAILED
            else:
                self._state = SessionState.STOPPED

        if self._manifest is not None:
            self._manifest.append_event(
                "recording_closed",
                frames_written=self._writer.total_frames,
                dropped_frames=self._queue.dropped_frames,
                degraded=self.degraded,
            )
        return self._result()

    def abandon(self) -> SessionResult:
        """Stop without finalising the open chunk, leaving it for recovery.

        Used when the device disappears or the writer fails: whatever is already
        finalised stays valid, and the partial is preserved so the next start can
        salvage it.
        """
        with self._state_lock:
            already_finished = self._state in {SessionState.STOPPED, SessionState.FAILED}
            if not already_finished:
                self._state = SessionState.STOPPING
        if already_finished:
            return self._result()
        self._stop_stream()
        self._await_queue_drain()
        self._shutdown_writer()
        with self._writer_lock:
            self._writer.abandon()
        self._monotonic_end_ns = time.monotonic_ns()
        with self._state_lock:
            self._state = SessionState.FAILED
        return self._result()

    # -- audio callback (real-time thread) ---------------------------------

    def _on_audio(self, pcm: bytes, frames: int, status: CallbackStatus) -> None:
        """Copy and enqueue. Nothing else may ever happen here."""
        self._stream_stats.record(frames, status)
        if not status.is_clean:
            self._pending_xruns += 1
        if not self._queue.put_nowait(pcm, frames):
            self._pending_dropped += frames

    # -- writer thread ------------------------------------------------------

    def _writer_loop(self) -> None:
        try:
            while True:
                item = self._queue.get(timeout=_QUEUE_POLL_SECONDS)
                if item is None:
                    self._writer_idle.set()
                    if self._stop_event.is_set():
                        break
                    continue
                self._writer_idle.clear()
                try:
                    self._consume(item)
                finally:
                    if len(self._queue) == 0:
                        self._writer_idle.set()
        except WriterError as exc:
            self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - a writer crash must be reported
            self._fail(f"writer thread failed: {type(exc).__name__}: {exc}")
        finally:
            _LOG.debug("Writer thread exiting after %d frames.", self._writer.total_frames)

    def _consume(self, item: tuple[bytes, int]) -> None:
        pcm, _frames = item
        xruns, self._pending_xruns = self._pending_xruns, 0
        dropped, self._pending_dropped = self._pending_dropped, 0
        if dropped:
            self._degraded = True
            self._record_loss(dropped)
        with self._writer_lock:
            self._writer.write(pcm, xrun_callbacks=xruns, dropped_frames=dropped)
            self._meter.add(pcm)
        self._feed_live_tap(pcm)

    def _feed_live_tap(self, pcm: bytes) -> None:
        """Hand a copy of the audio to the live preview. **Never blocks, never raises.**

        Three properties, and every one of them exists to protect the recording:

        * It runs **after** the master write and **outside** ``_writer_lock``, so a slow
          consumer cannot delay a byte of evidence or hold a lock the writer needs.
        * The tap is expected to be non-blocking and to discard what it cannot keep up
          with. A live preview that falls behind loses preview text; the master is
          already on disk by the time this is called and cannot lose anything.
        * Any exception is swallowed. A preview feature must not be able to fail a
          meeting -- the first failure is logged and the rest are counted, because a tap
          that throws on every block would otherwise fill the log with the same line.

        This is deliberately not on the device callback. That callback copies and
        enqueues and does nothing else (ADR-0006); putting a transcriber's queue on it
        would put a dropped frame in real audio behind a preview feature.
        """
        tap = self._live_tap
        if tap is None:
            return
        try:
            tap(pcm)
        except Exception as exc:  # noqa: BLE001 - a preview must never break a capture
            self._tap_failures += 1
            if self._tap_failures == 1:
                _LOG.warning(
                    "Live preview tap failed and will be ignored for the rest of this "
                    "recording: %s: %s. The recording itself is unaffected.",
                    type(exc).__name__,
                    exc,
                )
            self._live_tap = None

    def _record_loss(self, dropped: int) -> None:
        gap = {
            "type": "gap",
            "reason": "queue_overflow",
            "at_frame": self._writer.total_frames,
            "dropped_frames": dropped,
            "duration_seconds": round(dropped / self._profile.sample_rate, 4),
            "intentional": False,
            "utc": utc_now_iso(),
        }
        self._gaps.append(gap)
        if self._manifest is not None:
            self._manifest.append(gap)
        _LOG.warning(
            "Audio loss: %d frame(s) dropped because the writer could not keep up.",
            dropped,
        )

    def _await_queue_drain(self, timeout: float = 10.0) -> bool:
        """Wait for the writer thread to consume everything already queued.

        Preferred over writing from the calling thread: two threads feeding one
        :class:`~mom_igd.audio.writer.ChunkWriter` could interleave PCM within a
        chunk. Returns ``True`` if the queue emptied.

        If the writer thread is not running (it crashed, or was never started),
        the remaining blocks are written here instead -- at that point this is the
        only thread that can, so there is nobody to interleave with.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self._queue) == 0 and self._writer_idle.is_set():
                return True
            if not self.writer_alive():
                break
            time.sleep(_QUEUE_POLL_SECONDS / 2.0)
        return self._drain_into_writer()

    def _drain_into_writer(self) -> bool:
        """Write whatever is still queued from the calling thread.

        Only safe once the writer thread has exited, or when it never ran.
        """
        drained = True
        for item in self._queue.drain():
            try:
                self._consume(item)
            except WriterError as exc:
                self._fail(str(exc))
                drained = False
                break
        return drained

    # -- internals ----------------------------------------------------------

    def _handle_finalised(self, finalised: FinalisedChunk) -> None:
        self._chunks.append(finalised.record)
        if self._manifest is not None:
            self._manifest.append_chunk(finalised.record)
        if self._on_chunk is not None:
            self._on_chunk(finalised)

    def _chunk_quality_locked(self) -> dict[str, Any]:
        """Quality summary for a chunk record. Call with ``_writer_lock`` held."""
        snapshot = self._meter.rolling_snapshot()
        return {
            "peak_dbfs": snapshot.peak_dbfs,
            "rms_dbfs": snapshot.rms_dbfs,
            "clipped_samples": snapshot.clipped_samples,
        }

    def _stop_stream(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.stop()
        except Exception as exc:  # noqa: BLE001 - a failed stop must not block finalising
            _LOG.warning("Stopping the audio stream raised: %s", exc)
        finally:
            try:
                stream.close()
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("Closing the audio stream raised: %s", exc)

    def _shutdown_writer(self) -> None:
        """Signal the writer thread, let it drain, then join it."""
        self._stop_event.set()
        self._queue.close()
        thread = self._thread
        self._thread = None
        if thread is None:
            return
        thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
        if thread.is_alive():  # pragma: no cover - would mean the writer wedged
            self._degraded = True
            self._error = self._error or (
                "the writer thread did not exit within "
                f"{_JOIN_TIMEOUT_SECONDS} s; audio already finalised is unaffected"
            )
            _LOG.error("Writer thread failed to exit.")
        # Anything the thread left behind is written on this thread.
        self._drain_into_writer()

    def _fail(self, message: str) -> None:
        self._error = self._error or message
        self._degraded = True
        with self._state_lock:
            if self._state not in {SessionState.STOPPED, SessionState.STOPPING}:
                self._state = SessionState.FAILED
        _LOG.error("Capture session failed: %s", message)

    def _result(self) -> SessionResult:
        wall = self.elapsed_seconds()
        audio = self._writer.total_frames / self._profile.sample_rate
        return SessionResult(
            state=self.state,
            frames_captured=self._stream_stats.frames_delivered,
            frames_written=self._writer.total_frames,
            dropped_frames=self._queue.dropped_frames,
            xrun_callbacks=self._stream_stats.xrun_callbacks,
            chunks=list(self._chunks),
            gaps=list(self._gaps),
            error=self._error,
            degraded=self.degraded,
            wall_seconds=wall,
            audio_seconds=audio,
        )

    @property
    def writer_stats(self) -> dict[str, Any]:
        return self._writer.stats.to_dict()

    @property
    def queue_stats(self) -> dict[str, Any]:
        return self._queue.stats().to_dict()
