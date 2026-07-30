"""Phase 2 integration: the recording service, database, manifest, job and audit.

Drives :class:`FakeAudioBackend` against a temporary data root and a real migrated
database. No microphone, no network, no real data directory.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from mom_igd.audio.backend import CaptureProfile, DeviceTransport, StreamError
from mom_igd.audio.devices import DeviceDiscoveryService, DeviceSelection
from mom_igd.audio.fake_backend import (
    ClippingSource,
    CounterSource,
    FakeAudioBackend,
    SilenceSource,
    SineSource,
)
from mom_igd.audio.manifest import read_manifest, verify_manifest
from mom_igd.audio.quality import LevelVerdict
from mom_igd.audio.service import (
    ACTIVE_LIFECYCLE_STATES,
    InvalidLifecycleTransition,
    RecordingLifecycle,
    RecordingService,
    RecordingServiceError,
    SingleRecordingLock,
)
from mom_igd.audio.session import WRITER_THREAD_NAME
from mom_igd.config import AppConfig
from mom_igd.db import initialize_database
from mom_igd.jobs.state_machine import JobState

BLOCK = 1_200


def _live_writer_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name == WRITER_THREAD_NAME]


@pytest.fixture
def audio_config(config: AppConfig, tmp_path: Path) -> AppConfig:
    """Config with a small chunk and a short queue, so tests stay fast."""
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
def ready_db(audio_config: AppConfig, paths):
    initialize_database(
        paths.database_path(audio_config.database.filename),
        busy_timeout_ms=audio_config.database.busy_timeout_ms,
        app_version=audio_config.app_version,
    )
    return paths.database_path(audio_config.database.filename)


@pytest.fixture
def backend() -> FakeAudioBackend:
    # 8 kHz mono keeps a 10 s chunk at 80 000 frames while staying cheap.
    return FakeAudioBackend(blocksize=BLOCK, source=CounterSource())


@pytest.fixture
def service(audio_config: AppConfig, paths, ready_db, backend: FakeAudioBackend):
    discovery = DeviceDiscoveryService(backend, endpoint_provider=lambda: [])
    created = RecordingService(audio_config, paths, backend=backend, discovery=discovery)
    yield created
    try:
        created.abandon("test teardown")
    except Exception:  # noqa: BLE001
        pass
    assert _live_writer_threads() == [], "the service leaked a writer thread"


@pytest.fixture
def meeting_id(ready_db, audio_config: AppConfig, paths) -> int:
    from mom_igd.db.connection import connect

    conn = connect(ready_db)
    try:
        cursor = conn.execute("INSERT INTO meetings (title) VALUES ('Rapat uji')")
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def _select_mono_device(service: RecordingService, backend: FakeAudioBackend) -> None:
    """Pick the 1-channel fake device so the profile is mono."""
    devices = service.list_devices()["devices"]
    usb = next(d for d in devices if d["max_input_channels"] == 1)
    service.select_device(usb["fingerprint"])


def _db(path: Path) -> sqlite3.Connection:
    from mom_igd.db.connection import connect

    return connect(path)


def _pump(service: RecordingService, backend: FakeAudioBackend, frames: int) -> None:
    """Deliver exactly ``frames`` frames, paced so the writer keeps up.

    Pumping is synchronous while the writer consumes asynchronously, and a burst
    delivered faster than real time legitimately overflows the bounded queue. So
    this sends small batches and waits for the writer between them, and it counts
    against the session's cumulative total so it stays correct across a resume.
    """
    assert frames % BLOCK == 0, f"pump target {frames} must be a multiple of {BLOCK}"
    stream = backend.streams[-1]
    already = service.status()["session"]["frames_written"]
    blocks = frames // BLOCK
    sent = 0
    deadline = time.monotonic() + 30.0
    while sent < blocks:
        batch = min(8, blocks - sent)
        stream.pump(batch)
        sent += batch
        target = already + sent * BLOCK
        while service.status()["session"]["frames_written"] < target:
            assert time.monotonic() < deadline, (
                f"writer wrote {service.status()['session']['frames_written']} of "
                f"{target} frames"
            )
            time.sleep(0.001)
    assert service.status()["session"]["queue"]["dropped_frames"] == 0


# ===========================================================================
# Device selection
# ===========================================================================


def test_device_list_reports_usable_and_rejected(service: RecordingService) -> None:
    payload = service.list_devices()
    assert len(payload["devices"]) == 3
    assert any("output only" in r["name"] for r in payload["rejected"])
    assert payload["verified_usb_available"] is False, "fake devices have no OS evidence"


def test_selecting_a_device_stores_the_fingerprint_not_the_index(
    service: RecordingService, ready_db: Path
) -> None:
    device = service.list_devices()["devices"][0]
    selected = service.select_device(device["fingerprint"])
    assert selected.fingerprint == device["fingerprint"]

    stored = json.loads(service._setting("selected_audio_device"))  # noqa: SLF001
    assert stored["fingerprint"] == device["fingerprint"]
    assert stored["name"] == device["name"]
    # An index may be kept as a hint, never as the identity.
    assert "last_known_index" in stored
    assert service.selected_selection().fingerprint == device["fingerprint"]


def test_selecting_an_unknown_device_is_refused(service: RecordingService) -> None:
    from mom_igd.audio.backend import DeviceNotFoundError

    with pytest.raises(DeviceNotFoundError, match="No capture device with fingerprint"):
        service.select_device("f" * 32)


def test_device_selection_is_audited(service: RecordingService, ready_db: Path) -> None:
    device = service.list_devices()["devices"][0]
    service.select_device(device["fingerprint"])
    conn = _db(ready_db)
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT action, detail_json FROM audit_events WHERE action='device.selected'"
            )
        ]
    finally:
        conn.close()
    assert len(rows) == 1
    assert json.loads(rows[0]["detail_json"])["fingerprint"] == device["fingerprint"]


def test_a_removed_device_is_not_silently_replaced(
    service: RecordingService, backend: FakeAudioBackend, meeting_id: int
) -> None:
    device = service.list_devices()["devices"][0]
    service.select_device(device["fingerprint"])
    backend.remove_device(device["index"])

    resolved, error = service.resolve_device()
    assert resolved is None
    assert "will not start on a different device" in error

    with pytest.raises(RecordingServiceError, match="Preflight failed"):
        service.start(meeting_id)
    assert backend.open_calls == 0, "no stream may be opened for a missing device"


def test_a_reindexed_device_is_still_found(
    service: RecordingService, backend: FakeAudioBackend
) -> None:
    device = service.list_devices()["devices"][0]
    service.select_device(device["fingerprint"])
    backend.reindex(offset=9)
    resolved, error = service.resolve_device()
    assert error is None
    assert resolved.fingerprint == device["fingerprint"]
    assert resolved.index == device["index"] + 9


# ===========================================================================
# Preflight and calibration
# ===========================================================================


def test_preflight_passes_with_a_selected_device(service: RecordingService) -> None:
    _select_mono_device(service, None)
    report = service.preflight(planned_minutes=60)
    assert report.can_start, [i.to_dict() for i in report.failures]
    keys = {i.key for i in report.items}
    assert {
        "device",
        "device_transport",
        "format",
        "data_directory",
        "disk_space",
        "storage_estimate",
        "database",
        "single_recording",
    } <= keys


def test_preflight_estimate_uses_decimal_megabytes(service: RecordingService) -> None:
    _select_mono_device(service, None)
    report = service.preflight(planned_minutes=60)
    # The fake USB device is 48 kHz mono -> 345.6 MB/h.
    assert report.profile["megabytes_per_hour"] == pytest.approx(345.6, abs=0.1)
    assert report.estimate["planned_minutes"] == 60
    assert report.estimate["fits"] is True


def test_preflight_fails_when_the_disk_is_too_small(
    audio_config: AppConfig, paths, ready_db, backend: FakeAudioBackend
) -> None:
    payload = audio_config.model_dump()
    payload["audio"] = {**audio_config.audio.model_dump(), "min_free_disk_gb": 1_000_000.0}
    strict = AppConfig.model_validate(payload)
    service = RecordingService(
        strict, paths, backend=backend, discovery=DeviceDiscoveryService(backend, endpoint_provider=lambda: [])
    )
    report = service.preflight()
    assert not report.can_start
    assert any(i.key == "disk_space" for i in report.failures)


def test_preflight_warns_about_an_unverified_transport(service: RecordingService) -> None:
    _select_mono_device(service, None)
    report = service.preflight()
    transport = next(i for i in report.items if i.key == "device_transport")
    assert transport.status.value == "WARN"
    assert "could not be verified" in transport.detail


def test_open_test_reports_frames_without_recording(
    service: RecordingService, backend: FakeAudioBackend
) -> None:
    _select_mono_device(service, None)
    backend.realtime = True
    backend.speed = 60.0
    result = service.open_test()
    assert result["ok"] is True
    assert result["frames"] > 0
    assert backend.open_streams == [], "the open test must close its stream"


def test_calibration_measures_level_and_stores_evidence(
    service: RecordingService, backend: FakeAudioBackend, ready_db: Path
) -> None:
    _select_mono_device(service, None)
    backend.source = SineSource(frequency_hz=440.0, level_dbfs=-18.0)
    backend.realtime = True
    backend.speed = 80.0

    result = service.calibrate(seconds=0.4)

    assert result.error is None
    assert result.frames > 0
    assert result.verdict is LevelVerdict.GOOD
    assert result.snapshot.peak_dbfs == pytest.approx(-18.0, abs=1.0)
    assert result.saved_to is None, "calibration audio must not be kept by default"

    evidence = json.loads(service._setting("last_calibration"))  # noqa: SLF001
    assert evidence["verdict"] == "GOOD"
    assert "audio" not in evidence
    conn = _db(ready_db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events WHERE action='calibration.completed'"
        ).fetchone()["n"] == 1
    finally:
        conn.close()


def test_calibration_detects_clipping(
    service: RecordingService, backend: FakeAudioBackend
) -> None:
    _select_mono_device(service, None)
    backend.source = ClippingSource(overdrive_db=6.0)
    backend.realtime = True
    backend.speed = 80.0
    result = service.calibrate(seconds=0.4)
    assert result.verdict is LevelVerdict.CLIPPING
    assert result.ok is False
    assert "Lower the input level" in result.verdict.advice


def test_calibration_detects_silence(
    service: RecordingService, backend: FakeAudioBackend
) -> None:
    _select_mono_device(service, None)
    backend.source = SilenceSource()
    backend.realtime = True
    backend.speed = 80.0
    result = service.calibrate(seconds=0.4)
    assert result.verdict is LevelVerdict.NO_SIGNAL
    assert result.snapshot.silence_percent == 100.0


def test_calibration_can_save_audio_only_when_asked(
    service: RecordingService, backend: FakeAudioBackend, tmp_path: Path
) -> None:
    _select_mono_device(service, None)
    backend.realtime = True
    backend.speed = 80.0
    target = tmp_path / "calib" / "clip.pcm"
    result = service.calibrate(seconds=0.3, save_to=target)
    assert result.saved_to == "clip.pcm"
    assert target.is_file()


# ===========================================================================
# Recording lifecycle
# ===========================================================================


def test_start_records_and_advances_the_job(
    service: RecordingService, backend: FakeAudioBackend, meeting_id: int, ready_db: Path
) -> None:
    _select_mono_device(service, None)
    status = service.start(meeting_id, planned_minutes=5)

    assert status["recording_active"] is True
    assert status["lifecycle"] == RecordingLifecycle.RECORDING.value
    assert backend.open_calls == 1

    conn = _db(ready_db)
    try:
        recording = conn.execute("SELECT * FROM recordings").fetchone()
        job = conn.execute("SELECT state FROM jobs").fetchone()
    finally:
        conn.close()
    assert recording["status"] == RecordingLifecycle.RECORDING.value
    assert recording["sample_rate_hz"] == 48_000
    assert recording["channels"] == 1
    assert recording["sample_format"] == "int16"
    assert recording["chunk_seconds"] == 10
    assert recording["device_fingerprint"]
    assert recording["relative_dir"].count("/") == 1
    assert job["state"] == JobState.RECORDING.value


def test_the_job_only_reaches_recording_after_the_stream_opens(
    service: RecordingService, backend: FakeAudioBackend, meeting_id: int, ready_db: Path
) -> None:
    _select_mono_device(service, None)
    backend.fail_on_open = StreamError("device busy")

    with pytest.raises(StreamError, match="device busy"):
        service.start(meeting_id)

    conn = _db(ready_db)
    try:
        job = conn.execute("SELECT state FROM jobs").fetchone()
        recording = conn.execute("SELECT status, last_error FROM recordings").fetchone()
    finally:
        conn.close()
    assert job is None or job["state"] != JobState.RECORDING.value
    assert recording["status"] == RecordingLifecycle.FAILED.value
    assert "device busy" in recording["last_error"]
    assert service._lock.read_holder() is None, "the lock must be released"  # noqa: SLF001


def test_double_start_does_not_open_a_second_stream(
    service: RecordingService, backend: FakeAudioBackend, meeting_id: int
) -> None:
    _select_mono_device(service, None)
    service.start(meeting_id)
    service.start(meeting_id)
    assert backend.open_calls == 1


def test_a_second_process_is_refused_by_the_lock(
    service: RecordingService, audio_config: AppConfig, paths, meeting_id: int
) -> None:
    _select_mono_device(service, None)
    service.start(meeting_id)

    other_backend = FakeAudioBackend(blocksize=BLOCK)
    other = RecordingService(
        audio_config,
        paths,
        backend=other_backend,
        discovery=DeviceDiscoveryService(other_backend, endpoint_provider=lambda: []),
    )
    with pytest.raises(RecordingServiceError, match="already in progress"):
        other._lock.acquire("second")  # noqa: SLF001
    report = other.preflight()
    assert any(i.key == "single_recording" for i in report.failures)
    assert other_backend.open_calls == 0


def test_stop_finalises_everything_and_marks_the_job_recorded(
    service: RecordingService, backend: FakeAudioBackend, meeting_id: int, ready_db: Path, paths
) -> None:
    _select_mono_device(service, None)
    service.start(meeting_id, planned_minutes=1)
    _pump(service, backend, 24_000)
    status = service.stop()

    assert status["lifecycle"] == RecordingLifecycle.RECORDED.value
    conn = _db(ready_db)
    try:
        recording = conn.execute("SELECT * FROM recordings").fetchone()
        chunks = list(conn.execute("SELECT * FROM recording_chunks ORDER BY seq"))
        job = conn.execute("SELECT state FROM jobs").fetchone()
    finally:
        conn.close()

    assert recording["status"] == RecordingLifecycle.RECORDED.value
    assert recording["manifest_status"] == "VERIFIED"
    assert recording["written_frames"] == 24_000
    assert recording["dropped_frames"] == 0
    assert recording["degraded"] == 0
    assert recording["chunk_count"] == len(chunks) == 1
    assert recording["manifest_sha256"]
    assert job["state"] == JobState.RECORDED.value

    directory = paths.recordings_dir / recording["relative_dir"]
    report = verify_manifest(directory)
    assert report.ok, report.problems
    assert chunks[0]["sha256"] == read_manifest(directory)[0][0].sha256


def test_stop_is_idempotent(
    service: RecordingService, backend: FakeAudioBackend, meeting_id: int
) -> None:
    _select_mono_device(service, None)
    service.start(meeting_id)
    _pump(service, backend, 4_800)
    first = service.stop()
    second = service.stop()
    third = service.stop()
    assert first["lifecycle"] == RecordingLifecycle.RECORDED.value
    assert second["recording_active"] is False
    assert third["recording_active"] is False


def test_stop_without_start_is_safe(service: RecordingService) -> None:
    status = service.stop()
    assert status["recording_active"] is False
    assert status["lifecycle"] == RecordingLifecycle.IDLE.value


def test_pause_and_resume_are_recorded_as_an_intentional_gap(
    service: RecordingService, backend: FakeAudioBackend, meeting_id: int, ready_db: Path, paths
) -> None:
    _select_mono_device(service, None)
    service.start(meeting_id)
    _pump(service, backend, 4_800)
    service.pause()
    assert service.status()["lifecycle"] == RecordingLifecycle.PAUSED.value
    service.resume()
    assert service.status()["lifecycle"] == RecordingLifecycle.RECORDING.value
    _pump(service, backend, 4_800)
    status = service.stop()

    conn = _db(ready_db)
    try:
        recording = conn.execute("SELECT pause_count, chunk_count FROM recordings").fetchone()
        actions = [
            r["action"]
            for r in conn.execute("SELECT action FROM audit_events ORDER BY id")
        ]
    finally:
        conn.close()
    assert recording["pause_count"] == 1
    assert recording["chunk_count"] == 2, "pause closes a chunk boundary"
    assert "recording.paused" in actions
    assert "recording.resumed" in actions

    conn = _db(ready_db)
    try:
        relative = conn.execute("SELECT relative_dir FROM recordings").fetchone()["relative_dir"]
    finally:
        conn.close()
    _, events, _ = read_manifest(paths.recordings_dir / relative)
    gaps = [e for e in events if e.get("type") == "gap" and e.get("reason") == "paused"]
    assert len(gaps) == 1
    assert gaps[0]["intentional"] is True


def test_invalid_lifecycle_transition_is_refused(
    service: RecordingService, backend: FakeAudioBackend, meeting_id: int
) -> None:
    _select_mono_device(service, None)
    with pytest.raises(RecordingServiceError, match="No recording is in progress"):
        service.pause()
    service.start(meeting_id)
    service.pause()
    with pytest.raises(InvalidLifecycleTransition) as excinfo:
        service._assert_transition(  # noqa: SLF001 - exercising the guard directly
            RecordingLifecycle.PAUSED, RecordingLifecycle.ARMED
        )
    assert "Allowed from PAUSED" in str(excinfo.value)
    assert "PAUSED -> ARMED" in str(excinfo.value)
    # A recording cannot be resurrected once it is terminal.
    for terminal in (RecordingLifecycle.RECORDED, RecordingLifecycle.CANCELLED):
        with pytest.raises(InvalidLifecycleTransition):
            service._assert_transition(terminal, RecordingLifecycle.RECORDING)  # noqa: SLF001


def test_abandon_leaves_the_partial_for_recovery(
    service: RecordingService, backend: FakeAudioBackend, meeting_id: int, ready_db: Path, paths
) -> None:
    _select_mono_device(service, None)
    service.start(meeting_id)
    _pump(service, backend, 6_000)
    status = service.abandon("device was unplugged")

    assert status["lifecycle"] == RecordingLifecycle.RECOVERABLE.value
    conn = _db(ready_db)
    try:
        recording = conn.execute("SELECT * FROM recordings").fetchone()
        job = conn.execute("SELECT state FROM jobs").fetchone()
    finally:
        conn.close()
    assert recording["status"] == RecordingLifecycle.RECOVERABLE.value
    assert "unplugged" in recording["last_error"]
    assert job["state"] == JobState.FAILED.value

    directory = paths.recordings_dir / recording["relative_dir"]
    assert list(directory.glob("*.pcm.part")), "partial preserved for recovery"

    outcome = service.recover_all()
    assert outcome["recovered_chunks"] == 1
    assert (directory / "chunk_000000.wav").is_file()

    conn = _db(ready_db)
    try:
        after = conn.execute("SELECT recovered_chunks FROM recordings").fetchone()
        actions = {r["action"] for r in conn.execute("SELECT action FROM audit_events")}
    finally:
        conn.close()
    assert after["recovered_chunks"] == 1
    assert "recovery.completed" in actions


def test_recovery_is_idempotent_through_the_service(
    service: RecordingService, backend: FakeAudioBackend, meeting_id: int
) -> None:
    _select_mono_device(service, None)
    service.start(meeting_id)
    _pump(service, backend, 3_600)
    service.abandon("crash")
    first = service.recover_all()
    second = service.recover_all()
    assert first["recovered_chunks"] == 1
    assert second["recovered_chunks"] == 0


# ===========================================================================
# Status, quality, verification
# ===========================================================================


def test_status_is_json_safe_and_leaks_no_path(
    service: RecordingService, backend: FakeAudioBackend, meeting_id: int, paths
) -> None:
    _select_mono_device(service, None)
    service.start(meeting_id)
    _pump(service, backend, 2_400)
    status = service.status()
    text = json.dumps(status)

    assert str(paths.root) not in text
    assert str(paths.recordings_dir) not in text
    assert ":\\" not in text
    assert status["capabilities"] == {
        "audio_capture": True,
        "transcript": False,
        "speaker_identification": False,
        "mom_generation": False,
        "export": False,
    }


def test_quality_snapshot_is_available_while_recording(
    service: RecordingService, backend: FakeAudioBackend, meeting_id: int
) -> None:
    _select_mono_device(service, None)
    backend.source = SineSource(level_dbfs=-20.0)
    service.start(meeting_id)
    _pump(service, backend, 4_800)
    quality = service.quality()
    assert quality["available"] is True
    assert quality["rolling"]["verdict"] in {"GOOD", "TOO_QUIET"}
    assert "channels" in quality["cumulative"]
    service.stop()
    assert service.quality()["available"] is False


def test_verify_reports_database_and_manifest_agreement(
    service: RecordingService, backend: FakeAudioBackend, meeting_id: int
) -> None:
    _select_mono_device(service, None)
    status = service.start(meeting_id)
    uuid_value = status["recording_uuid"]
    _pump(service, backend, 4_800)
    service.stop()

    report = service.verify(uuid_value)
    assert report["ok"] is True
    assert report["database_mismatches"] == []
    assert report["database_chunk_count"] == report["verified_chunks"] == 1


def test_verify_detects_tampered_audio(
    service: RecordingService, backend: FakeAudioBackend, meeting_id: int, paths, ready_db: Path
) -> None:
    _select_mono_device(service, None)
    status = service.start(meeting_id)
    _pump(service, backend, 4_800)
    service.stop()

    conn = _db(ready_db)
    try:
        relative = conn.execute("SELECT relative_dir FROM recordings").fetchone()["relative_dir"]
    finally:
        conn.close()
    victim = paths.recordings_dir / relative / "chunk_000000.wav"
    data = bytearray(victim.read_bytes())
    data[60] ^= 0xFF
    victim.write_bytes(bytes(data))

    report = service.verify(status["recording_uuid"])
    assert report["ok"] is False
    assert report["checksum_mismatches"] == ["chunk_000000.wav"]


def test_chunk_finalisation_is_audited(
    service: RecordingService, backend: FakeAudioBackend, meeting_id: int, ready_db: Path
) -> None:
    _select_mono_device(service, None)
    service.start(meeting_id)
    # A 10 s chunk at 48 kHz is 480 000 frames; stop() finalises the tail chunk, so
    # a short recording is enough to exercise the finalisation path.
    _pump(service, backend, 2_400)
    service.stop()
    conn = _db(ready_db)
    try:
        rows = [
            json.loads(r["detail_json"])
            for r in conn.execute(
                "SELECT detail_json FROM audit_events WHERE action='recording.chunk_finalized'"
            )
        ]
    finally:
        conn.close()
    assert rows, "each finalised chunk must be audited"
    assert all("sha256" in r and len(r["sha256"]) == 16 for r in rows)


def test_audit_trail_covers_the_whole_lifecycle(
    service: RecordingService, backend: FakeAudioBackend, meeting_id: int, ready_db: Path
) -> None:
    _select_mono_device(service, None)
    service.start(meeting_id)
    _pump(service, backend, 2_400)
    service.pause()
    service.resume()
    _pump(service, backend, 2_400)
    service.stop()

    conn = _db(ready_db)
    try:
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit_events ORDER BY id")]
        from mom_igd.audit import verify_chain

        ok, bad, reason = verify_chain(conn)
    finally:
        conn.close()
    for expected in (
        "device.selected",
        "preflight.passed",
        "recording.armed",
        "recording.started",
        "recording.paused",
        "recording.resumed",
        "recording.chunk_finalized",
        "recording.stopped",
    ):
        assert expected in actions, expected
    assert ok, f"audit chain broken at {bad}: {reason}"


# ===========================================================================
# Lock behaviour
# ===========================================================================


def test_lock_is_atomic_and_reports_its_holder(tmp_path: Path) -> None:
    lock = SingleRecordingLock(tmp_path / "recording.lock")
    lock.acquire("rec-1")
    holder = lock.read_holder()
    assert holder["recording_uuid"] == "rec-1"
    assert holder["pid"] > 0

    other = SingleRecordingLock(tmp_path / "recording.lock")
    with pytest.raises(RecordingServiceError, match="already in progress"):
        other.acquire("rec-2")

    lock.release()
    other.acquire("rec-2")
    assert other.read_holder()["recording_uuid"] == "rec-2"
    other.release()


def test_a_stale_lock_from_a_dead_process_is_cleared(tmp_path: Path) -> None:
    """A crash must not leave the application permanently unable to record."""
    path = tmp_path / "recording.lock"
    path.write_text(
        json.dumps({"pid": 999_999_999, "recording_uuid": "ghost"}), encoding="utf-8"
    )
    lock = SingleRecordingLock(path)
    lock.acquire("fresh")
    assert lock.read_holder()["recording_uuid"] == "fresh"
    lock.release()


def test_read_live_holder_distinguishes_live_from_stale(tmp_path: Path) -> None:
    """One public home for the staleness rule.

    Both the recording preflight and Phase 3 enrollment ask this question, and they
    must answer it identically -- duplicated staleness logic would eventually
    diverge. It also keeps enrollment from reaching across modules into a private
    helper.
    """
    import os

    path = tmp_path / "recording.lock"
    lock = SingleRecordingLock(path)

    assert lock.read_live_holder() is None, "no lock file at all"

    path.write_text(
        json.dumps({"pid": 999_999_999, "recording_uuid": "ghost"}), encoding="utf-8"
    )
    assert lock.read_holder() is not None, "the file is there..."
    assert lock.read_live_holder() is None, "...but its owner is gone"

    path.write_text(
        json.dumps({"pid": os.getpid(), "recording_uuid": "mine"}), encoding="utf-8"
    )
    live = lock.read_live_holder()
    assert live is not None and live["recording_uuid"] == "mine"

    path.write_text("{not json", encoding="utf-8")
    assert lock.read_live_holder() is None, "a malformed lock has no live owner"


def test_a_stale_lock_does_not_block_preflight(
    service: RecordingService, backend: FakeAudioBackend
) -> None:
    """Regression: preflight runs *before* acquire, so it must ignore a dead holder.

    `acquire()` clears a lock whose owning process is gone, but preflight happens
    first -- and it used to read the holder without that check. A lock left by a
    killed process therefore failed preflight forever, permanently preventing
    recording. That is the exact failure `_owner_alive` was written to avoid, defeated
    by the ordering. Found while wiring Phase 3 enrollment onto the same lock.
    """
    _select_mono_device(service, None)
    lock_path = service._lock.path  # noqa: SLF001
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"pid": 999_999_999, "recording_uuid": "ghost"}), encoding="utf-8"
    )

    report = service.preflight()
    single = next(i for i in report.items if i.key == "single_recording")
    assert single.status.value == "PASS", single.detail

    # And a real recording can therefore still start.
    status = service.start(meeting_title="Setelah lock basi")
    assert status["recording_active"] is True
    service.stop()


def test_a_live_lock_still_blocks_preflight(
    service: RecordingService, backend: FakeAudioBackend
) -> None:
    """The stale-lock fix must not weaken the guard against a genuine holder."""
    _select_mono_device(service, None)
    lock_path = service._lock.path  # noqa: SLF001
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    import os

    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "recording_uuid": "live-holder"}),
        encoding="utf-8",
    )
    report = service.preflight()
    single = next(i for i in report.items if i.key == "single_recording")
    assert single.status.value == "FAIL"
    assert "live-holder" in single.detail
    lock_path.unlink()


def test_active_lifecycle_states_match_the_database_index() -> None:
    """The in-flight set must equal the states the partial index treats as active."""
    from mom_igd.db.migrator import discover_migrations

    sql = discover_migrations()[1].sql
    marker = sql.split("ux_recordings_single_active", 1)[1]
    clause = marker.split("WHERE", 1)[1].split(";", 1)[0]
    in_sql = {token.strip().strip("'") for token in clause.split("(", 1)[1].split(")", 1)[0].split(",")}
    assert in_sql == set(ACTIVE_LIFECYCLE_STATES)
