"""SQLite foundation: pragmas, schema surface, transaction nesting.

Covers Phase 1 test categories 9, 10 and 13.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mom_igd.config import AppConfig
from mom_igd.db.connection import (
    PragmaVerificationError,
    connect,
    maybe_transaction,
    read_pragmas,
    transaction,
    verify_pragmas,
)

# The Phase 1 table set, fixed by the phase specification. `schema_migrations`
# is created by the migration runner; the other eight come from 0001_initial.sql.
PHASE_1_TABLES = {
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

# Tables that belong to later phases and must NOT exist yet.
FUTURE_TABLES = {
    "meeting_participants",
    "voiceprints",
    "consents",
    "asr_words",
    "diarization_turns",
    "utterances",
    "speaker_assignments",
    "mom_items",
    "evidence_links",
    "action_items",
    "action_tracking",
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row["name"]) for row in rows}


# ------------------------------------------------------------------- 9. WAL


def test_wal_is_enabled_and_verified(conn: sqlite3.Connection) -> None:
    pragmas = verify_pragmas(conn)
    assert pragmas["journal_mode"] == "wal"


def test_wal_sidecar_files_appear(conn: sqlite3.Connection, db_path: Path) -> None:
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('probe', '1')")
    assert db_path.with_name(db_path.name + "-wal").exists()


def test_busy_timeout_is_applied(config: AppConfig, db_path: Path) -> None:
    connection = connect(db_path, busy_timeout_ms=7321)
    try:
        assert read_pragmas(connection)["busy_timeout"] == 7321
    finally:
        connection.close()


def test_synchronous_is_normal_under_wal(conn: sqlite3.Connection) -> None:
    # NORMAL (1) is the recommended durability level under WAL: a crash can lose
    # the last transaction but cannot corrupt the database.
    assert read_pragmas(conn)["synchronous"] == 1


def test_row_factory_gives_named_access(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT 1 AS answer").fetchone()
    assert row["answer"] == 1


def test_in_memory_database_is_refused_because_wal_is_impossible(tmp_path: Path) -> None:
    # sqlite3 cannot put an in-memory database into WAL. Rather than silently
    # degrading, connect() must fail loudly.
    with pytest.raises(PragmaVerificationError, match="journal_mode"):
        connect(":memory:", busy_timeout_ms=1000)


def test_connect_refuses_a_missing_parent_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        connect(tmp_path / "absent" / "x.db")


# --------------------------------------------------------- 10. foreign keys


def test_foreign_keys_pragma_is_on(conn: sqlite3.Connection) -> None:
    assert read_pragmas(conn)["foreign_keys"] == 1


def test_foreign_key_violation_is_actually_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir) "
            "VALUES (424242, 'u-orphan', 'x')"
        )


def test_cascade_delete_removes_dependent_rows(conn: sqlite3.Connection, meeting_id: int) -> None:
    conn.execute(
        "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir) "
        "VALUES (?, 'u-cascade', 'm1')",
        (meeting_id,),
    )
    recording_id = conn.execute("SELECT id FROM recordings").fetchone()["id"]
    conn.execute(
        "INSERT INTO recording_chunks (recording_id, seq, filename) VALUES (?, 0, 'seg_000001.flac')",
        (recording_id,),
    )
    conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
    assert conn.execute("SELECT COUNT(*) AS n FROM recordings").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM recording_chunks").fetchone()["n"] == 0


def test_foreign_key_check_is_clean_after_migration(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# ------------------------------------------------- 13. Phase 1 schema surface


def test_schema_contains_exactly_the_phase_1_tables(conn: sqlite3.Connection) -> None:
    assert _tables(conn) == PHASE_1_TABLES


def test_no_future_phase_table_exists(conn: sqlite3.Connection) -> None:
    present = _tables(conn) & FUTURE_TABLES
    assert present == set(), f"tables from a later phase were created: {sorted(present)}"


def test_check_constraints_reject_invalid_enumerations(conn: sqlite3.Connection, meeting_id: int) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs (meeting_id, state) VALUES (?, 'BANANA')", (meeting_id,))


def test_recording_relative_dir_must_stay_relative(conn: sqlite3.Connection, meeting_id: int) -> None:
    for bad in (r"D:\absolute\path", "/etc/passwd", r"..\escape", "\\unc"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO recordings (meeting_id, relative_dir) VALUES (?, ?)",
                (meeting_id, bad),
            )


def test_chunk_filename_must_be_bare(conn: sqlite3.Connection, meeting_id: int) -> None:
    conn.execute(
        "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir) "
        "VALUES (?, 'u-bare', 'm1')",
        (meeting_id,),
    )
    recording_id = conn.execute("SELECT id FROM recordings").fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO recording_chunks (recording_id, seq, filename) VALUES (?, 0, 'sub/seg.flac')",
            (recording_id,),
        )


def test_blank_titles_and_names_are_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO meetings (title) VALUES ('   ')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO participants (display_name) VALUES ('')")


def test_participant_names_are_unique(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO participants (display_name) VALUES ('Budi')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO participants (display_name) VALUES ('Budi')")


def test_timestamps_default_to_iso8601_utc(conn: sqlite3.Connection, meeting_id: int) -> None:
    created = conn.execute(
        "SELECT created_at FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()["created_at"]
    assert created.endswith("Z")
    assert created[4] == "-" and created[10] == "T"


# ------------------------------------------------------- transaction nesting


def test_transaction_commits_on_success(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('a', '1')")
    assert conn.execute("SELECT value FROM app_settings WHERE key='a'").fetchone()["value"] == "1"


def test_transaction_rolls_back_on_error(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError):
        with transaction(conn):
            conn.execute("INSERT INTO app_settings (key, value) VALUES ('b', '1')")
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) AS n FROM app_settings WHERE key='b'").fetchone()["n"] == 0


def test_nested_maybe_transaction_does_not_commit_early(conn: sqlite3.Connection) -> None:
    """The inner block must join the outer transaction, not commit it.

    This is the bug class that would make a state change durable while its audit
    event was still rolled back.
    """
    with pytest.raises(RuntimeError):
        with maybe_transaction(conn):
            conn.execute("INSERT INTO app_settings (key, value) VALUES ('outer', '1')")
            with maybe_transaction(conn):
                conn.execute("INSERT INTO app_settings (key, value) VALUES ('inner', '1')")
            assert conn.in_transaction, "inner block must not have committed"
            raise RuntimeError("boom")

    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM app_settings WHERE key IN ('outer','inner')"
    ).fetchone()["n"]
    assert remaining == 0, "both the outer and the inner write must be rolled back"


def test_maybe_transaction_opens_its_own_when_none_is_active(conn: sqlite3.Connection) -> None:
    assert not conn.in_transaction
    with maybe_transaction(conn):
        assert conn.in_transaction
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('solo', '1')")
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) AS n FROM app_settings WHERE key='solo'").fetchone()["n"] == 1
