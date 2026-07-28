"""Deterministic in-process audio backend for automated tests and smoke runs.

Every automated test in this project drives this backend. No test requires a
physical microphone, and no test opens PortAudio. That is not a convenience: the
properties that matter for capture -- frame ordering, exact frame counts, chunk
boundaries, checksums, queue overflow accounting, crash recovery -- can only be
asserted against a signal whose every byte is known in advance.

The PCM sources are pure functions of the absolute frame index, so
``source.read(0, n)`` equals the concatenation of any partition of that range.
A test can therefore compare a whole recovered recording byte-for-byte against
what the source *should* have produced.

No human voice recording is ever used as a fixture, in Git or anywhere else.
"""

from __future__ import annotations

import math
import threading
import time
from array import array
from dataclasses import dataclass, field, replace
from typing import Callable, Final, Protocol

from mom_igd.audio.backend import (
    CallbackStatus,
    CaptureCallback,
    CaptureProfile,
    DeviceNotFoundError,
    RawDeviceInfo,
    StreamError,
    UnsupportedProfileError,
)

__all__ = [
    "ClippingSource",
    "CounterSource",
    "FakeAudioBackend",
    "FakeStream",
    "PcmSource",
    "SilenceSource",
    "SineSource",
    "StereoActivitySource",
    "default_fake_devices",
    "dbfs_to_amplitude",
]

_FULL_SCALE: Final[int] = 32767
_INT16_MIN: Final[int] = -32768


def dbfs_to_amplitude(dbfs: float) -> float:
    """Convert dBFS to a linear int16 amplitude (0 dBFS == full scale)."""
    return _FULL_SCALE * (10.0 ** (dbfs / 20.0))


def _clamp(value: int) -> int:
    return _FULL_SCALE if value > _FULL_SCALE else (_INT16_MIN if value < _INT16_MIN else value)


# ---------------------------------------------------------------------------
# PCM sources
# ---------------------------------------------------------------------------


class PcmSource(Protocol):
    """A deterministic PCM16 generator addressed by absolute frame index."""

    def read(self, start_frame: int, frames: int, profile: CaptureProfile) -> bytes: ...

    @property
    def label(self) -> str: ...


@dataclass(frozen=True, slots=True)
class SilenceSource:
    """Digital silence. Used to test silence detection and noise floor."""

    @property
    def label(self) -> str:
        return "silence"

    def read(self, start_frame: int, frames: int, profile: CaptureProfile) -> bytes:
        return b"\x00" * (frames * profile.bytes_per_frame)


@dataclass(frozen=True, slots=True)
class SineSource:
    """A sine tone at a known level, identical on every channel."""

    frequency_hz: float = 440.0
    level_dbfs: float = -12.0

    @property
    def label(self) -> str:
        return f"sine{self.frequency_hz:g}Hz@{self.level_dbfs:g}dBFS"

    def read(self, start_frame: int, frames: int, profile: CaptureProfile) -> bytes:
        amplitude = dbfs_to_amplitude(self.level_dbfs)
        omega = 2.0 * math.pi * self.frequency_hz / profile.sample_rate
        samples = array("h", bytes(frames * profile.bytes_per_frame))
        channels = profile.channels
        for i in range(frames):
            value = _clamp(int(round(amplitude * math.sin(omega * (start_frame + i)))))
            base = i * channels
            for c in range(channels):
                samples[base + c] = value
        return samples.tobytes()


@dataclass(frozen=True, slots=True)
class ClippingSource:
    """A sine driven past full scale, so samples clamp at exactly +-full scale.

    Produces genuine hard clipping that the quality meter must count, rather than
    a merely loud signal.
    """

    frequency_hz: float = 440.0
    overdrive_db: float = 6.0

    @property
    def label(self) -> str:
        return f"clipping+{self.overdrive_db:g}dB"

    def read(self, start_frame: int, frames: int, profile: CaptureProfile) -> bytes:
        amplitude = dbfs_to_amplitude(self.overdrive_db)
        omega = 2.0 * math.pi * self.frequency_hz / profile.sample_rate
        samples = array("h", bytes(frames * profile.bytes_per_frame))
        channels = profile.channels
        for i in range(frames):
            value = _clamp(int(round(amplitude * math.sin(omega * (start_frame + i)))))
            base = i * channels
            for c in range(channels):
                samples[base + c] = value
        return samples.tobytes()


