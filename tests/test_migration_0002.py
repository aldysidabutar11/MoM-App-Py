"""Migration 0002 (audio capture): fresh install, upgrade, rollback, invariants.

0002 rebuilds ``recordings`` and ``recording_chunks`` because SQLite cannot alter
a CHECK constraint in place, and Phase 2 replaces the Phase 1 status vocabulary
with the recording lifecycle. A rebuild that loses rows is a data-loss bug, so the
upgrade path is tested with rows already present rather than only on an empty
database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mom_igd.db.connection import connect
from mom_igd.db.migrator import (
    MigrationError,
    apply_migrations,
    current_schema_version,
    discover_migrations,
    migration_status,
    verify_applied_checksums,
)
from mom_igd.version import SCHEMA_VERSION_HEAD

PHASE_1_AND_2_TABLES = {
    "schema_migrations",
    "app_settings",
    "participants",
    "meetings",
    "recordings",
    "recording_chunks",
    "jobs",
    "job_stages",
    "audit_events",
}

# Phase 3 (0003) adds exactly these four. Applying the whole migration set now
# reaches schema 3, so the head assertions below include them.
PHASE_3_TABLES = {
    "meeting_participants",
    "consent_events",
    "enrollment_sessions",
    "voiceprints",
}

# Phase 4 adds the transcription evidence chain (migration 0005).
PHASE_4_TABLES = {
    "audio_working_copies",
    "vad_runs",
    "speech_regions",
    "transcripts",
    "transcript_segments",
    "transcript_words",
}

HEAD_TABLES = PHASE_1_AND_2_TABLES | PHASE_3_TABLES | PHASE_4_TABLES

MIGRATION_0001_SHA256 = "f1426fa94b8ae90e4c0b646c0f132ac4a483525165675c947649cad124e89796"
MIGRATION_0002_SHA256 = "8d42086530a4560d28ca5cfd2707b5402c1b2872fea02a49ce3768106f570ded"
MIGRATION_0003_SHA256 = "fb3220d96d9b9a711189ca2a3d275e0bb74e6d0043e86db17d122d3cf9079fc1"


@pytest.fixture
def migrations():
    return discover_migrations()


@pytest.fixture
def db(paths, config) -> sqlite3.Connection:
    connection = connect(
        paths.database_path("t.db"), busy_timeout_ms=config.database.busy_timeout_ms
    )
    yield connection
    connection.close()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


# ---------------------------------------------------------------- discovery


def test_the_migrations_ship_in_order_up_to_the_head(migrations) -> None:
    """Every version present, contiguous, and agreeing with the declared head.

    Asserted as a property rather than a fixed count: a later phase adds a migration,
    and the thing that must stay true is that the sequence has no gap and that
    `SCHEMA_VERSION_HEAD` names the last one.
    """
    versions = [m.version for m in migrations]
    assert versions == list(range(1, len(versions) + 1))
    assert versions[-1] == SCHEMA_VERSION_HEAD
    assert migrations[1].name == "audio_capture"
    assert migrations[2].name == "participants_consent_voiceprints"
    assert migrations[3].name == "meeting_participant_capacity"
    assert migrations[4].name == "offline_asr"


def test_migration_0001_is_immutable(migrations) -> None:
    """A later phase must extend the schema, never edit an applied migration."""
    assert migrations[0].checksum == MIGRATION_0001_SHA256


def test_migration_0002_is_immutable(migrations) -> None:
    """Phase 3 must not edit the applied Phase 2 migration either."""
    assert migrations[1].checksum == MIGRATION_0002_SHA256


def test_migration_0003_is_immutable(migrations) -> None:
    """The corrective pass added 0004; it must not have touched 0003.

    0003 creates the consent and voiceprint tables. Editing it after it has been
    applied to an operator's database would make the recorded checksum disagree
    with the file and, worse, would silently skip the change on that machine.
    """
    assert migrations[2].checksum == MIGRATION_0003_SHA256


# ------------------------------------------------------------ fresh install


def test_fresh_database_reaches_head_directly(db: sqlite3.Connection) -> None:
    applied = apply_migrations(db)
    assert [m.version for m in applied] == list(range(1, SCHEMA_VERSION_HEAD + 1))
    assert current_schema_version(db) == SCHEMA_VERSION_HEAD


def test_schema_holds_exactly_the_expected_tables(db: sqlite3.Connection) -> None:
    apply_migrations(db)
    tables = _tables(db)
    assert tables == HEAD_TABLES
    assert not any(name.endswith("_v2") for name in tables), "rebuild scaffolding left behind"


def test_recordings_gains_the_phase_2_columns(db: sqlite3.Connection) -> None:
    apply_migrations(db)
    columns = _columns(db, "recordings")
    assert {
        "recording_uuid",
        "status",
        "sample_rate_hz",
        "channels",
        "sample_format",
        "chunk_seconds",
        "device_fingerprint",
        "device_transport",
        "device_transport_verified",
        "device_snapshot_json",
        "monotonic_start_ns",
        "monotonic_end_ns",
        "paused_ms",
        "pause_count",
        "written_frames",
        "dropped_frames",
        "xrun_callbacks",
        "queue_high_water_frames",
        "chunk_count",
        "total_bytes",
        "manifest_relative_path",
        "manifest_sha256",
        "manifest_status",
        "degraded",
        "recovered_chunks",
        "quarantined_chunks",
        "last_error",
    } <= columns


def test_chunks_gain_the_integrity_columns(db: sqlite3.Connection) -> None:
    apply_migrations(db)
    columns = _columns(db, "recording_chunks")
    assert {
        "seq",
        "filename",
        "start_frame",
        "end_frame",
        "frames",
        "duration_ms",
        "utc_start",
        "utc_end",
        "monotonic_start_ns",
        "monotonic_end_ns",
        "sample_rate_hz",
        "channels",
        "sample_format",
        "size_bytes",
        "sha256",
        "dropped_frames",
        "xrun_callbacks",
        "status",
        "recovery_status",
        "finalized",
    } <= columns


def test_meetings_gains_a_uuid_for_the_directory_layout(db: sqlite3.Connection) -> None:
    apply_migrations(db)
    assert "uuid" in _columns(db, "meetings")
    db.execute("INSERT INTO meetings (title) VALUES ('Rapat')")
    row = db.execute("SELECT uuid FROM meetings").fetchone()
    # A recording path must never contain a meeting title, so the layout keys on
    # this instead. Phase 2 code supplies it; the column allows NULL so 0002 can
    # backfill legacy rows.
    assert "uuid" in _columns(db, "meetings")
    assert row is not None


def test_no_phase_4_or_later_table_is_created(db: sqlite3.Connection) -> None:
    """Phase 3 arrived, so its four tables moved out of this list.

    `consents` stays forbidden on purpose: Phase 3 records consent as an
    append-only `consent_events` log, and a mutable `consents` row carrying a
    boolean flag would reintroduce the design 0003 deliberately rejected.
    """
    apply_migrations(db)
    forbidden = {
        "consents",
        "asr_words",
        "diarization_turns",
        "speaker_assignments",
        "utterances",
        "mom_items",
        "evidence_links",
        "action_tracking",
    }
    assert _tables(db) & forbidden == set()


# ------------------------------------------------------------- constraints


def test_recording_lifecycle_vocabulary_is_enforced(db: sqlite3.Connection) -> None:
    apply_migrations(db)
    db.execute("INSERT INTO meetings (title, uuid) VALUES ('m', 'mu')")
    for status in (
        "IDLE",
        "PREFLIGHT",
        "ARMED",
        "RECORDING",
        "PAUSED",
        "STOPPING",
        "FINALIZING",
        "RECORDED",
        "RECOVERABLE",
        "FAILED",
        "CANCELLED",
    ):
        db.execute(
            "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir, status) "
            "VALUES (1, ?, ?, ?)",
            (f"u-{status}", f"mu/{status}", status),
        )
        # Only one may stay active at a time, so park it immediately.
        db.execute(
            "UPDATE recordings SET status='RECORDED' WHERE recording_uuid = ?",
            (f"u-{status}",),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir, status) "
            "VALUES (1, 'bad', 'mu/bad', 'TRANSCRIBING')"
        )


def test_only_one_recording_may_be_active_across_the_data_root(
    db: sqlite3.Connection,
) -> None:
    apply_migrations(db)
    db.execute("INSERT INTO meetings (title, uuid) VALUES ('a', 'ua')")
    db.execute("INSERT INTO meetings (title, uuid) VALUES ('b', 'ub')")
    db.execute(
        "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir, status) "
        "VALUES (1, 'u1', 'ua/r1', 'RECORDING')"
    )
    # A different meeting must not be able to record concurrently either.
    with pytest.raises(sqlite3.IntegrityError, match="ux_recordings_single_active"):
        db.execute(
            "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir, status) "
            "VALUES (2, 'u2', 'ub/r1', 'PREFLIGHT')"
        )
    db.execute("UPDATE recordings SET status='RECORDED' WHERE recording_uuid='u1'")
    db.execute(
        "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir, status) "
        "VALUES (2, 'u2', 'ub/r1', 'RECORDING')"
    )


@pytest.mark.parametrize("bad", [r"D:\abs", "/abs", "../escape", "m/../r", ""])
def test_recording_directory_must_stay_relative(db: sqlite3.Connection, bad: str) -> None:
    apply_migrations(db)
    db.execute("INSERT INTO meetings (title, uuid) VALUES ('m', 'mu')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir) "
            "VALUES (1, 'u', ?)",
            (bad,),
        )


@pytest.mark.parametrize("bad", ["sub/chunk.wav", "..\\chunk.wav", "C:chunk.wav", "  "])
def test_chunk_filename_must_be_bare(db: sqlite3.Connection, bad: str) -> None:
    apply_migrations(db)
    db.execute("INSERT INTO meetings (title, uuid) VALUES ('m', 'mu')")
    db.execute(
        "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir) "
        "VALUES (1, 'u', 'mu/r')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO recording_chunks (recording_id, seq, filename) VALUES (1, 0, ?)",
            (bad,),
        )


def test_more_than_two_channels_is_rejected(db: sqlite3.Connection) -> None:
    apply_migrations(db)
    db.execute("INSERT INTO meetings (title, uuid) VALUES ('m', 'mu')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir, channels) "
            "VALUES (1, 'u', 'mu/r', 3)"
        )


def test_chunk_frame_range_must_be_ordered(db: sqlite3.Connection) -> None:
    apply_migrations(db)
    db.execute("INSERT INTO meetings (title, uuid) VALUES ('m', 'mu')")
    db.execute(
        "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir) "
        "VALUES (1, 'u', 'mu/r')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO recording_chunks (recording_id, seq, filename, start_frame, end_frame) "
            "VALUES (1, 0, 'chunk_000000.wav', 500, 100)"
        )


# ------------------------------------------------------- upgrade with data


@pytest.fixture
def upgraded(db: sqlite3.Connection, migrations):
    """A schema-1 database holding rows, then upgraded to head."""
    apply_migrations(db, [migrations[0]])
    assert current_schema_version(db) == 1
    db.execute("INSERT INTO meetings (title) VALUES ('legacy meeting')")
    db.execute(
        "INSERT INTO recordings (meeting_id, relative_dir, status, container, "
        "sample_rate_hz, channels, bit_depth, device_name, device_id, "
        "written_frames, dropped_frames) "
        "VALUES (1,'m1/r1','CAPTURING','wav',48000,4,16,'Old Mic','fp-old',1234,7)"
    )
    db.execute(
        "INSERT INTO recordings (meeting_id, relative_dir, status) VALUES (1,'m1/r2','COMPLETED')"
    )
    db.execute(
        "INSERT INTO recording_chunks (recording_id, seq, filename, sha256, size_bytes, "
        "frames, sample_offset, dropped_frames) VALUES (1,0,'chunk_000000.wav',?,4444,1000,0,3)",
        ("a" * 64,),
    )
    db.execute(
        "INSERT INTO recording_chunks (recording_id, seq, filename, frames, sample_offset) "
        "VALUES (1,1,'chunk_000001.wav',500,1000)"
    )
    applied = apply_migrations(db)
    # A schema-1 database catches up to head, which is now 4.
    assert [m.version for m in applied] == list(range(2, SCHEMA_VERSION_HEAD + 1))
    return db


def test_upgrade_preserves_recording_rows(upgraded: sqlite3.Connection) -> None:
    rows = {r["relative_dir"]: dict(r) for r in upgraded.execute("SELECT * FROM recordings")}
    assert set(rows) == {"m1/r1", "m1/r2"}
    assert rows["m1/r1"]["written_frames"] == 1234
    assert rows["m1/r1"]["dropped_frames"] == 7
    assert rows["m1/r1"]["device_fingerprint"] == "fp-old"
    assert rows["m1/r1"]["device_name"] == "Old Mic"


def test_upgrade_preserves_chunk_rows(upgraded: sqlite3.Connection) -> None:
    """The rebuild must not let the parent drop cascade the copied chunks away."""
    chunks = list(upgraded.execute("SELECT * FROM recording_chunks ORDER BY seq"))
    assert len(chunks) == 2
    assert chunks[0]["sha256"] == "a" * 64
    assert chunks[0]["size_bytes"] == 4444
    assert (chunks[0]["start_frame"], chunks[0]["end_frame"]) == (0, 1000)
    assert (chunks[1]["start_frame"], chunks[1]["end_frame"]) == (1000, 1500)


def test_upgrade_maps_the_old_status_vocabulary(upgraded: sqlite3.Connection) -> None:
    rows = {r["relative_dir"]: r["status"] for r in upgraded.execute("SELECT * FROM recordings")}
    # An interrupted capture found at migration time is awaiting recovery.
    assert rows["m1/r1"] == "RECOVERABLE"
    assert rows["m1/r2"] == "RECORDED"


def test_upgrade_caps_channels_at_two(upgraded: sqlite3.Connection) -> None:
    row = upgraded.execute(
        "SELECT channels FROM recordings WHERE relative_dir='m1/r1'"
    ).fetchone()
    assert row["channels"] == 2, "a 4-channel legacy row must be clamped, not rejected"


def test_upgrade_generates_uuids(upgraded: sqlite3.Connection) -> None:
    uuids = [r["recording_uuid"] for r in upgraded.execute("SELECT recording_uuid FROM recordings")]
    assert all(len(u) == 36 for u in uuids)
    assert len(set(uuids)) == len(uuids)
    meeting_uuid = upgraded.execute("SELECT uuid FROM meetings").fetchone()["uuid"]
    assert len(meeting_uuid) == 36


def test_upgrade_keeps_foreign_keys_intact(upgraded: sqlite3.Connection) -> None:
    assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
    sql = upgraded.execute(
        "SELECT sql FROM sqlite_master WHERE name='recording_chunks'"
    ).fetchone()["sql"]
    # The rename must have rewritten the foreign key from recordings_v2.
    assert "recordings_v2" not in sql
    assert "ON DELETE CASCADE" in sql

    upgraded.execute("DELETE FROM recordings WHERE relative_dir='m1/r1'")
    remaining = upgraded.execute("SELECT COUNT(*) AS n FROM recording_chunks").fetchone()["n"]
    assert remaining == 0, "cascade must still work after the rebuild"


def test_upgrade_is_idempotent(upgraded: sqlite3.Connection) -> None:
    assert apply_migrations(upgraded) == []
    status = migration_status(upgraded)
    assert status["up_to_date"] is True
    assert status["pending"] == []
    verify_applied_checksums(upgraded)


def test_wal_and_foreign_keys_survive_the_rebuild(upgraded: sqlite3.Connection) -> None:
    from mom_igd.db.connection import read_pragmas

    pragmas = read_pragmas(upgraded)
    assert pragmas["journal_mode"] == "wal"
    assert pragmas["foreign_keys"] == 1
    assert pragmas["user_version"] == SCHEMA_VERSION_HEAD


# ------------------------------------------------------------- rollback


def test_a_failing_later_migration_does_not_advance_the_version(
    db: sqlite3.Connection, migrations, tmp_path: Path
) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    for migration in migrations:
        (directory / migration.path.name).write_bytes(migration.path.read_bytes())
    # Numbered past the real head, computed rather than hard-coded: a new phase adds a
    # real migration, and this deliberately broken one must stay beyond it.
    broken = f"{SCHEMA_VERSION_HEAD + 1:04d}_broken"
    (directory / f"{broken}.sql").write_text(
        "CREATE TABLE should_not_exist (x INTEGER);\nSELECT this_column_is_missing;\n",
        encoding="utf-8",
    )

    with pytest.raises(MigrationError, match=broken):
        apply_migrations(db, discover_migrations(directory))

    assert current_schema_version(db) == SCHEMA_VERSION_HEAD
    assert "should_not_exist" not in _tables(db)
    assert "recordings" in _tables(db), "0002 stays applied"
    assert "voiceprints" in _tables(db), "0003 stays applied"


def test_0002_alone_cannot_be_applied_to_an_empty_database(
    db: sqlite3.Connection, migrations
) -> None:
    """0002 extends 0001; applying it first must fail rather than half-build."""
    with pytest.raises(MigrationError):
        apply_migrations(db, [migrations[1]])
    assert "recordings" not in _tables(db)
