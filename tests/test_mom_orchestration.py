"""The pipeline and the service, end to end, with the worker faked at the process boundary.

This closes the gap the audit found: `MinutesPipeline.run()` and `MinutesService.generate()`
are the code that actually runs in production, and they had been exercised only by hand
against the real model. Coverage sat at 45 % and 74 %.

The seam is :func:`mom_igd.asr.worker.run_in_worker`. Patching it there rather than
patching a method on the pipeline means everything below it is real: the store writes, the
transactions, the audit events, the revision bookkeeping, the export files on disk, and the
failure paths. Only the 2.3 GB of weights are absent.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mom_igd.asr.worker import WorkerOutcome
from mom_igd.mom import store
from mom_igd.mom.pipeline import MinutesExportError, MinutesPipeline, MinutesPipelineError
from mom_igd.mom.service import MinutesBusyError, MinutesService

UUID = "11111111-1111-4111-8111-111111111111"

SEGMENTS = [
    (0, 0, 8000, "Selamat pagi, kita mulai rapat koordinasi hari ini."),
    (1, 8000, 16000, "Kita sepakat menunda go-live ke tanggal 5 September."),
    (2, 16000, 24000, "Bu Sinta tolong siapkan dokumen requirement sebelum hari Jumat."),
]

EXTRACTION = json.dumps(
    {
        "items": [
            {
                "kind": "DECISION",
                "text": "Go-live ditunda ke 5 September.",
                "quote": "Kita sepakat menunda go-live ke tanggal 5 September",
                "segments": [1],
                "owner": None,
                "due": None,
            },
            {
                "kind": "ACTION",
                "text": "Menyiapkan dokumen requirement.",
                "quote": "Bu Sinta tolong siapkan dokumen requirement sebelum hari Jumat",
                "segments": [2],
                "owner": "Bu Sinta",
                "due": "hari Jumat",
            },
            {
                "kind": "ISSUE",
                "text": "Anggaran belum disetujui direksi.",
                "quote": "anggaran tahunan itu belum disetujui direksi sama sekali",
                "segments": [1],
                "owner": "Pak Hendra",
                "due": None,
            },
        ]
    }
)

SUMMARY = json.dumps(
    {"title": "Rapat Koordinasi", "summary": ["Go-live ditunda ke 5 September."]}
)

MODEL = {
    "model_name": "qwen3-4b-instruct",
    "revision": "bc640142c66e1fdd",
    "manifest_sha256": "f" * 64,
    "quantisation": "Q4_K_M",
    "context_tokens": 8192,
    "threads": 12,
}


@pytest.fixture
def recording(conn: sqlite3.Connection, meeting_id: int) -> str:
    conn.execute(
        "UPDATE meetings SET title = 'Koordinasi SIMRS' WHERE id = ?", (meeting_id,)
    )
    conn.execute(
        "INSERT INTO recordings (id, meeting_id, recording_uuid, relative_dir, status, "
        "started_at, duration_ms) VALUES (1, ?, ?, 'rec/1', 'RECORDED', "
        "'2026-08-09T09:00:00Z', 24000)",
        (meeting_id, UUID),
    )
    conn.execute(
        "INSERT INTO audio_working_copies (id, recording_id, relative_path, sha256, "
        "size_bytes, frames, duration_ms) VALUES (1, 1, 'w.wav', ?, 10, 10, 24000)",
        ("a" * 64,),
    )
    conn.execute(
        "INSERT INTO transcripts (id, recording_id, working_copy_id, revision, status, "
        "is_active, language, segment_count, word_count) "
        "VALUES (1, 1, 1, 1, 'COMPLETE', 1, 'id', 3, 30)"
    )
    for seq, start, end, text in SEGMENTS:
        conn.execute(
            "INSERT INTO transcript_segments (transcript_id, seq, asr_pass, start_ms, "
            "end_ms, text, text_raw, is_active) VALUES (1, ?, 1, ?, ?, ?, ?, 1)",
            (seq, start, end, text, text),
        )
    conn.commit()
    return UUID


@pytest.fixture
def fake_worker(monkeypatch: pytest.MonkeyPatch):
    """Replace the spawned worker with a dictionary lookup. Everything else stays real."""
    calls: list[dict[str, Any]] = []

    def make(extraction: str = EXTRACTION, summary: str = SUMMARY, **outcome):
        def run_in_worker(task_name, payload, **kwargs):
            calls.append({"task": task_name, "payload": payload, "kwargs": kwargs})
            if outcome.get("fail"):
                return WorkerOutcome(
                    ok=False, payload=None, error=outcome["fail"], peak_rss_bytes=0
                )
            outputs = [
                {
                    "key": prompt["key"],
                    "text": summary if prompt["key"] == "summary" else extraction,
                    "prompt_tokens": 900,
                    "completion_tokens": 320,
                    "seconds": 42.0,
                }
                for prompt in payload["prompts"]
            ]
            return WorkerOutcome(
                ok=True,
                payload={
                    "model": MODEL,
                    "outputs": outputs,
                    "cancelled": bool(outcome.get("cancelled")),
                },
                peak_rss_bytes=outcome.get("peak", 5_351 * 1024 * 1024),
            )

        monkeypatch.setattr("mom_igd.asr.worker.run_in_worker", run_in_worker)
        return calls

    return make


def _pipeline(config, paths, db_path, **kwargs) -> MinutesPipeline:
    from mom_igd.db.connection import connect

    return MinutesPipeline(
        config=config, paths=paths, connect=lambda: connect(db_path), **kwargs
    )


# ===========================================================================
# The happy path, checked all the way to disk
# ===========================================================================


def test_a_run_writes_a_minute_its_items_and_a_document(
    config, paths, db_path, conn, recording, fake_worker
) -> None:
    fake_worker()
    result = _pipeline(config, paths, db_path).run(recording, export_formats=("docx",))

    assert result.status == "DRAFT"
    assert result.revision == 1
    assert result.item_count == 3
    assert result.verified_count == 2, "the third item quotes something nobody said"
    assert result.unverified_count == 1
    assert result.covered_ms == result.transcript_ms
    assert result.peak_rss_bytes > 0

    fresh = sqlite3.connect(db_path)
    fresh.row_factory = sqlite3.Row
    try:
        row = fresh.execute("SELECT * FROM minutes WHERE id = ?", (result.minute_id,)).fetchone()
        assert row["status"] == "DRAFT" and row["is_active"] == 1
        assert row["title"] == "Rapat Koordinasi"
        assert row["model_name"] == "qwen3-4b-instruct"
        assert row["quantisation"] == "Q4_K_M"
        assert row["item_count"] == 3 and row["verified_count"] == 2
        assert row["owners_dropped"] == 1, "Pak Hendra was never said out loud"

        items = fresh.execute(
            "SELECT * FROM minute_items WHERE minute_id = ? ORDER BY seq",
            (result.minute_id,),
        ).fetchall()
        # Stored in meeting order, not grouped by kind: a minute reads in the order the
        # meeting happened, and the renderer does the grouping.
        times = [row["start_ms"] for row in items if row["start_ms"] is not None]
        assert times == sorted(times)
        assert {row["kind"] for row in items} == {"DECISION", "ACTION", "ISSUE"}

        by_kind = {row["kind"]: row for row in items}
        assert by_kind["ACTION"]["owner"] == "Bu Sinta"
        assert by_kind["ACTION"]["due_text"] == "hari Jumat"
        assert by_kind["ISSUE"]["verification"] == "UNVERIFIED"
        assert by_kind["ISSUE"]["owner"] is None, "Pak Hendra was never said out loud"
        assert by_kind["DECISION"]["verification"] == "VERIFIED"

        exports = fresh.execute("SELECT * FROM minute_exports").fetchall()
        assert len(exports) == 1
        assert exports[0]["format"] == "docx"
        assert exports[0]["included_unverified"] == 1
    finally:
        fresh.close()

    [record] = result.exports
    written = Path(paths.exports_dir) / record["relative_path"]
    assert written.is_file() and written.stat().st_size == record["size_bytes"]
    assert record["relative_path"].endswith("-notulen-rev1.docx")


def test_the_document_is_named_by_uuid_and_leaves_no_partial_behind(
    config, paths, db_path, conn, recording, fake_worker
) -> None:
    fake_worker()
    _pipeline(config, paths, db_path).run(recording, export_formats=("markdown", "html"))
    written = sorted(path.name for path in Path(paths.exports_dir).iterdir())
    assert len(written) == 2
    assert not any(name.endswith(".partial") for name in written)
    assert all("Koordinasi" not in name for name in written), (
        "a display name is never a path component (ADR-0009)"
    )


def test_the_roster_canonicalises_a_spoken_name_through_the_real_pipeline(
    config, paths, db_path, conn, recording, meeting_id, fake_worker
) -> None:
    conn.execute(
        "INSERT INTO participants (id, uuid, display_name, is_active) "
        "VALUES (1, '44444444-4444-4444-8444-444444444444', 'Sinta Wijaya', 1)"
    )
    conn.execute(
        "INSERT INTO meeting_participants (meeting_id, participant_id, is_active) "
        "VALUES (?, 1, 1)",
        (meeting_id,),
    )
    conn.commit()
    fake_worker()
    _pipeline(config, paths, db_path).run(recording)
    fresh = sqlite3.connect(db_path)
    try:
        owners = [row[0] for row in fresh.execute("SELECT owner FROM minute_items")]
    finally:
        fresh.close()
    assert "Sinta Wijaya" in owners
    assert "Pak Hendra" not in owners, "a roster name that was never spoken stays out"


def test_the_worker_is_asked_for_extraction_then_the_summary(
    config, paths, db_path, conn, recording, fake_worker
) -> None:
    calls = fake_worker()
    _pipeline(config, paths, db_path).run(recording)
    assert [call["task"] for call in calls] == ["mom_generate", "mom_generate"]
    assert [prompt["key"] for prompt in calls[0]["payload"]["prompts"]] == ["chunk-0"]
    assert [prompt["key"] for prompt in calls[1]["payload"]["prompts"]] == ["summary"]
    assert calls[0]["payload"]["context_tokens"] == config.mom.context_tokens
    assert calls[0]["payload"]["batch_tokens"] == config.mom.batch_tokens
    assert "models_dir" in calls[0]["payload"]


def test_running_twice_supersedes_rather_than_overwrites(
    config, paths, db_path, conn, recording, fake_worker
) -> None:
    fake_worker()
    first = _pipeline(config, paths, db_path).run(recording)
    second = _pipeline(config, paths, db_path).run(recording)
    assert (first.revision, second.revision) == (1, 2)

    fresh = sqlite3.connect(db_path)
    fresh.row_factory = sqlite3.Row
    try:
        rows = {int(r["revision"]): dict(r) for r in fresh.execute("SELECT * FROM minutes")}
        assert rows[1]["is_active"] == 0 and rows[2]["is_active"] == 1
        assert rows[1]["item_count"] == 3, "the earlier revision keeps its items"
    finally:
        fresh.close()


# ===========================================================================
# Failure paths
# ===========================================================================


def test_a_recording_with_no_transcript_is_refused_by_reason_code(
    config, paths, db_path, conn, fake_worker
) -> None:
    fake_worker()
    with pytest.raises(MinutesPipelineError, match="NO_TRANSCRIPT"):
        _pipeline(config, paths, db_path).run("99999999-9999-4999-8999-999999999999")


def test_a_transcript_with_no_segments_is_refused(
    config, paths, db_path, conn, meeting_id, fake_worker
) -> None:
    fake_worker()
    conn.execute(
        "INSERT INTO recordings (id, meeting_id, recording_uuid, relative_dir, status) "
        "VALUES (2, ?, '55555555-5555-4555-8555-555555555555', 'rec/2', 'RECORDED')",
        (meeting_id,),
    )
    conn.execute(
        "INSERT INTO audio_working_copies (id, recording_id, relative_path, sha256, "
        "size_bytes, frames, duration_ms) VALUES (2, 2, 'w2.wav', ?, 10, 10, 1000)",
        ("b" * 64,),
    )
    conn.execute(
        "INSERT INTO transcripts (id, recording_id, working_copy_id, revision, status, "
        "is_active, language) VALUES (2, 2, 2, 1, 'COMPLETE', 1, 'id')"
    )
    conn.commit()
    with pytest.raises(MinutesPipelineError, match="NO_TRANSCRIPT"):
        _pipeline(config, paths, db_path).run("55555555-5555-4555-8555-555555555555")


def test_a_failed_worker_marks_the_revision_failed_and_never_active(
    config, paths, db_path, conn, recording, fake_worker
) -> None:
    """A crashed run must not be left behind as the meeting's minute."""
    fake_worker(fail="MODEL_UNAVAILABLE: no language model is provisioned")
    with pytest.raises(MinutesPipelineError, match="MODEL_UNAVAILABLE"):
        _pipeline(config, paths, db_path).run(recording)

    fresh = sqlite3.connect(db_path)
    fresh.row_factory = sqlite3.Row
    try:
        [row] = fresh.execute("SELECT * FROM minutes").fetchall()
        assert row["status"] == "FAILED"
        assert row["is_active"] == 0
        assert "MODEL_UNAVAILABLE" in row["last_error"]
        assert fresh.execute("SELECT COUNT(*) FROM minute_items").fetchone()[0] == 0
    finally:
        fresh.close()


