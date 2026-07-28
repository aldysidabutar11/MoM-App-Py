"""SQLite foundation: connections with verified pragmas, and migrations.

Phase 1 creates only the foundational tables (``schema_migrations``,
``app_settings``, ``participants``, ``meetings``, ``recordings``,
``recording_chunks``, ``jobs``, ``job_stages``, ``audit_events``). Tables for
voiceprints, consents, ASR words, diarization turns, utterances, speaker
assignments, MoM items, evidence links and action tracking are deliberately
deferred to their implementation phases.
"""

from mom_igd.db.connection import (
    PragmaVerificationError,
    connect,
    read_pragmas,
    verify_pragmas,
)
from mom_igd.db.migrator import (
    MIGRATIONS_DIR,
    Migration,
    MigrationError,
    apply_migrations,
    current_schema_version,
    discover_migrations,
    head_version,
    initialize_database,
    migration_status,
    split_sql_statements,
    verify_applied_checksums,
)

__all__ = [
    "MIGRATIONS_DIR",
    "Migration",
    "MigrationError",
    "PragmaVerificationError",
    "apply_migrations",
    "connect",
    "current_schema_version",
    "discover_migrations",
    "head_version",
    "initialize_database",
    "migration_status",
    "read_pragmas",
    "split_sql_statements",
    "verify_applied_checksums",
    "verify_pragmas",
]
