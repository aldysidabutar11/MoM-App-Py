-- ===========================================================================
-- MoM-IGD  migration 0006  --  minutes of meeting
--
-- WHAT THIS ADDS
--
-- The last link of the evidence chain: from a transcript to the document a human
-- actually reads.
--
--   transcripts        (Phase 4, untouched)
--     -> minutes            one revision of a minute
--          -> minute_items  one decision, action, discussion point or issue
--          -> minute_exports one file written to disk from that minute
--
-- WHY AN ITEM STORES ITS EVIDENCE AND NOT JUST ITS TEXT
--
-- Because the text was written by a language model, and a language model's output
-- is a proposal. `quote` holds the verbatim span the model claimed to be quoting,
-- `segment_seqs` holds the transcript segments it was found in, and
-- `verification` records what a **non-model** check concluded about that pairing.
-- Together they let a reviewer jump from a line in the minute to the moment in the
-- recording where it was said. Store only the prose and that is gone for ever, and
-- the minute becomes a document nobody can check.
--
-- WHY `verification` IS A COLUMN AND NOT A FILTER
--
-- An item whose quote could not be located is **kept**, marked `UNVERIFIED`, and
-- shown. Deleting it would hide from the reviewer that the model produced it, and
-- keeping it unmarked would present a guess as a record. Neither is acceptable, so
-- the state is stored and every renderer is required to show it.
--
-- WHY `owner` IS NULLABLE AND HAS NO FOREIGN KEY
--
-- An owner is written **only** when the meeting said the name out loud, and it is
-- kept as the text that was said. Linking it to `participants` would invite the
-- system to resolve an ambiguous first name to whoever is on the roster, which is
-- a guess about who is responsible for what -- the most damaging thing a minute can
-- get wrong. `owner_participant_id` is deliberately absent, not merely unused.
--
-- WHY THERE IS NO SPEAKER COLUMN, STILL
--
-- Phase 4 assigns no speaker and this phase invents none. A PIC recorded here came
-- from words in the transcript, never from who was talking. Diarization is Phase 5
-- and voice identification is Phase 6; when they land, an item will be able to gain
-- a *separately sourced* attribution, and it must not be able to overwrite this one
-- silently.
--
-- WHY REVISIONS, AGAIN
--
-- Same reasoning as transcripts. Re-running writes revision n+1 and deactivates the
-- previous one; nothing is edited in place. A reviewer who has read a minute must be
-- able to see what changed underneath them.
--
-- WHY `status` HAS NO `APPROVED`
--
-- Because approval is a human act with its own audit requirements, and this phase
-- does not implement it. Leaving the value available would let something write it.
-- A minute here is a DRAFT; the workflow that makes one final is Phase 7's.
--
-- NOTHING IN PHASES 1-4 IS MODIFIED
--
-- No ALTER, no table rebuild, no change to any existing index.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- minutes: one generated revision of a meeting's minute.
--
-- The model provenance columns are not decoration. A minute produced by a
-- different quantisation of a different model revision is a different artefact,
-- and six months later the only way to know which one a document came from is to
-- have written it down at the time.
-- ---------------------------------------------------------------------------
CREATE TABLE minutes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transcript_id   INTEGER NOT NULL REFERENCES transcripts (id) ON DELETE CASCADE,
    meeting_id      INTEGER NOT NULL REFERENCES meetings (id) ON DELETE CASCADE,
    job_id          INTEGER REFERENCES jobs (id) ON DELETE SET NULL,

    revision        INTEGER NOT NULL CHECK (revision >= 1),
    status          TEXT    NOT NULL DEFAULT 'BUILDING'
                    CHECK (status IN ('BUILDING', 'DRAFT', 'FAILED', 'CANCELLED')),
    is_active       INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),

    title           TEXT    NOT NULL DEFAULT '',
    -- Summary sentences as a JSON array. A minute's summary is an ordered list of
    -- short statements, not a paragraph, and storing it as a list keeps a renderer
    -- from having to re-split prose it did not write.
    summary_json    TEXT    NOT NULL DEFAULT '[]',
    -- Digit strings that appear in the summary and in no item it was written from.
    -- Empty is the expected state. Anything here is a fabricated figure, recorded
    -- so it stays visible after the run that found it has finished.
    summary_unsupported_numbers TEXT NOT NULL DEFAULT '[]',
    -- Machine-readable reasons the minute is incomplete: a window that failed to
    -- parse, one that hit the item ceiling, a summary that could not be built.
    warnings_json   TEXT    NOT NULL DEFAULT '[]',

    language        TEXT    NOT NULL DEFAULT 'id',

    model_name      TEXT,
    model_revision  TEXT,
    manifest_sha256 TEXT,
    quantisation    TEXT,
    context_tokens  INTEGER CHECK (context_tokens IS NULL OR context_tokens > 0),
    threads         INTEGER CHECK (threads IS NULL OR threads > 0),

    -- Coverage and cost. `covered_ms` against `transcript_ms` is how an operator
    -- sees that a window was lost: a minute covering 70 of 90 minutes is not a
    -- minute of that meeting, and the ratio says so without reading the warnings.
    chunk_count     INTEGER NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    chunks_failed   INTEGER NOT NULL DEFAULT 0 CHECK (chunks_failed >= 0),
    covered_ms      INTEGER NOT NULL DEFAULT 0 CHECK (covered_ms >= 0),
    transcript_ms   INTEGER NOT NULL DEFAULT 0 CHECK (transcript_ms >= 0),

    item_count      INTEGER NOT NULL DEFAULT 0 CHECK (item_count >= 0),
    verified_count  INTEGER NOT NULL DEFAULT 0 CHECK (verified_count >= 0),
    unverified_count INTEGER NOT NULL DEFAULT 0 CHECK (unverified_count >= 0),
    owners_dropped  INTEGER NOT NULL DEFAULT 0 CHECK (owners_dropped >= 0),

    prompt_tokens     INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    model_ms          INTEGER NOT NULL DEFAULT 0 CHECK (model_ms >= 0),
    total_ms          INTEGER NOT NULL DEFAULT 0 CHECK (total_ms >= 0),
    peak_rss_bytes    INTEGER CHECK (peak_rss_bytes IS NULL OR peak_rss_bytes >= 0),

    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT minute_coverage CHECK (covered_ms <= transcript_ms OR transcript_ms = 0),
    -- Only a finished minute may be current. A BUILDING row that crashed cannot be
    -- left behind as the meeting's minute.
    CONSTRAINT minute_active_is_draft
        CHECK (is_active = 0 OR status = 'DRAFT')
);

