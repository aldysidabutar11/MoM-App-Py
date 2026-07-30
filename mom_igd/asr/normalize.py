"""Build the 16 kHz mono working copy the models read, from the master recording.

**The master is never touched.** It is opened read-only, and everything this module
produces is a new file under ``<data_root>/working``. If a working copy is lost, it is
rebuilt from the master; if the master is lost, nothing here can help.

**Why a working copy exists at all.** Phase 2 captures at the device's native rate,
which on the target hardware is 48 kHz, possibly stereo, split across chunk files.
Whisper's feature extractor wants 16 kHz mono. Handing the engine a 48 kHz file works --
it resamples internally -- but then the resampling is invisible, unversioned and
unmeasured, and the audio the model actually saw is not something anybody can inspect.
Doing it here makes it a stage with an input hash, an output hash and a duration.

**Why gaps become silence here and never in the master.** Phase 2's rule is that a gap
in the master is recorded and never filled, because the master is evidence. The reason
given in CLAUDE.md is that an invisible gap shifts every downstream timestamp. A working
copy is not evidence -- it is the model's input, and its entire job is to carry the
master's timeline. So each chunk is placed at the frame offset the manifest recorded for
it and a hole becomes explicit silence, which keeps transcript timestamps equal to
offsets into the meeting. Every filled gap is recorded on the working-copy row and any
speech region overlapping one is flagged, so the fill is visible to the reviewer. Filled
*and* recorded is the only combination that keeps both the timeline and the truth.

**Resampling.** Linear interpolation over integer sample positions, in pure Python
against ``array``. Not because it is the best resampler -- it is not -- but because it is
auditable, deterministic, dependency-free and adequate for speech at these rates. The
alternative was pulling ``scipy`` or handing the file to ``av``; ``av`` is a declared
dependency and could do it, but then the working copy's content would depend on an
FFmpeg build rather than on code in this repository. A test asserts a known-frequency
tone survives the conversion, and the ADR records the trade.
"""

from __future__ import annotations

import array
import math
import os
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable, Sequence

from mom_igd.logging_setup import get_logger

__all__ = [
    "WORKING_CHANNELS",
    "WORKING_SAMPLE_RATE",
    "WORKING_SAMPLE_WIDTH",
    "Gap",
    "NormalizationError",
    "NormalizationResult",
    "downmix_to_mono",
    "normalize_recording",
    "resample_linear",
]

_LOG = get_logger("asr.normalize")

#: The working-copy format. Fixed by the ASR stack, not configurable -- the database
#: enforces the same three values with a CHECK constraint.
WORKING_SAMPLE_RATE: Final[int] = 16_000
WORKING_CHANNELS: Final[int] = 1
WORKING_SAMPLE_WIDTH: Final[int] = 2

#: How much audio to convert per pass. 4 MiB of int16 is about 20 s of 48 kHz stereo:
#: large enough that the per-block overhead is irrelevant, small enough that a
#: three-hour recording never needs more than a few megabytes of resident memory.
_BLOCK_FRAMES: Final[int] = 1 << 20

_MAX_INT16: Final[int] = 32_767
_MIN_INT16: Final[int] = -32_768


class NormalizationError(RuntimeError):
    """The working copy could not be built. The master is unchanged."""


@dataclass(frozen=True, slots=True)
class Gap:
    """A hole in the master timeline that was filled with silence in the copy."""

    start_ms: int
    end_ms: int
    frames: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "duration_ms": self.end_ms - self.start_ms,
            "frames": self.frames,
            "reason": self.reason,
        }


@dataclass(slots=True)
class NormalizationResult:
    """What was built, and everything a row in ``audio_working_copies`` needs."""

    path: Path
    relative_path: str
    sha256: str
    size_bytes: int
    frames: int
    duration_ms: int
    source_sample_rate: int
    source_channels: int
    source_frames: int
    source_chunk_count: int
    source_manifest_sha256: str | None
    gaps: tuple[Gap, ...] = ()
    peak_dbfs: float | None = None
    rms_dbfs: float | None = None
    clipped_samples: int = 0
    skipped_chunks: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def gap_total_ms(self) -> int:
        return sum(gap.end_ms - gap.start_ms for gap in self.gaps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "sample_rate_hz": WORKING_SAMPLE_RATE,
            "channels": WORKING_CHANNELS,
            "sample_format": "int16",
            "frames": self.frames,
            "duration_ms": self.duration_ms,
            "source_sample_rate_hz": self.source_sample_rate,
            "source_channels": self.source_channels,
            "source_frames": self.source_frames,
            "source_chunk_count": self.source_chunk_count,
            "source_manifest_sha256": self.source_manifest_sha256,
            "gap_count": len(self.gaps),
            "gap_total_ms": self.gap_total_ms,
            "gaps": [gap.to_dict() for gap in self.gaps],
            "peak_dbfs": self.peak_dbfs,
            "rms_dbfs": self.rms_dbfs,
            "clipped_samples": self.clipped_samples,
            "skipped_chunks": list(self.skipped_chunks),
            "warnings": list(self.warnings),
        }


