"""The pipeline end to end: real normalisation, real VAD, real database, stub engine.

Only the ASR engine is substituted. Everything else here is the production code path: the
master audio is built from real chunk files with a real Phase 2 manifest, the working copy
is derived by the real normaliser, speech regions come from the real bundled Silero model,
and every row is written by the real store into a real migrated SQLite database.

The engine is stubbed because a 464 MiB model load per test would make the suite unusable,
and because CLAUDE.md forbids the suite depending on a provisioned model. What the stub
cannot cover -- that a real model loads, decodes and releases -- is covered by
``asr smoke`` and by the recorded end-to-end run in ``docs/phase-4-progress.md``.

Audio is synthesised arithmetically. No fixture contains a human voice.
"""

from __future__ import annotations

import array
import json
import math
import sqlite3
import wave
from pathlib import Path
from typing import Any, Callable

import pytest

from mom_igd.asr.pipeline import (
    REASON_CANCELLED,
    REASON_MODEL_UNAVAILABLE,
    REASON_NO_SPEECH,
    REASON_PASS2_BUDGET_TOO_SMALL,
    REASON_PASS2_DISABLED,
    REASON_PASS2_MODEL_UNAVAILABLE,
    REASON_PASS2_NOTHING_FLAGGED,
    PipelineError,
    TranscriptionPipeline,
)


# ===========================================================================
# A recording, built the way Phase 2 builds one
# ===========================================================================

_VOWELS = (
    (730.0, 1090.0, 2440.0),
    (270.0, 2290.0, 3010.0),
    (300.0, 870.0, 2240.0),
)


def _formant_frames(count: int, *, rate: int, channels: int, offset: int = 0) -> array.array:
    """Deterministic formant synthesis: VAD accepts it, and it is not a voice."""
    samples = array.array("h")
    states = [[0.0, 0.0] for _ in range(3)]
    for index in range(count):
        n = offset + index
        t = n / rate
        cycle = t % 2.1
        if cycle > 1.5:
            for _ in range(channels):
                samples.append(0)
            continue
        formants = _VOWELS[int(cycle / 0.2) % len(_VOWELS)]
        pitch = 120.0 * (1.0 + 0.1 * math.sin(2 * math.pi * 0.9 * t))
        phase = (n * pitch / rate) % 1.0
        excitation = 1.0 if phase < pitch / rate else -0.06 * phase
        total = 0.0
        for slot, frequency in enumerate(formants):
            decay = math.exp(-math.pi * (70.0 + 30.0 * slot) / rate)
            theta = 2 * math.pi * frequency / rate
            state = states[slot]
            value = (
                excitation
                + 2 * decay * math.cos(theta) * state[0]
                - decay * decay * state[1]
            )
            state[1], state[0] = state[0], value
            total += value / (slot + 1.5)
        envelope = 0.5 + 0.5 * math.sin(2 * math.pi * 4.0 * t - math.pi / 2)
        value = int(max(-32768, min(32767, total * envelope * 0.28 * 9000)))
        for _ in range(channels):
            samples.append(value)
    return samples