CREATE UNIQUE INDEX ux_minutes_revision ON minutes (transcript_id, revision);
-- At most one current minute per transcript, as a database guarantee rather than a
-- convention: two concurrent runs must not both end up current.
CREATE UNIQUE INDEX ux_minutes_active ON minutes (transcript_id) WHERE is_active = 1;
CREATE INDEX ix_minutes_meeting ON minutes (meeting_id, created_at);
CREATE INDEX ix_minutes_status ON minutes (status);


-- ---------------------------------------------------------------------------
-- minute_items: one line of the minute, with the evidence for it.
-- ---------------------------------------------------------------------------
CREATE TABLE minute_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    minute_id     INTEGER NOT NULL REFERENCES minutes (id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL CHECK (seq >= 0),

    kind          TEXT    NOT NULL
                  CHECK (kind IN ('DECISION', 'ACTION', 'DISCUSSION', 'ISSUE')),

    -- What the reader reads, and what the model claims was said. Separate on
    -- purpose: the model may tidy a spoken sentence into a written one, and only a
    -- verbatim span can be checked against a transcript.
    text          TEXT    NOT NULL,
    quote         TEXT    NOT NULL,

    -- The transcript segments the quote was actually located in, as a JSON array of
    -- `transcript_segments.seq`. Seq rather than id, so the reference survives a
    -- re-import and reads the same as the citation the model emitted.
    segment_seqs  TEXT    NOT NULL DEFAULT '[]',
    start_ms      INTEGER CHECK (start_ms IS NULL OR start_ms >= 0),
    end_ms        INTEGER CHECK (end_ms IS NULL OR end_ms >= 0),

    -- Written only when the transcript said the name. No foreign key, deliberately.
    owner         TEXT,
    due_text      TEXT,

    verification  TEXT    NOT NULL DEFAULT 'UNVERIFIED'
                  CHECK (verification IN ('VERIFIED', 'REBOUND', 'UNVERIFIED')),
    -- Named reasons: OWNER_NOT_IN_TRANSCRIPT, QUOTE_NEAR_MATCH, DUE_CONFLICT, ...
    -- A JSON array of stable identifiers, so a reviewer sees *why* and a test can
    -- assert which rule fired.
    verification_notes TEXT NOT NULL DEFAULT '[]',

    -- How many extracted copies were folded into this one. Windows overlap, so 2 is
    -- ordinary; a high count means several parts of the meeting said the same thing.
    merged_count  INTEGER NOT NULL DEFAULT 1 CHECK (merged_count >= 1),
    chunk_index   INTEGER NOT NULL DEFAULT 0 CHECK (chunk_index >= 0),

    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT minute_item_range
        CHECK (start_ms IS NULL OR end_ms IS NULL OR end_ms >= start_ms),
    -- A verified item must say where it was verified. Without this, a bug that lost
    -- the citations would leave rows that look checked and cannot be checked.
    CONSTRAINT minute_item_verified_has_evidence
        CHECK (verification = 'UNVERIFIED' OR segment_seqs <> '[]')
);

CREATE UNIQUE INDEX ux_minute_items_seq ON minute_items (minute_id, seq);
CREATE INDEX ix_minute_items_kind ON minute_items (minute_id, kind);
CREATE INDEX ix_minute_items_verification ON minute_items (minute_id, verification);


-- ---------------------------------------------------------------------------
-- minute_exports: a file written from a minute.
--
-- Recorded because an export leaves the application. Once a .docx is on a shared
-- drive it can be edited, forwarded and quoted, and the question "which revision
-- of the minute is this, and did it contain unverified items?" has to be
-- answerable afterwards. The SHA-256 makes the file on disk identifiable as the
-- one this row describes.
--
-- The path is stored **relative to the exports directory**, never absolute:
-- `mom_igd/paths.py` owns runtime paths, and an absolute path in a row would
-- survive a data-root move and point at nothing.
-- ---------------------------------------------------------------------------
CREATE TABLE minute_exports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    minute_id     INTEGER NOT NULL REFERENCES minutes (id) ON DELETE CASCADE,

    format        TEXT    NOT NULL CHECK (format IN ('markdown', 'html', 'docx', 'txt')),
    relative_path TEXT    NOT NULL,
    sha256        TEXT    NOT NULL CHECK (length(sha256) = 64),
    size_bytes    INTEGER NOT NULL CHECK (size_bytes >= 0),

    -- What the file contained, as a fact about the exported document rather than
    -- about the minute, which may have gained a revision since.
    included_unverified INTEGER NOT NULL DEFAULT 0
                        CHECK (included_unverified IN (0, 1)),
    include_evidence    INTEGER NOT NULL DEFAULT 1
                        CHECK (include_evidence IN (0, 1)),

    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX ix_minute_exports_minute ON minute_exports (minute_id, created_at);
CREATE UNIQUE INDEX ux_minute_exports_path ON minute_exports (relative_path);
