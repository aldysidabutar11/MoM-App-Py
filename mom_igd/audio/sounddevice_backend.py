"""Real audio backend: PortAudio via ``sounddevice``, WASAPI on Windows.

``sounddevice`` (and therefore PortAudio) is imported **lazily**. Importing this
module, or the ``mom_igd.audio`` package, must never load an audio driver, and
must never open a microphone. A stream is opened only from
:meth:`SoundDeviceBackend.open_input_stream`, which is reached only through an
explicit user action.

``RawInputStream`` is used rather than ``InputStream`` because it delivers raw
bytes. That keeps NumPy out of the dependency set entirely and keeps the audio
callback free of array construction: the callback copies bytes and enqueues them,
nothing more.
"""

from __future__ import annotations

import platform
from typing import Any, Final

from mom_igd.audio.backend import (
    BackendUnavailableError,
    CallbackStatus,
    CaptureCallback,
    CaptureProfile,
    DeviceNotFoundError,
    RawDeviceInfo,
    StreamError,
    UnsupportedProfileError,
)
from mom_igd.logging_setup import get_logger

__all__ = ["PREFERRED_HOST_APIS", "SoundDeviceBackend", "sounddevice_available"]

_LOG = get_logger("audio.backend")

PREFERRED_HOST_APIS: Final[tuple[str, ...]] = (
    "Windows WASAPI",
    "Windows WDM-KS",
    "Windows DirectSound",
    "MME",
)
"""Host API preference on Windows, best first.

WASAPI is preferred because it is the modern Windows audio path with the lowest
latency and the most accurate device metadata. It is used in **shared** mode:
exclusive mode would block every other application from the microphone and can
fail outright on devices that do not support the requested format. No Windows
audio setting is ever changed by this application.
"""


def sounddevice_available() -> tuple[bool, str]:
    """Report whether the backend can be loaded, without loading a stream.

    Returns:
        ``(available, detail)``. Never raises.
    """
    try:
        import sounddevice  # noqa: PLC0415 - deliberate lazy import
    except Exception as exc:  # noqa: BLE001 - any import failure is a plain "no"
        return False, f"{type(exc).__name__}: {exc}"
    try:
        version = getattr(sounddevice, "__version__", "unknown")
        portaudio = sounddevice.get_portaudio_version()[1]
    except Exception as exc:  # noqa: BLE001 - PortAudio present but unusable
        return False, f"sounddevice imported but PortAudio failed: {exc}"
    return True, f"sounddevice {version}, {portaudio}"


