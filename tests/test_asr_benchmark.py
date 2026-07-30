"""The accuracy metrics, and the corpus loader that decides what may be measured.

These are the functions that will eventually turn a reference transcript into a number
somebody quotes in a gate decision, so they are tested against hand-computed edit
distances rather than against themselves. A WER implementation that is wrong in the
flattering direction is worse than no WER at all.

Nothing here loads a model or runs a decode. `run_benchmark` needs both and is exercised
by `asr bench` on the real device; what is tested here is everything around it that can
be tested honestly offline.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from mom_igd.asr.benchmark import (
    DEFAULT_THREAD_SWEEP,
    TARGETS,
    BenchmarkError,
    CorpusSample,
    RunRecord,
    _format_table,
    _normalise_for_wer,
    load_corpus_manifest,
    technical_term_recall,
    timestamp_error_stats,
    validate_corpus_manifest,
    word_error_rate,
)

BENCH = Path(__file__).resolve().parent.parent / "mom_igd" / "asr" / "benchmark.py"


# ===========================================================================
# Normalisation
# ===========================================================================


def test_normalisation_lowercases_and_drops_punctuation() -> None:
    assert _normalise_for_wer("Rapat, Selasa.") == ["rapat", "selasa"]


def test_normalisation_keeps_the_apostrophe_inside_a_word() -> None:
    assert _normalise_for_wer("don't") == ["don't"]


def test_normalisation_collapses_whitespace_and_newlines() -> None:
    assert _normalise_for_wer("  a \n\t b  ") == ["a", "b"]


def test_normalisation_folds_compatibility_forms() -> None:
    """NFKC only. A full-width character and its ASCII form are the same word."""
    assert _normalise_for_wer("ＡＰＩ") == ["api"]


def test_normalisation_does_not_expand_numbers_or_fold_synonyms() -> None:
    """Aggressive normalisation is the easiest way to make WER look better than it is."""
    assert _normalise_for_wer("3") == ["3"]
    assert _normalise_for_wer("tiga") == ["tiga"]
    assert _normalise_for_wer("3") != _normalise_for_wer("tiga")


def test_normalisation_does_not_stem() -> None:
    assert _normalise_for_wer("laporan") != _normalise_for_wer("lapor")


def test_normalisation_of_only_punctuation_is_empty() -> None:
    assert _normalise_for_wer("... --- ???") == []


# ===========================================================================
# Word error rate, against hand-computed distances
# ===========================================================================


def test_an_identical_transcript_scores_zero() -> None:
    result = word_error_rate("rapat mingguan tim backend", "Rapat mingguan tim backend.")
    assert result["wer"] == 0.0
    assert (result["substitutions"], result["insertions"], result["deletions"]) == (0, 0, 0)


def test_one_substitution_in_four_words_is_one_quarter() -> None:
    result = word_error_rate("a b c d", "a x c d")
    assert result["wer"] == pytest.approx(0.25)
    assert result["substitutions"] == 1
    assert result["insertions"] == 0
    assert result["deletions"] == 0


def test_one_deletion_is_counted_as_a_deletion() -> None:
    result = word_error_rate("a b c d", "a c d")
    assert result["wer"] == pytest.approx(0.25)
    assert result["deletions"] == 1
    assert result["substitutions"] == 0


def test_one_insertion_is_counted_as_an_insertion() -> None:
    result = word_error_rate("a b c", "a b x c")
    assert result["wer"] == pytest.approx(1 / 3)
    assert result["insertions"] == 1


def test_the_operation_counts_sum_to_the_edit_distance() -> None:
    """Otherwise the breakdown and the headline number describe different alignments."""
    result = word_error_rate("satu dua tiga empat lima", "satu tiga tiga empat enam extra")
    total = result["substitutions"] + result["insertions"] + result["deletions"]
    assert result["wer"] == pytest.approx(total / result["reference_words"])


def test_a_completely_wrong_transcript_scores_one() -> None:
    result = word_error_rate("a b c", "x y z")
    assert result["wer"] == pytest.approx(1.0)


def test_wer_can_exceed_one_and_is_not_clamped() -> None:
    """Clamping would hide a hallucinating decoder, which is the case that matters most."""
    result = word_error_rate("a", "x y z w")
    assert result["wer"] > 1.0


def test_an_empty_hypothesis_deletes_every_word() -> None:
    result = word_error_rate("a b c", "")
    assert result["wer"] == pytest.approx(1.0)
    assert result["deletions"] == 3


def test_an_empty_reference_yields_no_number_rather_than_zero() -> None:
    """Zero would read as a perfect score; there is simply nothing to divide by."""
    result = word_error_rate("", "anything at all")
    assert result["wer"] is None
    assert "empty" in result["detail"]


def test_a_punctuation_only_reference_is_also_treated_as_empty() -> None:
    assert word_error_rate("...", "anything")["wer"] is None


def test_wer_is_asymmetric_in_the_way_edit_distance_is() -> None:
    forward = word_error_rate("a b c d", "a b")
    backward = word_error_rate("a b", "a b c d")
    assert forward["wer"] == pytest.approx(0.5)
    assert backward["wer"] == pytest.approx(1.0)


def test_word_counts_are_reported_so_a_number_can_be_weighted() -> None:
    result = word_error_rate("a b c", "a b")
    assert result["reference_words"] == 3
    assert result["hypothesis_words"] == 2


def test_a_long_reference_does_not_blow_up() -> None:
    """The DP table is O(ref x hyp); a meeting transcript is thousands of words."""
    reference = " ".join(f"kata{index}" for index in range(400))
    hypothesis = " ".join(f"kata{index}" for index in range(400) if index % 40)
    result = word_error_rate(reference, hypothesis)
    assert result["deletions"] == 10
    assert result["wer"] == pytest.approx(10 / 400)


# ===========================================================================
# Technical term recall
# ===========================================================================


def test_term_recall_finds_a_single_word_term() -> None:
    result = technical_term_recall(("backend", "sprint"), "kita bahas backend dan sprint")
    assert result["recall"] == pytest.approx(1.0)
    assert result["total"] == 2


def test_term_recall_finds_a_multi_word_term_only_as_a_phrase() -> None:
    result = technical_term_recall(("load balancer",), "kita pakai load balancer baru")
    assert result["recall"] == pytest.approx(1.0)
    scattered = technical_term_recall(("load balancer",), "load rusak dan balancer hilang")
    assert scattered["recall"] == pytest.approx(0.0)


def test_term_recall_reports_a_partial_score() -> None:
    result = technical_term_recall(("backend", "sprint", "kanban"), "backend saja")
    assert result["recall"] == pytest.approx(1 / 3)
    assert result["found"] == 1
    assert result["missing_count"] == 2


def test_term_recall_is_case_and_punctuation_insensitive() -> None:
    result = technical_term_recall(("API",), "dokumentasi api, sudah siap")
    assert result["recall"] == pytest.approx(1.0)


def test_no_declared_terms_means_no_number_rather_than_zero() -> None:
    result = technical_term_recall((), "apapun")
    assert result["recall"] is None
    assert "no technical terms declared" in result["detail"]


def test_a_term_that_normalises_to_nothing_is_not_counted_against_the_score() -> None:
    """Otherwise a stray '---' in a manifest would cap recall below 100 % forever."""
    result = technical_term_recall(("backend", "---"), "backend")
    assert result["recall"] == pytest.approx(1.0)
    assert result["total"] == 1


def test_term_recall_does_not_credit_a_substring_of_a_longer_word() -> None:
    """'api' inside 'apik' is not the term. Substring matching would inflate recall."""
    result = technical_term_recall(("api",), "kerjanya apik sekali")
    assert result["recall"] == pytest.approx(0.0)


# ===========================================================================
# Word-timestamp error
# ===========================================================================


def _word(text: str, start: float) -> dict[str, object]:
    return {"text": text, "start": start, "end": start + 0.3}


def test_timestamp_error_is_the_absolute_start_difference_in_milliseconds() -> None:
    gold = [_word("satu", 1.0), _word("dua", 2.0)]
    words = [_word("satu", 1.05), _word("dua", 1.90)]
    stats = timestamp_error_stats(gold, words)
    assert stats["matched_words"] == 2
    assert stats["median_ms"] == pytest.approx(75.0, abs=0.1)


def test_a_perfect_alignment_has_zero_error() -> None:
    gold = [_word("satu", 1.0), _word("dua", 2.0), _word("tiga", 3.0)]
    stats = timestamp_error_stats(gold, list(gold))
    assert stats["median_ms"] == 0.0
    assert stats["p95_ms"] == 0.0


def test_an_unmatched_gold_word_contributes_nothing_rather_than_an_invented_error() -> None:
    gold = [_word("satu", 1.0), _word("hilang", 2.0), _word("tiga", 3.0)]
    words = [_word("satu", 1.0), _word("tiga", 3.0)]
    stats = timestamp_error_stats(gold, words)
    assert stats["matched_words"] == 2
    assert stats["median_ms"] == 0.0


def test_matching_is_in_order_so_a_repeated_word_is_not_matched_twice() -> None:
    gold = [_word("ya", 1.0), _word("ya", 5.0)]
    words = [_word("ya", 1.0), _word("ya", 5.2)]
    stats = timestamp_error_stats(gold, words)
    assert stats["matched_words"] == 2
    assert stats["p95_ms"] == pytest.approx(200.0, abs=0.1)


def test_no_gold_timestamps_means_no_number() -> None:
    stats = timestamp_error_stats([], [_word("satu", 1.0)])
    assert stats["median_ms"] is None
    assert "no gold timestamps" in stats["detail"]


def test_nothing_matchable_means_no_number_rather_than_zero_error() -> None:
    stats = timestamp_error_stats([_word("satu", 1.0)], [_word("lain", 1.0)])
    assert stats["median_ms"] is None
    assert "no words could be matched" in stats["detail"]


def test_p95_reflects_a_tail_the_median_hides() -> None:
    """Ten percent of words a second late must show in P95 and not in the median."""
    gold = [_word(f"w{index}", float(index)) for index in range(100)]
    words = [
        _word(f"w{index}", float(index) + (1.0 if index >= 90 else 0.0))
        for index in range(100)
    ]
    stats = timestamp_error_stats(gold, words)
    assert stats["median_ms"] == 0.0
    assert stats["p95_ms"] == pytest.approx(1000.0, abs=0.1)


def test_p95_uses_nearest_rank_so_a_single_outlier_in_a_hundred_does_not_set_it() -> None:
    """Recording the convention, because a gate is read against it.

    Nearest-rank on a sorted list: index ``round(0.95 x (n-1))``. With one bad word in a
    hundred, that index still lands in the clean part, so P95 is 0 -- by definition, not
    by accident. The maximum is deliberately not reported as P95.
    """
    gold = [_word(f"w{index}", float(index)) for index in range(100)]
    words = [
        _word(f"w{index}", float(index) + (5.0 if index == 99 else 0.0))
        for index in range(100)
    ]
    stats = timestamp_error_stats(gold, words)
    assert stats["p95_ms"] == 0.0
    assert stats["matched_words"] == 100


def test_a_gold_entry_with_empty_text_is_skipped() -> None:
    gold = [_word("", 1.0), _word("satu", 2.0)]
    stats = timestamp_error_stats(gold, [_word("satu", 2.0)])
    assert stats["matched_words"] == 1


# ===========================================================================
# The corpus manifest gate
# ===========================================================================


def _write_sample(directory: Path, name: str, payload: bytes) -> tuple[Path, str]:
    path = directory / name
    path.write_bytes(payload)
    return path, hashlib.sha256(payload).hexdigest()


def _manifest(directory: Path, samples: list[dict[str, object]]) -> Path:
    path = directory / "corpus.json"
    path.write_text(json.dumps({"samples": samples}), encoding="utf-8")
    return path


def test_a_well_formed_manifest_loads(tmp_path: Path) -> None:
    audio, digest = _write_sample(tmp_path, "a.wav", b"synthetic-bytes")
    manifest = _manifest(
        tmp_path,
        [
            {
                "sample_uuid": "11111111-1111-4111-8111-111111111111",
                "audio_path": str(audio),
                "sha256": digest,
                "duration_seconds": 12.5,
                "language": "id",
                "consent_status": "synthetic",
                "license_name": "generated",
                "technical_terms": ["backend"],
                "condition": "clean",
            }
        ],
    )
    samples = load_corpus_manifest(manifest)
    assert len(samples) == 1
    assert samples[0].condition == "clean"
    assert samples[0].technical_terms == ("backend",)
    assert samples[0].has_reference is False


def test_a_missing_manifest_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkError, match="does not exist"):
        load_corpus_manifest(tmp_path / "nope.json")


def test_a_manifest_that_is_not_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "corpus.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="not valid JSON"):
        load_corpus_manifest(path)


@pytest.mark.parametrize("payload", ['{"samples": []}', '{"samples": {}}', "{}"])
def test_a_manifest_with_no_samples_is_refused(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "corpus.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(BenchmarkError, match="lists no samples"):
        load_corpus_manifest(path)


def test_a_sample_missing_a_required_key_is_refused(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, [{"sample_uuid": "x"}])
    with pytest.raises(BenchmarkError, match="malformed sample entry"):
        load_corpus_manifest(manifest)


def test_a_sample_with_an_unparseable_duration_is_refused(tmp_path: Path) -> None:
    audio, digest = _write_sample(tmp_path, "a.wav", b"x")
    manifest = _manifest(
        tmp_path,
        [
            {
                "sample_uuid": "x",
                "audio_path": str(audio),
                "sha256": digest,
                "duration_seconds": "twelve",
                "consent_status": "synthetic",
            }
        ],
    )
    with pytest.raises(BenchmarkError, match="malformed sample entry"):
        load_corpus_manifest(manifest)


def test_a_missing_audio_file_is_an_error_not_a_skipped_sample(tmp_path: Path) -> None:
    """Silently shrinking the corpus is how a benchmark flatters itself."""
    manifest = _manifest(
        tmp_path,
        [
            {
                "sample_uuid": "x",
                "audio_path": str(tmp_path / "absent.wav"),
                "sha256": "00" * 32,
                "duration_seconds": 1.0,
                "consent_status": "synthetic",
            }
        ],
    )
    with pytest.raises(BenchmarkError, match="must not silently skip"):
        load_corpus_manifest(manifest)


def test_a_checksum_mismatch_is_refused(tmp_path: Path) -> None:
    """A run against different bytes than the manifest describes is not reproducible."""
    audio, _ = _write_sample(tmp_path, "a.wav", b"real-bytes")
    manifest = _manifest(
        tmp_path,
        [
            {
                "sample_uuid": "x",
                "audio_path": str(audio),
                "sha256": hashlib.sha256(b"different-bytes").hexdigest(),
                "duration_seconds": 1.0,
                "consent_status": "synthetic",
            }
        ],
    )
    with pytest.raises(BenchmarkError, match="checksum mismatch"):
        load_corpus_manifest(manifest)


def test_an_uppercase_checksum_in_the_manifest_still_matches(tmp_path: Path) -> None:
    audio, digest = _write_sample(tmp_path, "a.wav", b"bytes")
    manifest = _manifest(
        tmp_path,
        [
            {
                "sample_uuid": "x",
                "audio_path": str(audio),
                "sha256": digest.upper(),
                "duration_seconds": 1.0,
                "consent_status": "synthetic",
            }
        ],
    )
    assert len(load_corpus_manifest(manifest)) == 1


@pytest.mark.parametrize(
    "consent", ["unknown", "", "assumed", "recorded", "granted-later", "no"]
)
def test_audio_without_recorded_consent_is_refused(tmp_path: Path, consent: str) -> None:
    """Benchmarking somebody's voice is processing biometric data."""
    audio, digest = _write_sample(tmp_path, "a.wav", b"voice")
    manifest = _manifest(
        tmp_path,
        [
            {
                "sample_uuid": "x",
                "audio_path": str(audio),
                "sha256": digest,
                "duration_seconds": 1.0,
                "consent_status": consent,
            }
        ],
    )
    with pytest.raises(BenchmarkError, match="requires recorded"):
        load_corpus_manifest(manifest)


