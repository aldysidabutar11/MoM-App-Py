"""Workflow state machine: legal transitions, rejections, atomic audit.

Covers Phase 1 test categories 22, 23 and 24.
"""

from __future__ import annotations

import sqlite3

import pytest

from mom_igd.jobs.state_machine import (
    ALLOWED_STAGE_TRANSITIONS,
    ALLOWED_TRANSITIONS,
    PIPELINE_STAGES,
    TERMINAL_STATES,
    InvalidStageTransitionError,
    InvalidTransitionError,
    JobState,
    StageStatus,
    allowed_from,
    assert_transition,
    can_transition,
    create_job,
    get_job,
    is_terminal,
    list_stages,
    load_checkpoint,
    next_pending_stage,
    save_checkpoint,
    set_stage_status,
    transition_job,
    validate_transition_graph,
)

REQUIRED_STATES = {
    "DRAFT",
    "READY",
    "RECORDING",
    "RECORDED",
    "QUEUED",
    "PROCESSING",
    "REVIEW_REQUIRED",
    "APPROVED",
    "FAILED",
    "CANCELLED",
}

HAPPY_PATH = [
    JobState.READY,
    JobState.RECORDING,
    JobState.RECORDED,
    JobState.QUEUED,
    JobState.PROCESSING,
    JobState.REVIEW_REQUIRED,
    JobState.APPROVED,
]


def _audit_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall())


# ----------------------------------------------------------- graph structure


def test_the_required_state_vocabulary_is_implemented() -> None:
    assert {state.value for state in JobState} == REQUIRED_STATES


def test_database_check_constraint_matches_the_enum(conn: sqlite3.Connection) -> None:
    """The schema and the code must agree on the state vocabulary."""
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()["sql"]
    for state in JobState:
        assert f"'{state.value}'" in sql, f"{state.value} missing from the jobs CHECK constraint"


def test_stage_status_check_constraint_matches_the_enum(conn: sqlite3.Connection) -> None:
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='job_stages'"
    ).fetchone()["sql"]
    for status in StageStatus:
        assert f"'{status.value}'" in sql


def test_transition_graph_is_self_consistent() -> None:
    validate_transition_graph()


def test_terminal_states_have_no_outgoing_transitions() -> None:
    assert TERMINAL_STATES == frozenset({JobState.APPROVED, JobState.CANCELLED})
    for state in TERMINAL_STATES:
        assert allowed_from(state) == frozenset()
        assert is_terminal(state)


def test_failed_is_recoverable_not_terminal() -> None:
    assert not is_terminal(JobState.FAILED)
    assert JobState.QUEUED in allowed_from(JobState.FAILED)


def test_every_state_is_reachable_from_draft() -> None:
    reachable = {JobState.DRAFT}
    frontier = [JobState.DRAFT]
    while frontier:
        for target in ALLOWED_TRANSITIONS[frontier.pop()]:
            if target not in reachable:
                reachable.add(target)
                frontier.append(target)
    assert reachable == set(JobState)


def test_pipeline_stages_are_declaration_only() -> None:
    names = [spec.name for spec in PIPELINE_STAGES]
    assert len(names) == len(set(names)), "stage names must be unique"
    assert "diarize" in names and "asr_pass1" in names
    # Diarization must precede selective re-transcription: two of the strongest
    # pass-2 selection signals only exist after diarization has run.
    assert names.index("diarize") < names.index("asr_pass2_selective")
    heavy = [spec.name for spec in PIPELINE_STAGES if spec.is_heavy]
    assert set(heavy) >= {"asr_pass1", "diarize", "voice_id", "mom_extract"}
    for spec in PIPELINE_STAGES:
        assert spec.phase_introduced != "1", "no pipeline stage is implemented in Phase 1"


# ---------------------------------------------------- 22. legal transitions


def test_pure_helpers_agree_on_legal_transitions() -> None:
    assert can_transition(JobState.DRAFT, JobState.READY)
    assert assert_transition(JobState.DRAFT, JobState.READY) is JobState.READY
    assert can_transition("DRAFT", "READY"), "strings must be accepted"