@pytest.fixture()
def recording(
    conn: sqlite3.Connection, paths: Any, tmp_path: Path
) -> dict[str, Any]:
    """A RECORDED 48 kHz stereo recording with two chunks and a real manifest."""
    from mom_igd.audio.backend import CaptureProfile, SampleFormat
    from mom_igd.audio.manifest import (
        ChunkRecord,
        ManifestWriter,
        read_manifest,
        sha256_file,
        utc_now_iso,
        write_manifest_summary,
    )

    meeting_uuid = "22222222-2222-4222-8222-222222222222"
    recording_uuid = "33333333-3333-4333-8333-333333333333"
    relative_dir = f"{meeting_uuid}/{recording_uuid}"
    directory = paths.recordings_dir / meeting_uuid / recording_uuid
    directory.mkdir(parents=True, exist_ok=True)

    rate, channels, per_chunk = 48_000, 2, 48_000 * 4
    profile = CaptureProfile(
        sample_rate=rate, channels=channels, sample_format=SampleFormat.INT16, chunk_seconds=10
    )
    writer = ManifestWriter(directory)
    for seq in range(2):
        name = f"chunk-{seq:06d}.wav"
        frames = _formant_frames(
            per_chunk, rate=rate, channels=channels, offset=seq * per_chunk
        )
        path = directory / name
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(channels)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(frames.tobytes())
        writer.append_chunk(
            ChunkRecord(
                seq=seq,
                filename=name,
                start_frame=seq * per_chunk,
                end_frame=(seq + 1) * per_chunk,
                frame_count=per_chunk,
                duration_ms=per_chunk * 1000.0 / rate,
                utc_start=utc_now_iso(),
                utc_end=utc_now_iso(),
                monotonic_start_ns=seq * per_chunk,
                monotonic_end_ns=(seq + 1) * per_chunk,
                sample_rate=rate,
                channels=channels,
                sample_format="int16",
                byte_count=path.stat().st_size,
                sha256=sha256_file(path),
            )
        )
    records, _events, _errors = read_manifest(directory)
    summary = write_manifest_summary(
        directory,
        recording_uuid=recording_uuid,
        meeting_uuid=meeting_uuid,
        profile=profile,
        records=records,
    )

    with conn:
        cursor = conn.execute(
            "INSERT INTO meetings (title, uuid, participant_capacity) VALUES (?, ?, 9)",
            ("Rapat uji pipeline", meeting_uuid),
        )
        meeting_id = int(cursor.lastrowid or 0)
        cursor = conn.execute(
            "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir, status,"
            " sample_rate_hz, channels, chunk_count, written_frames, duration_ms,"
            " manifest_sha256, manifest_status) VALUES (?, ?, ?, 'RECORDED', ?, ?, ?, ?,"
            " ?, ?, 'VERIFIED')",
            (
                meeting_id,
                recording_uuid,
                relative_dir,
                rate,
                channels,
                len(records),
                per_chunk * 2,
                int(per_chunk * 2 * 1000 / rate),
                summary.get("manifest_sha256"),
            ),
        )
        recording_id = int(cursor.lastrowid or 0)
        for record in records:
            conn.execute(
                "INSERT INTO recording_chunks (recording_id, seq, filename, start_frame,"
                " end_frame, frames, sample_rate_hz, channels, size_bytes, sha256, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'WRITTEN')",
                (
                    recording_id,
                    record.seq,
                    record.filename,
                    record.start_frame,
                    record.end_frame,
                    record.frame_count,
                    record.sample_rate,
                    record.channels,
                    record.byte_count,
                    record.sha256,
                ),
            )
    return {
        "recording_uuid": recording_uuid,
        "recording_id": recording_id,
        "meeting_uuid": meeting_uuid,
        "directory": directory,
        "duration_ms": int(per_chunk * 2 * 1000 / rate),
    }


# ===========================================================================
# A stub engine, standing in only for the model
# ===========================================================================


