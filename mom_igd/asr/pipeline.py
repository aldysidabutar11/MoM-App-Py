"""The Phase 4 pipeline: master audio in, reviewed-ready transcript revision out.

Six stages, run in order, each one persisted before the next begins:

1. **validate** -- the master's manifest verifies and its chunks are readable.
2. **normalise** -- build the 16 kHz mono working copy on the master's timeline.
3. **VAD** -- find speech regions, in a worker.
4. **pass 1** -- transcribe every region with the fast configuration, in a worker.
5. **pass 2** -- re-transcribe the least confident regions under a budget, in a worker,
   then merge. Skipped cleanly when nothing is flagged or pass 2 is off.
6. **normalise terminology** -- fix technical spellings, keeping the raw text.

**One heavy model at a time, by construction.** Each heavy stage is a separate spawned
worker that exits before the next starts, so pass 1 has been fully released before pass 2
is loaded. That is not an optimisation: the measured worst-case working sets are 693 MiB and
1 910 MiB, and 2 603 MiB together exceeds the 2.5 GB budget. Co-residency would breach it.

**Checkpointed at every stage boundary.** A working copy whose recorded SHA-256 still
matches the file is reused; a VAD run whose configuration hash matches the current
configuration is reused. Restarting a three-hour meeting from the beginning because the
machine slept is not acceptable, and re-deriving something that is provably identical is
not evidence, it is waiting.

**A missing model is `MODEL_UNAVAILABLE` and stops the run.** Never a download, never a
fallback to whichever model happens to be present, never a fake provider. Pass 2 is the one
exception, and only in one direction: if the *pass-2* model is missing the transcript keeps
its pass-1 result and records why pass 2 did not run, because a complete first pass is worth
more than no transcript at all.

**Cancellation is cooperative and honest.** The check happens between regions and between
stages. A cancelled run leaves its revision `CANCELLED` and never active, so a partial
transcript is never mistaken for a finished one.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from mom_igd.logging_setup import get_logger

__all__ = ["PipelineError", "PipelineResult", "TranscriptionPipeline"]

_LOG = get_logger("asr.pipeline")

#: Cancellation and failure reason codes. Stable identifiers, because the UI and the
#: tests both key on them and a prose message is not an interface.
REASON_MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
REASON_PASS2_DISABLED = "PASS2_DISABLED"
REASON_PASS2_NOTHING_FLAGGED = "PASS2_NOTHING_FLAGGED"
REASON_PASS2_BUDGET_TOO_SMALL = "PASS2_BUDGET_TOO_SMALL"
REASON_PASS2_MODEL_UNAVAILABLE = "PASS2_MODEL_UNAVAILABLE"
REASON_NO_SPEECH = "NO_SPEECH_DETECTED"
REASON_CANCELLED = "CANCELLED"


class PipelineError(RuntimeError):
    """A stage failed in a way the run cannot continue past."""


@dataclass(slots=True)
class PipelineResult:
    """What one run produced. Serialisable, and carrying no transcript text."""

    ok: bool
    recording_uuid: str
    transcript_id: int | None = None
    revision: int | None = None
    working_copy_id: int | None = None
    vad_run_id: int | None = None
    error: str | None = None
    reason_code: str | None = None
    cancelled: bool = False
    stages: list[dict[str, Any]] = field(default_factory=list)
    audio_ms: int = 0
    speech_ms: int = 0
    region_count: int = 0
    segment_count: int = 0
    word_count: int = 0
    pass1_processing_ms: int = 0
    pass2_processing_ms: int = 0
    pass2_region_count: int = 0
    pass2_budget_ms: int = 0
    pass2_selected_ms: int = 0
    pass2_budget_exhausted: bool = False
    pass2_skipped_reason: str | None = None
    glossary_replacements: int = 0
    peak_rss_bytes: int = 0
    wall_ms: int = 0

    @property
    def rtf(self) -> float | None:
        """Wall-clock over audio duration. The number the operator actually waits for."""
        if self.audio_ms <= 0:
            return None
        return round(self.wall_ms / self.audio_ms, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "recording_uuid": self.recording_uuid,
            "transcript_id": self.transcript_id,
            "revision": self.revision,
            "working_copy_id": self.working_copy_id,
            "vad_run_id": self.vad_run_id,
            "error": self.error,
            "reason_code": self.reason_code,
            "cancelled": self.cancelled,
            "stages": self.stages,
            "audio_ms": self.audio_ms,
            "speech_ms": self.speech_ms,
            "region_count": self.region_count,
            "segment_count": self.segment_count,
            "word_count": self.word_count,
            "pass1_processing_ms": self.pass1_processing_ms,
            "pass2_processing_ms": self.pass2_processing_ms,
            "pass2_region_count": self.pass2_region_count,
            "pass2_budget_ms": self.pass2_budget_ms,
            "pass2_selected_ms": self.pass2_selected_ms,
            "pass2_budget_exhausted": self.pass2_budget_exhausted,
            "pass2_skipped_reason": self.pass2_skipped_reason,
            "glossary_replacements": self.glossary_replacements,
            "peak_rss_bytes": self.peak_rss_bytes,
            "peak_rss_mib": round(self.peak_rss_bytes / (1 << 20), 1),
            "wall_ms": self.wall_ms,
            "rtf": self.rtf,
        }


def _to_ms(seconds: float) -> int:
    return int(round(float(seconds) * 1000.0))


class TranscriptionPipeline:
    """Runs the pipeline for one recording. Construct per run, not per application."""

    def __init__(
        self,
        *,
        config: Any,
        paths: Any,
        connect: Callable[[], sqlite3.Connection],
        progress: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self._config = config
        self._paths = paths
        self._connect = connect
        self._progress = progress
        self._should_cancel = should_cancel
        self._peak_rss = 0

    # -- helpers ------------------------------------------------------------

    def _say(self, message: str) -> None:
        if self._progress:
            self._progress(message)

    def _cancelled(self) -> bool:
        return bool(self._should_cancel and self._should_cancel())

    def _stage(
        self, stages: list[dict[str, Any]], name: str, ok: bool, detail: str, ms: int = 0
    ) -> None:
        stages.append({"name": name, "ok": bool(ok), "detail": detail, "ms": ms})
        self._say(f"{'ok ' if ok else 'FAIL'} {name}: {detail}")

    def _run_worker(self, task: str, payload: Mapping[str, Any]) -> Any:
        """One heavy stage, in its own process. Peak RSS is accumulated across stages."""
        from mom_igd.asr.worker import WorkerTimeout, run_in_worker

        try:
            outcome = run_in_worker(
                task,
                dict(payload),
                timeout_seconds=float(self._config.asr.worker_timeout_seconds),
                should_cancel=self._should_cancel,
                progress=self._progress,
            )
        except WorkerTimeout as exc:
            raise PipelineError(f"{REASON_CANCELLED}: {exc}") from None
        self._peak_rss = max(self._peak_rss, outcome.peak_rss_bytes)
        if not outcome.ok:
            raise PipelineError(outcome.error or f"the {task} worker failed")
        return outcome.payload

    # -- stage 1: validate --------------------------------------------------

    def _load_recording(self, conn: sqlite3.Connection, recording_uuid: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT r.*, m.uuid AS meeting_uuid FROM recordings r "
            "JOIN meetings m ON m.id = r.meeting_id WHERE r.recording_uuid = ?",
            (recording_uuid,),
        ).fetchone()
        if row is None:
            raise PipelineError(
                f"no recording with uuid {recording_uuid}. `audio devices` and the "
                "meeting panel list what exists."
            )
        if str(row["status"]) != "RECORDED":
            raise PipelineError(
                f"recording {recording_uuid} is {row['status']}, not RECORDED. "
                "Transcription runs on a closed recording: finish or recover the "
                "capture first."
            )
        return row

    # -- stage 2: normalise -------------------------------------------------

    def _ensure_working_copy(
        self, conn: sqlite3.Connection, recording: sqlite3.Row, stages: list[dict[str, Any]]
    ) -> tuple[int, Path, dict[str, Any]]:
        from mom_igd.asr.manifest import sha256_file
        from mom_igd.asr.normalize import normalize_recording
        from mom_igd.asr.store import get_working_copy, save_working_copy
        from mom_igd.audio.manifest import read_manifest

        recording_uuid = str(recording["recording_uuid"])
        target = self._paths.working_copy_path(recording_uuid)
        existing = get_working_copy(conn, recording_id=int(recording["id"]))

        if (
            existing is not None
            and str(existing["status"]) == "READY"
            and target.is_file()
            and existing["sha256"]
            and sha256_file(target) == str(existing["sha256"])
        ):
            self._stage(
                stages,
                "normalize_audio",
                True,
                f"reused the existing working copy ({int(existing['duration_ms']) / 1000:.1f}s, "
                f"{int(existing['gap_count'])} recorded gap(s)); its SHA-256 still matches, "
                "so re-deriving it would produce the same bytes",
            )
            return (
                int(existing["id"]),
                target,
                {
                    "duration_ms": int(existing["duration_ms"]),
                    "gaps": _load_gaps(existing),
                },
            )

        directory = self._paths.recordings_dir / str(recording["relative_dir"])
        records, _events, _errors = read_manifest(directory)
        usable = [record for record in records if record.is_usable_audio]
        if not usable:
            raise PipelineError(
                f"the manifest for {recording_uuid} lists no usable chunk. Run "
                "`audio verify` -- every chunk is missing, corrupt or quarantined, and "
                "there is no audio to transcribe."
            )

        started = time.perf_counter()
        result = normalize_recording(
            chunk_paths=[directory / record.filename for record in usable],
            chunk_start_frames=[record.start_frame for record in usable],
            chunk_frame_counts=[record.frame_count for record in usable],
            target_path=target,
            data_root=self._paths.root,
            source_manifest_sha256=(
                str(recording["manifest_sha256"]) if recording["manifest_sha256"] else None
            ),
            expected_total_frames=(
                int(recording["written_frames"]) if recording["written_frames"] else None
            ),
        )
        payload = result.to_dict()
        payload["status"] = "READY"
        working_copy_id = save_working_copy(
            conn, recording_id=int(recording["id"]), payload=payload
        )
        detail = (
            f"{result.duration_ms / 1000:.1f}s at {result.source_sample_rate} Hz / "
            f"{result.source_channels}ch -> 16 kHz mono, {len(usable)} chunk(s), "
            f"{len(result.gaps)} gap(s) filled with recorded silence"
        )
        if result.warnings:
            detail += f"; {len(result.warnings)} warning(s): {result.warnings[0]}"
        if result.skipped_chunks:
            detail += f"; {len(result.skipped_chunks)} chunk(s) absent from disk"
        self._stage(
            stages,
            "normalize_audio",
            True,
            detail,
            _to_ms(time.perf_counter() - started),
        )
        return working_copy_id, target, payload

    # -- stage 3: VAD -------------------------------------------------------

    def _vad_config(self) -> dict[str, Any]:
        asr = self._config.asr
        return {
            "threshold": asr.vad_threshold,
            "min_speech_ms": asr.vad_min_speech_ms,
            "min_silence_ms": asr.vad_min_silence_ms,
            "speech_pad_ms": asr.vad_speech_pad_ms,
            "merge_gap_ms": asr.vad_merge_gap_ms,
            "max_region_seconds": asr.vad_max_region_seconds,
        }

    def _ensure_vad(
        self,
        conn: sqlite3.Connection,
        *,
        working_copy_id: int,
        audio_path: Path,
        gaps: Sequence[Mapping[str, Any]],
        stages: list[dict[str, Any]],
    ) -> tuple[int, list[dict[str, Any]], int]:
        from mom_igd.asr.store import get_active_vad_run, list_regions, save_vad_run
        from mom_igd.asr.vad import VadConfig

        wanted = VadConfig(**self._vad_config()).config_hash
        existing = get_active_vad_run(conn, working_copy_id=working_copy_id)
        if existing is not None and str(existing["config_hash"]) == wanted:
            regions = [
                {
                    "seq": int(row["seq"]),
                    "start_ms": int(row["start_ms"]),
                    "end_ms": int(row["end_ms"]),
                    "overlaps_gap": bool(row["overlaps_gap"]),
                }
                for row in list_regions(conn, vad_run_id=int(existing["id"]))
            ]
            self._stage(
                stages,
                "vad",
                True,
                f"reused the existing run: {len(regions)} region(s), "
                f"{int(existing['total_speech_ms']) / 1000:.1f}s of speech, same "
                "configuration hash",
            )
            return int(existing["id"]), regions, int(existing["total_speech_ms"])

        started = time.perf_counter()
        payload = self._run_worker(
            "vad", {"audio_path": str(audio_path), "config": self._vad_config()}
        )
        regions = [
            {
                "seq": index,
                "start_ms": _to_ms(region["start"]),
                "end_ms": _to_ms(region["end"]),
            }
            for index, region in enumerate(payload.get("regions") or [])
        ]
        speech_ms = _to_ms(payload.get("total_speech_seconds", 0.0))
        vad_run_id = save_vad_run(
            conn,
            working_copy_id=working_copy_id,
            payload={
                "model_name": payload["model_name"],
                "model_sha256": payload["model_sha256"],
                "config_hash": payload["config_hash"],
                "config": payload.get("config") or {},
                "audio_ms": _to_ms(payload.get("audio_seconds", 0.0)),
                "total_speech_ms": speech_ms,
                "merged_count": payload.get("merged_count", 0),
                "split_count": payload.get("split_count", 0),
                "dropped_short_count": payload.get("dropped_short_count", 0),
            },
            regions=regions,
            gaps=gaps,
        )
        flagged = sum(
            1
            for row in list_regions(conn, vad_run_id=vad_run_id)
            if int(row["overlaps_gap"])
        )
        detail = (
            f"{len(regions)} region(s), {speech_ms / 1000:.1f}s of speech "
            f"({payload.get('speech_ratio', 0.0) * 100:.0f}% of the recording)"
        )
        if flagged:
            detail += f"; {flagged} overlap a gap that was filled with silence"
        self._stage(stages, "vad", True, detail, _to_ms(time.perf_counter() - started))
        return vad_run_id, regions, speech_ms

    # -- stages 4 and 5: transcription --------------------------------------

    def _transcribe(
        self,
        *,
        audio_path: Path,
        regions: Sequence[Mapping[str, Any]],
        role: str,
        beam_size: int,
        cpu_threads: int,
        initial_prompt: str | None,
        asr_pass: int,
    ) -> dict[str, Any]:
        return self._run_worker(
            "transcribe",
            {
                "models_dir": str(self._paths.models_dir),
                "audio_path": str(audio_path),
                "role": role,
                "asr_pass": asr_pass,
                "regions": [
                    {
                        "index": int(region["seq"]),
                        "start": int(region["start_ms"]) / 1000.0,
                        "end": int(region["end_ms"]) / 1000.0,
                    }
                    for region in regions
                ],
                "language": self._config.asr.language,
                "beam_size": beam_size,
                "temperature": 0.0,
                "cpu_threads": cpu_threads,
                "compute_type": self._config.asr.compute_type,
                "word_timestamps": True,
                "initial_prompt": initial_prompt,
            },
        )

    # -- the run ------------------------------------------------------------

    def run(self, recording_uuid: str, *, job_id: int | None = None) -> PipelineResult:
        """Run every stage for one recording and return what happened.

        Never raises for an expected outcome: a missing model, an unusable recording or a
        cancellation come back as a result with a reason code, because those are states
        an operator has to be told about rather than a stack trace.
        """
        from mom_igd.asr.glossary import load_glossary, normalise_segments
        from mom_igd.asr.merge import merge_pass2_into_pass1
        from mom_igd.asr.selection import SelectionPolicy, select_regions_for_pass2
        from mom_igd.asr.store import (
            activate_transcript,
            create_transcript,
            fail_transcript,
            save_segments,
            update_transcript,
        )

        wall_started = time.perf_counter()
        stages: list[dict[str, Any]] = []
        result = PipelineResult(ok=False, recording_uuid=recording_uuid, stages=stages)
        transcript_id: int | None = None
        conn = self._connect()
        try:
            recording = self._load_recording(conn, recording_uuid)
            self._stage(
                stages,
                "validate_audio",
                True,
                f"{int(recording['chunk_count'])} chunk(s), "
                f"{int(recording['duration_ms'] or 0) / 1000:.1f}s, manifest "
                f"{str(recording['manifest_status'])}",
            )

            working_copy_id, audio_path, copy_payload = self._ensure_working_copy(
                conn, recording, stages
            )
            result.working_copy_id = working_copy_id
            result.audio_ms = int(copy_payload["duration_ms"])

            if self._cancelled():
                raise PipelineError(f"{REASON_CANCELLED}: cancelled before VAD")

            vad_run_id, regions, speech_ms = self._ensure_vad(
                conn,
                working_copy_id=working_copy_id,
                audio_path=audio_path,
                gaps=copy_payload.get("gaps") or [],
                stages=stages,
            )
            result.vad_run_id = vad_run_id
            result.region_count = len(regions)
            result.speech_ms = speech_ms

            transcript_id = create_transcript(
                conn,
                recording_id=int(recording["id"]),
                working_copy_id=working_copy_id,
                vad_run_id=vad_run_id,
                job_id=job_id,
                language=self._config.asr.language,
            )
            result.transcript_id = transcript_id

            glossary = None
            if self._config.asr.glossary_enabled:
                from mom_igd.paths import repo_root

                glossary = load_glossary(
                    repo_root() / "config" / self._config.asr.glossary_filename
                )
            prompt = (
                glossary.initial_prompt(
                    max_chars=int(self._config.asr.initial_prompt_max_chars)
                )
                if glossary is not None
                else None
            )

            if not regions:
                # A recording with no detected speech is a legitimate outcome, not a
                # failure. It completes as an empty revision with the reason recorded,
                # so an operator sees "no speech was detected" rather than an empty
                # transcript they have to interpret.
                update_transcript(
                    conn,
                    transcript_id,
                    audio_ms=result.audio_ms,
                    speech_ms=0,
                    segment_count=0,
                    word_count=0,
                    pass2_skipped_reason=REASON_NO_SPEECH,
                )
                activate_transcript(conn, transcript_id=transcript_id)
                self._stage(
                    stages,
                    "asr_pass1",
                    True,
                    "no speech regions were detected, so there was nothing to "
                    "transcribe. The revision is complete and empty.",
                )
                result.ok = True
                result.reason_code = REASON_NO_SPEECH
                result.revision = _revision_of(conn, transcript_id)
                return result

            if self._cancelled():
                raise PipelineError(f"{REASON_CANCELLED}: cancelled before pass 1")

            started = time.perf_counter()
            pass1 = self._transcribe(
                audio_path=audio_path,
                regions=regions,
                role="pass1",
                beam_size=int(self._config.asr.pass1_beam_size),
                cpu_threads=int(self._config.asr.pass1_cpu_threads),
                initial_prompt=prompt,
                asr_pass=1,
            )
            pass1_ms = _to_ms(time.perf_counter() - started)
            result.pass1_processing_ms = pass1_ms
            if pass1.get("cancelled"):
                raise PipelineError(
                    f"{REASON_CANCELLED}: pass 1 stopped after "
                    f"{pass1.get('regions_completed', 0)} of {len(regions)} region(s)"
                )
            segments = _segments_from_worker(pass1, asr_pass=1)
            model1 = pass1.get("model") or {}
            update_transcript(
                conn,
                transcript_id,
                audio_ms=result.audio_ms,
                speech_ms=speech_ms,
                language=str(pass1.get("language") or self._config.asr.language),
                language_probability=pass1.get("language_probability"),
                pass1_model_name=model1.get("model_name"),
                pass1_model_revision=model1.get("revision"),
                pass1_manifest_sha256=model1.get("manifest_sha256"),
                pass1_compute_type=model1.get("compute_type"),
                pass1_beam_size=int(self._config.asr.pass1_beam_size),
                pass1_cpu_threads=int(self._config.asr.pass1_cpu_threads),
                pass1_processing_ms=pass1_ms,
            )
            self._stage(
                stages,
                "asr_pass1",
                True,
                f"{len(segments)} segment(s) over {len(regions)} region(s) with "
                f"{model1.get('model_name', 'the pass-1 model')} "
                f"(beam {self._config.asr.pass1_beam_size}, "
                f"{self._config.asr.pass1_cpu_threads} threads)"
                + _straddle_note(pass1),
                pass1_ms,
            )

            # -- pass 2 -----------------------------------------------------
            policy = SelectionPolicy.from_config(self._config)
            selection = select_regions_for_pass2(
                segments=segments, regions=regions, policy=policy
            )
            result.pass2_budget_ms = selection.budget_ms
            result.pass2_selected_ms = selection.selected_ms
            result.pass2_budget_exhausted = selection.budget_exhausted
            _mark_selection(segments, selection)

            merged = segments
            pass2_ms = 0
            skipped_reason: str | None = None
            if not policy.enabled:
                skipped_reason = REASON_PASS2_DISABLED
            elif not selection.selected:
                # "Nothing needed re-transcribing" and "the budget could not cover
                # anything that did" are different facts about a transcript, and only one
                # of them is good news.
                skipped_reason = (
                    REASON_PASS2_BUDGET_TOO_SMALL
                    if selection.flagged
                    else REASON_PASS2_NOTHING_FLAGGED
                )
            elif self._cancelled():
                raise PipelineError(f"{REASON_CANCELLED}: cancelled before pass 2")

            if skipped_reason is None:
                chosen = [
                    {
                        "seq": region.region_seq,
                        "start_ms": region.start_ms,
                        "end_ms": region.end_ms,
                    }
                    for region in sorted(
                        selection.selected, key=lambda item: item.start_ms
                    )
                ]
                started = time.perf_counter()
                try:
                    pass2 = self._transcribe(
                        audio_path=audio_path,
                        regions=chosen,
                        role="pass2",
                        beam_size=int(self._config.asr.pass2_beam_size),
                        cpu_threads=int(self._config.asr.pass2_cpu_threads),
                        initial_prompt=prompt,
                        asr_pass=2,
                    )
                except PipelineError as exc:
                    # A missing pass-2 model must not cost the operator the pass-1
                    # transcript they already paid for. Recorded, not hidden.
                    if REASON_MODEL_UNAVAILABLE in str(exc):
                        skipped_reason = REASON_PASS2_MODEL_UNAVAILABLE
                        self._stage(
                            stages,
                            "asr_pass2_selective",
                            True,
                            "the pass-2 model is not provisioned, so the pass-1 "
                            "transcript stands. Provision it with `asr provision "
                            "asr-pass2` and re-run to improve the flagged regions.",
                        )
                        pass2 = None
                    else:
                        raise
                else:
                    pass2_ms = _to_ms(time.perf_counter() - started)

                if pass2 is not None:
                    if pass2.get("cancelled"):
                        raise PipelineError(
                            f"{REASON_CANCELLED}: pass 2 stopped after "
                            f"{pass2.get('regions_completed', 0)} of {len(chosen)} "
                            "region(s)"
                        )
                    replacements = _segments_from_worker(pass2, asr_pass=2)
                    merge = merge_pass2_into_pass1(
                        pass1_segments=segments,
                        pass2_segments=replacements,
                        replaced_region_seqs=[item["seq"] for item in chosen],
                    )
                    merged = list(merge.segments)
                    model2 = pass2.get("model") or {}
                    update_transcript(
                        conn,
                        transcript_id,
                        pass2_model_name=model2.get("model_name"),
                        pass2_model_revision=model2.get("revision"),
                        pass2_manifest_sha256=model2.get("manifest_sha256"),
                        pass2_compute_type=model2.get("compute_type"),
                        pass2_beam_size=int(self._config.asr.pass2_beam_size),
                        pass2_cpu_threads=int(self._config.asr.pass2_cpu_threads),
                        pass2_processing_ms=pass2_ms,
                        pass2_budget_ms=selection.budget_ms,
                        pass2_selected_ms=selection.selected_ms,
                        pass2_region_count=len(chosen),
                        pass2_budget_exhausted=1 if selection.budget_exhausted else 0,
                    )
                    result.pass2_region_count = len(chosen)
                    result.pass2_processing_ms = pass2_ms
                    detail = (
                        f"re-transcribed {len(chosen)} of {len(regions)} region(s) "
                        f"({selection.selected_ms / 1000:.1f}s of a "
                        f"{selection.budget_ms / 1000:.1f}s budget), "
                        f"{len(merge.text_changed_regions)} came back different"
                    )
                    if selection.budget_exhausted:
                        detail += (
                            f"; the budget was exhausted and "
                            f"{len(selection.flagged) - len(selection.selected)} flagged "
                            "region(s) were left on the pass-1 result"
                        )
                    if merge.regions_without_replacement:
                        detail += (
                            f"; {len(merge.regions_without_replacement)} region(s) "
                            "returned nothing and kept their pass-1 text"
                        )
                    if merge.coverage_supersessions:
                        # Normal in small numbers -- a pass-2 segment routinely spans
                        # more than the region it was filed under. Reported because a
                        # large number means region attribution is drifting, and
                        # because this is the count that was silently zero while the
                        # transcript said things twice.
                        detail += (
                            f"; {merge.coverage_supersessions} pass-1 segment(s) were "
                            "retired by a replacement filed under a neighbouring region"
                        )
                    # Pass 2 runs the same validation, so it makes the same corrections.
                    # Reporting them on pass 1 only would have made a pass-2 regression
                    # invisible -- and pass 2 is the one that rewrites text.
                    detail += _straddle_note(pass2)
                    self._stage(stages, "asr_pass2_selective", True, detail, pass2_ms)
            else:
                if skipped_reason != REASON_PASS2_MODEL_UNAVAILABLE:
                    self._stage(
                        stages,
                        "asr_pass2_selective",
                        True,
                        _skip_detail(skipped_reason, selection),
                    )

            result.pass2_skipped_reason = skipped_reason
            if skipped_reason is not None:
                update_transcript(
                    conn,
                    transcript_id,
                    pass2_skipped_reason=skipped_reason,
                    pass2_budget_ms=selection.budget_ms,
                    pass2_selected_ms=0,
                    pass2_region_count=0,
                )

            # -- terminology -------------------------------------------------
            normalised, replacements_made = normalise_segments(merged, glossary)
            result.glossary_replacements = replacements_made
            segment_count, word_count = save_segments(
                conn, transcript_id=transcript_id, segments=normalised
            )
            update_transcript(
                conn,
                transcript_id,
                glossary_version=glossary.version if glossary else None,
                glossary_sha256=glossary.sha256 if glossary else None,
                glossary_replacements=replacements_made,
                segment_count=segment_count,
                word_count=word_count,
                peak_rss_bytes=self._peak_rss,
            )
            self._stage(
                stages,
                "normalize_terminology",
                True,
                (
                    f"{replacements_made} technical term(s) corrected under glossary "
                    f"v{glossary.version}; the model's original wording is kept beside "
                    "each segment"
                )
                if glossary is not None
                else "terminology normalisation is disabled in configuration",
            )

            activate_transcript(conn, transcript_id=transcript_id)
            result.segment_count = segment_count
            result.word_count = word_count
            result.peak_rss_bytes = self._peak_rss
            result.revision = _revision_of(conn, transcript_id)
            result.ok = True
            return result

        except PipelineError as exc:
            message = str(exc)
            cancelled = message.startswith(REASON_CANCELLED)
            result.error = message
            result.cancelled = cancelled
            result.reason_code = (
                REASON_CANCELLED
                if cancelled
                else (REASON_MODEL_UNAVAILABLE if REASON_MODEL_UNAVAILABLE in message else None)
            )
            if transcript_id is not None:
                fail_transcript(
                    conn, transcript_id=transcript_id, error=message, cancelled=cancelled
                )
            self._stage(stages, "pipeline", False, message)
            return result
        except Exception as exc:  # noqa: BLE001 - reported, never a crash into the UI
            message = f"{type(exc).__name__}: {str(exc)[:300]}"
            result.error = message
            if transcript_id is not None:
                fail_transcript(conn, transcript_id=transcript_id, error=message)
            self._stage(stages, "pipeline", False, message)
            _LOG.exception("asr.pipeline.unexpected", extra={"recording": recording_uuid})
            return result
        finally:
            result.wall_ms = _to_ms(time.perf_counter() - wall_started)
            result.peak_rss_bytes = self._peak_rss
            conn.close()


def _revision_of(conn: sqlite3.Connection, transcript_id: int) -> int | None:
    row = conn.execute(
        "SELECT revision FROM transcripts WHERE id = ?", (transcript_id,)
    ).fetchone()
    return int(row["revision"]) if row is not None else None


def _load_gaps(row: sqlite3.Row) -> list[dict[str, Any]]:
    import json

    raw = row["gaps_json"]
    if not raw:
        return []
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return loaded if isinstance(loaded, list) else []


def _straddle_note(payload: Mapping[str, Any]) -> str:
    """Report word/segment boundary corrections, and only when there were any.

    The engine derives segment bounds and word bounds from two different estimators, so
    a few words per recording sit just outside the segment holding them and are clamped
    back in. Four such words is the engine being itself. Four hundred, or one off by a
    second, is a regression in the model or the windowing -- and the difference is only
    visible if the number is printed.

    Silent on a clean run, because a line that appears every time is a line nobody
    reads.
    """
    count = int(payload.get("straddling_words", 0) or 0)
    if count <= 0:
        return ""
    worst = float(payload.get("worst_straddle_ms", 0.0) or 0.0)
    return (
        f"; {count} word(s) sat outside the segment holding them, by up to "
        f"{worst:.0f} ms, and were clamped back into it"
    )


def _segments_from_worker(payload: Mapping[str, Any], *, asr_pass: int) -> list[dict[str, Any]]:
    """Convert a worker's float-seconds segments into the millisecond rows we store."""
    out: list[dict[str, Any]] = []
    for segment in payload.get("segments") or []:
        words = [
            {
                "text": word.get("text", ""),
                "start_ms": _to_ms(word.get("start", 0.0)),
                "end_ms": _to_ms(word.get("end", 0.0)),
                "probability": word.get("probability"),
            }
            for word in segment.get("words") or []
        ]
        out.append(
            {
                "seq": len(out),
                "region_seq": segment.get("region_index"),
                "asr_pass": asr_pass,
                "start_ms": _to_ms(segment.get("start", 0.0)),
                "end_ms": _to_ms(segment.get("end", 0.0)),
                "text": segment.get("text", ""),
                "avg_logprob": segment.get("avg_logprob"),
                "no_speech_prob": segment.get("no_speech_prob"),
                "compression_ratio": segment.get("compression_ratio"),
                "temperature": segment.get("temperature"),
                "words": words,
                "is_active": True,
            }
        )
    return out