@pytest.mark.parametrize("consent", ["granted", "public-licensed", "synthetic", "GRANTED"])
def test_the_three_acceptable_consent_states(tmp_path: Path, consent: str) -> None:
    audio, digest = _write_sample(tmp_path, "a.wav", b"voice")
    manifest = _manifest(
        tmp_path,
        [
            {
                "sample_uuid": "x",
                "audio_path": str(audio),
                "sha256": digest,
                "duration_seconds": 1.0,
                "consent_status": consent,
            }
        ],
    )
    assert len(load_corpus_manifest(manifest)) == 1


def test_a_consent_default_is_never_permissive(tmp_path: Path) -> None:
    """Omitting the field must not be read as consent."""
    audio, digest = _write_sample(tmp_path, "a.wav", b"voice")
    manifest = _manifest(
        tmp_path,
        [
            {
                "sample_uuid": "x",
                "audio_path": str(audio),
                "sha256": digest,
                "duration_seconds": 1.0,
            }
        ],
    )
    with pytest.raises(BenchmarkError):
        load_corpus_manifest(manifest)


def test_the_second_of_two_samples_is_also_checked(tmp_path: Path) -> None:
    """A loop that validates only the first entry would pass every test above."""
    good, good_digest = _write_sample(tmp_path, "a.wav", b"ok")
    bad, _ = _write_sample(tmp_path, "b.wav", b"tampered")
    manifest = _manifest(
        tmp_path,
        [
            {
                "sample_uuid": "a",
                "audio_path": str(good),
                "sha256": good_digest,
                "duration_seconds": 1.0,
                "consent_status": "synthetic",
            },
            {
                "sample_uuid": "b",
                "audio_path": str(bad),
                "sha256": "11" * 32,
                "duration_seconds": 1.0,
                "consent_status": "synthetic",
            },
        ],
    )
    with pytest.raises(BenchmarkError, match="checksum mismatch"):
        load_corpus_manifest(manifest)


