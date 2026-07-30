"""Migration 0004: per-meeting roster capacity.

The property that matters is not "the column exists" -- it is that a database an
operator has already recorded meetings into comes through the upgrade with every
meeting, recording, chunk, job, consent event and voiceprint row intact, and with
every existing meeting holding the old fixed capacity of nine.

Everything runs on a temporary data root. The real one is guarded by a session
fixture and must never be touched by a migration test.
"""

from __future__ import annotations

import sqlite3
import uuid as uuid_module
from pathlib import Path

import pytest

from mom_igd.db.migrator import (
    apply_migrations,
    current_schema_version,
    discover_migrations,
)
from mom_igd.enrollment.participants import BASELINE_MEETING_CAPACITY
from mom_igd.version import SCHEMA_VERSION_HEAD

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "mom_igd" / "db" / "migrations"


@pytest.fixture
def migrations():
    return discover_migrations(MIGRATIONS_DIR)


@pytest.fixture
def db(tmp_path: Path):
    from mom_igd.db.connection import connect

    connection = connect(tmp_path / "probe.db")
    yield connection
    connection.close()


def _upto(connection: sqlite3.Connection, migrations, version: int) -> None:
    apply_migrations(connection, [m for m in migrations if m.version <= version])


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {r["name"]: r for r in connection.execute(f"PRAGMA table_info({table})")}


# ------------------------------------------------------------------ discovery


@pytest.fixture()
def migration_0004(migrations):
    """0004 itself, found by version rather than by being last.

    It was the head when Phase 3's corrective pass shipped it; Phase 4 added 0005. What
    these tests are about is the content of 0004, so they address it directly.
    """
    return next(m for m in migrations if m.version == 4)


def test_0004_ships_with_the_expected_name(migration_0004) -> None:
    assert migration_0004.name == "meeting_participant_capacity"
    assert SCHEMA_VERSION_HEAD >= 4


def test_0004_does_not_use_executescript(migration_0004) -> None:
    """`executescript` issues an implicit COMMIT, defeating the transaction."""
    text = migration_0004.path.read_text(encoding="utf-8")
    assert "executescript" not in text


def test_0004_leaves_the_business_ceiling_out_of_the_schema(migration_0004) -> None:
    """The DB invariant is `>= 1`, and only that.

    A CHECK of `<= 50` would force a full table rebuild -- foreign keys, indexes and
    cascades -- the first time an operator legitimately needs a larger room. The
    ceiling belongs in configuration, where it is one edited value.
    """
    text = migration_0004.path.read_text(encoding="utf-8")
    assert "participant_capacity >= 1" in text
    assert "<= 50" not in text
    assert "participant_capacity <=" not in text


# --------------------------------------------------------------- fresh install


def test_a_fresh_database_reaches_schema_four(db, migrations) -> None:
    applied = apply_migrations(db, migrations)
    assert [m.version for m in applied] == list(range(1, SCHEMA_VERSION_HEAD + 1))
    assert current_schema_version(db) == SCHEMA_VERSION_HEAD


def test_a_new_meeting_gets_the_default_capacity(db, migrations) -> None:
    apply_migrations(db, migrations)
    db.execute("INSERT INTO meetings (title) VALUES ('Rapat baru')")
    row = db.execute(
        "SELECT participant_capacity FROM meetings WHERE title = 'Rapat baru'"
    ).fetchone()
    assert row["participant_capacity"] == BASELINE_MEETING_CAPACITY == 9


def test_the_column_is_not_null_with_a_default(db, migrations) -> None:
    apply_migrations(db, migrations)
    column = _columns(db, "meetings")["participant_capacity"]
    assert column["type"] == "INTEGER"
    assert column["notnull"] == 1
    assert column["dflt_value"] == "9"


def test_the_capacity_check_refuses_zero_and_negatives(db, migrations) -> None:
    apply_migrations(db, migrations)
    db.execute("INSERT INTO meetings (title) VALUES ('Rapat')")
    for bad in (0, -1, -999):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE meetings SET participant_capacity = ?", (bad,))


def test_a_capacity_above_the_configured_ceiling_is_not_a_database_error(
    db, migrations
) -> None:
    apply_migrations(db, migrations)
    db.execute("INSERT INTO meetings (title) VALUES ('Rapat besar')")
    db.execute("UPDATE meetings SET participant_capacity = 500")
    assert (
        db.execute("SELECT participant_capacity FROM meetings").fetchone()[0] == 500
    ), "the ceiling is policy, enforced by the service and the API, not by SQLite"


# ------------------------------------------------- upgrade from schema 3 with data


