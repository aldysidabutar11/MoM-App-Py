-- ===========================================================================
-- MoM-IGD  migration 0005  --  offline ASR (Phase 4)
--
-- WHAT THIS ADDS
--
-- The evidence chain from a recording to a transcript, one link per table:
--
--   recordings          (Phase 2, untouched)
--     -> audio_working_copies   the 16 kHz mono derivative the models read
--          -> vad_runs          one voice-activity pass over that derivative
--               -> speech_regions
--          -> transcripts       one revision of a transcript
--               -> transcript_segments
--                    -> transcript_words
--
-- Every link records the provenance of the thing above it -- a SHA-256, a model
-- revision, a configuration hash -- so a transcript can always be traced back to
-- the exact bytes and the exact model that produced it. Phase 8 verifies that
-- chain. Nothing here can be reconstructed later if it is not stored now.
--
-- NOTHING IN PHASE 2 IS MODIFIED
--
-- No ALTER on `recordings` or `recording_chunks`, and no table rebuild. The
-- master audio is read-only to this phase: a working copy is a *new* file in a
-- separate directory, and the row below points at it. If a working copy is
-- deleted, the master is still authoritative and the copy is rebuilt.
--
-- WHY A WORKING COPY IS A ROW AND NOT JUST A FILE
--
-- Because a 16 kHz mono copy is a *derivation*, and a derivation with no recorded
-- provenance is indistinguishable from a stray file. The row records which
-- master manifest it came from, what it hashes to, how long it is, and -- the
-- part that matters most -- how gaps in the master were handled.
--
-- WHY GAPS ARE FILLED IN THE COPY AND RECORDED IN THE ROW
--
-- Phase 2's rule is that a gap in the *master* is recorded and never filled: the
-- master is evidence, and synthesising audio into evidence is forgery. The reason
-- the rule exists, stated in CLAUDE.md, is that "an invisible gap shifts every
-- downstream timestamp".
--
-- A working copy is not evidence -- it is an input to a model, and its whole
-- purpose is to carry the master's timeline. So each chunk is placed at the frame
-- offset the master recorded for it, and a hole between two chunks becomes
-- explicit silence. That keeps every transcript timestamp equal to a wall-clock
-- offset into the meeting. The gap does not become invisible: `gap_count`,
-- `gap_total_ms` and `gaps_json` record every one of them, a region that overlaps
-- one is flagged, and the reviewer sees it. Filled *and* recorded is the only
-- combination that keeps both the timeline and the truth.
--
-- WHY REVISIONS INSTEAD OF UPDATES
--
-- Re-running the pipeline writes a new transcript revision and deactivates the
-- old one. It never edits segments in place. The same reasoning as APPROVED being
-- terminal in the job state machine: a reviewer who has read a transcript must be
-- able to see what changed underneath them, and Phase 7 reconciliation needs the
-- earlier revision to diff against.
--
-- WHY PASS-2 SUPERSEDES RATHER THAN REPLACES
--
-- Pass 2 re-transcribes selected regions with a slower, more accurate
-- configuration. Its segments are inserted with `asr_pass = 2` and the pass-1
-- segments they cover are marked inactive and pointed at their replacement. Both
-- rows survive. That is what makes "pass 2 improved this region" a checkable
-- claim rather than an assertion, and it is the only way to measure whether pass 2
-- was worth its budget.
--
-- WHAT IS DELIBERATELY ABSENT
--
-- No speaker column anywhere. Diarization is Phase 5 and voice identification is
-- Phase 6, and each owns its own schema. A `speaker_id` column added now would sit
-- NULL for two phases and invite something to write a guess into it. Phase 4
-- assigns no speaker at all: `mom_igd/asr/provider.py` rejects any transcription
-- result that carries one.
--
-- No text search index either. Phase 7 owns review and search, and an FTS table
-- built before its query patterns are known is an FTS table rebuilt later.
--
-- TIME UNITS
--
-- Whole milliseconds, as INTEGER. The engine reports seconds as floats, and the
-- conversion happens once, at persistence, rather than being re-derived by every
-- reader. Millisecond resolution is finer than the models produce -- word
-- timestamps land on ~20 ms boundaries -- and integers compare and sum exactly,
-- which floats in a database do not.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- audio_working_copies: the 16 kHz mono PCM16 derivative the models read.
--
-- One per recording. Re-normalising replaces the row and rewrites the file: the
-- copy is reproducible from the master, so it is a cache with provenance rather
-- than a record. The master it derives from is never touched.
-- ---------------------------------------------------------------------------
CREATE TABLE audio_working_copies (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id           INTEGER NOT NULL
                           REFERENCES recordings (id) ON DELETE CASCADE,

    -- Relative to <data_root>. Paths are never absolute in the database: the data
    -- root moves between machines and a backup restored elsewhere must still work.
    relative_path          TEXT    NOT NULL,
    sha256                 TEXT    CHECK (sha256 IS NULL OR length(sha256) = 64),
    size_bytes             INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),

    -- The working-copy format is fixed by the ASR stack, not configurable. A
    -- CHECK here rather than a comment, because a 44.1 kHz stereo "working copy"
    -- would be silently resampled inside the engine and every timestamp would
    -- still look plausible.
    sample_rate_hz         INTEGER NOT NULL DEFAULT 16000
                           CHECK (sample_rate_hz = 16000),
    channels               INTEGER NOT NULL DEFAULT 1 CHECK (channels = 1),
    sample_format          TEXT    NOT NULL DEFAULT 'int16'
                           CHECK (sample_format = 'int16'),
    frames                 INTEGER NOT NULL DEFAULT 0 CHECK (frames >= 0),
    duration_ms            INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),

    -- Provenance of the master this was derived from.
    source_manifest_sha256 TEXT    CHECK (source_manifest_sha256 IS NULL OR
                                          length(source_manifest_sha256) = 64),
    source_chunk_count     INTEGER NOT NULL DEFAULT 0 CHECK (source_chunk_count >= 0),
    source_sample_rate_hz  INTEGER CHECK (source_sample_rate_hz IS NULL OR
                                          source_sample_rate_hz > 0),
    source_channels        INTEGER CHECK (source_channels IS NULL OR source_channels >= 1),
    source_frames          INTEGER NOT NULL DEFAULT 0 CHECK (source_frames >= 0),

    -- Gaps: filled with silence in the copy so the timeline survives, and recorded
    -- here so they stay visible. See the header.
    gap_count              INTEGER NOT NULL DEFAULT 0 CHECK (gap_count >= 0),
    gap_total_ms           INTEGER NOT NULL DEFAULT 0 CHECK (gap_total_ms >= 0),
    gaps_json              TEXT,

    -- Level summary, so an unusable working copy is visible without opening it.
    peak_dbfs              REAL,
    rms_dbfs               REAL,
    clipped_samples        INTEGER NOT NULL DEFAULT 0 CHECK (clipped_samples >= 0),

    status                 TEXT    NOT NULL DEFAULT 'BUILDING'
                           CHECK (status IN ('BUILDING', 'READY', 'STALE', 'MISSING',
                                             'FAILED')),
    last_error             TEXT,
    created_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT working_copy_path_relative
        CHECK (relative_path NOT LIKE '%:%' AND relative_path NOT LIKE '/%'
               AND relative_path NOT LIKE '\%' AND relative_path NOT LIKE '%..%'
               AND length(trim(relative_path)) > 0)
);

