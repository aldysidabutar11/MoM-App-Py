"""Pre-recording gate.

A meeting happens once. Every failure this catches beforehand is a failure that
would otherwise be discovered only when the audio turns out to be unusable, with
nothing left to re-record. So the checks are pessimistic and the messages say what
to do rather than what went wrong.

By default nothing here opens the microphone: the device list, the format, the
disk and the database can all be checked without engaging hardware. The optional
open test (:func:`microphone_open_test`) does engage it, briefly, and only when the
caller explicitly asks -- it is triggered by an operator pressing a button, never
by a status poll or a diagnostic.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from mom_igd.audio.backend import (
    BYTES_PER_GB,
    AudioBackend,
    CaptureProfile,
    DeviceTransport,
)
from mom_igd.audio.devices import DeviceInfo, DeviceSelection
from mom_igd.logging_setup import get_logger

__all__ = [
    "PreflightItem",
    "PreflightReport",
    "PreflightStatus",
    "microphone_open_test",
    "run_preflight",
]

_LOG = get_logger("audio.preflight")
_OPEN_TEST_SECONDS = 0.6


class PreflightStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class PreflightItem:
    key: str
    title: str
    status: PreflightStatus
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status.value,
            "detail": self.detail,
            "data": self.data,
        }


@dataclass(slots=True)
class PreflightReport:
    items: list[PreflightItem] = field(default_factory=list)
    device: dict[str, Any] | None = None
    profile: dict[str, Any] | None = None
    estimate: dict[str, Any] | None = None

    @property
    def failures(self) -> list[PreflightItem]:
        return [i for i in self.items if i.status is PreflightStatus.FAIL]

    @property
    def warnings(self) -> list[PreflightItem]:
        return [i for i in self.items if i.status is PreflightStatus.WARN]

    @property
    def can_start(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "can_start": self.can_start,
            "failure_count": len(self.failures),
            "warning_count": len(self.warnings),
            "items": [i.to_dict() for i in self.items],
            "device": self.device,
            "profile": self.profile,
            "estimate": self.estimate,
        }


def _item(key, title, status, detail, **data) -> PreflightItem:
    return PreflightItem(key=key, title=title, status=status, detail=detail, data=data)


def storage_estimate(profile: CaptureProfile, minutes: float, free_bytes: int) -> dict[str, Any]:
    """Bytes needed for a meeting of ``minutes``, and whether they fit.

    All arithmetic is in bytes; MB/GB appear only for display, so no unit
    conversion can change the outcome of the decision.
    """
    needed = profile.estimate_bytes(minutes * 60.0)
    return {
        "planned_minutes": minutes,
        "needed_bytes": needed,
        "needed_gb": round(needed / BYTES_PER_GB, 2),
        "free_bytes": free_bytes,
        "free_gb": round(free_bytes / BYTES_PER_GB, 2),
        "megabytes_per_hour": round(profile.megabytes_per_hour, 1),
        "fits": needed < free_bytes,
        "headroom_bytes": free_bytes - needed,
        "max_minutes_available": (
            int(free_bytes / profile.bytes_per_second / 60) if profile.bytes_per_second else 0
        ),
    }


def run_preflight(
    *,
    device: DeviceInfo | None,
    selection: DeviceSelection | None,
    profile: CaptureProfile | None,
    recordings_dir: Path,
    database_ready: bool,
    active_recording: str | None,
    min_free_disk_gb: float,
    planned_minutes: float = 120.0,
    device_error: str | None = None,
    pending_recovery: int = 0,
    production_requires_usb: bool = True,
) -> PreflightReport:
    """Evaluate readiness to record. Opens no audio stream."""
    report = PreflightReport()
    add = report.items.append

    # -- device -------------------------------------------------------------
    if device is None:
        add(
            _item(
                "device",
                "Microphone",
                PreflightStatus.FAIL,
                device_error
                or (
                    "No microphone selected. Choose one explicitly: recording will "
                    "not guess, because capturing a meeting through the wrong "
                    "device cannot be undone."
                ),
                selection=selection.to_dict() if selection else None,
            )
        )
    else:
        report.device = device.to_dict()
        add(
            _item(
                "device",
                "Microphone",
                PreflightStatus.PASS,
                f"{device.name} [{device.host_api}], {device.max_input_channels} ch",
                fingerprint=device.fingerprint,
            )
        )
        if device.transport is DeviceTransport.INTERNAL:
            add(
                _item(
                    "device_transport",
                    "Microphone type",
                    PreflightStatus.WARN,
                    "This is the built-in microphone array. Its beamforming and noise "
                    "suppression suppress speakers who are not facing the laptop, so a "
                    "meeting with several people around a table will lose voices. "
                    "Acceptable for development; "
                    "a USB conference microphone at the centre of the table is required "
                    "for production.",
                    transport=device.transport.value,
                )
            )
        elif device.transport is DeviceTransport.USB:
            add(
                _item(
                    "device_transport",
                    "Microphone type",
                    PreflightStatus.PASS,
                    "USB capture device, bus confirmed by Windows.",
                    transport=device.transport.value,
                )
            )
        else:
            add(
                _item(
                    "device_transport",
                    "Microphone type",
                    PreflightStatus.FAIL if production_requires_usb else PreflightStatus.WARN,
                    f"Transport is {device.transport.value} ({device.transport_evidence}). "
                    "Confirm manually that this is the intended conference microphone; "
                    "the name alone is not evidence.",
                    transport=device.transport.value,
                    evidence=device.transport_evidence,
                )
                if device.transport is DeviceTransport.BLUETOOTH
                else _item(
                    "device_transport",
                    "Microphone type",
                    PreflightStatus.WARN,
                    f"Transport could not be verified ({device.transport_evidence}). "
                    "Confirm manually which microphone this is.",
                    transport=device.transport.value,
                    evidence=device.transport_evidence,
                )
            )

    # -- format -------------------------------------------------------------
    if profile is None:
        add(
            _item(
                "format",
                "Capture format",
                PreflightStatus.FAIL,
                "No capture format could be derived from the selected device.",
            )
        )
    else:
        report.profile = profile.describe()
        add(
            _item(
                "format",
                "Capture format",
                PreflightStatus.PASS,
                f"{profile.sample_rate} Hz / {profile.channels} ch / "
                f"{profile.sample_format.value}, {profile.chunk_seconds} s chunks, "
                f"{profile.megabytes_per_hour:.0f} MB/h",
            )
        )

    # -- data directory -----------------------------------------------------
    probe = recordings_dir
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    writable = False
    try:
        recordings_dir.mkdir(parents=True, exist_ok=True)
        marker = recordings_dir / ".preflight_probe"
        marker.write_bytes(b"")
        marker.unlink()
        writable = True
    except OSError as exc:
        add(
            _item(
                "data_directory",
                "Recording directory",
                PreflightStatus.FAIL,
                f"Cannot write into the recording directory: {exc}",
            )
        )
    if writable:
        add(
            _item(
                "data_directory",
                "Recording directory",
                PreflightStatus.PASS,
                "Recording directory is writable.",
            )
        )

    # -- disk ---------------------------------------------------------------
    free_bytes = 0
    try:
        free_bytes = shutil.disk_usage(probe if probe.exists() else recordings_dir).free
    except OSError as exc:
        add(
            _item(
                "disk_space",
                "Free disk space",
                PreflightStatus.FAIL,
                f"Cannot determine free disk space: {exc}",
            )
        )
    else:
        threshold = int(min_free_disk_gb * BYTES_PER_GB)
        if free_bytes < threshold:
            add(
                _item(
                    "disk_space",
                    "Free disk space",
                    PreflightStatus.FAIL,
                    f"{free_bytes / BYTES_PER_GB:.1f} GB free, below the required "
                    f"{min_free_disk_gb} GB. Free space before recording: a recording "
                    "that runs out of disk mid-meeting loses the rest of the meeting.",
                    free_bytes=free_bytes,
                )
            )
        else:
            add(
                _item(
                    "disk_space",
                    "Free disk space",
                    PreflightStatus.PASS,
                    f"{free_bytes / BYTES_PER_GB:.1f} GB free.",
                    free_bytes=free_bytes,
                )
            )

    if profile is not None:
        report.estimate = storage_estimate(profile, planned_minutes, free_bytes)
        if not report.estimate["fits"]:
            add(
                _item(
                    "storage_estimate",
                    "Storage estimate",
                    PreflightStatus.FAIL,
                    f"A {planned_minutes:.0f}-minute meeting needs about "
                    f"{report.estimate['needed_gb']} GB but only "
                    f"{report.estimate['free_gb']} GB is free. At this format the disk "
                    f"holds about {report.estimate['max_minutes_available']} minutes.",
                    **report.estimate,
                )
            )
        else:
            add(
                _item(
                    "storage_estimate",
                    "Storage estimate",
                    PreflightStatus.PASS,
                    f"About {report.estimate['needed_gb']} GB for "
                    f"{planned_minutes:.0f} minutes; room for roughly "
                    f"{report.estimate['max_minutes_available']} minutes.",
                    **report.estimate,
                )
            )

    # -- database and exclusivity ------------------------------------------
    add(
        _item(
            "database",
            "Database",
            PreflightStatus.PASS if database_ready else PreflightStatus.FAIL,
            "Schema is at head."
            if database_ready
            else "Database is not initialised or not at the expected schema version. "
            "Run `python -m mom_igd db init`.",
        )
    )
    add(
        _item(
            "single_recording",
            "No other recording active",
            PreflightStatus.PASS if active_recording is None else PreflightStatus.FAIL,
            "No other recording is in progress."
            if active_recording is None
            else f"Recording {active_recording} is still active. Only one recording "
            "may run per data root; stop it before starting another.",
        )
    )
    if pending_recovery:
        add(
            _item(
                "pending_recovery",
                "Interrupted recordings",
                PreflightStatus.WARN,
                f"{pending_recovery} interrupted recording(s) have not been recovered. "
                "Run recovery first so salvaged audio is not confused with this "
                "meeting.",
                pending=pending_recovery,
            )
        )
    return report


def microphone_open_test(
    backend: AudioBackend, device: DeviceInfo, profile: CaptureProfile
) -> dict[str, Any]:
    """Briefly open the microphone to prove it can actually deliver audio.

    **This engages the hardware**, so it runs only on an explicit request. It opens
    the stream, collects for well under a second, and closes it. Enumeration and
    format validation cannot detect a device that is present but silently refuses
    to start -- a muted or permission-blocked microphone, for instance -- which is
    exactly the failure that would otherwise surface only mid-meeting.
    """
    import time

    frames = 0
    callbacks = 0
    xruns = 0

    def _sink(_pcm: bytes, count: int, status) -> None:
        nonlocal frames, callbacks, xruns
        frames += count
        callbacks += 1
        if not status.is_clean:
            xruns += 1

    stream = backend.open_input_stream(device.index, profile, _sink)
    started = time.monotonic()
    try:
        stream.start()
        while time.monotonic() - started < _OPEN_TEST_SECONDS and frames == 0:
            time.sleep(0.01)
        deadline = started + _OPEN_TEST_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        try:
            stream.stop()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("Open test could not stop the stream: %s", exc)
        stream.close()

    elapsed = time.monotonic() - started
    ok = frames > 0
    return {
        "ok": ok,
        "frames": frames,
        "callbacks": callbacks,
        "xrun_callbacks": xruns,
        "seconds": round(elapsed, 3),
        "detail": (
            f"Received {frames} frame(s) in {elapsed:.2f} s."
            if ok
            else "The stream opened but delivered no audio. Check that the "
            "microphone is not muted and that Windows has granted microphone "
            "access to this application."
        ),
    }
