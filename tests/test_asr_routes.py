"""The transcription endpoints: authenticated, path-free, and never a downloader.

Every route here is behind the session token, addresses a recording by UUID only, and
returns no filesystem path. The most important negative test is that no endpoint can
cause a model download: provisioning is a deliberate command-line action, and a request
that could trigger a 1.5 GB fetch would put the offline guarantee behind an HTTP call.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mom_igd.security import SESSION_TOKEN_HEADER

ROUTES = Path(__file__).resolve().parent.parent / "mom_igd" / "api" / "asr_routes.py"
RECORDING_UUID = "55555555-5555-4555-8555-555555555555"


@pytest.fixture()
def auth(token: Any) -> dict[str, str]:
    return {SESSION_TOKEN_HEADER: token.value}


@pytest.fixture()
def stored_transcript(conn: sqlite3.Connection, meeting_id: int) -> int:
    """A recording with one complete transcript revision, written directly."""
    with conn:
        cursor = conn.execute(
            "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir, status) "
            "VALUES (?, ?, 'm/r', 'RECORDED')",
            (meeting_id, RECORDING_UUID),
        )
        recording_id = int(cursor.lastrowid or 0)
        cursor = conn.execute(
            "INSERT INTO audio_working_copies (recording_id, relative_path, sha256, "
            "frames, duration_ms, status) VALUES (?, 'working/a.wav', ?, 16000, 1000, "
            "'READY')",
            (recording_id, "ab" * 32),
        )
        working_copy_id = int(cursor.lastrowid or 0)
        cursor = conn.execute(
            "INSERT INTO transcripts (recording_id, working_copy_id, revision, status, "
            "is_active, language, pass1_model_name, segment_count, word_count) "
            "VALUES (?, ?, 1, 'COMPLETE', 1, 'id', 'faster-whisper-small', 1, 1)",
            (recording_id, working_copy_id),
        )
        transcript_id = int(cursor.lastrowid or 0)
        cursor = conn.execute(
            "INSERT INTO transcript_segments (transcript_id, seq, region_seq, asr_pass, "
            "start_ms, end_ms, text, text_raw, word_count, selected_for_pass2, "
            "pass2_reason_codes) VALUES (?, 0, 0, 1, 0, 1000, 'rapat mingguan', "
            "'rapat mingguan', 1, 1, '[\"LOW_AVG_LOGPROB\"]')",
            (transcript_id,),
        )
        segment_id = int(cursor.lastrowid or 0)
        conn.execute(
            "INSERT INTO transcript_words (segment_id, seq, start_ms, end_ms, text, "
            "probability) VALUES (?, 0, 0, 400, 'rapat', 0.9)",
            (segment_id,),
        )
    return recording_id


# ===========================================================================
# Authentication
# ===========================================================================


@pytest.mark.parametrize(
    "path",
    [
        "/asr/status",
        "/asr/models",
        f"/asr/transcript/{RECORDING_UUID}",
        f"/asr/revisions/{RECORDING_UUID}",
        f"/asr/flagged/{RECORDING_UUID}",
    ],
)
def test_every_read_requires_the_session_token(client: Any, path: str) -> None:
    assert client.get(path).status_code == 401


def test_transcribing_requires_the_session_token(client: Any) -> None:
    response = client.post("/asr/transcribe", json={"recording_uuid": RECORDING_UUID})
    assert response.status_code == 401


def test_cancelling_requires_the_session_token(client: Any) -> None:
    assert client.post("/asr/cancel").status_code == 401


def test_a_token_in_the_query_string_is_refused(client: Any, token: Any) -> None:
    response = client.get(f"/asr/status?token={token.value}")
    assert response.status_code == 400


# ===========================================================================
# Status and models
# ===========================================================================


def test_status_reports_readiness_without_loading_a_model(
    client: Any, auth: dict[str, str]
) -> None:
    payload = client.get("/asr/status", headers=auth).json()
    assert payload["busy"] is False
    assert payload["running_recording_uuid"] is None
    assert payload["models"]["pass1_ready"] is False
    assert payload["models"]["readable_index"] is True
    assert payload["pass2_budget_ratio"] == 0.25


def test_status_carries_no_transcript_text(
    client: Any, auth: dict[str, str], stored_transcript: int
) -> None:
    """It is polled frequently; a meeting's words have no business in it."""
    body = client.get("/asr/status", headers=auth).text
    assert "rapat mingguan" not in body


