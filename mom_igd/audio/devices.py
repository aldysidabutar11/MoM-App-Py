"""Device discovery, stable identity, transport resolution and validation.

Three rules drive this module.

**A PortAudio index is not an identity.** Indices shift when a device is plugged
in, unplugged, or after a reboot, so persisting one and reusing it later can point
at a completely different microphone. Devices are identified by a
:func:`device_fingerprint` derived from host API, name and channel count, and the
index is looked up fresh every time.

**Never fall back silently.** If the microphone the operator chose is gone, the
answer is an error that says so, not a quiet switch to whatever else is
available. Recording a room full of people through the wrong microphone -- or
through a laptop lid array when a conference microphone was expected -- produces
audio nobody can fix afterwards.

**Never guess the transport.** ``USB`` is only ever reported when Windows itself
says so, read from the ``MMDevices`` registry where the audio stack records each
endpoint's bus enumerator. A microphone named "USB Audio Device" may not be one,
and the Phase 2 production gate depends on this distinction being real. When it
cannot be verified the answer is :attr:`~DeviceTransport.UNKNOWN`, and the
operator is asked to confirm.
"""

from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass, field
from typing import Final, Iterable, Sequence

from mom_igd.audio.backend import (
    MAX_CHANNELS,
    SUPPORTED_SAMPLE_RATES,
    AudioBackend,
    CaptureProfile,
    DeviceNotFoundError,
    DeviceTransport,
    RawDeviceInfo,
    SampleFormat,
    UnsupportedProfileError,
)
from mom_igd.logging_setup import get_logger

__all__ = [
    "DeviceInfo",
    "DeviceDiscoveryService",
    "DeviceSelection",
    "WindowsAudioEndpoint",
    "device_fingerprint",
    "query_windows_capture_endpoints",
    "resolve_transport",
]

_LOG = get_logger("audio.devices")

_HOST_API_RANK: Final[dict[str, int]] = {
    "Windows WASAPI": 0,
    "Windows WDM-KS": 1,
    "Windows DirectSound": 2,
    "MME": 3,
}
_UNRANKED: Final[int] = 50

_LOOPBACK_MARKERS: Final[tuple[str, ...]] = ("loopback", "what-u-hear", "stereo mix", "wave out mix")
"""Name fragments that indicate an output-capture device.

Rejecting on a name is safe in a way that *asserting* on one is not: a false
positive costs one unusable device in the list, while a false "this is a USB
conference microphone" would defeat the production gate.
"""

_VIRTUAL_DEVICE_MARKERS: Final[tuple[str, ...]] = (
    "sound mapper",
    "primary sound capture driver",
    "primary sound driver",
)
"""Aggregate/virtual endpoints that are not a physical microphone.

MME's "Microsoft Sound Mapper" and DirectSound's "Primary Sound Capture Driver"
follow whatever Windows currently considers the default device. Recording a
meeting through one means the actual input can change underneath the recording
with no way to tell afterwards which microphone was used -- precisely the silent
substitution this module exists to prevent.
"""

_WHITESPACE = re.compile(r"\s+")

# MMDevices property keys. These are stable, documented Windows PROPERTYKEYs.
_PKEY_DEVICE_DESCRIPTION: Final[str] = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
_PKEY_INTERFACE_FRIENDLY_NAME: Final[str] = "{b3f8fa53-0004-438e-9003-51a46e139bf2},6"
_PKEY_ENUMERATOR_NAME: Final[str] = "{a45c254e-df1c-4efd-8020-67d146a850e0},24"
_MMDEVICES_CAPTURE_KEY: Final[str] = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
)
_DEVICE_STATE_ACTIVE: Final[int] = 1

_ENUMERATOR_TRANSPORT: Final[dict[str, DeviceTransport]] = {
    "USB": DeviceTransport.USB,
    "HDAUDIO": DeviceTransport.INTERNAL,
    "INTELAUDIO": DeviceTransport.INTERNAL,
    "HDMI": DeviceTransport.INTERNAL,
    "PCI": DeviceTransport.INTERNAL,
    "ACPI": DeviceTransport.INTERNAL,
    "ROOT": DeviceTransport.INTERNAL,
    "BTHENUM": DeviceTransport.BLUETOOTH,
    "BTHHFENUM": DeviceTransport.BLUETOOTH,
    "BTHLEENUM": DeviceTransport.BLUETOOTH,
    "BTH": DeviceTransport.BLUETOOTH,
}


