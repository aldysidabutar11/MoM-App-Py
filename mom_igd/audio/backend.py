"""Audio backend boundary: protocol, capture profile, device description.

This module contains **no hardware access at all**. It defines the contract that
:mod:`mom_igd.audio.sounddevice_backend` implements against PortAudio and that
:mod:`mom_igd.audio.fake_backend` implements deterministically for tests.

Why a protocol rather than calling ``sounddevice`` directly: capture correctness
(frame ordering, chunk boundaries, checksums, crash recovery) is logic that must
be tested exhaustively, and it cannot be tested against real hardware in CI or on
a machine with no microphone. Every automated test therefore drives the fake
backend, and the real one stays a thin adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Final, Protocol, runtime_checkable

__all__ = [
    "AudioBackend",
    "AudioError",
    "BackendUnavailableError",
    "CHUNK_SECONDS_MAX",
    "CHUNK_SECONDS_MIN",
    "CallbackStatus",
    "CaptureCallback",
    "CaptureProfile",
    "DeviceNotFoundError",
    "DeviceTransport",
    "InputStreamHandle",
    "MAX_CHANNELS",
    "RawDeviceInfo",
    "SUPPORTED_SAMPLE_RATES",
    "SampleFormat",
    "StreamError",
    "UnsupportedProfileError",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AudioError(RuntimeError):
    """Base class for every audio-capture error."""


class BackendUnavailableError(AudioError):
    """The audio backend (PortAudio) could not be loaded."""


class DeviceNotFoundError(AudioError):
    """The requested device does not exist, or no longer exists."""


class UnsupportedProfileError(AudioError):
    """The device cannot provide the requested capture profile."""


class StreamError(AudioError):
    """The audio stream failed to open, start or stop."""


# ---------------------------------------------------------------------------
# Capture profile
# ---------------------------------------------------------------------------


class SampleFormat(StrEnum):
    """Sample formats supported for capture.

    Phase 2 captures signed 16-bit little-endian PCM only. It is what every
    conference microphone delivers natively, it is what the standard-library
    ``wave`` module writes without a codec, and it keeps the callback free of any
    conversion work. Higher bit depths and float formats are not needed to
    transcribe speech and would inflate storage for no accuracy gain.
    """

    INT16 = "int16"

    @property
    def bytes_per_sample(self) -> int:
        return 2

    @property
    def sounddevice_dtype(self) -> str:
        return "int16"


MAX_CHANNELS: Final[int] = 2
"""Phase 2 supports mono and stereo only.

A device's native channel count is preserved: a mono microphone is never faked
up to stereo, and stereo is never downmixed during capture. Deriving a 16 kHz
mono working copy for ASR is a *processing* step, not a capture step.
"""

CHUNK_SECONDS_MIN: Final[int] = 10
CHUNK_SECONDS_MAX: Final[int] = 120
CHUNK_SECONDS_DEFAULT: Final[int] = 30

SUPPORTED_SAMPLE_RATES: Final[tuple[int, ...]] = (
    8_000,
    11_025,
    16_000,
    22_050,
    32_000,
    44_100,
    48_000,
    88_200,
    96_000,
    176_400,
    192_000,
)
"""Sample rates the application will accept from a device.

The default is whatever the device reports as native -- usually 48 kHz. Resampling
never happens in the audio callback.
"""

BYTES_PER_MB: Final[int] = 1_000_000
"""Decimal megabyte.

Storage figures are quoted in **decimal** MB/GB so they match the documented
capture rates (48 kHz PCM16 mono ~=345.6 MB/h, stereo ~=691.2 MB/h) and the way
drive capacities are advertised. All *decisions* -- free-space checks, preflight
estimates, low-disk aborts -- are made in raw bytes, so no unit conversion can
change an outcome; MB/GB appear only in text meant for a human.
"""

BYTES_PER_GB: Final[int] = 1_000_000_000

INT16_POSITIVE_FULL_SCALE: Final[int] = 32_767
INT16_NEGATIVE_FULL_SCALE: Final[int] = -32_768
INT16_MAGNITUDE_FULL_SCALE: Final[int] = 32_768
"""Reference for peak *magnitude* in dBFS.

