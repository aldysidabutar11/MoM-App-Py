"""The ASR provider contract, its output validation, and the fake-provider firewall.

Two independent things are asserted here.

**Validation rejects rather than repairs.** A transcription engine is a large
third-party numerical program, and it does emit `NaN` probabilities, reversed intervals
and words outside their segment. Every one of those is a data-integrity problem for a
system whose product is evidence: a shifted timestamp breaks the link back to the master
recording, and a `NaN` confidence silently poisons the pass-2 selection that reads it. So
the validator is strict, and these tests prove each rejection individually rather than
trusting one happy path.

**The fake provider cannot reach production.** A transcript produced by a stand-in and
stored as though a real model made it would be a fabricated record. There is deliberately
no configuration key, environment variable, request field, registry entry or CLI option
that selects it, and that absence is asserted by searching the actual source rather than
by assertion-by-comment.

No model is loaded anywhere in this file: the deterministic fake covers the contract, and
the real engine is exercised by `asr smoke` and the benchmark.
"""

from __future__ import annotations

import ast
import math
import re
from pathlib import Path

import pytest

from mom_igd.asr.fake_provider import (
    FAKE_MODEL_NAME,
    BrokenAsrProvider,
    DeterministicAsrProvider,
    SlowAsrProvider,
)
from mom_igd.asr.provider import (
    MAX_SEGMENT_CHARS,
    MAX_WORDS_PER_SEGMENT,
    AsrModelInfo,
    AsrProvider,
    ProviderError,
    ProviderOutputError,
    SpeakerStatus,
    SpeechRegion,
    TranscriptSegment,
    TranscriptionRequest,
    TranscriptionResult,
    Word,
    validate_transcription,
)

REPO = Path(__file__).resolve().parent.parent


def _regions(*spans: tuple[float, float]) -> tuple[SpeechRegion, ...]:
    return tuple(
        SpeechRegion(index=index, start=start, end=end)
        for index, (start, end) in enumerate(spans)
    )


def _result(*segments: TranscriptSegment, audio: float = 60.0) -> TranscriptionResult:
    return TranscriptionResult(
        segments=segments,
        model=AsrModelInfo(
            model_name="m",
            revision="r",
            manifest_sha256="a" * 64,
            compute_type="int8",
            cpu_threads=4,
            provider_id="test",
        ),
        language="id",
        audio_seconds=audio,
        processing_seconds=1.0,
    )


# ===========================================================================
# Valid output is accepted
# ===========================================================================


def test_well_formed_output_is_accepted_unchanged() -> None:
    segment = TranscriptSegment(
        index=0,
        start=1.0,
        end=3.0,
        text="kita perlu deploy server",
        words=(
            Word(text="kita", start=1.0, end=1.4, probability=0.9),
            Word(text="perlu", start=1.4, end=1.9, probability=0.8),
            Word(text="deploy", start=2.0, end=2.5, probability=0.7),
            Word(text="server", start=2.5, end=3.0, probability=0.95),
        ),
        avg_logprob=-0.22,
        no_speech_prob=0.03,
        compression_ratio=1.4,
    )
    validated = validate_transcription(_result(segment))
    assert len(validated.segments) == 1
    assert validated.segments[0].text == "kita perlu deploy server"
    assert len(validated.segments[0].words) == 4
    assert validated.segments[0].speaker is None
    assert validated.segments[0].speaker_status == SpeakerStatus.UNASSIGNED


def test_a_segment_with_no_words_is_accepted() -> None:
    """Music or noise legitimately transcribes to text with no aligned words."""
    validated = validate_transcription(
        _result(TranscriptSegment(index=0, start=0.0, end=2.0, text="(musik)"))
    )
    assert validated.segments[0].words == ()


def test_missing_optional_metrics_are_accepted_as_none() -> None:
    validated = validate_transcription(
        _result(TranscriptSegment(index=0, start=0.0, end=1.0, text="x"))
    )
    segment = validated.segments[0]
    assert segment.avg_logprob is None
    assert segment.no_speech_prob is None
    assert segment.compression_ratio is None