def test_the_models_endpoint_says_provisioning_is_a_cli_action(
    client: Any, auth: dict[str, str]
) -> None:
    payload = client.get("/asr/models", headers=auth).json()
    assert payload["provisioning_is_a_cli_action"] is True
    assert "asr provision" in payload["provision_command"]


def test_no_endpoint_can_trigger_a_download() -> None:
    """Structural: the route module must not reach the provisioning download path."""
    import ast

    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(alias.name for alias in node.names)
    for banned in (
        "provision_model",
        "snapshot_download",
        "hf_hub_download",
        "mom_igd.asr.provision",
        "huggingface_hub",
    ):
        assert banned not in names, banned


# ===========================================================================
# Transcribing
# ===========================================================================


def test_transcribing_without_a_model_is_a_conflict_not_a_download(
    client: Any, auth: dict[str, str], stored_transcript: int
) -> None:
    response = client.post(
        "/asr/transcribe", json={"recording_uuid": RECORDING_UUID}, headers=auth
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "MODEL_UNAVAILABLE" in detail
    assert "asr provision" in detail


@pytest.mark.parametrize(
    "value", ["not-a-uuid", "55555555555555555555555555555555555555", "../../etc/passwd"]
)
def test_a_malformed_recording_identifier_is_refused(
    client: Any, auth: dict[str, str], value: str
) -> None:
    response = client.post(
        "/asr/transcribe", json={"recording_uuid": value}, headers=auth
    )
    assert response.status_code in (422, 400)


def test_a_path_cannot_be_supplied_instead_of_an_identifier(
    client: Any, auth: dict[str, str]
) -> None:
    response = client.post(
        "/asr/transcribe",
        json={"recording_uuid": "D:/MoM-IGD-Data/recordings/x/y.wav"},
        headers=auth,
    )
    assert response.status_code in (422, 400)


def test_cancelling_when_nothing_runs_is_a_conflict(
    client: Any, auth: dict[str, str]
) -> None:
    response = client.post("/asr/cancel", headers=auth)
    assert response.status_code == 409
    assert "nothing to cancel" in response.json()["detail"]


# ===========================================================================
# Reads
# ===========================================================================


def test_the_active_transcript_is_returned(
    client: Any, auth: dict[str, str], stored_transcript: int
) -> None:
    payload = client.get(f"/asr/transcript/{RECORDING_UUID}", headers=auth).json()
    assert payload["recording_uuid"] == RECORDING_UUID
    assert payload["transcript"]["revision"] == 1
    assert payload["transcript"]["is_active"] is True
    assert len(payload["segments"]) == 1
    segment = payload["segments"][0]
    assert segment["text"] == "rapat mingguan"
    assert segment["text_raw"] == "rapat mingguan"
    assert segment["words"][0]["text"] == "rapat"


def test_a_segment_reports_no_speaker_explicitly(
    client: Any, auth: dict[str, str], stored_transcript: int
) -> None:
    """The UI renders "unassigned" from data, not from a missing field."""
    payload = client.get(f"/asr/transcript/{RECORDING_UUID}", headers=auth).json()
    segment = payload["segments"][0]
    assert segment["speaker"] is None
    assert segment["speaker_status"] == "UNASSIGNED"


def test_no_response_contains_a_filesystem_path(
    client: Any, auth: dict[str, str], stored_transcript: int, paths: Any
) -> None:
    for path in (
        "/asr/status",
        f"/asr/transcript/{RECORDING_UUID}",
        f"/asr/revisions/{RECORDING_UUID}",
        f"/asr/flagged/{RECORDING_UUID}",
    ):
        body = client.get(path, headers=auth).text
        assert str(paths.root) not in body, path
        assert "working/a.wav" not in body, path
        assert ":\\" not in body, path


def test_a_recording_with_no_transcript_is_a_404(
    client: Any, auth: dict[str, str], conn: sqlite3.Connection, meeting_id: int
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir, status) "
            "VALUES (?, '66666666-6666-4666-8666-666666666666', 'm/r2', 'RECORDED')",
            (meeting_id,),
        )
    response = client.get(
        "/asr/transcript/66666666-6666-4666-8666-666666666666", headers=auth
    )
    assert response.status_code == 404
    assert "asr transcribe" in response.json()["detail"]


