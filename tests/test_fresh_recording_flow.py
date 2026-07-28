"""The operator flow, end to end, from a data root that has never been used.

**Why this file exists separately.** Every other Phase 2 test starts from a
fixture that has already inserted a meeting row. That hid a real defect: the shell
had no way to obtain a ``meeting_id``, because Meeting setup is a later phase, so
on a genuinely fresh install Start could not succeed at all. These tests walk the
exact sequence the recording panel walks -- devices, select, calibrate, preflight,
start, pause, resume, stop, verify -- against a freshly migrated database, through
the HTTP API rather than the service object, so the wiring is covered too.

No microphone: the app under test has a ``FakeAudioBackend`` on ``app.state``.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest

from mom_igd.audio.devices import DeviceDiscoveryService
from mom_igd.audio.fake_backend import CounterSource, FakeAudioBackend, SineSource
from mom_igd.audio.manifest import verify_manifest
from mom_igd.audio.service import RecordingService
from mom_igd.audio.session import WRITER_THREAD_NAME
from mom_igd.config import AppConfig
from mom_igd.db import initialize_database
from mom_igd.security import SESSION_TOKEN_HEADER, SessionToken

BLOCK = 1_200


@pytest.fixture
def fresh_config(config: AppConfig) -> AppConfig:
    """Small chunks and no disk floor, so the flow completes quickly."""
    payload = config.model_dump()
    payload["audio"] = {
        **config.audio.model_dump(),
        "chunk_seconds": 10,
        "queue_seconds": 5.0,
        "min_free_disk_gb": 0.0,
        "low_disk_abort_gb": 0.0,
        "calibration_seconds": 10,
    }
    return AppConfig.model_validate(payload)


@pytest.fixture
def fresh_client(fresh_config: AppConfig, paths, token: SessionToken):
    """A migrated but otherwise empty database: no meeting, no recording."""
    from starlette.testclient import TestClient

    from mom_igd.api.app import create_app

    initialize_database(
        paths.database_path(fresh_config.database.filename),
        busy_timeout_ms=fresh_config.database.busy_timeout_ms,
        app_version=fresh_config.app_version,
    )
    backend = FakeAudioBackend(blocksize=BLOCK, source=CounterSource())
    app = create_app(fresh_config, session_token=token, paths=paths)
    app.state.recording_service = RecordingService(
        fresh_config,
        paths,
        backend=backend,
        discovery=DeviceDiscoveryService(backend, endpoint_provider=lambda: []),
    )
    app.state.fake_backend = backend
    headers = {SESSION_TOKEN_HEADER: token.value}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers.update(headers)
        try:
            yield client, app
        finally:
            try:
                app.state.recording_service.abandon("test teardown")
            except Exception:  # noqa: BLE001
                pass
    live = [t.name for t in threading.enumerate() if t.name == WRITER_THREAD_NAME]
    assert live == [], "a writer thread leaked out of the fresh-flow test"


def _meeting_rows(paths, config: AppConfig) -> list[tuple[int, str, str]]:
    from mom_igd.db.connection import connect

    conn = connect(paths.database_path(config.database.filename))
    try:
        return [
            (int(r["id"]), str(r["title"]), str(r["uuid"]))
            for r in conn.execute("SELECT id, title, uuid FROM meetings ORDER BY id")
        ]
    finally:
        conn.close()


def _pump(client, app, frames: int) -> None:
    """Deliver ``frames`` frames, paced so the bounded queue never overflows."""
    assert frames % BLOCK == 0
    stream = app.state.fake_backend.streams[-1]
    already = client.get("/audio/recordings/status").json()["session"]["frames_written"]
    blocks, sent = frames // BLOCK, 0
    deadline = time.monotonic() + 30.0
    while sent < blocks:
        batch = min(8, blocks - sent)
        stream.pump(batch)
        sent += batch
        target = already + sent * BLOCK
        while (
            client.get("/audio/recordings/status").json()["session"]["frames_written"]
            < target
        ):
            assert time.monotonic() < deadline, "writer fell behind the pump"
            time.sleep(0.001)


# ===========================================================================
# The defect this file was written for
# ===========================================================================


def test_start_needs_no_meeting_id_on_a_fresh_database(fresh_client) -> None:
    """The whole point: Start works with no meeting row and no id from the user."""
    client, _app = fresh_client
    device = client.get("/audio/devices").json()["devices"][0]
    client.post("/audio/devices/select", json={"fingerprint": device["fingerprint"]})

    response = client.post("/audio/recordings/start", json={"meeting_title": ""})
    assert response.status_code == 200, response.text
    assert response.json()["recording_active"] is True


def test_a_nonexistent_meeting_id_is_refused_with_an_actionable_message(
    fresh_client,
) -> None:
    """The old UI sent meeting_id=1. It must fail loudly, and say what to do."""
    client, _app = fresh_client
    device = client.get("/audio/devices").json()["devices"][0]
    client.post("/audio/devices/select", json={"fingerprint": device["fingerprint"]})

    response = client.post("/audio/recordings/start", json={"meeting_id": 1})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "No meeting with id=1" in detail
    assert "without a meeting_id" in detail


def test_a_blank_title_becomes_a_timestamp_never_an_empty_title(
    fresh_client, fresh_config: AppConfig, paths
) -> None:
    client, _app = fresh_client
    device = client.get("/audio/devices").json()["devices"][0]
    client.post("/audio/devices/select", json={"fingerprint": device["fingerprint"]})
    client.post("/audio/recordings/start", json={"meeting_title": "   "})

    rows = _meeting_rows(paths, fresh_config)
    assert len(rows) == 1
    _id, title, meeting_uuid = rows[0]
    assert title.strip(), "the meetings CHECK constraint forbids a blank title"
    assert title.startswith("Rapat ")
    assert meeting_uuid, "a draft meeting must still get a UUID for the on-disk layout"


def test_the_meeting_title_never_reaches_the_filesystem(
    fresh_client, fresh_config: AppConfig, paths
) -> None:
    """A title may hold a participant's name; a path must never repeat it."""
    client, _app = fresh_client
    device = client.get("/audio/devices").json()["devices"][0]
    client.post("/audio/devices/select", json={"fingerprint": device["fingerprint"]})
    client.post(
        "/audio/recordings/start",
        json={"meeting_title": "Rapat dengan Budi Santoso"},
    )
    client.post("/audio/recordings/stop")

    rows = _meeting_rows(paths, fresh_config)
    assert rows[0][1] == "Rapat dengan Budi Santoso"
    for path in paths.recordings_dir.rglob("*"):
        assert "Budi" not in str(path), f"participant name leaked into {path}"
        assert "Santoso" not in str(path), f"participant name leaked into {path}"


