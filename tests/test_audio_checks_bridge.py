"""Production doctor gate and the shell's token-bearing bridge."""

from __future__ import annotations

import json

import pytest

from mom_igd.audio.backend import DeviceTransport
from mom_igd.audio.devices import DeviceInfo
from mom_igd.audio.manifest import utc_now_iso
from mom_igd.config import AppConfig
from mom_igd.db import initialize_database
from mom_igd.diagnostics.audio_checks import audio_checks
from mom_igd.diagnostics.model import Status
from mom_igd.security import SessionToken


def _by_key(results) -> dict[str, object]:
    return {r.key: r for r in results}


def _store(config: AppConfig, paths, key: str, value: str) -> None:
    from mom_igd.db.connection import connect, maybe_transaction

    conn = connect(paths.database_path(config.database.filename))
    try:
        with maybe_transaction(conn):
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
    finally:
        conn.close()


# ================================================= calibration evidence gate


def test_production_gate_fails_without_a_database(config: AppConfig, paths) -> None:
    result = _by_key(audio_checks(config, paths, production=True))[
        "audio_calibration_evidence"
    ]
    assert result.status is Status.FAIL
    assert "db init" in result.detail


def test_production_gate_fails_when_no_calibration_was_recorded(
    config: AppConfig, paths
) -> None:
    initialize_database(paths.database_path(config.database.filename))
    result = _by_key(audio_checks(config, paths, production=True))[
        "audio_calibration_evidence"
    ]
    assert result.status is Status.FAIL
    assert "audio calibrate" in result.detail


# A stored GOOD verdict is not enough on its own: it must belong to the device that
# is actually selected, on a verified USB bus, and be recent. These helpers fake a
# present USB device so the gate can be exercised without hardware.

USB_FINGERPRINT = "a" * 32
OTHER_FINGERPRINT = "b" * 32


def _device(fingerprint: str, name: str, transport: str) -> DeviceInfo:
    """A real DeviceInfo, so the other checks see the shape they expect."""
    return DeviceInfo(
        index=0,
        name=name,
        host_api="Windows WASAPI",
        max_input_channels=2,
        max_output_channels=0,
        default_sample_rate=48_000,
        default_low_input_latency=0.01,
        default_high_input_latency=0.05,
        is_default_input=True,
        fingerprint=fingerprint,
        transport=DeviceTransport(transport),
        transport_source="windows-mmdevices-registry",
        transport_evidence=f"test fixture: {transport}",
    )


def _patch_devices(monkeypatch: pytest.MonkeyPatch, devices: list[DeviceInfo]) -> None:
    from mom_igd.audio.devices import DeviceDiscoveryService

    monkeypatch.setattr(DeviceDiscoveryService, "input_devices", lambda self, **k: devices)
    monkeypatch.setattr(DeviceDiscoveryService, "rejected_devices", lambda self, **k: [])