def _normalise(text: str) -> str:
    return _WHITESPACE.sub(" ", (text or "").strip()).casefold()


# ---------------------------------------------------------------------------
# Stable identity
# ---------------------------------------------------------------------------


def device_fingerprint(
    host_api: str, name: str, max_input_channels: int, *, length: int = 32
) -> str:
    """Derive a stable identifier for a capture device.

    Built from host API, normalised name and input-channel count -- all metadata
    that survives a replug or a reboot. The PortAudio index is deliberately
    excluded: including it is exactly the bug this function exists to prevent.

    The same physical microphone exposed through two host APIs (WASAPI and MME,
    say) yields two fingerprints, which is correct: they are different capture
    paths with different capabilities.
    """
    payload = "|".join(
        (_normalise(host_api), _normalise(name), str(int(max_input_channels)))
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:length]


@dataclass(frozen=True, slots=True)
class DeviceSelection:
    """A persisted device preference.

    Stores the fingerprint plus the descriptive fields needed to re-find the
    device and to explain a mismatch to the operator. It deliberately does **not**
    store a PortAudio index as the identity; ``last_known_index`` is a hint used
    only for diagnostics.
    """

    fingerprint: str
    name: str
    host_api: str
    max_input_channels: int
    transport: DeviceTransport = DeviceTransport.UNKNOWN
    last_known_index: int | None = None

    @classmethod
    def from_device(cls, device: DeviceInfo) -> DeviceSelection:
        return cls(
            fingerprint=device.fingerprint,
            name=device.name,
            host_api=device.host_api,
            max_input_channels=device.max_input_channels,
            transport=device.transport,
            last_known_index=device.index,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "name": self.name,
            "host_api": self.host_api,
            "max_input_channels": self.max_input_channels,
            "transport": self.transport.value,
            "last_known_index": self.last_known_index,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> DeviceSelection:
        transport_raw = str(payload.get("transport", DeviceTransport.UNKNOWN.value))
        try:
            transport = DeviceTransport(transport_raw)
        except ValueError:
            transport = DeviceTransport.UNKNOWN
        return cls(
            fingerprint=str(payload["fingerprint"]),
            name=str(payload.get("name", "")),
            host_api=str(payload.get("host_api", "")),
            max_input_channels=int(payload.get("max_input_channels", 0) or 0),
            transport=transport,
            last_known_index=(
                int(payload["last_known_index"])
                if payload.get("last_known_index") is not None
                else None
            ),
        )


# ---------------------------------------------------------------------------
# Windows transport resolution (read-only registry)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WindowsAudioEndpoint:
    """One capture endpoint as Windows records it."""

    endpoint_id: str
    description: str
    interface_name: str
    enumerator: str
    active: bool

    @property
    def transport(self) -> DeviceTransport:
        return _ENUMERATOR_TRANSPORT.get(self.enumerator.upper(), DeviceTransport.UNKNOWN)


def query_windows_capture_endpoints() -> list[WindowsAudioEndpoint]:
    """Read capture endpoints from the Windows registry. Read-only; never writes.

    Returns an empty list on any non-Windows platform or on any failure --
    inability to verify is reported as :attr:`DeviceTransport.UNKNOWN`
    downstream, never as a guess.
    """
    if sys.platform != "win32":
        return []
    try:
        import winreg  # noqa: PLC0415 - Windows-only standard-library module
    except ImportError:  # pragma: no cover - win32 always has winreg
        return []

    endpoints: list[WindowsAudioEndpoint] = []
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, _MMDEVICES_CAPTURE_KEY, 0, winreg.KEY_READ
        ) as root:
            count = winreg.QueryInfoKey(root)[0]
            for position in range(count):
                try:
                    endpoint_id = winreg.EnumKey(root, position)
                except OSError:  # pragma: no cover - key vanished mid-enumeration
                    continue
                endpoint = _read_endpoint(winreg, root, endpoint_id)
                if endpoint is not None:
                    endpoints.append(endpoint)
    except OSError as exc:
        _LOG.debug("Windows audio endpoint registry unavailable: %s", exc)
        return []
    return endpoints


