"""Crash-safe chunk writer.

The durability order is the whole point of this module, and it is not negotiable:

1. raw PCM is appended to ``chunk_NNNNNN.pcm.part``;
2. ``chunk_NNNNNN.meta.json`` records the format needed to interpret that partial
   *before* any audio goes into it, so recovery is never left guessing;
3. at a chunk boundary the partial is flushed and ``fsync``ed;
4. a valid PCM WAV is built at ``chunk_NNNNNN.wav.tmp`` from the partial's whole
   frames;
5. the temporary WAV is ``fsync``ed and hashed with SHA-256, streaming;
6. ``os.replace`` moves it into place -- atomic, because it stays on the same
   volume;
7. the caller commits the database row and the manifest line;
8. only then are the partial and its metadata removed.

Two consequences follow from that ordering, and both matter.

**Any ``.wav`` file that exists is complete.** It was renamed into place only
after being fully written, flushed and hashed. A crash can leave a `.part` or a
`.tmp` behind, never a half-written `.wav`.

**The partial is raw PCM, not a WAV.** A WAV needs its header patched with the
final length, so a crash mid-recording would leave an invalid file. Raw PCM has no
header to patch: recovery reads whole frames from the front, discards a trailing
fragment that is not a complete frame, and wraps the result in a fresh header.
"""

from __future__ import annotations

import json
import os
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Final

from mom_igd.audio.backend import CaptureProfile
from mom_igd.audio.manifest import (
    CHUNK_META_SUFFIX,
    PARTIAL_SUFFIX,
    ChunkRecord,
    ChunkStatus,
    RecoveryStatus,
    chunk_filename,
    sha256_file,
    utc_now_iso,
)
from mom_igd.logging_setup import get_logger

__all__ = [
    "ChunkWriter",
    "FinalisedChunk",
    "WriterError",
    "build_wav_from_pcm",
    "partial_meta_path",
    "partial_path",
    "read_partial_meta",
    "write_partial_meta",
]

_LOG = get_logger("audio.writer")
_COPY_BLOCK: Final[int] = 1024 * 1024
_TMP_SUFFIX: Final[str] = ".wav.tmp"


class WriterError(RuntimeError):
    """Raised when a chunk cannot be written or finalised."""


def partial_path(directory: Path, seq: int) -> Path:
    return directory / f"{chunk_filename(seq).removesuffix('.wav')}{PARTIAL_SUFFIX}"


def partial_meta_path(directory: Path, seq: int) -> Path:
    return directory / f"{chunk_filename(seq).removesuffix('.wav')}{CHUNK_META_SUFFIX}"


def write_partial_meta(
    directory: Path,
    seq: int,
    profile: CaptureProfile,
    *,
    start_frame: int,
    utc_start: str,
    monotonic_start_ns: int,
    recording_uuid: str = "",
) -> Path:
    """Record how to interpret a partial file, before any audio is written to it.

    Without this, a partial left by a crash is an anonymous blob of bytes: the
    sample rate, channel count and sample format are all unknowable, and the audio
    would be unrecoverable in practice.
    """
    path = partial_meta_path(directory, seq)
    payload = {
        "seq": seq,
        "recording_uuid": recording_uuid,
        "start_frame": start_frame,
        "utc_start": utc_start,
        "monotonic_start_ns": monotonic_start_ns,
        "sample_rate": profile.sample_rate,
        "channels": profile.channels,
        "sample_format": profile.sample_format.value,
        "bytes_per_frame": profile.bytes_per_frame,
    }
    text = json.dumps(payload, sort_keys=True, indent=2)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def read_partial_meta(directory: Path, seq: int) -> dict[str, Any] | None:
    """Read a partial's recovery metadata, or ``None`` if it is absent/unusable."""
    path = partial_meta_path(directory, seq)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _LOG.warning("Chunk %d recovery metadata is unreadable: %s", seq, exc)
        return None
    if not isinstance(payload, dict):
        return None
    for required in ("sample_rate", "channels", "bytes_per_frame"):
        if required not in payload:
            return None
    return payload


