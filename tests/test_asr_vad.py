"""The VAD stage: geometry, provenance, and the refusal to degrade silently.

Most of what can go wrong here is arithmetic on intervals, and all of it is testable
without a model: padding that pushes a region past the end of the audio, a merge that
swallows a real pause, a split that loses a millisecond, a short blip counted as a turn.
Every one of those corrupts the mapping back to the master recording, which is the thing
Phase 4 exists to preserve.

The Silero model itself ships inside the faster-whisper wheel, so the end-to-end tests here
need no download — but they do need the wheel, and they are honest about what a synthetic
tone can and cannot demonstrate.
"""

from __future__ import annotations

import ast
import math
import struct
import wave
from pathlib import Path

import pytest

from mom_igd.asr.vad import (
    VAD_MODEL_NAME,
    VadConfig,
    VadError,
    VadResult,
    _merge_and_bound,
    detect_speech_regions,
    onnx_provider_evidence,
    vad_asset_digest,
)
from mom_igd.asr.provider import SpeechRegion

ASR = Path(__file__).resolve().parent.parent / "mom_igd" / "asr"


def _write_wav(
    path: Path,
    seconds: float,
    *,
    rate: int = 16_000,
    channels: int = 1,
    width: int = 2,
    tone: bool = True,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    total = int(seconds * rate)
    for index in range(total):
        value = 0
        if tone:
            value = int(0.3 * 22000 * math.sin(2 * math.pi * 220 * index / rate))
        frames += struct.pack("<h", max(-32768, min(32767, value))) * channels
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return path


# ===========================================================================
# Configuration and provenance
# ===========================================================================


def test_the_defaults_are_the_ones_the_benchmark_used() -> None:
    config = VadConfig()
    assert config.threshold == 0.5
    assert config.min_speech_ms == 250
    assert config.min_silence_ms == 500
    assert config.speech_pad_ms == 200
    assert config.merge_gap_ms == 150
    assert config.max_region_seconds == 30.0


def test_the_config_hash_is_stable_and_discriminating() -> None:
    """Checkpoint invalidation depends on it: same settings, same hash; any change, new."""
    assert VadConfig().config_hash == VadConfig().config_hash
    assert VadConfig().config_hash != VadConfig(threshold=0.6).config_hash
    assert VadConfig().config_hash != VadConfig(min_silence_ms=400).config_hash
    assert len(VadConfig().config_hash) == 64


def test_the_bundled_asset_is_hashed_for_provenance() -> None:
    """A wheel upgrade can change the VAD; a stored region set must say which one ran."""
    name, digest = vad_asset_digest()
    assert name.endswith(".onnx")
    assert len(digest) == 64
    assert vad_asset_digest() == (name, digest), "the digest must be stable"


def test_the_onnx_session_runs_on_cpu() -> None:
    evidence = onnx_provider_evidence()
    assert evidence["session"] == ["CPUExecutionProvider"], evidence
    assert evidence["ok"] is True


# ===========================================================================
# Interval geometry: padding, clamping, merging, splitting, dropping
# ===========================================================================


def test_padding_never_produces_a_negative_start() -> None:
    """A region at t=0 with 200 ms padding must clamp, not go negative.

    A negative timestamp would break the mapping back to the master recording, which is
    what every downstream stage and every stored row is expressed against.
    """
    regions, _merged, _split, _dropped = _merge_and_bound(
        [(0.05, 2.0)], VadConfig(speech_pad_ms=200), audio_seconds=10.0
    )
    assert regions[0][0] == 0.0


def test_padding_never_runs_past_the_end_of_the_audio() -> None:
    regions, _merged, _split, _dropped = _merge_and_bound(
        [(8.0, 9.95)], VadConfig(speech_pad_ms=200), audio_seconds=10.0
    )
    assert regions[-1][1] == pytest.approx(10.0)


def test_a_short_gap_is_merged_into_one_region() -> None:
    """Two regions 60 ms apart are one utterance; paying the model twice is waste."""
    regions, merged, _split, _dropped = _merge_and_bound(
        [(1.0, 2.0), (2.06, 3.0)],
        VadConfig(speech_pad_ms=0, merge_gap_ms=150),
        audio_seconds=10.0,
    )
    assert len(regions) == 1
    assert merged == 1
    assert regions[0] == (1.0, 3.0)


def test_a_long_gap_is_not_merged() -> None:
    regions, merged, _split, _dropped = _merge_and_bound(
        [(1.0, 2.0), (5.0, 6.0)],
        VadConfig(speech_pad_ms=0, merge_gap_ms=150),
        audio_seconds=10.0,
    )
    assert len(regions) == 2
    assert merged == 0


def test_padding_is_applied_before_the_merge_decision() -> None:
    """Order matters: the merge must see the geometry the engine will actually decode.

    Two regions 300 ms apart do not merge unpadded (gap 300 > 150), but with 200 ms of
    padding each side they overlap, and treating them as separate would decode the same
    audio twice.
    """
    unpadded, _m1, _s1, _d1 = _merge_and_bound(
        [(1.0, 2.0), (2.3, 3.0)],
        VadConfig(speech_pad_ms=0, merge_gap_ms=150),
        audio_seconds=10.0,
    )
    padded, merged, _s2, _d2 = _merge_and_bound(
        [(1.0, 2.0), (2.3, 3.0)],
        VadConfig(speech_pad_ms=200, merge_gap_ms=150),
        audio_seconds=10.0,
    )
    assert len(unpadded) == 2
    assert len(padded) == 1
    assert merged == 1


def test_a_region_shorter_than_the_minimum_is_dropped() -> None:
    """A cough or a chair is not a turn."""
    regions, _merged, _split, dropped = _merge_and_bound(
        [(1.0, 1.1), (3.0, 5.0)],
        VadConfig(min_speech_ms=250, speech_pad_ms=0),
        audio_seconds=10.0,
    )
    assert len(regions) == 1
    assert dropped == 1
    assert regions[0][0] == pytest.approx(3.0)


def test_a_region_exactly_at_the_minimum_is_kept() -> None:
    regions, _merged, _split, dropped = _merge_and_bound(
        [(1.0, 1.25)], VadConfig(min_speech_ms=250, speech_pad_ms=0), audio_seconds=10.0
    )
    assert len(regions) == 1
    assert dropped == 0


def test_an_over_long_region_is_split_without_losing_time() -> None:
    """One long monologue must not become a single enormous decode.

    The split must be lossless: the pieces have to tile the original exactly, or audio
    silently stops being transcribed.
    """
    regions, _merged, split, _dropped = _merge_and_bound(
        [(0.0, 95.0)],
        VadConfig(speech_pad_ms=0, max_region_seconds=30.0),
        audio_seconds=100.0,
    )
    assert split == 3
    assert len(regions) == 4
    assert regions[0][0] == pytest.approx(0.0)
    assert regions[-1][1] == pytest.approx(95.0)
    for piece in regions:
        assert piece[1] - piece[0] <= 30.0 + 1e-6
    # Contiguous: each piece starts where the last ended.
    for earlier, later in zip(regions, regions[1:]):
        assert later[0] == pytest.approx(earlier[1])
    total = sum(end - start for start, end in regions)
    assert total == pytest.approx(95.0)


def test_regions_come_back_ordered() -> None:
    regions, _merged, _split, _dropped = _merge_and_bound(
        [(5.0, 6.0), (1.0, 2.0), (3.0, 4.0)],
        VadConfig(speech_pad_ms=0, merge_gap_ms=0),
        audio_seconds=10.0,
    )
    starts = [start for start, _end in regions]
    assert starts == sorted(starts)


def test_no_intervals_yields_no_regions() -> None:
    regions, merged, split, dropped = _merge_and_bound([], VadConfig(), audio_seconds=10.0)
    assert regions == []
    assert (merged, split, dropped) == (0, 0, 0)


# ===========================================================================
# The result object
# ===========================================================================


def test_the_result_reports_speech_duration_and_ratio() -> None:
    result = VadResult(
        regions=(
            SpeechRegion(index=0, start=0.0, end=2.0),
            SpeechRegion(index=1, start=5.0, end=8.0),
        ),
        audio_seconds=20.0,
        model_name=VAD_MODEL_NAME,
        model_sha256="a" * 64,
        config=VadConfig(),
    )
    assert result.total_speech_seconds == pytest.approx(5.0)
    assert result.speech_ratio == pytest.approx(0.25)
    payload = result.to_dict()
    assert payload["region_count"] == 2
    assert payload["ran"] is True
    assert payload["config_hash"] == VadConfig().config_hash


def test_a_zero_length_recording_has_a_zero_ratio_not_a_division_error() -> None:
    result = VadResult(
        regions=(),
        audio_seconds=0.0,
        model_name=VAD_MODEL_NAME,
        model_sha256="a" * 64,
        config=VadConfig(),
    )
    assert result.speech_ratio == 0.0
    assert result.total_speech_seconds == 0.0


# ===========================================================================
# Input validation: the working copy must be what normalisation produced
# ===========================================================================


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(VadError, match="does not exist"):
        detect_speech_regions(tmp_path / "absent.wav")


def test_stereo_audio_is_refused_rather_than_coped_with(tmp_path: Path) -> None:
    """A surprise format means normalisation did not run, and hiding that hides the bug."""
    path = _write_wav(tmp_path / "stereo.wav", 1.0, channels=2)
    with pytest.raises(VadError, match="requires the\n?.*16 kHz mono|2ch"):
        detect_speech_regions(path)


def test_eight_bit_audio_is_refused(tmp_path: Path) -> None:
    path = _write_wav(tmp_path / "eight.wav", 1.0, width=1)
    with pytest.raises(VadError):
        detect_speech_regions(path)


def test_a_wrong_sample_rate_is_refused(tmp_path: Path) -> None:
    path = _write_wav(tmp_path / "wrong.wav", 1.0, rate=44_100)
    with pytest.raises(VadError, match="44100 Hz|16 kHz"):
        detect_speech_regions(path)


# ===========================================================================
# End to end, with the bundled model
# ===========================================================================


def test_silence_produces_zero_regions_and_that_is_a_valid_result(tmp_path: Path) -> None:
    """A silent meeting is a legitimate transcript, not a failure.

    What must never happen is a VAD *error* being presented as an empty transcript, which
    is why `ran` distinguishes "ran and found nothing" from "did not run".
    """
    path = _write_wav(tmp_path / "silence.wav", 3.0, tone=False)
    result = detect_speech_regions(path)
    assert result.ran is True
    assert result.regions == ()
    assert result.total_speech_seconds == 0.0
    assert result.audio_seconds == pytest.approx(3.0, abs=0.01)


def test_a_synthetic_tone_produces_no_speech_regions(tmp_path: Path) -> None:
    """Silero detects human speech, and a tone is not speech.

    Asserting otherwise would be asserting a false positive. This is the honest bound on
    what synthetic audio can demonstrate about VAD.
    """
    path = _write_wav(tmp_path / "tone.wav", 3.0, tone=True)
    result = detect_speech_regions(path)
    assert result.ran is True
    assert result.regions == ()


def test_every_region_is_bounded_and_ordered_end_to_end(tmp_path: Path) -> None:
    path = _write_wav(tmp_path / "probe.wav", 4.0, tone=True)
    result = detect_speech_regions(path)
    for region in result.regions:
        assert 0.0 <= region.start <= region.end <= result.audio_seconds + 1e-6
    for earlier, later in zip(result.regions, result.regions[1:]):
        assert earlier.end <= later.start + 1e-6


def test_the_result_records_which_model_and_configuration_ran(tmp_path: Path) -> None:
    path = _write_wav(tmp_path / "probe.wav", 2.0, tone=False)
    config = VadConfig(threshold=0.55, min_silence_ms=400)
    result = detect_speech_regions(path, config)
    assert result.model_name == VAD_MODEL_NAME
    assert len(result.model_sha256) == 64
    assert result.config.threshold == 0.55
    assert result.to_dict()["config"]["min_silence_ms"] == 400


# ===========================================================================
# No silent fallback
# ===========================================================================


def test_a_configuration_that_cannot_be_applied_raises(tmp_path: Path, monkeypatch) -> None:
    """Silently ignoring the operator's thresholds is how a tuned VAD becomes untuned.

    An earlier version caught the `TypeError` from a field-name mismatch and fell back to
    library defaults, so every configured threshold was discarded without a word.
    """
    import mom_igd.asr.vad as vad_module

    class Incompatible:
        def __init__(self, *_args, **_kwargs):
            raise TypeError("unexpected keyword argument 'threshold'")

    path = _write_wav(tmp_path / "probe.wav", 1.0, tone=False)
    monkeypatch.setattr(
        "faster_whisper.vad.VadOptions", Incompatible, raising=True
    )
    with pytest.raises(vad_module.VadError, match="Refusing to run VAD with default"):
        detect_speech_regions(path)


def test_the_vad_module_contains_no_silent_except_fallback() -> None:
    """`except TypeError: use defaults` is the exact shape of the bug that was fixed."""
    source = (ASR / "vad.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        # Every handler must either raise or log -- never quietly continue with a default.
        raises = any(isinstance(inner, ast.Raise) for inner in ast.walk(node))
        returns_default = any(isinstance(inner, ast.Return) for inner in ast.walk(node))
        assert raises or returns_default, (
            f"vad.py:{node.lineno} swallows an exception without raising or returning"
        )


def test_vad_is_not_delegated_to_the_asr_engine() -> None:
    """The stored regions and the transcribed audio must not be able to disagree."""
    source = (ASR / "faster_whisper_provider.py").read_text(encoding="utf-8")
    assert '"vad_filter": False' in source
