"""Persistence for minutes. Same rules as the ASR store, for the same reasons.

Every function writes its state change and its audit event **in one transaction**, through
:func:`mom_igd.db.connection.maybe_transaction`. None of them opens a transaction directly.

**Revisions, not updates.** Re-running writes a new minute revision and deactivates the
previous one. The partial unique index in migration 0006 makes "at most one current" a
database guarantee, so two concurrent runs cannot both end up current.

**No minute text in an audit event.** Counts, identifiers, durations, model provenance --
never an item's text or quote. An audit row is read in support contexts where the content
of somebody's meeting has no business appearing, and a quote is verbatim speech.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Sequence

from mom_igd.audit import record_event
from mom_igd.db.connection import maybe_transaction
from mom_igd.logging_setup import get_logger

__all__ = [
    "MinuteStoreError",
    "activate_minute",
    "assign_document_number",
    "create_minute",
    "fail_minute",
    "get_active_minute",
    "get_minute",
    "list_minutes",
    "load_items",
    "record_export",
    "save_items",
    "update_minute",
]

_LOG = get_logger("mom.store")


class MinuteStoreError(RuntimeError):
    """A persistence precondition was not met. Never used to wrap a SQLite error."""


def _dumps(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _loads(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


def create_minute(
    conn: sqlite3.Connection,
    *,
    transcript_id: int,
    meeting_id: int,
    job_id: int | None,
    language: str = "id",
) -> int:
    """Open a new BUILDING revision. It becomes active only when it completes."""
    with maybe_transaction(conn):
        row = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) AS top FROM minutes WHERE transcript_id = ?",
            (transcript_id,),
        ).fetchone()
        revision = int((row["top"] if isinstance(row, sqlite3.Row) else row[0]) or 0) + 1
        cursor = conn.execute(
            """
            INSERT INTO minutes (
                transcript_id, meeting_id, job_id, revision, status, is_active, language
            ) VALUES (?, ?, ?, ?, 'BUILDING', 0, ?)
            """,
            (transcript_id, meeting_id, job_id, revision, language),
        )
        minute_id = int(cursor.lastrowid or 0)
        record_event(
            conn,
            category="JOB",
            action="mom.minute.opened",
            entity_type="minute",
            entity_id=minute_id,
            to_state="BUILDING",
            detail={
                "transcript_id": transcript_id,
                "meeting_id": meeting_id,
                "revision": revision,
                "job_id": job_id,
                "language": language,
            },
        )
    return minute_id


#: Columns :func:`update_minute` will write. A closed set, so a typo in a caller's keyword
#: raises instead of being silently ignored -- which is how a recorded model provenance
#: quietly becomes NULL and a document six months later cannot be traced to a model.
_UPDATABLE: frozenset[str] = frozenset(
    {
        "title",
        "summary_json",
        "summary_unsupported_numbers",
        "warnings_json",
        "language",
        "document_number",
        "document_seq",
        "model_name",
        "model_revision",
        "manifest_sha256",
        "quantisation",
        "context_tokens",
        "threads",
        "chunk_count",
        "chunks_failed",
        "covered_ms",
        "transcript_ms",
        "item_count",
        "verified_count",
        "unverified_count",
        "owners_dropped",
        "prompt_tokens",
        "completion_tokens",
        "model_ms",
        "total_ms",
        "peak_rss_bytes",
        "status",
        "last_error",
    }
)


def update_minute(conn: sqlite3.Connection, minute_id: int, **fields: Any) -> None:
    """Write named columns on a minute. Unknown names raise."""
    unknown = sorted(set(fields) - _UPDATABLE)
    if unknown:
        raise MinuteStoreError(
            f"cannot update minute column(s) {unknown}. Allowed: {sorted(_UPDATABLE)}."
        )
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with maybe_transaction(conn):
        conn.execute(
            f"UPDATE minutes SET {assignments}, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (*fields.values(), minute_id),
        )


def save_result(
    conn: sqlite3.Connection,
    *,
    minute_id: int,
    draft: Mapping[str, Any],
    stats: Mapping[str, Any],
    model: Mapping[str, Any],
) -> tuple[int, int]:
    """Write a whole generation result: header, items, provenance. Returns (items, verified).

    One transaction for the header and the items together. A minute whose header says
    fourteen items and whose item table holds nine is worse than no minute, because the
    discrepancy is invisible in every rendering of it.
    """
    with maybe_transaction(conn):
        update_minute(
            conn,
            minute_id,
            title=str(draft.get("title") or ""),
            summary_json=_dumps(list(draft.get("summary") or [])),
            summary_unsupported_numbers=_dumps(
                list(draft.get("summary_unsupported_numbers") or [])
            ),
            warnings_json=_dumps(list(draft.get("warnings") or [])),
            model_name=model.get("model_name"),
            model_revision=model.get("revision"),
            manifest_sha256=model.get("manifest_sha256"),
            quantisation=model.get("quantisation"),
            context_tokens=model.get("context_tokens"),
            threads=model.get("threads"),
            chunk_count=int(stats.get("chunk_count") or 0),
            chunks_failed=int(stats.get("chunks_failed") or 0),
            covered_ms=int(stats.get("covered_ms") or 0),
            transcript_ms=int(stats.get("transcript_ms") or 0),
            prompt_tokens=int(stats.get("prompt_tokens") or 0),
            completion_tokens=int(stats.get("completion_tokens") or 0),
            model_ms=int(float(stats.get("model_seconds") or 0.0) * 1000),
            total_ms=int(float(stats.get("total_seconds") or 0.0) * 1000),
        )
        return save_items(conn, minute_id=minute_id, items=draft.get("items") or ())


def save_items(
    conn: sqlite3.Connection,
    *,
    minute_id: int,
    items: Sequence[Mapping[str, Any]],
) -> tuple[int, int]:
    """Replace a revision's items. Returns (item count, verified count).

    Replacing rather than patching, for the same reason the transcript merge does: the
    generator produces the whole list, and re-writing it wholesale is what makes a re-run
    idempotent. Header counts are written from what was actually inserted, never from what
    the caller believed it was inserting.
    """
    with maybe_transaction(conn):
        conn.execute("DELETE FROM minute_items WHERE minute_id = ?", (minute_id,))
        verified = 0
        unverified = 0
        owners_dropped = 0
        rows = []
        for seq, item in enumerate(items):
            state = str(item.get("verification") or "UNVERIFIED")
            notes = list(item.get("verification_notes") or [])
            segment_ids = [int(value) for value in (item.get("segment_ids") or [])]
            if state == "UNVERIFIED":
                unverified += 1
            else:
                verified += 1
            if "OWNER_NOT_IN_TRANSCRIPT" in notes or "OWNER_NOT_A_NAME" in notes:
                owners_dropped += 1
            rows.append(
                (
                    minute_id,
                    seq,
                    str(item.get("kind") or ""),
                    str(item.get("text") or ""),
                    str(item.get("quote") or ""),
                    _dumps(segment_ids),
                    item.get("start_ms"),
                    item.get("end_ms"),
                    item.get("owner"),
                    item.get("due"),
                    state,
                    _dumps(notes),
                    int(item.get("merged_count") or 1),
                    max(0, int(item.get("chunk_index") or 0)),
                )
            )
        if rows:
            conn.executemany(
                """
                INSERT INTO minute_items (
                    minute_id, seq, kind, text, quote, segment_seqs, start_ms, end_ms,
                    owner, due_text, verification, verification_notes, merged_count,
                    chunk_index
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        update_minute(
            conn,
            minute_id,
            item_count=len(rows),
            verified_count=verified,
            unverified_count=unverified,
            owners_dropped=owners_dropped,
        )
    return len(rows), verified


def load_items(conn: sqlite3.Connection, *, minute_id: int) -> list[dict[str, Any]]:
    """Read a minute's items in order, with their JSON columns decoded."""
    out: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT * FROM minute_items WHERE minute_id = ? ORDER BY seq", (minute_id,)
    ):
        item = dict(row)
        item["segment_seqs"] = _loads(row["segment_seqs"], [])
        item["verification_notes"] = _loads(row["verification_notes"], [])
        out.append(item)
    return out


