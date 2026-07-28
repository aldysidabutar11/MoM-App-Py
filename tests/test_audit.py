"""Audit trail: append-only semantics and hash-chain tamper detection.

Covers Phase 1 test categories 24 and 25.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from mom_igd.audit import (
    AUDIT_CATEGORIES,
    AuditChainError,
    assert_chain_intact,
    count_events,
    record_event,
    verify_chain,
)


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall())


# ------------------------------------------------------------- basic writes


def test_recording_an_event_returns_its_id(conn: sqlite3.Connection) -> None:
    event_id = record_event(conn, category="APP", action="app.started")
    assert event_id > 0
    assert count_events(conn) == 1


def test_event_fields_are_stored_verbatim(conn: sqlite3.Connection) -> None:
    record_event(
        conn,
        category="MEETING",
        action="meeting.created",
        entity_type="meeting",
        entity_id=7,
        actor="operator",
        from_state=None,
        to_state="DRAFT",
        detail={"title": "Rapat mingguan"},
    )
    row = _rows(conn)[-1]
    assert row["category"] == "MEETING"
    assert row["action"] == "meeting.created"
    assert row["entity_type"] == "meeting"
    assert row["entity_id"] == 7
    assert row["actor"] == "operator"
    assert row["to_state"] == "DRAFT"
    assert json.loads(row["detail_json"]) == {"title": "Rapat mingguan"}
    assert row["occurred_at"].endswith("Z")


def test_unknown_category_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="Unknown audit category"):
        record_event(conn, category="GOSSIP", action="x")
    assert count_events(conn) == 0


def test_blank_action_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        record_event(conn, category="APP", action="   ")


def test_declared_categories_match_the_check_constraint(conn: sqlite3.Connection) -> None:
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_events'"
    ).fetchone()["sql"]
    for category in AUDIT_CATEGORIES:
        assert f"'{category}'" in sql


def test_every_declared_category_is_actually_insertable(conn: sqlite3.Connection) -> None:
    for category in sorted(AUDIT_CATEGORIES):
        record_event(conn, category=category, action=f"{category.lower()}.probe")
    assert count_events(conn) == len(AUDIT_CATEGORIES)
    assert verify_chain(conn)[0] is True


# --------------------------------------------------------------- 25. chain


def test_empty_chain_verifies(conn: sqlite3.Connection) -> None:
    assert verify_chain(conn) == (True, None, None)
    assert_chain_intact(conn)


def test_chain_links_each_event_to_its_predecessor(conn: sqlite3.Connection) -> None:
    for index in range(5):
        record_event(conn, category="APP", action=f"step.{index}")
    rows = _rows(conn)
    assert rows[0]["prev_hash"] == ""
    for previous, current in zip(rows[:-1], rows[1:], strict=True):
        assert current["prev_hash"] == previous["event_hash"]
    assert all(len(row["event_hash"]) == 64 for row in rows)
    assert verify_chain(conn)[0] is True


def test_hashes_are_unique_even_for_identical_payloads(conn: sqlite3.Connection) -> None:
    record_event(conn, category="APP", action="same", occurred_at="2026-01-01T00:00:00.000Z")
    record_event(conn, category="APP", action="same", occurred_at="2026-01-01T00:00:00.000Z")
    rows = _rows(conn)
    assert rows[0]["event_hash"] != rows[1]["event_hash"], "the chain must differentiate them"


def test_editing_an_event_body_is_detected(conn: sqlite3.Connection) -> None:
    for index in range(4):
        record_event(conn, category="JOB", action=f"job.step{index}", entity_id=index)
    target = _rows(conn)[1]["id"]

    conn.execute("UPDATE audit_events SET action = 'job.tampered' WHERE id = ?", (target,))

    ok, bad_id, reason = verify_chain(conn)
    assert ok is False
    assert bad_id == target
    assert "modified" in reason
    with pytest.raises(AuditChainError, match=f"id={target}"):
        assert_chain_intact(conn)


def test_editing_the_actor_is_detected(conn: sqlite3.Connection) -> None:
    record_event(conn, category="REVIEW", action="mom.approved", actor="reviewer-a")
    conn.execute("UPDATE audit_events SET actor = 'reviewer-b'")
    assert verify_chain(conn)[0] is False


def test_deleting_a_middle_event_is_detected(conn: sqlite3.Connection) -> None:
    for index in range(5):
        record_event(conn, category="APP", action=f"step.{index}")
    victim = _rows(conn)[2]["id"]

    conn.execute("DELETE FROM audit_events WHERE id = ?", (victim,))

    ok, bad_id, reason = verify_chain(conn)
    assert ok is False
    assert "prev_hash" in reason
    assert bad_id is not None


def test_deleting_the_last_event_is_not_detectable_by_the_chain_alone(
    conn: sqlite3.Connection,
) -> None:
    """Honest documentation of the mechanism's limit.

    A hash chain detects modification and mid-history deletion. Truncating the
    tail keeps the remaining chain internally consistent, so detecting it needs
    an external anchor (a signed high-water mark). That is Phase 11 work; the
    limitation is asserted here so it cannot be mistaken for a bug later.
    """
    for index in range(3):
        record_event(conn, category="APP", action=f"step.{index}")
    conn.execute("DELETE FROM audit_events WHERE id = (SELECT MAX(id) FROM audit_events)")
    assert verify_chain(conn)[0] is True


def test_appending_after_tampering_does_not_repair_the_chain(conn: sqlite3.Connection) -> None:
    record_event(conn, category="APP", action="first")
    record_event(conn, category="APP", action="second")
    conn.execute("UPDATE audit_events SET action='edited' WHERE id = 1")
    record_event(conn, category="APP", action="third")
    assert verify_chain(conn)[0] is False


# ------------------------------------- transaction participation (atomicity)


def test_record_event_joins_an_open_transaction(conn: sqlite3.Connection) -> None:
    from mom_igd.db.connection import maybe_transaction

    with pytest.raises(RuntimeError):
        with maybe_transaction(conn):
            record_event(conn, category="APP", action="inside.transaction")
            assert conn.in_transaction
            raise RuntimeError("boom")
    assert count_events(conn) == 0, "the audit write must roll back with its caller"


def test_record_event_opens_its_own_transaction_when_needed(conn: sqlite3.Connection) -> None:
    assert not conn.in_transaction
    record_event(conn, category="APP", action="standalone")
    assert not conn.in_transaction
    assert count_events(conn) == 1


def test_chain_stays_valid_across_a_rollback(conn: sqlite3.Connection) -> None:
    from mom_igd.db.connection import maybe_transaction

    record_event(conn, category="APP", action="kept")
    try:
        with maybe_transaction(conn):
            record_event(conn, category="APP", action="discarded")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    record_event(conn, category="APP", action="kept-too")
    ok, bad_id, reason = verify_chain(conn)
    assert ok is True, f"chain broken at {bad_id}: {reason}"
    assert [row["action"] for row in _rows(conn)] == ["kept", "kept-too"]


def test_detail_is_canonicalised_deterministically(conn: sqlite3.Connection) -> None:
    record_event(
        conn, category="APP", action="a", detail={"b": 2, "a": 1}, occurred_at="2026-01-01T00:00:00.000Z"
    )
    assert _rows(conn)[0]["detail_json"] == '{"a":1,"b":2}'