def _read_endpoint(winreg_module, root, endpoint_id: str) -> WindowsAudioEndpoint | None:
    try:
        with winreg_module.OpenKey(root, endpoint_id, 0, winreg_module.KEY_READ) as key:
            try:
                state = int(winreg_module.QueryValueEx(key, "DeviceState")[0])
            except OSError:
                state = _DEVICE_STATE_ACTIVE
            try:
                with winreg_module.OpenKey(
                    key, "Properties", 0, winreg_module.KEY_READ
                ) as properties:
                    description = _read_string(winreg_module, properties, _PKEY_DEVICE_DESCRIPTION)
                    interface = _read_string(
                        winreg_module, properties, _PKEY_INTERFACE_FRIENDLY_NAME
                    )
                    enumerator = _read_string(winreg_module, properties, _PKEY_ENUMERATOR_NAME)
            except OSError:
                return None
    except OSError:
        return None
    if not description and not interface:
        return None
    return WindowsAudioEndpoint(
        endpoint_id=endpoint_id,
        description=description,
        interface_name=interface,
        enumerator=enumerator,
        active=state == _DEVICE_STATE_ACTIVE,
    )


def _read_string(winreg_module, key, value_name: str) -> str:
    try:
        value = winreg_module.QueryValueEx(key, value_name)[0]
    except OSError:
        return ""
    return str(value).strip() if value is not None else ""


def split_device_name(name: str) -> tuple[str, str]:
    """Split ``"Microphone Array (Intel Smart Sound)"`` into head and adapter.

    PortAudio composes Windows device names as ``"<endpoint description>
    (<adapter friendly name>)"``. Splitting on the first ``" ("`` recovers the two
    parts, which is what makes an exact endpoint match possible.
    """
    text = (name or "").strip()
    marker = text.find(" (")
    if marker == -1:
        return text, ""
    head = text[:marker].strip()
    tail = text[marker + 2 :].rstrip()
    if tail.endswith(")"):
        tail = tail[:-1]
    return head, tail.strip()


def match_windows_endpoints(
    device_name: str, endpoints: Sequence[WindowsAudioEndpoint]
) -> list[WindowsAudioEndpoint]:
    """Find the Windows capture endpoints that correspond to a PortAudio device.

    Matching is on **exact equality** of the endpoint description with the head of
    the PortAudio name, not on substring containment. Containment is far too
    loose: a Bluetooth endpoint whose description is simply ``"Microphone"``
    matches almost every device name, which made the internal microphone array
    resolve to an ambiguous bus and report ``UNKNOWN``.
    """
    if not endpoints:
        return []
    head, tail = split_device_name(device_name)
    normalised_head = _normalise(head)
    matches = [
        endpoint
        for endpoint in endpoints
        if normalised_head and _normalise(endpoint.description) == normalised_head
    ]
    if matches:
        return matches
    normalised_tail = _normalise(tail)
    if normalised_tail:
        matches = [
            endpoint
            for endpoint in endpoints
            if endpoint.interface_name and _normalise(endpoint.interface_name) == normalised_tail
        ]
    return matches


def resolve_transport(
    device_name: str, endpoints: Sequence[WindowsAudioEndpoint]
) -> tuple[DeviceTransport, str, str]:
    """Read a device's physical bus from the Windows registry.

    Returns:
        ``(transport, source, evidence)``. ``source`` is
        ``"windows-mmdevices-registry"`` when Windows answered and
        ``"unverified"`` otherwise.

    Windows keeps stale endpoint records for every device ever attached, so
    **active endpoints are preferred** when resolving. If the active endpoints
    still disagree about the bus, the answer is
    :attr:`DeviceTransport.UNKNOWN` -- an ambiguous answer is reported as
    ambiguous rather than settled by picking one.
    """
    if not endpoints:
        return DeviceTransport.UNKNOWN, "unverified", "no Windows endpoint data available"

    matches = match_windows_endpoints(device_name, endpoints)
    if not matches:
        return (
            DeviceTransport.UNKNOWN,
            "unverified",
            "no Windows capture endpoint matches this device name",
        )

    considered = [m for m in matches if m.active] or matches
    transports = {m.transport for m in considered if m.transport is not DeviceTransport.UNKNOWN}
    enumerators = sorted({m.enumerator for m in considered if m.enumerator})

    if len(transports) == 1:
        return (
            transports.pop(),
            "windows-mmdevices-registry",
            f"enumerator={','.join(enumerators) or 'unknown'}"
            f"; matched {len(considered)} endpoint(s)"
            f"{'' if any(m.active for m in matches) else ' (all inactive)'}",
        )
    if not transports:
        return (
            DeviceTransport.UNKNOWN,
            "windows-mmdevices-registry",
            f"unrecognised enumerator={','.join(enumerators) or 'empty'}",
        )
    return (
        DeviceTransport.UNKNOWN,
        "windows-mmdevices-registry",
        "ambiguous: active endpoints disagree about the bus "
        f"({','.join(sorted(t.value for t in transports))})",
    )


