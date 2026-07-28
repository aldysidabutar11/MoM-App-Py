-- ===========================================================================
-- MoM-IGD  migration 0002  --  audio capture (Phase 2)
--
-- Extends the two tables Phase 1 already created. No new table is added: the
-- Phase 1 schema was designed for this, and `recordings` / `recording_chunks`
-- carry everything Phase 2 needs once the columns below exist.
--
-- WHY THIS REBUILDS TWO TABLES INSTEAD OF USING ALTER TABLE
--
-- Both tables carry a CHECK constraint on their status column, and SQLite cannot
-- modify a CHECK constraint in place. Phase 2 replaces the coarse Phase 1
-- vocabulary with the recording lifecycle the capture engine actually drives, so
-- the constraint has to change. The alternative -- keeping the old column and
-- adding a second "capture_state" beside it -- would create two sources of
-- truth for one fact, which is exactly the failure this project avoids
-- elsewhere (see the note on `meetings` having no state column in 0001).
--
-- The rebuild runs inside the migrator's single transaction, with foreign keys
-- enabled, in an order that cannot lose data:
--
--   1. create both replacement tables, with the new child's foreign key pointing
--      at the new parent (`recordings_v2`), NOT at `recordings`;
--   2. copy every row, mapping the old status vocabulary to the new one;
--   3. drop the old child, then the old parent;
--   4. rename the parent, which makes SQLite rewrite the child's foreign key to
--      the new name;
--   5. rename the child.
--
-- Step 1 is the subtle part, and getting it wrong destroys data. With foreign
-- keys enabled, DROP TABLE performs an implicit DELETE of every row. If the new
-- child referenced `recordings`, dropping the old `recordings` would cascade and
-- delete the chunk rows that had just been copied into the new table -- the old
-- child being gone does not help, because the *new* child is the live referrer.
-- Pointing the new child at `recordings_v2` keeps it out of that cascade, and
-- `ALTER TABLE ... RENAME TO` (SQLite 3.25+, with legacy_alter_table off) updates
-- referencing tables, so the foreign key ends up naming `recordings` as intended.
-- A test asserts the chunk rows survive, because a comment cannot enforce this.
--
-- STATUS VOCABULARY MAPPING (Phase 1 -> Phase 2)
--   PENDING    -> IDLE         not started
--   CAPTURING  -> RECOVERABLE  an interrupted capture found at migration time is
--                              by definition awaiting recovery
--   COMPLETED  -> RECORDED
--   RECOVERED  -> RECORDED     "was recovered" is now a fact recorded in
--                              recovered_chunks / recovery_notes, not a state
--   FAILED     -> FAILED
--   DISCARDED  -> CANCELLED
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- meetings: a UUID, so the on-disk layout never contains a meeting title.
--
-- Recording paths are <data_root>/recordings/<meeting_uuid>/<recording_uuid>/.
-- File and directory names leak into backups, file pickers and error messages,
-- so they must not carry a meeting title or a participant name.
-- ---------------------------------------------------------------------------
ALTER TABLE meetings ADD COLUMN uuid TEXT;

UPDATE meetings
   SET uuid = lower(
           substr(hex(randomblob(4)), 1, 8) || '-' ||
           substr(hex(randomblob(2)), 1, 4) || '-4' ||
           substr(hex(randomblob(2)), 2, 3) || '-' ||
           substr('89ab', 1 + (abs(random()) % 4), 1) ||
           substr(hex(randomblob(2)), 2, 3) || '-' ||
           substr(hex(randomblob(6)), 1, 12)
       )
 WHERE uuid IS NULL;

CREATE UNIQUE INDEX ux_meetings_uuid ON meetings (uuid);