def test_full_happy_path_is_persisted(conn: sqlite3.Connection, meeting_id: int) -> None:
    job_id = create_job(conn, meeting_id)
    assert get_job(conn, job_id)["state"] == JobState.DRAFT.value

    for target in HAPPY_PATH:
        assert transition_job(conn, job_id, target) is target
        assert get_job(conn, job_id)["state"] == target.value

    row = get_job(conn, job_id)
    assert row["started_at"] is not None
    assert row["finished_at"] is not None
    assert row["attempt_count"] == 1, "attempt_count increments on entering PROCESSING"


def test_reprocessing_after_review_is_allowed(conn: sqlite3.Connection, meeting_id: int) -> None:
    job_id = create_job(conn, meeting_id)
    for target in HAPPY_PATH[:-1]:
        transition_job(conn, job_id, target)
    assert get_job(conn, job_id)["state"] == JobState.REVIEW_REQUIRED.value
    transition_job(conn, job_id, JobState.PROCESSING, reason="reviewer resolved UNKNOWN speakers")
    assert get_job(conn, job_id)["attempt_count"] == 2


def test_failure_then_requeue(conn: sqlite3.Connection, meeting_id: int) -> None:
    job_id = create_job(conn, meeting_id)
    transition_job(conn, job_id, JobState.READY)
    transition_job(conn, job_id, JobState.RECORDING)
    transition_job(conn, job_id, JobState.FAILED, error="microphone disconnected")
    row = get_job(conn, job_id)
    assert row["last_error"] == "microphone disconnected"
    assert row["finished_at"] is not None

    transition_job(conn, job_id, JobState.QUEUED)
    row = get_job(conn, job_id)
    assert row["state"] == JobState.QUEUED.value
    assert row["finished_at"] is None, "re-queuing clears the finish timestamp"


def test_create_job_materialises_the_declared_stages(conn: sqlite3.Connection, meeting_id: int) -> None:
    job_id = create_job(conn, meeting_id)
    stages = list_stages(conn, job_id)
    assert [row["name"] for row in stages] == [spec.name for spec in PIPELINE_STAGES]
    assert all(row["status"] == StageStatus.PENDING.value for row in stages)
    assert [row["seq"] for row in stages] == list(range(1, len(PIPELINE_STAGES) + 1))


def test_only_one_active_workflow_per_meeting(conn: sqlite3.Connection, meeting_id: int) -> None:
    create_job(conn, meeting_id)
    with pytest.raises(sqlite3.IntegrityError):
        create_job(conn, meeting_id)


def test_a_new_workflow_is_allowed_once_the_previous_is_terminal(
    conn: sqlite3.Connection, meeting_id: int
) -> None:
    first = create_job(conn, meeting_id)
    transition_job(conn, first, JobState.CANCELLED)
    second = create_job(conn, meeting_id)
    assert second != first


# --------------------------------------------------- 23. illegal transitions


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (JobState.DRAFT, JobState.APPROVED),
        (JobState.DRAFT, JobState.PROCESSING),
        (JobState.READY, JobState.RECORDED),
        (JobState.QUEUED, JobState.APPROVED),
        (JobState.PROCESSING, JobState.RECORDING),
        (JobState.APPROVED, JobState.PROCESSING),
        (JobState.CANCELLED, JobState.READY),
        (JobState.FAILED, JobState.APPROVED),
    ],
)
def test_illegal_transition_is_rejected_by_the_pure_helper(
    source: JobState, target: JobState
) -> None:
    assert not can_transition(source, target)
    with pytest.raises(InvalidTransitionError) as excinfo:
        assert_transition(source, target)
    message = str(excinfo.value)
    assert source.value in message and target.value in message
    assert "Allowed from" in message, "the error must name what IS allowed"


def test_illegal_transition_changes_nothing_in_the_database(
    conn: sqlite3.Connection, meeting_id: int
) -> None:
    job_id = create_job(conn, meeting_id)
    before_state = get_job(conn, job_id)["state"]
    before_updated = get_job(conn, job_id)["updated_at"]
    before_audit = len(_audit_rows(conn))

    with pytest.raises(InvalidTransitionError):
        transition_job(conn, job_id, JobState.APPROVED)

    row = get_job(conn, job_id)
    assert row["state"] == before_state
    assert row["updated_at"] == before_updated
    assert len(_audit_rows(conn)) == before_audit, "a rejected transition must write no audit event"
    assert not conn.in_transaction, "the failed transaction must be rolled back, not left open"