def test_a_reference_transcript_is_detected_when_present(tmp_path: Path) -> None:
    audio, digest = _write_sample(tmp_path, "a.wav", b"ok")
    reference = tmp_path / "a.txt"
    reference.write_text("rapat mingguan", encoding="utf-8")
    manifest = _manifest(
        tmp_path,
        [
            {
                "sample_uuid": "a",
                "audio_path": str(audio),
                "sha256": digest,
                "duration_seconds": 1.0,
                "consent_status": "synthetic",
                "reference_transcript_path": str(reference),
            }
        ],
    )
    sample = load_corpus_manifest(manifest)[0]
    assert sample.has_reference is True


def test_a_declared_reference_that_is_absent_reports_no_reference(tmp_path: Path) -> None:
    """`has_reference` is about the file, not about the manifest's claim."""
    audio, digest = _write_sample(tmp_path, "a.wav", b"ok")
    manifest = _manifest(
        tmp_path,
        [
            {
                "sample_uuid": "a",
                "audio_path": str(audio),
                "sha256": digest,
                "duration_seconds": 1.0,
                "consent_status": "synthetic",
                "reference_transcript_path": str(tmp_path / "absent.txt"),
            }
        ],
    )
    assert load_corpus_manifest(manifest)[0].has_reference is False