-- ---------------------------------------------------------------------------
-- recordings: the Phase 2 lifecycle plus capture, device and quality metadata.
-- ---------------------------------------------------------------------------
CREATE TABLE recordings_v2 (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id           INTEGER NOT NULL REFERENCES meetings (id) ON DELETE CASCADE,
    recording_uuid       TEXT    NOT NULL,
    relative_dir         TEXT    NOT NULL,

    -- Lifecycle. Drives, and is driven by, mom_igd/audio/service.py. `jobs`
    -- remains the owner of the meeting workflow; this is the capture detail.
    status               TEXT    NOT NULL DEFAULT 'IDLE'
                         CHECK (status IN ('IDLE', 'PREFLIGHT', 'ARMED', 'RECORDING',
                                           'PAUSED', 'STOPPING', 'FINALIZING',
                                           'RECORDED', 'RECOVERABLE', 'FAILED',
                                           'CANCELLED')),

    -- Capture format. No compression in Phase 2.
    container            TEXT    NOT NULL DEFAULT 'wav'
                         CHECK (container IN ('wav')),
    sample_rate_hz       INTEGER CHECK (sample_rate_hz IS NULL OR sample_rate_hz > 0),
    channels             INTEGER CHECK (channels IS NULL OR (channels >= 1 AND channels <= 2)),
    sample_format        TEXT    NOT NULL DEFAULT 'int16'
                         CHECK (sample_format IN ('int16')),
    bit_depth            INTEGER CHECK (bit_depth IS NULL OR bit_depth > 0),
    chunk_seconds        INTEGER CHECK (chunk_seconds IS NULL OR
                                        (chunk_seconds >= 10 AND chunk_seconds <= 120)),

    -- Device identity. The fingerprint is the identity; the PortAudio index is a
    -- transient hint kept only for diagnostics.
    device_fingerprint   TEXT,
    device_name          TEXT,
    device_host_api      TEXT,
    device_transport     TEXT    CHECK (device_transport IS NULL OR
                                        device_transport IN ('USB', 'INTERNAL',
                                                             'BLUETOOTH', 'UNKNOWN')),
    device_transport_verified INTEGER NOT NULL DEFAULT 0
                         CHECK (device_transport_verified IN (0, 1)),
    device_index_hint    INTEGER,
    device_snapshot_json TEXT,

    -- Timing. Monotonic values order and measure; UTC values are for humans.
    started_at           TEXT,
    ended_at             TEXT,
    monotonic_start_ns   INTEGER,
    monotonic_end_ns     INTEGER,
    duration_ms          INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    paused_ms            INTEGER NOT NULL DEFAULT 0 CHECK (paused_ms >= 0),
    pause_count          INTEGER NOT NULL DEFAULT 0 CHECK (pause_count >= 0),

    -- Accounting. Loss is recorded, never smoothed over.
    expected_frames      INTEGER,
    written_frames       INTEGER NOT NULL DEFAULT 0 CHECK (written_frames >= 0),
    dropped_frames       INTEGER NOT NULL DEFAULT 0 CHECK (dropped_frames >= 0),
    xrun_callbacks       INTEGER NOT NULL DEFAULT 0 CHECK (xrun_callbacks >= 0),
    queue_high_water_frames INTEGER NOT NULL DEFAULT 0
                         CHECK (queue_high_water_frames >= 0),
    chunk_count          INTEGER NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    total_bytes          INTEGER NOT NULL DEFAULT 0 CHECK (total_bytes >= 0),

    -- Manifest. Path is relative to <data_root>/recordings.
    manifest_relative_path TEXT,
    manifest_sha256      TEXT CHECK (manifest_sha256 IS NULL OR length(manifest_sha256) = 64),
    manifest_status      TEXT NOT NULL DEFAULT 'PENDING'
                         CHECK (manifest_status IN ('PENDING', 'WRITTEN', 'VERIFIED',
                                                    'MISMATCH', 'MISSING')),

    -- Quality summary, so a degraded recording is visible without reopening audio.
    peak_dbfs            REAL,
    rms_dbfs             REAL,
    clipped_samples      INTEGER NOT NULL DEFAULT 0 CHECK (clipped_samples >= 0),
    quality_verdict      TEXT,
    degraded             INTEGER NOT NULL DEFAULT 0 CHECK (degraded IN (0, 1)),

    -- Recovery and failure.
    recovered_chunks     INTEGER NOT NULL DEFAULT 0 CHECK (recovered_chunks >= 0),
    quarantined_chunks   INTEGER NOT NULL DEFAULT 0 CHECK (quarantined_chunks >= 0),
    recovery_notes       TEXT,
    last_error           TEXT,

    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT recordings_relative_dir_relative
        CHECK (relative_dir NOT LIKE '%:%' AND relative_dir NOT LIKE '/%'
               AND relative_dir NOT LIKE '\%' AND relative_dir NOT LIKE '%..%'
               AND length(trim(relative_dir)) > 0),
    CONSTRAINT recordings_manifest_path_relative
        CHECK (manifest_relative_path IS NULL OR
               (manifest_relative_path NOT LIKE '%:%'
                AND manifest_relative_path NOT LIKE '/%'
                AND manifest_relative_path NOT LIKE '\%'
                AND manifest_relative_path NOT LIKE '%..%'))
);