# ---------------------------------------------------------------------------
# Enriched device
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """A capture device with a stable identity and a verified-or-unknown bus."""

    index: int
    name: str
    host_api: str
    max_input_channels: int
    max_output_channels: int
    default_sample_rate: int
    default_low_input_latency: float
    default_high_input_latency: float
    is_default_input: bool
    fingerprint: str
    transport: DeviceTransport
    transport_source: str
    transport_evidence: str
    rejection_reason: str | None = None

    @property
    def is_usable(self) -> bool:
        return self.rejection_reason is None

    @property
    def is_internal_microphone(self) -> bool:
        return self.transport is DeviceTransport.INTERNAL

    @property
    def is_usb_conference_candidate(self) -> bool:
        """A verified USB capture device -- the Phase 2 production requirement."""
        return self.transport is DeviceTransport.USB and self.is_usable

    def recommended_profile(self, chunk_seconds: int | None = None) -> CaptureProfile:
        """The capture profile this device should be recorded with.

        Prefers the device's native sample rate; falls back to 48 kHz when the
        native rate is not one this application accepts. Channel count is the
        native count capped at two: Phase 2 handles mono and stereo, and a mono
        microphone is never inflated to fake stereo.
        """
        rate = self.default_sample_rate
        if rate not in SUPPORTED_SAMPLE_RATES:
            rate = 48_000
        channels = max(1, min(self.max_input_channels, MAX_CHANNELS))
        kwargs: dict[str, object] = {
            "sample_rate": rate,
            "channels": channels,
            "sample_format": SampleFormat.INT16,
        }
        if chunk_seconds is not None:
            kwargs["chunk_seconds"] = chunk_seconds
        return CaptureProfile(**kwargs)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, object]:
        """Serialisable description. Contains no filesystem path."""
        return {
            "index": self.index,
            "fingerprint": self.fingerprint,
            "name": self.name,
            "host_api": self.host_api,
            "max_input_channels": self.max_input_channels,
            "default_sample_rate": self.default_sample_rate,
            "default_low_input_latency": round(self.default_low_input_latency, 4),
            "default_high_input_latency": round(self.default_high_input_latency, 4),
            "is_default_input": self.is_default_input,
            "transport": self.transport.value,
            "transport_source": self.transport_source,
            "transport_evidence": self.transport_evidence,
            "transport_verified": self.transport_source == "windows-mmdevices-registry"
            and self.transport is not DeviceTransport.UNKNOWN,
            "usable": self.is_usable,
            "rejection_reason": self.rejection_reason,
            "is_internal_microphone": self.is_internal_microphone,
            "is_usb_conference_candidate": self.is_usb_conference_candidate,
        }


# ---------------------------------------------------------------------------
# Discovery service
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Cache:
    devices: list[DeviceInfo] = field(default_factory=list)
    endpoints: list[WindowsAudioEndpoint] = field(default_factory=list)
    populated: bool = False