def test_a_corpus_sample_is_frozen() -> None:
    sample = CorpusSample(
        sample_uuid="a",
        audio_path=Path("a.wav"),
        sha256="00" * 32,
        duration_seconds=1.0,
        language="id",
        reference_transcript_path=None,
        consent_status="synthetic",
        license_name="generated",
    )
    with pytest.raises(Exception):
        sample.sha256 = "ff" * 32  # type: ignore[misc]


# ===========================================================================
# The report
# ===========================================================================


def _record(**overrides: object) -> RunRecord:
    base: dict[str, object] = {
        "model_key": "asr-pass1",
        "model_name": "faster-whisper-small",
        "revision": "536b0662742c1234",
        "compute_type": "int8",
        "cpu_threads": 8,
        "beam_size": 1,
        "manifest_sha256": "ab" * 32,
        "network_attempts": (),
        "audio_kind": "synthetic",
        "audio_label": "tone-60s",
        "audio_seconds": 60.0,
        "load_seconds": 1.11,
        "wall_seconds": 10.42,
        "decode_seconds": 9.31,
        "rtf": 0.174,
        "peak_rss_bytes": 547 * (1 << 20),
        "cpu_seconds": 66.1,
        "peak_threads": 35,
        "segments": 2,
        "words": 112,
        "vad_speech_seconds": None,
    }
    base.update(overrides)
    return RunRecord(**base)  # type: ignore[arg-type]