def test_adjacent_segments_that_touch_exactly_are_accepted() -> None:
    """A segment may start exactly where the previous one ended."""
    validated = validate_transcription(
        _result(
            TranscriptSegment(index=0, start=0.0, end=2.0, text="a"),
            TranscriptSegment(index=1, start=2.0, end=4.0, text="b"),
        )
    )
    assert len(validated.segments) == 2


def test_sub_millisecond_word_drift_is_clamped_not_rejected() -> None:
    """Frame arithmetic in float32 puts a word a few microseconds outside its segment.

    Discarding a correct word over a microsecond would be worse than clamping it, so
    this one case is a correction of representation rather than of content.
    """
    segment = TranscriptSegment(
        index=0,
        start=1.0,
        end=2.0,
        text="x",
        words=(Word(text="x", start=0.9999, end=2.0001),),
    )
    validated = validate_transcription(_result(segment))
    word = validated.segments[0].words[0]
    assert word.start >= 1.0
    assert word.end <= 2.0


# ===========================================================================
# Non-finite numbers are refused
# ===========================================================================


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_a_non_finite_no_speech_probability_is_refused(bad: float) -> None:
    with pytest.raises(ProviderOutputError, match="not a finite number"):
        validate_transcription(
            _result(
                TranscriptSegment(index=0, start=0.0, end=1.0, text="x", no_speech_prob=bad)
            )
        )


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_a_non_finite_avg_logprob_is_refused(bad: float) -> None:
    with pytest.raises(ProviderOutputError, match="not a finite number"):
        validate_transcription(
            _result(
                TranscriptSegment(index=0, start=0.0, end=1.0, text="x", avg_logprob=bad)
            )
        )


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_a_non_finite_timestamp_is_refused(bad: float) -> None:
    with pytest.raises(ProviderOutputError, match="not a finite number"):
        validate_transcription(
            _result(TranscriptSegment(index=0, start=bad, end=1.0, text="x"))
        )


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_a_non_finite_word_probability_is_refused(bad: float) -> None:
    with pytest.raises(ProviderOutputError, match="not a finite number"):
        validate_transcription(
            _result(
                TranscriptSegment(
                    index=0,
                    start=0.0,
                    end=1.0,
                    text="x",
                    words=(Word(text="x", start=0.0, end=1.0, probability=bad),),
                )
            )
        )


def test_a_non_finite_compression_ratio_is_refused() -> None:
    with pytest.raises(ProviderOutputError, match="not a finite number"):
        validate_transcription(
            _result(
                TranscriptSegment(
                    index=0, start=0.0, end=1.0, text="x", compression_ratio=math.inf
                )
            )
        )


# ===========================================================================
# Timestamps: negative, reversed, out of order, outside the parent
# ===========================================================================


def test_a_negative_segment_start_is_refused() -> None:
    with pytest.raises(ProviderOutputError, match="negative timestamp"):
        validate_transcription(
            _result(TranscriptSegment(index=0, start=-0.5, end=1.0, text="x"))
        )


def test_a_reversed_segment_is_refused() -> None:
    with pytest.raises(ProviderOutputError, match="ends before it starts"):
        validate_transcription(
            _result(TranscriptSegment(index=0, start=5.0, end=2.0, text="x"))
        )


def test_out_of_order_segments_are_refused() -> None:
    """Ordering is what makes a transcript navigable, and overlap would double-count."""
    with pytest.raises(ProviderOutputError, match="before the previous segment ended"):
        validate_transcription(
            _result(
                TranscriptSegment(index=0, start=5.0, end=8.0, text="a"),
                TranscriptSegment(index=1, start=1.0, end=3.0, text="b"),
            )
        )


def test_overlapping_segments_are_refused() -> None:
    with pytest.raises(ProviderOutputError, match="before the previous segment ended"):
        validate_transcription(
            _result(
                TranscriptSegment(index=0, start=0.0, end=5.0, text="a"),
                TranscriptSegment(index=1, start=3.0, end=7.0, text="b"),
            )
        )


def test_a_segment_starting_past_the_end_of_the_audio_is_refused() -> None:
    with pytest.raises(ProviderOutputError, match="past the end"):
        validate_transcription(
            _result(TranscriptSegment(index=0, start=90.0, end=95.0, text="x"), audio=60.0),
            audio_seconds=60.0,
        )