CREATE UNIQUE INDEX ux_working_copies_recording ON audio_working_copies (recording_id);
CREATE UNIQUE INDEX ux_working_copies_path ON audio_working_copies (relative_path);
CREATE INDEX ix_working_copies_status ON audio_working_copies (status);


-- ---------------------------------------------------------------------------
-- vad_runs: one voice-activity pass over one working copy.
--
-- A row per run, not per working copy: a changed threshold produces a different
-- segmentation, and the transcript that was built from the old one must still be
-- able to say which regions it used. `config_hash` makes "same settings" a
-- comparison rather than a judgement.
-- ---------------------------------------------------------------------------
CREATE TABLE vad_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    working_copy_id  INTEGER NOT NULL
                     REFERENCES audio_working_copies (id) ON DELETE CASCADE,

    model_name       TEXT    NOT NULL,
    model_sha256     TEXT    NOT NULL CHECK (length(model_sha256) = 64),
    config_hash      TEXT    NOT NULL CHECK (length(config_hash) = 64),
    config_json      TEXT    NOT NULL,

    audio_ms         INTEGER NOT NULL DEFAULT 0 CHECK (audio_ms >= 0),
    region_count     INTEGER NOT NULL DEFAULT 0 CHECK (region_count >= 0),
    total_speech_ms  INTEGER NOT NULL DEFAULT 0 CHECK (total_speech_ms >= 0),
    merged_count     INTEGER NOT NULL DEFAULT 0 CHECK (merged_count >= 0),
    split_count      INTEGER NOT NULL DEFAULT 0 CHECK (split_count >= 0),
    dropped_short_count INTEGER NOT NULL DEFAULT 0 CHECK (dropped_short_count >= 0),

    -- Zero regions is a legitimate answer -- a recording of an empty room has no
    -- speech in it -- so it is not an error state. It is, however, something the
    -- operator must be told about rather than left to infer from an empty
    -- transcript.
    is_active        INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX ix_vad_runs_working_copy ON vad_runs (working_copy_id);