def test_an_unmeasured_accuracy_prints_as_not_available() -> None:
    """It must never print as 0.0 %, which would read as a perfect score."""
    table = _format_table([_record()])
    assert "N/A" in table
    assert "0.0%" not in table


def test_a_measured_accuracy_prints_as_a_percentage() -> None:
    table = _format_table([_record(wer=0.213)])
    assert "21.3%" in table


def test_a_failed_run_prints_its_error_rather_than_vanishing() -> None:
    table = _format_table([_record(error="MODEL_UNAVAILABLE", rtf=None)])
    assert "ERROR: MODEL_UNAVAILABLE" in table
    assert table.count("\n") >= 3


def test_a_long_model_name_is_truncated_but_the_row_still_aligns() -> None:
    table = _format_table([_record(model_name="x" * 80)])
    rows = table.splitlines()
    assert len(rows[0]) == len(rows[1])
    assert len(rows[2]) <= len(rows[0]) + 2


def test_an_empty_run_list_still_produces_a_header() -> None:
    assert _format_table([]).splitlines()[0].startswith("model")


def test_the_record_reports_zero_egress_as_a_derived_fact() -> None:
    """`zero_network_egress` must follow the recorded attempts, not be set by hand."""
    assert _record().to_dict()["zero_network_egress"] is True
    assert _record(network_attempts=("dns:huggingface.co",)).to_dict()[
        "zero_network_egress"
    ] is False