INSERT INTO recordings_v2 (
    id, meeting_id, recording_uuid, relative_dir, status, container,
    sample_rate_hz, channels, bit_depth, device_name, device_fingerprint,
    started_at, ended_at, duration_ms, expected_frames, written_frames,
    dropped_frames, manifest_sha256, created_at, updated_at
)
SELECT
    id,
    meeting_id,
    lower(
        substr(hex(randomblob(4)), 1, 8) || '-' ||
        substr(hex(randomblob(2)), 1, 4) || '-4' ||
        substr(hex(randomblob(2)), 2, 3) || '-' ||
        substr('89ab', 1 + (abs(random()) % 4), 1) ||
        substr(hex(randomblob(2)), 2, 3) || '-' ||
        substr(hex(randomblob(6)), 1, 12)
    ),
    relative_dir,
    CASE status
        WHEN 'PENDING'   THEN 'IDLE'
        WHEN 'CAPTURING' THEN 'RECOVERABLE'
        WHEN 'COMPLETED' THEN 'RECORDED'
        WHEN 'RECOVERED' THEN 'RECORDED'
        WHEN 'FAILED'    THEN 'FAILED'
        WHEN 'DISCARDED' THEN 'CANCELLED'
        ELSE 'IDLE'
    END,
    'wav',
    sample_rate_hz,
    CASE WHEN channels IS NULL THEN NULL
         WHEN channels > 2 THEN 2
         ELSE channels END,
    bit_depth,
    device_name,
    device_id,
    started_at,
    ended_at,
    duration_ms,
    expected_frames,
    COALESCE(written_frames, 0),
    COALESCE(dropped_frames, 0),
    manifest_sha256,
    created_at,
    updated_at
FROM recordings;


