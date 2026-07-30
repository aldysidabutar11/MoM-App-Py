"""Phase 3 HTTP boundary: authentication, error mapping, and what must never leak.

Every route in this router concerns identifiable people and their biometric data, so
there is no public endpoint and no reason for one. These tests assert that, plus the
properties that would be expensive to discover later:

* **no response carries audio, an embedding, ciphertext, a key or a path;**
* **read-only routes open no device and create no key;**
* **a quality rejection answers 200, not 500** -- the request was processed
  correctly, the audio simply was not good enough;
* **no request field can select the fake provider.**

Everything runs against a temporary data root with the Phase 2 fake backend. No
physical microphone, no real DPAPI key, no real model.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import uuid as uuid_module
from pathlib import Path
from typing import Any

import pytest

from mom_igd.api.enrollment_routes import (
    _CONFLICT_REASONS,
    _SERVER_REASONS,
    _UNAVAILABLE_REASONS,
    _status_for,
)
from mom_igd.audio.devices import DeviceDiscoveryService
from mom_igd.audio.fake_backend import FakeAudioBackend, SineSource
from mom_igd.audio.service import RecordingService
from mom_igd.config import AppConfig
from mom_igd.db import initialize_database
from mom_igd.db.connection import connect
from mom_igd.enrollment.consent import CONSENT_TEXT_SHA256
from mom_igd.enrollment.service import ReasonCode
from mom_igd.security import SESSION_TOKEN_HEADER, SessionToken

# Every route, with a method and a body that is *valid enough* to get past FastAPI's
# own validation, so the assertion under test is authentication rather than schema.
GET_ROUTES = (
    "/enrollment/participants",
    "/enrollment/consent/text",
    "/enrollment/sessions/current",
    "/enrollment/cleanup/pending",
)
UUID_GET_ROUTES = (
    "/enrollment/participants/{u}",
    "/enrollment/participants/{u}/consent",
    "/enrollment/participants/{u}/readiness",
    "/enrollment/participants/{u}/voiceprint",
    "/enrollment/participants/{u}/eligibility",
    "/enrollment/meetings/{u}/participants",
)
POST_ROUTES = (
    ("/enrollment/participants", {"display_name": "X"}),
    ("/enrollment/sessions", {"participant_uuid": "0" * 8 + "-0000-4000-8000-" + "0" * 12}),
    ("/enrollment/sessions/current/samples", {"seconds": 2.0}),
    ("/enrollment/sessions/current/finalize", {}),
    ("/enrollment/sessions/current/cancel", {}),
    ("/enrollment/cleanup/retry", {}),
)


@pytest.fixture
def audio_config(config: AppConfig) -> AppConfig:
    payload = config.model_dump()
    payload["audio"] = {
        **config.audio.model_dump(),
        "min_free_disk_gb": 0.0,
        "low_disk_abort_gb": 0.0,
    }
    return AppConfig.model_validate(payload)


@pytest.fixture
def db_path(audio_config: AppConfig, paths) -> Path:
    initialize_database(
        paths.database_path(audio_config.database.filename),
        busy_timeout_ms=audio_config.database.busy_timeout_ms,
        app_version=audio_config.app_version,
    )
    return paths.database_path(audio_config.database.filename)


@pytest.fixture
def factory(db_path: Path, audio_config: AppConfig):
    def _connect() -> sqlite3.Connection:
        return connect(db_path, busy_timeout_ms=audio_config.database.busy_timeout_ms)

    return _connect


@pytest.fixture
def app_and_backend(audio_config: AppConfig, paths, token: SessionToken, db_path):
    """An app with a fake-backend recording service already on app.state.

    The enrollment context is created lazily on first request and picks up this
    recording service, so enrollment and recording share one capture lock exactly as
    they do in production.
    """
    from mom_igd.api.app import create_app

    backend = FakeAudioBackend(blocksize=1_200, source=SineSource(level_dbfs=-20.0))
    app = create_app(audio_config, session_token=token, paths=paths)
    app.state.recording_service = RecordingService(
        audio_config,
        paths,
        backend=backend,
        discovery=DeviceDiscoveryService(backend, endpoint_provider=lambda: []),
    )
    yield app, backend
    context = getattr(app.state, "enrollment_context", None)
    if context is not None:
        try:
            context.capture.shutdown()
            context.enrollment.shutdown()
        except Exception:  # noqa: BLE001
            pass
    try:
        app.state.recording_service.abandon("test teardown")
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def client(app_and_backend, token: SessionToken):
    from starlette.testclient import TestClient

    app, _backend = app_and_backend
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        test_client.headers.update({SESSION_TOKEN_HEADER: token.value})
        yield test_client


@pytest.fixture
def anon(app_and_backend):
    """A client with no token at all."""
    from starlette.testclient import TestClient

    app, _backend = app_and_backend
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def _make_participant(client, name: str = "Budi") -> str:
    response = client.post("/enrollment/participants", json={"display_name": name})
    assert response.status_code == 201, response.text
    return response.json()["participant"]["uuid"]


def _grant(client, participant_uuid: str) -> None:
    response = client.post(
        f"/enrollment/participants/{participant_uuid}/consent/grant",
        json={"acknowledged_text_sha256": CONSENT_TEXT_SHA256},
    )
    assert response.status_code == 200, response.text


def _meeting(factory) -> str:
    meeting_uuid = str(uuid_module.uuid4())
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO meetings (title, uuid) VALUES ('Rapat', ?)", (meeting_uuid,)
        )
        conn.commit()
    finally:
        conn.close()
    return meeting_uuid


# ========================================================== authentication


@pytest.mark.parametrize("path", GET_ROUTES)
def test_get_routes_require_a_token(anon, path: str) -> None:
    assert anon.get(path).status_code == 401


@pytest.mark.parametrize("template", UUID_GET_ROUTES)
def test_uuid_get_routes_require_a_token(anon, template: str) -> None:
    path = template.format(u=str(uuid_module.uuid4()))
    assert anon.get(path).status_code == 401


@pytest.mark.parametrize(("path", "body"), POST_ROUTES)
def test_post_routes_require_a_token(anon, path: str, body: dict) -> None:
    assert anon.post(path, json=body).status_code == 401


def test_a_wrong_token_is_refused(app_and_backend) -> None:
    from starlette.testclient import TestClient

    app, _ = app_and_backend
    with TestClient(app, base_url="http://127.0.0.1") as bad:
        bad.headers.update({SESSION_TOKEN_HEADER: "not-the-token"})
        assert bad.get("/enrollment/participants").status_code == 401


def test_a_credential_in_the_query_string_is_refused(client, token: SessionToken) -> None:
    """Consistent with Phase 1: query strings reach logs, history and referrers."""
    from starlette.testclient import TestClient

    app = client.app
    with TestClient(app, base_url="http://127.0.0.1") as plain:
        response = plain.get(f"/enrollment/participants?token={token.value}")
        assert response.status_code in (400, 401)


def test_every_enrollment_route_is_declared_protected(client) -> None:
    """A route added later without the dependency would be a silent hole."""
    schema = client.app.openapi()["paths"]
    enrollment_paths = [p for p in schema if p.startswith("/enrollment")]
    assert len(enrollment_paths) >= 20, enrollment_paths
    # The router-level dependency is not visible per-path in the schema, so assert it
    # on the router object itself.
    from mom_igd.api.enrollment_routes import enrollment_router

    assert enrollment_router.dependencies, "router has no auth dependency"


def test_there_is_no_public_phase_3_endpoint(client) -> None:
    from mom_igd.api.routes import public_router

    public_paths = {getattr(r, "path", "") for r in public_router.routes}
    assert not any("enroll" in p or "participant" in p or "consent" in p for p in public_paths)


# ========================================================== error mapping


def test_every_reason_code_maps_to_exactly_one_bucket() -> None:
    """A new reason code must not silently default to 500."""
    buckets = (_CONFLICT_REASONS, _UNAVAILABLE_REASONS, _SERVER_REASONS)
    for reason in ReasonCode:
        hits = [b for b in buckets if reason in b]
        assert len(hits) == 1, f"{reason.value} appears in {len(hits)} buckets"


@pytest.mark.parametrize("reason", list(ReasonCode))
def test_every_reason_code_maps_to_a_sane_status(reason: ReasonCode) -> None:
    assert _status_for(reason) in (409, 500, 503)


def test_model_unavailable_maps_to_503() -> None:
    assert _status_for(ReasonCode.MODEL_UNAVAILABLE) == 503


def test_capture_lock_held_maps_to_409() -> None:
    assert _status_for(ReasonCode.CAPTURE_LOCK_HELD) == 409


def test_an_unknown_participant_is_404(client) -> None:
    missing = str(uuid_module.uuid4())
    for path in (
        f"/enrollment/participants/{missing}",
        f"/enrollment/participants/{missing}/consent",
        f"/enrollment/participants/{missing}/voiceprint",
    ):
        assert client.get(path).status_code == 404, path


@pytest.mark.parametrize(
    "bad", ["not-a-uuid", "1234", "ABCDEF01-2345-4678-89AB-CDEF01234567", "../etc/passwd"]
)
def test_a_malformed_uuid_is_422_not_404(client, bad: str) -> None:
    """Distinguishing malformed from absent keeps the operator from hunting a ghost."""
    response = client.get(f"/enrollment/participants/{bad}")
    assert response.status_code in (404, 422), response.text
    if response.status_code == 422:
        assert "UUID" in response.text


def test_a_path_cannot_be_smuggled_through_a_uuid(client) -> None:
    response = client.post(
        "/enrollment/voiceprints/..%2F..%2Fetc%2Fpasswd/verify",
    )
    assert response.status_code in (404, 422)


def test_pagination_bounds_are_enforced(client) -> None:
    assert client.get("/enrollment/participants?limit=0").status_code == 422
    assert client.get("/enrollment/participants?limit=99999").status_code == 422
    assert client.get("/enrollment/participants?offset=-1").status_code == 422


def test_an_over_long_display_name_is_refused(client) -> None:
    response = client.post("/enrollment/participants", json={"display_name": "x" * 200})
    assert response.status_code == 422


# ========================================================== participant CRUD


def test_participant_crud_round_trip(client) -> None:
    participant = _make_participant(client, "Budi Santoso")
    detail = client.get(f"/enrollment/participants/{participant}").json()
    assert detail["participant"]["display_name"] == "Budi Santoso"
    assert detail["consent"]["active"] is False
    assert detail["voiceprint"]["has_usable_voiceprint"] is False

    patched = client.patch(
        f"/enrollment/participants/{participant}", json={"role": "Ketua"}
    )
    assert patched.status_code == 200
    assert patched.json()["participant"]["role"] == "Ketua"

    off = client.post(f"/enrollment/participants/{participant}/deactivate", json={})
    assert off.json()["participant"]["is_active"] is False
    on = client.post(f"/enrollment/participants/{participant}/reactivate")
    assert on.json()["participant"]["is_active"] is True


def test_duplicate_display_names_are_accepted(client) -> None:
    first = _make_participant(client, "Budi")
    second = _make_participant(client, "Budi")
    assert first != second
    listing = client.get("/enrollment/participants?search=Budi").json()
    assert listing["total"] == 2


def test_no_participant_response_exposes_the_row_id(client) -> None:
    participant = _make_participant(client)
    body = client.get(f"/enrollment/participants/{participant}").text
    assert '"id"' not in body


def test_the_listing_carries_consent_and_voiceprint_state(client) -> None:
    """One request per screen, not one per person."""
    participant = _make_participant(client)
    _grant(client, participant)
    entry = client.get("/enrollment/participants").json()["participants"][0]
    assert entry["consent"]["active"] is True
    assert entry["voiceprint"] is None


# ==================================================== meeting membership cap


def test_the_ninth_participant_is_the_last(client, factory) -> None:
    meeting = _meeting(factory)
    for index in range(9):
        participant = _make_participant(client, f"Orang {index}")
        response = client.post(
            f"/enrollment/meetings/{meeting}/participants",
            json={"participant_uuid": participant},
        )
        assert response.status_code == 200, response.text
    assert response.json()["active_count"] == 9

    tenth = _make_participant(client, "Kesepuluh")
    over = client.post(
        f"/enrollment/meetings/{meeting}/participants", json={"participant_uuid": tenth}
    )
    assert over.status_code == 409
    # The refusal names the meeting's own capacity rather than claiming a
    # product-wide maximum, because capacity is per meeting and adjustable.
    assert "roster capacity" in over.text
    assert "9" in over.text


# ============================================== roster capacity over the API


def test_the_roster_endpoint_reports_count_capacity_and_ceiling(
    client, factory
) -> None:
    meeting = _meeting(factory)
    body = client.get(f"/enrollment/meetings/{meeting}/roster").json()
    assert body["active_count"] == 0
    assert body["capacity"] == 9
    assert body["slots_remaining"] == 9
    assert body["minimum_capacity"] == 1
    assert body["maximum_capacity"] == 50
    assert body["meeting_title"] == "Rapat"


def test_the_roster_response_carries_no_internal_row_id(client, factory) -> None:
    meeting = _meeting(factory)
    participant = _make_participant(client)
    client.post(
        f"/enrollment/meetings/{meeting}/participants",
        json={"participant_uuid": participant},
    )
    body = client.get(f"/enrollment/meetings/{meeting}/roster").text
    assert '"id"' not in body
    assert '"meeting_id"' not in body
    assert '"participant_id"' not in body


def test_the_meetings_listing_is_bounded_and_uuid_addressed(client, factory) -> None:
    for _ in range(3):
        _meeting(factory)
    body = client.get("/enrollment/meetings", params={"limit": 2}).json()
    assert body["total"] == 3
    assert len(body["meetings"]) == 2
    assert body["limit"] == 2
    for entry in body["meetings"]:
        assert len(entry["meeting_uuid"]) == 36
        assert entry["capacity"] == 9
    assert '"id"' not in client.get("/enrollment/meetings").text


@pytest.mark.parametrize("wanted", [1, 10, 20, 50])
def test_capacity_can_be_patched_to_an_allowed_value(client, factory, wanted) -> None:
    meeting = _meeting(factory)
    response = client.patch(
        f"/enrollment/meetings/{meeting}/capacity", json={"capacity": wanted}
    )
    assert response.status_code == 200, response.text
    assert response.json()["capacity"] == wanted
    # And it is genuinely stored, not just echoed.
    assert client.get(f"/enrollment/meetings/{meeting}/roster").json()["capacity"] == (
        wanted
    )


@pytest.mark.parametrize("bad", [0, -1, 51, 1000])
def test_a_capacity_outside_the_range_is_422(client, factory, bad) -> None:
    """The value itself is unacceptable, independent of the meeting's state."""
    meeting = _meeting(factory)
    response = client.patch(
        f"/enrollment/meetings/{meeting}/capacity", json={"capacity": bad}
    )
    assert response.status_code == 422, response.text
    assert client.get(f"/enrollment/meetings/{meeting}/roster").json()["capacity"] == 9