def test_terminal_state_cannot_be_left(conn: sqlite3.Connection, meeting_id: int) -> None:
    job_id = create_job(conn, meeting_id)
    transition_job(conn, job_id, JobState.CANCELLED)
    for target in JobState:
        with pytest.raises(InvalidTransitionError):
            transition_job(conn, job_id, target)
    assert get_job(conn, job_id)["state"] == JobState.CANCELLED.value


def test_self_transition_is_rejected(conn: sqlite3.Connection, meeting_id: int) -> None:
    job_id = create_job(conn, meeting_id)
    with pytest.raises(InvalidTransitionError):
        transition_job(conn, job_id, JobState.DRAFT)


def test_unknown_state_value_is_rejected(conn: sqlite3.Connection, meeting_id: int) -> None:
    job_id = create_job(conn, meeting_id)
    with pytest.raises(ValueError):
        transition_job(conn, job_id, "TOTALLY_INVALID")


def test_transition_on_a_missing_job_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(LookupError, match="No job with id"):
        transition_job(conn, 987654, JobState.READY)


# -------------------------------------------- 24. atomic state + audit event


def test_accepted_transition_writes_exactly_one_audit_event(
    conn: sqlite3.Connection, meeting_id: int
) -> None:
    job_id = create_job(conn, meeting_id)
    before = len(_audit_rows(conn))
    transition_job(conn, job_id, JobState.READY, actor="tester", reason="pre-flight passed")
    rows = _audit_rows(conn)
    assert len(rows) == before + 1

    event = rows[-1]
    assert event["category"] == "JOB"
    assert event["action"] == "job.state_changed"
    assert event["entity_type"] == "job"
    assert event["entity_id"] == job_id
    assert event["actor"] == "tester"
    assert event["from_state"] == JobState.DRAFT.value
    assert event["to_state"] == JobState.READY.value
    assert "pre-flight passed" in event["detail_json"]


def test_create_job_is_audited(conn: sqlite3.Connection, meeting_id: int) -> None:
    job_id = create_job(conn, meeting_id, actor="operator")
    event = _audit_rows(conn)[-1]
    assert event["action"] == "job.created"
    assert event["to_state"] == JobState.DRAFT.value
    assert event["entity_id"] == job_id
    assert event["actor"] == "operator"