@dataclass(frozen=True, slots=True)
class StereoActivitySource:
    """Different levels per channel, to test per-channel activity reporting.

    A conference microphone wired to only one channel is a real and easily missed
    fault; this makes it detectable.
    """

    left_dbfs: float = -12.0
    right_dbfs: float = -60.0
    frequency_hz: float = 440.0

    @property
    def label(self) -> str:
        return f"stereo(L{self.left_dbfs:g}/R{self.right_dbfs:g})"

    def read(self, start_frame: int, frames: int, profile: CaptureProfile) -> bytes:
        levels = [self.left_dbfs, self.right_dbfs][: profile.channels]
        amplitudes = [dbfs_to_amplitude(level) for level in levels]
        omega = 2.0 * math.pi * self.frequency_hz / profile.sample_rate
        samples = array("h", bytes(frames * profile.bytes_per_frame))
        channels = profile.channels
        for i in range(frames):
            phase = math.sin(omega * (start_frame + i))
            base = i * channels
            for c in range(channels):
                samples[base + c] = _clamp(int(round(amplitudes[c] * phase)))
        return samples.tobytes()


@dataclass(frozen=True, slots=True)
class CounterSource:
    """Encodes the absolute frame index into every frame.

    This is the strongest available capture test: because the value of frame *i*
    is a pure function of *i*, a test can regenerate the entire expected byte
    stream and compare it to what was written. Any dropped, duplicated or
    reordered frame changes the bytes and is caught exactly, not statistically.

    Channel 0 carries ``i mod 32768``; channel 1 carries its negation, so a
    channel swap is also detectable.
    """

    @property
    def label(self) -> str:
        return "counter"

    def read(self, start_frame: int, frames: int, profile: CaptureProfile) -> bytes:
        samples = array("h", bytes(frames * profile.bytes_per_frame))
        channels = profile.channels
        for i in range(frames):
            value = (start_frame + i) % 32768
            base = i * channels
            samples[base] = value
            if channels > 1:
                samples[base + 1] = -value
        return samples.tobytes()

    @staticmethod
    def frame_index_of(sample_value: int) -> int:
        """Recover ``i mod 32768`` from a channel-0 sample."""
        return sample_value % 32768


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


def default_fake_devices() -> list[RawDeviceInfo]:
    """A device list that exercises every acceptance and rejection rule."""
    return [
        RawDeviceInfo(
            index=0,
            name="Fake Internal Microphone Array",
            host_api="Windows WASAPI",
            max_input_channels=2,
            max_output_channels=0,
            default_sample_rate=48_000.0,
            default_low_input_latency=0.01,
            default_high_input_latency=0.08,
            is_default_input=True,
        ),
        RawDeviceInfo(
            index=1,
            name="Fake USB Conference Mic",
            host_api="Windows WASAPI",
            max_input_channels=1,
            max_output_channels=0,
            default_sample_rate=48_000.0,
            default_low_input_latency=0.01,
            default_high_input_latency=0.08,
        ),
        RawDeviceInfo(
            index=2,
            name="Fake Speakers (output only)",
            host_api="Windows WASAPI",
            max_input_channels=0,
            max_output_channels=2,
            default_sample_rate=48_000.0,
        ),
        RawDeviceInfo(
            index=3,
            name="Fake Narrowband Phone Mic",
            host_api="MME",
            max_input_channels=1,
            max_output_channels=0,
            default_sample_rate=8_000.0,
        ),
    ]


# ---------------------------------------------------------------------------
# Stream
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _StreamState:
    started: bool = False
    closed: bool = False
    frames_produced: int = 0
    callback_invocations: int = 0
    thread: threading.Thread | None = None
    stop_flag: threading.Event = field(default_factory=threading.Event)