@pytest.fixture
def populated_schema_3(db, migrations):
    """A schema-3 database with a row in every table 0004 must not disturb."""
    _upto(db, migrations, 3)
    assert current_schema_version(db) == 3

    meeting_uuid = str(uuid_module.uuid4())
    db.execute(
        "INSERT INTO meetings (title, agenda, location, uuid) "
        "VALUES ('Rapat lama', 'Agenda lama', 'Ruang A', ?)",
        (meeting_uuid,),
    )
    meeting_id = db.execute(
        "SELECT id FROM meetings WHERE uuid = ?", (meeting_uuid,)
    ).fetchone()["id"]

    db.execute(
        "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir, status) "
        "VALUES (?, ?, 'rec-1', 'RECORDED')",
        (meeting_id, str(uuid_module.uuid4())),
    )
    recording_id = db.execute("SELECT id FROM recordings").fetchone()["id"]
    db.execute(
        "INSERT INTO recording_chunks (recording_id, seq, filename, sha256, "
        " size_bytes, frames, start_frame, end_frame, status) "
        "VALUES (?, 0, 'chunk_000000.wav', ?, 4444, 1000, 0, 1000, 'WRITTEN')",
        (recording_id, "a" * 64),
    )
    db.execute(
        "INSERT INTO jobs (meeting_id, state) VALUES (?, 'DRAFT')", (meeting_id,)
    )

    participant_uuid = str(uuid_module.uuid4())
    db.execute(
        "INSERT INTO participants (display_name, role, uuid) VALUES ('Budi', 'Ketua', ?)",
        (participant_uuid,),
    )
    participant_id = db.execute("SELECT id FROM participants").fetchone()["id"]
    db.execute(
        "INSERT INTO meeting_participants (meeting_id, participant_id, seat_label) "
        "VALUES (?, ?, 'kepala meja')",
        (meeting_id, participant_id),
    )
    db.execute(
        "INSERT INTO consent_events (participant_id, event_uuid, action, "
        " consent_version, consent_text_sha256, purpose, confirmation_method, actor) "
        "VALUES (?, ?, 'GRANTED', '1.0-draft', ?, 'speaker-identification', "
        " 'PARTICIPANT_CONFIRMED_ON_DEVICE', 'local-operator')",
        (participant_id, str(uuid_module.uuid4()), "b" * 64),
    )
    db.commit()
    return {
        "meeting_uuid": meeting_uuid,
        "meeting_id": meeting_id,
        "participant_uuid": participant_uuid,
        "participant_id": participant_id,
    }


TABLES = (
    "meetings",
    "recordings",
    "recording_chunks",
    "jobs",
    "participants",
    "meeting_participants",
    "consent_events",
    "voiceprints",
    "enrollment_sessions",
    "audit_events",
)


def test_the_upgrade_preserves_every_row(db, migrations, populated_schema_3) -> None:
    before = {
        table: db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
        for table in TABLES
    }
    applied = apply_migrations(db, migrations)
    assert [m.version for m in applied] == list(
        range(4, SCHEMA_VERSION_HEAD + 1)
    ), "0004 and every later migration should still be pending"
    after = {
        table: db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
        for table in TABLES
    }
    assert before == after


def test_the_upgrade_preserves_meeting_and_chunk_content(
    db, migrations, populated_schema_3
) -> None:
    before_meeting = dict(db.execute("SELECT * FROM meetings").fetchone())
    before_chunk = dict(db.execute("SELECT * FROM recording_chunks").fetchone())
    apply_migrations(db, migrations)
    after_meeting = dict(db.execute("SELECT * FROM meetings").fetchone())
    after_chunk = dict(db.execute("SELECT * FROM recording_chunks").fetchone())

    assert after_chunk == before_chunk, "chunk integrity metadata must be untouched"
    # The only difference in `meetings` is the new column.
    added = set(after_meeting) - set(before_meeting)
    assert added == {"participant_capacity"}
    for key in before_meeting:
        assert after_meeting[key] == before_meeting[key], key


def test_every_existing_meeting_is_backfilled_with_nine(
    db, migrations, populated_schema_3
) -> None:
    apply_migrations(db, migrations)
    rows = db.execute("SELECT uuid, participant_capacity FROM meetings").fetchall()
    assert rows
    for row in rows:
        assert row["participant_capacity"] == 9, (
            "a meeting recorded under the old fixed cap must keep behaving identically"
        )


def test_the_upgrade_preserves_consent_and_roster_history(
    db, migrations, populated_schema_3
) -> None:
    apply_migrations(db, migrations)
    consent = db.execute("SELECT * FROM consent_events").fetchone()
    assert consent["action"] == "GRANTED"
    assert consent["consent_version"] == "1.0-draft"
    membership = db.execute("SELECT * FROM meeting_participants").fetchone()
    assert membership["seat_label"] == "kepala meja"
    assert membership["is_active"] == 1


def test_the_upgrade_keeps_foreign_keys_intact(
    db, migrations, populated_schema_3
) -> None:
    apply_migrations(db, migrations)
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_the_upgrade_is_idempotent(db, migrations, populated_schema_3) -> None:
    apply_migrations(db, migrations)
    assert apply_migrations(db, migrations) == []
    assert current_schema_version(db) == SCHEMA_VERSION_HEAD


def test_the_upgrade_advances_user_version(db, migrations, populated_schema_3) -> None:
    apply_migrations(db, migrations)
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION_HEAD


def test_wal_and_foreign_keys_survive_the_upgrade(
    db, migrations, populated_schema_3
) -> None:
    apply_migrations(db, migrations)
    assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_the_index_the_migration_adds_exists(db, migrations) -> None:
    apply_migrations(db, migrations)
    names = {
        r["name"]
        for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert "ix_meetings_capacity" in names


def test_earlier_migrations_are_untouched_by_the_upgrade(migrations) -> None:
    """0004 must extend the schema, not edit an applied migration."""
    for migration in migrations[:3]:
        text = migration.path.read_text(encoding="utf-8")
        assert "participant_capacity" not in text, (
            f"{migration.path.name} mentions participant_capacity, so 0004's change "
            "was made by editing an applied migration"
        )
