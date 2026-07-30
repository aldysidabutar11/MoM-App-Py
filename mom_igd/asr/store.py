"""Persistence for the ASR evidence chain: working copy, VAD run, transcript revisions.

Every function here writes its state change and its audit event **in one transaction**,
through :func:`mom_igd.db.connection.maybe_transaction`, so the two can never disagree.
None of them opens a transaction directly.

**Revisions, not updates.** A second run over the same recording writes a new transcript
revision and deactivates the previous one. Nothing rewrites a segment in place. The
partial unique indexes in migration 0005 make "at most one active" a database guarantee
rather than a convention, so two concurrent runs cannot both end up current.

**No transcript text in an audit event.** The audit trail records counts, identifiers,
durations and model provenance. Decoded speech goes in ``transcript_segments`` and nowhere
else -- an audit row is read in support contexts where a participant's words have no
business appearing.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Sequence

from mom_igd.audit import record_event
from mom_igd.db.connection import maybe_transaction
from mom_igd.logging_setup import get_logger

__all__ = [
    "StoreError",
    "activate_transcript",
    "create_transcript",
    "fail_transcript",
    "get_active_transcript",
    "get_active_vad_run",
    "get_working_copy",
    "list_regions",
    "load_segments",
    "save_segments",
    "save_vad_run",
    "save_working_copy",
    "update_transcript",
]

_LOG = get_logger("asr.store")


class StoreError(RuntimeError):
    """A persistence precondition was not met. Never used to wrap a SQLite error."""


def _dumps(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ===========================================================================
# Working copy
# ===========================================================================


def save_working_copy(
    conn: sqlite3.Connection,
    *,
    recording_id: int,
    payload: Mapping[str, Any],
) -> int:
    """Record the working copy for a recording, replacing any previous row.

    Replacing rather than versioning: a working copy is reproducible from the master, so
    keeping old rows would accumulate references to files that were overwritten anyway.
    The *transcripts* built from an earlier copy keep their own reference, and the copy's
    SHA-256 on the transcript row is what makes a stale one detectable.
    """
    with maybe_transaction(conn):
        row = conn.execute(
            "SELECT id FROM audio_working_copies WHERE recording_id = ?",
            (recording_id,),
        ).fetchone()
        values = (
            payload["relative_path"],
            payload["sha256"],
            payload["size_bytes"],
            payload["frames"],
            payload["duration_ms"],
            payload.get("source_manifest_sha256"),
            payload.get("source_chunk_count", 0),
            payload.get("source_sample_rate_hz"),
            payload.get("source_channels"),
            payload.get("source_frames", 0),
            payload.get("gap_count", 0),
            payload.get("gap_total_ms", 0),
            _dumps(payload.get("gaps") or []),
            payload.get("peak_dbfs"),
            payload.get("rms_dbfs"),
            payload.get("clipped_samples", 0),
            str(payload.get("status", "READY")),
        )
        if row is None:
            cursor = conn.execute(
                """
                INSERT INTO audio_working_copies (
                    recording_id, relative_path, sha256, size_bytes, frames,
                    duration_ms, source_manifest_sha256, source_chunk_count,
                    source_sample_rate_hz, source_channels, source_frames,
                    gap_count, gap_total_ms, gaps_json, peak_dbfs, rms_dbfs,
                    clipped_samples, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (recording_id, *values),
            )
            working_copy_id = int(cursor.lastrowid or 0)
        else:
            working_copy_id = int(row["id"] if isinstance(row, sqlite3.Row) else row[0])
            conn.execute(
                """
                UPDATE audio_working_copies
                   SET relative_path = ?, sha256 = ?, size_bytes = ?, frames = ?,
                       duration_ms = ?, source_manifest_sha256 = ?,
                       source_chunk_count = ?, source_sample_rate_hz = ?,
                       source_channels = ?, source_frames = ?, gap_count = ?,
                       gap_total_ms = ?, gaps_json = ?, peak_dbfs = ?, rms_dbfs = ?,
                       clipped_samples = ?, status = ?, last_error = NULL,
                       updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                 WHERE id = ?
                """,
                (*values, working_copy_id),
            )
        record_event(
            conn,
            category="JOB",
            action="asr.working_copy.saved",
            entity_type="recording",
            entity_id=recording_id,
            detail={
                "working_copy_id": working_copy_id,
                "duration_ms": payload["duration_ms"],
                "frames": payload["frames"],
                "sha256": payload["sha256"],
                "gap_count": payload.get("gap_count", 0),
                "gap_total_ms": payload.get("gap_total_ms", 0),
                "source_sample_rate_hz": payload.get("source_sample_rate_hz"),
                "source_channels": payload.get("source_channels"),
                "replaced": row is not None,
            },
        )
    return working_copy_id