def test_state_and_audit_event_are_written_in_one_transaction(
    conn: sqlite3.Connection, meeting_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If writing the audit event fails, the state change must not survive."""
    job_id = create_job(conn, meeting_id)
    audit_before = len(_audit_rows(conn))

    import mom_igd.jobs.state_machine as sm

    def _explode(*_args, **_kwargs):
        raise RuntimeError("audit sink unavailable")

    monkeypatch.setattr(sm, "record_event", _explode)

    with pytest.raises(RuntimeError, match="audit sink unavailable"):
        sm.transition_job(conn, job_id, JobState.READY)

    assert get_job(conn, job_id)["state"] == JobState.DRAFT.value, (
        "the state change must be rolled back together with the failed audit write"
    )
    assert len(_audit_rows(conn)) == audit_before
    assert not conn.in_transaction


# ---------------------------------------------- stages, checkpoints, resume


def test_stage_status_transitions_are_validated(conn: sqlite3.Connection, meeting_id: int) -> None:
    job_id = create_job(conn, meeting_id)
    stage_id = list_stages(conn, job_id)[0]["id"]

    assert set_stage_status(conn, stage_id, StageStatus.RUNNING) is StageStatus.RUNNING
    assert set_stage_status(conn, stage_id, StageStatus.COMPLETED) is StageStatus.COMPLETED

    row = conn.execute("SELECT * FROM job_stages WHERE id = ?", (stage_id,)).fetchone()
    assert row["progress_percent"] == 100
    assert row["attempt_count"] == 1
    assert row["finished_at"] is not None


def test_illegal_stage_transition_is_rejected(conn: sqlite3.Connection, meeting_id: int) -> None:
    job_id = create_job(conn, meeting_id)
    stage_id = list_stages(conn, job_id)[0]["id"]
    with pytest.raises(InvalidStageTransitionError):
        set_stage_status(conn, stage_id, StageStatus.COMPLETED)  # PENDING -> COMPLETED
    row = conn.execute("SELECT status FROM job_stages WHERE id = ?", (stage_id,)).fetchone()
    assert row["status"] == StageStatus.PENDING.value


def test_completed_stage_is_terminal() -> None:
    assert ALLOWED_STAGE_TRANSITIONS[StageStatus.COMPLETED] == frozenset()
    assert StageStatus.RUNNING in ALLOWED_STAGE_TRANSITIONS[StageStatus.FAILED]


def test_stage_status_change_is_audited(conn: sqlite3.Connection, meeting_id: int) -> None:
    job_id = create_job(conn, meeting_id)
    stage_id = list_stages(conn, job_id)[0]["id"]
    before = len(_audit_rows(conn))
    set_stage_status(conn, stage_id, StageStatus.RUNNING)
    event = _audit_rows(conn)[-1]
    assert len(_audit_rows(conn)) == before + 1
    assert event["action"] == "job_stage.status_changed"
    assert event["entity_type"] == "job_stage"


def test_checkpoint_and_resume_metadata_round_trip(conn: sqlite3.Connection, meeting_id: int) -> None:
    job_id = create_job(conn, meeting_id)
    stage_id = list_stages(conn, job_id)[0]["id"]

    save_checkpoint(
        conn,
        stage_id,
        checkpoint={"last_chunk_seq": 42, "partial": True},
        resume_metadata={"resume_from_ms": 1_260_000},
    )
    loaded = load_checkpoint(conn, stage_id)
    assert loaded["checkpoint"] == {"last_chunk_seq": 42, "partial": True}
    assert loaded["resume_metadata"] == {"resume_from_ms": 1_260_000}


def test_checkpoint_requires_something_to_save(conn: sqlite3.Connection, meeting_id: int) -> None:
    job_id = create_job(conn, meeting_id)
    stage_id = list_stages(conn, job_id)[0]["id"]
    with pytest.raises(ValueError, match="requires"):
        save_checkpoint(conn, stage_id)


def test_next_pending_stage_is_the_resume_point(conn: sqlite3.Connection, meeting_id: int) -> None:
    job_id = create_job(conn, meeting_id)
    stages = list_stages(conn, job_id)
    assert next_pending_stage(conn, job_id)["name"] == stages[0]["name"]

    set_stage_status(conn, stages[0]["id"], StageStatus.RUNNING)
    set_stage_status(conn, stages[0]["id"], StageStatus.COMPLETED)
    assert next_pending_stage(conn, job_id)["name"] == stages[1]["name"]

    set_stage_status(conn, stages[1]["id"], StageStatus.RUNNING)
    set_stage_status(conn, stages[1]["id"], StageStatus.FAILED, error="worker died")
    assert next_pending_stage(conn, job_id)["name"] == stages[1]["name"], (
        "a failed stage is the resume point, not the one after it"
    )


def test_all_stages_completed_leaves_no_resume_point(conn: sqlite3.Connection, meeting_id: int) -> None:
    job_id = create_job(conn, meeting_id)
    for row in list_stages(conn, job_id):
        set_stage_status(conn, row["id"], StageStatus.RUNNING)
        set_stage_status(conn, row["id"], StageStatus.COMPLETED)
    assert next_pending_stage(conn, job_id) is None


def test_no_stage_executes_anything(conn: sqlite3.Connection, meeting_id: int) -> None:
    """Phase 1 declares the pipeline; it must not run it."""
    job_id = create_job(conn, meeting_id)
    for row in list_stages(conn, job_id):
        assert row["provider_name"] is None
        assert row["provider_version"] is None
        assert row["peak_rss_mb"] is None
        assert row["duration_ms"] is None
        assert row["started_at"] is None