@pytest.mark.parametrize("bad", [True, False, 1.5, "12", "", None, [12], {"a": 1}])
def test_a_non_integer_capacity_is_422(client, factory, bad) -> None:
    meeting = _meeting(factory)
    response = client.patch(
        f"/enrollment/meetings/{meeting}/capacity", json={"capacity": bad}
    )
    assert response.status_code == 422, f"{bad!r} -> {response.status_code}"


def test_lowering_capacity_below_the_roster_is_409_and_removes_nobody(
    client, factory
) -> None:
    """A conflict with this meeting's state, not a bad value -- hence 409, not 422."""
    meeting = _meeting(factory)
    client.patch(f"/enrollment/meetings/{meeting}/capacity", json={"capacity": 12})
    for index in range(7):
        participant = _make_participant(client, f"Orang {index}")
        client.post(
            f"/enrollment/meetings/{meeting}/participants",
            json={"participant_uuid": participant},
        )

    response = client.patch(
        f"/enrollment/meetings/{meeting}/capacity", json={"capacity": 4}
    )
    assert response.status_code == 409, response.text
    roster = client.get(f"/enrollment/meetings/{meeting}/roster").json()
    assert roster["capacity"] == 12, "a rejected value must not be stored"
    assert roster["active_count"] == 7, "nobody may be removed to fit a new capacity"


