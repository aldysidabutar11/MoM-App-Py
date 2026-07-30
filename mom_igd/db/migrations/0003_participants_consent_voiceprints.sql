-- ===========================================================================
-- MoM-IGD  migration 0003  --  participants, biometric consent, voiceprints
--                              (Phase 3)
--
-- Adds the four tables Phase 1 deferred (see the header of 0001), plus the two
-- columns `participants` needs to become UUID-addressed:
--
--   meeting_participants   link table, capped at 9 active rows per meeting
--   consent_events         APPEND-ONLY biometric consent history
--   enrollment_sessions    one voice-enrollment attempt
--   voiceprints            encrypted biometric templates (metadata only here)
--
-- WHAT THIS DOES *NOT* DO
--
-- No embedding, no ciphertext and no raw enrollment audio is stored in this
-- database. `voiceprints` holds non-biometric metadata plus a pointer to an
-- AES-256-GCM envelope under <data_root>/voiceprints/<uuid>.vpx. Every biometric
-- component lives inside that envelope (ADR-0010). A row here is useless to an
-- attacker who cannot also read the DPAPI-protected master key.
--
-- WHY `participants` IS ALTERED RATHER THAN REBUILT
--
-- Two changes are needed and neither touches a CHECK constraint, so the
-- destructive rebuild that 0002 had to perform is unnecessary here:
--
--   1. ADD COLUMN uuid            -- participants become UUID-addressed, so a
--                                    display name never becomes an identifier
--   2. DROP INDEX ux_participants_display_name
--
-- (2) is a deliberate reversal of a Phase 1 decision, and it is the whole reason
-- this migration touches `participants` at all. Phase 1 made `display_name`
-- UNIQUE. That is wrong for this product: two people in one organisation
-- genuinely share a name ("Budi"), and refusing the second one -- or forcing an
-- operator to invent "Budi 2" -- corrupts the registry to satisfy an index.
-- Identity is the UUID; the name is a label. See ADR-0009.
--
-- SQLite drops an index without rewriting the table, so no data moves and no
-- foreign key cascade is involved.
--
-- SECURE DELETION -- READ THIS BEFORE TRUSTING IT
--
-- Revoking consent deletes the voiceprint envelope from disk and clears the
-- pointer here. Two limits are real and are NOT hidden:
--
--   * `PRAGMA secure_delete` overwrites freed database pages, and this build
--     turns it on for the connection that deletes voiceprint metadata. It does
--     nothing for pages already copied into the -wal file, so a checkpoint is
--     forced afterwards.
--   * On an SSD, neither overwrite reaches the physical NAND: wear levelling and
--     over-provisioning keep old copies until the controller reuses them. Only
--     full-volume encryption makes deleted biometric data genuinely
--     unrecoverable.
--
-- The honest conclusion is that BitLocker is a Phase 11 requirement, not an
-- optional hardening step. Documented in ADR-0010 and
-- docs/phase-3-participants-enrollment.md.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- participants: a UUID identity, and the name is no longer unique.
-- ---------------------------------------------------------------------------
ALTER TABLE participants ADD COLUMN uuid TEXT;

-- Backfill with a v4 UUID, using the same expression 0002 used for meetings so
-- the two migrations cannot drift in format.
UPDATE participants
   SET uuid = lower(
           substr(hex(randomblob(4)), 1, 8) || '-' ||
           substr(hex(randomblob(2)), 1, 4) || '-4' ||
           substr(hex(randomblob(2)), 2, 3) || '-' ||
           substr('89ab', 1 + (abs(random()) % 4), 1) ||
           substr(hex(randomblob(2)), 2, 3) || '-' ||
           substr(hex(randomblob(6)), 1, 12)
       )
 WHERE uuid IS NULL;

CREATE UNIQUE INDEX ux_participants_uuid ON participants (uuid);

-- Duplicate display names are now allowed. Dropping the unique index leaves an
-- ordinary index behind so name search stays cheap.
DROP INDEX ux_participants_display_name;
CREATE INDEX ix_participants_display_name ON participants (display_name);


-- ---------------------------------------------------------------------------
-- meeting_participants: who is expected in which meeting.
--
-- The nine-participant cap is enforced in the service layer inside the same
-- transaction as the insert, because SQLite cannot express "at most 9 rows per
-- meeting_id" as a constraint. What the schema *can* guarantee is that the same
-- participant is never linked twice, which is the other half of the invariant.
--
-- `seat_label` is optional and descriptive ("kepala meja"). It is never a path
-- component and never an identifier.
-- ---------------------------------------------------------------------------
CREATE TABLE meeting_participants (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id     INTEGER NOT NULL REFERENCES meetings (id) ON DELETE CASCADE,
    participant_id INTEGER NOT NULL REFERENCES participants (id) ON DELETE RESTRICT,
    seat_label     TEXT,
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    added_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    removed_at     TEXT,
    CONSTRAINT meeting_participants_removed_after
        CHECK (removed_at IS NULL OR removed_at >= added_at)
);

