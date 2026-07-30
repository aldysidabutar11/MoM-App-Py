"""Participant registry and biometric consent.

Two properties dominate this file:

* **Consent is append-only and derived.** No row is ever updated or deleted, and
  "is consent active?" is answered by the latest event, never by a flag.
* **A meeting's roster capacity is enforced in the transaction**, not in the UI, so
  two concurrent requests cannot both take the last slot. The capacity a meeting
  starts with is nine, which is what these tests exercise.

Everything runs against a real migrated database in a temporary data root.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid as uuid_module
from pathlib import Path

import pytest

from mom_igd.config import AppConfig
from mom_igd.db import initialize_database
from mom_igd.db.connection import connect
from mom_igd.enrollment.consent import (
    CONSENT_PURPOSE,
    CONSENT_TEXT_SHA256,
    CONSENT_TEXT_V1,
    CONSENT_VERSION,
    ConfirmationMethod,
    ConsentError,
    ConsentService,
    consent_text_sha256,
)
from mom_igd.enrollment.participants import (
    BASELINE_MEETING_CAPACITY,
    ParticipantError,
    ParticipantService,
)

# These tests describe the DEFAULT roster capacity, which is still nine. Capacity
# became per-meeting and configurable in the Phase 3 corrective pass, so the number
# is no longer a hard cap -- but a meeting nobody has reconfigured still holds nine,
# and that is what is asserted here. Capacity changes live in
# tests/test_participants_capacity.py.


@pytest.fixture
def db_path(config: AppConfig, paths) -> Path:
    initialize_database(
        paths.database_path(config.database.filename),
        busy_timeout_ms=config.database.busy_timeout_ms,
        app_version=config.app_version,
    )
    return paths.database_path(config.database.filename)


@pytest.fixture
def factory(db_path: Path, config: AppConfig):
    def _connect() -> sqlite3.Connection:
        return connect(db_path, busy_timeout_ms=config.database.busy_timeout_ms)

    return _connect


@pytest.fixture
def people(factory, config: AppConfig) -> ParticipantService:
    return ParticipantService(factory, config=config)


@pytest.fixture
def consent(factory) -> ConsentService:
    return ConsentService(factory)


def _meeting(factory, title: str = "Rapat uji") -> str:
    """Create a meeting and return its UUID."""
    meeting_uuid = str(uuid_module.uuid4())
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO meetings (title, uuid) VALUES (?, ?)", (title, meeting_uuid)
        )
        conn.commit()
    finally:
        conn.close()
    return meeting_uuid


def _pid(factory, participant_uuid: str) -> int:
    conn = factory()
    try:
        return int(
            conn.execute(
                "SELECT id FROM participants WHERE uuid = ?", (participant_uuid,)
            ).fetchone()["id"]
        )
    finally:
        conn.close()


# ============================================================== participants


def test_create_assigns_a_uuid_and_never_exposes_the_row_id(people) -> None:
    person = people.create(display_name="Budi Santoso", role="Ketua")
    assert person.uuid and len(person.uuid) == 36
    assert person.is_active is True
    payload = person.to_dict()
    assert "id" not in payload, "the autoincrement id is an internal detail"
    assert payload["uuid"] == person.uuid


def test_duplicate_display_names_are_allowed(people) -> None:
    """Two people genuinely share a name; identity is the UUID (ADR-0009)."""
    first = people.create(display_name="Budi")
    second = people.create(display_name="Budi")
    assert first.uuid != second.uuid
    listing = people.list(search="Budi")
    assert listing["total"] == 2


@pytest.mark.parametrize("bad", ["", "   ", "\t\n", None])
def test_a_blank_display_name_is_refused(people, bad) -> None:
    with pytest.raises(ParticipantError, match="display_name"):
        people.create(display_name=bad)


def test_an_over_long_display_name_is_refused(people) -> None:
    with pytest.raises(ParticipantError, match="at most"):
        people.create(display_name="x" * 121)


def test_control_characters_are_refused(people) -> None:
    """A newline in a name corrupts every log line and table that renders it."""
    with pytest.raises(ParticipantError, match="control characters"):
        people.create(display_name="Budi\x00Santoso")


def test_update_changes_labels_but_not_identity(people) -> None:
    person = people.create(display_name="Budi", role="Anggota")
    updated = people.update(person.uuid, display_name="Budi Santoso", role="Ketua")
    assert updated.uuid == person.uuid
    assert updated.display_name == "Budi Santoso"
    assert updated.role == "Ketua"


def test_deactivation_is_reversible_and_never_deletes(people, factory) -> None:
    person = people.create(display_name="Siti")
    people.set_active(person.uuid, active=False, reason="resigned")
    assert people.get(person.uuid).is_active is False
    # Still present, still visible in history.
    conn = factory()
    try:
        assert conn.execute("SELECT count(*) AS n FROM participants").fetchone()["n"] == 1
    finally:
        conn.close()
    people.set_active(person.uuid, active=True)
    assert people.get(person.uuid).is_active is True


def test_a_participant_in_history_cannot_be_hard_deleted(people, factory) -> None:
    """ON DELETE RESTRICT is what protects the meeting record."""
    person = people.create(display_name="Budi")
    meeting_uuid = _meeting(factory)
    people.add_to_meeting(meeting_uuid, person.uuid)
    conn = factory()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM participants WHERE uuid = ?", (person.uuid,))
            conn.commit()
    finally:
        conn.close()


def test_listing_is_paginated_and_bounded(people) -> None:
    for index in range(12):
        people.create(display_name=f"Orang {index:02d}")
    page = people.list(limit=5, offset=0)
    assert page["total"] == 12
    assert len(page["participants"]) == 5
    assert people.list(limit=10_000)["limit"] == 200, "limit must be clamped"


def test_search_escapes_like_wildcards(people) -> None:
    """A search for '%' must not turn into a pattern that matches everyone."""
    people.create(display_name="Budi")
    people.create(display_name="Siti")
    assert people.list(search="%")["total"] == 0
    assert people.list(search="_")["total"] == 0


# ======================================================= meeting membership


def test_a_deactivated_participant_cannot_join_a_meeting(people, factory) -> None:
    person = people.create(display_name="Budi")
    people.set_active(person.uuid, active=False)
    meeting_uuid = _meeting(factory)
    with pytest.raises(ParticipantError, match="deactivated"):
        people.add_to_meeting(meeting_uuid, person.uuid)


def test_deactivating_removes_active_memberships(people, factory) -> None:
    person = people.create(display_name="Budi")
    meeting_uuid = _meeting(factory)
    people.add_to_meeting(meeting_uuid, person.uuid)
    assert people.meeting_participants(meeting_uuid)["active_count"] == 1
    people.set_active(person.uuid, active=False)
    summary = people.meeting_participants(meeting_uuid)
    assert summary["active_count"] == 0
    # The row survives as history rather than vanishing.
    assert len(summary["participants"]) == 1
    assert summary["participants"][0]["membership_active"] is False


def test_adding_the_same_participant_twice_is_idempotent(people, factory) -> None:
    person = people.create(display_name="Budi")
    meeting_uuid = _meeting(factory)
    people.add_to_meeting(meeting_uuid, person.uuid)
    summary = people.add_to_meeting(meeting_uuid, person.uuid)
    assert summary["active_count"] == 1


def test_the_ninth_slot_is_the_last_one(people, factory) -> None:
    meeting_uuid = _meeting(factory)
    for index in range(BASELINE_MEETING_CAPACITY):
        person = people.create(display_name=f"Orang {index}")
        summary = people.add_to_meeting(meeting_uuid, person.uuid)
    assert summary["active_count"] == 9
    assert summary["slots_remaining"] == 0

    tenth = people.create(display_name="Orang kesepuluh")
    with pytest.raises(ParticipantError, match="roster capacity"):
        people.add_to_meeting(meeting_uuid, tenth.uuid)


def test_removing_someone_frees_a_slot(people, factory) -> None:
    meeting_uuid = _meeting(factory)
    created = []
    for index in range(BASELINE_MEETING_CAPACITY):
        created.append(people.create(display_name=f"Orang {index}"))
        people.add_to_meeting(meeting_uuid, created[-1].uuid)
    people.remove_from_meeting(meeting_uuid, created[0].uuid)
    assert people.meeting_participants(meeting_uuid)["active_count"] == 8
    tenth = people.create(display_name="Pengganti")
    assert people.add_to_meeting(meeting_uuid, tenth.uuid)["active_count"] == 9


def test_re_adding_a_removed_participant_reuses_the_row(people, factory) -> None:
    """The unique (meeting, participant) index must not be violated."""
    person = people.create(display_name="Budi")
    meeting_uuid = _meeting(factory)
    people.add_to_meeting(meeting_uuid, person.uuid)
    people.remove_from_meeting(meeting_uuid, person.uuid)
    summary = people.add_to_meeting(meeting_uuid, person.uuid, seat_label="kepala meja")
    assert summary["active_count"] == 1
    members = people.meeting_participants(meeting_uuid)["participants"]
    assert len(members) == 1
    assert members[0]["seat_label"] == "kepala meja"


def test_concurrent_adds_cannot_produce_a_tenth_participant(people, factory) -> None:
    """The cap is transactional, so a race cannot overshoot it.

    Eight seats are taken, then two threads race for the ninth and tenth. Exactly
    one must win; a check-then-insert implementation lets both through.
    """
    meeting_uuid = _meeting(factory)
    for index in range(BASELINE_MEETING_CAPACITY - 1):
        person = people.create(display_name=f"Orang {index}")
        people.add_to_meeting(meeting_uuid, person.uuid)

    contenders = [people.create(display_name=f"Perebut {i}") for i in range(2)]
    outcomes: list[str] = []
    barrier = threading.Barrier(len(contenders))

    def _try(candidate) -> None:
        barrier.wait()
        try:
            people.add_to_meeting(meeting_uuid, candidate.uuid)
            outcomes.append("added")
        except Exception as exc:  # noqa: BLE001 - the type is asserted below
            outcomes.append(type(exc).__name__)

    threads = [threading.Thread(target=_try, args=(c,)) for c in contenders]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert outcomes.count("added") == 1, outcomes
    final = people.meeting_participants(meeting_uuid)
    assert final["active_count"] == BASELINE_MEETING_CAPACITY


def test_an_unknown_meeting_or_participant_is_refused(people, factory) -> None:
    person = people.create(display_name="Budi")
    with pytest.raises(ParticipantError, match="No meeting"):
        people.add_to_meeting(str(uuid_module.uuid4()), person.uuid)
    with pytest.raises(ParticipantError, match="No participant"):
        people.add_to_meeting(_meeting(factory), str(uuid_module.uuid4()))


# ==================================================================== consent


def test_the_consent_text_is_hashed_over_normalised_line_endings() -> None:
    """core.autocrlf must not make every stored consent look superseded."""
    crlf = CONSENT_TEXT_V1.replace("\n", "\r\n")
    assert consent_text_sha256(crlf) == CONSENT_TEXT_SHA256
    assert len(CONSENT_TEXT_SHA256) == 64


def test_the_shipped_text_states_what_it_must(people) -> None:
    """The brief lists eight things consent has to disclose. Check each appears."""
    text = CONSENT_TEXT_V1.lower()
    for phrase in (
        "templat biometrik",       # voice becomes a biometric template
        "offline",                  # processing is local
        "tujuan",                   # purpose is limited
        "tidak ditulis ke penyimpanan",  # raw audio not retained
        "mencabut persetujuan",     # right to withdraw
        "dihapus",                  # effect of withdrawal on the voiceprint
        "historis tetap ada",       # historical meeting data is not auto-deleted
        "unknown",                  # speaker becomes UNKNOWN afterwards
    ):
        assert phrase in text, f"consent text does not mention: {phrase}"


def test_the_text_declares_itself_a_draft_pending_review(consent) -> None:
    """A false claim of legal compliance is worse than an honest gap."""
    bundle = consent.text_bundle()
    assert bundle["review_pending"] is True
    assert CONSENT_VERSION.endswith("-draft")
    assert "draf" in bundle["text"].lower()
    assert bundle["purpose"] == CONSENT_PURPOSE


def test_no_consent_means_no_enrollment(consent, people, factory) -> None:
    person = people.create(display_name="Budi")
    conn = factory()
    try:
        state = consent.state(conn, _pid(factory, person.uuid))
    finally:
        conn.close()
    assert state.active is False
    assert state.enrollment_allowed is False
    assert state.action is None


def test_granting_records_the_exact_version_and_hash(consent, people, factory) -> None:
    person = people.create(display_name="Budi")
    pid = _pid(factory, person.uuid)
    consent.grant(
        pid,
        confirmation_method=ConfirmationMethod.PARTICIPANT_CONFIRMED_ON_DEVICE,
        acknowledged_text_sha256=CONSENT_TEXT_SHA256,
    )
    conn = factory()
    try:
        state = consent.state(conn, pid)
    finally:
        conn.close()
    assert state.active is True
    assert state.enrollment_allowed is True
    assert state.consent_version == CONSENT_VERSION
    assert state.consent_text_sha256 == CONSENT_TEXT_SHA256
    assert state.purpose == CONSENT_PURPOSE
    assert state.text_matches_current is True


def test_consent_for_wording_that_was_not_shown_is_refused(consent, people, factory) -> None:
    """If the dialog and this module disagree, do not guess which was on screen."""
    person = people.create(display_name="Budi")
    with pytest.raises(ConsentError, match="does not match"):
        consent.grant(
            _pid(factory, person.uuid),
            confirmation_method=ConfirmationMethod.OPERATOR_CONFIRMED_IN_PERSON,
            acknowledged_text_sha256="a" * 64,
        )


def test_a_deactivated_participant_cannot_grant_consent(consent, people, factory) -> None:
    person = people.create(display_name="Budi")
    people.set_active(person.uuid, active=False)
    with pytest.raises(ConsentError, match="deactivated"):
        consent.grant(
            _pid(factory, person.uuid),
            confirmation_method=ConfirmationMethod.OPERATOR_CONFIRMED_IN_PERSON,
        )


def test_double_granting_is_idempotent(consent, people, factory) -> None:
    person = people.create(display_name="Budi")
    pid = _pid(factory, person.uuid)
    first = consent.grant(
        pid, confirmation_method=ConfirmationMethod.OPERATOR_CONFIRMED_IN_PERSON
    )
    second = consent.grant(
        pid, confirmation_method=ConfirmationMethod.OPERATOR_CONFIRMED_IN_PERSON
    )
    assert second["already_active"] is True
    assert second["event_uuid"] == first["event_uuid"]
    conn = factory()
    try:
        assert len(consent.history(conn, pid)) == 1, "no duplicate event"
    finally:
        conn.close()


def test_revoking_appends_rather_than_erasing(consent, people, factory) -> None:
    person = people.create(display_name="Budi")
    pid = _pid(factory, person.uuid)
    consent.grant(pid, confirmation_method=ConfirmationMethod.OPERATOR_CONFIRMED_IN_PERSON)
    consent.revoke(pid, reason="participant asked")

    conn = factory()
    try:
        state = consent.state(conn, pid)
        history = consent.history(conn, pid)
    finally:
        conn.close()
    assert state.active is False
    assert state.action.value == "REVOKED"
    # Both events survive: the grant is still on the record.
    assert [h["action"] for h in history] == ["REVOKED", "GRANTED"]


def test_double_revoking_is_idempotent(consent, people, factory) -> None:
    person = people.create(display_name="Budi")
    pid = _pid(factory, person.uuid)
    consent.grant(pid, confirmation_method=ConfirmationMethod.OPERATOR_CONFIRMED_IN_PERSON)
    consent.revoke(pid)
    again = consent.revoke(pid)
    assert again["already_revoked"] is True
    conn = factory()
    try:
        assert len(consent.history(conn, pid)) == 2
    finally:
        conn.close()


def test_revoking_without_consent_is_refused(consent, people, factory) -> None:
    person = people.create(display_name="Budi")
    with pytest.raises(ConsentError, match="no consent to revoke"):
        consent.revoke(_pid(factory, person.uuid))


def test_a_re_grant_produces_a_third_event(consent, people, factory) -> None:
    """Re-grant is permission to enrol again -- not a revival of what was deleted."""
    person = people.create(display_name="Budi")
    pid = _pid(factory, person.uuid)
    consent.grant(pid, confirmation_method=ConfirmationMethod.OPERATOR_CONFIRMED_IN_PERSON)
    consent.revoke(pid)
    consent.grant(pid, confirmation_method=ConfirmationMethod.OPERATOR_CONFIRMED_IN_PERSON)

    conn = factory()
    try:
        state = consent.state(conn, pid)
        history = consent.history(conn, pid)
    finally:
        conn.close()
    assert state.active is True
    assert [h["action"] for h in history] == ["GRANTED", "REVOKED", "GRANTED"]
    assert len({h["event_uuid"] for h in history}) == 3


def test_consent_history_carries_no_biometric_payload(consent, people, factory) -> None:
    person = people.create(display_name="Budi")
    pid = _pid(factory, person.uuid)
    consent.grant(pid, confirmation_method=ConfirmationMethod.OPERATOR_CONFIRMED_IN_PERSON)
    conn = factory()
    try:
        history = consent.history(conn, pid)
    finally:
        conn.close()
    blob = repr(history).lower()
    for forbidden in ("embedding", "centroid", "ciphertext", "vector", "nonce", "key"):
        assert forbidden not in blob, f"consent history leaked {forbidden}"


def test_audit_events_are_written_without_biometric_data(consent, people, factory) -> None:
    person = people.create(display_name="Budi")
    pid = _pid(factory, person.uuid)
    consent.grant(pid, confirmation_method=ConfirmationMethod.OPERATOR_CONFIRMED_IN_PERSON)
    consent.revoke(pid)
    conn = factory()
    try:
        rows = list(
            conn.execute(
                "SELECT action, category, detail_json FROM audit_events ORDER BY id"
            )
        )
    finally:
        conn.close()
    actions = [r["action"] for r in rows]
    assert "PARTICIPANT_CREATED" in actions
    assert "CONSENT_GRANTED" in actions
    assert "CONSENT_REVOKED" in actions
    blob = " ".join(str(r["detail_json"] or "") for r in rows).lower()
    for forbidden in ("embedding", "centroid", "ciphertext", "vector"):
        assert forbidden not in blob


def test_the_audit_chain_stays_valid_after_phase_3_writes(
    consent, people, factory, db_path: Path
) -> None:
    """Phase 3 must not break the Phase 1 hash chain."""
    from mom_igd.audit import verify_chain

    person = people.create(display_name="Budi")
    pid = _pid(factory, person.uuid)
    consent.grant(pid, confirmation_method=ConfirmationMethod.OPERATOR_CONFIRMED_IN_PERSON)
    people.update(person.uuid, role="Ketua")
    consent.revoke(pid)
    conn = factory()
    try:
        verify_chain(conn)
    finally:
        conn.close()
