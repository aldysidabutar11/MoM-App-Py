"""Migration 0005: the schema that has to make a wrong transcript impossible to store.

A CHECK constraint here is worth more than a comment, because it survives a future code
path that forgets the rule. What is tested is that each one actually refuses what it
claims to, and that nothing in Phase 2's or Phase 3's schema was disturbed.
"""

from __future__ import annotations

import sqlite3

import pytest

from mom_igd.version import SCHEMA_VERSION_HEAD


@pytest.fixture()
def recording_id(conn: sqlite3.Connection, meeting_id: int) -> int:
    cursor = conn.execute(
        "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir, status) "
        "VALUES (?, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'm/r', 'RECORDED')",
        (meeting_id,),
    )
    return int(cursor.lastrowid or 0)


def _working_copy(conn: sqlite3.Connection, recording_id: int, **overrides: object) -> int:
    values: dict[str, object] = {
        "recording_id": recording_id,
        "relative_path": "working/a.wav",
        "sha256": "ab" * 32,
        "frames": 16_000,
        "duration_ms": 1000,
        "status": "READY",
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    cursor = conn.execute(
        f"INSERT INTO audio_working_copies ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )
    return int(cursor.lastrowid or 0)


def _vad_run(conn: sqlite3.Connection, working_copy_id: int, **overrides: object) -> int:
    values: dict[str, object] = {
        "working_copy_id": working_copy_id,
        "model_name": "silero-vad-v6-bundled",
        "model_sha256": "cd" * 32,
        "config_hash": "ef" * 32,
        "config_json": "{}",
        "is_active": 1,
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    cursor = conn.execute(
        f"INSERT INTO vad_runs ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )
    return int(cursor.lastrowid or 0)


def _transcript(conn: sqlite3.Connection, recording_id: int, wc: int, **overrides: object) -> int:
    values: dict[str, object] = {
        "recording_id": recording_id,
        "working_copy_id": wc,
        "revision": 1,
        "status": "BUILDING",
        "is_active": 0,
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    cursor = conn.execute(
        f"INSERT INTO transcripts ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )
    return int(cursor.lastrowid or 0)


def _segment(conn: sqlite3.Connection, transcript_id: int, **overrides: object) -> int:
    values: dict[str, object] = {
        "transcript_id": transcript_id,
        "seq": 0,
        "start_ms": 0,
        "end_ms": 1000,
        "text": "kata",
        "text_raw": "kata",
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    cursor = conn.execute(
        f"INSERT INTO transcript_segments ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )
    return int(cursor.lastrowid or 0)


# ===========================================================================
# Applied, and nothing else disturbed
# ===========================================================================


def test_the_head_version_is_five(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    assert int(row["v"]) == SCHEMA_VERSION_HEAD == 5


def test_the_new_tables_exist(conn: sqlite3.Connection) -> None:
    names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "audio_working_copies",
        "vad_runs",
        "speech_regions",
        "transcripts",
        "transcript_segments",
        "transcript_words",
    } <= names


def test_phase_2_and_phase_3_tables_are_untouched(conn: sqlite3.Connection) -> None:
    """0005 adds tables. It must not rebuild anything that already held data."""
    for table in ("recordings", "recording_chunks", "voiceprints", "meeting_participants"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    assert integrity == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_no_speaker_column_was_introduced(conn: sqlite3.Connection) -> None:
    """Phase 5 and 6 own that. A column sitting NULL for two phases invites a guess."""
    for table in ("transcripts", "transcript_segments", "transcript_words"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert not any("speaker" in name for name in columns), (table, sorted(columns))


def test_the_asr_migration_is_the_fifth_and_the_earlier_four_are_intact(
    conn: sqlite3.Connection,
) -> None:
    """0001 through 0004 are checksummed; editing one would fail verification."""
    from mom_igd.db.migrator import discover_migrations, verify_applied_checksums

    discovered = discover_migrations()
    assert [migration.version for migration in discovered] == [1, 2, 3, 4, 5]
    assert discovered[-1].name == "offline_asr"
    verify_applied_checksums(conn)


# ===========================================================================
# The working copy
# ===========================================================================


def test_a_working_copy_must_be_16k_mono_int16(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    """A 44.1 kHz "working copy" would be silently resampled inside the engine."""
    _working_copy(conn, recording_id)
    for column, value in (
        ("sample_rate_hz", 44_100),
        ("channels", 2),
        ("sample_format", "float32"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            _working_copy(
                conn,
                recording_id,
                relative_path=f"working/{column}.wav",
                **{column: value},
            )


def test_only_one_working_copy_per_recording(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    _working_copy(conn, recording_id)
    with pytest.raises(sqlite3.IntegrityError):
        _working_copy(conn, recording_id, relative_path="working/b.wav")


@pytest.mark.parametrize(
    "path", ["C:/absolute.wav", "/absolute.wav", "../escape.wav", "  ", "a:b"]
)
def test_a_working_copy_path_must_be_relative_and_contained(
    conn: sqlite3.Connection, recording_id: int, path: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _working_copy(conn, recording_id, relative_path=path)


def test_a_working_copy_status_is_a_closed_set(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _working_copy(conn, recording_id, status="PROBABLY_FINE")


def test_deleting_a_recording_removes_its_working_copy(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    _working_copy(conn, recording_id)
    conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
    assert conn.execute("SELECT COUNT(*) FROM audio_working_copies").fetchone()[0] == 0


# ===========================================================================
# VAD runs and regions
# ===========================================================================


def test_only_one_active_vad_run_per_working_copy(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    working_copy_id = _working_copy(conn, recording_id)
    _vad_run(conn, working_copy_id)
    with pytest.raises(sqlite3.IntegrityError):
        _vad_run(conn, working_copy_id)


def test_an_inactive_vad_run_may_coexist(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    """Earlier runs are kept: a transcript must be able to say what it was built from."""
    working_copy_id = _working_copy(conn, recording_id)
    _vad_run(conn, working_copy_id, is_active=0)
    _vad_run(conn, working_copy_id, is_active=0)
    _vad_run(conn, working_copy_id, is_active=1)
    assert conn.execute("SELECT COUNT(*) FROM vad_runs").fetchone()[0] == 3


def test_a_vad_model_digest_must_be_a_full_sha256(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    working_copy_id = _working_copy(conn, recording_id)
    with pytest.raises(sqlite3.IntegrityError):
        _vad_run(conn, working_copy_id, model_sha256="tooshort")


def test_a_reversed_speech_region_is_refused(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    working_copy_id = _working_copy(conn, recording_id)
    vad_run_id = _vad_run(conn, working_copy_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO speech_regions (vad_run_id, seq, start_ms, end_ms) "
            "VALUES (?, 0, 5000, 1000)",
            (vad_run_id,),
        )


def test_a_duplicate_region_sequence_is_refused(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    working_copy_id = _working_copy(conn, recording_id)
    vad_run_id = _vad_run(conn, working_copy_id)
    conn.execute(
        "INSERT INTO speech_regions (vad_run_id, seq, start_ms, end_ms) VALUES (?, 0, 0, 1)",
        (vad_run_id,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO speech_regions (vad_run_id, seq, start_ms, end_ms) VALUES (?, 0, 2, 3)",
            (vad_run_id,),
        )


# ===========================================================================
# Transcripts
# ===========================================================================


def test_only_one_active_transcript_per_recording(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    working_copy_id = _working_copy(conn, recording_id)
    _transcript(conn, recording_id, working_copy_id, revision=1, is_active=1)
    with pytest.raises(sqlite3.IntegrityError):
        _transcript(conn, recording_id, working_copy_id, revision=2, is_active=1)


def test_a_duplicate_revision_number_is_refused(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    working_copy_id = _working_copy(conn, recording_id)
    _transcript(conn, recording_id, working_copy_id, revision=1)
    with pytest.raises(sqlite3.IntegrityError):
        _transcript(conn, recording_id, working_copy_id, revision=1)


def test_a_revision_below_one_is_refused(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    working_copy_id = _working_copy(conn, recording_id)
    with pytest.raises(sqlite3.IntegrityError):
        _transcript(conn, recording_id, working_copy_id, revision=0)


def test_a_transcript_status_is_a_closed_set(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    working_copy_id = _working_copy(conn, recording_id)
    with pytest.raises(sqlite3.IntegrityError):
        _transcript(conn, recording_id, working_copy_id, status="NEARLY_DONE")


def test_a_language_probability_outside_zero_to_one_is_refused(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    working_copy_id = _working_copy(conn, recording_id)
    with pytest.raises(sqlite3.IntegrityError):
        _transcript(conn, recording_id, working_copy_id, language_probability=1.5)


def test_a_truncated_model_digest_is_refused(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    """The provenance of a transcript is only as good as the digest recorded on it."""
    working_copy_id = _working_copy(conn, recording_id)
    with pytest.raises(sqlite3.IntegrityError):
        _transcript(conn, recording_id, working_copy_id, pass1_manifest_sha256="abc")


# ===========================================================================
# Segments and words
# ===========================================================================


def test_a_reversed_segment_is_refused(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    working_copy_id = _working_copy(conn, recording_id)
    transcript_id = _transcript(conn, recording_id, working_copy_id)
    with pytest.raises(sqlite3.IntegrityError):
        _segment(conn, transcript_id, start_ms=5000, end_ms=1000)


def test_a_negative_timestamp_is_refused(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    working_copy_id = _working_copy(conn, recording_id)
    transcript_id = _transcript(conn, recording_id, working_copy_id)
    with pytest.raises(sqlite3.IntegrityError):
        _segment(conn, transcript_id, start_ms=-1)


def test_an_unknown_pass_number_is_refused(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    working_copy_id = _working_copy(conn, recording_id)
    transcript_id = _transcript(conn, recording_id, working_copy_id)
    with pytest.raises(sqlite3.IntegrityError):
        _segment(conn, transcript_id, asr_pass=3)


def test_an_active_segment_cannot_claim_a_replacement(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    """A half-failed merge must not leave a segment both current and superseded."""
    working_copy_id = _working_copy(conn, recording_id)
    transcript_id = _transcript(conn, recording_id, working_copy_id)
    first = _segment(conn, transcript_id, seq=0)
    with pytest.raises(sqlite3.IntegrityError):
        _segment(conn, transcript_id, seq=1, is_active=1, superseded_by_id=first)


def test_an_inactive_segment_may_point_at_its_replacement(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    working_copy_id = _working_copy(conn, recording_id)
    transcript_id = _transcript(conn, recording_id, working_copy_id)
    replacement = _segment(conn, transcript_id, seq=0, asr_pass=2)
    retired = _segment(conn, transcript_id, seq=1, is_active=0, superseded_by_id=replacement)
    row = conn.execute(
        "SELECT superseded_by_id FROM transcript_segments WHERE id = ?", (retired,)
    ).fetchone()
    assert row["superseded_by_id"] == replacement


def test_a_probability_outside_zero_to_one_is_refused(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    working_copy_id = _working_copy(conn, recording_id)
    transcript_id = _transcript(conn, recording_id, working_copy_id)
    segment_id = _segment(conn, transcript_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO transcript_words (segment_id, seq, start_ms, end_ms, text, "
            "probability) VALUES (?, 0, 0, 100, 'kata', 1.4)",
            (segment_id,),
        )


def test_words_cascade_with_their_segment(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    working_copy_id = _working_copy(conn, recording_id)
    transcript_id = _transcript(conn, recording_id, working_copy_id)
    segment_id = _segment(conn, transcript_id)
    conn.execute(
        "INSERT INTO transcript_words (segment_id, seq, start_ms, end_ms, text) "
        "VALUES (?, 0, 0, 100, 'kata')",
        (segment_id,),
    )
    conn.execute("DELETE FROM transcript_segments WHERE id = ?", (segment_id,))
    assert conn.execute("SELECT COUNT(*) FROM transcript_words").fetchone()[0] == 0


def test_deleting_a_recording_removes_the_whole_chain(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    working_copy_id = _working_copy(conn, recording_id)
    vad_run_id = _vad_run(conn, working_copy_id)
    conn.execute(
        "INSERT INTO speech_regions (vad_run_id, seq, start_ms, end_ms) VALUES (?, 0, 0, 1)",
        (vad_run_id,),
    )
    transcript_id = _transcript(conn, recording_id, working_copy_id)
    segment_id = _segment(conn, transcript_id)
    conn.execute(
        "INSERT INTO transcript_words (segment_id, seq, start_ms, end_ms, text) "
        "VALUES (?, 0, 0, 100, 'kata')",
        (segment_id,),
    )
    conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
    for table in (
        "audio_working_copies",
        "vad_runs",
        "speech_regions",
        "transcripts",
        "transcript_segments",
        "transcript_words",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