def test_a_negative_word_timestamp_is_refused() -> None:
    with pytest.raises(ProviderOutputError, match="negative timestamp"):
        validate_transcription(
            _result(
                TranscriptSegment(
                    index=0,
                    start=0.0,
                    end=1.0,
                    text="x",
                    words=(Word(text="x", start=-1.0, end=0.5),),
                )
            )
        )


def test_a_reversed_word_is_refused() -> None:
    with pytest.raises(ProviderOutputError, match="ends before it starts"):
        validate_transcription(
            _result(
                TranscriptSegment(
                    index=0,
                    start=1.0,
                    end=2.0,
                    text="x",
                    words=(Word(text="x", start=1.8, end=1.2),),
                )
            )
        )


def test_a_word_outside_its_parent_segment_is_refused() -> None:
    """The strongest structural guarantee: a word belongs to the segment that holds it."""
    with pytest.raises(ProviderOutputError, match="outside its segment"):
        validate_transcription(
            _result(
                TranscriptSegment(
                    index=0,
                    start=1.0,
                    end=2.0,
                    text="x",
                    words=(Word(text="x", start=8.0, end=9.0),),
                )
            )
        )


def test_out_of_order_words_are_refused() -> None:
    with pytest.raises(ProviderOutputError, match="before the previous word ended"):
        validate_transcription(
            _result(
                TranscriptSegment(
                    index=0,
                    start=0.0,
                    end=3.0,
                    text="a b",
                    words=(
                        Word(text="a", start=2.0, end=2.9),
                        Word(text="b", start=0.1, end=0.5),
                    ),
                )
            )
        )


# ===========================================================================
# Probabilities, text size, pass number, speaker
# ===========================================================================


@pytest.mark.parametrize("bad", [1.9, 2.0, -0.5, 100.0])
def test_a_probability_outside_zero_to_one_is_refused(bad: float) -> None:
    with pytest.raises(ProviderOutputError, match="outside \\[0, 1\\]"):
        validate_transcription(
            _result(
                TranscriptSegment(index=0, start=0.0, end=1.0, text="x", no_speech_prob=bad)
            )
        )


def test_a_word_probability_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(ProviderOutputError, match="outside \\[0, 1\\]"):
        validate_transcription(
            _result(
                TranscriptSegment(
                    index=0,
                    start=0.0,
                    end=1.0,
                    text="x",
                    words=(Word(text="x", start=0.0, end=1.0, probability=3.0),),
                )
            )
        )


def test_text_beyond_the_size_cap_is_refused() -> None:
    """A wall of repeated tokens is a known decoder failure, not speech."""
    with pytest.raises(ProviderOutputError, match="repetition loop"):
        validate_transcription(
            _result(
                TranscriptSegment(
                    index=0, start=0.0, end=1.0, text="a" * (MAX_SEGMENT_CHARS + 1)
                )
            )
        )


def test_text_exactly_at_the_cap_is_accepted() -> None:
    validated = validate_transcription(
        _result(TranscriptSegment(index=0, start=0.0, end=1.0, text="a" * MAX_SEGMENT_CHARS))
    )
    assert len(validated.segments[0].text) == MAX_SEGMENT_CHARS


def test_too_many_words_in_one_segment_is_refused() -> None:
    words = tuple(
        Word(text="x", start=0.0, end=0.0) for _ in range(MAX_WORDS_PER_SEGMENT + 1)
    )
    with pytest.raises(ProviderOutputError, match="repetition loop"):
        validate_transcription(
            _result(TranscriptSegment(index=0, start=0.0, end=1.0, text="x", words=words))
        )


@pytest.mark.parametrize("bad", [0, 3, 7, -1])
def test_an_unknown_asr_pass_number_is_refused(bad: int) -> None:
    with pytest.raises(ProviderOutputError, match="asr_pass"):
        validate_transcription(
            _result(TranscriptSegment(index=0, start=0.0, end=1.0, text="x", asr_pass=bad))
        )


def test_a_segment_carrying_a_speaker_is_refused() -> None:
    """Phase 4 assigns no speakers. A provider that invented one must be caught."""
    segment = TranscriptSegment(index=0, start=0.0, end=1.0, text="x")
    object.__setattr__(segment, "speaker", "Budi")
    with pytest.raises(ProviderOutputError, match="Phase 4 assigns no speakers"):
        validate_transcription(_result(segment))


