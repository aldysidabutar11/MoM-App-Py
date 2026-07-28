"""Phase 2 audio diagnostics.

Kept out of :mod:`mom_igd.diagnostics.doctor` so that module stays readable, and
so the only place that can touch PortAudio is one file.

**No check here opens a stream.** Loading the PortAudio library and enumerating
devices are read-only operations; engaging the microphone happens only when the
operator asks for a calibration or a recording.

Development readiness and production readiness are deliberately different. A
laptop with only its built-in array is fine for development and is *not* fine for
recording a nine-person meeting, so the built-in array is a ``WARN`` in the
default run and a ``FAIL`` under ``--production``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from mom_igd.diagnostics.model import CheckResult, Status

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mom_igd.config import AppConfig
    from mom_igd.paths import RuntimePaths

__all__ = ["CALIBRATION_MAX_AGE_DAYS", "audio_checks"]

CALIBRATION_MAX_AGE_DAYS: Final[int] = 30
"""How long a calibration stays evidence.

A room's acoustics, a microphone's position and Windows' own level settings all
drift. An indefinitely valid calibration would let a measurement taken once, months
ago, vouch for a meeting recorded today -- which is exactly the false assurance the
production gate exists to prevent.
"""


def audio_checks(
    config: AppConfig, paths: RuntimePaths, *, production: bool = False
) -> list[CheckResult]:
    """Run every Phase 2 audio check. Opens no audio stream."""
    results: list[CheckResult] = []
    backend_result, backend_ok = _check_backend()
    results.append(backend_result)

    devices: list[Any] = []
    if backend_ok:
        device_result, devices = _check_input_devices(production=production)
        results.append(device_result)
        results.append(_check_capture_profile(config, devices))
        results.append(_check_usb_conference_microphone(devices, production=production))
    results.append(_check_stale_recordings(paths, production=production))
    if production:
        results.append(_check_calibration_evidence(config, paths, devices))
    return results


# ---------------------------------------------------------------------------


def _check_backend() -> tuple[CheckResult, bool]:
    from mom_igd.audio.sounddevice_backend import sounddevice_available

    available, detail = sounddevice_available()
    if not available:
        return (
            CheckResult(
                key="audio_backend",
                title="Audio backend (sounddevice / PortAudio)",
                status=Status.FAIL,
                detail=(
                    f"{detail}. Phase 2 records audio through PortAudio; install the "
                    "runtime dependencies with "
                    r".venv\Scripts\python.exe -m pip install -r requirements.txt"
                ),
                required_in_phase="2",
                data={"available": False},
            ),
            False,
        )
    return (
        CheckResult(
            key="audio_backend",
            title="Audio backend (sounddevice / PortAudio)",
            status=Status.PASS,
            detail=detail,
            required_in_phase="2",
            data={"available": True, "detail": detail},
        ),
        True,
    )


def _discover():
    from mom_igd.audio.devices import DeviceDiscoveryService
    from mom_igd.audio.sounddevice_backend import SoundDeviceBackend

    return DeviceDiscoveryService(SoundDeviceBackend())


def _check_input_devices(*, production: bool) -> tuple[CheckResult, list[Any]]:
    try:
        service = _discover()
        usable = service.input_devices(refresh=True)
        rejected = service.rejected_devices()
    except Exception as exc:  # noqa: BLE001 - enumeration failure is a real fault
        return (
            CheckResult(
                key="audio_input_devices",
                title="Audio input devices",
                status=Status.FAIL,
                detail=f"Device enumeration failed: {type(exc).__name__}: {exc}",
                required_in_phase="2",
            ),
            [],
        )

    data = {
        "usable_count": len(usable),
        "rejected_count": len(rejected),
        "devices": [d.to_dict() for d in usable],
        "rejected": [
            {"name": d.name, "host_api": d.host_api, "reason": d.rejection_reason}
            for d in rejected
        ],
    }
    if not usable:
        return (
            CheckResult(
                key="audio_input_devices",
                title="Audio input devices",
                status=Status.FAIL,
                detail=(
                    f"No usable capture device. {len(rejected)} device(s) were "
                    "enumerated and excluded (output-only, loopback, virtual or "
                    "disabled). Connect a microphone and enable it in Windows "
                    "Sound settings."
                ),
                required_in_phase="2",
                data=data,
            ),
            usable,
        )

    names = ", ".join(f"{d.name} [{d.transport.value}]" for d in usable[:3])
    return (
        CheckResult(
            key="audio_input_devices",
            title="Audio input devices",
            status=Status.PASS,
            detail=f"{len(usable)} usable device(s): {names}. No stream was opened.",
            required_in_phase="2",
            data=data,
        ),
        usable,
    )


def _check_capture_profile(config: AppConfig, devices: list[Any]) -> CheckResult:
    if not devices:
        return CheckResult(
            key="audio_capture_profile",
            title="Capture profile",
            status=Status.WARN,
            detail="No device to derive a capture profile from.",
            required_in_phase="2",
        )
    chunk_seconds = config.audio.chunk_seconds
    entries: list[dict[str, Any]] = []
    for device in devices[:5]:
        try:
            profile = device.recommended_profile(chunk_seconds)
        except Exception as exc:  # noqa: BLE001
            entries.append({"device": device.name, "error": str(exc)})
            continue
        entries.append(
            {
                "device": device.name,
                "sample_rate": profile.sample_rate,
                "channels": profile.channels,
                "sample_format": profile.sample_format.value,
                "megabytes_per_hour": round(profile.megabytes_per_hour, 1),
            }
        )
    broken = [e for e in entries if "error" in e]
    best = entries[0]
    if broken:
        return CheckResult(
            key="audio_capture_profile",
            title="Capture profile",
            status=Status.WARN,
            detail=f"{len(broken)} device(s) cannot provide a supported profile.",
            required_in_phase="2",
            data={"profiles": entries},
        )
    return CheckResult(
        key="audio_capture_profile",
        title="Capture profile",
        status=Status.PASS,
        detail=(
            f"{best['device']}: {best['sample_rate']} Hz / {best['channels']} ch / "
            f"{best['sample_format']}, {best['megabytes_per_hour']} MB/h, "
            f"{chunk_seconds} s chunks"
        ),
        required_in_phase="2",
        data={"profiles": entries},
    )


def _check_usb_conference_microphone(devices: list[Any], *, production: bool) -> CheckResult:
    verified_usb = [d for d in devices if d.is_usb_conference_candidate]
    internal = [d for d in devices if d.is_internal_microphone]
    unverified = [
        d for d in devices if d.transport_source != "windows-mmdevices-registry"
    ]
    data = {
        "verified_usb": [d.name for d in verified_usb],
        "internal": [d.name for d in internal],
        "unverified_transport": [d.name for d in unverified],
        "production_gate": production,
    }
    if verified_usb:
        return CheckResult(
            key="usb_conference_microphone",
            title="USB conference microphone",
            status=Status.PASS,
            detail=(
                f"Verified USB capture device present: {verified_usb[0].name} "
                "(bus confirmed by Windows, not inferred from the name)."
            ),
            required_in_phase="2",
            data=data,
        )
    detail = (
        "No USB capture device verified by Windows. A single omnidirectional USB "
        "conference microphone at the centre of the table is required before Phase 2 "
        "production acceptance: the built-in array applies beamforming and noise "
        "suppression that suppress non-dominant speakers, which is unusable for a "
        "nine-person meeting."
    )
    if internal:
        detail += f" Present instead: {internal[0].name} (INTERNAL, development only)."
    if unverified:
        detail += (
            f" {len(unverified)} device(s) have an unverified transport; confirm the "
            "bus manually before relying on them."
        )
    return CheckResult(
        key="usb_conference_microphone",
        title="USB conference microphone",
        status=Status.FAIL if production else Status.WARN,
        detail=detail,
        required_in_phase="2" if production else None,
        data=data,
    )


def _check_stale_recordings(paths: RuntimePaths, *, production: bool) -> CheckResult:
    from mom_igd.audio.recovery import scan_recoverable

    try:
        pending = scan_recoverable(paths.recordings_dir)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            key="audio_stale_recordings",
            title="Interrupted recordings awaiting recovery",
            status=Status.WARN,
            detail=f"Could not scan for interrupted recordings: {exc}",
            required_in_phase="2",
        )
    data = {"pending_count": len(pending), "pending": [p.name for p in pending]}
    if not pending:
        return CheckResult(
            key="audio_stale_recordings",
            title="Interrupted recordings awaiting recovery",
            status=Status.PASS,
            detail="No interrupted recording is waiting for recovery.",
            required_in_phase="2",
            data=data,
        )
    return CheckResult(
        key="audio_stale_recordings",
        title="Interrupted recordings awaiting recovery",
        status=Status.FAIL if production else Status.WARN,
        detail=(
            f"{len(pending)} interrupted recording(s) have not been recovered. Run "
            "`python -m mom_igd audio recover` before starting a new meeting so the "
            "salvaged audio is not confused with the new recording."
        ),
        required_in_phase="2",
        data=data,
    )


def _calibration_age_days(recorded_utc: str) -> float | None:
    """Age of a calibration in days, or ``None`` if the timestamp is unusable."""
    from datetime import datetime, timezone

    text = (recorded_utc or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - stamp
    return delta.total_seconds() / 86_400.0


def _check_calibration_evidence(
    config: AppConfig, paths: RuntimePaths, devices: list[Any]
) -> CheckResult:
    """Production only: a recent calibration **of the device now in use**.

    A stored ``GOOD`` verdict on its own is not evidence. It has to be the same
    microphone, on a verified bus, measured recently -- otherwise a calibration of
    the laptop array from weeks ago would vouch for a USB microphone plugged in
    this morning, and the gate would certify something nobody measured.
    """
    db_path = paths.database_path(config.database.filename)
    if not db_path.exists():
        return CheckResult(
            key="audio_calibration_evidence",
            title="Microphone calibration evidence",
            status=Status.FAIL,
            detail=(
                "No database, so no calibration has been recorded. Run "
                "`python -m mom_igd db init` then `python -m mom_igd audio calibrate`."
            ),
            required_in_phase="2",
        )
    try:
        from mom_igd.db.connection import connect

        conn = connect(db_path, busy_timeout_ms=config.database.busy_timeout_ms)
        try:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = 'last_calibration'"
            ).fetchone()
            selected_row = conn.execute(
                "SELECT value FROM app_settings WHERE key = 'selected_audio_device'"
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            key="audio_calibration_evidence",
            title="Microphone calibration evidence",
            status=Status.FAIL,
            detail=f"Could not read calibration evidence: {exc}",
            required_in_phase="2",
        )
    if row is None:
        return CheckResult(
            key="audio_calibration_evidence",
            title="Microphone calibration evidence",
            status=Status.FAIL,
            detail=(
                "No calibration recorded. Run `python -m mom_igd audio calibrate` "
                "with the production microphone in its meeting position."
            ),
            required_in_phase="2",
        )
    import json

    try:
        payload = json.loads(str(row["value"]))
    except (json.JSONDecodeError, TypeError):
        payload = {}
    try:
        selected = json.loads(str(selected_row["value"])) if selected_row else {}
    except (json.JSONDecodeError, TypeError):
        selected = {}

    verdict = str(payload.get("verdict", "UNKNOWN"))
    calibrated_fp = str(payload.get("device_fingerprint") or "")
    calibrated_name = str(payload.get("device", "unknown"))
    recorded_utc = str(payload.get("utc", ""))
    age_days = _calibration_age_days(recorded_utc)
    selected_fp = str(selected.get("fingerprint") or "")

    present = {str(getattr(d, "fingerprint", "")): d for d in devices}
    device_now = present.get(calibrated_fp)

    reasons: list[str] = []
    if verdict != "GOOD":
        reasons.append(f"the verdict was {verdict}, not GOOD -- re-calibrate")
    if not calibrated_fp:
        reasons.append(
            "the record predates device-bound calibration, so it cannot be tied to a "
            "microphone -- re-calibrate"
        )
    elif selected_fp and calibrated_fp != selected_fp:
        reasons.append(
            f"it measured {calibrated_name!r}, but the selected device is a different "
            "one -- calibrate the device you will actually record with"
        )
    elif not selected_fp:
        reasons.append("no capture device is selected, so there is nothing to vouch for")
    if calibrated_fp and device_now is None:
        reasons.append(
            f"the calibrated device {calibrated_name!r} is not present on this machine"
        )
    elif device_now is not None:
        transport = str(getattr(getattr(device_now, "transport", None), "value", ""))
        if transport != "USB":
            reasons.append(
                f"it measured a {transport or 'UNKNOWN'} device; production requires a "
                "verified USB conference microphone"
            )
    if age_days is None:
        reasons.append(f"its timestamp {recorded_utc!r} is unreadable")
    elif age_days > CALIBRATION_MAX_AGE_DAYS:
        reasons.append(
            f"it is {age_days:.0f} days old, older than the "
            f"{CALIBRATION_MAX_AGE_DAYS}-day limit"
        )

    data = {
        **payload,
        "selected_device_fingerprint": selected_fp,
        "age_days": None if age_days is None else round(age_days, 2),
        "max_age_days": CALIBRATION_MAX_AGE_DAYS,
        "rejections": reasons,
    }
    if reasons:
        return CheckResult(
            key="audio_calibration_evidence",
            title="Microphone calibration evidence",
            status=Status.FAIL,
            detail=(
                f"Calibration evidence is not usable: {'; '.join(reasons)}. "
                "Run `python -m mom_igd audio calibrate` with the production "
                "microphone in its meeting position."
            ),
            required_in_phase="2",
            data=data,
        )
    return CheckResult(
        key="audio_calibration_evidence",
        title="Microphone calibration evidence",
        status=Status.PASS,
        detail=(
            f"Verdict GOOD on {calibrated_name} (USB), recorded {recorded_utc} "
            f"({age_days:.1f} days ago)."
        ),
        required_in_phase="2",
        data=data,
    )