def get_working_copy(
    conn: sqlite3.Connection, *, recording_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM audio_working_copies WHERE recording_id = ?", (recording_id,)
    ).fetchone()


# ===========================================================================
# VAD run and regions
# ===========================================================================


def save_vad_run(
    conn: sqlite3.Connection,
    *,
    working_copy_id: int,
    payload: Mapping[str, Any],
    regions: Sequence[Mapping[str, Any]],
    gaps: Sequence[Mapping[str, Any]] = (),
) -> int:
    """Insert a VAD run and its regions, deactivating the previous run.

    A region that overlaps a gap the normaliser filled with silence is flagged here
    rather than left for a reader to work out. Part of the audio under it is synthetic,
    and a reviewer has to be able to see that before trusting what was transcribed.
    """
    with maybe_transaction(conn):
        conn.execute(
            "UPDATE vad_runs SET is_active = 0 WHERE working_copy_id = ? AND is_active = 1",
            (working_copy_id,),
        )
        cursor = conn.execute(
            """
            INSERT INTO vad_runs (
                working_copy_id, model_name, model_sha256, config_hash, config_json,
                audio_ms, region_count, total_speech_ms, merged_count, split_count,
                dropped_short_count, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                working_copy_id,
                str(payload["model_name"]),
                str(payload["model_sha256"]),
                str(payload["config_hash"]),
                _dumps(payload.get("config") or {}),
                int(payload.get("audio_ms", 0)),
                len(regions),
                int(payload.get("total_speech_ms", 0)),
                int(payload.get("merged_count", 0)),
                int(payload.get("split_count", 0)),
                int(payload.get("dropped_short_count", 0)),
            ),
        )
        vad_run_id = int(cursor.lastrowid or 0)

        spans = [
            (int(gap["start_ms"]), int(gap["end_ms"]))
            for gap in gaps
            if int(gap.get("end_ms", 0)) > int(gap.get("start_ms", 0))
        ]
        rows = []
        overlapping = 0
        for seq, region in enumerate(regions):
            start_ms = int(region["start_ms"])
            end_ms = int(region["end_ms"])
            overlaps = any(start_ms < gap_end and end_ms > gap_start for gap_start, gap_end in spans)
            overlapping += 1 if overlaps else 0
            rows.append((vad_run_id, seq, start_ms, end_ms, 1 if overlaps else 0))
        if rows:
            conn.executemany(
                """
                INSERT INTO speech_regions (vad_run_id, seq, start_ms, end_ms, overlaps_gap)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )
        record_event(
            conn,
            category="JOB",
            action="asr.vad.saved",
            entity_type="working_copy",
            entity_id=working_copy_id,
            detail={
                "vad_run_id": vad_run_id,
                "region_count": len(regions),
                "total_speech_ms": int(payload.get("total_speech_ms", 0)),
                "regions_overlapping_a_filled_gap": overlapping,
                "model_name": str(payload["model_name"]),
                "model_sha256": str(payload["model_sha256"]),
                "config_hash": str(payload["config_hash"]),
            },
        )
    return vad_run_id


def get_active_vad_run(
    conn: sqlite3.Connection, *, working_copy_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM vad_runs WHERE working_copy_id = ? AND is_active = 1",
        (working_copy_id,),
    ).fetchone()


def list_regions(conn: sqlite3.Connection, *, vad_run_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM speech_regions WHERE vad_run_id = ? ORDER BY seq",
            (vad_run_id,),
        )
    )


# ===========================================================================
# Transcript revisions
# ===========================================================================


def create_transcript(
    conn: sqlite3.Connection,
    *,
    recording_id: int,
    working_copy_id: int,
    vad_run_id: int | None,
    job_id: int | None,
    language: str,
) -> int:
    """Open a new BUILDING revision. It becomes active only when it completes."""
    with maybe_transaction(conn):
        row = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) AS top FROM transcripts WHERE recording_id = ?",
            (recording_id,),
        ).fetchone()
        revision = int((row["top"] if isinstance(row, sqlite3.Row) else row[0]) or 0) + 1
        cursor = conn.execute(
            """
            INSERT INTO transcripts (
                recording_id, working_copy_id, vad_run_id, job_id, revision,
                status, is_active, language
            ) VALUES (?, ?, ?, ?, ?, 'BUILDING', 0, ?)
            """,
            (recording_id, working_copy_id, vad_run_id, job_id, revision, language),
        )
        transcript_id = int(cursor.lastrowid or 0)
        record_event(
            conn,
            category="JOB",
            action="asr.transcript.opened",
            entity_type="transcript",
            entity_id=transcript_id,
            to_state="BUILDING",
            detail={
                "recording_id": recording_id,
                "revision": revision,
                "working_copy_id": working_copy_id,
                "vad_run_id": vad_run_id,
                "job_id": job_id,
                "language": language,
            },
        )
    return transcript_id