def test_an_unknown_recording_is_a_404(
    client: Any, auth: dict[str, str], conn: sqlite3.Connection
) -> None:
    response = client.get(
        "/asr/transcript/77777777-7777-4777-8777-777777777777", headers=auth
    )
    assert response.status_code == 404


def test_a_specific_revision_can_be_requested(
    client: Any, auth: dict[str, str], stored_transcript: int
) -> None:
    payload = client.get(
        f"/asr/transcript/{RECORDING_UUID}?revision=1", headers=auth
    ).json()
    assert payload["transcript"]["revision"] == 1
    missing = client.get(f"/asr/transcript/{RECORDING_UUID}?revision=9", headers=auth)
    assert missing.status_code == 404


def test_a_revision_below_one_is_refused(
    client: Any, auth: dict[str, str], stored_transcript: int
) -> None:
    response = client.get(f"/asr/transcript/{RECORDING_UUID}?revision=0", headers=auth)
    assert response.status_code == 422


def test_the_revision_list_is_newest_first(
    client: Any, auth: dict[str, str], conn: sqlite3.Connection, stored_transcript: int
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO transcripts (recording_id, working_copy_id, revision, status, "
            "is_active) SELECT recording_id, working_copy_id, 2, 'COMPLETE', 0 FROM "
            "transcripts WHERE revision = 1"
        )
    payload = client.get(f"/asr/revisions/{RECORDING_UUID}", headers=auth).json()
    assert [row["revision"] for row in payload["revisions"]] == [2, 1]


def test_flagged_regions_carry_their_reason_codes(
    client: Any, auth: dict[str, str], stored_transcript: int
) -> None:
    payload = client.get(f"/asr/flagged/{RECORDING_UUID}", headers=auth).json()
    assert len(payload["flagged"]) == 1
    row = payload["flagged"][0]
    assert row["reason_codes"] == ["LOW_AVG_LOGPROB"]
    assert row["selected_for_pass2"] is True
    assert row["region_seq"] == 0


# ===========================================================================
# Structure
# ===========================================================================


def test_the_router_is_mounted_under_asr_and_token_protected() -> None:
    from mom_igd.api.asr_routes import asr_router

    assert asr_router.prefix == "/asr"
    assert asr_router.dependencies, "the router must carry the token dependency"


def test_the_service_is_created_once_per_process(client: Any, auth: dict[str, str]) -> None:
    """Two instances would each hold their own single-run lock."""
    client.get("/asr/status", headers=auth)
    first = client.app.state.asr_service
    client.get("/asr/status", headers=auth)
    assert client.app.state.asr_service is first


def test_the_openapi_schema_lists_the_asr_routes(client: Any) -> None:
    schema = client.get("/openapi.json").json()
    paths = set(schema["paths"])
    assert {"/asr/status", "/asr/transcribe", "/asr/cancel"} <= paths


def test_no_route_accepts_a_free_form_path_parameter() -> None:
    """A client must never be able to name a file."""
    from mom_igd.api.asr_routes import asr_router

    for route in asr_router.routes:
        for name in getattr(route, "param_convertors", {}):
            assert name in {"recording_uuid"}, name
        assert "path:" not in getattr(route, "path_format", ""), route
