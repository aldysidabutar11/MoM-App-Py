"""`doctor`'s roster voiceprint coverage: identity-aware, not a count.

Two earlier versions of this check answered the wrong question. The first demanded a
hard-coded nine. The second used the largest configured roster **capacity** -- which
is a number of seats, so a meeting with capacity 15 and ten people on its roster was
reported as needing fifteen templates, inventing five attendees who do not exist.

Worse, both were *global counts*: fifteen voiceprints belonging to people who are not
on a meeting's roster would satisfy them while every actual attendee stayed
unrecognised.

Coverage is now computed per roster by joining each active member to that same
participant's own live voiceprint. A member counts as covered only when the
participant is active, the membership is active, their latest consent event is a
grant, and they own a voiceprint that is ``ACTIVE`` and ``production_eligible``.

Every test runs on a temporary data root. `doctor` must remain read-only throughout.
"""

from __future__ import annotations

import sqlite3
import uuid as uuid_module
from pathlib import Path

import pytest

from mom_igd.config import AppConfig
from mom_igd.db import initialize_database
from mom_igd.db.connection import connect
from mom_igd.diagnostics.enrollment_checks import enrollment_checks
from mom_igd.diagnostics.model import Status

MIGRATIONS = Path(__file__).resolve().parent.parent / "mom_igd" / "db" / "migrations"


@pytest.fixture
def migrated(config: AppConfig, paths) -> Path:
    database = paths.database_path(config.database.filename)
    initialize_database(
        database,
        busy_timeout_ms=config.database.busy_timeout_ms,
        app_version=config.app_version,
    )
    return database


@pytest.fixture
def db(migrated: Path, config: AppConfig):
    connection = connect(migrated, busy_timeout_ms=config.database.busy_timeout_ms)
    yield connection
    connection.commit()
    connection.close()


def _coverage(config: AppConfig, paths, *, production: bool = True):
    results = {r.key: r for r in enrollment_checks(config, paths, production=production)}
    return results["production_voiceprints"]


# --------------------------------------------------------------- fixture data


def _meeting(db: sqlite3.Connection, title: str, capacity: int) -> int:
    meeting_uuid = str(uuid_module.uuid4())
    db.execute(
        "INSERT INTO meetings (title, uuid, participant_capacity) VALUES (?,?,?)",
        (title, meeting_uuid, capacity),
    )
    return int(
        db.execute("SELECT id FROM meetings WHERE uuid = ?", (meeting_uuid,)).fetchone()[
            "id"
        ]
    )


def _person(db: sqlite3.Connection, name: str, *, active: bool = True) -> int:
    db.execute(
        "INSERT INTO participants (display_name, uuid, is_active) VALUES (?,?,?)",
        (name, str(uuid_module.uuid4()), 1 if active else 0),
    )
    return int(db.execute("SELECT max(id) AS id FROM participants").fetchone()["id"])


def _join(db: sqlite3.Connection, meeting_id: int, person_id: int, *, active=True) -> None:
    # removed_at must be >= added_at, so let SQLite stamp it rather than inventing a
    # date that violates the CHECK.
    db.execute(
        "INSERT INTO meeting_participants (meeting_id, participant_id, is_active,"
        " removed_at) VALUES (?,?,?,"
        " CASE WHEN ?=1 THEN NULL ELSE strftime('%Y-%m-%dT%H:%M:%fZ','now') END)",
        (meeting_id, person_id, 1 if active else 0, 1 if active else 0),
    )


def _consent(db: sqlite3.Connection, person_id: int, action: str = "GRANTED") -> None:
    db.execute(
        "INSERT INTO consent_events (participant_id, event_uuid, action, consent_version,"
        " consent_text_sha256, purpose, confirmation_method, actor) "
        "VALUES (?,?,?,'1.0-draft',?,'speaker-identification',"
        " 'PARTICIPANT_CONFIRMED_ON_DEVICE','local-operator')",
        (person_id, str(uuid_module.uuid4()), action, "b" * 64),
    )