-- ON DELETE RESTRICT above is deliberate: a participant who has been in a
-- meeting cannot be deleted out from under the history. Deactivation is the
-- supported lifecycle (ADR-0009).
CREATE UNIQUE INDEX ux_meeting_participants_pair
    ON meeting_participants (meeting_id, participant_id);
CREATE INDEX ix_meeting_participants_meeting
    ON meeting_participants (meeting_id, is_active);
CREATE INDEX ix_meeting_participants_participant
    ON meeting_participants (participant_id);


-- ---------------------------------------------------------------------------
-- consent_events: append-only biometric consent history.
--
-- There is NO `consents` table with a mutable `granted` flag, on purpose. A flag
-- would let one UPDATE erase the fact that consent was ever given or withdrawn,
-- and for biometric data that record is the point. Current state is derived:
-- the latest event per participant wins.
--
-- `consent_text_sha256` pins the exact wording the person agreed to. Changing
-- the text later produces a different hash, so a later reader can tell that a
-- stored consent refers to superseded wording rather than assuming equivalence.
--
-- No trigger enforces append-only; the service never issues UPDATE or DELETE and
-- a test asserts that. A trigger would also have to be bypassable for the
-- retention deletion that Phase 11 will need.
-- ---------------------------------------------------------------------------
CREATE TABLE consent_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uuid          TEXT    NOT NULL,
    participant_id      INTEGER NOT NULL REFERENCES participants (id) ON DELETE RESTRICT,
    action              TEXT    NOT NULL
                        CHECK (action IN ('GRANTED', 'REVOKED')),
    purpose             TEXT    NOT NULL,
    consent_version     TEXT    NOT NULL,
    consent_text_sha256 TEXT    NOT NULL CHECK (length(consent_text_sha256) = 64),
    confirmation_method TEXT    NOT NULL
                        CHECK (confirmation_method IN
                               ('OPERATOR_CONFIRMED_IN_PERSON',
                                'PARTICIPANT_CONFIRMED_ON_DEVICE')),
    actor               TEXT    NOT NULL DEFAULT 'local-operator',
    reason              TEXT,
    occurred_at         TEXT    NOT NULL
                        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT consent_events_purpose_not_blank CHECK (length(trim(purpose)) > 0),
    CONSTRAINT consent_events_version_not_blank
        CHECK (length(trim(consent_version)) > 0)
);

CREATE UNIQUE INDEX ux_consent_events_uuid ON consent_events (event_uuid);
-- The derived-state query is "latest event for this participant", so index the
-- participant with the ordering column.
CREATE INDEX ix_consent_events_participant
    ON consent_events (participant_id, id DESC);
CREATE INDEX ix_consent_events_action ON consent_events (action);