int16 is asymmetric: the negative rail reaches -32768 while the positive rail
stops at +32767. A hard-clipped signal therefore has a legitimate peak magnitude
of 32768, and normalising against 32767 would report a positive dBFS. Magnitude
is normalised against 32768 and dBFS is clamped at 0.0.
"""


@dataclass(frozen=True, slots=True)
class CaptureProfile:
    """A fully specified, validated capture configuration."""

    sample_rate: int
    channels: int
    sample_format: SampleFormat = SampleFormat.INT16
    chunk_seconds: int = CHUNK_SECONDS_DEFAULT
    blocksize: int = 0
    """Frames per callback. ``0`` lets PortAudio pick, which is usually best."""

    def __post_init__(self) -> None:
        if self.sample_rate not in SUPPORTED_SAMPLE_RATES:
            raise UnsupportedProfileError(
                f"sample_rate={self.sample_rate} is not supported. Allowed: "
                f"{list(SUPPORTED_SAMPLE_RATES)}."
            )
        if not 1 <= self.channels <= MAX_CHANNELS:
            raise UnsupportedProfileError(
                f"channels={self.channels} is out of range. Phase 2 supports 1 "
                f"(mono) or {MAX_CHANNELS} (stereo); a device's native channel "
                "count is preserved rather than converted."
            )
        if not isinstance(self.sample_format, SampleFormat):
            raise UnsupportedProfileError(
                f"sample_format={self.sample_format!r} is not a SampleFormat."
            )
        if not CHUNK_SECONDS_MIN <= self.chunk_seconds <= CHUNK_SECONDS_MAX:
            raise UnsupportedProfileError(
                f"chunk_seconds={self.chunk_seconds} is outside the safe range "
                f"{CHUNK_SECONDS_MIN}-{CHUNK_SECONDS_MAX}. Shorter chunks lose "
                "less audio to a crash but multiply file count; longer chunks "
                "risk the 4 GiB WAV size limit and lose more on corruption."
            )
        if self.blocksize < 0:
            raise UnsupportedProfileError(f"blocksize={self.blocksize} must be >= 0.")

    # -- derived sizes ------------------------------------------------------

    @property
    def bytes_per_frame(self) -> int:
        """One frame = one sample per channel."""
        return self.channels * self.sample_format.bytes_per_sample

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.bytes_per_frame

    @property
    def frames_per_chunk(self) -> int:
        return self.sample_rate * self.chunk_seconds

    @property
    def bytes_per_chunk(self) -> int:
        return self.frames_per_chunk * self.bytes_per_frame

    @property
    def bytes_per_hour(self) -> int:
        """Exact uncompressed storage rate. All disk decisions use this."""
        return self.bytes_per_second * 3600

    @property
    def megabytes_per_hour(self) -> float:
        """Uncompressed storage rate in **decimal** MB, for display only.

        48 kHz PCM16 mono is 345.6 MB/h; stereo is 691.2 MB/h.
        """
        return self.bytes_per_hour / BYTES_PER_MB

    def frames_to_ms(self, frames: int) -> float:
        return frames * 1000.0 / self.sample_rate

    def bytes_to_frames(self, size: int) -> int:
        """Whole frames only -- a trailing partial frame is not a frame."""
        return size // self.bytes_per_frame

    def estimate_bytes(self, seconds: float) -> int:
        return int(self.bytes_per_second * max(seconds, 0.0))

    def describe(self) -> dict[str, object]:
        return {
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "sample_format": self.sample_format.value,
            "bytes_per_frame": self.bytes_per_frame,
            "bytes_per_second": self.bytes_per_second,
            "chunk_seconds": self.chunk_seconds,
            "frames_per_chunk": self.frames_per_chunk,
            "bytes_per_hour": self.bytes_per_hour,
            "megabytes_per_hour": round(self.megabytes_per_hour, 1),
            "blocksize": self.blocksize,
        }


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


class DeviceTransport(StrEnum):
    """How a capture device is physically attached.

    ``UNKNOWN`` is a first-class, honest answer. The transport is only ever set to
    a concrete value when the operating system reports it (on Windows, the
    ``MMDevices`` enumerator name in the registry). It is **never** guessed from
    the device name: a microphone called "USB Audio" may not be one, and calling
    an internal array "USB" would make the production gate meaningless.
    """

    USB = "USB"
    INTERNAL = "INTERNAL"
    BLUETOOTH = "BLUETOOTH"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RawDeviceInfo:
    """What a backend reports about one device, before enrichment.

    ``index`` is PortAudio's position in its current device list. It is
    **transient**: it changes when a device is plugged, unplugged, or after a
    reboot. It must never be persisted as a device identity -- see
    :func:`mom_igd.audio.devices.device_fingerprint`.
    """

    index: int
    name: str
    host_api: str
    max_input_channels: int
    max_output_channels: int
    default_sample_rate: float
    default_low_input_latency: float = 0.0
    default_high_input_latency: float = 0.0
    is_default_input: bool = False

    @property
    def is_input_capable(self) -> bool:
        return self.max_input_channels > 0

    @property
    def is_output_only(self) -> bool:
        return self.max_input_channels == 0 and self.max_output_channels > 0


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CallbackStatus:
    """Backend-independent view of PortAudio's per-callback status flags.

    An overflow means the host discarded input because we did not consume it fast
    enough -- audio was genuinely lost. It is counted and surfaced, never hidden.
    """

    input_overflow: bool = False
    input_underflow: bool = False
    output_overflow: bool = False
    output_underflow: bool = False
    priming_output: bool = False

    @property
    def is_clean(self) -> bool:
        return not (
            self.input_overflow
            or self.input_underflow
            or self.output_overflow
            or self.output_underflow
        )

    @property
    def is_input_xrun(self) -> bool:
        """Whether this callback lost input audio."""
        return self.input_overflow or self.input_underflow

    def labels(self) -> list[str]:
        names = []
        for flag, label in (
            (self.input_overflow, "input_overflow"),
            (self.input_underflow, "input_underflow"),
            (self.output_overflow, "output_overflow"),
            (self.output_underflow, "output_underflow"),
            (self.priming_output, "priming_output"),
        ):
            if flag:
                names.append(label)
        return names


CaptureCallback = Callable[[bytes, int, CallbackStatus], None]
"""``(pcm_bytes, frame_count, status) -> None``.