class DeviceDiscoveryService:
    """Enumerates, identifies and validates capture devices.

    Enumeration is read-only: it never opens a stream, so listing devices cannot
    engage the microphone or steal it from another application.
    """

    def __init__(self, backend: AudioBackend, *, endpoint_provider=None) -> None:
        """
        Args:
            endpoint_provider: Source of Windows capture-endpoint evidence.
                ``None`` resolves :func:`query_windows_capture_endpoints` at call
                time rather than binding it as a default argument -- a default
                captured at definition time cannot be substituted, which makes the
                seam useless to tests and to any non-Windows caller.
        """
        self._backend = backend
        self._endpoint_provider = endpoint_provider
        self._cache = _Cache()

    def _endpoints(self) -> list[WindowsAudioEndpoint]:
        provider = self._endpoint_provider
        if provider is None:
            return list(query_windows_capture_endpoints())
        return list(provider())

    # -- enumeration --------------------------------------------------------

    def refresh(self) -> list[DeviceInfo]:
        """Re-read the device list from the backend and the OS."""
        raw = self._backend.list_devices()
        try:
            endpoints = self._endpoints()
        except Exception as exc:  # noqa: BLE001 - unverifiable is not fatal
            _LOG.debug("Transport resolution unavailable: %s", exc)
            endpoints = []
        self._cache = _Cache(
            devices=[self._enrich(entry, endpoints) for entry in raw],
            endpoints=endpoints,
            populated=True,
        )
        return list(self._cache.devices)

    def all_devices(self, *, refresh: bool = False) -> list[DeviceInfo]:
        """Every enumerated device, including the ones that cannot be used."""
        if refresh or not self._cache.populated:
            return self.refresh()
        return list(self._cache.devices)

    def input_devices(self, *, refresh: bool = False) -> list[DeviceInfo]:
        """Usable capture devices, best host API first."""
        devices = [d for d in self.all_devices(refresh=refresh) if d.is_usable]
        return sorted(devices, key=self._rank)

    def rejected_devices(self, *, refresh: bool = False) -> list[DeviceInfo]:
        """Devices that were enumerated and deliberately excluded, with reasons."""
        return [d for d in self.all_devices(refresh=refresh) if not d.is_usable]

    def default_input_device(self, *, refresh: bool = False) -> DeviceInfo | None:
        devices = self.input_devices(refresh=refresh)
        for device in devices:
            if device.is_default_input:
                return device
        return devices[0] if devices else None

    # -- identity -----------------------------------------------------------

    def find_by_fingerprint(
        self, fingerprint: str, *, refresh: bool = False
    ) -> DeviceInfo | None:
        for device in self.all_devices(refresh=refresh):
            if device.fingerprint == fingerprint:
                return device
        return None

    def resolve_selection(
        self, selection: DeviceSelection, *, refresh: bool = True
    ) -> DeviceInfo:
        """Re-find a previously chosen device. Never substitutes another one.

        Raises:
            DeviceNotFoundError: If the device is absent, naming the device that
                was expected and listing what is present instead. The operator
                decides what to do; the application does not choose for them.
            UnsupportedProfileError: If the device is present but unusable.
        """
        device = self.find_by_fingerprint(selection.fingerprint, refresh=refresh)
        if device is None:
            available = ", ".join(
                f"{d.name} [{d.host_api}]" for d in self.input_devices(refresh=False)
            )
            raise DeviceNotFoundError(
                f"The selected microphone is not available: {selection.name!r} "
                f"[{selection.host_api}] (fingerprint {selection.fingerprint}). "
                "Recording will not start on a different device: capturing a "
                "meeting through the wrong microphone cannot be undone. "
                f"Available now: {available or 'none'}. Reconnect the device, or "
                "choose another one explicitly."
            )
        if not device.is_usable:
            raise UnsupportedProfileError(
                f"The selected microphone {device.name!r} cannot be used: "
                f"{device.rejection_reason}"
            )
        return device

    # -- validation ---------------------------------------------------------

    def validate_for_capture(self, device: DeviceInfo, profile: CaptureProfile) -> None:
        """Check a device/profile pair without opening a stream.

        Raises:
            UnsupportedProfileError: If the pair is invalid.
        """
        if not device.is_usable:
            raise UnsupportedProfileError(
                f"Device {device.name!r} is not usable for capture: "
                f"{device.rejection_reason}"
            )
        if profile.channels > device.max_input_channels:
            raise UnsupportedProfileError(
                f"Device {device.name!r} provides {device.max_input_channels} input "
                f"channel(s) but {profile.channels} were requested."
            )
        self._backend.check_input_settings(device.index, profile)

    # -- internals ----------------------------------------------------------

    def _enrich(
        self, raw: RawDeviceInfo, endpoints: Sequence[WindowsAudioEndpoint]
    ) -> DeviceInfo:
        transport, source, evidence = resolve_transport(raw.name, endpoints)
        return DeviceInfo(
            index=raw.index,
            name=raw.name,
            host_api=raw.host_api,
            max_input_channels=raw.max_input_channels,
            max_output_channels=raw.max_output_channels,
            default_sample_rate=int(round(raw.default_sample_rate)),
            default_low_input_latency=raw.default_low_input_latency,
            default_high_input_latency=raw.default_high_input_latency,
            is_default_input=raw.is_default_input,
            fingerprint=device_fingerprint(raw.host_api, raw.name, raw.max_input_channels),
            transport=transport,
            transport_source=source,
            transport_evidence=evidence,
            rejection_reason=_rejection_reason(raw, endpoints),
        )

    @staticmethod
    def _rank(device: DeviceInfo) -> tuple[int, int, str]:
        return (
            _HOST_API_RANK.get(device.host_api, _UNRANKED),
            0 if device.is_default_input else 1,
            _normalise(device.name),
        )

    def describe(self) -> dict[str, object]:
        """Backend and OS-verification summary for diagnostics."""
        return {
            "backend": self._backend.describe(),
            "windows_endpoints_available": len(self._cache.endpoints),
            "host_api_preference": [
                api for api, _ in sorted(_HOST_API_RANK.items(), key=lambda item: item[1])
            ],
        }