-- ---------------------------------------------------------------------------
-- enrollment_sessions: one attempt at building a voiceprint.
--
-- Kept even when it fails: a rejected attempt and its reason are what tell an
-- operator why enrolment is not working. The row holds quality *metrics*, which
-- are statistics about audio, not audio and not biometric features.
--
-- At most one session may be non-terminal at a time. That is enforced by the
-- partial unique index below plus the Phase 2 capture lock, for the same reason
-- Phase 2 guards recordings twice: the index survives a deleted lock file, and
-- the lock file stops a second *process* before it reaches the microphone.
-- ---------------------------------------------------------------------------
CREATE TABLE enrollment_sessions (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_uuid             TEXT    NOT NULL,
    participant_id           INTEGER NOT NULL REFERENCES participants (id) ON DELETE RESTRICT,
    consent_event_id         INTEGER REFERENCES consent_events (id) ON DELETE RESTRICT,
    state                    TEXT    NOT NULL DEFAULT 'CREATED'
                             CHECK (state IN ('CREATED', 'CONSENT_REQUIRED', 'READY',
                                              'CAPTURING', 'VALIDATING', 'EMBEDDING',
                                              'ENCRYPTING', 'COMPLETED', 'REJECTED',
                                              'CANCELLED', 'FAILED')),
    attempt                  INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    samples_target           INTEGER NOT NULL DEFAULT 5 CHECK (samples_target >= 1),
    samples_accepted         INTEGER NOT NULL DEFAULT 0 CHECK (samples_accepted >= 0),
    samples_rejected         INTEGER NOT NULL DEFAULT 0 CHECK (samples_rejected >= 0),
    speech_seconds           REAL    NOT NULL DEFAULT 0 CHECK (speech_seconds >= 0),
    total_seconds            REAL    NOT NULL DEFAULT 0 CHECK (total_seconds >= 0),
    -- capture provenance, mirroring the Phase 2 recording columns
    device_fingerprint       TEXT,
    device_name              TEXT,
    device_transport         TEXT,
    device_transport_verified INTEGER NOT NULL DEFAULT 0
                             CHECK (device_transport_verified IN (0, 1)),
    sample_rate_hz           INTEGER CHECK (sample_rate_hz IS NULL OR sample_rate_hz > 0),
    channels                 INTEGER CHECK (channels IS NULL OR channels BETWEEN 1 AND 2),
    sample_format            TEXT,
    calibration_utc          TEXT,
    calibration_verdict      TEXT,
    -- quality metrics: statistics about audio, never audio itself
    peak_dbfs                REAL,
    rms_dbfs                 REAL,
    noise_floor_dbfs         REAL,
    estimated_snr_db         REAL,
    clipping_percent         REAL CHECK (clipping_percent IS NULL OR clipping_percent >= 0),
    silence_percent          REAL CHECK (silence_percent IS NULL OR silence_percent >= 0),
    speech_active_ratio      REAL CHECK (speech_active_ratio IS NULL
                                         OR (speech_active_ratio >= 0
                                             AND speech_active_ratio <= 1)),
    dropped_frames           INTEGER NOT NULL DEFAULT 0 CHECK (dropped_frames >= 0),
    xrun_callbacks           INTEGER NOT NULL DEFAULT 0 CHECK (xrun_callbacks >= 0),
    min_pair_cosine          REAL,
    mean_pair_cosine         REAL,
    quality_verdict          TEXT,
    reason_code              TEXT,
    last_error               TEXT,
    started_at               TEXT,
    finished_at              TEXT,
    created_at               TEXT    NOT NULL
                             DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at               TEXT    NOT NULL
                             DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT enrollment_sessions_finished_after
        CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at)
);

CREATE UNIQUE INDEX ux_enrollment_sessions_uuid
    ON enrollment_sessions (session_uuid);
CREATE INDEX ix_enrollment_sessions_participant
    ON enrollment_sessions (participant_id, id DESC);
CREATE INDEX ix_enrollment_sessions_state ON enrollment_sessions (state);

-- One live enrollment at a time, across the whole database. The expression
-- `(1)` gives every in-flight row the same index key, so the second insert
-- fails rather than racing. Same technique as ux_recordings_single_active.
CREATE UNIQUE INDEX ux_enrollment_single_active ON enrollment_sessions ((1))
    WHERE state IN ('CREATED', 'CONSENT_REQUIRED', 'READY', 'CAPTURING',
                    'VALIDATING', 'EMBEDDING', 'ENCRYPTING');