class FakeStream:
    """A capture stream driven either manually (deterministic) or by a thread.

    Manual mode (:meth:`pump`) is what tests use: no sleeping, no wall-clock
    dependency, exact control over how many callbacks fire and with which status
    flags. Threaded mode (:meth:`start` with ``realtime=True``) exists for the
    fake soak run, where an ``speed`` multiplier lets a simulated hour finish in
    seconds.
    """

    def __init__(
        self,
        profile: CaptureProfile,
        callback: CaptureCallback,
        source: PcmSource,
        *,
        blocksize: int = 1024,
        realtime: bool = False,
        speed: float = 1.0,
        total_frames: int | None = None,
        latency_seconds: float = 0.01,
        on_close: Callable[[FakeStream], None] | None = None,
    ) -> None:
        if blocksize <= 0:
            raise StreamError("FakeStream requires a positive blocksize.")
        self._profile = profile
        self._callback = callback
        self._source = source
        self._blocksize = blocksize
        self._realtime = realtime
        self._speed = max(speed, 0.001)
        self._total_frames = total_frames
        self._latency = latency_seconds
        self._on_close = on_close
        self._state = _StreamState()
        # Injectable faults, set by tests.
        self.inject_status: CallbackStatus | None = None
        self.inject_status_every: int = 0
        self.inject_callback_error: BaseException | None = None
        self.raise_on_stop: BaseException | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._state.closed:
            raise StreamError("Cannot start a closed stream.")
        if self._state.started:
            return
        self._state.started = True
        if self._realtime:
            thread = threading.Thread(
                target=self._run_realtime, name="fake-audio-source", daemon=True
            )
            self._state.thread = thread
            thread.start()

    def stop(self) -> None:
        self._state.started = False
        self._state.stop_flag.set()
        thread = self._state.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._state.thread = None
        if self.raise_on_stop is not None:
            error, self.raise_on_stop = self.raise_on_stop, None
            raise error

    def close(self) -> None:
        if self._state.closed:
            return
        try:
            self.stop()
        finally:
            self._state.closed = True
            if self._on_close is not None:
                self._on_close(self)

    # -- introspection ------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._state.started and not self._state.closed

    @property
    def closed(self) -> bool:
        return self._state.closed

    @property
    def actual_blocksize(self) -> int:
        return self._blocksize

    @property
    def latency_seconds(self) -> float:
        return self._latency

    @property
    def frames_produced(self) -> int:
        return self._state.frames_produced

    @property
    def callback_invocations(self) -> int:
        return self._state.callback_invocations

    # -- production ---------------------------------------------------------

    def pump(self, blocks: int = 1, *, status: CallbackStatus | None = None) -> int:
        """Deliver ``blocks`` callbacks synchronously. Returns frames delivered.

        The stream must be started, mirroring the real backend where no callback
        fires before ``start()``.
        """
        if not self._state.started:
            raise StreamError("pump() requires the stream to be started.")
        delivered = 0
        for _ in range(blocks):
            frames = self._blocksize
            if self._total_frames is not None:
                remaining = self._total_frames - self._state.frames_produced
                if remaining <= 0:
                    break
                frames = min(frames, remaining)
            delivered += self._emit(frames, status)
        return delivered

    def pump_frames(self, frames: int, *, status: CallbackStatus | None = None) -> int:
        """Deliver at least ``frames`` frames, in whole blocks."""
        blocks = max(1, math.ceil(frames / self._blocksize))
        return self.pump(blocks, status=status)

    def _emit(self, frames: int, status: CallbackStatus | None) -> int:
        effective = status
        if effective is None and self.inject_status is not None:
            every = self.inject_status_every
            index = self._state.callback_invocations
            if every <= 0 or index % every == 0:
                effective = self.inject_status
        # Raise BEFORE advancing the counters: a source that failed produced
        # nothing, so `frames_produced` must not imply audio that never existed.
        # Otherwise expected_bytes() would drift out of step with what was sent.
        if self.inject_callback_error is not None:
            error, self.inject_callback_error = self.inject_callback_error, None
            raise error
        payload = self._source.read(self._state.frames_produced, frames, self._profile)
        self._state.frames_produced += frames
        self._state.callback_invocations += 1
        self._callback(payload, frames, effective or CallbackStatus())
        return frames

    def _run_realtime(self) -> None:
        block_seconds = self._blocksize / self._profile.sample_rate
        interval = block_seconds / self._speed
        next_deadline = time.monotonic()
        while not self._state.stop_flag.is_set() and self._state.started:
            if self._total_frames is not None and self._state.frames_produced >= self._total_frames:
                break
            frames = self._blocksize
            if self._total_frames is not None:
                frames = min(frames, self._total_frames - self._state.frames_produced)
            try:
                self._emit(frames, None)
            except BaseException:  # noqa: BLE001 - a real callback error ends the stream
                break
            next_deadline += interval
            delay = next_deadline - time.monotonic()
            if delay > 0:
                self._state.stop_flag.wait(delay)
            else:
                next_deadline = time.monotonic()

    def expected_bytes(self, frames: int, start_frame: int = 0) -> bytes:
        """Regenerate what the source should have produced, for byte comparison."""
        return self._source.read(start_frame, frames, self._profile)


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class FakeAudioBackend:
    """In-process :class:`~mom_igd.audio.backend.AudioBackend` implementation."""

    def __init__(
        self,
        devices: list[RawDeviceInfo] | None = None,
        *,
        source: PcmSource | None = None,
        blocksize: int = 1024,
        realtime: bool = False,
        speed: float = 1.0,
        total_frames: int | None = None,
    ) -> None:
        self._devices = list(devices) if devices is not None else default_fake_devices()
        self.source: PcmSource = source or CounterSource()
        self.blocksize = blocksize
        self.realtime = realtime
        self.speed = speed
        self.total_frames = total_frames
        # Injectable faults.
        self.fail_on_open: BaseException | None = None
        self.unsupported_devices: set[int] = set()
        self.list_devices_error: BaseException | None = None
        # Observability for leak assertions.
        self.open_calls: int = 0
        self.streams: list[FakeStream] = []

    # -- protocol -----------------------------------------------------------

    @property
    def name(self) -> str:
        return "fake"

    def describe(self) -> dict[str, object]:
        return {
            "backend": "fake",
            "library_version": "n/a",
            "portaudio_version": "n/a",
            "deterministic": True,
            "source": self.source.label,
        }

    def list_devices(self) -> list[RawDeviceInfo]:
        if self.list_devices_error is not None:
            raise self.list_devices_error
        return list(self._devices)

    def check_input_settings(self, device_index: int, profile: CaptureProfile) -> None:
        device = self._require(device_index)
        if not device.is_input_capable:
            raise UnsupportedProfileError(
                f"Device {device.name!r} has no input channels; it cannot capture."
            )
        if device_index in self.unsupported_devices:
            raise UnsupportedProfileError(
                f"Device {device.name!r} rejected the requested profile "
                f"({profile.sample_rate} Hz, {profile.channels} ch)."
            )
        if profile.channels > device.max_input_channels:
            raise UnsupportedProfileError(
                f"Device {device.name!r} offers {device.max_input_channels} input "
                f"channel(s); {profile.channels} requested."
            )

    def open_input_stream(
        self,
        device_index: int,
        profile: CaptureProfile,
        callback: CaptureCallback,
    ) -> FakeStream:
        self.check_input_settings(device_index, profile)
        if self.fail_on_open is not None:
            error, self.fail_on_open = self.fail_on_open, None
            raise error
        self.open_calls += 1
        stream = FakeStream(
            profile,
            callback,
            self.source,
            blocksize=self.blocksize,
            realtime=self.realtime,
            speed=self.speed,
            total_frames=self.total_frames,
            on_close=self._forget,
        )
        self.streams.append(stream)
        return stream

    # -- test helpers -------------------------------------------------------

    def _require(self, device_index: int) -> RawDeviceInfo:
        for device in self._devices:
            if device.index == device_index:
                return device
        raise DeviceNotFoundError(
            f"No audio device with index {device_index}. Available: "
            f"{[d.index for d in self._devices]}."
        )

    def _forget(self, stream: FakeStream) -> None:
        # Keep the object for assertions but mark it as no longer open.
        pass

    @property
    def open_streams(self) -> list[FakeStream]:
        return [s for s in self.streams if not s.closed]

    def set_devices(self, devices: list[RawDeviceInfo]) -> None:
        self._devices = list(devices)

    def remove_device(self, device_index: int) -> None:
        """Simulate a microphone being unplugged."""
        self._devices = [d for d in self._devices if d.index != device_index]

    def reindex(self, offset: int = 1) -> None:
        """Simulate PortAudio renumbering devices after a reboot or replug.

        Names and channel counts are preserved, so a correct implementation still
        matches the saved device by fingerprint.
        """
        self._devices = [replace(d, index=d.index + offset) for d in self._devices]