def test_the_record_truncates_the_revision_but_keeps_the_full_manifest_digest() -> None:
    payload = _record().to_dict()
    assert payload["revision"] == "536b0662742c"
    assert len(payload["manifest_sha256"]) == 64


def test_the_serialised_record_carries_no_transcript_text() -> None:
    """A benchmark JSON is committed. Decoded speech must not be in it."""
    payload = _record().to_dict()
    for key in payload:
        assert "text" not in key
        assert "transcript" not in key
    assert "segments" in payload and isinstance(payload["segments"], int)


# ===========================================================================
# Targets and structure
# ===========================================================================


def test_the_targets_match_the_documented_gate() -> None:
    assert TARGETS["peak_rss_bytes"] == 2.5 * (1 << 30)
    assert TARGETS["total_rtf"] == 1.0
    assert TARGETS["clean_wer"] == 0.25
    assert TARGETS["far_field_wer"] == 0.35
    assert TARGETS["median_timestamp_error_ms"] == 200.0
    assert TARGETS["p95_timestamp_error_ms"] == 500.0


def test_the_thread_sweep_covers_both_sides_of_the_core_split() -> None:
    """The i7-1260P has 4 P-cores; a sweep that stopped at 4 could not find the knee."""
    assert min(DEFAULT_THREAD_SWEEP) <= 4
    assert max(DEFAULT_THREAD_SWEEP) >= 12
    assert list(DEFAULT_THREAD_SWEEP) == sorted(DEFAULT_THREAD_SWEEP)


def test_accuracy_is_never_derived_from_the_models_own_output() -> None:
    """Comparing a model against its own transcript measures self-consistency.

    Asserted structurally: `word_error_rate` may only be called with a reference read
    from the corpus, never with a value taken from a transcription result.
    """
    tree = ast.parse(BENCH.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "word_error_rate"
    ]
    assert calls, "the benchmark must actually call word_error_rate"
    for call in calls:
        reference = ast.unparse(call.args[0])
        assert "reference" in reference, (
            f"WER reference came from {reference!r}, which is not a corpus reference"
        )


def test_the_benchmark_runs_every_measurement_in_a_worker() -> None:
    source = BENCH.read_text(encoding="utf-8")
    assert "run_in_worker" in source
    assert "FasterWhisperProvider(" not in source, (
        "the benchmark must not load a model in the parent process"
    )