def assign_document_number(
    conn: sqlite3.Connection,
    *,
    minute_id: int,
    transcript_id: int,
    number_format: str,
    stamp: str,
) -> str | None:
    """Give a minute its filing reference, or inherit the one its predecessor has.

    ``stamp`` is the ISO date the reference is dated from, passed in rather than read
    from the clock here so the caller owns the time and a test can fix it.

    Two rules, and both are about the document being a physical thing somebody files:

    * **Revisions inherit.** Re-running the pipeline produces revision *n+1* of the same
      meeting's minute, not a new document. Renumbering it would leave the copy already
      in somebody's inbox pointing at a reference that no longer resolves.
    * **The number is assigned once.** If this minute already has one, it is returned
      unchanged. Nothing here recomputes an issued reference.

    Returns ``None`` when ``number_format`` is empty, which is how an operator turns
    filing references off.
    """
    if not number_format:
        return None

    with maybe_transaction(conn):
        existing = conn.execute(
            "SELECT document_number, document_seq FROM minutes WHERE id = ?",
            (minute_id,),
        ).fetchone()
        if existing is not None and existing["document_number"]:
            return str(existing["document_number"])

        inherited = conn.execute(
            "SELECT document_number, document_seq FROM minutes "
            "WHERE transcript_id = ? AND document_number IS NOT NULL "
            "ORDER BY revision DESC LIMIT 1",
            (transcript_id,),
        ).fetchone()
        if inherited is not None:
            number = str(inherited["document_number"])
            conn.execute(
                "UPDATE minutes SET document_number = ?, document_seq = ? WHERE id = ?",
                (number, inherited["document_seq"], minute_id),
            )
            return number

        year, month, day = stamp[:4], stamp[5:7], stamp[8:10]
        # Counted within the month, from the stored integer rather than by parsing the
        # rendered string: the format comes from configuration and may change between
        # one month and the next, and a parser would then start the count again at one.
        row = conn.execute(
            "SELECT COALESCE(MAX(document_seq), 0) AS top FROM minutes "
            "WHERE document_seq IS NOT NULL AND substr(created_at, 1, 7) = ?",
            (f"{year}-{month}",),
        ).fetchone()
        sequence = int(row["top"] or 0) + 1
        try:
            number = number_format.format(
                year=year, month=month, day=day, seq=sequence
            )
        except (IndexError, KeyError, ValueError) as exc:
            raise MinuteStoreError(
                f"document_number_format {number_format!r} could not be rendered "
                f"({exc}). Allowed placeholders: {{year}}, {{month}}, {{day}}, {{seq}}."
            ) from None

        conn.execute(
            "UPDATE minutes SET document_number = ?, document_seq = ? WHERE id = ?",
            (number, sequence, minute_id),
        )
        record_event(
            conn,
            category="JOB",
            action="mom.minute.numbered",
            entity_type="minute",
            entity_id=minute_id,
            detail={"document_number": number, "document_seq": sequence},
        )
    return number


