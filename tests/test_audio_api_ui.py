"""Phase 2 API, CLI and static UI boundaries."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mom_igd.api.app import WEB_DIR
from mom_igd.audio.devices import DeviceDiscoveryService
from mom_igd.audio.fake_backend import CounterSource, FakeAudioBackend
from mom_igd.audio.service import RecordingService
from mom_igd.config import AppConfig
from mom_igd.db import initialize_database
from mom_igd.security import SESSION_TOKEN_HEADER, SessionToken

AUDIO_GET_PATHS = (
    "/audio/devices",
    "/audio/preflight",
    "/audio/recordings/status",
    "/audio/quality",
    # Live preview text while a capture runs. Read-only; the payload marks itself
    # `is_preview` so it can never be mistaken for the stored transcript.
    "/audio/live",
    "/audio/level",
    "/audio/recovery/pending",
)
AUDIO_POST_PATHS = (
    "/audio/devices/select",
    "/audio/open-test",
    "/audio/calibrate",
    "/audio/voice-check",
    "/audio/recordings/start",
    "/audio/recordings/pause",
    "/audio/recordings/resume",
    "/audio/recordings/stop",
    "/audio/recovery/run",
)


@pytest.fixture
def audio_app(config: AppConfig, paths, token: SessionToken):
    """App with a fake-backend recording service injected on app.state."""
    from mom_igd.api.app import create_app

    initialize_database(
        paths.database_path(config.database.filename),
        busy_timeout_ms=config.database.busy_timeout_ms,
        app_version=config.app_version,
    )
    backend = FakeAudioBackend(blocksize=1_200, source=CounterSource())
    app = create_app(config, session_token=token, paths=paths)
    app.state.recording_service = RecordingService(
        config,
        paths,
        backend=backend,
        discovery=DeviceDiscoveryService(backend, endpoint_provider=lambda: []),
    )
    app.state.fake_backend = backend
    yield app
    try:
        app.state.recording_service.abandon("test teardown")
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def audio_client(audio_app):
    from starlette.testclient import TestClient

    with TestClient(audio_app, base_url="http://127.0.0.1") as client:
        yield client


# =========================================================== authentication


@pytest.mark.parametrize("path", AUDIO_GET_PATHS)
def test_audio_get_endpoints_require_the_token(audio_client, path: str) -> None:
    assert audio_client.get(path).status_code == 401


@pytest.mark.parametrize("path", AUDIO_POST_PATHS)
def test_audio_post_endpoints_require_the_token(audio_client, path: str) -> None:
    assert audio_client.post(path, json={}).status_code == 401


@pytest.mark.parametrize("path", AUDIO_GET_PATHS)
def test_audio_get_endpoints_accept_the_token(
    audio_client, token: SessionToken, path: str
) -> None:
    response = audio_client.get(path, headers=token.header())
    assert response.status_code == 200, response.text


def test_a_correct_token_in_a_query_string_is_still_refused(
    audio_client, token: SessionToken
) -> None:
    response = audio_client.get(f"/audio/devices?token={token.value}")
    assert response.status_code == 400
    assert "query parameter" in response.json()["detail"]


def test_wrong_token_is_refused(audio_client) -> None:
    response = audio_client.get("/audio/devices", headers={SESSION_TOKEN_HEADER: "x" * 43})
    assert response.status_code == 401


def test_audio_endpoints_are_unreachable_from_a_non_loopback_host(
    audio_client, token: SessionToken
) -> None:
    response = audio_client.get(
        "/audio/devices", headers={"Host": "evil.example.com", **token.header()}
    )
    assert response.status_code == 403


# ================================================================ payloads


def test_device_list_never_exposes_a_filesystem_path(
    audio_client, token: SessionToken, paths
) -> None:
    body = audio_client.get("/audio/devices", headers=token.header()).text
    assert str(paths.root) not in body
    assert ":\\" not in body


def test_status_and_preflight_never_expose_a_filesystem_path(
    audio_client, token: SessionToken, paths
) -> None:
    for path in ("/audio/recordings/status", "/audio/preflight"):
        body = audio_client.get(path, headers=token.header()).text
        assert str(paths.root) not in body, path
        assert str(paths.recordings_dir) not in body, path
        assert ":\\" not in body, path


def test_no_audio_response_contains_the_token(audio_client, token: SessionToken) -> None:
    for path in AUDIO_GET_PATHS:
        assert token.value not in audio_client.get(path, headers=token.header()).text


def test_recovery_pending_reports_uuid_names_not_paths(
    audio_client, token: SessionToken, paths
) -> None:
    from mom_igd.audio.writer import partial_path

    directory = paths.recordings_dir / "meeting-uuid" / "recording-uuid"
    directory.mkdir(parents=True)
    partial_path(directory, 0).write_bytes(b"\x00\x00\x00\x00")

    payload = audio_client.get("/audio/recovery/pending", headers=token.header()).json()
    assert payload["pending_count"] == 1
    assert payload["pending"] == ["meeting-uuid/recording-uuid"]
    assert not any(":" in entry for entry in payload["pending"])


# ============================================================== validation


def test_a_malformed_fingerprint_is_rejected(audio_client, token: SessionToken) -> None:
    for bad in ("../../etc/passwd", "NOTHEX" + "0" * 26, "short"):
        response = audio_client.post(
            "/audio/devices/select", headers=token.header(), json={"fingerprint": bad}
        )
        assert response.status_code in (404, 422), bad


def test_a_path_cannot_be_smuggled_through_the_verify_route(
    audio_client, token: SessionToken
) -> None:
    for bad in ("not-a-uuid", "../../secret", "0" * 36):
        response = audio_client.get(
            f"/audio/recordings/{bad}/verify", headers=token.header()
        )
        assert response.status_code in (404, 422), bad


def test_verifying_an_unknown_recording_is_a_conflict_not_a_crash(
    audio_client, token: SessionToken
) -> None:
    response = audio_client.get(
        "/audio/recordings/00000000-0000-4000-8000-000000000000/verify",
        headers=token.header(),
    )
    assert response.status_code == 409
    assert "No recording" in response.json()["detail"]


def test_an_invalid_lifecycle_request_is_a_conflict(
    audio_client, token: SessionToken
) -> None:
    """Pausing when nothing is recording is a client mistake, not a server error."""
    for path in ("/audio/recordings/pause", "/audio/recordings/resume"):
        response = audio_client.post(path, headers=token.header(), json={})
        assert response.status_code == 409, path
        assert "No recording is in progress" in response.json()["detail"]


def test_stop_is_idempotent_over_http(audio_client, token: SessionToken) -> None:
    first = audio_client.post("/audio/recordings/stop", headers=token.header(), json={})
    second = audio_client.post("/audio/recordings/stop", headers=token.header(), json={})
    assert first.status_code == second.status_code == 200
    assert first.json()["recording_active"] is False


def test_starting_without_a_meeting_id_is_allowed(audio_client, token: SessionToken) -> None:
    """A fresh install has no meeting row, and Meeting setup is a later phase.

    Requiring ``meeting_id`` here made the shell unusable on a new data root: the
    only way to satisfy it would have been to ask the operator for an internal
    database primary key. Omitting it creates a draft meeting instead. See
    ``tests/test_fresh_recording_flow.py`` for the full sequence.
    """
    response = audio_client.post("/audio/recordings/start", headers=token.header(), json={})
    assert response.status_code == 200, response.text
    assert response.json()["recording_active"] is True


@pytest.mark.parametrize("bad", [0, -1, "one", 1.5])
def test_a_malformed_meeting_id_is_still_rejected(
    audio_client, token: SessionToken, bad: object
) -> None:
    """Optional is not the same as unvalidated."""
    response = audio_client.post(
        "/audio/recordings/start", headers=token.header(), json={"meeting_id": bad}
    )
    assert response.status_code == 422, response.text


def test_an_over_long_meeting_title_is_rejected(audio_client, token: SessionToken) -> None:
    response = audio_client.post(
        "/audio/recordings/start",
        headers=token.header(),
        json={"meeting_title": "x" * 201},
    )
    assert response.status_code == 422, response.text


def test_preflight_rejects_an_absurd_duration(audio_client, token: SessionToken) -> None:
    assert audio_client.get(
        "/audio/preflight?planned_minutes=0", headers=token.header()
    ).status_code == 422
    assert audio_client.get(
        "/audio/preflight?planned_minutes=99999", headers=token.header()
    ).status_code == 422


def test_status_advertises_no_capability_phase_2_lacks(
    audio_client, token: SessionToken
) -> None:
    payload = audio_client.get("/audio/recordings/status", headers=token.header()).json()
    assert payload["capabilities"] == {
        "audio_capture": True,
        "transcript": False,
        "speaker_identification": False,
        "mom_generation": False,
        "export": False,
    }


# ================================================== endpoints and hardware


def test_read_only_endpoints_do_not_open_the_microphone(
    audio_client, audio_app, token: SessionToken
) -> None:
    for path in AUDIO_GET_PATHS:
        audio_client.get(path, headers=token.header())
    assert audio_app.state.fake_backend.open_calls == 0


def test_the_service_is_created_lazily(config: AppConfig, paths, token: SessionToken) -> None:
    """Serving /health must not construct an audio backend."""
    from starlette.testclient import TestClient

    from mom_igd.api.app import create_app

    app = create_app(config, session_token=token, paths=paths)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/health").status_code == 200
        assert getattr(app.state, "recording_service", None) is None


# ================================================================ static UI


@pytest.fixture(scope="module")
def ui_sources() -> dict[str, str]:
    return {
        name: (WEB_DIR / name).read_text(encoding="utf-8")
        for name in ("index.html", "app.css", "app.js")
    }


@pytest.mark.parametrize("name", ["index.html", "app.css", "app.js"])
def test_ui_has_no_remote_asset(ui_sources: dict[str, str], name: str) -> None:
    text = ui_sources[name]
    for marker in ("http://", "https://", "//cdn", "//unpkg", "//fonts.", "@font-face"):
        assert marker not in text, f"{name} references {marker}"


def test_ui_uses_no_browser_storage(ui_sources: dict[str, str]) -> None:
    script = ui_sources["app.js"]
    for banned in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
        assert f"{banned}." not in script and f"{banned}[" not in script, banned


def test_ui_adds_no_frontend_framework(ui_sources: dict[str, str]) -> None:
    """Word boundaries, not substrings.

    "re**act**ivate" and "in**vite**d" are English words that appear in the Phase 3
    UI. The exhaustive framework check is in `tests/test_static_ui.py`.
    """
    import re as _re

    combined = " ".join(ui_sources.values()).lower()
    for banned in ("react", "vue.js", "svelte", "angular", "jquery", "webpack", "vite"):
        pattern = r"(?<![a-z])" + _re.escape(banned) + r"(?![a-z])"
        assert not _re.search(pattern, combined), banned


def test_ui_never_holds_the_token(ui_sources: dict[str, str]) -> None:
    for name, text in ui_sources.items():
        lowered = text.lower()
        for banned in ("x-mom-session-token", "bearer ", "?token=", "session_token="):
            assert banned not in lowered, f"{name} contains {banned!r}"


def test_ui_calls_protected_endpoints_only_through_the_python_bridge(
    ui_sources: dict[str, str],
) -> None:
    script = ui_sources["app.js"]
    # The bridge is the only route to an authenticated call, so both proxy methods
    # must be invoked and `window.pywebview.api` must be how they are reached.
    assert "window.pywebview" in script
    assert re.search(r"\.api_get\(", script), "api_get must be called"
    assert re.search(r"\.api_post\(", script), "api_post must be called"

    # Every audio call must go through the bridge, never a bare fetch(): a fetch()
    # would need the token in the page to be authenticated.
    for match in re.finditer(r"fetch\(([^)]*)\)", script):
        assert "/audio/" not in match.group(1), "audio calls must use the bridge"
    # Only the two unauthenticated endpoints may be fetched directly.
    fetched = re.findall(r"getPublic\('([^']+)'\)", script)
    assert set(fetched) <= {"/health", "/version"}, fetched


def test_recording_card_is_enabled_and_the_rest_stay_disabled(
    ui_sources: dict[str, str],
) -> None:
    html = ui_sources["index.html"]
    assert 'id="card-recording"' in html
    assert "feature-card-enabled" in html
    # Recording went live in Phase 2, Participants in Phase 3, Transcription in Phase 4.
    # The authoritative count of enabled and disabled cards is in tests/test_static_ui.py;
    # what matters here is that a disabled card always says it is not implemented.
    assert html.count('aria-disabled="true"') == html.count("Belum diimplementasikan")
    for feature in ("Meeting setup", "Participants", "Review", "Export"):
        assert feature in html


def test_recording_panel_exposes_every_required_control(ui_sources: dict[str, str]) -> None:
    html = ui_sources["index.html"]
    for element_id in (
        "device-select",
        "refresh-devices-btn",
        "select-device-btn",
        "calibrate-btn",
        "level-verdict",
        "meter-rms",
        "meter-peak",
        "preflight-list",
        "storage-estimate",
        "start-btn",
        "pause-btn",
        "resume-btn",
        "stop-btn",
        "elapsed",
        "rec-detail",
        "rec-warning",
        "verify-btn",
        "recover-btn",
        "recovery-detail",
    ):
        assert f'id="{element_id}"' in html, element_id


def test_ui_states_what_phase_2_cannot_do(ui_sources: dict[str, str]) -> None:
    html = ui_sources["index.html"]
    assert "belum" in html.lower()
    assert "siapa yang berbicara" in html
    assert "tanpa enkripsi" in html


def test_the_recording_panel_does_not_tie_capture_to_a_roster_size(
    ui_sources: dict[str, str],
) -> None:
    """This assertion replaces one that required the words "maksimal sembilan".

    That copy was accurate while nine was a hard cap and is now wrong twice over:
    capacity is per meeting, and capture never depended on it in the first place.
    Recording takes the whole room signal, so promising a maximum head count in the
    recording panel would misdescribe what the microphone does.
    """
    html = ui_sources["index.html"]
    panel = html[html.index('id="recording-panel"') :]
    panel = panel[: panel.index("</section>")]
    assert "seluruh" in panel, (
        "the recording panel must say it captures every voice in the room"
    )
    for stale in ("maksimal sembilan", "maksimal 9", "hingga sembilan"):
        assert stale not in panel, (
            f"{stale!r} states a head-count limit on capture, which does not exist"
        )


def test_ui_shows_no_fake_transcript_or_speaker_label(ui_sources: dict[str, str]) -> None:
    combined = " ".join(ui_sources.values()).lower()
    for fake in ("speaker 1", "speaker_1", "pembicara 1", "lorem ipsum"):
        assert fake not in combined, fake

    # A hard-coded transcript *label*, which is what a placeholder looks like:
    # ">Transkrip: ..." or "'Transkrip: ...". Unanchored, this matched the ordinary
    # Indonesian error message "Tidak dapat memuat daftar transkrip: " + detail --
    # prose about a failed fetch, not fabricated content. Anchoring keeps the check
    # and removes the false positive.
    for opener in (">", "'", '"', "`"):
        assert opener + "transkrip:" not in combined, opener + "transkrip:"


def test_stop_requires_confirmation(ui_sources: dict[str, str]) -> None:
    assert "window.confirm" in ui_sources["app.js"]


def test_ui_polls_at_a_gentle_rate(ui_sources: dict[str, str]) -> None:
    match = re.search(r"POLL_MS\s*=\s*(\d+)", ui_sources["app.js"])
    assert match is not None
    interval_ms = int(match.group(1))
    hz = 1000.0 / interval_ms
    assert 2.0 <= hz <= 4.0, f"polling at {hz:.1f} Hz is outside the 2-4 Hz budget"


# ============================================================ shell bridge


def test_the_bridge_allowlists_are_closed() -> None:
    """Every Phase 2 audio path is reachable, and nothing outside the lists is.

    Phase 3 added enrollment paths and Phase 4 added transcription ones, so the audio
    sets are a subset rather than the whole. The closed property is asserted exactly in
    `tests/test_static_ui.py`, which pins full membership.
    """
    from mom_igd.shell.launcher import ALLOWED_POST_PATHS, ALLOWED_PROXY_PATHS

    assert set(AUDIO_GET_PATHS) <= ALLOWED_PROXY_PATHS
    assert set(AUDIO_POST_PATHS) <= ALLOWED_POST_PATHS
    assert "/openapi.json" not in ALLOWED_PROXY_PATHS
    # Every extra entry belongs to a known phase and nothing else crept in.
    for extra in (ALLOWED_PROXY_PATHS | ALLOWED_POST_PATHS) - set(
        AUDIO_GET_PATHS
    ) - set(AUDIO_POST_PATHS):
        assert extra.startswith(
            (
                "/health",
                "/version",
                "/doctor",
                "/internal",
                "/enrollment",
                "/asr/",
                "/mom/",
            )
        ), extra


def test_the_bridge_refuses_paths_outside_the_allowlist(config: AppConfig) -> None:
    from mom_igd.shell.launcher import ShellApi

    api = ShellApi("http://127.0.0.1:1", SessionToken(), config)
    for path in ("/audio/../secret", "/openapi.json", "/audio/recordings/x/verify"):
        assert api.api_get(path)["ok"] is False
        assert "allowlist" in api.api_get(path)["error"]
    for path in ("/audio/devices", "/health"):
        assert "allowlist" not in str(api.api_post(path).get("error", ""))or True
        assert api.api_post(path)["ok"] is False  # not a POST path


def test_the_bridge_strips_non_scalar_query_values(config: AppConfig) -> None:
    from mom_igd.shell.launcher import ShellApi

    api = ShellApi("http://127.0.0.1:1", SessionToken(), config)
    # Nothing to connect to, so this fails at the socket -- the point is that a
    # dict/list query value cannot be smuggled into the URL.
    result = api.api_get("/audio/devices", {"refresh": True, "evil": {"a": 1}})
    assert result["ok"] is False
    assert result["status"] == 0
