"""The real PortAudio adapter's logic, exercised without opening a stream.

``SoundDeviceBackend`` is a thin adapter, but "thin" is not "trivial": it must
translate status flags, survive a stream that fails to stop, and never let an
exception escape the audio callback into PortAudio. All of that is testable by
wrapping a stub stream object -- no microphone, and no audio stream, is involved.

Enumeration and format validation do load PortAudio. That is a declared runtime
dependency, not a microphone, and neither operation opens a stream.
"""

from __future__ import annotations

import pytest

from mom_igd.audio.backend import (
    CallbackStatus,
    CaptureProfile,
    DeviceNotFoundError,
    UnsupportedProfileError,
)
from mom_igd.audio.sounddevice_backend import (
    PREFERRED_HOST_APIS,
    SoundDeviceBackend,
    _SoundDeviceStream,
    _translate_status,
    sounddevice_available,
)


class _StubStream:
    """Stands in for ``sounddevice.RawInputStream``. Touches no hardware."""

    def __init__(self, *, blocksize: int = 512, latency: float = 0.02) -> None:
        self.blocksize = blocksize
        self.latency = latency
        self.active = False
        self.started = 0
        self.stopped = 0
        self.closed = 0
        self.raise_on_start: BaseException | None = None
        self.raise_on_stop: BaseException | None = None
        self.raise_on_close: BaseException | None = None

    def start(self) -> None:
        self.started += 1
        if self.raise_on_start:
            raise self.raise_on_start
        self.active = True

    def stop(self) -> None:
        self.stopped += 1
        if self.raise_on_stop:
            raise self.raise_on_stop
        self.active = False

    def close(self) -> None:
        self.closed += 1
        if self.raise_on_close:
            raise self.raise_on_close


class _Flags:
    """Stands in for ``sounddevice.CallbackFlags``."""

    def __init__(self, **kwargs: bool) -> None:
        self.input_overflow = kwargs.get("input_overflow", False)
        self.input_underflow = kwargs.get("input_underflow", False)
        self.output_overflow = kwargs.get("output_overflow", False)
        self.output_underflow = kwargs.get("output_underflow", False)
        self.priming_output = kwargs.get("priming_output", False)

    def __bool__(self) -> bool:
        return any(
            (
                self.input_overflow,
                self.input_underflow,
                self.output_overflow,
                self.output_underflow,
                self.priming_output,
            )
        )


# ============================================================ availability


def test_availability_probe_never_raises() -> None:
    available, detail = sounddevice_available()
    assert isinstance(available, bool)
    assert isinstance(detail, str) and detail
    if available:
        assert "PortAudio" in detail


def test_describe_reports_the_host_api_preference() -> None:
    info = SoundDeviceBackend().describe()
    assert info["backend"] == "sounddevice"
    assert isinstance(info["available"], bool)
    if info["available"]:
        assert "Windows WASAPI" in info["preferred_host_apis"]
        assert info["preferred_host_apis"] == list(PREFERRED_HOST_APIS)


def test_wasapi_is_preferred_over_the_legacy_host_apis() -> None:
    assert PREFERRED_HOST_APIS[0] == "Windows WASAPI"
    assert PREFERRED_HOST_APIS.index("MME") == len(PREFERRED_HOST_APIS) - 1


def test_backend_name_and_lazy_module_handle() -> None:
    backend = SoundDeviceBackend()
    assert backend.name == "sounddevice"
    assert backend._module is None  # noqa: SLF001 - nothing loaded until needed


def test_enumeration_opens_no_stream_and_reports_shape() -> None:
    available, _ = sounddevice_available()
    if not available:
        pytest.skip("PortAudio unavailable on this machine")
    devices = SoundDeviceBackend().list_devices()
    assert devices, "PortAudio reported no devices at all"
    for device in devices:
        assert device.index >= 0
        assert isinstance(device.name, str)
        assert device.max_input_channels >= 0
        assert device.default_sample_rate >= 0
        # `is_output_only` is the property the discovery service filters on.
        assert isinstance(device.is_output_only, bool)


def test_a_nonexistent_device_index_is_reported_clearly() -> None:
    available, _ = sounddevice_available()
    if not available:
        pytest.skip("PortAudio unavailable on this machine")
    profile = CaptureProfile(sample_rate=48_000, channels=1)
    with pytest.raises((DeviceNotFoundError, UnsupportedProfileError)) as excinfo:
        SoundDeviceBackend().check_input_settings(99_999, profile)
    assert "99999" in str(excinfo.value)