def activate_minute(conn: sqlite3.Connection, *, minute_id: int) -> None:
    """Complete a revision and make it the current one, atomically."""
    with maybe_transaction(conn):
        row = conn.execute(
            "SELECT transcript_id, revision, status FROM minutes WHERE id = ?",
            (minute_id,),
        ).fetchone()
        if row is None:
            raise MinuteStoreError(f"minute {minute_id} does not exist")
        transcript_id = int(row["transcript_id"])
        conn.execute(
            "UPDATE minutes SET is_active = 0 "
            "WHERE transcript_id = ? AND is_active = 1 AND id <> ?",
            (transcript_id, minute_id),
        )
        conn.execute(
            "UPDATE minutes SET status = 'DRAFT', is_active = 1, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (minute_id,),
        )
        counts = conn.execute(
            "SELECT revision, item_count, verified_count, unverified_count, "
            "chunks_failed, covered_ms, transcript_ms FROM minutes WHERE id = ?",
            (minute_id,),
        ).fetchone()
        record_event(
            conn,
            category="JOB",
            action="mom.minute.completed",
            entity_type="minute",
            entity_id=minute_id,
            from_state=str(row["status"]),
            # DRAFT, never APPROVED. Approval is a human act this phase does not
            # implement, and an audit trail that recorded one would be false.
            to_state="DRAFT",
            detail={
                "transcript_id": transcript_id,
                "revision": int(counts["revision"]),
                "item_count": int(counts["item_count"]),
                "verified_count": int(counts["verified_count"]),
                "unverified_count": int(counts["unverified_count"]),
                "chunks_failed": int(counts["chunks_failed"]),
                "covered_ms": int(counts["covered_ms"]),
                "transcript_ms": int(counts["transcript_ms"]),
            },
        )


