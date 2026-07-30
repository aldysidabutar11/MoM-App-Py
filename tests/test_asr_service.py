"""The service layer: one heavy run at a time, and honest reads.

The single-run guard is the whole reason this layer exists. `resources.max_heavy_workers`
is 1 and configuration validation refuses anything else, and the measured working sets say
two concurrent runs would breach the memory budget -- so a second request is refused
visibly rather than queued invisibly.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from mom_igd.asr.service import AsrBusyError, AsrService, AsrServiceError

RECORDING_UUID = "88888888-8888-4888-8888-888888888888"


@pytest.fixture()
def service(config: Any, paths: Any, db_path: Path) -> AsrService:
    from mom_igd.db.connection import connect

    def _connect() -> sqlite3.Connection:
        return connect(db_path, busy_timeout_ms=config.database.busy_timeout_ms)

    return AsrService(_connect, config=config, paths=paths)


@pytest.fixture()
def stored(conn: sqlite3.Connection, meeting_id: int) -> int:
    with conn:
        cursor = conn.execute(
            "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir, status) "
            "VALUES (?, ?, 'm/r', 'RECORDED')",
            (meeting_id, RECORDING_UUID),
        )
        recording_id = int(cursor.lastrowid or 0)
        cursor = conn.execute(
            "INSERT INTO audio_working_copies (recording_id, relative_path, sha256, "
            "frames, duration_ms, status) VALUES (?, 'working/a.wav', ?, 16000, 1000, "
            "'READY')",
            (recording_id, "ab" * 32),
        )
        working_copy_id = int(cursor.lastrowid or 0)
        for revision, active in ((1, 0), (2, 1)):
            cursor = conn.execute(
                "INSERT INTO transcripts (recording_id, working_copy_id, revision, "
                "status, is_active, segment_count, word_count) "
                "VALUES (?, ?, ?, 'COMPLETE', ?, 1, 2)",
                (recording_id, working_copy_id, revision, active),
            )
            transcript_id = int(cursor.lastrowid or 0)
            cursor = conn.execute(
                "INSERT INTO transcript_segments (transcript_id, seq, region_seq, "
                "asr_pass, start_ms, end_ms, text, text_raw, word_count, "
                "selected_for_pass2, pass2_reason_codes) VALUES "
                "(?, 0, 0, 1, 0, 1000, ?, ?, 2, 1, '[\"LOW_AVG_LOGPROB\"]')",
                (transcript_id, f"revisi {revision}", f"revisi {revision}"),
            )
            segment_id = int(cursor.lastrowid or 0)
            conn.executemany(
                "INSERT INTO transcript_words (segment_id, seq, start_ms, end_ms, text, "
                "probability) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (segment_id, 0, 0, 400, "revisi", 0.9),
                    (segment_id, 1, 400, 900, str(revision), 0.8),
                ],
            )
    return recording_id


# ===========================================================================
# Construction
# ===========================================================================


def test_a_service_without_configuration_is_refused(paths: Any) -> None:
    """Phase 3 learned this: a silent fallback means two runtimes disagree."""
    with pytest.raises(AsrServiceError, match="requires config"):
        AsrService(lambda: None, config=None, paths=paths)  # type: ignore[arg-type]


def test_more_than_one_heavy_worker_is_refused(
    config: Any, paths: Any, db_path: Path
) -> None:
    from mom_igd.db.connection import connect

    loosened = config.model_copy(
        update={"resources": config.resources.model_copy(update={"max_heavy_workers": 2})}
    )
    service = AsrService(
        lambda: connect(db_path), config=loosened, paths=paths
    )
    with pytest.raises(AsrServiceError, match="max_heavy_workers must be 1"):
        service.transcribe(RECORDING_UUID)


# ===========================================================================
# Status
# ===========================================================================


def test_an_idle_service_reports_idle(service: AsrService) -> None:
    status = service.status()
    assert status["busy"] is False
    assert status["running_recording_uuid"] is None
    assert status["cancel_requested"] is False
    assert status["last_result"] is None
    assert status["pass2_enabled"] is True


def test_status_reports_model_readiness_from_the_registry(service: AsrService) -> None:
    """From the readiness index, not a directory scan: a probe-failed model is not ready."""
    models = service.status()["models"]
    assert models["readable_index"] is True
    assert models["pass1_ready"] is False
    assert models["pass2_ready"] is False
    assert models["pass1_model"] is None


def test_status_never_carries_a_path(service: AsrService, paths: Any) -> None:
    assert str(paths.root) not in repr(service.status())


def test_cancelling_when_idle_reports_that_nothing_ran(service: AsrService) -> None:
    assert service.request_cancel() is False


# ===========================================================================
# The single-run guard
# ===========================================================================


def test_a_second_concurrent_run_is_refused_not_queued(
    service: AsrService, monkeypatch: pytest.MonkeyPatch, stored: int
) -> None:
    """Refusing visibly beats queueing invisibly: the operator pressed a button."""
    entered = threading.Event()
    release = threading.Event()
    outcome: dict[str, Any] = {}

    class _SlowPipeline:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run(self, recording_uuid: str, *, job_id: int | None = None) -> Any:
            entered.set()
            release.wait(timeout=30)
            return _result(recording_uuid)

    import mom_igd.asr.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "TranscriptionPipeline", _SlowPipeline)

    def first() -> None:
        try:
            service.transcribe(RECORDING_UUID)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            outcome["first"] = exc

    worker = threading.Thread(target=first, daemon=True)
    worker.start()
    try:
        assert entered.wait(timeout=30), "the first run never started"
        assert service.busy is True
        with pytest.raises(AsrBusyError, match="already running"):
            service.transcribe(RECORDING_UUID)
    finally:
        release.set()
        worker.join(timeout=30)
    assert "first" not in outcome, outcome
    assert service.busy is False


def test_the_slot_is_released_even_when_a_run_raises(
    service: AsrService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leaked slot would make every later run report "already running" forever."""

    class _ExplodingPipeline:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run(self, recording_uuid: str, *, job_id: int | None = None) -> Any:
            raise RuntimeError("boom")

    import mom_igd.asr.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "TranscriptionPipeline", _ExplodingPipeline)
    with pytest.raises(RuntimeError, match="boom"):
        service.transcribe(RECORDING_UUID)
    assert service.busy is False
    assert service.request_cancel() is False