-- At most one active VAD run per working copy. The others are kept as evidence of
-- what an earlier transcript was built from.
CREATE UNIQUE INDEX ux_vad_runs_one_active
    ON vad_runs (working_copy_id)
    WHERE is_active = 1;


-- ---------------------------------------------------------------------------
-- speech_regions: the bounded, ordered, non-overlapping spans VAD produced.
--
-- These are the unit of work for transcription and the unit of cancellation. The
-- 30-second bound is applied by the VAD stage, not here: it is a property of the
-- splitting algorithm and would need a table rebuild to change if it were a CHECK.
-- ---------------------------------------------------------------------------
CREATE TABLE speech_regions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    vad_run_id    INTEGER NOT NULL REFERENCES vad_runs (id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL CHECK (seq >= 0),

    start_ms      INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms        INTEGER NOT NULL CHECK (end_ms >= 0),

    -- Set when this region overlaps a gap that was filled with silence in the
    -- working copy. The audio under it is partly synthetic, so a reviewer must be
    -- able to see that before trusting what was transcribed there.
    overlaps_gap  INTEGER NOT NULL DEFAULT 0 CHECK (overlaps_gap IN (0, 1)),

    CONSTRAINT speech_region_range CHECK (end_ms >= start_ms)
);

CREATE UNIQUE INDEX ux_speech_regions_seq ON speech_regions (vad_run_id, seq);
CREATE INDEX ix_speech_regions_start ON speech_regions (vad_run_id, start_ms);