def test_the_benchmark_records_the_manifest_digest_of_what_it_measured() -> None:
    source = BENCH.read_text(encoding="utf-8")
    assert "manifest_sha256" in source


def test_the_benchmark_requests_egress_recording() -> None:
    """The 'zero network egress' line has to be a measurement, not an assertion."""
    source = BENCH.read_text(encoding="utf-8")
    assert "record_network_attempts" in source


# ===========================================================================
# `--validate-only`: check a manifest before spending hours on it
# ===========================================================================


def _corpus(tmp_path: Path, **overrides: Any) -> Path:
    audio, digest = _write_sample(tmp_path, "sample.wav", b"synthetic-audio-bytes")
    reference = tmp_path / "sample.txt"
    reference.write_text("kata kata", encoding="utf-8")
    entry: dict[str, Any] = {
        "sample_uuid": "00000000-0000-4000-8000-000000000001",
        "audio_path": str(audio),
        "sha256": digest,
        "duration_seconds": 12.0,
        "language": "id",
        "reference_transcript_path": str(reference),
        "consent_status": "synthetic",
        "license_name": "generated",
        "condition": "clean",
        "technical_terms": ["API"],
    }
    entry.update(overrides)
    return _manifest(tmp_path, [entry])


def test_validation_accepts_a_well_formed_manifest(tmp_path: Path) -> None:
    report = validate_corpus_manifest(_corpus(tmp_path))
    assert report["ok"] is True
    assert report["problem"] is None
    assert report["sample_count"] == 1
    assert report["with_reference"] == 1
    assert report["conditions"] == {"clean": 1}


def test_validation_applies_the_same_checksum_gate_as_the_benchmark(
    tmp_path: Path,
) -> None:
    """Same loader, so a manifest that validates cannot fail the real run on schema."""
    report = validate_corpus_manifest(_corpus(tmp_path, sha256="ff" * 32))
    assert report["ok"] is False
    assert "checksum mismatch" in report["problem"]


def test_validation_applies_the_same_consent_gate(tmp_path: Path) -> None:
    report = validate_corpus_manifest(_corpus(tmp_path, consent_status="assumed"))
    assert report["ok"] is False
    assert "requires recorded" in report["problem"]


def test_validation_refuses_a_missing_manifest(tmp_path: Path) -> None:
    report = validate_corpus_manifest(tmp_path / "absent.json")
    assert report["ok"] is False
    assert "does not exist" in report["problem"]


def test_validation_warns_when_no_sample_has_a_reference(tmp_path: Path) -> None:
    """Without one there is no accuracy number, only timing."""
    report = validate_corpus_manifest(_corpus(tmp_path, reference_transcript_path=None))
    assert report["ok"] is True
    assert report["with_reference"] == 0
    assert any("WER will be N/A" in warning for warning in report["warnings"])


def test_validation_warns_about_a_declared_reference_that_is_absent(
    tmp_path: Path,
) -> None:
    report = validate_corpus_manifest(
        _corpus(tmp_path, reference_transcript_path=str(tmp_path / "gone.txt"))
    )
    assert report["ok"] is True
    assert any("no readable reference" in warning for warning in report["warnings"])


def test_validation_warns_when_no_sample_is_far_field(tmp_path: Path) -> None:
    """Far-field is the condition that decides whether the product works in a real room."""
    report = validate_corpus_manifest(_corpus(tmp_path))
    assert any("far-field" in warning for warning in report["warnings"])