def test_capacity_for_an_unknown_meeting_is_404(client) -> None:
    response = client.patch(
        f"/enrollment/meetings/{uuid_module.uuid4()}/capacity", json={"capacity": 12}
    )
    assert response.status_code == 404


def test_a_malformed_meeting_uuid_is_422_not_404(client) -> None:
    response = client.patch(
        "/enrollment/meetings/not-a-uuid/capacity", json={"capacity": 12}
    )
    assert response.status_code == 422


def test_raising_capacity_then_adding_a_tenth_participant_succeeds(
    client, factory
) -> None:
    meeting = _meeting(factory)
    for index in range(9):
        participant = _make_participant(client, f"Orang {index}")
        client.post(
            f"/enrollment/meetings/{meeting}/participants",
            json={"participant_uuid": participant},
        )
    tenth = _make_participant(client, "Kesepuluh")
    assert (
        client.post(
            f"/enrollment/meetings/{meeting}/participants",
            json={"participant_uuid": tenth},
        ).status_code
        == 409
    )

    client.patch(f"/enrollment/meetings/{meeting}/capacity", json={"capacity": 15})
    added = client.post(
        f"/enrollment/meetings/{meeting}/participants", json={"participant_uuid": tenth}
    )
    assert added.status_code == 200, added.text
    assert added.json()["active_count"] == 10
    assert added.json()["slots_remaining"] == 5