def test_a_cancelled_run_is_recorded_as_cancelled_not_failed(
    config, paths, db_path, conn, recording, fake_worker
) -> None:
    fake_worker(cancelled=True)
    with pytest.raises(MinutesPipelineError, match="CANCELLED"):
        _pipeline(config, paths, db_path).run(recording)
    fresh = sqlite3.connect(db_path)
    fresh.row_factory = sqlite3.Row
    try:
        [row] = fresh.execute("SELECT * FROM minutes").fetchall()
        assert row["status"] == "CANCELLED" and row["is_active"] == 0
    finally:
        fresh.close()


def test_a_run_cancelled_before_it_starts_does_not_call_the_worker(
    config, paths, db_path, conn, recording, fake_worker
) -> None:
    calls = fake_worker()
    pipeline = _pipeline(config, paths, db_path, should_cancel=lambda: True)
    with pytest.raises(MinutesPipelineError, match="CANCELLED"):
        pipeline.run(recording)
    assert calls == []


def test_progress_is_reported_for_every_stage(
    config, paths, db_path, conn, recording, fake_worker
) -> None:
    fake_worker()
    messages: list[str] = []
    result = _pipeline(config, paths, db_path, progress=messages.append).run(
        recording, export_formats=("txt",)
    )
    names = [stage["name"] for stage in result.stages]
    assert names == ["transcript", "generate", "verify", "persist", "export:txt"]
    assert len(messages) >= len(names)