def test_a_double_clicked_start_creates_one_meeting_and_one_recording(
    fresh_client, fresh_config: AppConfig, paths
) -> None:
    client, _app = fresh_client
    device = client.get("/audio/devices").json()["devices"][0]
    client.post("/audio/devices/select", json={"fingerprint": device["fingerprint"]})

    first = client.post("/audio/recordings/start", json={"meeting_title": "Sekali saja"})
    second = client.post("/audio/recordings/start", json={"meeting_title": "Sekali saja"})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["recording_uuid"] == second.json()["recording_uuid"]
    assert len(_meeting_rows(paths, fresh_config)) == 1

    from mom_igd.db.connection import connect

    conn = connect(paths.database_path(fresh_config.database.filename))
    try:
        assert conn.execute("SELECT count(*) FROM recordings").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    finally:
        conn.close()


def test_concurrent_starts_still_produce_one_recording(fresh_client) -> None:
    """Two threads hitting Start together must not both arm a capture."""
    client, _app = fresh_client
    device = client.get("/audio/devices").json()["devices"][0]
    client.post("/audio/devices/select", json={"fingerprint": device["fingerprint"]})

    results: list[int] = []
    barrier = threading.Barrier(2)

    def _start() -> None:
        barrier.wait()
        results.append(
            client.post("/audio/recordings/start", json={"meeting_title": "Balapan"}).status_code
        )

    threads = [threading.Thread(target=_start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(results) == [200, 200], results
    status = client.get("/audio/recordings/status").json()
    assert status["recording_active"] is True


# ===========================================================================
# The full panel sequence
# ===========================================================================


def test_the_whole_panel_flow_from_an_unused_data_root(
    fresh_client, fresh_config: AppConfig, paths
) -> None:
    """devices -> select -> calibrate -> preflight -> start -> pause -> resume
    -> stop -> verify, exactly as the recording panel drives it."""
    client, app = fresh_client

    # 1. The panel opens and lists devices. No stream is opened by listing.
    listing = client.get("/audio/devices").json()
    assert listing["devices"], "the fake backend must expose a usable device"
    assert app.state.fake_backend.open_calls == 0
    mono = next(d for d in listing["devices"] if d["max_input_channels"] == 1)

    # 2. The operator picks one explicitly.
    selected = client.post(
        "/audio/devices/select", json={"fingerprint": mono["fingerprint"]}
    )
    assert selected.status_code == 200
    assert selected.json()["selected"]["fingerprint"] == mono["fingerprint"]

    # 3. Calibration. This one *does* open the microphone -- deliberately.
    # Calibration waits on wall-clock audio, so the fake stream must deliver it
    # on its own rather than waiting to be pumped.
    backend = app.state.fake_backend
    backend.source = SineSource(frequency_hz=440.0, level_dbfs=-18.0)
    backend.realtime = True
    backend.speed = 80.0
    calibration = client.post("/audio/calibrate", json={"seconds": 0.4})
    assert calibration.status_code == 200, calibration.text
    assert calibration.json()["verdict"] == "GOOD", calibration.json()
    assert calibration.json()["audio_saved"] is False, "calibration audio must not be kept"
    # The API answers with a boolean, never a filesystem path.
    assert "saved_to" not in calibration.json()

    # Back to manual pumping, and to the byte-exact source, for the recording.
    backend.realtime = False
    backend.source = CounterSource()

    # 4. Preflight, with a planned duration, before anything is recorded.
    preflight = client.get("/audio/preflight", params={"planned_minutes": 30}).json()
    assert preflight["can_start"] is True, preflight

    # 5. Start. No meeting_id: the backend creates the draft meeting.
    started = client.post(
        "/audio/recordings/start",
        json={"meeting_title": "Rapat integrasi Phase 2", "planned_minutes": 30},
    ).json()
    recording_uuid = started["recording_uuid"]
    assert started["recording_active"] is True

    # 6. Audio flows and a chunk is finalised.
    _pump(client, app, 12 * BLOCK)

    # 7. Pause closes the open chunk and records an intentional gap.
    paused = client.post("/audio/recordings/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["lifecycle"] == "PAUSED"

    # 8. Resume opens a new chunk.
    resumed = client.post("/audio/recordings/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["lifecycle"] == "RECORDING"
    _pump(client, app, 8 * BLOCK)

    # 9. Stop finalises everything.
    stopped = client.post("/audio/recordings/stop").json()
    assert stopped["recording_active"] is False
    assert stopped["lifecycle"] == "RECORDED"

    # 10. Verify: the manifest and every checksum agree with the bytes on disk.
    verified = client.get(f"/audio/recordings/{recording_uuid}/verify")
    assert verified.status_code == 200, verified.text
    report = verified.json()
    assert report["ok"] is True, report
    assert report["verified_chunks"] >= 1
    assert report["verified_chunks"] == report["chunk_count"]
    assert report["checksum_mismatches"] == []
    assert report["missing_files"] == []
    assert report["header_mismatches"] == []
    # Pause/resume left exactly one intentional gap, recorded rather than filled.
    assert len(report["gaps"]) == 1, report["gaps"]
    assert report["gaps"][0].get("intentional") is True, report["gaps"]

    # And the same conclusion reached independently of the API. The relative path
    # comes from the database, because no API response exposes a path at all.
    from mom_igd.db.connection import connect

    conn = connect(paths.database_path(fresh_config.database.filename))
    try:
        relative_dir = conn.execute(
            "SELECT relative_dir FROM recordings WHERE recording_uuid = ?",
            (recording_uuid,),
        ).fetchone()["relative_dir"]
    finally:
        conn.close()
    directory = paths.recordings_dir / str(relative_dir)
    independent = verify_manifest(directory)
    assert independent.ok is True
    assert independent.verified_chunks == report["verified_chunks"]
    # No partial or temporary file survives a clean stop.
    assert list(directory.glob("*.part")) == []
    assert list(directory.glob("*.tmp")) == []

    # The recording is attached to the draft meeting, and nothing is orphaned.
    rows = _meeting_rows(paths, fresh_config)
    assert len(rows) == 1
    assert rows[0][1] == "Rapat integrasi Phase 2"

    conn = connect(paths.database_path(fresh_config.database.filename))
    try:
        orphans = conn.execute(
            "SELECT count(*) FROM recordings r "
            "LEFT JOIN meetings m ON m.id = r.meeting_id WHERE m.id IS NULL"
        ).fetchone()[0]
        assert orphans == 0
        # The draft meeting was audited, in the MEETING category.
        audited = conn.execute(
            "SELECT count(*) FROM audit_events "
            "WHERE category = 'MEETING' AND action = 'meeting.draft_created'"
        ).fetchone()[0]
        assert audited == 1
    finally:
        conn.close()


def test_shutdown_finalises_an_active_recording_and_leaks_no_thread(
    fresh_config: AppConfig, paths, token: SessionToken
) -> None:
    """Closing the window mid-recording must finalise, not abandon.

    Whatever reached the writer is already on disk; stopping cleanly turns the open
    partial into a verified chunk instead of leaving work for `audio recover`. This
    exercises the real lifespan shutdown hook, not the service directly.
    """
    from starlette.testclient import TestClient

    from mom_igd.api.app import create_app

    initialize_database(
        paths.database_path(fresh_config.database.filename),
        busy_timeout_ms=fresh_config.database.busy_timeout_ms,
        app_version=fresh_config.app_version,
    )
    backend = FakeAudioBackend(blocksize=BLOCK, source=CounterSource())
    app = create_app(fresh_config, session_token=token, paths=paths)
    app.state.recording_service = RecordingService(
        fresh_config,
        paths,
        backend=backend,
        discovery=DeviceDiscoveryService(backend, endpoint_provider=lambda: []),
    )
    app.state.fake_backend = backend

    recording_uuid = None
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers.update({SESSION_TOKEN_HEADER: token.value})
        device = client.get("/audio/devices").json()["devices"][0]
        client.post("/audio/devices/select", json={"fingerprint": device["fingerprint"]})
        started = client.post(
            "/audio/recordings/start", json={"meeting_title": "Ditutup mendadak"}
        ).json()
        recording_uuid = started["recording_uuid"]
        assert started["recording_active"] is True
        _pump(client, app, 12 * BLOCK)
        # Leaving the context manager triggers lifespan shutdown with the recording
        # still running -- exactly what closing the desktop window does.

    # The writer thread is gone.
    live = [t.name for t in threading.enumerate() if t.name == WRITER_THREAD_NAME]
    assert live == [], f"shutdown leaked a writer thread: {live}"

    # The recording was finalised, not left for recovery.
    from mom_igd.db.connection import connect

    conn = connect(paths.database_path(fresh_config.database.filename))
    try:
        row = conn.execute(
            "SELECT status, relative_dir, chunk_count FROM recordings "
            "WHERE recording_uuid = ?",
            (recording_uuid,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["status"] == "RECORDED", f"shutdown left status {row['status']}"
    assert row["chunk_count"] >= 1

    directory = paths.recordings_dir / str(row["relative_dir"])
    report = verify_manifest(directory)
    assert report.ok is True, report.problems
    assert report.verified_chunks == row["chunk_count"]
    assert list(directory.glob("*.part")) == [], "a partial survived a clean shutdown"


def test_the_panel_sends_no_meeting_id_and_asks_for_no_database_id() -> None:
    """Guard the UI itself: the old prompt must not come back."""
    from mom_igd.api.app import WEB_DIR

    script = (Path(WEB_DIR) / "app.js").read_text(encoding="utf-8")
    html = (Path(WEB_DIR) / "index.html").read_text(encoding="utf-8")

    assert "meeting_title" in script
    assert "meeting_id" not in script, "the panel must not send an internal database id"
    assert "window.prompt" not in script, "the operator must not be prompted for an id"
    assert 'id="meeting-title"' in html


def test_the_panel_guards_the_transport_controls() -> None:
    """Lock in four behaviours that fail silently if someone edits them out.

    None of these is load-bearing for correctness -- the service refuses a bad
    request either way -- but each one was a real rough edge found in review, and a
    regression would only show up as a confusing UI.
    """
    from mom_igd.api.app import WEB_DIR

    script = (Path(WEB_DIR) / "app.js").read_text(encoding="utf-8")

    # 1. Start cannot be double-clicked through the preflight round trip.
    assert "startInFlight" in script
    assert "if (el.start.disabled || startInFlight) return;" in script

    # 2. Start also respects the last preflight verdict, not just "not recording".
    assert "el.start.disabled = active || !preflightOk;" in script

    # 3. The meeting title is locked during capture and freed again by the status
    #    render -- which is why stop() must report recording_active=false.
    assert "el.meetingTitle.disabled = active;" in script

    # 4. Stop asks first, and the poll timer is cleared when the window goes away.
    assert "window.confirm(" in script
    assert "stopPolling" in script
    assert "clearTimeout(pollTimer)" in script


def test_the_panel_takes_its_poll_rate_from_the_service() -> None:
    """A hardcoded interval silently ignores audio.status_poll_hz."""
    from mom_igd.api.app import WEB_DIR

    script = (Path(WEB_DIR) / "app.js").read_text(encoding="utf-8")
    assert "adoptPollRate(envelope.data.status_poll_hz)" in script
    assert "setTimeout(poll, pollMs)" in script


def test_the_status_payload_advertises_the_poll_rate_the_ui_needs(fresh_client) -> None:
    """The UI can only adopt the configured rate if the service publishes it."""
    client, _app = fresh_client
    payload = client.get("/audio/recordings/status").json()
    assert isinstance(payload["status_poll_hz"], (int, float))
    assert 1.0 <= payload["status_poll_hz"] <= 4.0


def test_no_api_response_leaks_a_filesystem_path(fresh_client) -> None:
    """A path in a response would hand the page something it must never have."""
    client, app = fresh_client
    device = client.get("/audio/devices").json()["devices"][0]
    client.post("/audio/devices/select", json={"fingerprint": device["fingerprint"]})
    started = client.post("/audio/recordings/start", json={"meeting_title": "Jalur"})
    stopped = client.post("/audio/recordings/stop")

    for label, response in (
        ("devices", client.get("/audio/devices")),
        ("preflight", client.get("/audio/preflight")),
        ("status", client.get("/audio/recordings/status")),
        ("recovery", client.get("/audio/recovery/pending")),
        ("start", started),
        ("stop", stopped),
    ):
        body = response.text
        assert response.status_code == 200, f"{label}: {body[:200]}"
        # No drive letter, in either JSON-escaped or raw form.
        assert not re.search(r"[A-Za-z]:(\\\\|\\|/)", body), f"{label} leaked a path"
        assert "relative_dir" not in body, f"{label} leaked a relative path"
        assert str(app.state.paths.root) not in body, f"{label} leaked the data root"