#: Columns `update_transcript` will write. A closed set, so a typo in a caller's keyword
#: raises instead of being silently ignored -- which is how a recorded model provenance
#: quietly becomes NULL.
_UPDATABLE: frozenset[str] = frozenset(
    {
        "language",
        "language_probability",
        "pass1_model_name",
        "pass1_model_revision",
        "pass1_manifest_sha256",
        "pass1_compute_type",
        "pass1_beam_size",
        "pass1_cpu_threads",
        "pass2_model_name",
        "pass2_model_revision",
        "pass2_manifest_sha256",
        "pass2_compute_type",
        "pass2_beam_size",
        "pass2_cpu_threads",
        "pass2_budget_ms",
        "pass2_selected_ms",
        "pass2_region_count",
        "pass2_budget_exhausted",
        "pass2_skipped_reason",
        "glossary_version",
        "glossary_sha256",
        "glossary_replacements",
        "audio_ms",
        "speech_ms",
        "pass1_processing_ms",
        "pass2_processing_ms",
        "peak_rss_bytes",
        "segment_count",
        "word_count",
        "vad_run_id",
        "status",
        "last_error",
    }
)


def update_transcript(conn: sqlite3.Connection, transcript_id: int, **fields: Any) -> None:
    """Write named columns on a transcript. Unknown names raise."""
    unknown = sorted(set(fields) - _UPDATABLE)
    if unknown:
        raise StoreError(
            f"cannot update transcript column(s) {unknown}. Allowed: "
            f"{sorted(_UPDATABLE)}."
        )
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with maybe_transaction(conn):
        conn.execute(
            f"UPDATE transcripts SET {assignments}, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (*fields.values(), transcript_id),
        )


def save_segments(
    conn: sqlite3.Connection,
    *,
    transcript_id: int,
    segments: Sequence[Mapping[str, Any]],
    replace: bool = True,
) -> tuple[int, int]:
    """Write the segment list and its words. Returns (segments, words).

    ``replace`` deletes the revision's existing segments first, which is what makes the
    merge step idempotent: it re-writes the whole list rather than trying to patch rows.
    Words cascade with their segment, so nothing is orphaned.
    """
    with maybe_transaction(conn):
        if replace:
            conn.execute(
                "DELETE FROM transcript_segments WHERE transcript_id = ?", (transcript_id,)
            )
        word_total = 0
        # Two passes: every segment is inserted before any supersession pointer is set,
        # because a pass-1 segment can only point at a pass-2 row that already exists.
        ids: list[int] = []
        for seq, segment in enumerate(segments):
            words = segment.get("words") or ()
            probabilities = [
                float(word["probability"])
                for word in words
                if isinstance(word, Mapping) and word.get("probability") is not None
            ]
            cursor = conn.execute(
                """
                INSERT INTO transcript_segments (
                    transcript_id, seq, region_seq, asr_pass, start_ms, end_ms,
                    text, text_raw, avg_logprob, no_speech_prob, compression_ratio,
                    temperature, word_count, min_word_probability,
                    selected_for_pass2, pass2_reason_codes, pass2_rank, is_active,
                    glossary_replacements
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transcript_id,
                    seq,
                    segment.get("region_seq"),
                    int(segment.get("asr_pass", 1)),
                    int(segment["start_ms"]),
                    int(segment["end_ms"]),
                    str(segment.get("text") or ""),
                    str(segment.get("text_raw", segment.get("text") or "")),
                    segment.get("avg_logprob"),
                    segment.get("no_speech_prob"),
                    segment.get("compression_ratio"),
                    segment.get("temperature"),
                    len(words),
                    min(probabilities) if probabilities else None,
                    1 if segment.get("selected_for_pass2") else 0,
                    _dumps(list(segment.get("pass2_reason_codes") or []))
                    if segment.get("pass2_reason_codes")
                    else None,
                    segment.get("pass2_rank"),
                    1 if segment.get("is_active", True) else 0,
                    int(segment.get("glossary_replacements", 0) or 0),
                ),
            )
            segment_id = int(cursor.lastrowid or 0)
            ids.append(segment_id)
            if words:
                conn.executemany(
                    """
                    INSERT INTO transcript_words (
                        segment_id, seq, start_ms, end_ms, text, probability
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            segment_id,
                            index,
                            int(word["start_ms"]),
                            int(word["end_ms"]),
                            str(word.get("text") or ""),
                            word.get("probability"),
                        )
                        for index, word in enumerate(words)
                    ],
                )
                word_total += len(words)

        # Now the supersession pointers: an inactive pass-1 segment points at the
        # active pass-2 segment covering its region.
        replacements: dict[int, int] = {}
        for segment, segment_id in zip(segments, ids):
            if int(segment.get("asr_pass", 1)) == 2 and segment.get("region_seq") is not None:
                replacements.setdefault(int(segment["region_seq"]), segment_id)
        for segment, segment_id in zip(segments, ids):
            if segment.get("is_active", True):
                continue
            region = segment.get("region_seq")
            target = replacements.get(int(region)) if region is not None else None
            if target is not None:
                conn.execute(
                    "UPDATE transcript_segments SET superseded_by_id = ? WHERE id = ?",
                    (target, segment_id),
                )
    return len(segments), word_total