#: Statuses the schema treats as dead. `voiceprints_dead_has_no_envelope` forbids an
#: envelope pointer on these, because revocation deletes the ciphertext.
DEAD_STATUSES = frozenset({"SUPERSEDED", "RE_ENROLL_REQUIRED", "REVOKED", "DELETE_PENDING"})


def _voiceprint(
    db: sqlite3.Connection, person_id: int, *, status: str = "ACTIVE", eligible: int = 1
) -> None:
    if status in DEAD_STATUSES:
        db.execute(
            "INSERT INTO voiceprints (voiceprint_uuid, participant_id, status,"
            " envelope_schema, cipher_suite, key_id, model_name, model_version,"
            " model_sha256, embedding_dim, sample_count, production_eligible) "
            "VALUES (?,?,?,1,'AES-256-GCM','k1','m','1',?,192,5,?)",
            (str(uuid_module.uuid4()), person_id, status, "d" * 64, eligible),
        )
        return
    db.execute(
        "INSERT INTO voiceprints (voiceprint_uuid, participant_id, status,"
        " envelope_relative_path, envelope_schema, envelope_sha256, envelope_bytes,"
        " cipher_suite, key_id, model_name, model_version, model_sha256,"
        " embedding_dim, sample_count, production_eligible) "
        "VALUES (?,?,?,?,1,?,100,'AES-256-GCM','k1','m','1',?,192,5,?)",
        (
            str(uuid_module.uuid4()),
            person_id,
            status,
            f"voiceprints/{uuid_module.uuid4()}.vpx",
            "c" * 64,
            "d" * 64,
            eligible,
        ),
    )


def _enrolled_member(db, meeting_id: int, name: str) -> int:
    person_id = _person(db, name)
    _join(db, meeting_id, person_id)
    _consent(db, person_id)
    _voiceprint(db, person_id)
    return person_id


# =============================================== the requirement is attendees


def test_a_fully_enrolled_roster_passes_regardless_of_spare_seats(
    config, paths, db
) -> None:
    """Capacity 15, roster 10, ten matching templates -> 10 of 10, PASS.

    The old check demanded fifteen here.
    """
    meeting = _meeting(db, "Rapat", 15)
    for index in range(10):
        _enrolled_member(db, meeting, f"Orang {index:02d}")
    db.commit()

    result = _coverage(config, paths)
    assert result.status is Status.PASS, result.detail
    roster = result.data["rosters"][0]
    assert roster["capacity"] == 15
    assert roster["roster_size"] == 10
    assert roster["covered"] == 10
    assert roster["missing"] == 0


def test_empty_seats_are_not_missing_voiceprints(config, paths, db) -> None:
    """Capacity 15, roster 10, nine matching -> exactly one missing, not six."""
    meeting = _meeting(db, "Rapat", 15)
    for index in range(9):
        _enrolled_member(db, meeting, f"Terdaftar {index}")
    naked = _person(db, "Belum enrol")
    _join(db, meeting, naked)
    _consent(db, naked)
    db.commit()

    result = _coverage(config, paths)
    assert result.status is Status.FAIL
    assert result.data["worst_roster"]["missing"] == 1, (
        "counting seats instead of members would report 6 missing"
    )
    assert result.data["worst_roster"]["roster_size"] == 10
    assert "seats, not attendees" in result.detail


def test_voiceprints_owned_by_non_members_do_not_count(config, paths, db) -> None:
    """The assertion a global count cannot survive."""
    meeting = _meeting(db, "Rapat", 15)
    for index in range(10):
        member = _person(db, f"Di roster {index}")
        _join(db, meeting, member)
        _consent(db, member)
    for index in range(10):  # strangers, fully enrolled
        outsider = _person(db, f"Di luar roster {index}")
        _consent(db, outsider)
        _voiceprint(db, outsider)
    db.commit()

    result = _coverage(config, paths)
    assert result.status is Status.FAIL, "ten global voiceprints must not satisfy this"
    assert result.data["worst_roster"]["covered"] == 0
    assert result.data["worst_roster"]["missing"] == 10
    registry = {
        r.key: r for r in enrollment_checks(config, paths, production=True)
    }["participant_registry"]
    assert registry.data["production_voiceprints"] == 10, (
        "the global total is still reported, it just does not decide the verdict"
    )