def test_a_cancel_request_reaches_the_pipeline(
    service: AsrService, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    class _CapturingPipeline:
        def __init__(self, **kwargs: Any) -> None:
            seen["should_cancel"] = kwargs["should_cancel"]

        def run(self, recording_uuid: str, *, job_id: int | None = None) -> Any:
            assert seen["should_cancel"]() is False
            service.request_cancel()
            assert seen["should_cancel"]() is True
            return _result(recording_uuid, cancelled=True)

    import mom_igd.asr.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "TranscriptionPipeline", _CapturingPipeline)
    result = service.transcribe(RECORDING_UUID)
    assert result.cancelled is True


def test_progress_messages_are_forwarded_and_bounded(
    service: AsrService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long run must not accumulate an unbounded message list in memory."""
    received: list[str] = []

    class _ChattyPipeline:
        def __init__(self, **kwargs: Any) -> None:
            self._say = kwargs["progress"]

        def run(self, recording_uuid: str, *, job_id: int | None = None) -> Any:
            for index in range(500):
                self._say(f"stage {index}")
            return _result(recording_uuid)

    import mom_igd.asr.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "TranscriptionPipeline", _ChattyPipeline)
    service.transcribe(RECORDING_UUID, progress=received.append)
    assert len(received) == 500, "the caller sees every message"
    status = service.status()
    assert status["busy"] is False
    assert status["last_result"] is not None


def test_the_last_result_is_kept_for_the_status_poll(
    service: AsrService, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _QuickPipeline:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run(self, recording_uuid: str, *, job_id: int | None = None) -> Any:
            return _result(recording_uuid)

    import mom_igd.asr.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "TranscriptionPipeline", _QuickPipeline)
    service.transcribe(RECORDING_UUID)
    status = service.status()
    assert status["last_result"]["recording_uuid"] == RECORDING_UUID
    assert status["last_error"] is None


def _result(recording_uuid: str, *, cancelled: bool = False) -> Any:
    from mom_igd.asr.pipeline import PipelineResult

    return PipelineResult(
        ok=not cancelled,
        recording_uuid=recording_uuid,
        revision=1,
        cancelled=cancelled,
        audio_ms=1000,
        segment_count=1,
        word_count=2,
    )


# ===========================================================================
# Reads
# ===========================================================================


def test_the_active_revision_is_returned_by_default(
    service: AsrService, stored: int
) -> None:
    payload = service.get_transcript(RECORDING_UUID)
    assert payload["transcript"]["revision"] == 2
    assert payload["transcript"]["is_active"] is True
    assert payload["segments"][0]["text"] == "revisi 2"


def test_an_earlier_revision_can_be_read(service: AsrService, stored: int) -> None:
    payload = service.get_transcript(RECORDING_UUID, revision=1)
    assert payload["transcript"]["revision"] == 1
    assert payload["transcript"]["is_active"] is False
    assert payload["segments"][0]["text"] == "revisi 1"


def test_words_come_back_with_their_segment(service: AsrService, stored: int) -> None:
    words = service.get_transcript(RECORDING_UUID)["segments"][0]["words"]
    assert [word["text"] for word in words] == ["revisi", "2"]
    assert words[0]["start_ms"] == 0


def test_a_segment_carries_no_speaker_and_says_so(
    service: AsrService, stored: int
) -> None:
    segment = service.get_transcript(RECORDING_UUID)["segments"][0]
    assert segment["speaker"] is None
    assert segment["speaker_status"] == "UNASSIGNED"


def test_the_working_copy_id_is_not_exposed(service: AsrService, stored: int) -> None:
    """An internal row id is not something a client has any use for."""
    transcript = service.get_transcript(RECORDING_UUID)["transcript"]
    assert "working_copy_id" not in transcript


def test_revisions_are_listed_newest_first(service: AsrService, stored: int) -> None:
    revisions = service.list_revisions(RECORDING_UUID)
    assert [row["revision"] for row in revisions] == [2, 1]
    assert [row["is_active"] for row in revisions] == [True, False]


def test_flagged_regions_carry_their_reasons(service: AsrService, stored: int) -> None:
    flagged = service.flagged_regions(RECORDING_UUID)
    assert len(flagged) == 1
    assert flagged[0]["reason_codes"] == ["LOW_AVG_LOGPROB"]
    assert flagged[0]["selected_for_pass2"] is True


def test_an_unknown_recording_is_reported_clearly(
    service: AsrService, conn: sqlite3.Connection
) -> None:
    for call in (
        lambda: service.get_transcript("99999999-9999-4999-8999-999999999999"),
        lambda: service.list_revisions("99999999-9999-4999-8999-999999999999"),
        lambda: service.flagged_regions("99999999-9999-4999-8999-999999999999"),
    ):
        with pytest.raises(AsrServiceError, match="no recording with uuid"):
            call()


def test_a_recording_without_a_transcript_names_the_next_step(
    service: AsrService, conn: sqlite3.Connection, meeting_id: int
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir, status) "
            "VALUES (?, 'aaaa1111-2222-4333-8444-555566667777', 'm/r9', 'RECORDED')",
            (meeting_id,),
        )
    with pytest.raises(AsrServiceError, match="asr transcribe"):
        service.get_transcript("aaaa1111-2222-4333-8444-555566667777")


def test_a_missing_revision_is_named_in_the_error(
    service: AsrService, stored: int
) -> None:
    with pytest.raises(AsrServiceError, match="revision 7"):
        service.get_transcript(RECORDING_UUID, revision=7)


def test_a_recording_with_no_revisions_lists_nothing_rather_than_raising(
    service: AsrService, conn: sqlite3.Connection, meeting_id: int
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir, status) "
            "VALUES (?, 'bbbb1111-2222-4333-8444-555566667777', 'm/r8', 'RECORDED')",
            (meeting_id,),
        )
    assert service.list_revisions("bbbb1111-2222-4333-8444-555566667777") == []


def test_the_service_does_not_import_the_participant_roster() -> None:
    """Roster size must never gate transcription, and it cannot if it is unreachable."""
    import ast

    source = (
        Path(__file__).resolve().parent.parent / "mom_igd" / "asr" / "service.py"
    ).read_text(encoding="utf-8")
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    assert not any("enrollment" in name or "participant" in name for name in modules), (
        sorted(modules)
    )