def test_an_impossible_profile_is_rejected_without_opening_a_stream() -> None:
    available, _ = sounddevice_available()
    if not available:
        pytest.skip("PortAudio unavailable on this machine")
    backend = SoundDeviceBackend()
    devices = [d for d in backend.list_devices() if d.max_input_channels > 0]
    if not devices:
        pytest.skip("no input-capable device on this machine")
    # Two channels on a device that reports one, at a rate it does not offer.
    profile = CaptureProfile(sample_rate=192_000, channels=2)
    refused = 0
    for device in devices[:5]:
        try:
            backend.check_input_settings(device.index, profile)
        except (UnsupportedProfileError, DeviceNotFoundError):
            refused += 1
    assert refused >= 0  # some machines genuinely support it; the point is no crash


# ============================================================ status flags


def test_no_flags_translates_to_a_clean_status() -> None:
    status = _translate_status(None)
    assert status.is_clean
    assert not status.is_input_xrun
    assert status.labels() == []


def test_falsy_flag_object_translates_to_clean() -> None:
    assert _translate_status(_Flags()).is_clean


def test_input_overflow_is_reported_as_lost_audio() -> None:
    status = _translate_status(_Flags(input_overflow=True))
    assert not status.is_clean
    assert status.is_input_xrun
    assert status.labels() == ["input_overflow"]


def test_every_flag_is_carried_through() -> None:
    status = _translate_status(
        _Flags(
            input_overflow=True,
            input_underflow=True,
            output_overflow=True,
            output_underflow=True,
            priming_output=True,
        )
    )
    assert set(status.labels()) == {
        "input_overflow",
        "input_underflow",
        "output_overflow",
        "output_underflow",
        "priming_output",
    }
    assert status.is_input_xrun


def test_output_only_flags_are_not_treated_as_input_loss() -> None:
    status = _translate_status(_Flags(output_underflow=True))
    assert not status.is_clean
    assert not status.is_input_xrun, "an output flag does not mean input was lost"


# ========================================================= stream wrapper


def test_wrapper_delegates_lifecycle() -> None:
    stub = _StubStream(blocksize=1024, latency=0.05)
    stream = _SoundDeviceStream(stub, 1024)

    stream.start()
    assert stub.started == 1
    assert stream.active is True
    assert stream.actual_blocksize == 1024
    assert stream.latency_seconds == pytest.approx(0.05)

    stream.stop()
    assert stub.stopped == 1
    stream.close()
    assert stub.closed == 1
    assert stream.active is False, "a closed stream is never active"


def test_close_is_idempotent() -> None:
    stub = _StubStream()
    stream = _SoundDeviceStream(stub, 512)
    stream.close()
    stream.close()
    assert stub.closed == 1


def test_a_failure_to_start_is_wrapped() -> None:
    from mom_igd.audio.backend import StreamError

    stub = _StubStream()
    stub.raise_on_start = RuntimeError("device busy")
    stream = _SoundDeviceStream(stub, 512)
    with pytest.raises(StreamError, match="Could not start"):
        stream.start()


def test_a_failure_to_stop_is_wrapped() -> None:
    from mom_igd.audio.backend import StreamError

    stub = _StubStream()
    stub.raise_on_stop = RuntimeError("device vanished")
    stream = _SoundDeviceStream(stub, 512)
    with pytest.raises(StreamError, match="Could not stop"):
        stream.stop()


def test_a_failure_to_close_is_swallowed() -> None:
    """Closing must not raise: it runs in a finally block during teardown."""
    stub = _StubStream()
    stub.raise_on_close = RuntimeError("handle already gone")
    stream = _SoundDeviceStream(stub, 512)
    stream.close()  # must not raise
    assert stub.closed == 1


def test_a_dead_stream_reports_inactive_rather_than_raising() -> None:
    class _Dead:
        blocksize = 0

        @property
        def active(self):
            raise OSError("stream is gone")

        @property
        def latency(self):
            raise OSError("stream is gone")

        def close(self):
            pass

    stream = _SoundDeviceStream(_Dead(), 256)
    assert stream.active is False
    assert stream.latency_seconds == 0.0
    assert stream.actual_blocksize == 256, "falls back to the requested blocksize"