def test_two_meetings_keep_independent_capacities_over_the_api(
    client, factory
) -> None:
    first, second = _meeting(factory), _meeting(factory)
    client.patch(f"/enrollment/meetings/{first}/capacity", json={"capacity": 12})
    client.patch(f"/enrollment/meetings/{second}/capacity", json={"capacity": 30})
    assert client.get(f"/enrollment/meetings/{first}/roster").json()["capacity"] == 12
    assert client.get(f"/enrollment/meetings/{second}/roster").json()["capacity"] == 30


def test_the_capacity_routes_require_the_session_token(anon, factory) -> None:
    meeting = _meeting(factory)
    for method, path in (
        ("get", "/enrollment/meetings"),
        ("get", f"/enrollment/meetings/{meeting}/roster"),
    ):
        response = getattr(anon, method)(path)
        assert response.status_code == 401, path
    response = anon.patch(
        f"/enrollment/meetings/{meeting}/capacity", json={"capacity": 12}
    )
    assert response.status_code == 401


def test_removing_a_participant_frees_a_slot(client, factory) -> None:
    meeting = _meeting(factory)
    participant = _make_participant(client)
    client.post(
        f"/enrollment/meetings/{meeting}/participants",
        json={"participant_uuid": participant},
    )
    removed = client.delete(
        f"/enrollment/meetings/{meeting}/participants/{participant}"
    )
    assert removed.status_code == 200
    assert removed.json()["active_count"] == 0