def test_negative_processing_or_audio_seconds_are_refused() -> None:
    result = _result(TranscriptSegment(index=0, start=0.0, end=1.0, text="x"))
    object.__setattr__(result, "processing_seconds", -1.0)
    with pytest.raises(ProviderOutputError, match=">= 0"):
        validate_transcription(result)


@pytest.mark.parametrize(
    "mode",
    [
        "nan_probability",
        "infinite_logprob",
        "reversed_interval",
        "negative_timestamp",
        "probability_above_one",
        "word_outside_segment",
        "reversed_word",
        "text_too_long",
        "too_many_words",
        "speaker_assigned",
        "bad_pass",
    ],
)
def test_every_broken_provider_mode_is_rejected(mode: str) -> None:
    """The provider boundary, exercised through a provider rather than the validator."""
    provider = BrokenAsrProvider(mode)
    with pytest.raises(ProviderOutputError):
        provider.transcribe(
            TranscriptionRequest(audio_path="ignored.wav", regions=_regions((0.0, 10.0)))
        )


# ===========================================================================
# The deterministic fake provider
# ===========================================================================


def test_the_fake_provider_satisfies_the_protocol() -> None:
    assert isinstance(DeterministicAsrProvider(), AsrProvider)


def test_the_fake_provider_is_deterministic() -> None:
    """Same seed, same regions, byte-identical output. A test can assert exact values."""
    regions = _regions((0.0, 4.0), (6.0, 9.5))
    request = TranscriptionRequest(audio_path="x.wav", regions=regions)

    first = DeterministicAsrProvider(seed="fixed").transcribe(request)
    second = DeterministicAsrProvider(seed="fixed").transcribe(request)

    assert [s.to_dict() for s in first.segments] == [s.to_dict() for s in second.segments]
    assert first.segments[0].text
    assert all(s.words for s in first.segments)


def test_a_different_seed_produces_different_text() -> None:
    regions = _regions((0.0, 4.0))
    request = TranscriptionRequest(audio_path="x.wav", regions=regions)
    a = DeterministicAsrProvider(seed="one").transcribe(request)
    b = DeterministicAsrProvider(seed="two").transcribe(request)
    assert a.segments[0].text != b.segments[0].text


def test_the_fake_provider_output_passes_the_real_validator() -> None:
    """It must model a *correct* provider, or tests built on it prove nothing."""
    result = DeterministicAsrProvider().transcribe(
        TranscriptionRequest(audio_path="x.wav", regions=_regions((1.0, 5.0), (7.0, 12.0)))
    )
    revalidated = validate_transcription(result)
    assert len(revalidated.segments) == 2
    for segment in revalidated.segments:
        assert segment.words
        for word in segment.words:
            assert segment.start <= word.start <= word.end <= segment.end


def test_the_fake_provider_produces_word_timestamps_inside_their_segments() -> None:
    result = DeterministicAsrProvider(words_per_second=4.0).transcribe(
        TranscriptionRequest(audio_path="x.wav", regions=_regions((2.0, 6.0)))
    )
    segment = result.segments[0]
    assert len(segment.words) >= 8
    assert segment.words[0].start >= 2.0
    assert segment.words[-1].end <= 6.0


def test_the_fake_provider_counts_its_calls_so_a_budget_can_be_asserted() -> None:
    provider = DeterministicAsrProvider()
    request = TranscriptionRequest(audio_path="x.wav", regions=_regions((0.0, 2.0)))
    provider.transcribe(request)
    provider.transcribe(request)
    assert provider.calls == 2


def test_the_fake_provider_refuses_a_whole_file_decode() -> None:
    """So a test cannot accidentally assert against a decode it did not set up."""
    with pytest.raises(ProviderError, match="requires explicit regions"):
        DeterministicAsrProvider().transcribe(
            TranscriptionRequest(audio_path="x.wav", regions=())
        )


def test_the_fake_provider_load_and_close_are_idempotent() -> None:
    provider = DeterministicAsrProvider()
    assert provider.loaded is False
    provider.load()
    provider.load()
    assert provider.loaded is True
    provider.close()
    provider.close()
    assert provider.loaded is False