Called from the backend's real-time audio thread. It must copy and enqueue, and
nothing else: no file I/O, no hashing, no database access, no allocation beyond
the frame copy, and it must never block waiting for the writer.
"""


@runtime_checkable
class InputStreamHandle(Protocol):
    """A started or stoppable capture stream."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...

    @property
    def active(self) -> bool: ...

    @property
    def actual_blocksize(self) -> int: ...

    @property
    def latency_seconds(self) -> float: ...


@runtime_checkable
class AudioBackend(Protocol):
    """The audio hardware boundary.

    Implementations must not touch hardware on construction or during
    :meth:`list_devices` beyond read-only enumeration. Opening a stream is the
    only operation that engages the microphone, and it happens only when the user
    explicitly asks.
    """

    @property
    def name(self) -> str: ...

    def describe(self) -> dict[str, object]:
        """Backend identity for diagnostics (library and PortAudio versions)."""
        ...

    def list_devices(self) -> list[RawDeviceInfo]:
        """Enumerate devices. Read-only; opens no stream."""
        ...

    def check_input_settings(self, device_index: int, profile: CaptureProfile) -> None:
        """Validate a profile against a device.

        Raises:
            DeviceNotFoundError: If the device does not exist.
            UnsupportedProfileError: If the device cannot provide the profile.
        """
        ...

    def open_input_stream(
        self,
        device_index: int,
        profile: CaptureProfile,
        callback: CaptureCallback,
    ) -> InputStreamHandle:
        """Open (but do not start) a capture stream."""
        ...


@dataclass(slots=True)
class StreamStats:
    """Counters accumulated from the real-time callback thread.

    Every field is a plain integer, deliberately. An earlier version kept a
    ``dict`` of status labels, which meant allocating a list and touching a hash
    table on every callback -- work that has no business happening on an audio
    thread -- and left ``snapshot()`` able to raise while the dict was being
    mutated concurrently. Fixed integer fields have neither problem.

    Increments are not atomic under the GIL, so a reader can observe a count that
    is at most one callback stale. That is acceptable for telemetry and is never
    used to decide anything: the authoritative loss accounting lives in
    :class:`~mom_igd.audio.frame_queue.BoundedFrameQueue`, which is lock-guarded.
    """

    callbacks: int = 0
    frames_delivered: int = 0
    xrun_callbacks: int = 0
    input_overflow_callbacks: int = 0
    input_underflow_callbacks: int = 0

    def record(self, frames: int, status: CallbackStatus) -> None:
        self.callbacks += 1
        self.frames_delivered += frames
        if status.is_clean:
            return
        if status.input_overflow:
            self.input_overflow_callbacks += 1
        if status.input_underflow:
            self.input_underflow_callbacks += 1
        if status.input_overflow or status.input_underflow:
            self.xrun_callbacks += 1

    def snapshot(self) -> dict[str, object]:
        return {
            "callbacks": self.callbacks,
            "frames_delivered": self.frames_delivered,
            "xrun_callbacks": self.xrun_callbacks,
            "input_overflow_callbacks": self.input_overflow_callbacks,
            "input_underflow_callbacks": self.input_underflow_callbacks,
        }