def build_wav_from_pcm(
    source: Path,
    target: Path,
    *,
    channels: int,
    sample_width: int,
    sample_rate: int,
    max_frames: int | None = None,
) -> tuple[int, int]:
    """Wrap raw PCM in a valid WAV container.

    Only whole frames are copied: a trailing fragment that does not complete a
    frame is left out, because a partial frame is not audio.

    Returns:
        ``(frames_written, trailing_bytes_discarded)``.
    """
    bytes_per_frame = channels * sample_width
    if bytes_per_frame <= 0:
        raise WriterError(f"Invalid frame size: {channels} ch x {sample_width} bytes.")

    total_bytes = source.stat().st_size
    usable_frames = total_bytes // bytes_per_frame
    if max_frames is not None:
        usable_frames = min(usable_frames, max_frames)
    usable_bytes = usable_frames * bytes_per_frame
    trailing = total_bytes - usable_bytes

    with source.open("rb") as raw_in, target.open("wb") as raw_out:
        with wave.open(raw_out, "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(sample_width)
            wav.setframerate(sample_rate)
            remaining = usable_bytes
            while remaining > 0:
                block = raw_in.read(min(_COPY_BLOCK, remaining))
                if not block:
                    break
                remaining -= len(block)
                wav.writeframes(block)
        # `wave` does not close a file object it did not open, so the descriptor
        # is still ours and can be forced to disk before it is released.
        raw_out.flush()
        os.fsync(raw_out.fileno())

    return usable_frames, trailing


@dataclass(frozen=True, slots=True)
class FinalisedChunk:
    """A chunk that is on disk, hashed, and safe to record in the database."""

    record: ChunkRecord
    path: Path
    trailing_bytes_discarded: int = 0
    finalise_seconds: float = 0.0


@dataclass(slots=True)
class _OpenChunk:
    seq: int
    start_frame: int
    utc_start: str
    monotonic_start_ns: int
    handle: Any
    path: Path
    frames: int = 0
    bytes_written: int = 0
    xrun_callbacks: int = 0
    dropped_frames: int = 0


@dataclass(slots=True)
class WriterStats:
    """Counters for diagnostics and the soak report."""

    chunks_finalised: int = 0
    frames_written: int = 0
    bytes_written: int = 0
    trailing_bytes_discarded: int = 0
    finalise_seconds_total: float = 0.0
    finalise_seconds_max: float = 0.0
    write_seconds_total: float = 0.0
    write_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunks_finalised": self.chunks_finalised,
            "frames_written": self.frames_written,
            "bytes_written": self.bytes_written,
            "trailing_bytes_discarded": self.trailing_bytes_discarded,
            "finalise_seconds_total": round(self.finalise_seconds_total, 4),
            "finalise_seconds_max": round(self.finalise_seconds_max, 4),
            "write_seconds_total": round(self.write_seconds_total, 4),
            "write_calls": self.write_calls,
            "mean_write_ms": (
                round(1000.0 * self.write_seconds_total / self.write_calls, 4)
                if self.write_calls
                else 0.0
            ),
        }


