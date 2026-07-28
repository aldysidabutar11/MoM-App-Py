"""Local API: public endpoints, token enforcement, loopback enforcement.

Covers Phase 1 test categories 14, 15, 16 and 17.
"""

from __future__ import annotations

import json

import pytest

from mom_igd.config import PUBLIC_ENDPOINTS, AppConfig
from mom_igd.security import SESSION_TOKEN_HEADER, SessionToken
from mom_igd.version import APP_NAME, APP_VERSION, CURRENT_PHASE

PROTECTED_PATHS = ("/doctor", "/internal/ready")


# ------------------------------------------------- 14. health and version


def test_health_is_public_and_reports_ok(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["app_name"] == APP_NAME
    assert payload["app_version"] == APP_VERSION
    assert payload["phase"] == CURRENT_PHASE
    assert payload["offline"] is True
    assert payload["runtime_mode"] == "offline"


def test_health_reports_database_and_data_dir_booleans(client) -> None:
    payload = client.get("/health").json()
    assert set(payload["database"]) == {
        "exists",
        "ready",
        "schema_version",
        "head_version",
        "wal",
        "foreign_keys",
    }
    assert set(payload["data_dir"]) == {"configured", "exists", "writable", "complete"}


def test_health_does_not_disclose_filesystem_paths(client, paths) -> None:
    body = client.get("/health").text
    assert str(paths.root) not in body
    assert "db_dir" not in body
    assert ":\\" not in body, "an unauthenticated endpoint must not leak a Windows path"


def test_version_is_public_and_reports_schema_versions(client) -> None:
    response = client.get("/version")
    assert response.status_code == 200
    payload = response.json()
    assert payload["app_version"] == APP_VERSION
    assert payload["phase"] == CURRENT_PHASE
    for key in ("config_schema_version", "registry_schema_version", "schema_version_head"):
        assert isinstance(payload[key], int)
    assert payload["python"].startswith("3.12")


def test_declared_public_endpoints_need_no_token(client) -> None:
    for path in PUBLIC_ENDPOINTS:
        assert client.get(path).status_code == 200, path


def test_health_works_before_the_database_exists(config: AppConfig, token: SessionToken) -> None:
    """Liveness must not depend on `db init` having been run."""
    from starlette.testclient import TestClient

    from mom_igd.api.app import create_app

    runtime = config.runtime_paths()  # deliberately NOT ensured
    app = create_app(config, session_token=token, paths=runtime)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["database"]["exists"] is False
    assert payload["database"]["ready"] is False


# ---------------------------------- 15/16. token rejection and acceptance


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_protected_endpoint_without_token_is_rejected(client, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 401
    assert "token" in response.json()["detail"].lower()


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_protected_endpoint_with_wrong_token_is_rejected(client, path: str) -> None:
    response = client.get(path, headers={SESSION_TOKEN_HEADER: "x" * 43})
    assert response.status_code == 401


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_protected_endpoint_with_correct_token_is_accepted(
    client, token: SessionToken, path: str
) -> None:
    response = client.get(path, headers={SESSION_TOKEN_HEADER: token.value})
    assert response.status_code in (200, 503), response.text
    assert response.json()


def test_bearer_authorization_header_is_accepted(client, token: SessionToken) -> None:
    response = client.get("/doctor", headers={"Authorization": f"Bearer {token.value}"})
    assert response.status_code == 200


def test_bearer_with_wrong_scheme_is_rejected(client, token: SessionToken) -> None:
    response = client.get("/doctor", headers={"Authorization": f"Basic {token.value}"})
    assert response.status_code == 401


def test_empty_token_header_is_rejected(client) -> None:
    assert client.get("/doctor", headers={SESSION_TOKEN_HEADER: ""}).status_code == 401


def test_ready_endpoint_reports_readiness_and_no_capabilities(
    client, token: SessionToken, conn
) -> None:
    # `conn` migrates the database: readiness legitimately requires it.
    payload = client.get("/internal/ready", headers=token.header()).json()
    assert payload["ready"] is True, payload["blockers"]
    assert payload["phase"] == CURRENT_PHASE
    # Phase 1 must not advertise a capability it does not have.
    assert payload["capabilities"] == {
        "audio_capture": False,
        "asr": False,
        "diarization": False,
        "voice_id": False,
        "mom_extraction": False,
        "export": False,
    }
    assert payload["offline"]["cloud_sdks"] == []
    assert payload["offline"]["firewall_enforcement"] == "deferred to Phase 11"


def test_ready_returns_503_when_the_database_is_missing(
    config: AppConfig, token: SessionToken
) -> None:
    from starlette.testclient import TestClient

    from mom_igd.api.app import create_app

    app = create_app(config, session_token=token, paths=config.runtime_paths())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/internal/ready", headers=token.header())
    assert response.status_code == 503
    assert response.json()["ready"] is False
    assert any("database" in blocker for blocker in response.json()["blockers"])


# ------------------------------------------------------ 17. token never leaks


@pytest.mark.parametrize("query_key", ["token", "session_token", "access_token", "api_key"])
def test_correct_token_in_a_query_string_is_refused(
    client, token: SessionToken, query_key: str
) -> None:
    """Refused with 400, not accepted: query strings end up in logs and history."""
    response = client.get(f"/doctor?{query_key}={token.value}")
    assert response.status_code == 400
    assert query_key in response.json()["detail"]


def test_token_never_appears_in_any_response_body(client, token: SessionToken) -> None:
    bodies = [
        client.get("/health").text,
        client.get("/version").text,
        client.get("/doctor", headers=token.header()).text,
        client.get("/internal/ready", headers=token.header()).text,
        client.get("/openapi.json").text,
        client.get("/ui/").text,
        client.get("/ui/app.js").text,
        client.get("/ui/app.css").text,
    ]
    for body in bodies:
        assert token.value not in body


def test_token_never_appears_in_response_headers(client, token: SessionToken) -> None:
    for path in ("/health", "/version", "/doctor"):
        response = client.get(path, headers=token.header())
        for name, value in response.headers.items():
            assert token.value not in value, f"{path} leaked the token in header {name}"


def test_doctor_payload_carries_no_credential_field(client, token: SessionToken) -> None:
    payload = client.get("/doctor", headers=token.header()).json()
    flattened = json.dumps(payload).lower()
    for forbidden in ("session_token", "bearer ", "password", "secret"):
        assert forbidden not in flattened


# --------------------------------------------- loopback / Host enforcement


@pytest.mark.parametrize(
    "host", ["evil.example.com", "attacker.test", "192.168.1.50", "0.0.0.0", "mom-igd.local"]
)
def test_non_loopback_host_header_is_refused(client, host: str) -> None:
    response = client.get("/health", headers={"Host": host})
    assert response.status_code == 403
    assert "loopback" in response.json()["detail"].lower()


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8765", "localhost", "localhost:9000", "[::1]:8765"])
def test_loopback_host_headers_are_accepted(client, host: str) -> None:
    assert client.get("/health", headers={"Host": host}).status_code == 200


def test_non_loopback_host_is_refused_even_with_a_valid_token(client, token: SessionToken) -> None:
    response = client.get(
        "/doctor", headers={"Host": "evil.example.com", **token.header()}
    )
    assert response.status_code == 403, "the Host check must run before authentication"


# ------------------------------------------------------------ docs and UI


def test_openapi_and_docs_are_served_on_loopback(client) -> None:
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_docs_can_be_disabled_by_configuration(config: AppConfig, paths, token: SessionToken) -> None:
    from starlette.testclient import TestClient

    from mom_igd.api.app import create_app

    hardened = AppConfig.model_validate(
        {**config.model_dump(), "api": {**config.api.model_dump(), "docs_enabled": False}}
    )
    app = create_app(hardened, session_token=token, paths=paths)
    with TestClient(app, base_url="http://127.0.0.1") as client_off:
        assert client_off.get("/docs").status_code == 404
        assert client_off.get("/openapi.json").status_code == 404


def test_docs_are_unreachable_from_a_non_loopback_host(client) -> None:
    assert client.get("/docs", headers={"Host": "evil.example.com"}).status_code == 403


def test_root_redirects_to_the_ui(client) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (307, 308)
    assert response.headers["location"] == "/ui/"


def test_static_ui_is_served(client) -> None:
    response = client.get("/ui/")
    assert response.status_code == 200
    assert "MoM-IGD" in response.text
    assert "Offline Mode" in response.text


def test_unknown_path_is_a_404_not_the_ui(client) -> None:
    assert client.get("/definitely-not-a-route").status_code == 404


# --------------------------------------------------- app wiring invariants


def test_app_state_exposes_config_paths_and_token(app, config: AppConfig, token: SessionToken) -> None:
    assert app.state.config is config
    assert app.state.paths.root == config.data_root
    assert app.state.session_token is token


def test_create_app_generates_a_token_when_none_is_given(config: AppConfig, paths) -> None:
    from mom_igd.api.app import create_app

    app = create_app(config, paths=paths)
    assert isinstance(app.state.session_token, SessionToken)
    assert len(app.state.session_token.value) >= 32
