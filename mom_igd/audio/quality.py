"""Audio quality metering for PCM16, standard library only.

This measures **signal quality**, not speech. It answers "is this recording
usable?" -- is the level sane, is anything clipping, is a channel dead, how loud
is the room floor. It never answers "is someone talking?" (that is VAD, excluded
from Phase 2) and never "who is talking?" (that is Phase 5-6).

Implementation notes worth knowing before editing:

* **No NumPy.** ``array.array('h')`` decodes PCM16 at C speed, ``max``/``min``
  find the peak at C speed, and ``array.count()`` counts hard-clipped samples at
  C speed. The one genuinely per-sample operation, sum of squares, is done with
  ``sum(map(operator.mul, a, a))`` so the loop also runs in C rather than in the
  interpreter.
* **``audioop`` is deliberately avoided** even though it would be faster still:
  it was removed from the standard library in Python 3.13, so depending on it
  would put a hard blocker in front of the next interpreter upgrade.
* **int16 is asymmetric.** The negative rail reaches -32768 while the positive
  rail stops at +32767, so a hard-clipped signal has a peak *magnitude* of 32768.
  Magnitude is therefore normalised against 32768 and dBFS is clamped at 0.0 --
  normalising against 32767 would report a positive dBFS, which is nonsense.
"""

from __future__ import annotations

import math
from array import array
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from operator import mul
from typing import Final, Iterable, Sequence

from mom_igd.audio.backend import (
    INT16_MAGNITUDE_FULL_SCALE,
    INT16_NEGATIVE_FULL_SCALE,
    INT16_POSITIVE_FULL_SCALE,
    CaptureProfile,
)

__all__ = [
    "CLIPPING_PERCENT_THRESHOLD",
    "ChannelActivity",
    "LevelVerdict",
    "MIN_DBFS",
    "QualityMeter",
    "QualitySnapshot",
    "SILENCE_DBFS_THRESHOLD",
    "analyse_block",
    "to_dbfs",
]

MIN_DBFS: Final[float] = -120.0
"""Reported instead of ``-inf`` for digital silence, so results stay JSON-safe."""

SILENCE_DBFS_THRESHOLD: Final[float] = -60.0
"""A block quieter than this counts as silence or near-silence."""

CHANNEL_ACTIVE_DBFS_THRESHOLD: Final[float] = -55.0
"""A channel quieter than this over the whole run is treated as inactive."""

TOO_QUIET_DBFS: Final[float] = -45.0
TOO_LOUD_PEAK_DBFS: Final[float] = -1.0
CLIPPING_PERCENT_THRESHOLD: Final[float] = 0.01
"""Above this share of hard-clipped samples the input is called clipping.

Deliberately strict: clipping destroys information permanently, and a recording
made once in a meeting cannot be redone.
"""

NOISE_FLOOR_PERCENTILE: Final[float] = 0.10
_ROLLING_SECONDS_DEFAULT: Final[float] = 3.0


def to_dbfs(magnitude: float) -> float:
    """Convert a linear int16 magnitude to dBFS, clamped to ``[MIN_DBFS, 0.0]``."""
    if magnitude <= 0.0:
        return MIN_DBFS
    value = 20.0 * math.log10(magnitude / INT16_MAGNITUDE_FULL_SCALE)
    if value > 0.0:
        return 0.0
    return max(value, MIN_DBFS)