class _StubWorker:
    """Answers the pipeline's worker calls without loading a model.

    Runs the *real* VAD task, because Silero ships in the wheel and is therefore always
    available -- there is no reason to fake the stage that can be exercised honestly.
    """

    def __init__(
        self,
        *,
        pass2_available: bool = True,
        text: Callable[[int, int], str] | None = None,
        avg_logprob: float = -0.2,
        pass2_avg_logprob: float = -0.1,
        cancel_pass: int | None = None,
        pass2_returns_nothing: bool = False,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.pass2_available = pass2_available
        self.text = text or (lambda pass_number, region: f"kata pass{pass_number} r{region}")
        self.avg_logprob = avg_logprob
        self.pass2_avg_logprob = pass2_avg_logprob
        self.cancel_pass = cancel_pass
        self.pass2_returns_nothing = pass2_returns_nothing

    def __call__(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((task, dict(payload)))
        if task == "vad":
            from mom_igd.asr.tasks import TASK_REGISTRY

            return TASK_REGISTRY["vad"](payload, lambda: False)
        if task != "transcribe":
            raise AssertionError(f"unexpected worker task {task!r}")

        asr_pass = int(payload.get("asr_pass", 1))
        if asr_pass == 2 and not self.pass2_available:
            raise PipelineError(f"{REASON_MODEL_UNAVAILABLE}: pass-2 model is absent")
        if asr_pass == self.cancel_pass:
            return {"segments": [], "cancelled": True, "regions_completed": 0}
        if asr_pass == 2 and self.pass2_returns_nothing:
            return _worker_payload([], asr_pass, self)

        segments = []
        for region in payload.get("regions") or []:
            segments.append(
                {
                    "index": len(segments),
                    "region_index": int(region["index"]),
                    "start": float(region["start"]),
                    "end": float(region["end"]),
                    "text": self.text(asr_pass, int(region["index"])),
                    "avg_logprob": (
                        self.pass2_avg_logprob if asr_pass == 2 else self.avg_logprob
                    ),
                    "no_speech_prob": 0.05,
                    "compression_ratio": 1.5,
                    "temperature": 0.0,
                    "words": [
                        {
                            "text": "kata",
                            "start": float(region["start"]),
                            "end": float(region["start"]) + 0.2,
                            "probability": 0.9,
                        }
                    ],
                }
            )
        return _worker_payload(segments, asr_pass, self)


def _worker_payload(segments: list[dict[str, Any]], asr_pass: int, stub: Any) -> dict[str, Any]:
    return {
        "segments": segments,
        "model": {
            "model_name": f"stub-pass{asr_pass}",
            "revision": f"{asr_pass}" * 12,
            "manifest_sha256": "cd" * 32,
            "compute_type": "int8",
            "is_test_double": True,
        },
        "language": "id",
        "language_probability": 0.98,
        "audio_seconds": 1.0,
        "processing_seconds": 0.1,
        "load_seconds": 0.1,
        "regions_requested": 1,
        "regions_completed": 1,
        "cancelled": False,
        "network_attempts": [],
    }


def _pipeline(
    config: Any,
    paths: Any,
    db_path: Path,
    worker: Any,
    *,
    should_cancel: Callable[[], bool] | None = None,
) -> TranscriptionPipeline:
    from mom_igd.db.connection import connect

    def _connect() -> sqlite3.Connection:
        return connect(db_path, busy_timeout_ms=config.database.busy_timeout_ms)

    pipeline = TranscriptionPipeline(
        config=config, paths=paths, connect=_connect, should_cancel=should_cancel
    )
    pipeline._run_worker = worker  # type: ignore[method-assign]
    return pipeline


# ===========================================================================
# The happy path
# ===========================================================================


def test_a_full_run_produces_an_active_transcript(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    worker = _StubWorker()
    result = _pipeline(config, paths, db_path, worker).run(recording["recording_uuid"])
    assert result.ok is True, result.error
    assert result.revision == 1
    assert result.region_count > 0
    assert result.segment_count > 0
    assert result.word_count > 0
    assert result.audio_ms == pytest.approx(recording["duration_ms"], abs=20)

    row = conn.execute(
        "SELECT * FROM transcripts WHERE recording_id = ?", (recording["recording_id"],)
    ).fetchone()
    assert row["status"] == "COMPLETE"
    assert row["is_active"] == 1
    assert row["pass1_model_name"] == "stub-pass1"


def test_every_stage_runs_in_order(
    config: Any, paths: Any, db_path: Path, recording: dict[str, Any]
) -> None:
    result = _pipeline(config, paths, db_path, _StubWorker()).run(
        recording["recording_uuid"]
    )
    names = [stage["name"] for stage in result.stages]
    assert names[:4] == ["validate_audio", "normalize_audio", "vad", "asr_pass1"]
    assert "normalize_terminology" in names
    assert all(stage["ok"] for stage in result.stages), result.stages


def test_the_working_copy_is_16k_mono_and_recorded(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    _pipeline(config, paths, db_path, _StubWorker()).run(recording["recording_uuid"])
    row = conn.execute(
        "SELECT * FROM audio_working_copies WHERE recording_id = ?",
        (recording["recording_id"],),
    ).fetchone()
    assert row["sample_rate_hz"] == 16_000
    assert row["channels"] == 1
    assert row["status"] == "READY"
    assert row["source_sample_rate_hz"] == 48_000
    assert row["source_channels"] == 2
    assert len(row["sha256"]) == 64
    assert ":" not in row["relative_path"]

    on_disk = paths.root / row["relative_path"]
    assert on_disk.is_file()
    with wave.open(str(on_disk), "rb") as handle:
        assert handle.getframerate() == 16_000
        assert handle.getnchannels() == 1


def test_the_master_audio_is_untouched(
    config: Any, paths: Any, db_path: Path, recording: dict[str, Any]
) -> None:
    import hashlib

    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(recording["directory"].glob("*.wav"))
    }
    _pipeline(config, paths, db_path, _StubWorker()).run(recording["recording_uuid"])
    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(recording["directory"].glob("*.wav"))
    }
    assert before == after and before


def test_vad_regions_are_persisted_with_provenance(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    _pipeline(config, paths, db_path, _StubWorker()).run(recording["recording_uuid"])
    run = conn.execute("SELECT * FROM vad_runs WHERE is_active = 1").fetchone()
    assert run["model_name"] == "silero-vad-v6-bundled"
    assert len(run["model_sha256"]) == 64
    assert len(run["config_hash"]) == 64
    assert json.loads(run["config_json"])["threshold"] == config.asr.vad_threshold
    regions = conn.execute(
        "SELECT * FROM speech_regions WHERE vad_run_id = ? ORDER BY seq", (run["id"],)
    ).fetchall()
    assert len(regions) == run["region_count"] > 0
    for region in regions:
        assert region["end_ms"] > region["start_ms"]


def test_words_are_stored_with_their_segment(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    _pipeline(config, paths, db_path, _StubWorker()).run(recording["recording_uuid"])
    rows = conn.execute(
        "SELECT w.start_ms, w.end_ms, s.start_ms AS seg_start, s.end_ms AS seg_end "
        "FROM transcript_words w JOIN transcript_segments s ON s.id = w.segment_id"
    ).fetchall()
    assert rows
    for row in rows:
        assert row["seg_start"] <= row["start_ms"] <= row["seg_end"]


def test_no_segment_carries_a_speaker(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    """Phase 4 assigns none, and there is deliberately no column to put one in."""
    _pipeline(config, paths, db_path, _StubWorker()).run(recording["recording_uuid"])
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(transcript_segments)").fetchall()
    }
    assert not any("speaker" in name for name in columns), sorted(columns)


def test_terminology_is_normalised_and_the_original_kept(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    worker = _StubWorker(text=lambda pass_number, region: "kita deploi ke serper")
    result = _pipeline(config, paths, db_path, worker).run(recording["recording_uuid"])
    assert result.glossary_replacements > 0
    row = conn.execute(
        "SELECT text, text_raw, glossary_replacements FROM transcript_segments LIMIT 1"
    ).fetchone()
    assert row["text"] == "kita deploy ke server"
    assert row["text_raw"] == "kita deploi ke serper"
    assert row["glossary_replacements"] == 2
    transcript = conn.execute("SELECT * FROM transcripts").fetchone()
    assert transcript["glossary_version"]
    assert len(transcript["glossary_sha256"]) == 64


def test_the_initial_prompt_is_bounded_and_carries_terms(
    config: Any, paths: Any, db_path: Path, recording: dict[str, Any]
) -> None:
    worker = _StubWorker()
    _pipeline(config, paths, db_path, worker).run(recording["recording_uuid"])
    transcribe_calls = [payload for task, payload in worker.calls if task == "transcribe"]
    assert transcribe_calls
    prompt = transcribe_calls[0]["initial_prompt"]
    assert prompt and len(prompt) <= config.asr.initial_prompt_max_chars
    assert "deploy" in prompt


def test_the_measured_thread_counts_and_beams_reach_the_worker(
    config: Any, paths: Any, db_path: Path, recording: dict[str, Any]
) -> None:
    worker = _StubWorker(avg_logprob=-9.0)
    _pipeline(config, paths, db_path, worker).run(recording["recording_uuid"])
    passes = {
        int(payload["asr_pass"]): payload
        for task, payload in worker.calls
        if task == "transcribe"
    }
    assert passes[1]["beam_size"] == config.asr.pass1_beam_size
    assert passes[1]["cpu_threads"] == config.asr.pass1_cpu_threads
    assert passes[2]["beam_size"] == config.asr.pass2_beam_size
    assert passes[2]["cpu_threads"] == config.asr.pass2_cpu_threads
    assert passes[1]["role"] == "pass1"
    assert passes[2]["role"] == "pass2"


# ===========================================================================
# Checkpointing
# ===========================================================================


def test_a_second_run_reuses_the_working_copy_and_the_vad_run(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    """Re-deriving something provably identical is waiting, not evidence."""
    first_worker = _StubWorker()
    _pipeline(config, paths, db_path, first_worker).run(recording["recording_uuid"])
    second_worker = _StubWorker()
    result = _pipeline(config, paths, db_path, second_worker).run(
        recording["recording_uuid"]
    )
    assert result.ok is True
    assert result.revision == 2
    stages = {stage["name"]: stage["detail"] for stage in result.stages}
    assert "reused the existing working copy" in stages["normalize_audio"]
    assert "reused the existing run" in stages["vad"]
    assert not any(task == "vad" for task, _payload in second_worker.calls)
    assert conn.execute("SELECT COUNT(*) FROM vad_runs").fetchone()[0] == 1


def test_a_changed_vad_configuration_forces_a_new_run(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    _pipeline(config, paths, db_path, _StubWorker()).run(recording["recording_uuid"])
    retuned = config.model_copy(
        update={"asr": config.asr.model_copy(update={"vad_threshold": 0.8})}
    )
    result = _pipeline(retuned, paths, db_path, _StubWorker()).run(
        recording["recording_uuid"]
    )
    assert result.ok is True
    runs = conn.execute("SELECT config_hash, is_active FROM vad_runs ORDER BY id").fetchall()
    assert len(runs) == 2
    assert runs[0]["config_hash"] != runs[1]["config_hash"]
    assert [row["is_active"] for row in runs] == [0, 1]


def test_a_deleted_working_copy_is_rebuilt(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    _pipeline(config, paths, db_path, _StubWorker()).run(recording["recording_uuid"])
    row = conn.execute("SELECT relative_path FROM audio_working_copies").fetchone()
    (paths.root / row["relative_path"]).unlink()
    result = _pipeline(config, paths, db_path, _StubWorker()).run(
        recording["recording_uuid"]
    )
    assert result.ok is True
    stages = {stage["name"]: stage["detail"] for stage in result.stages}
    assert "reused" not in stages["normalize_audio"]
    assert (paths.root / row["relative_path"]).is_file()


def test_a_tampered_working_copy_is_rebuilt_rather_than_trusted(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    _pipeline(config, paths, db_path, _StubWorker()).run(recording["recording_uuid"])
    row = conn.execute("SELECT relative_path, sha256 FROM audio_working_copies").fetchone()
    target = paths.root / row["relative_path"]
    with open(target, "r+b") as handle:
        handle.seek(200)
        handle.write(b"\x7f\x7f\x7f\x7f")
    result = _pipeline(config, paths, db_path, _StubWorker()).run(
        recording["recording_uuid"]
    )
    assert result.ok is True
    stages = {stage["name"]: stage["detail"] for stage in result.stages}
    assert "reused" not in stages["normalize_audio"]


# ===========================================================================
# Revisions
# ===========================================================================


def test_a_new_run_supersedes_the_previous_revision(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    for _ in range(3):
        _pipeline(config, paths, db_path, _StubWorker()).run(recording["recording_uuid"])
    rows = conn.execute("SELECT revision, is_active FROM transcripts ORDER BY revision").fetchall()
    assert [row["revision"] for row in rows] == [1, 2, 3]
    assert [row["is_active"] for row in rows] == [0, 0, 1]


def test_earlier_revisions_keep_their_segments(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    """Phase 7 needs the earlier revision to diff against."""
    _pipeline(config, paths, db_path, _StubWorker()).run(recording["recording_uuid"])
    _pipeline(config, paths, db_path, _StubWorker()).run(recording["recording_uuid"])
    counts = conn.execute(
        "SELECT transcript_id, COUNT(*) AS n FROM transcript_segments GROUP BY transcript_id"
    ).fetchall()
    assert len(counts) == 2
    assert all(row["n"] > 0 for row in counts)


# ===========================================================================
# Pass 2
# ===========================================================================


def test_a_confident_pass1_skips_pass2(
    config: Any, paths: Any, db_path: Path, recording: dict[str, Any]
) -> None:
    worker = _StubWorker(avg_logprob=-0.1)
    result = _pipeline(config, paths, db_path, worker).run(recording["recording_uuid"])
    assert result.pass2_skipped_reason == REASON_PASS2_NOTHING_FLAGGED
    assert not any(
        int(payload.get("asr_pass", 1)) == 2 for task, payload in worker.calls
    )


def test_a_low_confidence_pass1_triggers_pass2_and_supersedes(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    worker = _StubWorker(
        avg_logprob=-9.0,
        text=lambda pass_number, region: f"hasil pass {pass_number} region {region}",
    )
    result = _pipeline(config, paths, db_path, worker).run(recording["recording_uuid"])
    assert result.ok is True
    assert result.pass2_skipped_reason is None
    assert result.pass2_region_count > 0
    rows = conn.execute(
        "SELECT asr_pass, is_active, superseded_by_id, pass2_reason_codes "
        "FROM transcript_segments ORDER BY seq"
    ).fetchall()
    assert any(row["asr_pass"] == 2 and row["is_active"] == 1 for row in rows)
    retired = [row for row in rows if row["is_active"] == 0]
    assert retired, "a superseded pass-1 segment must remain as evidence"
    assert all(row["superseded_by_id"] is not None for row in retired)
    assert any(row["pass2_reason_codes"] for row in rows)


def test_pass2_is_bounded_by_the_budget(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    worker = _StubWorker(avg_logprob=-9.0)
    result = _pipeline(config, paths, db_path, worker).run(recording["recording_uuid"])
    assert result.pass2_selected_ms <= result.pass2_budget_ms
    row = conn.execute("SELECT * FROM transcripts WHERE is_active = 1").fetchone()
    assert row["pass2_selected_ms"] <= row["pass2_budget_ms"]


def test_disabling_pass2_is_reported_as_such(
    config: Any, paths: Any, db_path: Path, recording: dict[str, Any]
) -> None:
    off = config.model_copy(
        update={"asr": config.asr.model_copy(update={"pass2_enabled": False})}
    )
    worker = _StubWorker(avg_logprob=-9.0)
    result = _pipeline(off, paths, db_path, worker).run(recording["recording_uuid"])
    assert result.pass2_skipped_reason == REASON_PASS2_DISABLED
    assert result.ok is True


def test_a_budget_too_small_for_any_flagged_region_is_distinguished(
    config: Any, paths: Any, db_path: Path, recording: dict[str, Any]
) -> None:
    """"Nothing needed it" and "we could not afford it" are different facts."""
    tiny = config.model_copy(
        update={"asr": config.asr.model_copy(update={"pass2_budget_ratio": 0.001})}
    )
    worker = _StubWorker(avg_logprob=-9.0)
    result = _pipeline(tiny, paths, db_path, worker).run(recording["recording_uuid"])
    assert result.pass2_skipped_reason == REASON_PASS2_BUDGET_TOO_SMALL
    detail = next(
        stage["detail"]
        for stage in result.stages
        if stage["name"] == "asr_pass2_selective"
    )
    assert "pass2_budget_ratio" in detail


def test_a_missing_pass2_model_keeps_the_pass1_transcript(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    """A complete first pass is worth more than no transcript at all."""
    worker = _StubWorker(avg_logprob=-9.0, pass2_available=False)
    result = _pipeline(config, paths, db_path, worker).run(recording["recording_uuid"])
    assert result.ok is True
    assert result.pass2_skipped_reason == REASON_PASS2_MODEL_UNAVAILABLE
    row = conn.execute("SELECT * FROM transcripts WHERE is_active = 1").fetchone()
    assert row["status"] == "COMPLETE"
    assert row["segment_count"] > 0
    assert row["pass2_model_name"] is None


def test_a_pass2_that_returns_nothing_keeps_the_pass1_text(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    worker = _StubWorker(avg_logprob=-9.0, pass2_returns_nothing=True)
    result = _pipeline(config, paths, db_path, worker).run(recording["recording_uuid"])
    assert result.ok is True
    rows = conn.execute("SELECT asr_pass, is_active FROM transcript_segments").fetchall()
    assert all(row["is_active"] == 1 for row in rows)
    assert all(row["asr_pass"] == 1 for row in rows)


# ===========================================================================
# Failure and cancellation
# ===========================================================================


def test_a_missing_recording_is_reported_not_raised(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection
) -> None:
    result = _pipeline(config, paths, db_path, _StubWorker()).run(
        "44444444-4444-4444-8444-444444444444"
    )
    assert result.ok is False
    assert "no recording with uuid" in (result.error or "")


def test_a_recording_that_is_not_closed_is_refused(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    with conn:
        conn.execute(
            "UPDATE recordings SET status = 'RECORDING' WHERE recording_uuid = ?",
            (recording["recording_uuid"],),
        )
    result = _pipeline(config, paths, db_path, _StubWorker()).run(
        recording["recording_uuid"]
    )
    assert result.ok is False
    assert "not RECORDED" in (result.error or "")


def test_a_missing_pass1_model_fails_the_run_without_a_download(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    def refuse(task: str, payload: dict[str, Any]) -> dict[str, Any]:
        if task == "vad":
            from mom_igd.asr.tasks import TASK_REGISTRY

            return TASK_REGISTRY["vad"](payload, lambda: False)
        raise PipelineError(f"{REASON_MODEL_UNAVAILABLE}: nothing is ready")

    result = _pipeline(config, paths, db_path, refuse).run(recording["recording_uuid"])
    assert result.ok is False
    assert result.reason_code == REASON_MODEL_UNAVAILABLE
    row = conn.execute("SELECT * FROM transcripts").fetchone()
    assert row["status"] == "FAILED"
    assert row["is_active"] == 0


def test_a_failed_revision_never_becomes_active(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    _pipeline(config, paths, db_path, _StubWorker()).run(recording["recording_uuid"])

    def explode(task: str, payload: dict[str, Any]) -> dict[str, Any]:
        if task == "vad":
            from mom_igd.asr.tasks import TASK_REGISTRY

            return TASK_REGISTRY["vad"](payload, lambda: False)
        raise PipelineError("the worker died")

    result = _pipeline(config, paths, db_path, explode).run(recording["recording_uuid"])
    assert result.ok is False
    active = conn.execute("SELECT revision FROM transcripts WHERE is_active = 1").fetchall()
    assert len(active) == 1
    assert active[0]["revision"] == 1, "the earlier good revision must stay current"


def test_cancellation_before_pass1_leaves_a_cancelled_revision(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    seen = {"vad": False}

    def worker(task: str, payload: dict[str, Any]) -> dict[str, Any]:
        if task == "vad":
            from mom_igd.asr.tasks import TASK_REGISTRY

            seen["vad"] = True
            return TASK_REGISTRY["vad"](payload, lambda: False)
        raise AssertionError("pass 1 must not run after a cancel")

    result = _pipeline(
        config, paths, db_path, worker, should_cancel=lambda: seen["vad"]
    ).run(recording["recording_uuid"])
    assert result.ok is False
    assert result.cancelled is True
    assert result.reason_code == REASON_CANCELLED
    row = conn.execute("SELECT status, is_active FROM transcripts").fetchone()
    assert row["status"] == "CANCELLED"
    assert row["is_active"] == 0


def test_a_worker_that_reports_partial_completion_is_a_cancellation(
    config: Any, paths: Any, db_path: Path, recording: dict[str, Any]
) -> None:
    worker = _StubWorker(cancel_pass=1)
    result = _pipeline(config, paths, db_path, worker).run(recording["recording_uuid"])
    assert result.ok is False
    assert result.cancelled is True
    assert "pass 1 stopped after" in (result.error or "")


# ===========================================================================
# Empty and edge cases
# ===========================================================================


def test_a_recording_with_no_speech_completes_as_an_empty_revision(
    config: Any, paths: Any, db_path: Path, conn: sqlite3.Connection, recording: dict[str, Any]
) -> None:
    """An empty room is a legitimate outcome, not a failure to interpret."""

    def silent_vad(task: str, payload: dict[str, Any]) -> dict[str, Any]:
        if task == "vad":
            return {
                "ran": True,
                "regions": [],
                "region_count": 0,
                "audio_seconds": 8.0,
                "total_speech_seconds": 0.0,
                "speech_ratio": 0.0,
                "model_name": "silero-vad-v6-bundled",
                "model_sha256": "ab" * 32,
                "config_hash": "cd" * 32,
                "config": {},
            }
        raise AssertionError("nothing should be transcribed")

    result = _pipeline(config, paths, db_path, silent_vad).run(
        recording["recording_uuid"]
    )
    assert result.ok is True
    assert result.reason_code == REASON_NO_SPEECH
    assert result.segment_count == 0
    row = conn.execute("SELECT * FROM transcripts").fetchone()
    assert row["status"] == "COMPLETE"
    assert row["is_active"] == 1
    assert row["pass2_skipped_reason"] == REASON_NO_SPEECH


def test_the_result_carries_no_transcript_text_or_path(
    config: Any, paths: Any, db_path: Path, recording: dict[str, Any]
) -> None:
    worker = _StubWorker(text=lambda pass_number, region: "rahasia direksi")
    result = _pipeline(config, paths, db_path, worker).run(recording["recording_uuid"])
    blob = json.dumps(result.to_dict())
    assert "rahasia" not in blob
    assert str(paths.root) not in blob
    assert ":\\" not in blob


def test_the_run_reports_its_own_cost(
    config: Any, paths: Any, db_path: Path, recording: dict[str, Any]
) -> None:
    result = _pipeline(config, paths, db_path, _StubWorker()).run(
        recording["recording_uuid"]
    )
    assert result.wall_ms > 0
    assert result.rtf is not None and result.rtf > 0
    assert result.to_dict()["peak_rss_mib"] >= 0.0