class ChunkWriter:
    """Writes PCM into rotating, individually verifiable WAV chunks.

    Used from the single writer thread only. Never touched by the audio callback.
    """

    def __init__(
        self,
        directory: Path,
        profile: CaptureProfile,
        *,
        recording_uuid: str = "",
        start_seq: int = 0,
        on_finalised: Callable[[FinalisedChunk], None] | None = None,
    ) -> None:
        self._directory = directory
        self._profile = profile
        self._recording_uuid = recording_uuid
        self._next_seq = start_seq
        self._on_finalised = on_finalised
        self._current: _OpenChunk | None = None
        self._carry = b""
        self._total_frames = 0
        self.stats = WriterStats()
        self.finalised: list[ChunkRecord] = []

    # -- introspection ------------------------------------------------------

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def current_seq(self) -> int | None:
        return self._current.seq if self._current else None

    @property
    def current_frames(self) -> int:
        return self._current.frames if self._current else 0

    @property
    def total_frames(self) -> int:
        return self._total_frames

    @property
    def next_seq(self) -> int:
        return self._next_seq

    def chunk_progress(self) -> float:
        """How full the open chunk is, 0.0-1.0."""
        if self._current is None or self._profile.frames_per_chunk == 0:
            return 0.0
        return min(1.0, self._current.frames / self._profile.frames_per_chunk)

    # -- writing ------------------------------------------------------------

    def write(self, pcm: bytes, *, xrun_callbacks: int = 0, dropped_frames: int = 0) -> int:
        """Append PCM, rotating at exact chunk boundaries.

        A block that straddles a boundary is split precisely on the frame
        boundary, so every chunk holds exactly ``frames_per_chunk`` frames and the
        recorded frame ranges are contiguous with no rounding.

        Returns:
            Frames written.

        Raises:
            WriterError: If the underlying file cannot be written (disk full,
                device removed, permissions).
        """
        if not pcm:
            return 0
        started = time.perf_counter()
        bytes_per_frame = self._profile.bytes_per_frame
        buffer = self._carry + pcm if self._carry else pcm
        self._carry = b""

        usable = (len(buffer) // bytes_per_frame) * bytes_per_frame
        if usable != len(buffer):
            # Keep a fragment that does not complete a frame; it will be joined
            # with the next block rather than written as if it were audio.
            self._carry = buffer[usable:]
            buffer = buffer[:usable]
        if not buffer:
            return 0

        view = memoryview(buffer)
        offset = 0
        frames_written = 0
        try:
            while offset < len(view):
                if self._current is None:
                    self._open_next()
                assert self._current is not None  # noqa: S101 - invariant after _open_next
                space_frames = self._profile.frames_per_chunk - self._current.frames
                if space_frames <= 0:
                    self._rotate(xrun_callbacks=xrun_callbacks, dropped_frames=dropped_frames)
                    continue
                take_bytes = min(space_frames * bytes_per_frame, len(view) - offset)
                block = view[offset : offset + take_bytes]
                self._current.handle.write(block)
                taken_frames = take_bytes // bytes_per_frame
                self._current.frames += taken_frames
                self._current.bytes_written += take_bytes
                self._current.xrun_callbacks += xrun_callbacks
                self._current.dropped_frames += dropped_frames
                xrun_callbacks = 0
                dropped_frames = 0
                self._total_frames += taken_frames
                frames_written += taken_frames
                offset += take_bytes
                if self._current.frames >= self._profile.frames_per_chunk:
                    self._rotate()
        except OSError as exc:
            raise WriterError(
                f"Writing audio failed after {self._total_frames} frames: {exc}. "
                "Audio already finalised on disk is unaffected."
            ) from exc
        finally:
            elapsed = time.perf_counter() - started
            self.stats.write_seconds_total += elapsed
            self.stats.write_calls += 1
            self.stats.frames_written += frames_written
        return frames_written

    # -- chunk lifecycle ----------------------------------------------------

    def _open_next(self) -> None:
        seq = self._next_seq
        self._next_seq += 1
        self._directory.mkdir(parents=True, exist_ok=True)
        path = partial_path(self._directory, seq)
        utc_start = utc_now_iso()
        monotonic_start_ns = time.monotonic_ns()
        # Metadata FIRST: a partial with no metadata is unrecoverable.
        write_partial_meta(
            self._directory,
            seq,
            self._profile,
            start_frame=self._total_frames,
            utc_start=utc_start,
            monotonic_start_ns=monotonic_start_ns,
            recording_uuid=self._recording_uuid,
        )
        try:
            handle = path.open("wb")
        except OSError as exc:
            raise WriterError(f"Cannot open chunk {seq} for writing: {exc}") from exc
        self._current = _OpenChunk(
            seq=seq,
            start_frame=self._total_frames,
            utc_start=utc_start,
            monotonic_start_ns=monotonic_start_ns,
            handle=handle,
            path=path,
        )
        _LOG.debug("Opened chunk %d at frame %d", seq, self._total_frames)

    def _rotate(self, *, xrun_callbacks: int = 0, dropped_frames: int = 0) -> None:
        self.finalise_current(xrun_callbacks=xrun_callbacks, dropped_frames=dropped_frames)

    def finalise_current(
        self,
        *,
        xrun_callbacks: int = 0,
        dropped_frames: int = 0,
        quality: dict[str, Any] | None = None,
    ) -> FinalisedChunk | None:
        """Turn the open partial into a verified WAV chunk.

        Returns ``None`` when there is nothing to finalise, or when the open chunk
        holds zero frames -- an empty chunk is discarded rather than written as a
        zero-length WAV that would only confuse verification later.
        """
        current = self._current
        if current is None:
            return None
        self._current = None
        started = time.perf_counter()

        current.xrun_callbacks += xrun_callbacks
        current.dropped_frames += dropped_frames

        # Step 3: flush and fsync the partial before reading it back.
        try:
            current.handle.flush()
            os.fsync(current.handle.fileno())
        except OSError as exc:  # pragma: no cover - disk failure path
            _LOG.error("Could not fsync chunk %d partial: %s", current.seq, exc)
        finally:
            try:
                current.handle.close()
            except OSError:  # pragma: no cover
                pass

        if current.frames == 0:
            # Nothing captured: remove the empty partial and its metadata.
            self._discard_partial(current.seq)
            return None

        final_path = self._directory / chunk_filename(current.seq)
        temp_path = self._directory / (
            chunk_filename(current.seq).removesuffix(".wav") + _TMP_SUFFIX
        )

        # Step 4-5: build a valid WAV, fsync it, then hash what is on disk.
        try:
            frames, trailing = build_wav_from_pcm(
                current.path,
                temp_path,
                channels=self._profile.channels,
                sample_width=self._profile.sample_format.bytes_per_sample,
                sample_rate=self._profile.sample_rate,
            )
        except OSError as exc:
            raise WriterError(
                f"Could not build WAV for chunk {current.seq}: {exc}. The raw "
                f"partial is preserved at {current.path.name} for recovery."
            ) from exc

        digest = sha256_file(temp_path)
        byte_count = temp_path.stat().st_size

        # Step 6: atomic rename within the same directory (same volume).
        if final_path.exists():
            # Never overwrite audio that is already final.
            temp_path.unlink(missing_ok=True)
            raise WriterError(
                f"Refusing to overwrite an existing final chunk: {final_path.name}. "
                "This indicates a sequence collision; the new data is discarded "
                "rather than destroying audio that is already verified."
            )
        os.replace(temp_path, final_path)

        monotonic_end_ns = time.monotonic_ns()
        record = ChunkRecord(
            seq=current.seq,
            filename=final_path.name,
            start_frame=current.start_frame,
            end_frame=current.start_frame + frames,
            frame_count=frames,
            duration_ms=round(self._profile.frames_to_ms(frames), 3),
            utc_start=current.utc_start,
            utc_end=utc_now_iso(),
            monotonic_start_ns=current.monotonic_start_ns,
            monotonic_end_ns=monotonic_end_ns,
            sample_rate=self._profile.sample_rate,
            channels=self._profile.channels,
            sample_format=self._profile.sample_format.value,
            byte_count=byte_count,
            sha256=digest,
            xrun_callbacks=current.xrun_callbacks,
            dropped_frames=current.dropped_frames,
            status=ChunkStatus.WRITTEN.value,
            recovery_status=RecoveryStatus.NONE.value,
            finalized=True,
            peak_dbfs=(quality or {}).get("peak_dbfs"),
            rms_dbfs=(quality or {}).get("rms_dbfs"),
            clipped_samples=int((quality or {}).get("clipped_samples", 0) or 0),
        )

        # Step 8: the partial goes only once the final file is proven present.
        if final_path.is_file() and final_path.stat().st_size == byte_count:
            self._discard_partial(current.seq)
        else:  # pragma: no cover - would mean os.replace lied
            _LOG.error(
                "Chunk %d final file failed verification after rename; keeping the "
                "partial as evidence.",
                current.seq,
            )

        elapsed = time.perf_counter() - started
        self.stats.chunks_finalised += 1
        self.stats.bytes_written += byte_count
        self.stats.trailing_bytes_discarded += trailing
        self.stats.finalise_seconds_total += elapsed
        self.stats.finalise_seconds_max = max(self.stats.finalise_seconds_max, elapsed)
        self.finalised.append(record)

        finalised = FinalisedChunk(
            record=record,
            path=final_path,
            trailing_bytes_discarded=trailing,
            finalise_seconds=elapsed,
        )
        if self._on_finalised is not None:
            self._on_finalised(finalised)
        return finalised

    def close(self, *, quality: dict[str, Any] | None = None) -> FinalisedChunk | None:
        """Finalise whatever is open and release the carry buffer."""
        result = self.finalise_current(quality=quality)
        if self._carry:
            _LOG.warning(
                "Discarding %d trailing byte(s) that do not form a complete frame.",
                len(self._carry),
            )
            self.stats.trailing_bytes_discarded += len(self._carry)
            self._carry = b""
        return result

    def abandon(self) -> None:
        """Close the open handle without finalising, leaving the partial in place.

        Used when the recording fails: the partial and its metadata stay on disk so
        the recovery service can salvage the audio on the next start.
        """
        current = self._current
        self._current = None
        if current is None:
            return
        try:
            current.handle.flush()
            os.fsync(current.handle.fileno())
        except OSError:  # pragma: no cover
            pass
        finally:
            try:
                current.handle.close()
            except OSError:  # pragma: no cover
                pass
        _LOG.warning(
            "Chunk %d left as a partial for recovery (%d frames captured).",
            current.seq,
            current.frames,
        )

    # -- helpers ------------------------------------------------------------

    def _discard_partial(self, seq: int) -> None:
        for path in (partial_path(self._directory, seq), partial_meta_path(self._directory, seq)):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover - locked file
                _LOG.warning("Could not remove %s: %s", path.name, exc)