class LevelVerdict(StrEnum):
    """Plain-language judgement shown to the operator."""

    NO_SIGNAL = "NO_SIGNAL"
    TOO_QUIET = "TOO_QUIET"
    GOOD = "GOOD"
    TOO_LOUD = "TOO_LOUD"
    CLIPPING = "CLIPPING"

    @property
    def is_acceptable(self) -> bool:
        return self is LevelVerdict.GOOD

    @property
    def advice(self) -> str:
        return {
            LevelVerdict.NO_SIGNAL: (
                "No signal. Check that the right microphone is selected, that it "
                "is not muted, and that Windows has granted microphone access."
            ),
            LevelVerdict.TOO_QUIET: (
                "Level is too low. Move the microphone closer to the centre of "
                "the table, or raise the input level in Windows Sound settings."
            ),
            LevelVerdict.GOOD: "Level is in the usable range.",
            LevelVerdict.TOO_LOUD: (
                "Level is close to full scale. Lower the input level in Windows "
                "Sound settings before recording."
            ),
            LevelVerdict.CLIPPING: (
                "Input is clipping and audio is being permanently destroyed. "
                "Lower the input level in Windows Sound settings and re-test."
            ),
        }[self]


@dataclass(frozen=True, slots=True)
class ChannelActivity:
    """Per-channel level, so a dead or mis-wired channel is visible."""

    channel: int
    rms_dbfs: float
    peak_dbfs: float
    active: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "channel": self.channel,
            "rms_dbfs": round(self.rms_dbfs, 2),
            "peak_dbfs": round(self.peak_dbfs, 2),
            "active": self.active,
        }


@dataclass(frozen=True, slots=True)
class BlockLevels:
    """Analysis of one PCM block."""

    frames: int
    sum_squares: int
    peak_magnitude: int
    clipped_samples: int
    per_channel_sum_squares: tuple[int, ...]
    per_channel_peak: tuple[int, ...]

    @property
    def samples(self) -> int:
        return self.frames * len(self.per_channel_peak)

    @property
    def rms(self) -> float:
        if self.samples == 0:
            return 0.0
        return math.sqrt(self.sum_squares / self.samples)

    @property
    def rms_dbfs(self) -> float:
        return to_dbfs(self.rms)

    @property
    def peak_dbfs(self) -> float:
        return to_dbfs(self.peak_magnitude)

    @property
    def is_silent(self) -> bool:
        return self.rms_dbfs < SILENCE_DBFS_THRESHOLD


@dataclass(frozen=True, slots=True)
class QualitySnapshot:
    """Aggregated levels over some window, safe to serialise and to log."""

    frames: int
    duration_seconds: float
    rms_dbfs: float
    peak_dbfs: float
    clipped_samples: int
    clipping_percent: float
    silence_percent: float
    noise_floor_dbfs: float
    channels: tuple[ChannelActivity, ...]
    verdict: LevelVerdict
    blocks: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "frames": self.frames,
            "duration_seconds": round(self.duration_seconds, 3),
            "blocks": self.blocks,
            "rms_dbfs": round(self.rms_dbfs, 2),
            "peak_dbfs": round(self.peak_dbfs, 2),
            "clipped_samples": self.clipped_samples,
            "clipping_percent": round(self.clipping_percent, 4),
            "silence_percent": round(self.silence_percent, 2),
            "noise_floor_dbfs": round(self.noise_floor_dbfs, 2),
            "channels": [c.to_dict() for c in self.channels],
            "verdict": self.verdict.value,
            "advice": self.verdict.advice,
        }

    @property
    def inactive_channels(self) -> tuple[int, ...]:
        return tuple(c.channel for c in self.channels if not c.active)


