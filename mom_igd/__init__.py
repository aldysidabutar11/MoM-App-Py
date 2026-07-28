"""MoM-IGD: fully offline Minutes of Meeting application.

Phase 1 scope is the application foundation only: configuration, runtime path
service, SQLite migrations, a deterministic job state machine, a loopback-only
API, environment diagnostics and a static desktop shell.

There is deliberately no audio capture, no ASR, no diarization, no speaker
identification and no LLM code in this package yet. Those arrive in their own
phases (see ``docs/architecture.md``).
"""

from mom_igd.version import (
    APP_NAME,
    APP_VERSION,
    CONFIG_SCHEMA_VERSION,
    CURRENT_PHASE,
    REGISTRY_SCHEMA_VERSION,
)

__all__ = [
    "APP_NAME",
    "APP_VERSION",
    "CONFIG_SCHEMA_VERSION",
    "CURRENT_PHASE",
    "REGISTRY_SCHEMA_VERSION",
]
