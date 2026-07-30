"""Phase 4A benchmark harness. Measures; never assumes.

Separate from the production pipeline on purpose: a benchmark that shares code with
production tends to grow flags that change production behaviour, and a benchmark that
*is* production cannot vary threads or compute type freely.

**Every heavy run happens in a spawned worker**, so peak resident memory is the ASR
process's own and not this interpreter's plus whatever pytest left behind. Peak RSS
cannot be read after a process exits, so the parent samples it while the child runs.

**What is measured and what is honestly unavailable.** Load time, wall-clock time,
real-time factor, peak RSS including children, CPU seconds, peak thread count, segment
and word counts, and VAD speech duration are all measured directly. Word error rate,
technical-term accuracy and word-timestamp error require a reference transcript; with no
evaluation corpus present they are reported as ``N/A`` with the reason, never estimated
and never derived from the model's own output. Thermal data is reported as
``N/A -- sensor unavailable`` on this platform rather than invented.

**The synthetic-audio caveat, stated once and carried into every record.** Without a
speech corpus the harness generates a deterministic tone. The decoder does real work on
it -- measurably so, and beam size alone changes throughput by more than 2x -- so the
numbers are a valid measurement of *engine throughput on this machine*. They are **not**
a substitute for a real-audio measurement: real speech has different segment density and
triggers the temperature-fallback path differently. Every synthetic result is labelled
``audio_kind="synthetic"`` and every report carries the caveat.
"""

from __future__ import annotations

import os
import platform
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final

from mom_igd.logging_setup import get_logger

__all__ = [
    "DEFAULT_THREAD_SWEEP",
    "BenchmarkError",
    "CorpusSample",
    "load_corpus_manifest",
    "run_benchmark",
    "validate_corpus_manifest",
]

_LOG = get_logger("asr.benchmark")

#: Thread counts to sweep on an i7-1260P: 4 P-cores, 8 E-cores, 16 logical. The sweep
#: brackets the interesting region -- below the P-core count, at it, and into the
#: E-cores where scheduling starts to cost more than it adds.
DEFAULT_THREAD_SWEEP: Final[tuple[int, ...]] = (4, 6, 8, 10, 12)

#: Go/no-go targets from the Phase 4A brief, recorded with every run so a report can
#: never be read without them.
TARGETS: Final[dict[str, Any]] = {
    "peak_rss_bytes": 2.5 * (1 << 30),
    "total_rtf": 1.0,
    "clean_wer": 0.25,
    "far_field_wer": 0.35,
    "median_timestamp_error_ms": 200.0,
    "p95_timestamp_error_ms": 500.0,
    "pass2_relative_wer_improvement": 0.05,
}


class BenchmarkError(RuntimeError):
    """The benchmark was refused. The message says what is missing."""


@dataclass(frozen=True, slots=True)
class CorpusSample:
    """One evaluation sample. Audio and transcripts live **outside** the repository."""

    sample_uuid: str
    audio_path: Path
    sha256: str
    duration_seconds: float
    language: str
    reference_transcript_path: Path | None
    consent_status: str
    license_name: str
    technical_terms: tuple[str, ...] = ()
    word_timestamp_reference_path: Path | None = None
    condition: str = "unknown"

    @property
    def has_reference(self) -> bool:
        return (
            self.reference_transcript_path is not None
            and self.reference_transcript_path.is_file()
        )