def load_segments(
    conn: sqlite3.Connection, *, transcript_id: int, active_only: bool = False
) -> list[dict[str, Any]]:
    """Read a revision's segments, each with its words, ordered by sequence."""
    clause = " AND is_active = 1" if active_only else ""
    rows = list(
        conn.execute(
            f"SELECT * FROM transcript_segments WHERE transcript_id = ?{clause} ORDER BY seq",
            (transcript_id,),
        )
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        segment = dict(row)
        segment["pass2_reason_codes"] = (
            json.loads(row["pass2_reason_codes"]) if row["pass2_reason_codes"] else []
        )
        segment["words"] = [
            dict(word)
            for word in conn.execute(
                "SELECT seq, start_ms, end_ms, text, probability FROM transcript_words "
                "WHERE segment_id = ? ORDER BY seq",
                (row["id"],),
            )
        ]
        out.append(segment)
    return out


def activate_transcript(conn: sqlite3.Connection, *, transcript_id: int) -> None:
    """Complete a revision and make it the current one, atomically.

    The previous active revision is deactivated in the same statement batch. The partial
    unique index means a race would raise rather than leave two current transcripts.
    """
    with maybe_transaction(conn):
        row = conn.execute(
            "SELECT recording_id, revision, status FROM transcripts WHERE id = ?",
            (transcript_id,),
        ).fetchone()
        if row is None:
            raise StoreError(f"transcript {transcript_id} does not exist")
        recording_id = int(row["recording_id"])
        conn.execute(
            "UPDATE transcripts SET is_active = 0 "
            "WHERE recording_id = ? AND is_active = 1 AND id <> ?",
            (recording_id, transcript_id),
        )
        conn.execute(
            "UPDATE transcripts SET status = 'COMPLETE', is_active = 1, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (transcript_id,),
        )
        counts = conn.execute(
            "SELECT segment_count, word_count, revision FROM transcripts WHERE id = ?",
            (transcript_id,),
        ).fetchone()
        record_event(
            conn,
            category="JOB",
            action="asr.transcript.completed",
            entity_type="transcript",
            entity_id=transcript_id,
            from_state=str(row["status"]),
            to_state="COMPLETE",
            detail={
                "recording_id": recording_id,
                "revision": int(counts["revision"]),
                "segment_count": int(counts["segment_count"]),
                "word_count": int(counts["word_count"]),
            },
        )


def fail_transcript(
    conn: sqlite3.Connection, *, transcript_id: int, error: str, cancelled: bool = False
) -> None:
    """Mark a revision failed or cancelled. It never becomes active.

    The message is truncated: an exception string from the ASR stack can contain an
    audio path, and a transcript row is shown in the UI.
    """
    state = "CANCELLED" if cancelled else "FAILED"
    with maybe_transaction(conn):
        conn.execute(
            "UPDATE transcripts SET status = ?, is_active = 0, last_error = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (state, str(error)[:300], transcript_id),
        )
        record_event(
            conn,
            category="JOB",
            action="asr.transcript.failed" if not cancelled else "asr.transcript.cancelled",
            entity_type="transcript",
            entity_id=transcript_id,
            to_state=state,
            detail={"error": str(error)[:300]},
        )


def get_active_transcript(
    conn: sqlite3.Connection, *, recording_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM transcripts WHERE recording_id = ? AND is_active = 1",
        (recording_id,),
    ).fetchone()