# ================================================== who counts as covered


def test_a_member_without_active_consent_is_not_covered(config, paths, db) -> None:
    meeting = _meeting(db, "Rapat", 9)

    never = _person(db, "Tidak pernah setuju")
    _join(db, meeting, never)
    _voiceprint(db, never)

    revoked = _person(db, "Sudah mencabut")
    _join(db, meeting, revoked)
    _consent(db, revoked, "GRANTED")
    _consent(db, revoked, "REVOKED")
    _voiceprint(db, revoked)
    db.commit()

    result = _coverage(config, paths)
    assert result.status is Status.FAIL
    assert result.data["worst_roster"]["covered"] == 0
    assert result.data["worst_roster"]["roster_size"] == 2


def test_an_inactive_participant_is_not_an_active_roster_member(config, paths, db) -> None:
    """A deactivated person must not appear as somebody needing a voiceprint.

    A stale membership row can still say ``is_active = 1``; the participant's own flag
    decides.
    """
    meeting = _meeting(db, "Rapat", 9)
    _enrolled_member(db, meeting, "Hadir")
    deactivated = _person(db, "Nonaktif", active=False)
    _join(db, meeting, deactivated)
    db.commit()

    result = _coverage(config, paths)
    assert result.status is Status.PASS, result.detail
    assert result.data["rosters"][0]["roster_size"] == 1


def test_a_removed_membership_is_not_counted(config, paths, db) -> None:
    meeting = _meeting(db, "Rapat", 9)
    _enrolled_member(db, meeting, "Hadir")
    removed = _person(db, "Sudah dikeluarkan")
    _join(db, meeting, removed, active=False)
    db.commit()

    result = _coverage(config, paths)
    assert result.status is Status.PASS, result.detail
    assert result.data["rosters"][0]["roster_size"] == 1


@pytest.mark.parametrize(
    ("status", "eligible"),
    [("DEVELOPMENT_ONLY", 0), ("SUPERSEDED", 0), ("REVOKED", 0), ("ACTIVE", 0)],
)
def test_only_a_live_production_eligible_voiceprint_counts(
    config, paths, db, status: str, eligible: int
) -> None:
    meeting = _meeting(db, "Rapat", 9)
    person = _person(db, "Punya template lemah")
    _join(db, meeting, person)
    _consent(db, person)
    _voiceprint(db, person, status=status, eligible=eligible)
    db.commit()

    result = _coverage(config, paths)
    assert result.status is Status.FAIL, f"{status}/eligible={eligible} must not count"
    assert result.data["worst_roster"]["covered"] == 0


# ======================================================= honest empty states


def test_an_empty_roster_asks_for_nothing(config, paths, db) -> None:
    _meeting(db, "Belum ada anggota", 15)
    db.commit()

    result = _coverage(config, paths)
    assert result.status is Status.WARN
    assert result.data["populated_rosters"] == 0
    assert "nothing to enrol" in result.detail
    # No fabricated number of any kind.
    for invented in ("of 9", "of 15", "9 production", "15 production"):
        assert invented not in result.detail, result.detail


def test_no_meeting_at_all_is_reported_honestly(config, paths, migrated) -> None:
    result = _coverage(config, paths)
    assert result.status is Status.WARN
    assert result.data["coverage_available"] is True
    assert result.data["meetings"] == 0
    assert "of 9" not in result.detail


def test_a_schema_3_database_reports_the_pending_migration_without_crashing(
    config: AppConfig, paths
) -> None:
    """`doctor` is the tool an operator runs *before* migrating."""
    from mom_igd.db.migrator import apply_migrations, discover_migrations

    older = [m for m in discover_migrations(MIGRATIONS) if m.version <= 3]
    connection = connect(paths.database_path(config.database.filename))
    try:
        apply_migrations(connection, older)
    finally:
        connection.close()

    result = _coverage(config, paths)
    assert result.status is Status.WARN
    assert result.data["coverage_available"] is False
    assert result.data["coverage_reason"] == "migration_0004_pending"
    assert "0004" in result.detail
    assert "db init" in result.detail