# ===========================================================================
# The service around it
# ===========================================================================


def _service(config, paths, db_path) -> MinutesService:
    from mom_igd.db.connection import connect

    return MinutesService(lambda: connect(db_path), config=config, paths=paths)


def test_the_service_runs_a_generation_and_releases_its_slot(
    config, paths, db_path, conn, recording, fake_worker
) -> None:
    fake_worker()
    service = _service(config, paths, db_path)
    assert service.running is False
    result = service.generate(recording, export_formats=())
    assert result.status == "DRAFT"
    assert service.running is False, "the slot must be released even on success"


def test_the_slot_is_released_after_a_failure_too(
    config, paths, db_path, conn, recording, fake_worker
) -> None:
    """Otherwise one crash makes the application refuse every later run until restart."""
    fake_worker(fail="something went wrong")
    service = _service(config, paths, db_path)
    with pytest.raises(MinutesPipelineError):
        service.generate(recording, export_formats=())
    assert service.running is False


def test_a_second_concurrent_run_is_refused_rather_than_queued(
    config, paths, db_path, conn, recording, fake_worker, monkeypatch
) -> None:
    fake_worker()
    service = _service(config, paths, db_path)
    seen: list[bool] = []

    original = MinutesPipeline.run

    def reentrant(self, *args, **kwargs):
        try:
            service.generate(recording, export_formats=())
        except MinutesBusyError:
            seen.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MinutesPipeline, "run", reentrant)
    service.generate(recording, export_formats=())
    assert seen == [True], "a run inside a run must hit the single-worker guard"


