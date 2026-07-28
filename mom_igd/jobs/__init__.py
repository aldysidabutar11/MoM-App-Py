"""Deterministic workflow state machine for meeting jobs.

Phase 1 implements the state machine and its persistence only. No stage
*executes* anything: there is no audio capture, no ASR, no diarization, no
speaker identification and no LLM code here. The pipeline shape is declared as
data (:data:`mom_igd.jobs.state_machine.PIPELINE_STAGES`) so the orchestrator,
the database and the documentation cannot drift apart before the stages are
implemented in their own phases.
"""

from mom_igd.jobs.state_machine import (
    ALLOWED_STAGE_TRANSITIONS,
    ALLOWED_TRANSITIONS,
    PIPELINE_STAGES,
    TERMINAL_STATES,
    InvalidStageTransitionError,
    InvalidTransitionError,
    JobState,
    StageSpec,
    StageStatus,
    allowed_from,
    assert_transition,
    can_transition,
    create_job,
    get_job,
    get_stage,
    is_terminal,
    list_stages,
    load_checkpoint,
    next_pending_stage,
    save_checkpoint,
    set_stage_status,
    transition_job,
    validate_transition_graph,
)

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
