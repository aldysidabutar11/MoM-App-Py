"""The workflow state machine: states, legal transitions, and persistence.

A *job* is the workflow instance for exactly one meeting and spans the whole
lifecycle, from setup through recording, post-meeting processing, human review
and approval. It is the single owner of workflow state -- ``meetings`` has no
state column, so the two can never disagree.

Every transition is validated against an explicit table. An illegal transition
raises :class:`InvalidTransitionError` with the allowed set in the message; it is
never silently coerced. Every accepted transition writes an ``audit_events`` row
in the *same* transaction as the state change, so state and audit trail cannot
diverge.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Final, Mapping

from mom_igd.audit import record_event
from mom_igd.db.connection import maybe_transaction

__all__ = [
    "ALLOWED_STAGE_TRANSITIONS",
    "ALLOWED_TRANSITIONS",
    "PIPELINE_STAGES",
    "TERMINAL_STATES",
    "InvalidStageTransitionError",
    "InvalidTransitionError",
    "JobState",
    "StageSpec",
    "StageStatus",
    "allowed_from",
    "assert_transition",
    "can_transition",
    "create_job",
    "get_job",
    "get_stage",
    "is_terminal",
    "list_stages",
    "load_checkpoint",
    "next_pending_stage",
    "save_checkpoint",
    "set_stage_status",
    "transition_job",
    "validate_transition_graph",
]


class JobState(StrEnum):
    """Workflow states. Must match the CHECK constraint on ``jobs.state``."""

    DRAFT = "DRAFT"
    READY = "READY"
    RECORDING = "RECORDING"
    RECORDED = "RECORDED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVED = "APPROVED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StageStatus(StrEnum):
    """Per-stage status. Must match the CHECK constraint on ``job_stages.status``."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


INITIAL_STATE: Final[JobState] = JobState.DRAFT

TERMINAL_STATES: Final[frozenset[JobState]] = frozenset(
    {JobState.APPROVED, JobState.CANCELLED}
)
"""States with no outgoing transitions.

``APPROVED`` is terminal because approval freezes an immutable snapshot; a change
after approval must create a new revision rather than mutate the approved one.
``FAILED`` is *not* terminal: a failed run can be re-queued after the operator
fixes the cause.
"""

ALLOWED_TRANSITIONS: Final[Mapping[JobState, frozenset[JobState]]] = {
    # Meeting is being set up; participants and agenda may still change.
    JobState.DRAFT: frozenset({JobState.READY, JobState.CANCELLED}),
    # Pre-flight passed (device validated, disk checked); ready to record.
    JobState.READY: frozenset({JobState.RECORDING, JobState.DRAFT, JobState.CANCELLED}),
    # Capture in progress. FAILED covers device loss / disk full / crash.
    JobState.RECORDING: frozenset({JobState.RECORDED, JobState.FAILED, JobState.CANCELLED}),
    # Audio master closed and manifest verified.
    JobState.RECORDED: frozenset({JobState.QUEUED, JobState.FAILED, JobState.CANCELLED}),
    # Waiting for the single heavy worker slot.
    JobState.QUEUED: frozenset({JobState.PROCESSING, JobState.CANCELLED}),
    # Running the post-meeting pipeline, one heavy stage at a time.
    JobState.PROCESSING: frozenset(
        {JobState.REVIEW_REQUIRED, JobState.FAILED, JobState.CANCELLED}
    ),
    # Human review gate. Re-processing is allowed after a reviewer correction
    # (for example resolving UNKNOWN speakers and re-running MoM extraction).
    JobState.REVIEW_REQUIRED: frozenset(
        {JobState.APPROVED, JobState.PROCESSING, JobState.FAILED, JobState.CANCELLED}
    ),
    # Terminal: approval produces an immutable snapshot.
    JobState.APPROVED: frozenset(),
    # Recoverable: re-queue after the operator addresses the cause.
    JobState.FAILED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    # Terminal.
    JobState.CANCELLED: frozenset(),
}