-- ---------------------------------------------------------------------------
-- transcripts: one revision of one recording's transcript.
--
-- `recording_id` is what a transcript describes; `job_id` is which workflow run
-- produced it. Both are recorded because they answer different questions, and
-- `job_id` is nullable so a transcript produced by a CLI run outside a job is
-- still storable rather than needing a synthetic job.
-- ---------------------------------------------------------------------------
CREATE TABLE transcripts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id          INTEGER NOT NULL
                          REFERENCES recordings (id) ON DELETE CASCADE,
    working_copy_id       INTEGER NOT NULL
                          REFERENCES audio_working_copies (id) ON DELETE CASCADE,
    vad_run_id            INTEGER
                          REFERENCES vad_runs (id) ON DELETE SET NULL,
    job_id                INTEGER REFERENCES jobs (id) ON DELETE SET NULL,

    revision              INTEGER NOT NULL CHECK (revision >= 1),
    status                TEXT    NOT NULL DEFAULT 'BUILDING'
                          CHECK (status IN ('BUILDING', 'COMPLETE', 'FAILED',
                                            'CANCELLED')),
    is_active             INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),

    language              TEXT    NOT NULL DEFAULT 'id',
    language_probability  REAL    CHECK (language_probability IS NULL OR
                                         (language_probability >= 0.0
                                          AND language_probability <= 1.0)),

    -- Model provenance per pass. Recorded on the transcript rather than looked up
    -- later: the model store can be re-provisioned, and a transcript must still
    -- name the revision that produced it.
    pass1_model_name      TEXT,
    pass1_model_revision  TEXT,
    pass1_manifest_sha256 TEXT CHECK (pass1_manifest_sha256 IS NULL OR
                                      length(pass1_manifest_sha256) = 64),
    pass1_compute_type    TEXT,
    pass1_beam_size       INTEGER CHECK (pass1_beam_size IS NULL OR pass1_beam_size >= 1),
    pass1_cpu_threads     INTEGER CHECK (pass1_cpu_threads IS NULL OR pass1_cpu_threads >= 0),

    pass2_model_name      TEXT,
    pass2_model_revision  TEXT,
    pass2_manifest_sha256 TEXT CHECK (pass2_manifest_sha256 IS NULL OR
                                      length(pass2_manifest_sha256) = 64),
    pass2_compute_type    TEXT,
    pass2_beam_size       INTEGER CHECK (pass2_beam_size IS NULL OR pass2_beam_size >= 1),
    pass2_cpu_threads     INTEGER CHECK (pass2_cpu_threads IS NULL OR pass2_cpu_threads >= 0),

    -- Pass-2 accounting. `pass2_budget_ms` is what the policy allowed and
    -- `pass2_selected_ms` is what selection actually chose, so a budget that was
    -- exhausted is visible rather than inferred from a truncated region list.
    pass2_budget_ms       INTEGER CHECK (pass2_budget_ms IS NULL OR pass2_budget_ms >= 0),
    pass2_selected_ms     INTEGER NOT NULL DEFAULT 0 CHECK (pass2_selected_ms >= 0),
    pass2_region_count    INTEGER NOT NULL DEFAULT 0 CHECK (pass2_region_count >= 0),
    pass2_budget_exhausted INTEGER NOT NULL DEFAULT 0
                          CHECK (pass2_budget_exhausted IN (0, 1)),
    pass2_skipped_reason  TEXT,

    -- Terminology normalisation. The glossary version is recorded so a transcript
    -- normalised under an older glossary is identifiable.
    glossary_version      TEXT,
    glossary_sha256       TEXT CHECK (glossary_sha256 IS NULL OR
                                      length(glossary_sha256) = 64),
    glossary_replacements INTEGER NOT NULL DEFAULT 0
                          CHECK (glossary_replacements >= 0),

    -- Cost, for the operator and for the RTF gate.
    audio_ms              INTEGER NOT NULL DEFAULT 0 CHECK (audio_ms >= 0),
    speech_ms             INTEGER NOT NULL DEFAULT 0 CHECK (speech_ms >= 0),
    pass1_processing_ms   INTEGER NOT NULL DEFAULT 0 CHECK (pass1_processing_ms >= 0),
    pass2_processing_ms   INTEGER NOT NULL DEFAULT 0 CHECK (pass2_processing_ms >= 0),
    peak_rss_bytes        INTEGER NOT NULL DEFAULT 0 CHECK (peak_rss_bytes >= 0),

    segment_count         INTEGER NOT NULL DEFAULT 0 CHECK (segment_count >= 0),
    word_count            INTEGER NOT NULL DEFAULT 0 CHECK (word_count >= 0),

    last_error            TEXT,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE UNIQUE INDEX ux_transcripts_revision ON transcripts (recording_id, revision);
CREATE INDEX ix_transcripts_status ON transcripts (status);
CREATE INDEX ix_transcripts_job ON transcripts (job_id);

-- At most one active transcript per recording. Earlier revisions stay for the
-- diff Phase 7 needs.
CREATE UNIQUE INDEX ux_transcripts_one_active
    ON transcripts (recording_id)
    WHERE is_active = 1;


