-- ===========================================================================
-- MoM-IGD  migration 0007  --  the minute's reference number
--
-- WHAT THIS ADDS
--
-- Two columns on `minutes`: the human-readable reference an office files a
-- document under (`NOT/2026/08/001`) and the integer it was counted from.
--
-- WHY IT IS STORED AND NOT COMPUTED AT EXPORT TIME
--
-- Because it goes on a piece of paper that leaves the building. Derived at
-- render time, the same minute would renumber itself as soon as another meeting
-- was minuted before it -- so the copy in somebody's inbox and the copy exported
-- next week would carry different references while being the same document.
-- A reference number is only useful if it is immutable, so it is written once,
-- when the minute first becomes a draft, and never recalculated.
--
-- WHY BOTH A STRING AND AN INTEGER
--
-- The string is what the reader sees and its shape comes from configuration, so
-- it cannot be relied on to parse: an operator may change the format between one
-- month and the next. `document_seq` is what the next number is counted from,
-- and it stays correct across any format change.
--
-- WHY REVISIONS SHARE A NUMBER
--
-- Re-running the pipeline writes revision n+1 of the *same meeting's* minute. An
-- office expects one reference per meeting, with revisions under it -- not a new
-- filing number because the transcript was reprocessed. So revision 2 inherits
-- what revision 1 was given, and the revision number distinguishes them.
--
-- WHY THE UNIQUE INDEX IS PARTIAL
--
-- Revisions of one transcript deliberately share a number, so the column cannot
-- be globally unique. What must never happen is two *current* minutes sharing a
-- reference. `activate_minute` deactivates the previous revision before promoting
-- the new one, so the two never overlap and this index holds through a re-run.
--
-- NOTHING EXISTING IS MODIFIED
--
-- Two ADD COLUMNs and one index. No table rebuild, no change to any row: an
-- existing minute keeps a NULL reference, which renders as no reference at all
-- rather than as a wrong one.
-- ===========================================================================

ALTER TABLE minutes ADD COLUMN document_number TEXT;

ALTER TABLE minutes ADD COLUMN document_seq INTEGER;

-- At most one *current* minute may carry a given reference. NULLs are exempt in
-- SQLite, which is what lets minutes written before this migration stay valid.
CREATE UNIQUE INDEX ux_minutes_document_number
    ON minutes (document_number)
    WHERE is_active = 1 AND document_number IS NOT NULL;

-- The sequence is looked up by month, so it is worth an index of its own: the
-- lookup runs inside the transaction that promotes a minute, and that is not a
-- place to be scanning the table.
CREATE INDEX ix_minutes_document_seq ON minutes (document_seq);