def _rejection_reason(
    raw: RawDeviceInfo, endpoints: Sequence[WindowsAudioEndpoint]
) -> str | None:
    """Why this device cannot be used for capture, or ``None`` if it can.

    When Windows registry data is available it is treated as authoritative: a
    device that is not a registered capture endpoint is not a microphone,
    whatever its channel count claims. PortAudio's WDM-KS host API exposes output
    nodes such as "PC Speaker" with a non-zero input-channel count, so the channel
    count alone is not enough to tell a microphone from a loudspeaker.
    """
    if raw.max_input_channels <= 0:
        if raw.is_output_only:
            return "output-only device: it has no input channels"
        return "device reports zero input channels"

    lowered = _normalise(raw.name)
    for marker in _LOOPBACK_MARKERS:
        if marker in lowered:
            return (
                f"loopback/output-capture device (matched {marker!r}): it records "
                "what the computer plays, not the room"
            )
    for marker in _VIRTUAL_DEVICE_MARKERS:
        if marker in lowered:
            return (
                f"virtual/aggregate endpoint (matched {marker!r}): it follows the "
                "Windows default device, so the microphone actually used could "
                "change mid-recording without being recorded anywhere"
            )

    if endpoints:
        matches = match_windows_endpoints(raw.name, endpoints)
        if not matches:
            return (
                "not registered as a Windows capture endpoint: PortAudio reports "
                f"{raw.max_input_channels} input channel(s), but Windows does not "
                "list this as a recording device (typically an output node exposed "
                "by the WDM-KS host API)"
            )
        if not any(endpoint.active for endpoint in matches):
            return "device is present but disabled or unplugged in Windows"
    return None


def format_device_table(devices: Iterable[DeviceInfo]) -> str:
    """Render devices as aligned text for the CLI."""
    rows = list(devices)
    if not rows:
        return "No capture devices found."
    width = max(len(d.name) for d in rows)
    lines = [
        f"{'IDX':>3}  {'NAME':<{width}}  {'HOST API':<20} {'CH':>2} {'RATE':>6}  "
        f"{'TRANSPORT':<10} FINGERPRINT",
        "-" * (width + 62),
    ]
    for device in rows:
        marker = "*" if device.is_default_input else " "
        transport = device.transport.value
        if device.transport_source != "windows-mmdevices-registry":
            transport += "?"
        lines.append(
            f"{device.index:>3}{marker} {device.name:<{width}}  "
            f"{device.host_api:<20} {device.max_input_channels:>2} "
            f"{device.default_sample_rate:>6}  {transport:<10} {device.fingerprint}"
        )
        if not device.is_usable:
            lines.append(f"     -> unusable: {device.rejection_reason}")
    lines.append("")
    lines.append("* = system default input.  ? = transport not verified by Windows.")
    return "\n".join(lines)