def _mark_selection(segments: list[dict[str, Any]], selection: Any) -> None:
    """Copy each region's verdict onto the segments that belong to it."""
    by_region = {region.region_seq: region for region in selection.regions}
    for segment in segments:
        region_seq = segment.get("region_seq")
        verdict = by_region.get(region_seq) if region_seq is not None else None
        if verdict is None:
            continue
        segment["pass2_reason_codes"] = list(verdict.reason_codes)
        segment["selected_for_pass2"] = verdict.selected
        segment["pass2_rank"] = verdict.rank


def _skip_detail(reason: str, selection: Any) -> str:
    if reason == REASON_PASS2_DISABLED:
        return "pass 2 is disabled in configuration, so the pass-1 transcript stands"
    if reason == REASON_PASS2_NOTHING_FLAGGED:
        return (
            "no region tripped a selection rule, so there was nothing worth "
            f"re-transcribing ({selection.budget_ms / 1000:.1f}s of budget unspent)"
        )
    if reason == REASON_PASS2_BUDGET_TOO_SMALL:
        longest = min(
            (region.duration_ms for region in selection.flagged), default=0
        )
        return (
            f"{len(selection.flagged)} region(s) tripped a rule but none fits the "
            f"{selection.budget_ms / 1000:.1f}s budget (the shortest is "
            f"{longest / 1000:.1f}s). Raise [asr].pass2_budget_ratio to re-transcribe "
            "them; the pass-1 transcript stands unchanged."
        )
    return reason