def _good_evidence(**overrides: object) -> str:
    payload = {
        "verdict": "GOOD",
        "device": "Jabra Speak 750",
        "device_fingerprint": USB_FINGERPRINT,
        "transport": "USB",
        "utc": utc_now_iso(),
        "rms_dbfs": -18.0,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _arm(config: AppConfig, paths, evidence: str, *, selected: str = USB_FINGERPRINT) -> None:
    initialize_database(paths.database_path(config.database.filename))
    _store(config, paths, "last_calibration", evidence)
    _store(
        config,
        paths,
        "selected_audio_device",
        json.dumps({"fingerprint": selected, "name": "Jabra Speak 750"}),
    )


def _gate(config: AppConfig, paths):
    return _by_key(audio_checks(config, paths, production=True))[
        "audio_calibration_evidence"
    ]


def test_production_gate_accepts_a_recent_usb_calibration_of_the_selected_device(
    config: AppConfig, paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_devices(monkeypatch, [_device(USB_FINGERPRINT, "Jabra Speak 750", "USB")])
    _arm(config, paths, _good_evidence())
    result = _gate(config, paths)
    assert result.status is Status.PASS, result.detail
    assert "GOOD" in result.detail
    assert "Jabra Speak 750" in result.detail


@pytest.mark.parametrize("verdict", ["CLIPPING", "TOO_QUIET", "NO_SIGNAL", "TOO_LOUD"])
def test_production_gate_rejects_an_unusable_verdict(
    config: AppConfig, paths, monkeypatch: pytest.MonkeyPatch, verdict: str
) -> None:
    _patch_devices(monkeypatch, [_device(USB_FINGERPRINT, "Jabra Speak 750", "USB")])
    _arm(config, paths, _good_evidence(verdict=verdict))
    result = _gate(config, paths)
    assert result.status is Status.FAIL
    assert "re-calibrate" in result.detail


def test_production_gate_rejects_a_calibration_of_a_different_microphone(
    config: AppConfig, paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect this guards: a GOOD reading from the laptop array must not
    vouch for the USB microphone the operator has since selected."""
    _patch_devices(
        monkeypatch,
        [
            _device(USB_FINGERPRINT, "Jabra Speak 750", "USB"),
            _device(OTHER_FINGERPRINT, "Microphone Array (Intel)", "INTERNAL"),
        ],
    )
    _arm(
        config,
        paths,
        _good_evidence(
            device_fingerprint=OTHER_FINGERPRINT, device="Microphone Array (Intel)"
        ),
        selected=USB_FINGERPRINT,
    )
    result = _gate(config, paths)
    assert result.status is Status.FAIL
    assert "different" in result.detail
    assert "calibrate the device you will actually record with" in result.detail


def test_production_gate_rejects_a_calibration_of_an_internal_microphone(
    config: AppConfig, paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_devices(
        monkeypatch, [_device(OTHER_FINGERPRINT, "Microphone Array (Intel)", "INTERNAL")]
    )
    _arm(
        config,
        paths,
        _good_evidence(
            device_fingerprint=OTHER_FINGERPRINT, device="Microphone Array (Intel)"
        ),
        selected=OTHER_FINGERPRINT,
    )
    result = _gate(config, paths)
    assert result.status is Status.FAIL
    assert "verified USB conference microphone" in result.detail


def test_production_gate_rejects_a_calibration_of_an_absent_device(
    config: AppConfig, paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The microphone was unplugged after calibration."""
    _patch_devices(monkeypatch, [])
    _arm(config, paths, _good_evidence())
    result = _gate(config, paths)
    assert result.status is Status.FAIL
    assert "not present on this machine" in result.detail


def test_production_gate_rejects_a_stale_calibration(
    config: AppConfig, paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    from mom_igd.diagnostics.audio_checks import CALIBRATION_MAX_AGE_DAYS

    _patch_devices(monkeypatch, [_device(USB_FINGERPRINT, "Jabra Speak 750", "USB")])
    old = datetime.now(timezone.utc) - timedelta(days=CALIBRATION_MAX_AGE_DAYS + 5)
    _arm(config, paths, _good_evidence(utc=old.isoformat().replace("+00:00", "Z")))
    result = _gate(config, paths)
    assert result.status is Status.FAIL
    assert "older than" in result.detail


def test_production_gate_rejects_evidence_with_no_device_binding(
    config: AppConfig, paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evidence written before the fingerprint was recorded cannot be trusted."""
    _patch_devices(monkeypatch, [_device(USB_FINGERPRINT, "Jabra Speak 750", "USB")])
    _arm(config, paths, _good_evidence(device_fingerprint=None))
    result = _gate(config, paths)
    assert result.status is Status.FAIL
    assert "cannot be tied to a microphone" in result.detail


def test_production_gate_rejects_a_calibration_with_nothing_selected(
    config: AppConfig, paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_devices(monkeypatch, [_device(USB_FINGERPRINT, "Jabra Speak 750", "USB")])
    initialize_database(paths.database_path(config.database.filename))
    _store(config, paths, "last_calibration", _good_evidence())
    result = _gate(config, paths)
    assert result.status is Status.FAIL
    assert "no capture device is selected" in result.detail


def test_production_gate_rejects_an_unreadable_timestamp(
    config: AppConfig, paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_devices(monkeypatch, [_device(USB_FINGERPRINT, "Jabra Speak 750", "USB")])
    _arm(config, paths, _good_evidence(utc="kemarin sore"))
    result = _gate(config, paths)
    assert result.status is Status.FAIL
    assert "unreadable" in result.detail


def test_production_gate_rejects_unreadable_calibration_evidence(
    config: AppConfig, paths
) -> None:
    initialize_database(paths.database_path(config.database.filename))
    _store(config, paths, "last_calibration", "{not json")
    result = _gate(config, paths)
    assert result.status is Status.FAIL
    assert "UNKNOWN" in result.detail


def test_calibration_is_not_checked_outside_the_production_gate(
    config: AppConfig, paths
) -> None:
    keys = _by_key(audio_checks(config, paths, production=False))
    assert "audio_calibration_evidence" not in keys


def test_backend_failure_short_circuits_the_device_checks(
    config: AppConfig, paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no backend there is nothing to enumerate, so no device check runs."""
    monkeypatch.setattr(
        "mom_igd.audio.sounddevice_backend.sounddevice_available",
        lambda: (False, "ImportError: No module named 'sounddevice'"),
    )
    keys = _by_key(audio_checks(config, paths, production=False))
    assert keys["audio_backend"].status is Status.FAIL
    assert "requirements.txt" in keys["audio_backend"].detail
    assert "audio_input_devices" not in keys
    # The stale-recording scan does not need a backend, so it still runs.
    assert "audio_stale_recordings" in keys


def test_enumeration_failure_is_a_failure_not_a_crash(
    config: AppConfig, paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mom_igd.audio.devices import DeviceDiscoveryService

    def _boom(self, *args, **kwargs):
        raise RuntimeError("PortAudio exploded")

    monkeypatch.setattr(DeviceDiscoveryService, "input_devices", _boom)
    result = _by_key(audio_checks(config, paths, production=False))["audio_input_devices"]
    assert result.status is Status.FAIL
    assert "PortAudio exploded" in result.detail


def test_no_usable_device_is_a_failure(
    config: AppConfig, paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mom_igd.audio.devices import DeviceDiscoveryService

    monkeypatch.setattr(DeviceDiscoveryService, "input_devices", lambda self, **k: [])
    monkeypatch.setattr(DeviceDiscoveryService, "rejected_devices", lambda self, **k: [])
    keys = _by_key(audio_checks(config, paths, production=False))
    assert keys["audio_input_devices"].status is Status.FAIL
    assert "No usable capture device" in keys["audio_input_devices"].detail
    assert keys["audio_capture_profile"].status is Status.WARN


def test_stale_recording_scan_failure_is_reported_as_a_warning(
    config: AppConfig, paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "mom_igd.audio.recovery.scan_recoverable",
        lambda _root: (_ for _ in ()).throw(OSError("disk offline")),
    )
    result = _by_key(audio_checks(config, paths, production=False))[
        "audio_stale_recordings"
    ]
    assert result.status is Status.WARN
    assert "disk offline" in result.detail


# ==================================================== shell bridge over HTTP


@pytest.fixture
def bridge(config: AppConfig, paths, token: SessionToken):
    """A ShellApi wired to a real loopback server."""
    from mom_igd.api.app import create_app
    from mom_igd.api.server import BackgroundServer
    from mom_igd.shell.launcher import ShellApi

    initialize_database(paths.database_path(config.database.filename))
    app = create_app(config, session_token=token, paths=paths)
    server = BackgroundServer(app, port=0, log_level="warning").start()
    try:
        yield ShellApi(server.base_url, token, config), server
    finally:
        server.stop()


pytestmark_slow = pytest.mark.slow


@pytest.mark.slow
def test_bridge_get_succeeds_and_returns_an_envelope(bridge) -> None:
    api, _server = bridge
    result = api.api_get("/health")
    assert result["ok"] is True
    assert result["status"] == 200
    assert result["data"]["status"] == "ok"


@pytest.mark.slow
def test_bridge_attaches_the_token_so_protected_calls_succeed(bridge) -> None:
    api, _server = bridge
    result = api.api_get("/internal/ready")
    assert result["status"] in (200, 503)
    assert result["data"]["phase"]


@pytest.mark.slow
def test_bridge_passes_scalar_query_values(bridge) -> None:
    api, _server = bridge
    result = api.api_get("/audio/preflight", {"planned_minutes": 45})
    assert result["ok"] is True
    assert result["data"]["estimate"] is None or result["data"]["estimate"][
        "planned_minutes"
    ] == 45


@pytest.mark.slow
def test_bridge_reports_an_http_error_as_a_structured_envelope(bridge) -> None:
    api, _server = bridge
    # Pausing with nothing recording is a 409 from the API.
    result = api.api_post("/audio/recordings/pause", {})
    assert result["ok"] is False
    assert result["status"] == 409
    assert "No recording is in progress" in str(result["error"])


@pytest.mark.slow
def test_bridge_post_reaches_the_service(bridge) -> None:
    api, _server = bridge
    result = api.api_post("/audio/recovery/run", {})
    assert result["ok"] is True
    assert result["data"]["scanned"] == 0


@pytest.mark.slow
def test_bridge_bootstrap_carries_no_secret(bridge) -> None:
    api, _server = bridge
    payload = api.bootstrap()
    assert payload["offline"] is True
    assert payload["proxy_available"] is True
    assert "token" not in json.dumps(payload).lower()


def test_bridge_refuses_an_unlisted_get_path(config: AppConfig) -> None:
    from mom_igd.shell.launcher import ShellApi

    api = ShellApi("http://127.0.0.1:1", SessionToken(), config)
    for path in ("/openapi.json", "/docs", "/audio/../etc", "/audio/recordings/x/verify"):
        result = api.api_get(path)
        assert result["ok"] is False
        assert "allowlist" in result["error"]


def test_bridge_accepts_a_well_formed_verify_path(config: AppConfig) -> None:
    from mom_igd.shell.launcher import ShellApi

    api = ShellApi("http://127.0.0.1:1", SessionToken(), config)
    # Nothing is listening, so this fails at the socket -- not at the allowlist.
    result = api.api_get("/audio/recordings/00000000-0000-4000-8000-000000000000/verify")
    assert result["ok"] is False
    assert "allowlist" not in str(result["error"])
    assert result["status"] == 0


def test_bridge_refuses_an_unlisted_post_path(config: AppConfig) -> None:
    from mom_igd.shell.launcher import ShellApi

    api = ShellApi("http://127.0.0.1:1", SessionToken(), config)
    for path in ("/health", "/audio/devices", "/audio/recordings/x/verify"):
        result = api.api_post(path)
        assert result["ok"] is False
        assert "POST allowlist" in result["error"]


def test_bridge_reports_an_unreachable_backend(config: AppConfig) -> None:
    from mom_igd.shell.launcher import ShellApi

    api = ShellApi("http://127.0.0.1:1", SessionToken(), config)
    result = api.api_get("/health")
    assert result["ok"] is False
    assert result["status"] == 0
    assert "Error" in result["error"] or "error" in result["error"].lower()


def test_manual_launch_command_is_documented() -> None:
    from mom_igd.shell.launcher import manual_launch_command

    command = manual_launch_command()
    assert ".venv" in command and "mom_igd shell" in command
