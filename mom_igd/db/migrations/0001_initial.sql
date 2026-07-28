-- ===========================================================================
-- MoM-IGD  migration 0001  --  foundational schema (Phase 1)
--
-- Scope note: this migration creates EXACTLY the nine foundational tables of
-- Phase 1 and nothing else:
--
--   schema_migrations (created by the migration runner itself), app_settings,
--   participants, meetings, recordings, recording_chunks, jobs, job_stages,
--   audit_events
--
-- The following tables are deliberately absent and arrive in their own phases,
-- because designing them before their pipeline exists would bake in guesses:
--
--   meeting_participants         -> Phase 3  (link table; needs enrolment first)
--   voiceprints, consents        -> Phase 3  (biometric data, encrypted)
--   asr_words                    -> Phase 4
--   diarization_turns            -> Phase 5
--   speaker_assignments          -> Phase 6
--   utterances                   -> Phase 7
--   mom_items, evidence_links    -> Phase 8
--   action_tracking              -> Phase 10
--
-- Conventions:
--   * Timestamps are ISO-8601 UTC strings with millisecond precision.
--   * Booleans are INTEGER 0/1 with a CHECK constraint.
--   * Every enumerated column has an explicit CHECK constraint so the database
--     rejects an invalid state even if application code has a bug.
--   * `schema_migrations` is created by the migration runner itself, since it
--     must exist before any migration can be recorded.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- app_settings: small key/value store for provenance and operator settings.
-- ---------------------------------------------------------------------------
CREATE TABLE app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);


-- ---------------------------------------------------------------------------
-- participants: registered people who may be identified in a meeting.
--
-- Phase 1 stores identity only. Voiceprints are biometric data and are NOT
-- stored here; they arrive in Phase 3 in a separate encrypted table with an
-- accompanying consent record.
-- ---------------------------------------------------------------------------
CREATE TABLE participants (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT    NOT NULL,
    role         TEXT,
    email        TEXT,
    external_ref TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    notes        TEXT,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT participants_display_name_not_blank CHECK (length(trim(display_name)) > 0)
);

CREATE UNIQUE INDEX ux_participants_display_name ON participants (display_name);
CREATE INDEX ix_participants_active ON participants (is_active);


-- ---------------------------------------------------------------------------
-- meetings: descriptive record of one in-person meeting.
--
-- Intentionally has NO workflow state column. The workflow lives in `jobs`,
-- which is the single owner of the state machine; duplicating state here would
-- create two sources of truth that can disagree.
-- ---------------------------------------------------------------------------
CREATE TABLE meetings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL,
    agenda        TEXT,
    location      TEXT,
    scheduled_at  TEXT,
    timezone      TEXT    NOT NULL DEFAULT 'Asia/Jakarta',
    language_hint TEXT    NOT NULL DEFAULT 'id',
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT meetings_title_not_blank CHECK (length(trim(title)) > 0)
);

CREATE INDEX ix_meetings_scheduled_at ON meetings (scheduled_at);


-- NOTE: a meeting<->participant link table is deliberately NOT created here.
-- The Phase 1 table set is fixed at exactly nine tables (see the header) and a
-- join table is not one of them. Linking participants to a meeting only becomes
-- meaningful once enrolment exists, so it arrives with Phase 3 together with the
-- voiceprint and consent tables.


