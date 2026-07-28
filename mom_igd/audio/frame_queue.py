"""Bounded hand-off queue between the audio callback and the writer thread.

The audio callback runs on PortAudio's real-time thread. If it ever blocks, the
driver misses its deadline and the operating system discards input -- audio is
lost, permanently, in the middle of a meeting that cannot be repeated. So the
callback's only job is: copy the frames, enqueue them, return.

That forces a bound. An unbounded queue would trade a dropped frame for
unbounded memory growth, which on a 16 GB machine ends in swapping and then in
losing far more than one frame. This queue therefore has a hard capacity measured
in **seconds of audio** (default 5), and when it is full it says so immediately
instead of waiting.

A full queue means audio was genuinely lost. That is counted, surfaced in the UI,
written to the audit trail and recorded in the manifest. It is never smoothed
over, and no silence is ever fabricated to paper over the gap: a recording with a
known 40 ms hole is useful, a recording with an invisible one is not.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Final

from mom_igd.audio.backend import CaptureProfile

__all__ = ["BoundedFrameQueue", "QueueStats", "DEFAULT_QUEUE_SECONDS"]

DEFAULT_QUEUE_SECONDS: Final[float] = 5.0
"""Roughly 5 s of headroom: enough to ride out a disk hiccup, small enough that
1.9 MB (48 kHz stereo) is the worst-case memory cost."""

_MIN_QUEUE_SECONDS: Final[float] = 0.25
_MAX_QUEUE_SECONDS: Final[float] = 60.0


@dataclass(frozen=True, slots=True)
class QueueStats:
    """Immutable view of queue counters, safe to serialise."""

    capacity_frames: int
    capacity_seconds: float
    queued_frames: int
    queued_bytes: int
    high_water_frames: int
    high_water_percent: float
    enqueued_frames: int
    dequeued_frames: int
    dropped_frames: int
    drop_events: int
    blocks_queued: int

    @property
    def has_loss(self) -> bool:
        return self.dropped_frames > 0

    def to_dict(self) -> dict[str, object]:
        return {
            "capacity_frames": self.capacity_frames,
            "capacity_seconds": round(self.capacity_seconds, 3),
            "queued_frames": self.queued_frames,
            "queued_bytes": self.queued_bytes,
            "high_water_frames": self.high_water_frames,
            "high_water_percent": round(self.high_water_percent, 2),
            "enqueued_frames": self.enqueued_frames,
            "dequeued_frames": self.dequeued_frames,
            "dropped_frames": self.dropped_frames,
            "drop_events": self.drop_events,
            "blocks_queued": self.blocks_queued,
            "has_loss": self.has_loss,
        }


class BoundedFrameQueue:
    """A frame-bounded FIFO of raw PCM blocks.

    ``put_nowait`` never blocks and never grows past capacity; ``get`` blocks on a
    condition variable with a timeout so the writer thread can shut down
    promptly. The lock is held only for pointer moves and integer arithmetic --
    never across file I/O, hashing or any other slow work.
    """

    def __init__(
        self,
        profile: CaptureProfile,
        *,
        capacity_seconds: float = DEFAULT_QUEUE_SECONDS,
    ) -> None:
        if not _MIN_QUEUE_SECONDS <= capacity_seconds <= _MAX_QUEUE_SECONDS:
            raise ValueError(
                f"capacity_seconds={capacity_seconds} is outside the sane range "
                f"{_MIN_QUEUE_SECONDS}-{_MAX_QUEUE_SECONDS}. Too small drops audio "
                "on any disk hiccup; too large trades bounded memory for a "
                "problem that a bound is supposed to prevent."
            )
        self._profile = profile
        self._capacity_frames = max(1, int(profile.sample_rate * capacity_seconds))
        self._capacity_seconds = capacity_seconds
        self._buffer: deque[tuple[bytes, int]] = deque()
        self._condition = threading.Condition(threading.Lock())
        self._closed = False

        self._queued_frames = 0
        self._queued_bytes = 0
        self._high_water_frames = 0
        self._enqueued_frames = 0
        self._dequeued_frames = 0
        self._dropped_frames = 0
        self._drop_events = 0
        self._blocks_queued = 0

    # -- producer side (audio callback thread) ------------------------------

    def put_nowait(self, payload: bytes, frames: int) -> bool:
        """Enqueue one block. Returns ``False`` if it had to be dropped.

        Called from the real-time audio thread. It never waits for the writer:
        when the queue is at capacity the block is discarded and counted, because
        stalling the driver would lose more audio than dropping one block.
        """
        if frames <= 0:
            return True
        with self._condition:
            if self._closed:
                self._dropped_frames += frames
                self._drop_events += 1
                return False
            if self._queued_frames + frames > self._capacity_frames:
                self._dropped_frames += frames
                self._drop_events += 1
                return False
            self._buffer.append((payload, frames))
            self._queued_frames += frames
            self._queued_bytes += len(payload)
            self._enqueued_frames += frames
            self._blocks_queued += 1
            if self._queued_frames > self._high_water_frames:
                self._high_water_frames = self._queued_frames
            self._condition.notify()
        return True

    # -- consumer side (writer thread) -------------------------------------

    def get(self, timeout: float = 0.1) -> tuple[bytes, int] | None:
        """Dequeue one block, or ``None`` if none arrived within ``timeout``."""
        with self._condition:
            if not self._buffer:
                self._condition.wait(timeout)
            if not self._buffer:
                return None
            payload, frames = self._buffer.popleft()
            self._queued_frames -= frames
            self._queued_bytes -= len(payload)
            self._dequeued_frames += frames
            return payload, frames

    def drain(self) -> list[tuple[bytes, int]]:
        """Remove and return everything queued. Used when finalising."""
        with self._condition:
            items = list(self._buffer)
            self._buffer.clear()
            for _, frames in items:
                self._dequeued_frames += frames
            self._queued_frames = 0
            self._queued_bytes = 0
            return items

    def close(self) -> None:
        """Refuse further input and wake any waiting consumer."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    # -- introspection ------------------------------------------------------

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    @property
    def capacity_frames(self) -> int:
        return self._capacity_frames

    def __len__(self) -> int:
        with self._condition:
            return len(self._buffer)

    @property
    def dropped_frames(self) -> int:
        with self._condition:
            return self._dropped_frames

    def stats(self) -> QueueStats:
        with self._condition:
            return QueueStats(
                capacity_frames=self._capacity_frames,
                capacity_seconds=self._capacity_seconds,
                queued_frames=self._queued_frames,
                queued_bytes=self._queued_bytes,
                high_water_frames=self._high_water_frames,
                high_water_percent=(
                    100.0 * self._high_water_frames / self._capacity_frames
                    if self._capacity_frames
                    else 0.0
                ),
                enqueued_frames=self._enqueued_frames,
                dequeued_frames=self._dequeued_frames,
                dropped_frames=self._dropped_frames,
                drop_events=self._drop_events,
                blocks_queued=self._blocks_queued,
            )