def test_the_service_reads_back_what_the_pipeline_wrote(
    config, paths, db_path, conn, recording, fake_worker
) -> None:
    fake_worker()
    service = _service(config, paths, db_path)
    service.generate(recording, export_formats=("markdown",))

    minute = service.get_minute(recording)
    assert minute["title"] == "Rapat Koordinasi"
    assert len(minute["items"]) == 3
    assert minute["summary"] == ["Go-live ditunda ke 5 September."]
    assert len(minute["exports"]) == 1

    [revision] = service.list_revisions(recording)
    assert revision["revision"] == 1 and revision["is_active"] == 1

    listed = service.list_minuteable()
    assert listed and listed[0]["minute_id"] == minute["id"]


def test_exporting_an_older_revision_reads_that_revision(
    config, paths, db_path, conn, recording, fake_worker
) -> None:
    fake_worker()
    service = _service(config, paths, db_path)
    service.generate(recording, export_formats=())
    service.generate(recording, export_formats=())
    record = service.export(recording, export_format="txt", revision=1)
    assert record["relative_path"].endswith("-notulen-rev1.txt")
    assert (Path(paths.exports_dir) / record["relative_path"]).is_file()


# ===========================================================================
# The invariant a broken item would trip at the database, loudly
# ===========================================================================