-- ---------------------------------------------------------------------------
-- transcript_segments: what the engine produced, plus why pass 2 was or was not
-- run over it.
--
-- `text` is what a reader sees and `text_raw` is what the model emitted. Both,
-- because terminology normalisation is a transformation of evidence and the
-- original has to remain checkable.
-- ---------------------------------------------------------------------------
CREATE TABLE transcript_segments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    transcript_id     INTEGER NOT NULL REFERENCES transcripts (id) ON DELETE CASCADE,
    seq               INTEGER NOT NULL CHECK (seq >= 0),

    -- Which VAD region produced it. NULL means a whole-file decode, which only
    -- the smoke test and the benchmark do.
    region_seq        INTEGER CHECK (region_seq IS NULL OR region_seq >= 0),
    asr_pass          INTEGER NOT NULL DEFAULT 1 CHECK (asr_pass IN (1, 2)),

    start_ms          INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms            INTEGER NOT NULL CHECK (end_ms >= 0),

    text              TEXT    NOT NULL,
    text_raw          TEXT    NOT NULL,

    -- Confidence signals, straight from the decoder. These are what pass-2
    -- selection reads, so they are stored rather than recomputed.
    avg_logprob       REAL,
    no_speech_prob    REAL CHECK (no_speech_prob IS NULL OR
                                  (no_speech_prob >= 0.0 AND no_speech_prob <= 1.0)),
    compression_ratio REAL,
    temperature       REAL,
    word_count        INTEGER NOT NULL DEFAULT 0 CHECK (word_count >= 0),
    min_word_probability REAL CHECK (min_word_probability IS NULL OR
                                     (min_word_probability >= 0.0
                                      AND min_word_probability <= 1.0)),

    -- Pass-2 selection. Reason codes are a JSON array of stable identifiers, so a
    -- reviewer sees *why* a region was re-run and a test can assert the rule that
    -- fired.
    selected_for_pass2 INTEGER NOT NULL DEFAULT 0
                       CHECK (selected_for_pass2 IN (0, 1)),
    pass2_reason_codes TEXT,
    pass2_rank         INTEGER CHECK (pass2_rank IS NULL OR pass2_rank >= 0),

    -- Supersession. A pass-1 segment replaced by a pass-2 result keeps its row and
    -- points at the replacement.
    is_active         INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    superseded_by_id  INTEGER REFERENCES transcript_segments (id) ON DELETE SET NULL,

    glossary_replacements INTEGER NOT NULL DEFAULT 0
                      CHECK (glossary_replacements >= 0),

    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT transcript_segment_range CHECK (end_ms >= start_ms),
    -- An inactive segment must say what replaced it, and an active one must not
    -- claim a replacement. Without this a merge that half-failed would leave a
    -- segment that is both current and superseded.
    CONSTRAINT transcript_segment_supersession
        CHECK ((is_active = 1 AND superseded_by_id IS NULL)
               OR (is_active = 0))
);

CREATE UNIQUE INDEX ux_transcript_segments_seq ON transcript_segments (transcript_id, seq);
CREATE INDEX ix_transcript_segments_active
    ON transcript_segments (transcript_id, is_active, start_ms);
CREATE INDEX ix_transcript_segments_pass ON transcript_segments (transcript_id, asr_pass);
CREATE INDEX ix_transcript_segments_selected
    ON transcript_segments (transcript_id, selected_for_pass2);


-- ---------------------------------------------------------------------------
-- transcript_words: word-level timings, which the review UI needs to play a word
-- and Phase 8 needs to prove a quotation.
--
-- Stored for every segment rather than only for reviewed ones: they cannot be
-- recovered without re-running the model, and re-running it is minutes of work
-- and a different result if the model changed.
-- ---------------------------------------------------------------------------
CREATE TABLE transcript_words (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id   INTEGER NOT NULL REFERENCES transcript_segments (id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL CHECK (seq >= 0),

    start_ms     INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms       INTEGER NOT NULL CHECK (end_ms >= 0),
    text         TEXT    NOT NULL,
    probability  REAL    CHECK (probability IS NULL OR
                                (probability >= 0.0 AND probability <= 1.0)),

    CONSTRAINT transcript_word_range CHECK (end_ms >= start_ms)
);

CREATE UNIQUE INDEX ux_transcript_words_seq ON transcript_words (segment_id, seq);
