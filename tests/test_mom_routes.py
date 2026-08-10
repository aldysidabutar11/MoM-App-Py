"""The minutes endpoints. Token-protected, loopback-only, and they load no model.

Every test here goes through the real FastAPI app. The pipeline is not exercised -- that is
`test_mom_pipeline.py`'s job -- but the *refusals* are, because a refusal is what the
operator actually meets when something is not ready, and a 500 where a 409 belongs sends
them looking for a crash instead of a missing model.
"""

from __future__ import annotations

import sqlite3

import pytest

from mom_igd.mom import store

UUID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def minute(conn: sqlite3.Connection, meeting_id: int) -> int:
    """A completed transcript with one DRAFT minute on it."""
    conn.execute(
        "INSERT INTO recordings (id, meeting_id, recording_uuid, relative_dir, status, "
        "started_at, duration_ms) VALUES (1, ?, ?, 'rec/1', 'RECORDED', "
        "'2026-08-09T09:00:00Z', 32000)",
        (meeting_id, UUID),
    )
    conn.execute(
        "INSERT INTO audio_working_copies (id, recording_id, relative_path, sha256, "
        "size_bytes, frames, duration_ms) VALUES (1, 1, 'w.wav', ?, 10, 10, 32000)",
        ("a" * 64,),
    )
    conn.execute(
        "INSERT INTO transcripts (id, recording_id, working_copy_id, revision, status, "
        "is_active, language, segment_count, word_count) "
        "VALUES (1, 1, 1, 1, 'COMPLETE', 1, 'id', 2, 20)"
    )
    minute_id = store.create_minute(
        conn, transcript_id=1, meeting_id=meeting_id, job_id=None
    )
    store.update_minute(
        conn,
        minute_id,
        title="Rapat Koordinasi",
        summary_json='["Go-live ditunda ke 5 September."]',
        transcript_ms=32000,
        covered_ms=32000,
    )
    store.save_items(
        conn,
        minute_id=minute_id,
        items=[
            {"kind": "DECISION", "text": "Go-live ditunda.", "quote": "menunda go-live",
             "segment_ids": [1], "verification": "VERIFIED", "verification_notes": [],
             "start_ms": 1000, "end_ms": 5000},
            {"kind": "ISSUE", "text": "Anggaran belum jelas.", "quote": "anggaran belum",
             "segment_ids": [], "verification": "UNVERIFIED",
             "verification_notes": ["QUOTE_NOT_FOUND"]},
        ],
    )
    store.activate_minute(conn, minute_id=minute_id)
    conn.commit()
    return minute_id


def _headers(token) -> dict[str, str]:
    return {"X-MoM-Session-Token": token.value}


# ===========================================================================
# Authentication
# ===========================================================================


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/mom/status"),
        ("get", "/mom/transcripts"),
        ("get", f"/mom/minute/{UUID}"),
        ("get", f"/mom/revisions/{UUID}"),
        ("post", "/mom/generate"),
        ("post", "/mom/export"),
        ("post", "/mom/cancel"),
    ],
)
def test_every_endpoint_requires_the_session_token(client, conn, method, path) -> None:
    response = (
        client.get(path) if method == "get" else client.post(path, json={})
    )
    assert response.status_code == 401, path


# ===========================================================================
# Reads
# ===========================================================================


def test_status_reports_readiness_without_loading_a_model(client, token, conn) -> None:
    response = client.get("/mom/status", headers=_headers(token))
    assert response.status_code == 200
    body = response.json()
    assert body["model_ready"] is False, "no model is provisioned in a test data root"
    assert body["running"] is False


def test_a_minute_comes_back_with_its_items_and_evidence(client, token, minute) -> None:
    response = client.get(f"/mom/minute/{UUID}", headers=_headers(token))
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Rapat Koordinasi"
    assert body["summary"] == ["Go-live ditunda ke 5 September."]
    assert len(body["items"]) == 2
    assert body["items"][0]["quote"] == "menunda go-live"
    assert body["items"][1]["verification"] == "UNVERIFIED"


def test_a_recording_with_no_minute_is_404_not_500(client, token, conn) -> None:
    other = "33333333-3333-4333-8333-333333333333"
    response = client.get(f"/mom/minute/{other}", headers=_headers(token))
    assert response.status_code == 404