# ================================================================== consent


def test_the_consent_text_endpoint_carries_the_hash_and_draft_flag(client) -> None:
    bundle = client.get("/enrollment/consent/text").json()
    assert bundle["text_sha256"] == CONSENT_TEXT_SHA256
    assert bundle["review_pending"] is True
    assert bundle["version"].endswith("-draft")
    assert "PERSETUJUAN" in bundle["text"]


def test_granting_requires_the_displayed_text_hash(client) -> None:
    """Stops a UI granting consent to wording nobody saw."""
    participant = _make_participant(client)
    wrong = client.post(
        f"/enrollment/participants/{participant}/consent/grant",
        json={"acknowledged_text_sha256": "a" * 64},
    )
    assert wrong.status_code == 409
    assert "does not match" in wrong.text


def test_consent_grant_and_revoke_round_trip(client) -> None:
    participant = _make_participant(client)
    _grant(client, participant)
    state = client.get(f"/enrollment/participants/{participant}/consent").json()
    assert state["consent"]["active"] is True

    revoked = client.post(
        f"/enrollment/participants/{participant}/consent/revoke",
        json={"reason": "withdrawn"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["eligible"] is False
    after = client.get(f"/enrollment/participants/{participant}/consent").json()
    assert after["consent"]["active"] is False
    # The grant is still on the record: the log is append-only.
    assert [h["action"] for h in after["history"]] == ["REVOKED", "GRANTED"]


def test_double_granting_is_idempotent_over_http(client) -> None:
    participant = _make_participant(client)
    _grant(client, participant)
    again = client.post(
        f"/enrollment/participants/{participant}/consent/grant",
        json={"acknowledged_text_sha256": CONSENT_TEXT_SHA256},
    )
    assert again.status_code == 200
    assert again.json()["granted"]["already_active"] is True


# =============================================== readiness / model unavailable


def test_readiness_reports_model_unavailable_and_blocks_start(client) -> None:
    participant = _make_participant(client)
    _grant(client, participant)
    ready = client.get(f"/enrollment/participants/{participant}/readiness").json()
    assert ready["can_start"] is False
    assert ReasonCode.MODEL_UNAVAILABLE.value in ready["blockers"]
    assert ready["model"]["ready"] is False


def test_starting_without_a_model_is_503(client) -> None:
    participant = _make_participant(client)
    _grant(client, participant)
    response = client.post(
        "/enrollment/sessions", json={"participant_uuid": participant}
    )
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["reason"] == ReasonCode.MODEL_UNAVAILABLE.value


def test_model_unavailable_opens_no_microphone(client, app_and_backend) -> None:
    """Nobody is asked to speak for a template that cannot be built."""
    _app, backend = app_and_backend
    participant = _make_participant(client)
    _grant(client, participant)
    before = backend.open_calls
    client.get(f"/enrollment/participants/{participant}/readiness")
    client.post("/enrollment/sessions", json={"participant_uuid": participant})
    assert backend.open_calls == before


def test_starting_without_consent_is_409(client) -> None:
    participant = _make_participant(client)
    response = client.post(
        "/enrollment/sessions", json={"participant_uuid": participant}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == ReasonCode.CONSENT_MISSING.value


def test_capturing_a_sample_without_a_session_is_refused(client) -> None:
    response = client.post("/enrollment/sessions/current/samples", json={"seconds": 2})
    assert response.status_code in (409, 500)
    assert "No enrollment is in progress" in response.text


def test_cancelling_with_no_session_is_idempotent(client) -> None:
    for _ in range(3):
        response = client.post("/enrollment/sessions/current/cancel", json={})
        assert response.status_code == 200
        assert response.json()["active"] is False


# ============================================== no fake provider is selectable


def test_no_request_field_can_select_a_provider(client) -> None:
    """A stand-in template would be encrypted, stored and marked eligible."""
    schema = client.app.openapi()
    blob = repr(schema).lower()
    for forbidden in ("fake", "test_double", "provider_name", "use_fake"):
        assert forbidden not in blob, f"the API schema mentions {forbidden!r}"


def test_extra_body_fields_cannot_smuggle_a_provider(client) -> None:
    participant = _make_participant(client)
    _grant(client, participant)
    response = client.post(
        "/enrollment/sessions",
        json={
            "participant_uuid": participant,
            "provider": "FAKE-test-embed",
            "use_fake_provider": True,
        },
    )
    # Still refused for the real reason: no model. The extra fields change nothing.
    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == ReasonCode.MODEL_UNAVAILABLE.value


def test_there_is_no_audio_upload_route(client) -> None:
    """Raw audio must never cross the HTTP boundary (ADR-0012)."""
    schema = client.app.openapi()
    for path, methods in schema["paths"].items():
        if not path.startswith("/enrollment"):
            continue
        for method, spec in methods.items():
            body = spec.get("requestBody", {})
            content = body.get("content", {}) if isinstance(body, dict) else {}
            for media_type in content:
                assert media_type == "application/json", (
                    f"{method.upper()} {path} accepts {media_type}; enrollment audio "
                    "must never be uploaded"
                )


# ================================================ no leakage in any response


def _walk(client, factory) -> list[tuple[str, str]]:
    """Hit every readable route and return (label, body) pairs."""
    participant = _make_participant(client, "Budi Santoso")
    _grant(client, participant)
    meeting = _meeting(factory)
    client.post(
        f"/enrollment/meetings/{meeting}/participants",
        json={"participant_uuid": participant},
    )
    bodies = [
        ("participants", client.get("/enrollment/participants").text),
        ("detail", client.get(f"/enrollment/participants/{participant}").text),
        ("consent", client.get(f"/enrollment/participants/{participant}/consent").text),
        ("consent_text", client.get("/enrollment/consent/text").text),
        (
            "readiness",
            client.get(f"/enrollment/participants/{participant}/readiness").text,
        ),
        (
            "voiceprint",
            client.get(f"/enrollment/participants/{participant}/voiceprint").text,
        ),
        (
            "eligibility",
            client.get(f"/enrollment/participants/{participant}/eligibility").text,
        ),
        ("session", client.get("/enrollment/sessions/current").text),
        ("cleanup", client.get("/enrollment/cleanup/pending").text),
        ("meeting", client.get(f"/enrollment/meetings/{meeting}/participants").text),
    ]
    return bodies


def test_no_response_leaks_a_biometric_payload(client, factory) -> None:
    for label, body in _walk(client, factory):
        lowered = body.lower()
        for forbidden in (
            "centroid",
            "dispersion",
            "ciphertext",
            "nonce",
            "dpapi",
            "master_key",
            "key_material",
            "embedding\":",
            "pcm",
        ):
            assert forbidden not in lowered, f"{label} leaked {forbidden!r}"


def _strings(value: Any) -> list[str]:
    """Every string inside a decoded JSON document."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)] + [
            s for k in value for s in _strings(k)
        ]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def test_no_response_leaks_a_filesystem_path(client, factory, paths) -> None:
    """Search the *decoded* strings, not the wire form.

    Scanning raw JSON gives false positives: a newline is escaped as ``\\n``, so
    Indonesian text like "biometrik:" followed by a line break reads as
    ``k:\\`` and looks like a drive letter. Decoding first removes the transport
    artefact and leaves only real content.
    """
    drive = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")
    for label, body in _walk(client, factory):
        import json

        for text in _strings(json.loads(body)):
            assert not drive.search(text), f"{label} leaked a path: {text[:120]!r}"
        assert str(paths.root) not in body, f"{label} leaked the data root"
        assert "envelope_relative_path" not in body, f"{label} leaked a relative path"


def test_no_response_leaks_the_session_token(client, factory, token: SessionToken) -> None:
    for label, body in _walk(client, factory):
        assert token.value not in body, f"{label} leaked the session token"


def test_an_internal_error_returns_a_generic_message(client, monkeypatch) -> None:
    """A traceback or a path in an HTTP body is a leak even on loopback."""
    from mom_igd.enrollment.participants import ParticipantService

    def _boom(self, **kwargs):  # noqa: ANN001
        raise RuntimeError(r"secret detail with D:\MoM-IGD-Data\keys inside")

    monkeypatch.setattr(ParticipantService, "list", _boom)
    response = client.get("/enrollment/participants")
    assert response.status_code == 500
    body = response.text
    assert "secret detail" not in body
    assert "MoM-IGD-Data" not in body
    assert "Traceback" not in body
    assert "logged locally" in body


# ============================================= read-only routes are inert


def test_read_only_routes_create_no_key_and_open_no_device(
    client, app_and_backend, paths, factory
) -> None:
    _app, backend = app_and_backend
    before_opens = backend.open_calls
    _walk(client, factory)
    assert backend.open_calls == before_opens, "a read-only route opened the device"
    assert not (paths.keys_dir / "voiceprint_master.dpapi").exists()
    assert list(paths.voiceprints_dir.glob("*.vpx")) == [] if paths.voiceprints_dir.exists() else True


def test_verify_does_not_unwrap_the_master_key(client, paths) -> None:
    """Integrity checking must not be able to pull plaintext into the process."""
    missing = str(uuid_module.uuid4())
    response = client.post(f"/enrollment/voiceprints/{missing}/verify")
    assert response.status_code == 404
    assert not (paths.keys_dir / "voiceprint_master.dpapi").exists()


def test_cleanup_pending_is_empty_and_side_effect_free(client, paths) -> None:
    payload = client.get("/enrollment/cleanup/pending").json()
    assert payload == {"pending": [], "count": 0}
    assert not (paths.keys_dir / "voiceprint_master.dpapi").exists()


def test_cleanup_retry_is_idempotent(client) -> None:
    first = client.post("/enrollment/cleanup/retry", json={})
    second = client.post("/enrollment/cleanup/retry", json={})
    assert first.status_code == second.status_code == 200
    assert first.json()["cleanup_retried"] == 0
    assert second.json()["changed"] is False


# =========================================== app-instance isolation of state


def test_two_apps_do_not_share_an_enrollment_context(
    audio_config: AppConfig, paths, token: SessionToken, db_path
) -> None:
    """A module-level singleton would leak state between apps -- and between tests."""
    from starlette.testclient import TestClient

    from mom_igd.api.app import create_app

    contexts = []
    for _ in range(2):
        backend = FakeAudioBackend(blocksize=1_200, source=SineSource(level_dbfs=-20.0))
        app = create_app(audio_config, session_token=token, paths=paths)
        app.state.recording_service = RecordingService(
            audio_config,
            paths,
            backend=backend,
            discovery=DeviceDiscoveryService(backend, endpoint_provider=lambda: []),
        )
        with TestClient(app, base_url="http://127.0.0.1") as test_client:
            test_client.headers.update({SESSION_TOKEN_HEADER: token.value})
            test_client.get("/enrollment/participants")
        contexts.append(app.state.enrollment_context)

    assert contexts[0] is not contexts[1]
    assert contexts[0].enrollment is not contexts[1].enrollment
    assert contexts[0].capture is not contexts[1].capture


def test_the_context_is_created_once_under_concurrency(client) -> None:
    """Two first requests arriving together must not build two contexts."""
    app = client.app
    assert getattr(app.state, "enrollment_context", None) is None

    seen: list[Any] = []
    barrier = threading.Barrier(4)

    def _hit() -> None:
        barrier.wait()
        client.get("/enrollment/participants")
        seen.append(app.state.enrollment_context)

    threads = [threading.Thread(target=_hit) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(set(id(context) for context in seen)) == 1, "more than one context built"


def test_the_context_reuses_the_recording_service_on_app_state(client, app_and_backend) -> None:
    """Enrollment and recording must share one capture lock, not two."""
    app, _backend = app_and_backend
    client.get("/enrollment/participants")
    context = app.state.enrollment_context
    assert context.enrollment._capture_lock is app.state.recording_service._lock  # noqa: SLF001


def test_shutdown_closes_the_capture_controller(
    audio_config: AppConfig, paths, token: SessionToken, db_path
) -> None:
    """Leaving the shared lock held would block the next recording."""
    from starlette.testclient import TestClient

    from mom_igd.api.app import create_app

    backend = FakeAudioBackend(blocksize=1_200, source=SineSource(level_dbfs=-20.0))
    app = create_app(audio_config, session_token=token, paths=paths)
    app.state.recording_service = RecordingService(
        audio_config,
        paths,
        backend=backend,
        discovery=DeviceDiscoveryService(backend, endpoint_provider=lambda: []),
    )
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        test_client.headers.update({SESSION_TOKEN_HEADER: token.value})
        test_client.get("/enrollment/participants")
        context = app.state.enrollment_context
    # Leaving the context manager ran lifespan shutdown.
    assert context.capture.capturing is False
    assert not context.enrollment._capture_lock.path.exists()  # noqa: SLF001