ALLOWED_STAGE_TRANSITIONS: Final[Mapping[StageStatus, frozenset[StageStatus]]] = {
    StageStatus.PENDING: frozenset(
        {StageStatus.RUNNING, StageStatus.SKIPPED, StageStatus.CANCELLED}
    ),
    StageStatus.RUNNING: frozenset(
        {StageStatus.COMPLETED, StageStatus.FAILED, StageStatus.CANCELLED}
    ),
    # A failed stage can be retried; that is what attempt_count records.
    StageStatus.FAILED: frozenset({StageStatus.RUNNING, StageStatus.SKIPPED, StageStatus.CANCELLED}),
    StageStatus.COMPLETED: frozenset(),
    StageStatus.SKIPPED: frozenset(),
    StageStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class StageSpec:
    """Declaration of one pipeline stage. Declaration only -- nothing executes."""

    name: str
    title: str
    is_heavy: bool
    phase_introduced: str


PIPELINE_STAGES: Final[tuple[StageSpec, ...]] = (
    StageSpec("validate_audio", "Audio validation", False, "4"),
    StageSpec("normalize_audio", "Normalisation and 16 kHz working copy", False, "4"),
    StageSpec("vad", "Voice activity detection", False, "4"),
    StageSpec("asr_pass1", "First-pass transcription", True, "4"),
    # Diarization runs BEFORE selective re-transcription: two of the strongest
    # selection signals for pass 2 (speaker-change boundaries and overlap
    # regions) only exist once diarization has run. See docs/architecture.md.
    StageSpec("diarize", "Speaker diarization", True, "5"),
    StageSpec("asr_pass2_selective", "Selective high-accuracy retranscription", True, "5"),
    StageSpec("voice_id", "Voice identification", True, "6"),
    StageSpec("reconcile_transcript", "Transcript reconciliation", False, "7"),
    StageSpec("mom_extract", "MoM extraction", True, "8"),
    StageSpec("verify_evidence", "Evidence verification", False, "8"),
)
"""The post-meeting pipeline, as data.

``is_heavy`` marks a stage that loads a model. Exactly one heavy stage may be
resident at a time, in its own short-lived worker process (ADR-0004).
"""


class InvalidTransitionError(ValueError):
    """Raised when a job state transition is not permitted."""

    def __init__(self, current: JobState, requested: JobState) -> None:
        allowed = sorted(state.value for state in ALLOWED_TRANSITIONS[current])
        allowed_text = ", ".join(allowed) if allowed else "<none: terminal state>"
        super().__init__(
            f"Illegal job transition {current.value} -> {requested.value}. "
            f"Allowed from {current.value}: {allowed_text}."
        )
        self.current = current
        self.requested = requested


class InvalidStageTransitionError(ValueError):
    """Raised when a stage status transition is not permitted."""

    def __init__(self, current: StageStatus, requested: StageStatus) -> None:
        allowed = sorted(status.value for status in ALLOWED_STAGE_TRANSITIONS[current])
        allowed_text = ", ".join(allowed) if allowed else "<none: terminal status>"
        super().__init__(
            f"Illegal stage transition {current.value} -> {requested.value}. "
            f"Allowed from {current.value}: {allowed_text}."
        )
        self.current = current
        self.requested = requested


# ---------------------------------------------------------------------------
# Pure graph helpers
# ---------------------------------------------------------------------------


def allowed_from(state: JobState | str) -> frozenset[JobState]:
    """Return the states reachable in one step from ``state``."""
    return ALLOWED_TRANSITIONS[JobState(state)]


def can_transition(current: JobState | str, requested: JobState | str) -> bool:
    """Return ``True`` if the transition is permitted."""
    return JobState(requested) in ALLOWED_TRANSITIONS[JobState(current)]


def assert_transition(current: JobState | str, requested: JobState | str) -> JobState:
    """Validate a transition and return the target state.

    Raises:
        InvalidTransitionError: If the transition is not permitted.
    """
    source = JobState(current)
    target = JobState(requested)
    if target not in ALLOWED_TRANSITIONS[source]:
        raise InvalidTransitionError(source, target)
    return target


def is_terminal(state: JobState | str) -> bool:
    """Return ``True`` for states with no outgoing transitions."""
    return JobState(state) in TERMINAL_STATES


def validate_transition_graph() -> None:
    """Self-check the transition tables.

    Verifies that every state is a key, every target is a known state, no state
    transitions to itself, terminal states have no outgoing edges, non-terminal
    states have at least one, and every state is reachable from ``DRAFT``.

    Raises:
        AssertionError: With a message naming the first inconsistency found.
    """
    states = set(JobState)
    missing = states - set(ALLOWED_TRANSITIONS)
    assert not missing, f"States missing from ALLOWED_TRANSITIONS: {sorted(missing)}"

    for source, targets in ALLOWED_TRANSITIONS.items():
        unknown = {t for t in targets if t not in states}
        assert not unknown, f"{source.value} points at unknown states {sorted(unknown)}"
        assert source not in targets, f"{source.value} has a self-transition"
        if source in TERMINAL_STATES:
            assert not targets, f"Terminal state {source.value} has outgoing edges {sorted(targets)}"
        else:
            assert targets, f"Non-terminal state {source.value} has no outgoing edges"

    reachable: set[JobState] = {INITIAL_STATE}
    frontier = [INITIAL_STATE]
    while frontier:
        current = frontier.pop()
        for target in ALLOWED_TRANSITIONS[current]:
            if target not in reachable:
                reachable.add(target)
                frontier.append(target)
    unreachable = states - reachable
    assert not unreachable, f"States unreachable from DRAFT: {sorted(unreachable)}"

    stage_statuses = set(StageStatus)
    missing_stage = stage_statuses - set(ALLOWED_STAGE_TRANSITIONS)
    assert not missing_stage, f"Stage statuses missing: {sorted(missing_stage)}"
    for source_status, target_statuses in ALLOWED_STAGE_TRANSITIONS.items():
        unknown_status = {t for t in target_statuses if t not in stage_statuses}
        assert not unknown_status, f"{source_status.value} -> unknown {sorted(unknown_status)}"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _dumps(payload: Mapping[str, Any] | None) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def create_job(
    conn: sqlite3.Connection,
    meeting_id: int,
    *,
    actor: str = "system",
    stages: tuple[StageSpec, ...] = PIPELINE_STAGES,
) -> int:
    """Create a workflow job in ``DRAFT`` with its pipeline stages materialised.

    The job row, every stage row and the audit event are written in one
    transaction.
    """
    with maybe_transaction(conn):
        cursor = conn.execute(
            "INSERT INTO jobs (meeting_id, kind, state) VALUES (?, 'MEETING_WORKFLOW', ?)",
            (meeting_id, INITIAL_STATE.value),
        )
        job_id = int(cursor.lastrowid or 0)
        for seq, spec in enumerate(stages, start=1):
            conn.execute(
                "INSERT INTO job_stages (job_id, seq, name, status, is_heavy, phase_introduced) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    seq,
                    spec.name,
                    StageStatus.PENDING.value,
                    1 if spec.is_heavy else 0,
                    spec.phase_introduced,
                ),
            )
        record_event(
            conn,
            category="JOB",
            action="job.created",
            entity_type="job",
            entity_id=job_id,
            actor=actor,
            to_state=INITIAL_STATE.value,
            detail={"meeting_id": meeting_id, "stage_count": len(stages)},
        )
    return job_id


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    """Fetch a job row.

    Raises:
        LookupError: If no such job exists.
    """
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise LookupError(f"No job with id={job_id}.")
    return row