# ===========================================================================
# Conversion primitives
# ===========================================================================


def downmix_to_mono(samples: array.array, channels: int) -> array.array:
    """Average interleaved channels into one, rounding half away from zero.

    Averaging rather than dropping a channel: a conference microphone that puts most
    of the room on one side would lose half the speakers, and a table microphone with
    one dead channel would halve the level of everybody. Averaging degrades
    gracefully in both cases.
    """
    if channels < 1:
        raise NormalizationError(f"channel count must be at least 1, got {channels}")
    if channels == 1:
        return samples
    if len(samples) % channels:
        raise NormalizationError(
            f"{len(samples)} samples do not divide into {channels} channels. The "
            "block boundary must fall on a whole frame."
        )
    out = array.array("h", bytes(2 * (len(samples) // channels)))
    for index in range(len(out)):
        base = index * channels
        total = 0
        for offset in range(channels):
            total += samples[base + offset]
        # Round half away from zero so a two-channel average does not drift towards
        # negative on every .5, which would put a DC offset on the whole file.
        if total >= 0:
            out[index] = (total + channels // 2) // channels
        else:
            out[index] = -((-total + channels // 2) // channels)
    return out


def resample_linear(
    samples: array.array,
    source_rate: int,
    target_rate: int,
    *,
    carry: int | None = None,
    position: float = 0.0,
) -> tuple[array.array, int | None, float]:
    """Linearly resample mono int16, resumably across blocks.

    ``carry`` is the last sample of the previous block and ``position`` the fractional
    read offset that block ended on. Threading both through is what makes a blocked
    conversion produce the same bytes as a single-shot one -- without them every block
    boundary would get a discontinuity, which is audible as a click and visible to a
    model as an onset.
    """
    if source_rate <= 0 or target_rate <= 0:
        raise NormalizationError(
            f"sample rates must be positive, got source={source_rate} "
            f"target={target_rate}"
        )
    if not samples:
        return array.array("h"), carry, position
    if source_rate == target_rate and carry is None and position == 0.0:
        return samples, samples[-1], 0.0

    # Work in a virtual buffer whose index 0 is the carried sample, so a read that
    # lands between blocks interpolates across the boundary instead of clamping.
    offset = 1 if carry is not None else 0
    length = len(samples) + offset
    ratio = source_rate / target_rate
    out = array.array("h")
    cursor = position
    while True:
        left = int(math.floor(cursor))
        if left + 1 >= length:
            break
        frac = cursor - left
        a = carry if (offset and left == 0) else samples[left - offset]
        b = carry if (offset and left + 1 == 0) else samples[left + 1 - offset]
        value = int(round(a + (b - a) * frac))
        out.append(max(_MIN_INT16, min(_MAX_INT16, value)))
        cursor += ratio
    # Rebase the cursor onto the next block, whose index 0 is this block's last sample.
    remaining = cursor - (length - 1)
    return out, samples[-1], max(0.0, remaining)


def _silence(frames: int) -> array.array:
    return array.array("h", bytes(2 * max(0, frames)))


# ===========================================================================
# The stage
# ===========================================================================


@dataclass(slots=True)
class _Meter:
    """Peak, RMS and clipping over the working copy, accumulated as it is written."""

    peak: int = 0
    sum_squares: float = 0.0
    count: int = 0
    clipped: int = 0

    def add(self, samples: Iterable[int]) -> None:
        for sample in samples:
            magnitude = -sample if sample < 0 else sample
            if magnitude > self.peak:
                self.peak = magnitude
            self.sum_squares += float(sample) * float(sample)
            self.count += 1
            if magnitude >= _MAX_INT16:
                self.clipped += 1

    @property
    def peak_dbfs(self) -> float | None:
        if not self.count:
            return None
        if self.peak <= 0:
            return -math.inf
        return round(20.0 * math.log10(self.peak / 32768.0), 2)

    @property
    def rms_dbfs(self) -> float | None:
        if not self.count:
            return None
        rms = math.sqrt(self.sum_squares / self.count)
        if rms <= 0:
            return -math.inf
        return round(20.0 * math.log10(rms / 32768.0), 2)


def _open_chunk(path: Path) -> tuple[wave.Wave_read, int, int, int]:
    handle = wave.open(str(path), "rb")
    channels = handle.getnchannels()
    width = handle.getsampwidth()
    rate = handle.getframerate()
    if width != 2:
        handle.close()
        raise NormalizationError(
            f"{path.name} has {width * 8}-bit samples. Phase 2 records int16 only, so "
            "a different width means the file did not come from this capture engine."
        )
    if channels < 1 or channels > 2:
        handle.close()
        raise NormalizationError(
            f"{path.name} has {channels} channels. Capture is mono or stereo."
        )
    return handle, channels, width, rate


def normalize_recording(
    *,
    chunk_paths: Sequence[Path],
    chunk_start_frames: Sequence[int],
    chunk_frame_counts: Sequence[int],
    target_path: Path,
    data_root: Path,
    source_manifest_sha256: str | None = None,
    expected_total_frames: int | None = None,
) -> NormalizationResult:
    """Concatenate the master's chunks onto one 16 kHz mono timeline.

    ``chunk_start_frames`` are offsets in the *master's* frame space, which is what
    makes gap handling possible: a chunk whose start does not equal the previous
    chunk's end has a hole before it, and the hole is filled with silence of exactly
    that length.

    Written to a ``.part`` file, fsynced, hashed from disk and then renamed, in the
    order ADR-0007 fixed for the capture writer. A crash leaves a ``.part``, never a
    half-written file that looks complete.
    """
    if not chunk_paths:
        raise NormalizationError(
            "a recording with no chunks cannot be normalised. Run `audio verify` -- "
            "either the manifest lists nothing or every chunk was quarantined."
        )
    if not (len(chunk_paths) == len(chunk_start_frames) == len(chunk_frame_counts)):
        raise NormalizationError(
            f"chunk metadata disagrees: {len(chunk_paths)} path(s), "
            f"{len(chunk_start_frames)} offset(s), {len(chunk_frame_counts)} count(s)"
        )

    from mom_igd.asr.manifest import sha256_file

    target_path.parent.mkdir(parents=True, exist_ok=True)
    partial = target_path.with_name(target_path.name + ".part")
    meter = _Meter()
    gaps: list[Gap] = []
    warnings: list[str] = []
    skipped: list[str] = []

    source_rate = 0
    source_channels = 0
    source_frames = 0
    written_frames = 0

    def to_working_frames(master_frames: int, rate: int) -> int:
        return int(round(master_frames * WORKING_SAMPLE_RATE / rate))

    try:
        with wave.open(str(partial), "wb") as out:
            out.setnchannels(WORKING_CHANNELS)
            out.setsampwidth(WORKING_SAMPLE_WIDTH)
            out.setframerate(WORKING_SAMPLE_RATE)

            expected_next_frame = int(chunk_start_frames[0]) if chunk_start_frames else 0
            for path, start_frame, frame_count in zip(
                chunk_paths, chunk_start_frames, chunk_frame_counts
            ):
                if not Path(path).is_file():
                    # A missing chunk is a hole of known length, not a reason to
                    # abandon the meeting. It is recorded as a gap and named.
                    if frame_count and source_rate:
                        filler = to_working_frames(int(frame_count), source_rate)
                        block = _silence(filler)
                        out.writeframes(block.tobytes())
                        meter.add(block)
                        gaps.append(
                            Gap(
                                start_ms=int(written_frames * 1000 / WORKING_SAMPLE_RATE),
                                end_ms=int(
                                    (written_frames + filler) * 1000 / WORKING_SAMPLE_RATE
                                ),
                                frames=filler,
                                reason="MISSING_CHUNK",
                            )
                        )
                        written_frames += filler
                        expected_next_frame = int(start_frame) + int(frame_count)
                    skipped.append(Path(path).name)
                    continue

                handle, channels, _width, rate = _open_chunk(Path(path))
                try:
                    if source_rate == 0:
                        source_rate, source_channels = rate, channels
                    elif rate != source_rate or channels != source_channels:
                        raise NormalizationError(
                            f"{Path(path).name} is {rate} Hz / {channels}ch but the "
                            f"recording started as {source_rate} Hz / "
                            f"{source_channels}ch. A capture cannot change format "
                            "mid-recording, so this recording is inconsistent."
                        )

                    # Fill the hole *before* this chunk, if the master left one.
                    if int(start_frame) > expected_next_frame:
                        missing = int(start_frame) - expected_next_frame
                        filler = to_working_frames(missing, source_rate)
                        block = _silence(filler)
                        out.writeframes(block.tobytes())
                        meter.add(block)
                        gaps.append(
                            Gap(
                                start_ms=int(written_frames * 1000 / WORKING_SAMPLE_RATE),
                                end_ms=int(
                                    (written_frames + filler) * 1000 / WORKING_SAMPLE_RATE
                                ),
                                frames=filler,
                                reason="DROPPED_FRAMES_OR_PAUSE",
                            )
                        )
                        written_frames += filler
                    elif int(start_frame) < expected_next_frame:
                        # Overlapping chunks would double-count time. Reported, not
                        # silently trimmed: the manifest is authoritative and a
                        # divergence is a finding.
                        warnings.append(
                            f"{Path(path).name} starts at frame {start_frame} but the "
                            f"previous chunk ended at {expected_next_frame}; the "
                            "overlap was written as-is and the timeline is longer "
                            "than the master's"
                        )

                    carry: int | None = None
                    position = 0.0
                    chunk_frames = 0
                    while True:
                        raw = handle.readframes(_BLOCK_FRAMES)
                        if not raw:
                            break
                        samples = array.array("h")
                        samples.frombytes(raw)
                        mono = downmix_to_mono(samples, channels)
                        converted, carry, position = resample_linear(
                            mono, source_rate, WORKING_SAMPLE_RATE,
                            carry=carry, position=position,
                        )
                        if converted:
                            out.writeframes(converted.tobytes())
                            meter.add(converted)
                            written_frames += len(converted)
                            chunk_frames += len(converted)
                        source_frames += len(mono)

                    expected_next_frame = int(start_frame) + int(frame_count)
                finally:
                    handle.close()

            out_frames = written_frames

        # Durability before hashing, as ADR-0007 fixed for the capture writer: the
        # digest has to describe bytes that are on the platter, not in a cache. Opened
        # "r+b" because `os.fsync` maps to FlushFileBuffers on Windows, which needs a
        # writable handle -- a read-only one silently flushes nothing.
        with open(partial, "r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
    except NormalizationError:
        partial.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001 - wrapped so the caller sees one type
        partial.unlink(missing_ok=True)
        raise NormalizationError(
            f"the working copy could not be written: {type(exc).__name__}: {exc}"
        ) from None

    # Hash from disk, not from the buffer that was written: the point of the digest is
    # to describe the bytes that actually landed.
    digest = sha256_file(partial)
    size = partial.stat().st_size
    target_path.unlink(missing_ok=True)
    os.replace(partial, target_path)

    if expected_total_frames is not None and source_rate:
        expected_working = to_working_frames(int(expected_total_frames), source_rate)
        drift = abs(expected_working - out_frames)
        # One millisecond of tolerance for the rounding in the frame-count conversion.
        if drift > WORKING_SAMPLE_RATE // 1000:
            warnings.append(
                f"working copy is {out_frames} frames but the manifest implies "
                f"{expected_working}; a drift of {drift} frames "
                f"({drift * 1000 / WORKING_SAMPLE_RATE:.0f} ms)"
            )

    try:
        relative = str(target_path.relative_to(data_root)).replace("\\", "/")
    except ValueError:
        raise NormalizationError(
            f"the working copy {target_path} is not inside the data root {data_root}. "
            "Every stored path is relative to the data root so a restored backup "
            "still resolves."
        ) from None

    result = NormalizationResult(
        path=target_path,
        relative_path=relative,
        sha256=digest,
        size_bytes=size,
        frames=out_frames,
        duration_ms=int(round(out_frames * 1000 / WORKING_SAMPLE_RATE)),
        source_sample_rate=source_rate,
        source_channels=source_channels,
        source_frames=source_frames,
        source_chunk_count=len(chunk_paths) - len(skipped),
        source_manifest_sha256=source_manifest_sha256,
        gaps=tuple(gaps),
        peak_dbfs=meter.peak_dbfs,
        rms_dbfs=meter.rms_dbfs,
        clipped_samples=meter.clipped,
        skipped_chunks=tuple(skipped),
        warnings=tuple(warnings),
    )
    _LOG.info(
        "asr.normalize.done",
        extra={
            "frames": out_frames,
            "duration_ms": result.duration_ms,
            "gaps": len(gaps),
            "source_rate": source_rate,
            "source_channels": source_channels,
        },
    )
    return result