def test_the_slow_fake_provider_still_produces_valid_output() -> None:
    result = SlowAsrProvider(delay_seconds=0.01).transcribe(
        TranscriptionRequest(audio_path="x.wav", regions=_regions((0.0, 2.0)))
    )
    assert validate_transcription(result).segments


# ===========================================================================
# The fake provider cannot reach production
# ===========================================================================


def test_the_fake_provider_marks_itself_as_a_test_double() -> None:
    """So a stored transcript would say so, rather than looking real."""
    info = DeterministicAsrProvider().info
    assert info.is_test_double is True
    assert info.model_name == FAKE_MODEL_NAME
    assert info.model_name.startswith("FAKE-")
    assert info.to_dict()["is_test_double"] is True
    assert DeterministicAsrProvider().health()["is_test_double"] is True


def test_the_real_provider_is_never_marked_as_a_test_double() -> None:
    """The flag must actually discriminate, or it is decoration."""
    source = (REPO / "mom_igd" / "asr" / "faster_whisper_provider.py").read_text(
        encoding="utf-8"
    )
    assert "is_test_double=False" in source
    assert "is_test_double=True" not in source


def test_no_runtime_module_imports_the_fake_provider() -> None:
    """The firewall, checked against the real import graph rather than asserted.

    Both forms are checked. An earlier version of this test only inspected
    ``ImportFrom.module``, so ``from mom_igd.asr import fake_provider`` -- where the
    module is ``mom_igd.asr`` and the name lives in the *alias* -- slipped straight
    through. A mutation test found that, and it is the more natural way to write the
    import.
    """
    offenders: list[str] = []
    for path in sorted((REPO / "mom_igd").rglob("*.py")):
        relative = path.relative_to(REPO).as_posix()
        if relative == "mom_igd/asr/fake_provider.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            candidates: list[str] = []
            if isinstance(node, ast.Import):
                candidates = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # The module AND every imported name: either can carry it.
                candidates = [node.module or ""] + [alias.name for alias in node.names]
            for name in candidates:
                if "fake_provider" in name or name in {"DeterministicAsrProvider",
                                                       "BrokenAsrProvider",
                                                       "SlowAsrProvider"}:
                    offenders.append(f"{relative}:{node.lineno} imports {name}")
    assert offenders == [], (
        f"the fake ASR provider must be reachable only from tests: {offenders}"
    )


#: The ASR stand-ins, named exactly.
#:
#: Scoped deliberately rather than sweeping for "fake". Two other legitimate test doubles
#: exist and must not be flagged: Phase 2's `FakeAudioBackend`, which real commands
#: (`audio smoke`, `audio bench`) are built on, and Phase 3's
#: `mom_igd/enrollment/fake_provider.py`, which is the speaker-embedding stand-in and has
#: its own `FAKE_MODEL_NAME`. A generic sweep flagged both, which is how a real check
#: becomes noise and then gets deleted.
_ASR_FAKE_NAMES = frozenset(
    {
        "DeterministicAsrProvider",
        "BrokenAsrProvider",
        "SlowAsrProvider",
    }
)


def test_no_runtime_module_references_the_asr_fake_by_name() -> None:
    """Catches a late import inside a function, or a reflective lookup by class name.

    Every ``fake_provider.py`` is excluded: each is a test double declared in its own
    module, and the property under test is that *other* modules do not reach for one.
    """
    offenders: list[str] = []
    for path in sorted((REPO / "mom_igd").rglob("*.py")):
        relative = path.relative_to(REPO).as_posix()
        if path.name == "fake_provider.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in _ASR_FAKE_NAMES:
                offenders.append(f"{relative}:{node.lineno} name {node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in _ASR_FAKE_NAMES:
                offenders.append(f"{relative}:{node.lineno} attribute {node.attr}")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in _ASR_FAKE_NAMES
            ):
                # A string literal naming the fake would be a reflective lookup.
                offenders.append(f"{relative}:{node.lineno} literal {node.value!r}")
    assert offenders == [], offenders