-- ---------------------------------------------------------------------------
-- recordings: one audio master per meeting (Phase 2 populates this).
--
-- `relative_dir` is relative to <data_root>/recordings. Absolute paths are
-- never stored, so the runtime data root can be relocated without rewriting
-- the database.
-- ---------------------------------------------------------------------------
CREATE TABLE recordings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id      INTEGER NOT NULL REFERENCES meetings (id) ON DELETE CASCADE,
    relative_dir    TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING', 'CAPTURING', 'COMPLETED',
                                      'RECOVERED', 'FAILED', 'DISCARDED')),
    container       TEXT    CHECK (container IS NULL OR container IN ('flac', 'wav')),
    sample_rate_hz  INTEGER CHECK (sample_rate_hz IS NULL OR sample_rate_hz > 0),
    channels        INTEGER CHECK (channels IS NULL OR channels > 0),
    bit_depth       INTEGER CHECK (bit_depth IS NULL OR bit_depth > 0),
    device_name     TEXT,
    device_id       TEXT,
    started_at      TEXT,
    ended_at        TEXT,
    duration_ms     INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    expected_frames INTEGER,
    written_frames  INTEGER,
    dropped_frames  INTEGER NOT NULL DEFAULT 0 CHECK (dropped_frames >= 0),
    manifest_sha256 TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT recordings_relative_dir_relative
        CHECK (relative_dir NOT LIKE '%:%' AND relative_dir NOT LIKE '/%'
               AND relative_dir NOT LIKE '\%' AND relative_dir NOT LIKE '%..%')
);

CREATE INDEX ix_recordings_meeting ON recordings (meeting_id);
CREATE INDEX ix_recordings_status ON recordings (status);


-- ---------------------------------------------------------------------------
-- recording_chunks: the lossless chunk manifest, mirrored into SQLite.
--
-- The authoritative manifest is the on-disk manifest.jsonl written by the
-- recorder; this table is the queryable index of it. `sha256` allows the
-- Phase 2 recovery path to detect truncated or missing chunks.
-- ---------------------------------------------------------------------------
CREATE TABLE recording_chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id    INTEGER NOT NULL REFERENCES recordings (id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL CHECK (seq >= 0),
    filename        TEXT    NOT NULL,
    sha256          TEXT    CHECK (sha256 IS NULL OR length(sha256) = 64),
    size_bytes      INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    frames          INTEGER CHECK (frames IS NULL OR frames >= 0),
    sample_offset   INTEGER CHECK (sample_offset IS NULL OR sample_offset >= 0),
    start_ms        INTEGER CHECK (start_ms IS NULL OR start_ms >= 0),
    end_ms          INTEGER CHECK (end_ms IS NULL OR end_ms >= 0),
    dropped_frames  INTEGER NOT NULL DEFAULT 0 CHECK (dropped_frames >= 0),
    peak_dbfs       REAL,
    rms_dbfs        REAL,
    clipped_samples INTEGER NOT NULL DEFAULT 0 CHECK (clipped_samples >= 0),
    status          TEXT    NOT NULL DEFAULT 'WRITTEN'
                    CHECK (status IN ('WRITTEN', 'TRUNCATED', 'MISSING', 'CORRUPT')),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT recording_chunks_filename_bare
        CHECK (filename NOT LIKE '%/%' AND filename NOT LIKE '%\%' AND length(trim(filename)) > 0)
);

CREATE UNIQUE INDEX ux_recording_chunks_seq ON recording_chunks (recording_id, seq);
CREATE INDEX ix_recording_chunks_status ON recording_chunks (status);


-- ---------------------------------------------------------------------------
-- jobs: the workflow instance for one meeting, and the single owner of the
-- state machine (see mom_igd/jobs/state_machine.py).
--
-- A job spans the whole lifecycle: DRAFT -> READY -> RECORDING -> RECORDED ->
-- QUEUED -> PROCESSING -> REVIEW_REQUIRED -> APPROVED, with FAILED and
-- CANCELLED as off-ramps. The CHECK constraint below must stay in sync with
-- JobState; tests assert that it does.
--
-- `resume_metadata_json` holds the information needed to continue an
-- interrupted run without redoing completed stages.
-- ---------------------------------------------------------------------------
CREATE TABLE jobs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id           INTEGER NOT NULL REFERENCES meetings (id) ON DELETE CASCADE,
    kind                 TEXT    NOT NULL DEFAULT 'MEETING_WORKFLOW'
                         CHECK (kind IN ('MEETING_WORKFLOW')),
    state                TEXT    NOT NULL DEFAULT 'DRAFT'
                         CHECK (state IN ('DRAFT', 'READY', 'RECORDING', 'RECORDED',
                                          'QUEUED', 'PROCESSING', 'REVIEW_REQUIRED',
                                          'APPROVED', 'FAILED', 'CANCELLED')),
    priority             INTEGER NOT NULL DEFAULT 0,
    attempt_count        INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    current_stage_id     INTEGER REFERENCES job_stages (id) ON DELETE SET NULL,
    resume_metadata_json TEXT,
    last_error           TEXT,
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    started_at           TEXT,
    finished_at          TEXT
);