def fail_minute(
    conn: sqlite3.Connection, *, minute_id: int, error: str, cancelled: bool = False
) -> None:
    """Mark a revision failed or cancelled. It never becomes active.

    The message is truncated: an exception string from the model stack can contain a path,
    and a minute row is shown in the UI.
    """
    state = "CANCELLED" if cancelled else "FAILED"
    with maybe_transaction(conn):
        conn.execute(
            "UPDATE minutes SET status = ?, is_active = 0, last_error = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (state, str(error)[:300], minute_id),
        )
        record_event(
            conn,
            category="JOB",
            action="mom.minute.cancelled" if cancelled else "mom.minute.failed",
            entity_type="minute",
            entity_id=minute_id,
            to_state=state,
            detail={"error": str(error)[:300]},
        )


def get_minute(conn: sqlite3.Connection, *, minute_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM minutes WHERE id = ?", (minute_id,)).fetchone()


def get_active_minute(
    conn: sqlite3.Connection, *, transcript_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM minutes WHERE transcript_id = ? AND is_active = 1",
        (transcript_id,),
    ).fetchone()


def list_minutes(
    conn: sqlite3.Connection, *, meeting_id: int | None = None, limit: int = 50
) -> list[sqlite3.Row]:
    if meeting_id is None:
        return list(
            conn.execute(
                "SELECT * FROM minutes ORDER BY created_at DESC LIMIT ?", (int(limit),)
            )
        )
    return list(
        conn.execute(
            "SELECT * FROM minutes WHERE meeting_id = ? ORDER BY created_at DESC LIMIT ?",
            (meeting_id, int(limit)),
        )
    )


def record_export(
    conn: sqlite3.Connection,
    *,
    minute_id: int,
    export_format: str,
    relative_path: str,
    sha256: str,
    size_bytes: int,
    included_unverified: bool,
    include_evidence: bool,
) -> int:
    """Record a file written from a minute.

    An export leaves the application, so the row is what makes "which revision is this
    document, and did it contain unverified items?" answerable after the file has been
    forwarded. Re-exporting to the same path replaces the row rather than accumulating
    duplicates -- the file on disk was replaced too, and two rows describing one path,
    with different hashes, would be a record of something that never existed.
    """
    with maybe_transaction(conn):
        conn.execute(
            "DELETE FROM minute_exports WHERE relative_path = ?", (relative_path,)
        )
        cursor = conn.execute(
            """
            INSERT INTO minute_exports (
                minute_id, format, relative_path, sha256, size_bytes,
                included_unverified, include_evidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                minute_id,
                str(export_format),
                str(relative_path),
                str(sha256),
                int(size_bytes),
                1 if included_unverified else 0,
                1 if include_evidence else 0,
            ),
        )
        export_id = int(cursor.lastrowid or 0)
        record_event(
            conn,
            category="JOB",
            action="mom.minute.exported",
            entity_type="minute",
            entity_id=minute_id,
            detail={
                "export_id": export_id,
                "format": str(export_format),
                "relative_path": str(relative_path),
                "sha256": str(sha256),
                "size_bytes": int(size_bytes),
                "included_unverified": bool(included_unverified),
                "include_evidence": bool(include_evidence),
            },
        )
    return export_id


def list_exports(conn: sqlite3.Connection, *, minute_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM minute_exports WHERE minute_id = ? ORDER BY created_at DESC",
            (minute_id,),
        )
    )