def analyse_block(pcm: bytes, channels: int, *, stride: int = 1) -> BlockLevels:
    """Analyse one PCM16 block.

    Args:
        pcm: Interleaved signed 16-bit little-endian samples. A trailing partial
            frame is ignored -- a partial frame is not a frame.
        channels: 1 or 2.
        stride: Analyse every ``stride``-th frame for the sum-of-squares and
            per-channel figures. ``1`` is exact; a larger value trades a little
            accuracy for CPU on the live meter. Peak and hard-clip counts are
            always exact because they cost nothing.

    Raises:
        ValueError: If ``channels`` is not positive or ``stride`` is below 1.
    """
    if channels < 1:
        raise ValueError(f"channels must be >= 1, got {channels}.")
    if stride < 1:
        raise ValueError(f"stride must be >= 1, got {stride}.")

    bytes_per_frame = channels * 2
    usable = (len(pcm) // bytes_per_frame) * bytes_per_frame
    if usable == 0:
        return BlockLevels(
            frames=0,
            sum_squares=0,
            peak_magnitude=0,
            clipped_samples=0,
            per_channel_sum_squares=tuple(0 for _ in range(channels)),
            per_channel_peak=tuple(0 for _ in range(channels)),
        )

    samples = array("h")
    samples.frombytes(pcm[:usable] if usable != len(pcm) else pcm)
    frames = usable // bytes_per_frame

    # Peak magnitude and hard-clip counts: exact, and entirely at C speed.
    peak = max(max(samples), -min(samples))
    clipped = samples.count(INT16_POSITIVE_FULL_SCALE) + samples.count(
        INT16_NEGATIVE_FULL_SCALE
    )

    per_channel_ss: list[int] = []
    per_channel_peak: list[int] = []
    total_ss = 0
    for channel in range(channels):
        lane = samples[channel::channels]
        if stride > 1:
            lane = lane[::stride]
        if len(lane) == 0:  # pragma: no cover - guarded by usable == 0 above
            per_channel_ss.append(0)
            per_channel_peak.append(0)
            continue
        # sum(map(mul, lane, lane)) keeps the multiply-accumulate loop in C.
        channel_ss = sum(map(mul, lane, lane))
        per_channel_ss.append(channel_ss)
        per_channel_peak.append(max(max(lane), -min(lane)))
        total_ss += channel_ss

    # When striding, scale the energy back up so RMS stays comparable.
    if stride > 1:
        total_ss *= stride
        per_channel_ss = [value * stride for value in per_channel_ss]

    return BlockLevels(
        frames=frames,
        sum_squares=total_ss,
        peak_magnitude=peak,
        clipped_samples=clipped,
        per_channel_sum_squares=tuple(per_channel_ss),
        per_channel_peak=tuple(per_channel_peak),
    )


@dataclass(slots=True)
class _Accumulator:
    frames: int = 0
    samples: int = 0
    sum_squares: int = 0
    peak_magnitude: int = 0
    clipped_samples: int = 0
    silent_frames: int = 0
    blocks: int = 0
    per_channel_ss: list[int] = field(default_factory=list)
    per_channel_peak: list[int] = field(default_factory=list)
    block_rms_dbfs: list[float] = field(default_factory=list)

    def reset(self, channels: int) -> None:
        self.frames = 0
        self.samples = 0
        self.sum_squares = 0
        self.peak_magnitude = 0
        self.clipped_samples = 0
        self.silent_frames = 0
        self.blocks = 0
        self.per_channel_ss = [0] * channels
        self.per_channel_peak = [0] * channels
        self.block_rms_dbfs = []

    def add(self, levels: BlockLevels) -> None:
        self.frames += levels.frames
        self.samples += levels.samples
        self.sum_squares += levels.sum_squares
        self.clipped_samples += levels.clipped_samples
        self.blocks += 1
        if levels.peak_magnitude > self.peak_magnitude:
            self.peak_magnitude = levels.peak_magnitude
        if levels.is_silent:
            self.silent_frames += levels.frames
        for index, value in enumerate(levels.per_channel_sum_squares):
            self.per_channel_ss[index] += value
        for index, value in enumerate(levels.per_channel_peak):
            if value > self.per_channel_peak[index]:
                self.per_channel_peak[index] = value
        self.block_rms_dbfs.append(levels.rms_dbfs)


class QualityMeter:
    """Accumulates block analyses into rolling and cumulative snapshots.

    Not thread-safe by itself. It is fed from the single writer thread, never
    from the audio callback -- the callback must stay free of any computation.
    """

    def __init__(
        self,
        profile: CaptureProfile,
        *,
        rolling_seconds: float = _ROLLING_SECONDS_DEFAULT,
        stride: int = 1,
        keep_block_history: int = 20_000,
    ) -> None:
        if rolling_seconds <= 0:
            raise ValueError("rolling_seconds must be positive.")
        self._profile = profile
        self._stride = stride
        self._rolling_frames = int(profile.sample_rate * rolling_seconds)
        self._keep_block_history = keep_block_history
        self._rolling: deque[BlockLevels] = deque()
        self._rolling_frame_count = 0
        self._cumulative = _Accumulator()
        self._cumulative.reset(profile.channels)

    # -- feeding ------------------------------------------------------------

    def add(self, pcm: bytes) -> BlockLevels:
        """Analyse and accumulate one block. Empty input is handled gracefully."""
        levels = analyse_block(pcm, self._profile.channels, stride=self._stride)
        if levels.frames == 0:
            return levels
        self._cumulative.add(levels)
        if len(self._cumulative.block_rms_dbfs) > self._keep_block_history:
            # Bound memory on a long recording: keep the history representative
            # by halving it rather than dropping the oldest, so the noise-floor
            # percentile still reflects the whole session.
            self._cumulative.block_rms_dbfs = self._cumulative.block_rms_dbfs[::2]
        self._rolling.append(levels)
        self._rolling_frame_count += levels.frames
        while self._rolling and self._rolling_frame_count > self._rolling_frames:
            oldest = self._rolling.popleft()
            self._rolling_frame_count -= oldest.frames
        return levels

    def reset(self) -> None:
        self._rolling.clear()
        self._rolling_frame_count = 0
        self._cumulative.reset(self._profile.channels)

    # -- reporting ----------------------------------------------------------

    @property
    def frames(self) -> int:
        return self._cumulative.frames

    @property
    def clipped_samples(self) -> int:
        return self._cumulative.clipped_samples

    def rolling_snapshot(self) -> QualitySnapshot:
        """Levels over the last few seconds -- what the live meter shows."""
        return self._snapshot_from(list(self._rolling))

    def cumulative_snapshot(self) -> QualitySnapshot:
        """Levels over everything seen since the last reset."""
        accumulator = self._cumulative
        if accumulator.samples == 0:
            return self._empty_snapshot()
        rms = math.sqrt(accumulator.sum_squares / accumulator.samples)
        channels = self._channel_activity(
            accumulator.per_channel_ss, accumulator.per_channel_peak, accumulator.frames
        )
        clipping_percent = (
            100.0 * accumulator.clipped_samples / accumulator.samples
            if accumulator.samples
            else 0.0
        )
        silence_percent = (
            100.0 * accumulator.silent_frames / accumulator.frames if accumulator.frames else 0.0
        )
        return QualitySnapshot(
            frames=accumulator.frames,
            duration_seconds=accumulator.frames / self._profile.sample_rate,
            rms_dbfs=to_dbfs(rms),
            peak_dbfs=to_dbfs(accumulator.peak_magnitude),
            clipped_samples=accumulator.clipped_samples,
            clipping_percent=clipping_percent,
            silence_percent=silence_percent,
            noise_floor_dbfs=_percentile(accumulator.block_rms_dbfs, NOISE_FLOOR_PERCENTILE),
            channels=channels,
            verdict=_verdict(to_dbfs(rms), to_dbfs(accumulator.peak_magnitude), clipping_percent, silence_percent),
            blocks=accumulator.blocks,
        )

    # -- internals ----------------------------------------------------------

    def _empty_snapshot(self) -> QualitySnapshot:
        return QualitySnapshot(
            frames=0,
            duration_seconds=0.0,
            rms_dbfs=MIN_DBFS,
            peak_dbfs=MIN_DBFS,
            clipped_samples=0,
            clipping_percent=0.0,
            silence_percent=0.0,
            noise_floor_dbfs=MIN_DBFS,
            channels=tuple(
                ChannelActivity(channel=c, rms_dbfs=MIN_DBFS, peak_dbfs=MIN_DBFS, active=False)
                for c in range(self._profile.channels)
            ),
            verdict=LevelVerdict.NO_SIGNAL,
            blocks=0,
        )

    def _snapshot_from(self, blocks: Sequence[BlockLevels]) -> QualitySnapshot:
        if not blocks:
            return self._empty_snapshot()
        frames = sum(b.frames for b in blocks)
        samples = sum(b.samples for b in blocks)
        if samples == 0:  # pragma: no cover - blocks with frames always have samples
            return self._empty_snapshot()
        sum_squares = sum(b.sum_squares for b in blocks)
        peak = max(b.peak_magnitude for b in blocks)
        clipped = sum(b.clipped_samples for b in blocks)
        silent_frames = sum(b.frames for b in blocks if b.is_silent)
        per_channel_ss = [0] * self._profile.channels
        per_channel_peak = [0] * self._profile.channels
        for block in blocks:
            for index, value in enumerate(block.per_channel_sum_squares):
                per_channel_ss[index] += value
            for index, value in enumerate(block.per_channel_peak):
                per_channel_peak[index] = max(per_channel_peak[index], value)
        rms = math.sqrt(sum_squares / samples)
        clipping_percent = 100.0 * clipped / samples
        silence_percent = 100.0 * silent_frames / frames if frames else 0.0
        return QualitySnapshot(
            frames=frames,
            duration_seconds=frames / self._profile.sample_rate,
            rms_dbfs=to_dbfs(rms),
            peak_dbfs=to_dbfs(peak),
            clipped_samples=clipped,
            clipping_percent=clipping_percent,
            silence_percent=silence_percent,
            noise_floor_dbfs=_percentile([b.rms_dbfs for b in blocks], NOISE_FLOOR_PERCENTILE),
            channels=self._channel_activity(per_channel_ss, per_channel_peak, frames),
            verdict=_verdict(to_dbfs(rms), to_dbfs(peak), clipping_percent, silence_percent),
            blocks=len(blocks),
        )

    def _channel_activity(
        self, sum_squares: Sequence[int], peaks: Sequence[int], frames: int
    ) -> tuple[ChannelActivity, ...]:
        result: list[ChannelActivity] = []
        for channel in range(self._profile.channels):
            channel_ss = sum_squares[channel] if channel < len(sum_squares) else 0
            channel_peak = peaks[channel] if channel < len(peaks) else 0
            rms = math.sqrt(channel_ss / frames) if frames else 0.0
            rms_db = to_dbfs(rms)
            result.append(
                ChannelActivity(
                    channel=channel,
                    rms_dbfs=rms_db,
                    peak_dbfs=to_dbfs(channel_peak),
                    active=rms_db >= CHANNEL_ACTIVE_DBFS_THRESHOLD,
                )
            )
        return tuple(result)


def _percentile(values: Iterable[float], fraction: float) -> float:
    """Nearest-rank percentile. Used for the noise-floor approximation."""
    ordered = sorted(values)
    if not ordered:
        return MIN_DBFS
    index = int(max(0.0, min(1.0, fraction)) * (len(ordered) - 1))
    return ordered[index]


def _verdict(
    rms_dbfs: float, peak_dbfs: float, clipping_percent: float, silence_percent: float
) -> LevelVerdict:
    if clipping_percent > CLIPPING_PERCENT_THRESHOLD:
        return LevelVerdict.CLIPPING
    if silence_percent >= 99.0 or rms_dbfs <= MIN_DBFS:
        return LevelVerdict.NO_SIGNAL
    if peak_dbfs > TOO_LOUD_PEAK_DBFS:
        return LevelVerdict.TOO_LOUD
    if rms_dbfs < TOO_QUIET_DBFS:
        return LevelVerdict.TOO_QUIET
    return LevelVerdict.GOOD