-- ---------------------------------------------------------------------------
-- voiceprints: metadata for one encrypted biometric template.
--
-- EVERY BIOMETRIC COMPONENT IS INSIDE THE ENVELOPE, NOT HERE. The centroid,
-- the dispersion vector and the per-sample embeddings live in the AES-256-GCM
-- ciphertext at <data_root>/voiceprints/<voiceprint_uuid>.vpx. This table keeps
-- only what indexing and auditing need.
--
-- `embedding_dim` and `sample_count` are shape, not content: knowing a template
-- has 192 dimensions reveals nothing about a voice, and Phase 6 needs both to
-- reject a mismatched model before attempting a decrypt.
--
-- LIFECYCLE
--   PENDING_WRITE     row claimed, envelope not yet proven; NEVER usable. This
--                     is the crash-recovery anchor: a row in this state means a
--                     save was in flight, so recovery knows to look for a
--                     temporary file rather than guessing.
--   ACTIVE            the one template Phase 6 may use
--   DEVELOPMENT_ONLY  usable for development; NOT production eligible, because
--                     capture did not use a verified USB conference microphone
--   SUPERSEDED        replaced by a re-enrollment; ciphertext already deleted
--   RE_ENROLL_REQUIRED the production device changed, so the template no longer
--                     matches the microphone it will be compared against
--   REVOKED           consent withdrawn; ciphertext deleted
--   DELETE_PENDING    consent withdrawn but envelope deletion FAILED; already
--                     unusable, and cleanup must be retried (ADR-0009)
--   INTEGRITY_FAILED  the envelope did not authenticate; never usable again
--
-- At most one row per participant may be ACTIVE or DEVELOPMENT_ONLY. Without
-- that index a failed re-enrollment could leave two live templates for one
-- person and Phase 6 would have no defensible way to choose.
-- ---------------------------------------------------------------------------
CREATE TABLE voiceprints (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    voiceprint_uuid       TEXT    NOT NULL,
    participant_id        INTEGER NOT NULL REFERENCES participants (id) ON DELETE RESTRICT,
    enrollment_session_id INTEGER REFERENCES enrollment_sessions (id) ON DELETE SET NULL,
    consent_event_id      INTEGER REFERENCES consent_events (id) ON DELETE RESTRICT,
    status                TEXT    NOT NULL DEFAULT 'PENDING_WRITE'
                          CHECK (status IN ('PENDING_WRITE', 'ACTIVE', 'DEVELOPMENT_ONLY',
                                            'SUPERSEDED', 'RE_ENROLL_REQUIRED',
                                            'REVOKED', 'DELETE_PENDING',
                                            'INTEGRITY_FAILED')),
    -- envelope pointer and integrity, never the payload
    envelope_relative_path TEXT,
    envelope_schema        INTEGER NOT NULL DEFAULT 1 CHECK (envelope_schema >= 1),
    envelope_sha256        TEXT CHECK (envelope_sha256 IS NULL
                                       OR length(envelope_sha256) = 64),
    envelope_bytes         INTEGER CHECK (envelope_bytes IS NULL OR envelope_bytes >= 0),
    cipher_suite           TEXT    NOT NULL DEFAULT 'AES-256-GCM',
    key_id                 TEXT,
    -- model provenance: Phase 6 must refuse to compare across models
    model_name             TEXT    NOT NULL,
    model_version          TEXT    NOT NULL,
    model_sha256           TEXT CHECK (model_sha256 IS NULL OR length(model_sha256) = 64),
    preprocessing_id       TEXT,
    embedding_dim          INTEGER NOT NULL CHECK (embedding_dim > 0),
    sample_count           INTEGER NOT NULL CHECK (sample_count > 0),
    -- capture provenance, so a device change can invalidate the template
    device_fingerprint     TEXT,
    device_transport       TEXT,
    sample_rate_hz         INTEGER CHECK (sample_rate_hz IS NULL OR sample_rate_hz > 0),
    channels               INTEGER CHECK (channels IS NULL OR channels BETWEEN 1 AND 2),
    production_eligible    INTEGER NOT NULL DEFAULT 0
                           CHECK (production_eligible IN (0, 1)),
    quality_verdict        TEXT,
    min_pair_cosine        REAL,
    -- lifecycle bookkeeping
    activated_at           TEXT,
    superseded_at          TEXT,
    revoked_at             TEXT,
    deleted_at             TEXT,
    delete_error           TEXT,
    integrity_checked_at   TEXT,
    created_at             TEXT    NOT NULL
                           DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at             TEXT    NOT NULL
                           DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- A live template must point at an envelope; a dead one must not.
    --
    -- PENDING_WRITE is included deliberately. In the save protocol the row is
    -- inserted *after* the envelope has been hashed but *before* the atomic rename
    -- (see mom_igd/enrollment/store.py). Requiring both fields here is what makes
    -- crash recovery possible at all: without the expected path and hash, a
    -- pending row could not be told apart from a corrupt one, and recovery would
    -- have to guess. Guessing about biometric data is not acceptable.
    CONSTRAINT voiceprints_live_has_envelope CHECK (
        (status NOT IN ('PENDING_WRITE', 'ACTIVE', 'DEVELOPMENT_ONLY'))
        OR (envelope_relative_path IS NOT NULL AND envelope_sha256 IS NOT NULL)
    ),
    CONSTRAINT voiceprints_dead_has_no_envelope CHECK (
        (status NOT IN ('REVOKED', 'SUPERSEDED'))
        OR envelope_relative_path IS NULL
    ),
    -- DEVELOPMENT_ONLY exists precisely because it is not production eligible.
    CONSTRAINT voiceprints_development_not_eligible CHECK (
        status <> 'DEVELOPMENT_ONLY' OR production_eligible = 0
    )
);

CREATE UNIQUE INDEX ux_voiceprints_uuid ON voiceprints (voiceprint_uuid);
CREATE UNIQUE INDEX ux_voiceprints_one_live_per_participant
    ON voiceprints (participant_id)
    WHERE status IN ('ACTIVE', 'DEVELOPMENT_ONLY');
CREATE INDEX ix_voiceprints_participant ON voiceprints (participant_id, id DESC);
CREATE INDEX ix_voiceprints_status ON voiceprints (status);
CREATE INDEX ix_voiceprints_cleanup ON voiceprints (status)
    WHERE status = 'DELETE_PENDING';
