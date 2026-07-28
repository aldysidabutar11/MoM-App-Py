"""Offline audio capture (Phase 2).

Scope: capture meeting audio with correct quality, ordering, integrity and
metadata. This package deliberately knows **nothing** about who is speaking.
There is no VAD, no speech segmentation, no ASR, no diarization, no speaker
embedding and no LLM here, and none may be added -- those belong to Phase 4
onwards.

Level monitoring (RMS, peak, clipping, silence, noise floor, per-channel
activity) *is* implemented, because it is signal-quality measurement rather than
speech detection: it answers "is this recording usable?", never "is someone
talking?" and never "who is talking?".

Layering, hardware at the bottom:

* :mod:`mom_igd.audio.backend` -- ``AudioBackend`` protocol, capture profile,
  device description, error types. No hardware access.
* :mod:`mom_igd.audio.fake_backend` -- deterministic in-process backend used by
  every automated test. No microphone is ever required.
* :mod:`mom_igd.audio.sounddevice_backend` -- the real PortAudio/WASAPI backend.
  ``sounddevice`` is imported lazily so importing this package never touches
  audio hardware.
* :mod:`mom_igd.audio.devices` -- discovery, stable fingerprints, transport
  resolution, capture-profile validation.
* :mod:`mom_igd.audio.frame_queue` -- bounded queue between callback and writer.
* :mod:`mom_igd.audio.quality` -- PCM16 level metering, standard library only.
* :mod:`mom_igd.audio.writer` -- crash-safe chunk writer.
* :mod:`mom_igd.audio.manifest` -- append-only manifest and verification.
* :mod:`mom_igd.audio.calibration` -- 10-15 s microphone test.
* :mod:`mom_igd.audio.preflight` -- pre-recording gate.
* :mod:`mom_igd.audio.session` -- callback plus writer thread orchestration.
* :mod:`mom_igd.audio.recovery` -- crash recovery for interrupted recordings.
* :mod:`mom_igd.audio.service` -- recording lifecycle, database and audit.

**No microphone is opened by importing anything.** A stream is opened only by an
explicit user action: a CLI command, an API call or a button press.
"""

from mom_igd.audio.backend import (
    AudioBackend,
    AudioError,
    BackendUnavailableError,
    CallbackStatus,
    CaptureProfile,
    DeviceNotFoundError,
    DeviceTransport,
    RawDeviceInfo,
    SampleFormat,
    StreamError,
    UnsupportedProfileError,
)

__all__ = [
    "AudioBackend",
    "AudioError",
    "BackendUnavailableError",
    "CallbackStatus",
    "CaptureProfile",
    "DeviceNotFoundError",
    "DeviceTransport",
    "RawDeviceInfo",
    "SampleFormat",
    "StreamError",
    "UnsupportedProfileError",
]