def test_validation_notices_a_wrong_declared_duration(tmp_path: Path) -> None:
    """The declared duration is what the real-time factor is computed against."""
    import struct
    import wave

    audio = tmp_path / "real.wav"
    with wave.open(str(audio), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(struct.pack("<h", 0) * 16_000)  # exactly 1 second
    import hashlib

    manifest = _manifest(
        tmp_path,
        [
            {
                "sample_uuid": "x",
                "audio_path": str(audio),
                "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                "duration_seconds": 600.0,
                "consent_status": "synthetic",
            }
        ],
    )
    report = validate_corpus_manifest(manifest)
    assert report["ok"] is True
    assert any("declares 600.0s" in warning for warning in report["warnings"])
    assert report["samples"][0]["measured_duration_seconds"] == pytest.approx(1.0)


def test_validation_reports_the_file_name_but_not_the_path(tmp_path: Path) -> None:
    """The report is quotable; an operator's private path is not."""
    report = validate_corpus_manifest(_corpus(tmp_path))
    blob = repr(report["samples"])
    assert "sample.wav" in blob
    assert str(tmp_path) not in blob


def test_validation_loads_no_model_and_runs_no_inference() -> None:
    """Structural: the whole point is that it is cheap and safe to run."""
    import ast

    tree = ast.parse(BENCH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "validate_corpus_manifest"
    )
    identifiers: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
    for forbidden in ("run_in_worker", "FasterWhisperProvider", "_run_one", "transcribe"):
        assert forbidden not in identifiers, forbidden


def test_validation_writes_nothing(tmp_path: Path) -> None:
    manifest = _corpus(tmp_path)
    before = {path.name: path.stat().st_mtime_ns for path in sorted(tmp_path.iterdir())}
    validate_corpus_manifest(manifest)
    after = {path.name: path.stat().st_mtime_ns for path in sorted(tmp_path.iterdir())}
    assert before == after


# ===========================================================================
# The shipped templates must actually work
# ===========================================================================


EXAMPLES = Path(__file__).resolve().parent.parent / "docs" / "examples"


def test_the_example_manifest_is_valid_json() -> None:
    import json

    payload = json.loads(
        (EXAMPLES / "asr-evaluation-manifest.example.json").read_text(encoding="utf-8")
    )
    assert isinstance(payload["samples"], list)
    assert len(payload["samples"]) >= 2


def test_every_field_the_loader_reads_appears_in_the_example() -> None:
    """A template that omits a field teaches the operator to omit it too."""
    import json

    payload = json.loads(
        (EXAMPLES / "asr-evaluation-manifest.example.json").read_text(encoding="utf-8")
    )
    keys: set[str] = set()
    for sample in payload["samples"]:
        keys.update(sample)
    for field_name in (
        "sample_uuid",
        "audio_path",
        "sha256",
        "duration_seconds",
        "language",
        "reference_transcript_path",
        "consent_status",
        "license_name",
        "technical_terms",
        "condition",
        "word_timestamp_reference_path",
    ):
        assert field_name in keys, field_name


def test_the_example_uses_only_accepted_consent_values() -> None:
    import json

    payload = json.loads(
        (EXAMPLES / "asr-evaluation-manifest.example.json").read_text(encoding="utf-8")
    )
    for sample in payload["samples"]:
        assert sample["consent_status"] in {"granted", "public-licensed", "synthetic"}


def test_the_example_manifest_is_refused_as_shipped(tmp_path: Path) -> None:
    """It must not look like working configuration: every value is a placeholder.

    Copying it and running the benchmark unchanged has to fail loudly, or somebody will
    produce a "result" from paths that do not exist.
    """
    import shutil

    target = tmp_path / "corpus.json"
    shutil.copy(EXAMPLES / "asr-evaluation-manifest.example.json", target)
    report = validate_corpus_manifest(target)
    assert report["ok"] is False
    assert report["problem"]


def test_the_example_manifest_contains_no_real_audio_path_or_person() -> None:
    text = (EXAMPLES / "asr-evaluation-manifest.example.json").read_text(encoding="utf-8")
    for forbidden in ("Aldy", "pangsor", "C:\\Users", "MoM-IGD-Data"):
        assert forbidden not in text, forbidden
    assert "REPLACE_WITH" in text, "placeholders must be obviously placeholders"


def test_the_example_reference_transcript_says_how_to_write_one() -> None:
    text = (EXAMPLES / "asr-reference-transcript.example.txt").read_text(encoding="utf-8")
    assert "PLACEHOLDER" in text
    for guidance in ("Write what was said", "Do not add speaker labels", "Do not translate"):
        assert guidance in text, guidance


def test_the_example_reference_is_not_a_recording_of_anybody() -> None:
    text = (EXAMPLES / "asr-reference-transcript.example.txt").read_text(encoding="utf-8")
    assert "Nothing below is a recording of anybody" in text
