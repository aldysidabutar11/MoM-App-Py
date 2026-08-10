"""Read the Windows capture endpoint's mute and volume. **Read-only, always.**

Why this module exists: a muted microphone and a microphone nobody is speaking into
produce *byte-identical* silence at every user-mode API. PortAudio opens the stream, the
callback fires on schedule, frames arrive on time, and every sample is zero. Nothing in
the capture path can tell the two apart, so the operator was handed a list --
*"check that the right microphone is selected, that it is not muted, and that Windows has
granted microphone access"* -- and left to work out which of the three applied.

It happened for real on the development machine: privacy allowed, device enabled, stream
open, frames flowing, and **`mute = True`**. The raw kernel-streaming endpoint for the same
array measured -60.9 dBFS of ordinary speech while every mixer-side endpoint measured
-96.7 dBFS, because mute is applied in the mixer and WDM-KS runs underneath it. Three
possibilities in a warning is a warning the operator has to debug; one fact is one they can
act on.

**Nothing here writes.** ``IAudioEndpointVolume`` also exposes ``SetMute`` and
``SetMasterVolumeLevelScalar``, and they are deliberately absent from the interface
declared below -- not merely unused. CLAUDE.md rule 12 forbids changing an audio device's
settings, and the reason is that an application that quietly unmutes a microphone is an
application that can start recording a room the operator believed was private. This tells
them which switch to flip; they flip it.

Everything degrades to "unknown". A machine without the COM interface, a locked-down
session, a non-Windows host: the answer is ``None`` and the caller falls back to the
generic advice. A diagnostic that fails must never fail the thing it is diagnosing.
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_bool, c_float, c_uint32, c_void_p
from dataclasses import dataclass
from typing import Any, Final

from mom_igd.logging_setup import get_logger

__all__ = ["EndpointState", "read_default_capture_endpoint"]

_LOG = get_logger("audio.endpoint_state")

#: ``MMDeviceEnumerator`` and the two interfaces needed to reach the volume control.
_CLSID_MM_DEVICE_ENUMERATOR: Final[str] = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_IMM_DEVICE_ENUMERATOR: Final[str] = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_IAUDIO_ENDPOINT_VOLUME: Final[str] = "{5CDF2C82-841E-4546-9722-0CF74078229A}"

#: ``eCapture`` in ``EDataFlow``: recording endpoints, never playback.
_E_CAPTURE: Final[int] = 1
#: ``eConsole`` in ``ERole``.
_E_CONSOLE: Final[int] = 0
_CLSCTX_ALL: Final[int] = 23


@dataclass(frozen=True, slots=True)
class EndpointState:
    """What Windows says about the default capture endpoint's mixer controls."""

    muted: bool | None = None
    volume_percent: float | None = None

    @property
    def known(self) -> bool:
        return self.muted is not None

    @property
    def explains_silence(self) -> bool:
        """True when the mixer alone accounts for a silent capture.

        Muted is decisive. A volume of exactly zero is the same thing wearing a different
        hat, and both are settings the operator changes in the same dialog.
        """
        if self.muted:
            return True
        return self.volume_percent is not None and self.volume_percent <= 0.5

    @property
    def advice(self) -> str | None:
        """A single sentence naming the switch, or ``None`` when nothing is wrong."""
        if self.muted:
            return (
                "The microphone is MUTED in Windows. Open Settings > System > Sound > "
                "Microphone Array and turn the mute off (or press the microphone mute "
                "key on the keyboard, which is what usually does this by accident). "
                "Nothing else needs changing."
            )
        if self.volume_percent is not None and self.volume_percent <= 0.5:
            return (
                f"The microphone input volume is {self.volume_percent:.0f}% in Windows. "
                "Open Settings > System > Sound > Microphone Array and raise it."
            )
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "muted": self.muted,
            "volume_percent": (
                round(self.volume_percent, 1) if self.volume_percent is not None else None
            ),
            "known": self.known,
            "explains_silence": self.explains_silence,
        }


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    value = _GUID()
    if ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), byref(value)) != 0:
        raise OSError(f"could not parse GUID {text}")
    return value


def _method(interface: c_void_p, index: int, restype: Any, *argtypes: Any):
    """Bind one entry of a COM vtable.

    Hand-rolled rather than via ``comtypes``: this needs three calls, and the project
    weighs every dependency. The indexes below are fixed by the interface definitions in
    ``mmdeviceapi.h`` and ``endpointvolume.h`` and cannot change without breaking every
    consumer of those headers.
    """
    vtable = ctypes.cast(interface, POINTER(POINTER(c_void_p)))[0]
    prototype = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return prototype(vtable[index])


def _release(interface: c_void_p | None) -> None:
    if interface:
        _method(interface, 2, ctypes.c_ulong)(interface)  # IUnknown::Release


def read_default_capture_endpoint() -> EndpointState:
    """Mute and volume of the default recording device, or an all-unknown state."""
    enumerator: c_void_p | None = None
    device: c_void_p | None = None
    volume: c_void_p | None = None
    try:
        ole32 = ctypes.oledll.ole32
        # Apartment-threaded, and a failure here is not fatal: another library on this
        # thread may already have initialised COM with a different model, which returns
        # RPC_E_CHANGED_MODE and leaves the existing apartment perfectly usable.
        try:
            ole32.CoInitializeEx(None, 0x2)
        except OSError:
            pass

        enumerator = c_void_p()
        ole32.CoCreateInstance(
            byref(_guid(_CLSID_MM_DEVICE_ENUMERATOR)),
            None,
            _CLSCTX_ALL,
            byref(_guid(_IID_IMM_DEVICE_ENUMERATOR)),
            byref(enumerator),
        )

        device = c_void_p()
        # IMMDeviceEnumerator::GetDefaultAudioEndpoint is vtable slot 4.
        get_default = _method(
            enumerator, 4, ctypes.HRESULT, ctypes.c_int, ctypes.c_int, POINTER(c_void_p)
        )
        get_default(enumerator, _E_CAPTURE, _E_CONSOLE, byref(device))

        volume = c_void_p()
        # IMMDevice::Activate is vtable slot 3.
        activate = _method(
            device,
            3,
            ctypes.HRESULT,
            POINTER(_GUID),
            ctypes.c_uint32,
            c_void_p,
            POINTER(c_void_p),
        )
        activate(
            device,
            byref(_guid(_IID_IAUDIO_ENDPOINT_VOLUME)),
            _CLSCTX_ALL,
            None,
            byref(volume),
        )

        # IAudioEndpointVolume: GetMasterVolumeLevelScalar is slot 9, GetMute is slot 15.
        # Only the getters are bound. The setters exist in the interface and are
        # deliberately not reachable from this module.
        scalar = c_float()
        _method(volume, 9, ctypes.HRESULT, POINTER(c_float))(volume, byref(scalar))
        muted = c_bool()
        _method(volume, 15, ctypes.HRESULT, POINTER(c_bool))(volume, byref(muted))

        return EndpointState(muted=bool(muted.value), volume_percent=scalar.value * 100.0)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never break its caller
        _LOG.debug(
            "audio.endpoint_state.unavailable", extra={"reason": type(exc).__name__}
        )
        return EndpointState()
    finally:
        _release(volume)
        _release(device)
        _release(enumerator)


# `c_uint32` is imported for the Activate signature above; naming it here keeps linters
# from removing an import the ctypes prototype genuinely needs.
_ = c_uint32