def test_the_two_fake_providers_are_separate_and_neither_is_reachable() -> None:
    """Phase 3 and Phase 4 each have a stand-in; they must not be confused for each other.

    Both define `FAKE_MODEL_NAME`, which is why the sweep above matches class names
    rather than that constant.
    """
    from mom_igd.asr.fake_provider import FAKE_MODEL_NAME as asr_fake
    from mom_igd.enrollment.fake_provider import FAKE_MODEL_NAME as embedding_fake

    assert asr_fake != embedding_fake
    assert asr_fake.startswith("FAKE-")
    assert embedding_fake.startswith("FAKE-")


def test_no_configuration_key_can_select_a_provider_implementation() -> None:
    """`providers` exists and is legitimate -- it is *endpoints*, not implementations.

    `[providers.endpoints]` came from Phase 1 and holds local filesystem paths or
    loopback URLs, validated by the offline policy. What must not exist is any key that
    chooses *which provider class* runs, because that is how a stand-in gets selected.
    """
    from mom_igd.config import AppConfig, ProvidersConfig

    # The only field named for providers is the endpoints mapping, and its sole member
    # is `endpoints`.
    assert set(ProvidersConfig.model_fields) == {"endpoints"}
    provider_fields = {
        name for name in AppConfig.model_fields if "provider" in name.lower()
    }
    assert provider_fields == {"providers"}, provider_fields

    # And no configured value anywhere names a stand-in.
    text = (REPO / "config" / "default.toml").read_text(encoding="utf-8").lower()
    for banned in ("fake", "test_double", "stub", "asr_provider", "provider_class"):
        assert banned not in text, banned


def test_a_provider_endpoint_cannot_be_used_to_smuggle_in_a_fake() -> None:
    """The endpoints mapping is validated; it takes local paths, not class names."""
    from mom_igd import offline_policy

    for hostile in (
        "http://example.com/asr",
        "https://api.openai.com/v1",
        "ws://10.0.0.5:9000",
    ):
        with pytest.raises(offline_policy.OfflinePolicyError):
            offline_policy.validate_provider_endpoint("asr", hostile)


def test_no_environment_variable_can_select_the_fake_provider() -> None:
    """A grep over the real source, because this is exactly what a hurried fix adds."""
    offenders: list[str] = []
    pattern = re.compile(
        r"(?:getenv|environ(?:\.get)?\s*[\[(])[^\n]{0,80}(?:fake|double|stub|mock)",
        re.IGNORECASE,
    )
    for path in sorted((REPO / "mom_igd").rglob("*.py")):
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(REPO).as_posix()}:{number}")
    assert offenders == [], offenders


def test_no_cli_option_can_select_a_provider() -> None:
    cli = (REPO / "mom_igd" / "cli.py").read_text(encoding="utf-8")
    for banned in ("--provider", "--fake", "--fake-provider", "--asr-provider", "--stub"):
        assert banned not in cli, banned


def test_the_model_catalogue_contains_no_fake_entry() -> None:
    from mom_igd.asr.provision import MODEL_CATALOGUE

    for key, spec in MODEL_CATALOGUE.items():
        assert "fake" not in key.lower()
        assert "fake" not in spec.model_name.lower()
        assert "fake" not in spec.repo_id.lower()


def test_a_fake_result_is_distinguishable_from_a_production_one() -> None:
    """Storing one as production would be a fabricated record, so it must be labelled."""
    fake = DeterministicAsrProvider().transcribe(
        TranscriptionRequest(audio_path="x.wav", regions=_regions((0.0, 2.0)))
    )
    assert fake.model.is_test_double is True
    assert fake.model.compute_type == "none"
    assert fake.model.provider_id.startswith("fake/")
    # Every field a persistence layer would key provenance on says "not real".
    payload = fake.model.to_dict()
    assert payload["is_test_double"] is True
    assert payload["model_name"].startswith("FAKE-")


def test_the_task_registry_offers_no_fake_task() -> None:
    """The worker dispatches by name from a closed set; none of them is a stand-in."""
    from mom_igd.asr.tasks import TASK_REGISTRY

    assert set(TASK_REGISTRY) == {"transcribe", "vad", "probe_model", "probe_directory"}
    for name in TASK_REGISTRY:
        assert "fake" not in name.lower()
