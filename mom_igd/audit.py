"""Append-only, hash-chained audit trail.

Every event stores the hash of the previous event, so deleting or editing a row
in the middle of the history is detectable. The chain is computed over a
canonical JSON serialisation of the event's own fields plus ``prev_hash``.

This is an *integrity* mechanism, not confidentiality: it proves the log has not
been altered, it does not hide its contents. Encryption at rest is Phase 11.

Nothing secret is ever written here. In particular the session token, file
contents and personal data beyond an entity id must not appear in ``detail``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Final

from mom_igd.db.connection import maybe_transaction

__all__ = [
    "AUDIT_CATEGORIES",
    "AuditChainError",
    "record_event",
    "verify_chain",
]

AUDIT_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "APP",
        "DB",
        "SECURITY",
        "MEETING",
        "PARTICIPANT",
        "RECORDING",
        "JOB",
        "REVIEW",
        "EXPORT",
        "RETENTION",
    }
)
"""Must stay in sync with the CHECK constraint on ``audit_events.category``."""

_GENESIS_PREV_HASH: Final[str] = ""


class AuditChainError(RuntimeError):
    """Raised when the audit hash chain does not verify."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _canonical_payload(
    *,
    occurred_at: str,
    category: str,
    action: str,
    entity_type: str | None,
    entity_id: int | None,
    actor: str,
    from_state: str | None,
    to_state: str | None,
    detail_json: str | None,
    prev_hash: str,
) -> bytes:
    """Serialise an event deterministically for hashing."""
    return json.dumps(
        {
            "occurred_at": occurred_at,
            "category": category,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "actor": actor,
            "from_state": from_state,
            "to_state": to_state,
            "detail_json": detail_json,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _last_hash(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT event_hash FROM audit_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return _GENESIS_PREV_HASH
    return str(row["event_hash"])


def record_event(
    conn: sqlite3.Connection,
    *,
    category: str,
    action: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    actor: str = "system",
    from_state: str | None = None,
    to_state: str | None = None,
    detail: dict[str, Any] | None = None,
    occurred_at: str | None = None,
) -> int:
    """Append one event to the audit trail and return its row id.

    Safe to call inside a transaction the caller has already opened; in that
    case it joins that transaction instead of committing on its own, which is
    what makes a state transition and its audit record atomic.
    """
    if category not in AUDIT_CATEGORIES:
        raise ValueError(
            f"Unknown audit category {category!r}. Allowed: {sorted(AUDIT_CATEGORIES)}."
        )
    if not action or not action.strip():
        raise ValueError("Audit action must be a non-empty string.")

    stamp = occurred_at or _utc_now_iso()
    detail_json = (
        json.dumps(detail, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if detail is not None
        else None
    )

    with maybe_transaction(conn):
        prev_hash = _last_hash(conn)
        event_hash = _hash(
            _canonical_payload(
                occurred_at=stamp,
                category=category,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                actor=actor,
                from_state=from_state,
                to_state=to_state,
                detail_json=detail_json,
                prev_hash=prev_hash,
            )
        )
        cursor = conn.execute(
            "INSERT INTO audit_events ("
            "  occurred_at, category, action, entity_type, entity_id, actor,"
            "  from_state, to_state, detail_json, prev_hash, event_hash"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stamp,
                category,
                action.strip(),
                entity_type,
                entity_id,
                actor,
                from_state,
                to_state,
                detail_json,
                prev_hash,
                event_hash,
            ),
        )
        row_id = int(cursor.lastrowid or 0)
    return row_id


def verify_chain(conn: sqlite3.Connection) -> tuple[bool, int | None, str | None]:
    """Recompute the whole chain.

    Returns:
        ``(ok, first_bad_row_id, reason)``. ``ok`` is ``True`` and the other two
        are ``None`` when the chain verifies.
    """
    expected_prev = _GENESIS_PREV_HASH
    rows = conn.execute(
        "SELECT id, occurred_at, category, action, entity_type, entity_id, actor,"
        "       from_state, to_state, detail_json, prev_hash, event_hash"
        "  FROM audit_events ORDER BY id"
    ).fetchall()

    for row in rows:
        if str(row["prev_hash"] or "") != expected_prev:
            return (
                False,
                int(row["id"]),
                "prev_hash does not match the previous event's hash "
                "(a row was deleted, reordered or inserted)",
            )
        recomputed = _hash(
            _canonical_payload(
                occurred_at=str(row["occurred_at"]),
                category=str(row["category"]),
                action=str(row["action"]),
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                actor=str(row["actor"]),
                from_state=row["from_state"],
                to_state=row["to_state"],
                detail_json=row["detail_json"],
                prev_hash=str(row["prev_hash"] or ""),
            )
        )
        if recomputed != str(row["event_hash"]):
            return (False, int(row["id"]), "event contents were modified after insertion")
        expected_prev = str(row["event_hash"])

    return (True, None, None)


def assert_chain_intact(conn: sqlite3.Connection) -> None:
    """Raise :class:`AuditChainError` if the audit chain does not verify."""
    ok, row_id, reason = verify_chain(conn)
    if not ok:
        raise AuditChainError(f"Audit chain broken at audit_events.id={row_id}: {reason}")


def count_events(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()
    return int(row["n"])
