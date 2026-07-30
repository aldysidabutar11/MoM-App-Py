-- ===========================================================================
-- MoM-IGD  migration 0004  --  per-meeting roster capacity (Phase 3 corrective)
--
-- WHY THIS EXISTS
--
-- Phase 3 shipped a single module constant,
-- `MAX_ACTIVE_PARTICIPANTS_PER_MEETING = 9`, and enforced it for every meeting.
-- Nine came from the original diarization sizing, but it was written into the
-- code as though it were a law of the product. It is not: it is the default the
-- first customer needs, and a different room legitimately seats a different
-- number of people.
--
-- Capacity is therefore per meeting, and it is stored -- not derived, not held in
-- configuration, not recomputed from the roster. An operator who sets a room to
-- 20 must still find 20 after restarting the application, and a later change to
-- the configured default must not silently retune every meeting recorded before
-- it.
--
-- WHY `meetings` AND NOT A NEW TABLE
--
-- This is one scalar fact about one meeting, with a 1:1 lifetime. A side table
-- would need its own insert on every meeting creation, its own left join on
-- every read, and would allow the two to disagree. `ALTER TABLE ADD COLUMN` with
-- a DEFAULT is also the cheapest correct upgrade SQLite offers: it rewrites no
-- rows and moves no data.
--
-- This does NOT reopen the rule that `meetings` has no state column (see 0001).
-- Capacity is configuration of a meeting, not a position in a workflow. Workflow
-- state stays in `jobs`, which remains its single owner.
--
-- WHY THE CHECK IS ONLY `>= 1`
--
-- The 50-participant safety ceiling is a *business* limit and it lives in
-- configuration (`[participants].maximum_meeting_participant_capacity`), where
-- raising it is an edit to one TOML value. Encoding 50 in a CHECK constraint
-- would mean SQLite has to rebuild this table -- with its foreign keys, indexes
-- and cascades -- the first time somebody legitimately needs 60. What belongs in
-- the database is the invariant that can never become false: a roster cannot
-- hold a negative or zero number of people.
--
-- The ceiling is a guard rail, not a validated capability. Nothing here claims
-- that 50 speakers can be told apart: that needs a real embedding model, a USB
-- conference microphone and acceptance in the actual room, none of which exist
-- yet. See docs/phase-3-participants-enrollment.md and ADR-0013.
--
-- UPGRADE BEHAVIOUR
--
-- `DEFAULT 9` backfills every meeting that already exists, so a database
-- recorded under the old fixed cap keeps exactly the behaviour it had. No
-- recording, chunk, job, consent event or voiceprint row is touched.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- meetings: how many people this meeting's roster may hold.
-- ---------------------------------------------------------------------------
ALTER TABLE meetings
    ADD COLUMN participant_capacity INTEGER NOT NULL DEFAULT 9
        CHECK (participant_capacity >= 1);

-- Existing rows are backfilled by the DEFAULT above. This statement is a
-- belt-and-braces guard for any row that somehow carries NULL (a database
-- restored from an older tool, for example): the column is NOT NULL, so a NULL
-- here would be a corruption we would rather repair than propagate.
UPDATE meetings SET participant_capacity = 9 WHERE participant_capacity IS NULL;

-- Reading a roster always reads its meeting's capacity alongside the membership
-- count, so keep the lookup covered.
CREATE INDEX ix_meetings_capacity ON meetings (id, participant_capacity);