CREATE INDEX ix_jobs_state ON jobs (state);
CREATE INDEX ix_jobs_meeting ON jobs (meeting_id);

-- At most one non-terminal workflow per meeting.
CREATE UNIQUE INDEX ux_jobs_one_active_per_meeting
    ON jobs (meeting_id)
    WHERE state NOT IN ('APPROVED', 'CANCELLED');


-- ---------------------------------------------------------------------------
-- job_stages: the ordered pipeline steps of a job, with checkpoints.
--
-- `is_heavy = 1` marks a stage that loads a heavy model. The orchestrator will
-- run at most one heavy stage at a time in its own short-lived worker process
-- (ADR-0004), which is what makes the memory budget hold on a 16 GB machine.
--
-- `provider_name`/`provider_version` are NULL in Phase 1: no AI provider has
-- been selected yet (ADR-0005).
-- ---------------------------------------------------------------------------
CREATE TABLE job_stages (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id               INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    seq                  INTEGER NOT NULL CHECK (seq >= 0),
    name                 TEXT    NOT NULL,
    status               TEXT    NOT NULL DEFAULT 'PENDING'
                         CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED',
                                           'FAILED', 'SKIPPED', 'CANCELLED')),
    is_heavy             INTEGER NOT NULL DEFAULT 0 CHECK (is_heavy IN (0, 1)),
    phase_introduced     TEXT,
    attempt_count        INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    progress_percent     REAL    NOT NULL DEFAULT 0
                         CHECK (progress_percent >= 0 AND progress_percent <= 100),
    checkpoint_json      TEXT,
    resume_metadata_json TEXT,
    provider_name        TEXT,
    provider_version     TEXT,
    peak_rss_mb          INTEGER CHECK (peak_rss_mb IS NULL OR peak_rss_mb >= 0),
    duration_ms          INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    last_error           TEXT,
    started_at           TEXT,
    finished_at          TEXT,
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT job_stages_name_not_blank CHECK (length(trim(name)) > 0)
);

CREATE UNIQUE INDEX ux_job_stages_job_seq ON job_stages (job_id, seq);
CREATE UNIQUE INDEX ux_job_stages_job_name ON job_stages (job_id, name);
CREATE INDEX ix_job_stages_status ON job_stages (status);


-- ---------------------------------------------------------------------------
-- audit_events: append-only, hash-chained record of everything that matters.
--
-- Each row stores the hash of the previous row, so removing or altering a row
-- in the middle of the chain is detectable (see mom_igd/audit.py). This is an
-- integrity mechanism, not encryption; encryption at rest is Phase 11.
--
-- The chain is intentionally established in Phase 1 so that no later migration
-- has to backfill hashes over existing audit history.
-- ---------------------------------------------------------------------------
CREATE TABLE audit_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    category     TEXT    NOT NULL
                 CHECK (category IN ('APP', 'DB', 'SECURITY', 'MEETING',
                                     'PARTICIPANT', 'RECORDING', 'JOB',
                                     'REVIEW', 'EXPORT', 'RETENTION')),
    action       TEXT    NOT NULL,
    entity_type  TEXT,
    entity_id    INTEGER,
    actor        TEXT    NOT NULL DEFAULT 'system',
    from_state   TEXT,
    to_state     TEXT,
    detail_json  TEXT,
    prev_hash    TEXT,
    event_hash   TEXT    NOT NULL CHECK (length(event_hash) = 64),
    CONSTRAINT audit_events_action_not_blank CHECK (length(trim(action)) > 0)
);

CREATE INDEX ix_audit_events_occurred ON audit_events (occurred_at);
CREATE INDEX ix_audit_events_entity ON audit_events (entity_type, entity_id);
CREATE INDEX ix_audit_events_category ON audit_events (category, action);