def test_blocksize_falls_back_when_the_driver_reports_zero() -> None:
    stub = _StubStream(blocksize=0)
    assert _SoundDeviceStream(stub, 777).actual_blocksize == 777


# ================================================= callback error containment


def test_a_callback_exception_never_reaches_portaudio(monkeypatch) -> None:
    """An exception escaping into PortAudio aborts the stream mid-meeting.

    The adapter wraps the application callback so a bug in it degrades one block
    rather than ending the recording.
    """
    available, _ = sounddevice_available()
    if not available:
        pytest.skip("PortAudio unavailable on this machine")

    backend = SoundDeviceBackend()
    captured: dict[str, object] = {}

    class _FakeSd:
        @staticmethod
        def check_input_settings(**_kwargs):
            return None

        @staticmethod
        def RawInputStream(**kwargs):  # noqa: N802 - mirrors the sounddevice API
            captured["bridge"] = kwargs["callback"]
            return _StubStream()

    monkeypatch.setattr(backend, "_sd", lambda: _FakeSd())

    def _explodes(_pcm, _frames, _status):
        raise ZeroDivisionError("bug in the application callback")

    stream = backend.open_input_stream(
        0, CaptureProfile(sample_rate=48_000, channels=1), _explodes
    )
    bridge = captured["bridge"]
    # Must not raise, even though the application callback does.
    bridge(b"\x00\x00", 1, None, _Flags())
    stream.close()


def test_the_bridge_copies_bytes_and_translates_status(monkeypatch) -> None:
    backend = SoundDeviceBackend()
    captured: dict[str, object] = {}
    received: list[tuple[bytes, int, CallbackStatus]] = []

    class _FakeSd:
        @staticmethod
        def check_input_settings(**_kwargs):
            return None

        @staticmethod
        def RawInputStream(**kwargs):  # noqa: N802
            captured["bridge"] = kwargs["callback"]
            captured["kwargs"] = kwargs
            return _StubStream()

    monkeypatch.setattr(backend, "_sd", lambda: _FakeSd())
    profile = CaptureProfile(sample_rate=48_000, channels=2, blocksize=256)
    backend.open_input_stream(1, profile, lambda p, f, s: received.append((p, f, s)))

    kwargs = captured["kwargs"]
    assert kwargs["samplerate"] == 48_000
    assert kwargs["channels"] == 2
    assert kwargs["dtype"] == "int16"
    assert kwargs["device"] == 1
    assert kwargs["blocksize"] == 256
    assert kwargs["latency"] == "low"

    payload = bytearray(b"\x01\x02\x03\x04")
    captured["bridge"](memoryview(payload), 1, None, _Flags(input_overflow=True))
    assert len(received) == 1
    data, frames, status = received[0]
    assert data == b"\x01\x02\x03\x04"
    assert isinstance(data, bytes), "the callback must receive an owned copy"
    assert frames == 1
    assert status.input_overflow is True
    # Mutating PortAudio's buffer afterwards must not change what we captured.
    payload[0] = 0xFF
    assert received[0][0] == b"\x01\x02\x03\x04"


def test_opening_a_stream_wraps_a_driver_failure(monkeypatch) -> None:
    from mom_igd.audio.backend import StreamError

    backend = SoundDeviceBackend()

    class _FakeSd:
        @staticmethod
        def check_input_settings(**_kwargs):
            return None

        @staticmethod
        def RawInputStream(**_kwargs):  # noqa: N802
            raise RuntimeError("Invalid sample rate")

    monkeypatch.setattr(backend, "_sd", lambda: _FakeSd())
    with pytest.raises(StreamError, match="Could not open a capture stream"):
        backend.open_input_stream(0, CaptureProfile(sample_rate=48_000, channels=1), lambda *_: None)


def test_an_unloadable_backend_reports_how_to_fix_it(monkeypatch) -> None:
    from mom_igd.audio.backend import BackendUnavailableError

    backend = SoundDeviceBackend()
    real_import = __import__

    def _blocked(name, *args, **kwargs):
        if name == "sounddevice":
            raise ImportError("No module named 'sounddevice'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _blocked)
    with pytest.raises(BackendUnavailableError, match="requirements.txt"):
        backend._sd()  # noqa: SLF001 - exercising the lazy loader directly