def test_a_malformed_uuid_is_422_not_404(client, token) -> None:
    """422 says "you sent nonsense"; 404 would say "it does not exist", which is different."""
    response = client.get("/mom/minute/not-a-uuid", headers=_headers(token))
    assert response.status_code == 422


def test_revisions_lists_newest_first(client, token, minute) -> None:
    response = client.get(f"/mom/revisions/{UUID}", headers=_headers(token))
    assert response.status_code == 200
    revisions = response.json()["revisions"]
    assert len(revisions) == 1
    assert revisions[0]["revision"] == 1
    assert revisions[0]["is_active"] == 1


def test_the_transcript_list_explains_why_a_row_is_not_eligible(client, token, minute) -> None:
    response = client.get("/mom/transcripts", headers=_headers(token))
    assert response.status_code == 200
    [row] = response.json()["transcripts"]
    assert row["recording_uuid"] == UUID
    assert row["eligible"] is False
    assert "model" in row["reason"], row["reason"]


# ===========================================================================
# Refusals
# ===========================================================================


def test_generating_without_a_model_is_409_model_unavailable(client, token, conn) -> None:
    """409, not 503: the server is fine and the fix is an operator action."""
    response = client.post(
        "/mom/generate", json={"recording_uuid": UUID}, headers=_headers(token)
    )
    assert response.status_code == 409
    assert "MODEL_UNAVAILABLE" in response.json()["detail"]


def test_cancelling_when_nothing_runs_is_409(client, token, conn) -> None:
    response = client.post("/mom/cancel", json={}, headers=_headers(token))
    assert response.status_code == 409


def test_an_unknown_export_format_is_rejected_by_the_schema(client, token, minute) -> None:
    """A closed set, validated before any code runs: an export path is a way to write a file."""
    response = client.post(
        "/mom/export",
        json={"recording_uuid": UUID, "export_format": "pdf"},
        headers=_headers(token),
    )
    assert response.status_code == 422


# ===========================================================================
# Export
# ===========================================================================


def test_exporting_writes_a_file_and_returns_no_filesystem_path(
    client, token, minute, paths
) -> None:
    response = client.post(
        "/mom/export",
        json={"recording_uuid": UUID, "export_format": "docx"},
        headers=_headers(token),
    )
    assert response.status_code == 200
    body = response.json()
    assert "path" not in body, (
        "an API that returns a path is the beginning of one that accepts one"
    )
    assert body["relative_path"].endswith(".docx")
    assert body["included_unverified"] is True
    assert (paths.exports_dir / body["relative_path"]).is_file()


@pytest.mark.parametrize(
    "fmt,media",
    [
        ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("html", "text/html"),
        ("markdown", "text/markdown"),
        ("txt", "text/plain"),
    ],
)
def test_download_streams_the_bytes_with_the_right_type(client, token, minute, fmt, media) -> None:
    response = client.get(
        f"/mom/download/{UUID}", params={"format": fmt}, headers=_headers(token)
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(media)
    assert response.headers["x-mom-included-unverified"] == "1"
    assert len(response.headers["x-mom-sha256"]) == 64
    assert response.content


def test_a_download_carries_the_draft_banner(client, token, minute) -> None:
    response = client.get(
        f"/mom/download/{UUID}", params={"format": "txt"}, headers=_headers(token)
    )
    assert "DRAF OTOMATIS" in response.text


def test_hiding_unverified_points_is_recorded_on_the_export(client, token, minute) -> None:
    response = client.post(
        "/mom/export",
        json={"recording_uuid": UUID, "export_format": "txt", "include_unverified": False},
        headers=_headers(token),
    )
    assert response.status_code == 200
    assert response.json()["included_unverified"] is False


def test_re_exporting_the_same_path_replaces_the_row_rather_than_duplicating(
    client, token, minute, conn
) -> None:
    for _ in range(3):
        assert (
            client.post(
                "/mom/export",
                json={"recording_uuid": UUID, "export_format": "docx"},
                headers=_headers(token),
            ).status_code
            == 200
        )
    fresh = sqlite3.connect(conn.execute("PRAGMA database_list").fetchone()[2])
    fresh.row_factory = sqlite3.Row
    try:
        rows = list(fresh.execute("SELECT * FROM minute_exports"))
    finally:
        fresh.close()
    assert len(rows) == 1, "two rows describing one path, with different hashes, is a lie"