def get_stage(conn: sqlite3.Connection, stage_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM job_stages WHERE id = ?", (stage_id,)).fetchone()
    if row is None:
        raise LookupError(f"No job stage with id={stage_id}.")
    return row


def list_stages(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM job_stages WHERE job_id = ? ORDER BY seq", (job_id,)).fetchall()
    )


def transition_job(
    conn: sqlite3.Connection,
    job_id: int,
    to_state: JobState | str,
    *,
    actor: str = "system",
    reason: str | None = None,
    error: str | None = None,
) -> JobState:
    """Validate and persist a job state transition, with its audit event.

    Raises:
        LookupError: If the job does not exist.
        InvalidTransitionError: If the transition is not permitted. Nothing is
            written in that case.
    """
    with maybe_transaction(conn):
        row = get_job(conn, job_id)
        current = JobState(str(row["state"]))
        target = assert_transition(current, to_state)

        assignments = ["state = ?", "updated_at = ?"]
        params: list[Any] = [target.value, _utc_now_iso()]

        if row["started_at"] is None and target is not JobState.CANCELLED:
            assignments.append("started_at = ?")
            params.append(_utc_now_iso())
        if target is JobState.PROCESSING:
            assignments.append("attempt_count = attempt_count + 1")
        if target in TERMINAL_STATES or target is JobState.FAILED:
            assignments.append("finished_at = ?")
            params.append(_utc_now_iso())
        else:
            assignments.append("finished_at = NULL")
        assignments.append("last_error = ?")
        params.append(error)

        params.append(job_id)
        conn.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", tuple(params))

        detail: dict[str, Any] = {}
        if reason:
            detail["reason"] = reason
        if error:
            detail["error"] = error
        record_event(
            conn,
            category="JOB",
            action="job.state_changed",
            entity_type="job",
            entity_id=job_id,
            actor=actor,
            from_state=current.value,
            to_state=target.value,
            detail=detail or None,
        )
    return target


def set_stage_status(
    conn: sqlite3.Connection,
    stage_id: int,
    status: StageStatus | str,
    *,
    actor: str = "system",
    error: str | None = None,
    progress_percent: float | None = None,
    provider_name: str | None = None,
    provider_version: str | None = None,
    peak_rss_mb: int | None = None,
) -> StageStatus:
    """Validate and persist a stage status change, with its audit event.

    Raises:
        InvalidStageTransitionError: If the status change is not permitted.
    """
    with maybe_transaction(conn):
        row = get_stage(conn, stage_id)
        current = StageStatus(str(row["status"]))
        target = StageStatus(status)
        if target not in ALLOWED_STAGE_TRANSITIONS[current]:
            raise InvalidStageTransitionError(current, target)

        assignments = ["status = ?", "updated_at = ?", "last_error = ?"]
        params: list[Any] = [target.value, _utc_now_iso(), error]

        if target is StageStatus.RUNNING:
            assignments += ["started_at = ?", "attempt_count = attempt_count + 1"]
            params.append(_utc_now_iso())
        if target in {StageStatus.COMPLETED, StageStatus.FAILED, StageStatus.CANCELLED}:
            assignments.append("finished_at = ?")
            params.append(_utc_now_iso())
        if target is StageStatus.COMPLETED:
            assignments.append("progress_percent = 100")
        elif progress_percent is not None:
            assignments.append("progress_percent = ?")
            params.append(float(progress_percent))
        for column, value in (
            ("provider_name", provider_name),
            ("provider_version", provider_version),
            ("peak_rss_mb", peak_rss_mb),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                params.append(value)

        params.append(stage_id)
        conn.execute(
            f"UPDATE job_stages SET {', '.join(assignments)} WHERE id = ?", tuple(params)
        )
        record_event(
            conn,
            category="JOB",
            action="job_stage.status_changed",
            entity_type="job_stage",
            entity_id=stage_id,
            actor=actor,
            from_state=current.value,
            to_state=target.value,
            detail={"job_id": int(row["job_id"]), "stage": str(row["name"])},
        )
    return target


def save_checkpoint(
    conn: sqlite3.Connection,
    stage_id: int,
    *,
    checkpoint: Mapping[str, Any] | None = None,
    resume_metadata: Mapping[str, Any] | None = None,
) -> None:
    """Persist a stage checkpoint and/or its resume metadata.

    A checkpoint lets an interrupted run continue without redoing completed
    work; resume metadata carries what the next attempt needs to know (for
    example the last processed chunk sequence).
    """
    if checkpoint is None and resume_metadata is None:
        raise ValueError("save_checkpoint requires checkpoint and/or resume_metadata.")
    assignments = ["updated_at = ?"]
    params: list[Any] = [_utc_now_iso()]
    if checkpoint is not None:
        assignments.append("checkpoint_json = ?")
        params.append(_dumps(checkpoint))
    if resume_metadata is not None:
        assignments.append("resume_metadata_json = ?")
        params.append(_dumps(resume_metadata))
    params.append(stage_id)
    with maybe_transaction(conn):
        get_stage(conn, stage_id)  # existence check inside the transaction
        conn.execute(
            f"UPDATE job_stages SET {', '.join(assignments)} WHERE id = ?", tuple(params)
        )


def load_checkpoint(conn: sqlite3.Connection, stage_id: int) -> dict[str, Any]:
    """Return ``{"checkpoint": ..., "resume_metadata": ...}`` for a stage."""
    row = get_stage(conn, stage_id)
    return {
        "checkpoint": json.loads(row["checkpoint_json"]) if row["checkpoint_json"] else None,
        "resume_metadata": (
            json.loads(row["resume_metadata_json"]) if row["resume_metadata_json"] else None
        ),
    }


def next_pending_stage(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    """Return the lowest-sequence stage that still needs to run, or ``None``.

    This is the resume point after an interrupted run.
    """
    return conn.execute(
        "SELECT * FROM job_stages WHERE job_id = ? AND status IN (?, ?) "
        "ORDER BY seq LIMIT 1",
        (job_id, StageStatus.PENDING.value, StageStatus.FAILED.value),
    ).fetchone()