# ============================================ several rosters, and the worst one


def test_the_worst_roster_is_reported_and_complete_ones_are_not_blamed(
    config, paths, db
) -> None:
    good = _meeting(db, "Lengkap", 12)
    for index in range(3):
        _enrolled_member(db, good, f"Lengkap {index}")

    bad = _meeting(db, "Belum lengkap", 30)
    for index in range(2):
        _enrolled_member(db, bad, f"Sudah {index}")
    for index in range(3):
        pending = _person(db, f"Belum {index}")
        _join(db, bad, pending)
        _consent(db, pending)
    db.commit()

    result = _coverage(config, paths)
    assert result.status is Status.FAIL
    assert result.data["meetings"] == 2
    assert result.data["populated_rosters"] == 2
    assert result.data["incomplete_rosters"] == 1
    assert result.data["worst_roster"]["missing"] == 3
    assert result.data["worst_roster"]["roster_size"] == 5


def test_the_limitation_is_stated_rather_than_guessed(config, paths, db) -> None:
    """There is no upcoming/historical meeting state in the schema, so say so."""
    meeting = _meeting(db, "Rapat", 9)
    _enrolled_member(db, meeting, "Hadir")
    db.commit()

    result = _coverage(config, paths)
    assert "no single meeting is assumed to be the relevant one" in result.detail
    assert "per roster" in result.detail


# ================================================ privacy and read-only-ness


def test_no_display_name_or_meeting_title_reaches_the_report(config, paths, db) -> None:
    """A diagnostic gets pasted into tickets; a UUID is enough to act on."""
    meeting = _meeting(db, "Rapat Direksi Rahasia", 9)
    person = _person(db, "Budi Santoso")
    _join(db, meeting, person)
    _consent(db, person)
    db.commit()

    result = _coverage(config, paths)
    blob = result.detail + repr(result.data)
    for secret in ("Budi", "Santoso", "Direksi", "Rahasia"):
        assert secret not in blob, f"{secret!r} leaked into the diagnostic"
    # The meeting UUID is present, and that is what an operator needs.
    assert result.data["worst_roster"]["meeting_uuid"]


def test_computing_coverage_writes_nothing(config, paths, db) -> None:
    import hashlib

    meeting = _meeting(db, "Rapat", 9)
    _enrolled_member(db, meeting, "Hadir")
    db.commit()

    database = paths.database_path(config.database.filename)

    def snapshot() -> dict[str, str]:
        # `-shm` is shared-memory coordination state that SQLite rewrites on any WAL
        # read, and `-wal` grows on checkpoint. Neither is user data; hashing them
        # would assert that reading is a write. The database file is the evidence.
        return {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in database.parent.iterdir()
            if p.is_file() and p.suffix not in ("-shm", "-wal")
            and not p.name.endswith(("-shm", "-wal"))
        }

    before = snapshot()
    assert before, "the snapshot must actually cover the database file"
    for _ in range(3):
        enrollment_checks(config, paths, production=True)
    assert snapshot() == before, "doctor must not write to the database"
    assert list(paths.keys_dir.iterdir()) == [], "doctor must not create a key"
    assert list(paths.voiceprints_dir.iterdir()) == [], "doctor must not write a voiceprint"


def test_coverage_creates_no_participant_or_consent_row(config, paths, db) -> None:
    meeting = _meeting(db, "Rapat", 9)
    _enrolled_member(db, meeting, "Hadir")
    db.commit()

    def counts() -> dict[str, int]:
        connection = connect(paths.database_path(config.database.filename))
        try:
            return {
                table: int(
                    connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()[
                        "n"
                    ]
                )
                for table in (
                    "participants",
                    "consent_events",
                    "voiceprints",
                    "meeting_participants",
                    "meetings",
                    "audit_events",
                )
            }
        finally:
            connection.close()

    before = counts()
    enrollment_checks(config, paths, production=True)
    assert counts() == before