class _SoundDeviceStream:
    """Adapter around ``sounddevice.RawInputStream``."""

    def __init__(self, stream: Any, blocksize: int) -> None:
        self._stream = stream
        self._blocksize = blocksize
        self._closed = False

    def start(self) -> None:
        try:
            self._stream.start()
        except Exception as exc:  # noqa: BLE001
            raise StreamError(f"Could not start the capture stream: {exc}") from exc

    def stop(self) -> None:
        try:
            self._stream.stop()
        except Exception as exc:  # noqa: BLE001
            raise StreamError(f"Could not stop the capture stream: {exc}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.close()
        except Exception as exc:  # noqa: BLE001 - closing must not mask a failure
            _LOG.warning("Error while closing the capture stream: %s", exc)

    @property
    def active(self) -> bool:
        if self._closed:
            return False
        try:
            return bool(self._stream.active)
        except Exception:  # noqa: BLE001 - a dead stream is simply not active
            return False

    @property
    def actual_blocksize(self) -> int:
        try:
            return int(self._stream.blocksize) or self._blocksize
        except Exception:  # noqa: BLE001
            return self._blocksize

    @property
    def latency_seconds(self) -> float:
        try:
            return float(self._stream.latency)
        except Exception:  # noqa: BLE001
            return 0.0


class SoundDeviceBackend:
    """PortAudio-backed capture. Opens hardware only when explicitly asked."""

    def __init__(self) -> None:
        self._module: Any | None = None

    # -- lazy loading -------------------------------------------------------

    def _sd(self) -> Any:
        if self._module is None:
            try:
                import sounddevice  # noqa: PLC0415 - deliberate lazy import
            except Exception as exc:  # noqa: BLE001
                raise BackendUnavailableError(
                    "The audio backend could not be loaded: "
                    f"{type(exc).__name__}: {exc}. Install the Phase 2 runtime "
                    "dependencies (`pip install -r requirements.txt`)."
                ) from exc
            self._module = sounddevice
        return self._module

    # -- protocol -----------------------------------------------------------

    @property
    def name(self) -> str:
        return "sounddevice"

    def describe(self) -> dict[str, object]:
        available, detail = sounddevice_available()
        info: dict[str, object] = {
            "backend": "sounddevice",
            "available": available,
            "detail": detail,
            "platform": platform.system(),
        }
        if not available:
            return info
        sd = self._sd()
        try:
            info["library_version"] = getattr(sd, "__version__", "unknown")
            info["portaudio_version"] = sd.get_portaudio_version()[1]
            info["host_apis"] = [api["name"] for api in sd.query_hostapis()]
            info["preferred_host_apis"] = list(PREFERRED_HOST_APIS)
        except Exception as exc:  # noqa: BLE001
            info["error"] = str(exc)
        return info

    def list_devices(self) -> list[RawDeviceInfo]:
        """Enumerate input-capable and output-only devices. Opens no stream."""
        sd = self._sd()
        try:
            raw_devices = sd.query_devices()
            host_apis = sd.query_hostapis()
            default_input = sd.default.device[0] if sd.default.device else -1
        except Exception as exc:  # noqa: BLE001
            raise BackendUnavailableError(f"Device enumeration failed: {exc}") from exc

        devices: list[RawDeviceInfo] = []
        for index, entry in enumerate(raw_devices):
            host_index = int(entry.get("hostapi", -1))
            host_name = "unknown"
            if 0 <= host_index < len(host_apis):
                host_name = str(host_apis[host_index].get("name", "unknown"))
            devices.append(
                RawDeviceInfo(
                    index=index,
                    name=str(entry.get("name", "")).strip(),
                    host_api=host_name,
                    max_input_channels=int(entry.get("max_input_channels", 0)),
                    max_output_channels=int(entry.get("max_output_channels", 0)),
                    default_sample_rate=float(entry.get("default_samplerate", 0.0)),
                    default_low_input_latency=float(entry.get("default_low_input_latency", 0.0)),
                    default_high_input_latency=float(entry.get("default_high_input_latency", 0.0)),
                    is_default_input=(index == default_input),
                )
            )
        return devices

    def check_input_settings(self, device_index: int, profile: CaptureProfile) -> None:
        sd = self._sd()
        try:
            sd.check_input_settings(
                device=device_index,
                channels=profile.channels,
                dtype=profile.sample_format.sounddevice_dtype,
                samplerate=profile.sample_rate,
            )
        except ValueError as exc:
            raise DeviceNotFoundError(
                f"Audio device index {device_index} does not exist: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - sd.PortAudioError and friends
            raise UnsupportedProfileError(
                f"Device index {device_index} cannot provide "
                f"{profile.sample_rate} Hz / {profile.channels} ch / "
                f"{profile.sample_format.value}: {exc}"
            ) from exc

    def open_input_stream(
        self,
        device_index: int,
        profile: CaptureProfile,
        callback: CaptureCallback,
    ) -> _SoundDeviceStream:
        """Open (but do not start) a capture stream. This engages the microphone."""
        sd = self._sd()
        self.check_input_settings(device_index, profile)

        def _bridge(indata: Any, frames: int, _time: Any, status: Any) -> None:
            # Runs on PortAudio's real-time thread. Copy and hand over; never
            # raise, because an exception here aborts the stream.
            try:
                callback(bytes(indata), int(frames), _translate_status(status))
            except BaseException as exc:  # noqa: BLE001 - must not reach PortAudio
                _LOG.error("Capture callback raised (suppressed): %r", exc)

        try:
            stream = sd.RawInputStream(
                samplerate=profile.sample_rate,
                blocksize=profile.blocksize,
                device=device_index,
                channels=profile.channels,
                dtype=profile.sample_format.sounddevice_dtype,
                latency="low",
                callback=_bridge,
            )
        except Exception as exc:  # noqa: BLE001
            raise StreamError(
                f"Could not open a capture stream on device index {device_index}: "
                f"{exc}"
            ) from exc
        return _SoundDeviceStream(stream, profile.blocksize or 0)


def _translate_status(status: Any) -> CallbackStatus:
    """Map ``sounddevice.CallbackFlags`` onto our backend-independent status."""
    if not status:
        return CallbackStatus()
    return CallbackStatus(
        input_overflow=bool(getattr(status, "input_overflow", False)),
        input_underflow=bool(getattr(status, "input_underflow", False)),
        output_overflow=bool(getattr(status, "output_overflow", False)),
        output_underflow=bool(getattr(status, "output_underflow", False)),
        priming_output=bool(getattr(status, "priming_output", False)),
    )