def load_corpus_manifest(path: str | Path) -> tuple[CorpusSample, ...]:
    """Load an evaluation manifest that references audio outside the repository.

    Refuses a manifest whose samples are missing or whose checksums disagree: a
    benchmark run against different bytes than the manifest describes is not
    reproducible, and silently skipping a missing sample would quietly shrink the
    corpus and flatter the result.
    """
    import json

    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise BenchmarkError(f"evaluation manifest {manifest_path} does not exist")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BenchmarkError(f"{manifest_path} is not valid JSON: {exc}") from None
    entries = payload.get("samples")
    if not isinstance(entries, list) or not entries:
        raise BenchmarkError(f"{manifest_path} lists no samples")

    from mom_igd.asr.manifest import sha256_file

    samples: list[CorpusSample] = []
    for raw in entries:
        try:
            audio = Path(str(raw["audio_path"]))
            sample = CorpusSample(
                sample_uuid=str(raw["sample_uuid"]),
                audio_path=audio,
                sha256=str(raw["sha256"]).lower(),
                duration_seconds=float(raw["duration_seconds"]),
                language=str(raw.get("language", "id")),
                reference_transcript_path=(
                    Path(str(raw["reference_transcript_path"]))
                    if raw.get("reference_transcript_path")
                    else None
                ),
                consent_status=str(raw.get("consent_status", "unknown")),
                license_name=str(raw.get("license_name", "unknown")),
                technical_terms=tuple(str(t) for t in (raw.get("technical_terms") or ())),
                word_timestamp_reference_path=(
                    Path(str(raw["word_timestamp_reference_path"]))
                    if raw.get("word_timestamp_reference_path")
                    else None
                ),
                condition=str(raw.get("condition", "unknown")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BenchmarkError(f"malformed sample entry in {manifest_path}: {exc}") from None
        if not sample.audio_path.is_file():
            raise BenchmarkError(
                f"sample {sample.sample_uuid} references {sample.audio_path}, which does "
                "not exist. A benchmark must not silently skip a missing sample."
            )
        actual = sha256_file(sample.audio_path)
        if actual != sample.sha256:
            raise BenchmarkError(
                f"sample {sample.sample_uuid} checksum mismatch: manifest says "
                f"{sample.sha256[:16]}..., file hashes to {actual[:16]}..."
            )
        if sample.consent_status.lower() not in {"granted", "public-licensed", "synthetic"}:
            raise BenchmarkError(
                f"sample {sample.sample_uuid} has consent_status="
                f"{sample.consent_status!r}. Benchmarking real voices requires recorded "
                "consent or a clear public licence."
            )
        samples.append(sample)
    return tuple(samples)


# ---------------------------------------------------------------------------
# Accuracy metrics -- only ever computed against a real reference
# ---------------------------------------------------------------------------


def _normalise_for_wer(text: str) -> list[str]:
    """Lower-case, strip punctuation, collapse whitespace. Nothing cleverer.

    Deliberately conservative: aggressive normalisation is the easiest way to make a
    WER number look better than the transcript is. No number expansion, no synonym
    folding, no stemming.
    """
    import re
    import unicodedata

    folded = unicodedata.normalize("NFKC", text).lower()
    folded = re.sub(r"[^\w\s']", " ", folded, flags=re.UNICODE)
    return [token for token in folded.split() if token]


def validate_corpus_manifest(path: str | Path) -> dict[str, Any]:
    """Check an evaluation manifest without loading a model or decoding anything.

    Producing a reference transcript costs four to six times the audio duration, and a
    benchmark run costs minutes. Finding out that a checksum is wrong *after* both is
    avoidable, so this exists: it applies exactly the same loader -- the same schema, the
    same checksum verification, the same consent gate -- and then reports what it found.

    Read-only. It touches no model, runs no inference and writes nothing to the database.
    """
    report: dict[str, Any] = {
        "manifest": str(Path(path)),
        "ok": False,
        "problem": None,
        "sample_count": 0,
        "samples": [],
        "total_duration_seconds": 0.0,
        "with_reference": 0,
        "with_word_timestamps": 0,
        "conditions": {},
        "warnings": [],
    }
    try:
        samples = load_corpus_manifest(path)
    except BenchmarkError as exc:
        report["problem"] = str(exc)
        return report

    conditions: dict[str, int] = {}
    for sample in samples:
        conditions[sample.condition] = conditions.get(sample.condition, 0) + 1
        actual_seconds: float | None = None
        try:
            import wave

            with wave.open(str(sample.audio_path), "rb") as handle:
                rate = handle.getframerate()
                if rate:
                    actual_seconds = handle.getnframes() / rate
        except Exception:  # noqa: BLE001 - not a WAV, or a format wave cannot read
            actual_seconds = None

        if actual_seconds is not None and abs(actual_seconds - sample.duration_seconds) > 1.0:
            report["warnings"].append(
                f"sample {sample.sample_uuid} declares {sample.duration_seconds:.1f}s but "
                f"the file is {actual_seconds:.1f}s. The declared duration is what the "
                "real-time factor is computed against, so a wrong one makes the timing "
                "wrong."
            )
        if not sample.has_reference:
            report["warnings"].append(
                f"sample {sample.sample_uuid} has no readable reference transcript, so it "
                "contributes no accuracy number -- only timing."
            )
        else:
            report["with_reference"] += 1
        if sample.word_timestamp_reference_path is not None:
            if sample.word_timestamp_reference_path.is_file():
                report["with_word_timestamps"] += 1
            else:
                report["warnings"].append(
                    f"sample {sample.sample_uuid} declares a word-timestamp reference that "
                    "does not exist; word-timestamp error will be N/A."
                )
        # The audio path is deliberately not echoed: this report is quotable, and an
        # operator's private path is not something to paste into a ticket.
        report["samples"].append(
            {
                "sample_uuid": sample.sample_uuid,
                "audio_name": sample.audio_path.name,
                "sha256_verified": True,
                "declared_duration_seconds": sample.duration_seconds,
                "measured_duration_seconds": (
                    None if actual_seconds is None else round(actual_seconds, 2)
                ),
                "language": sample.language,
                "consent_status": sample.consent_status,
                "license_name": sample.license_name,
                "condition": sample.condition,
                "has_reference_transcript": sample.has_reference,
                "technical_term_count": len(sample.technical_terms),
            }
        )

    report["sample_count"] = len(samples)
    report["total_duration_seconds"] = round(
        sum(sample.duration_seconds for sample in samples), 1
    )
    report["conditions"] = conditions
    report["ok"] = True
    if report["with_reference"] == 0:
        report["warnings"].append(
            "no sample has a reference transcript, so this manifest can measure "
            "throughput but not accuracy. WER will be N/A."
        )
    if conditions.get("far-field", 0) == 0:
        report["warnings"].append(
            "no far-field sample. Far-field is the condition that decides whether the "
            "product works in a real meeting, and it has its own acceptance target."
        )
    _LOG.info(
        "asr.benchmark.manifest_validated",
        extra={
            "samples": report["sample_count"],
            "with_reference": report["with_reference"],
            "warnings": len(report["warnings"]),
        },
    )
    return report


def word_error_rate(reference: str, hypothesis: str) -> dict[str, Any]:
    """Levenshtein WER over normalised tokens, with the substitution breakdown."""
    ref = _normalise_for_wer(reference)
    hyp = _normalise_for_wer(hypothesis)
    if not ref:
        return {"wer": None, "detail": "reference is empty after normalisation"}

    previous = list(range(len(hyp) + 1))
    ops_prev: list[tuple[int, int, int]] = [(0, index, 0) for index in range(len(hyp) + 1)]
    for i, ref_token in enumerate(ref, start=1):
        current = [i]
        ops_cur: list[tuple[int, int, int]] = [(0, 0, i)]
        for j, hyp_token in enumerate(hyp, start=1):
            if ref_token == hyp_token:
                cost = previous[j - 1]
                ops = ops_prev[j - 1]
            else:
                sub = previous[j - 1] + 1
                ins = current[j - 1] + 1
                dele = previous[j] + 1
                cost = min(sub, ins, dele)
                if cost == sub:
                    s, ii, d = ops_prev[j - 1]
                    ops = (s + 1, ii, d)
                elif cost == ins:
                    s, ii, d = ops_cur[j - 1]
                    ops = (s, ii + 1, d)
                else:
                    s, ii, d = ops_prev[j]
                    ops = (s, ii, d + 1)
            current.append(cost)
            ops_cur.append(ops)
        previous, ops_prev = current, ops_cur

    distance = previous[-1]
    subs, ins, dels = ops_prev[-1]
    return {
        "wer": distance / len(ref),
        "reference_words": len(ref),
        "hypothesis_words": len(hyp),
        "substitutions": subs,
        "insertions": ins,
        "deletions": dels,
    }


def technical_term_recall(reference_terms: tuple[str, ...], hypothesis: str) -> dict[str, Any]:
    """How many declared technical terms survive in the hypothesis."""
    if not reference_terms:
        return {"recall": None, "detail": "no technical terms declared for this sample"}
    # Padded with spaces on both sides so a term only matches on word boundaries. A bare
    # substring test would credit "api" to a transcript containing "apik", which inflates
    # recall -- the one direction an accuracy metric must never be wrong in.
    haystack = f" {' '.join(_normalise_for_wer(hypothesis))} "
    found = 0
    missing: list[str] = []
    for term in reference_terms:
        needle = " ".join(_normalise_for_wer(term))
        if not needle:
            continue
        if f" {needle} " in haystack:
            found += 1
        else:
            missing.append(term)
    total = len([t for t in reference_terms if _normalise_for_wer(t)])
    return {
        "recall": (found / total) if total else None,
        "found": found,
        "total": total,
        "missing_count": len(missing),
    }


def timestamp_error_stats(
    gold: list[dict[str, Any]], words: list[dict[str, Any]]
) -> dict[str, Any]:
    """Median and P95 absolute start-time error, in milliseconds.

    Matched greedily on identical normalised text in order. A word that cannot be
    matched contributes nothing rather than an invented error.
    """
    if not gold:
        return {"median_ms": None, "p95_ms": None, "detail": "no gold timestamps supplied"}
    errors: list[float] = []
    cursor = 0
    for entry in gold:
        target = " ".join(_normalise_for_wer(str(entry.get("text", ""))))
        if not target:
            continue
        for index in range(cursor, len(words)):
            candidate = " ".join(_normalise_for_wer(str(words[index].get("text", ""))))
            if candidate == target:
                errors.append(
                    abs(float(words[index]["start"]) - float(entry["start"])) * 1000.0
                )
                cursor = index + 1
                break
    if not errors:
        return {"median_ms": None, "p95_ms": None, "detail": "no words could be matched"}
    ordered = sorted(errors)
    p95_index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return {
        "median_ms": round(statistics.median(ordered), 1),
        "p95_ms": round(ordered[p95_index], 1),
        "matched_words": len(errors),
    }


# ---------------------------------------------------------------------------
# Running one configuration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RunRecord:
    """One (model, threads, audio) measurement."""

    model_key: str
    model_name: str
    revision: str
    compute_type: str
    cpu_threads: int
    beam_size: int
    manifest_sha256: str
    network_attempts: tuple[str, ...]
    audio_kind: str
    audio_label: str
    audio_seconds: float
    load_seconds: float
    wall_seconds: float
    decode_seconds: float
    rtf: float | None
    peak_rss_bytes: int
    cpu_seconds: float
    peak_threads: int
    segments: int
    words: int
    vad_speech_seconds: float | None
    wer: float | None = None
    wer_detail: str | None = None
    term_recall: float | None = None
    timestamp_median_ms: float | None = None
    timestamp_p95_ms: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_key": self.model_key,
            "model_name": self.model_name,
            "revision": self.revision[:12],
            "compute_type": self.compute_type,
            "cpu_threads": self.cpu_threads,
            "beam_size": self.beam_size,
            "manifest_sha256": self.manifest_sha256,
            "network_attempts": list(self.network_attempts),
            "zero_network_egress": not self.network_attempts,
            "audio_kind": self.audio_kind,
            "audio_label": self.audio_label,
            "audio_seconds": round(self.audio_seconds, 2),
            "load_seconds": round(self.load_seconds, 3),
            "wall_seconds": round(self.wall_seconds, 3),
            "decode_seconds": round(self.decode_seconds, 3),
            "rtf": None if self.rtf is None else round(self.rtf, 4),
            "peak_rss_bytes": self.peak_rss_bytes,
            "peak_rss_mib": round(self.peak_rss_bytes / (1 << 20), 1),
            "cpu_seconds": round(self.cpu_seconds, 2),
            "peak_threads": self.peak_threads,
            "segments": self.segments,
            "words": self.words,
            "vad_speech_seconds": (
                None if self.vad_speech_seconds is None else round(self.vad_speech_seconds, 2)
            ),
            "wer": self.wer,
            "wer_detail": self.wer_detail,
            "technical_term_recall": self.term_recall,
            "timestamp_median_ms": self.timestamp_median_ms,
            "timestamp_p95_ms": self.timestamp_p95_ms,
            "error": self.error,
        }


def _run_one(
    *,
    models_dir: Path,
    model_key: str,
    role: str,
    cpu_threads: int,
    audio_path: Path,
    audio_seconds: float,
    audio_kind: str,
    audio_label: str,
    beam_size: int,
    language: str,
    sample: CorpusSample | None,
    vad_speech_seconds: float | None,
    progress: Callable[[str], None],
) -> RunRecord:
    from mom_igd.asr.provision import MODEL_CATALOGUE
    from mom_igd.asr.worker import WorkerError, run_in_worker

    spec = MODEL_CATALOGUE[model_key]
    progress(f"{model_key} threads={cpu_threads} on {audio_label}")
    started = time.perf_counter()
    try:
        outcome = run_in_worker(
            "transcribe",
            {
                "models_dir": str(models_dir),
                "role": role,
                "audio_path": str(audio_path),
                "regions": [],
                "language": language,
                "cpu_threads": cpu_threads,
                "beam_size": beam_size,
                "word_timestamps": True,
                "asr_pass": 1,
                # Measure egress rather than asserting it.
                "record_network_attempts": True,
            },
            timeout_seconds=60 * 60,
        )
    except WorkerError as exc:
        return RunRecord(
            model_key=model_key,
            model_name=spec.model_name,
            revision="",
            compute_type="int8",
            cpu_threads=cpu_threads,
            beam_size=beam_size,
            manifest_sha256="",
            network_attempts=(),
            audio_kind=audio_kind,
            audio_label=audio_label,
            audio_seconds=audio_seconds,
            load_seconds=0.0,
            wall_seconds=time.perf_counter() - started,
            decode_seconds=0.0,
            rtf=None,
            peak_rss_bytes=0,
            cpu_seconds=0.0,
            peak_threads=0,
            segments=0,
            words=0,
            vad_speech_seconds=vad_speech_seconds,
            error=str(exc),
        )

    payload = outcome.payload
    if not outcome.ok:
        return RunRecord(
            model_key=model_key,
            model_name=spec.model_name,
            revision="",
            compute_type="int8",
            cpu_threads=cpu_threads,
            beam_size=beam_size,
            manifest_sha256="",
            network_attempts=(),
            audio_kind=audio_kind,
            audio_label=audio_label,
            audio_seconds=audio_seconds,
            load_seconds=0.0,
            wall_seconds=outcome.wall_seconds,
            decode_seconds=0.0,
            rtf=None,
            peak_rss_bytes=outcome.peak_rss_bytes,
            cpu_seconds=outcome.cpu_seconds,
            peak_threads=outcome.peak_threads,
            segments=0,
            words=0,
            vad_speech_seconds=vad_speech_seconds,
            error=outcome.error,
        )

    segments = payload.get("segments") or []
    words = sum(len(s.get("words") or ()) for s in segments)
    model_info = payload.get("model") or {}
    decode = float(payload.get("processing_seconds") or 0.0)
    load = float(payload.get("load_seconds") or 0.0)
    # RTF is measured against wall-clock, which is what an operator waits for -- it
    # includes model load. Decode-only time is recorded separately so the two are
    # never confused.
    rtf = (outcome.wall_seconds / audio_seconds) if audio_seconds > 0 else None

    record = RunRecord(
        model_key=model_key,
        model_name=str(model_info.get("model_name") or spec.model_name),
        revision=str(model_info.get("revision") or ""),
        compute_type=str(model_info.get("compute_type") or "int8"),
        cpu_threads=cpu_threads,
        beam_size=beam_size,
        manifest_sha256=str(model_info.get("manifest_sha256") or ""),
        # The worker runs with the offline flags set and addresses a local directory; any
        # attempt it made would be a defect, so the field is recorded rather than assumed.
        network_attempts=tuple(payload.get("network_attempts") or ()),
        audio_kind=audio_kind,
        audio_label=audio_label,
        audio_seconds=audio_seconds,
        load_seconds=load,
        wall_seconds=outcome.wall_seconds,
        decode_seconds=decode,
        rtf=rtf,
        peak_rss_bytes=outcome.peak_rss_bytes,
        cpu_seconds=outcome.cpu_seconds,
        peak_threads=outcome.peak_threads,
        segments=len(segments),
        words=words,
        vad_speech_seconds=vad_speech_seconds,
    )

    if sample is not None and sample.has_reference:
        hypothesis = " ".join(str(s.get("text", "")) for s in segments)
        reference = sample.reference_transcript_path.read_text(  # type: ignore[union-attr]
            encoding="utf-8"
        )
        wer = word_error_rate(reference, hypothesis)
        record.wer = None if wer.get("wer") is None else round(float(wer["wer"]), 4)
        record.wer_detail = (
            f"S{wer.get('substitutions')}/I{wer.get('insertions')}/D{wer.get('deletions')} "
            f"over {wer.get('reference_words')} reference words"
        )
        recall = technical_term_recall(sample.technical_terms, hypothesis)
        record.term_recall = (
            None if recall.get("recall") is None else round(float(recall["recall"]), 4)
        )
        if sample.word_timestamp_reference_path and sample.word_timestamp_reference_path.is_file():
            import json

            gold = json.loads(
                sample.word_timestamp_reference_path.read_text(encoding="utf-8")
            )
            flat_words = [w for s in segments for w in (s.get("words") or ())]
            stats = timestamp_error_stats(list(gold.get("words") or []), flat_words)
            record.timestamp_median_ms = stats.get("median_ms")
            record.timestamp_p95_ms = stats.get("p95_ms")
    else:
        record.wer_detail = (
            "N/A -- no reference transcript. WER is never derived from the model's own "
            "output."
        )
    return record


def _format_table(records: list[RunRecord]) -> str:
    header = (
        f"{'model':30} {'thr':>3} {'beam':>4} {'audio':>8} {'load':>6} {'wall':>7} "
        f"{'RTF':>6} {'peakRSS':>9} {'cpu_s':>7} {'thr#':>4} {'segs':>5} {'words':>6} "
        f"{'WER':>7}"
    )
    lines = [header, "-" * len(header)]
    for record in records:
        wer = "N/A" if record.wer is None else f"{record.wer * 100:.1f}%"
        rtf = "N/A" if record.rtf is None else f"{record.rtf:.3f}"
        lines.append(
            f"{record.model_name[:30]:30} {record.cpu_threads:>3} "
            f"{record.beam_size:>4} "
            f"{record.audio_seconds:>7.1f}s {record.load_seconds:>5.2f}s "
            f"{record.wall_seconds:>6.2f}s {rtf:>6} "
            f"{record.peak_rss_bytes / (1 << 20):>8.0f}M {record.cpu_seconds:>6.1f}s "
            f"{record.peak_threads:>4} {record.segments:>5} {record.words:>6} {wer:>7}"
        )
        if record.error:
            lines.append(f"    ERROR: {record.error}")
    return "\n".join(lines)


def run_benchmark(
    config: Any,
    paths: Any,
    *,
    corpus_manifest: str | None = None,
    thread_counts: list[int] | None = None,
    model_keys: list[str] | None = None,
    synthetic_seconds: float | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the Phase 4A benchmark and return a machine-readable report.

    Contains no transcript text and no personal data: only counts, durations and
    aggregate accuracy figures, so the artefact is safe to commit.
    """
    from mom_igd.asr.provision import MODEL_CATALOGUE, promoted_models
    from mom_igd.asr.smoke import generate_speech_like_wav
    from mom_igd.asr.vad import detect_speech_regions
    from mom_igd.asr.worker import temperature_evidence, worker_environment_summary

    say = progress or (lambda _message: None)
    models_dir = Path(paths.models_dir)

    available = {
        entry["model_name"] for entry in promoted_models(models_dir) if entry.get("ok")
    }
    keys = model_keys or list(MODEL_CATALOGUE)
    runnable: list[str] = []
    skipped: list[str] = []
    for key in keys:
        spec = MODEL_CATALOGUE.get(key)
        if spec is None:
            raise BenchmarkError(f"unknown model key {key!r}")
        (runnable if spec.model_name in available else skipped).append(key)
    if not runnable:
        raise BenchmarkError(
            "no benchmarkable model is provisioned. Run "
            "`python -m mom_igd asr provision all` first; the benchmark refuses to "
            "report numbers produced by a stand-in."
        )

    threads = [t for t in (thread_counts or list(DEFAULT_THREAD_SWEEP)) if t > 0]
    if not threads:
        raise BenchmarkError("no positive thread count to sweep")

    samples = ()
    if corpus_manifest:
        samples = load_corpus_manifest(corpus_manifest)
        say(f"corpus: {len(samples)} sample(s) from the manifest")

    notes: list[str] = []
    records: list[RunRecord] = []
    work_dir = Path(paths.temp_dir) / "asr-bench"
    work_dir.mkdir(parents=True, exist_ok=True)
    synthetic_path: Path | None = None

    try:
        if samples:
            audio_jobs = [
                (
                    sample.audio_path,
                    sample.duration_seconds,
                    "corpus",
                    f"{sample.sample_uuid[:8]}/{sample.condition}",
                    sample,
                )
                for sample in samples
            ]
        else:
            seconds = float(synthetic_seconds or 60.0)
            # Per-process filename, not a fixed one. Two concurrent benchmark runs used
            # to share `bench-synthetic-16k-mono.wav`, so whichever finished first
            # deleted the audio the other was still decoding -- observed as four runs
            # failing with "the working copy to transcribe does not exist". The content
            # is still deterministic; only the filename varies.
            synthetic_path = work_dir / f"bench-synthetic-16k-mono-{os.getpid()}.wav"
            say(f"generating {seconds:.0f}s of deterministic synthetic audio")
            actual = generate_speech_like_wav(synthetic_path, seconds)
            audio_jobs = [(synthetic_path, actual, "synthetic", f"synthetic-{actual:.0f}s", None)]
            notes.append(
                "No evaluation corpus was supplied, so timing was measured on "
                "deterministic synthetic audio. The decoder does real work on it, so "
                "these are valid measurements of engine throughput on this machine -- "
                "but they are NOT a substitute for real speech: segment density and the "
                "temperature-fallback path differ. WER, technical-term accuracy and "
                "word-timestamp error are reported as N/A because there is no reference "
                "transcript, and they are never derived from the model's own output."
            )

        for audio_path, audio_seconds, audio_kind, audio_label, sample in audio_jobs:
            vad_speech: float | None = None
            try:
                vad_result = detect_speech_regions(audio_path)
                vad_speech = vad_result.total_speech_seconds
                say(
                    f"VAD on {audio_label}: {len(vad_result.regions)} region(s), "
                    f"{vad_speech:.1f}s speech of {audio_seconds:.1f}s"
                )
            except Exception as exc:  # noqa: BLE001 - a VAD failure must not hide timings
                notes.append(f"VAD could not run on {audio_label}: {type(exc).__name__}")

            for key in runnable:
                spec = MODEL_CATALOGUE[key]
                for count in threads:
                    records.append(
                        _run_one(
                            models_dir=models_dir,
                            model_key=key,
                            role=spec.role,
                            cpu_threads=count,
                            audio_path=Path(audio_path),
                            audio_seconds=float(audio_seconds),
                            audio_kind=audio_kind,
                            audio_label=audio_label,
                            beam_size=1 if spec.role == "pass1" else 5,
                            language=(sample.language if sample else "id"),
                            sample=sample,
                            vad_speech_seconds=vad_speech,
                            progress=say,
                        )
                    )
    finally:
        if synthetic_path is not None:
            try:
                synthetic_path.unlink(missing_ok=True)
            except OSError:
                pass

    if skipped:
        notes.append(
            f"not benchmarked because they are not provisioned: {sorted(skipped)}"
        )

    good = [r for r in records if r.error is None and r.rtf is not None]
    best_by_model: dict[str, dict[str, Any]] = {}
    for key in runnable:
        candidates = [r for r in good if r.model_key == key]
        if not candidates:
            continue
        winner = min(candidates, key=lambda r: (r.rtf if r.rtf is not None else 9e9))
        best_by_model[key] = {
            "cpu_threads": winner.cpu_threads,
            "rtf": round(winner.rtf, 4) if winner.rtf else None,
            "peak_rss_mib": round(winner.peak_rss_bytes / (1 << 20), 1),
            "load_seconds": round(winner.load_seconds, 3),
        }

    peak_rss = max((r.peak_rss_bytes for r in records), default=0)
    gate = {
        "peak_rss_bytes": peak_rss,
        "peak_rss_mib": round(peak_rss / (1 << 20), 1),
        "peak_rss_within_target": bool(peak_rss and peak_rss < TARGETS["peak_rss_bytes"]),
        "best_rtf_by_model": best_by_model,
        "wer_measured": any(r.wer is not None for r in records),
        "timestamp_error_measured": any(r.timestamp_median_ms is not None for r in records),
        "errors": [r.error for r in records if r.error],
        "zero_network_egress": all(not r.network_attempts for r in records),
    }

    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "phase": "4A-ASR",
        "machine": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "python": platform.python_version(),
            **worker_environment_summary(),
        },
        "thermal": temperature_evidence(),
        "targets": {
            "peak_rss_bytes": TARGETS["peak_rss_bytes"],
            "total_rtf": TARGETS["total_rtf"],
            "clean_wer": TARGETS["clean_wer"],
            "far_field_wer": TARGETS["far_field_wer"],
            "median_timestamp_error_ms": TARGETS["median_timestamp_error_ms"],
            "p95_timestamp_error_ms": TARGETS["p95_timestamp_error_ms"],
        },
        "thread_sweep": threads,
        "models_benchmarked": runnable,
        "runs": [r.to_dict() for r in records],
        "gate": gate,
        "notes": notes,
        "table": _format_table(records),
    }
    _LOG.info(
        "asr.benchmark",
        extra={"runs": len(records), "peak_rss_mib": round(peak_rss / (1 << 20), 1)},
    )
    return report