def test_no_stored_item_can_be_verified_without_a_citation(
    config, paths, db_path, conn, recording, fake_worker
) -> None:
    """Migration 0006 has a CHECK for this, so a violation aborts a whole run.

    Worth a live test rather than only a constraint test: the path from the verifier
    through the deduplicator to the store has three places that rebuild an item, and any
    of them dropping the citations would take the run down with an IntegrityError.
    """
    fake_worker()
    _pipeline(config, paths, db_path).run(recording)
    fresh = sqlite3.connect(db_path)
    fresh.row_factory = sqlite3.Row
    try:
        for row in fresh.execute("SELECT * FROM minute_items"):
            if row["verification"] != "UNVERIFIED":
                assert json.loads(row["segment_seqs"]), row["text"]
    finally:
        fresh.close()


# ===========================================================================
# A document that cannot be written must not lose the minute
# ===========================================================================


def test_a_locked_destination_fails_the_export_and_keeps_the_minute(
    config, paths, db_path, conn, recording, fake_worker
) -> None:
    """The likeliest failure in the field: last week's minute is still open in Word.

    Windows locks the destination, `replace` raises PermissionError, and before this the
    OSError escaped raw -- the CLI printed "Minutes FAILED", the API returned 500, and the
    operator would have re-run twenty minutes of work that had already succeeded.
    """
    fake_worker()
    real_replace = Path.replace

    def locked(self, target):
        if str(target).endswith('.docx'):
            raise PermissionError(13, 'The process cannot access the file')
        return real_replace(self, target)

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, 'replace', locked)
        result = _pipeline(config, paths, db_path).run(
            recording, export_formats=('docx', 'txt')
        )

    assert result.status == 'DRAFT', 'the minute itself succeeded'
    assert result.item_count == 3
    stages = {stage['name']: stage['ok'] for stage in result.stages}
    assert stages['persist'] is True
    assert stages['export:docx'] is False
    assert stages['export:txt'] is True, 'one bad format must not block the others'
    assert any('EXPORT_FAILED' in warning for warning in result.warnings)
    assert any('mom export' in warning for warning in result.warnings), (
        'the warning must name a remedy that works'
    )
    assert [record['format'] for record in result.exports] == ['txt']

    on_disk = sorted(path.name for path in Path(paths.exports_dir).iterdir())
    assert not any(name.endswith('.partial') for name in on_disk), (
        'a half-written document must not be left behind for somebody to forward'
    )


def test_an_export_error_is_a_precondition_not_a_crash() -> None:
    """So the API answers 409 and the CLI does not call a stored minute a failure."""
    assert issubclass(MinutesExportError, MinutesPipelineError)


def test_a_meeting_without_a_uuid_still_gets_a_unique_filename(
    config, paths, db_path, conn, recording, fake_worker
) -> None:
    """`meetings.uuid` is nullable: it arrived by ALTER in 0002, which cannot add NOT NULL.

    A NULL produced "None-notulen-rev1.docx", and two such meetings would collide --
    `record_export` deletes by path, so one meeting's row would come to describe another
    meeting's file. The conftest fixture creates exactly such a row, which is how this
    was found.
    """
    fake_worker()
    assert conn.execute('SELECT uuid FROM meetings').fetchone()[0] is None
    result = _pipeline(config, paths, db_path).run(recording, export_formats=('txt',))
    [record] = result.exports
    assert not record['relative_path'].startswith('None-')
    assert str(result.minute_id) in record['relative_path']
