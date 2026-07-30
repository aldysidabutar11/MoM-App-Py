"""The transcription service: one run at a time, and the reads the UI needs.

**One heavy run at a time, enforced in this process.** ``resources.max_heavy_workers`` is
1 and configuration validation refuses anything else (ADR-0004), so the service refuses a
second concurrent run rather than queueing it. Refusing is the honest answer: a queued run
would sit invisible while the operator wonders whether they pressed the button.

The measured reason is in ``docs/benchmarks.md`` -- the two models' worst-case working sets
total 2 603 MiB against a 2.5 GB budget, so two runs in parallel would breach it.

**Recording is never blocked by transcription, and the reverse is also true.** Capture owns
the microphone and a lock file; this owns a worker slot. Neither imports the other, and
nothing here can prevent a recording from starting -- an operator must always be able to
record the next meeting while the last one is still being transcribed. That is why the
guard here is a *worker* guard and not a global one.

**Reads carry no filesystem path.** A transcript is addressed by recording UUID. The client
never learns where the audio or the working copy lives.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Callable, Final

from mom_igd.logging_setup import get_logger

__all__ = [
    "ACTIVE_CAPTURE_STATES",
    "AsrBusyError",
    "AsrService",
    "AsrServiceError",
    "RecordingInProgressError",
    "TranscriptionHandle",
]

_LOG = get_logger("asr.service")


class AsrServiceError(RuntimeError):
    """A request the service cannot satisfy. Carries an operator-facing message."""


class AsrBusyError(AsrServiceError):
    """A heavy run is already in flight. Exactly one is permitted."""


class RecordingInProgressError(AsrServiceError):
    """A capture is live. Transcription must not compete with it for the machine."""


#: Capture states that mean a microphone is open or a recording is being finalised.
#: Must match the partial unique index in migration 0002 that enforces one active
#: recording across the data root.
ACTIVE_CAPTURE_STATES: Final[tuple[str, ...]] = (
    "PREFLIGHT",
    "ARMED",
    "RECORDING",
    "PAUSED",
    "STOPPING",
    "FINALIZING",
)


@dataclass(slots=True)
class TranscriptionHandle:
    """A run in flight, or the last one that finished."""

    recording_uuid: str
    started_at: float
    finished: bool = False
    result: Any = None
    error: str | None = None
    messages: list[str] | None = None
    cancel_requested: bool = False


class AsrService:
    """Owns the single worker slot and answers transcript queries."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        *,
        config: Any,
        paths: Any,
    ) -> None:
        if config is None:
            raise AsrServiceError(
                "AsrService requires config=. Without it the pipeline would fall back "
                "to built-in thread counts and thresholds, and two runtimes would "
                "disagree about the same policy."
            )
        self._connect = connect
        self._config = config
        self._paths = paths
        self._lock: Final[threading.Lock] = threading.Lock()
        self._current: TranscriptionHandle | None = None
        self._last: TranscriptionHandle | None = None

    # -- state --------------------------------------------------------------

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._current is not None and not self._current.finished

    def status(self) -> dict[str, Any]:
        """What the operator's status poll needs. No path, no transcript text."""
        with self._lock:
            current = self._current
            last = self._last
        models = self._model_status()
        return {
            "busy": bool(current is not None and not current.finished),
            "running_recording_uuid": current.recording_uuid if current else None,
            "cancel_requested": bool(current.cancel_requested) if current else False,
            "last_result": (last.result.to_dict() if last and last.result else None),
            "last_error": last.error if last else None,
            "models": models,
            "pass2_enabled": bool(self._config.asr.pass2_enabled),
            "pass2_budget_ratio": float(self._config.asr.pass2_budget_ratio),
        }

    def _model_status(self) -> dict[str, Any]:
        """Which models are ready, without loading one.

        Answers the question the UI must ask before offering a Transcribe button, and it
        answers it from the readiness index rather than a directory scan -- a model that
        verifies but failed its load probe is not ready (ADR-0015).
        """
        from mom_igd.asr.installed import load_index

        index = load_index(self._paths.models_dir)
        ready = {entry.role: entry for entry in index.ready(self._paths.models_dir)}
        return {
            "readable_index": index.readable,
            "problem": index.problem,
            "pass1_ready": "pass1" in ready,
            "pass2_ready": "pass2" in ready,
            "pass1_model": ready["pass1"].model_name if "pass1" in ready else None,
            "pass2_model": ready["pass2"].model_name if "pass2" in ready else None,
        }

    # -- what can be transcribed --------------------------------------------

    def active_capture(self) -> str | None:
        """The UUID of a recording that is currently capturing, if any.

        Asked with SQL rather than by importing the capture service: nothing under
        `mom_igd/asr/` may depend on `mom_igd.audio`, and this needs one fact, not a
        service. The state list mirrors migration 0002's partial unique index.
        """
        placeholders = ", ".join("?" for _ in ACTIVE_CAPTURE_STATES)
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT recording_uuid FROM recordings WHERE status IN ({placeholders}) "
                "LIMIT 1",
                ACTIVE_CAPTURE_STATES,
            ).fetchone()
        finally:
            conn.close()
        return str(row["recording_uuid"]) if row is not None else None

    def list_transcribable(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Closed recordings, newest first, each with its transcript state.

        The panel needs this so an operator picks a recording from a list rather than
        typing a UUID: a typed identifier is a way to get a 404 and no way to discover
        what exists. `eligible` and `reason` are computed here rather than in JavaScript,
        so the button's enabled state and the explanation cannot disagree.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT r.recording_uuid, r.status, r.duration_ms, r.chunk_count,
                       r.manifest_status, r.degraded, r.created_at,
                       m.title AS meeting_title,
                       t.revision AS transcript_revision,
                       t.status  AS transcript_status,
                       t.segment_count, t.word_count, t.pass2_skipped_reason,
                       (SELECT COUNT(*) FROM transcripts a WHERE a.recording_id = r.id)
                           AS revision_count
                  FROM recordings r
                  JOIN meetings m ON m.id = r.meeting_id
                  LEFT JOIN transcripts t
                         ON t.recording_id = r.id AND t.is_active = 1
                 WHERE r.status = 'RECORDED'
                 ORDER BY r.id DESC
                 LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        finally:
            conn.close()

        models = self._model_status()
        busy_with = self.active_capture()
        # Two kinds of blocker, and the specific one wins. "This recording has no audio"
        # is about the recording and will never be fixed by provisioning a model;
        # "no model" and "a capture is running" are about the machine and apply to every
        # row. Reporting the global one first hid the row-specific ones entirely.
        global_reason: str | None = None
        if not models["pass1_ready"]:
            global_reason = "MODEL_UNAVAILABLE"
        elif busy_with is not None:
            global_reason = "RECORDING_IN_PROGRESS"

        out: list[dict[str, Any]] = []
        for row in rows:
            reason: str | None = None
            if int(row["chunk_count"] or 0) <= 0:
                reason = "NO_AUDIO"
            elif global_reason is not None:
                reason = global_reason
            out.append(
                {
                    "recording_uuid": row["recording_uuid"],
                    "meeting_title": row["meeting_title"],
                    "duration_ms": int(row["duration_ms"] or 0),
                    "chunk_count": int(row["chunk_count"] or 0),
                    "manifest_status": row["manifest_status"],
                    "degraded": bool(row["degraded"]),
                    "created_at": row["created_at"],
                    "transcript_revision": row["transcript_revision"],
                    "transcript_status": row["transcript_status"],
                    "revision_count": int(row["revision_count"] or 0),
                    "segment_count": row["segment_count"],
                    "word_count": row["word_count"],
                    "pass2_skipped_reason": row["pass2_skipped_reason"],
                    "eligible": reason is None,
                    "ineligible_reason": reason,
                }
            )
        return out

    def preflight(self, recording_uuid: str | None = None) -> dict[str, Any]:
        """Everything that must be true before a run, checked without loading a model.

        Deliberately separate from `transcribe`: an operator who is told *before* pressing
        the button that no model is provisioned has a problem they can fix, while one told
        five seconds into a run has a failure to interpret.
        """
        import shutil

        models = self._model_status()
        capture = self.active_capture()
        checks: list[dict[str, Any]] = [
            {
                "key": "model_pass1",
                "ok": bool(models["pass1_ready"]),
                "detail": (
                    f"pass 1 ready: {models['pass1_model']}"
                    if models["pass1_ready"]
                    else "no pass-1 model is provisioned. Run "
                    "`python -m mom_igd asr provision asr-pass1` once, with network "
                    "access. Transcription never downloads a model by itself."
                ),
                "blocking": True,
            },
            {
                "key": "model_pass2",
                "ok": bool(models["pass2_ready"]),
                "detail": (
                    f"pass 2 ready: {models['pass2_model']}"
                    if models["pass2_ready"]
                    else "no pass-2 model. The run will complete on the first pass and "
                    "record PASS2_MODEL_UNAVAILABLE."
                ),
                "blocking": False,
            },
            {
                "key": "no_active_recording",
                "ok": capture is None,
                "detail": (
                    "no capture is in progress"
                    if capture is None
                    else "a recording is in progress. Transcription would compete with "
                    "it for CPU and disk, and a recording must never be put at risk by "
                    "post-processing. Stop the recording first."
                ),
                "blocking": True,
            },
            {
                "key": "worker_slot",
                "ok": not self.busy,
                "detail": (
                    "the heavy worker slot is free"
                    if not self.busy
                    else "another transcription is running. Exactly one heavy model may "
                    "be resident at a time."
                ),
                "blocking": True,
            },
        ]

        try:
            free_gb = shutil.disk_usage(self._paths.root).free / (1 << 30)
        except OSError as exc:  # pragma: no cover - unreadable data root
            checks.append(
                {
                    "key": "disk",
                    "ok": False,
                    "detail": f"the data root could not be measured: {exc}",
                    "blocking": True,
                }
            )
        else:
            # The working copy is 16 kHz mono PCM16: about 115 MB per hour. Two
            # gigabytes is generous for a long meeting plus the database.
            checks.append(
                {
                    "key": "disk",
                    "ok": free_gb >= 2.0,
                    "detail": (
                        f"{free_gb:.1f} GB free in the data root"
                        + ("" if free_gb >= 2.0 else " -- below the 2.0 GB minimum")
                    ),
                    "blocking": free_gb < 2.0,
                }
            )

        if recording_uuid is not None:
            entry = next(
                (
                    item
                    for item in self.list_transcribable(limit=500)
                    if item["recording_uuid"] == recording_uuid
                ),
                None,
            )
            checks.append(
                {
                    "key": "recording",
                    "ok": entry is not None,
                    "detail": (
                        f"{entry['chunk_count']} chunk(s), "
                        f"{entry['duration_ms'] / 1000:.0f}s, manifest "
                        f"{entry['manifest_status']}"
                        if entry is not None
                        else "this recording is not a closed recording with audio. Only "
                        "a RECORDED recording can be transcribed."
                    ),
                    "blocking": entry is None,
                }
            )

        blocking = [check for check in checks if check["blocking"] and not check["ok"]]
        return {
            "ok": not blocking,
            "recording_uuid": recording_uuid,
            "checks": checks,
            "blocking_count": len(blocking),
        }

    def request_cancel(self) -> bool:
        """Ask the running pipeline to stop at its next boundary."""
        with self._lock:
            if self._current is None or self._current.finished:
                return False
            self._current.cancel_requested = True
        _LOG.info("asr.service.cancel_requested")
        return True

    # -- the run ------------------------------------------------------------

    def transcribe(
        self,
        recording_uuid: str,
        *,
        job_id: int | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> Any:
        """Run the pipeline synchronously. Raises :class:`AsrBusyError` if one is running.

        Synchronous by design: the heavy work is already in a separate process, and the
        caller (a CLI command, or a threadpool worker behind an endpoint) is the right
        place to decide about waiting.
        """
        import time

        from mom_igd.asr.pipeline import TranscriptionPipeline

        if self._config.resources.max_heavy_workers != 1:
            raise AsrServiceError(
                "resources.max_heavy_workers must be 1. Configuration validation "
                "normally rejects anything else."
            )

        # Checked here rather than only in preflight, because preflight is advice and
        # this is the gate. A recording must never be put at risk by post-processing
        # competing for CPU and disk -- and the operator can always record the next
        # meeting while an earlier one transcribes, which is why the guard is on the
        # capture side of the pair and not the reverse.
        capturing = self.active_capture()
        if capturing is not None:
            raise RecordingInProgressError(
                f"recording {capturing} is in progress, so transcription will not start. "
                "Stop the recording first. Transcription is deliberately never allowed "
                "to compete with a live capture."
            )

        handle = TranscriptionHandle(
            recording_uuid=recording_uuid, started_at=time.time(), messages=[]
        )
        with self._lock:
            if self._current is not None and not self._current.finished:
                raise AsrBusyError(
                    f"a transcription of {self._current.recording_uuid} is already "
                    "running, and exactly one heavy model may be resident at a time. "
                    "Wait for it to finish, or cancel it first."
                )
            self._current = handle

        def say(message: str) -> None:
            if handle.messages is not None and len(handle.messages) < 200:
                handle.messages.append(message)
            if progress:
                progress(message)

        pipeline = TranscriptionPipeline(
            config=self._config,
            paths=self._paths,
            connect=self._connect,
            progress=say,
            should_cancel=lambda: handle.cancel_requested,
        )
        try:
            result = pipeline.run(recording_uuid, job_id=job_id)
            handle.result = result
            handle.error = result.error
            return result
        finally:
            handle.finished = True
            with self._lock:
                self._last = handle
                self._current = None

    # -- reads --------------------------------------------------------------

    def get_transcript(
        self, recording_uuid: str, *, revision: int | None = None
    ) -> dict[str, Any]:
        """Return one transcript revision with its active segments.

        Active segments only: a superseded pass-1 segment is evidence, not something a
        reader should see twice. ``/asr/segments/{uuid}?include_superseded=1`` is the
        route that shows the rest.
        """
        conn = self._connect()
        try:
            row = self._transcript_row(conn, recording_uuid, revision)
            from mom_igd.asr.store import load_segments

            segments = load_segments(
                conn, transcript_id=int(row["id"]), active_only=True
            )
            return {
                "recording_uuid": recording_uuid,
                "transcript": _public_transcript(row),
                "segments": [_public_segment(segment) for segment in segments],
            }
        finally:
            conn.close()

    def list_revisions(self, recording_uuid: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            recording = conn.execute(
                "SELECT id FROM recordings WHERE recording_uuid = ?", (recording_uuid,)
            ).fetchone()
            if recording is None:
                raise AsrServiceError(f"no recording with uuid {recording_uuid}")
            return [
                _public_transcript(row)
                for row in conn.execute(
                    "SELECT * FROM transcripts WHERE recording_id = ? "
                    "ORDER BY revision DESC",
                    (int(recording["id"]),),
                )
            ]
        finally:
            conn.close()

    def flagged_regions(self, recording_uuid: str) -> list[dict[str, Any]]:
        """Which regions a selection rule fired on, and whether pass 2 reached them."""
        conn = self._connect()
        try:
            row = self._transcript_row(conn, recording_uuid, None)
            rows = conn.execute(
                "SELECT seq, region_seq, asr_pass, start_ms, end_ms, "
                "selected_for_pass2, pass2_reason_codes, pass2_rank, is_active "
                "FROM transcript_segments WHERE transcript_id = ? "
                "AND pass2_reason_codes IS NOT NULL ORDER BY seq",
                (int(row["id"]),),
            ).fetchall()
            import json

            return [
                {
                    "seq": int(item["seq"]),
                    "region_seq": item["region_seq"],
                    "asr_pass": int(item["asr_pass"]),
                    "start_ms": int(item["start_ms"]),
                    "end_ms": int(item["end_ms"]),
                    "selected_for_pass2": bool(item["selected_for_pass2"]),
                    "reason_codes": json.loads(item["pass2_reason_codes"] or "[]"),
                    "rank": item["pass2_rank"],
                    "is_active": bool(item["is_active"]),
                }
                for item in rows
            ]
        finally:
            conn.close()

    def _transcript_row(
        self, conn: sqlite3.Connection, recording_uuid: str, revision: int | None
    ) -> sqlite3.Row:
        recording = conn.execute(
            "SELECT id FROM recordings WHERE recording_uuid = ?", (recording_uuid,)
        ).fetchone()
        if recording is None:
            raise AsrServiceError(f"no recording with uuid {recording_uuid}")
        if revision is None:
            row = conn.execute(
                "SELECT * FROM transcripts WHERE recording_id = ? AND is_active = 1",
                (int(recording["id"]),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM transcripts WHERE recording_id = ? AND revision = ?",
                (int(recording["id"]), int(revision)),
            ).fetchone()
        if row is None:
            raise AsrServiceError(
                f"recording {recording_uuid} has no "
                f"{'active transcript' if revision is None else f'revision {revision}'}. "
                "Run `asr transcribe` first."
            )
        return row


def _public_transcript(row: sqlite3.Row) -> dict[str, Any]:
    """Everything about a revision except a filesystem path. Paths never leave Python."""
    payload = {key: row[key] for key in row.keys()}
    payload.pop("working_copy_id", None)
    payload["is_active"] = bool(row["is_active"])
    payload["pass2_budget_exhausted"] = bool(row["pass2_budget_exhausted"])
    return payload


def _public_segment(segment: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": segment["seq"],
        "region_seq": segment["region_seq"],
        "asr_pass": segment["asr_pass"],
        "start_ms": segment["start_ms"],
        "end_ms": segment["end_ms"],
        "text": segment["text"],
        "text_raw": segment["text_raw"],
        "avg_logprob": segment["avg_logprob"],
        "no_speech_prob": segment["no_speech_prob"],
        "word_count": segment["word_count"],
        "min_word_probability": segment["min_word_probability"],
        "selected_for_pass2": bool(segment["selected_for_pass2"]),
        "pass2_reason_codes": segment["pass2_reason_codes"],
        "glossary_replacements": segment["glossary_replacements"],
        # Phase 4 assigns no speaker. Reported explicitly rather than omitted, so the UI
        # renders "unassigned" from data instead of inferring it from a missing field.
        "speaker": None,
        "speaker_status": "UNASSIGNED",
        "words": segment["words"],
    }