-- ---------------------------------------------------------------------------
-- recording_chunks: per-chunk integrity and recovery detail.
-- ---------------------------------------------------------------------------
-- The foreign key names `recordings_v2` on purpose; see the header. The rename
-- below rewrites it to `recordings`.
CREATE TABLE recording_chunks_v2 (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id    INTEGER NOT NULL REFERENCES recordings_v2 (id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL CHECK (seq >= 0),
    filename        TEXT    NOT NULL,

    start_frame     INTEGER NOT NULL DEFAULT 0 CHECK (start_frame >= 0),
    end_frame       INTEGER NOT NULL DEFAULT 0 CHECK (end_frame >= 0),
    frames          INTEGER NOT NULL DEFAULT 0 CHECK (frames >= 0),
    duration_ms     REAL    CHECK (duration_ms IS NULL OR duration_ms >= 0),

    utc_start       TEXT,
    utc_end         TEXT,
    monotonic_start_ns INTEGER,
    monotonic_end_ns   INTEGER,

    sample_rate_hz  INTEGER CHECK (sample_rate_hz IS NULL OR sample_rate_hz > 0),
    channels        INTEGER CHECK (channels IS NULL OR (channels >= 1 AND channels <= 2)),
    sample_format   TEXT    NOT NULL DEFAULT 'int16'
                    CHECK (sample_format IN ('int16')),

    size_bytes      INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    sha256          TEXT    CHECK (sha256 IS NULL OR length(sha256) = 64),

    dropped_frames  INTEGER NOT NULL DEFAULT 0 CHECK (dropped_frames >= 0),
    xrun_callbacks  INTEGER NOT NULL DEFAULT 0 CHECK (xrun_callbacks >= 0),
    peak_dbfs       REAL,
    rms_dbfs        REAL,
    clipped_samples INTEGER NOT NULL DEFAULT 0 CHECK (clipped_samples >= 0),

    status          TEXT    NOT NULL DEFAULT 'WRITTEN'
                    CHECK (status IN ('WRITING', 'WRITTEN', 'TRUNCATED', 'MISSING',
                                      'CORRUPT', 'QUARANTINED')),
    recovery_status TEXT    NOT NULL DEFAULT 'NONE'
                    CHECK (recovery_status IN ('NONE', 'RECOVERED', 'RECOVERY_FAILED')),
    finalized       INTEGER NOT NULL DEFAULT 1 CHECK (finalized IN (0, 1)),
    notes           TEXT,

    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT recording_chunks_filename_bare
        CHECK (filename NOT LIKE '%/%' AND filename NOT LIKE '%\%'
               AND filename NOT LIKE '%..%' AND filename NOT LIKE '%:%'
               AND length(trim(filename)) > 0),
    CONSTRAINT recording_chunks_frame_range
        CHECK (end_frame >= start_frame)
);

INSERT INTO recording_chunks_v2 (
    id, recording_id, seq, filename, start_frame, end_frame, frames,
    sample_rate_hz, channels, size_bytes, sha256, dropped_frames,
    peak_dbfs, rms_dbfs, clipped_samples, status, created_at
)
SELECT
    id,
    recording_id,
    seq,
    filename,
    COALESCE(sample_offset, 0),
    COALESCE(sample_offset, 0) + COALESCE(frames, 0),
    COALESCE(frames, 0),
    NULL,
    NULL,
    size_bytes,
    sha256,
    COALESCE(dropped_frames, 0),
    peak_dbfs,
    rms_dbfs,
    COALESCE(clipped_samples, 0),
    status,
    created_at
FROM recording_chunks;


-- Old child first, then the old parent. The new child references `recordings_v2`,
-- so the implicit DELETE that DROP TABLE performs on `recordings` cannot cascade
-- into the rows just copied.
DROP TABLE recording_chunks;
DROP TABLE recordings;

-- Renaming the parent rewrites recording_chunks_v2's foreign key to `recordings`.
ALTER TABLE recordings_v2 RENAME TO recordings;
ALTER TABLE recording_chunks_v2 RENAME TO recording_chunks;


-- ---------------------------------------------------------------------------
-- Indexes and invariants
-- ---------------------------------------------------------------------------
CREATE INDEX ix_recordings_meeting ON recordings (meeting_id);
CREATE INDEX ix_recordings_status ON recordings (status);
CREATE UNIQUE INDEX ux_recordings_uuid ON recordings (recording_uuid);
CREATE UNIQUE INDEX ux_recordings_relative_dir ON recordings (relative_dir);

-- At most one capture in flight across the whole data root. This is the database
-- half of the single-active-recording guarantee; the other half is a lock file,
-- because a second process must be refused before it opens the microphone.
CREATE UNIQUE INDEX ux_recordings_single_active
    ON recordings ((1))
    WHERE status IN ('PREFLIGHT', 'ARMED', 'RECORDING', 'PAUSED', 'STOPPING',
                     'FINALIZING');

CREATE UNIQUE INDEX ux_recording_chunks_seq ON recording_chunks (recording_id, seq);
CREATE INDEX ix_recording_chunks_status ON recording_chunks (status);
CREATE INDEX ix_recording_chunks_recovery ON recording_chunks (recovery_status);
