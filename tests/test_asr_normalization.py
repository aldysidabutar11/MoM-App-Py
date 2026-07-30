"""Building the 16 kHz mono working copy: the master must survive it untouched.

This is the only stage that reads the master recording, and the only one whose output the
models actually see. Three things have to be exactly right or every downstream timestamp
is wrong: the resampling ratio, the downmix, and how a gap in the master is handled.

Audio here is generated arithmetically. No fixture contains a human voice.
"""

from __future__ import annotations

import array
import math
import struct
import wave
from pathlib import Path

import pytest

from mom_igd.asr.normalize import (
    WORKING_CHANNELS,
    WORKING_SAMPLE_RATE,
    NormalizationError,
    downmix_to_mono,
    normalize_recording,
    resample_linear,
)


def _write_wav(
    path: Path,
    samples: array.array,
    *,
    rate: int,
    channels: int,
    width: int = 2,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())
    return path


def _tone(frames: int, *, rate: int, hz: float = 440.0, channels: int = 1, amplitude: float = 0.4):
    samples = array.array("h")
    for index in range(frames):
        value = int(amplitude * 32767 * math.sin(2 * math.pi * hz * index / rate))
        for _ in range(channels):
            samples.append(value)
    return samples


def _read(path: Path) -> tuple[array.array, int, int]:
    with wave.open(str(path), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
        samples = array.array("h")
        samples.frombytes(raw)
        return samples, handle.getframerate(), handle.getnchannels()


# ===========================================================================
# Downmix
# ===========================================================================


def test_mono_is_returned_unchanged() -> None:
    samples = array.array("h", [1, -2, 3])
    assert downmix_to_mono(samples, 1) is samples


def test_stereo_is_averaged_not_halved() -> None:
    """Dropping a channel would lose half the room on a directional microphone."""
    samples = array.array("h", [100, 200, -100, -300])
    assert list(downmix_to_mono(samples, 2)) == [150, -200]


def test_averaging_rounds_half_away_from_zero() -> None:
    """Rounding half down would put a DC offset on the whole file."""
    assert list(downmix_to_mono(array.array("h", [1, 2]), 2)) == [2]
    assert list(downmix_to_mono(array.array("h", [-1, -2]), 2)) == [-2]


def test_a_partial_frame_is_refused() -> None:
    with pytest.raises(NormalizationError, match="whole frame"):
        downmix_to_mono(array.array("h", [1, 2, 3]), 2)


def test_zero_channels_is_refused() -> None:
    with pytest.raises(NormalizationError, match="at least 1"):
        downmix_to_mono(array.array("h", [1]), 0)


def test_a_dead_channel_halves_the_level_rather_than_silencing_a_speaker() -> None:
    samples = array.array("h", [1000, 0, 2000, 0])
    assert list(downmix_to_mono(samples, 2)) == [500, 1000]


# ===========================================================================
# Resampling
# ===========================================================================


def test_downsampling_produces_the_expected_frame_count() -> None:
    source = _tone(48_000, rate=48_000)
    out, _carry, _position = resample_linear(source, 48_000, 16_000)
    assert abs(len(out) - 16_000) <= 2


def test_upsampling_produces_the_expected_frame_count() -> None:
    source = _tone(8_000, rate=8_000)
    out, _carry, _position = resample_linear(source, 8_000, 16_000)
    assert abs(len(out) - 16_000) <= 2


def test_an_identical_rate_is_a_passthrough() -> None:
    source = array.array("h", [1, 2, 3, 4])
    out, carry, position = resample_linear(source, 16_000, 16_000)
    assert out is source
    assert carry == 4
    assert position == 0.0


def test_a_known_frequency_survives_the_conversion() -> None:
    """The point of resampling: the signal is still the signal afterwards.

    Measured by zero crossings rather than an FFT -- a 440 Hz tone crosses zero 880 times
    a second whatever the sample rate, and that needs no extra dependency.
    """
    source = _tone(48_000, rate=48_000, hz=440.0)
    out, _carry, _position = resample_linear(source, 48_000, 16_000)
    crossings = sum(
        1
        for index in range(1, len(out))
        if (out[index - 1] < 0) != (out[index] < 0)
    )
    assert 870 <= crossings <= 890, crossings


def test_a_blocked_conversion_matches_a_single_shot_one() -> None:
    """Without carrying the boundary state, every block seam becomes a click."""
    source = _tone(48_000, rate=48_000, hz=300.0)
    whole, _c, _p = resample_linear(source, 48_000, 16_000)

    blocked = array.array("h")
    carry: int | None = None
    position = 0.0
    for start in range(0, len(source), 4800):
        chunk = source[start : start + 4800]
        out, carry, position = resample_linear(
            chunk, 48_000, 16_000, carry=carry, position=position
        )
        blocked.extend(out)
    assert abs(len(blocked) - len(whole)) <= 2
    overlap = min(len(blocked), len(whole))
    worst = max(abs(blocked[i] - whole[i]) for i in range(overlap))
    assert worst <= 2, f"block seams introduced a discontinuity of {worst}"


def test_an_empty_block_is_handled() -> None:
    out, carry, position = resample_linear(array.array("h"), 48_000, 16_000)
    assert len(out) == 0
    assert carry is None
    assert position == 0.0


@pytest.mark.parametrize(("source_rate", "target"), [(0, 16_000), (48_000, 0), (-1, 16_000)])
def test_a_non_positive_rate_is_refused(source_rate: int, target: int) -> None:
    with pytest.raises(NormalizationError, match="positive"):
        resample_linear(array.array("h", [1, 2]), source_rate, target)


def test_resampling_never_exceeds_int16() -> None:
    loud = array.array("h", [32767, -32768] * 100)
    out, _carry, _position = resample_linear(loud, 48_000, 16_000)
    assert all(-32768 <= sample <= 32767 for sample in out)


# ===========================================================================
# The stage
# ===========================================================================


def test_a_single_chunk_becomes_a_working_copy(tmp_path: Path) -> None:
    chunk = _write_wav(
        tmp_path / "src" / "chunk-000000.wav",
        _tone(48_000 * 2, rate=48_000, channels=2),
        rate=48_000,
        channels=2,
    )
    result = normalize_recording(
        chunk_paths=[chunk],
        chunk_start_frames=[0],
        chunk_frame_counts=[48_000 * 2],
        target_path=tmp_path / "working" / "out.wav",
        data_root=tmp_path,
    )
    samples, rate, channels = _read(result.path)
    assert rate == WORKING_SAMPLE_RATE
    assert channels == WORKING_CHANNELS
    assert abs(len(samples) - 32_000) <= 4
    assert result.source_sample_rate == 48_000
    assert result.source_channels == 2
    assert abs(result.duration_ms - 2000) <= 2
    assert len(result.sha256) == 64
    assert result.gaps == ()


def test_the_master_is_not_modified(tmp_path: Path) -> None:
    """The one thing this stage must never do."""
    import hashlib

    chunk = _write_wav(
        tmp_path / "src" / "chunk-000000.wav", _tone(16_000, rate=16_000), rate=16_000, channels=1
    )
    before = hashlib.sha256(chunk.read_bytes()).hexdigest()
    normalize_recording(
        chunk_paths=[chunk],
        chunk_start_frames=[0],
        chunk_frame_counts=[16_000],
        target_path=tmp_path / "working" / "out.wav",
        data_root=tmp_path,
    )
    assert hashlib.sha256(chunk.read_bytes()).hexdigest() == before


def test_chunks_are_concatenated_in_frame_order(tmp_path: Path) -> None:
    first = _write_wav(
        tmp_path / "src" / "c0.wav", _tone(16_000, rate=16_000, hz=200.0), rate=16_000, channels=1
    )
    second = _write_wav(
        tmp_path / "src" / "c1.wav", _tone(16_000, rate=16_000, hz=200.0), rate=16_000, channels=1
    )
    result = normalize_recording(
        chunk_paths=[first, second],
        chunk_start_frames=[0, 16_000],
        chunk_frame_counts=[16_000, 16_000],
        target_path=tmp_path / "working" / "out.wav",
        data_root=tmp_path,
    )
    samples, _rate, _channels = _read(result.path)
    assert abs(len(samples) - 32_000) <= 4
    assert result.gaps == ()
    assert result.source_chunk_count == 2


def test_a_gap_between_chunks_is_filled_with_silence_and_recorded(tmp_path: Path) -> None:
    """The timeline must survive a dropped frame, and the gap must stay visible."""
    first = _write_wav(
        tmp_path / "src" / "c0.wav", _tone(16_000, rate=16_000), rate=16_000, channels=1
    )
    second = _write_wav(
        tmp_path / "src" / "c1.wav", _tone(16_000, rate=16_000), rate=16_000, channels=1
    )
    result = normalize_recording(
        chunk_paths=[first, second],
        # The second chunk starts half a second late: 8000 frames are missing.
        chunk_start_frames=[0, 24_000],
        chunk_frame_counts=[16_000, 16_000],
        target_path=tmp_path / "working" / "out.wav",
        data_root=tmp_path,
    )
    assert len(result.gaps) == 1
    gap = result.gaps[0]
    assert gap.reason == "DROPPED_FRAMES_OR_PAUSE"
    assert abs((gap.end_ms - gap.start_ms) - 500) <= 2
    assert abs(result.gap_total_ms - 500) <= 2
    samples, _rate, _channels = _read(result.path)
    assert abs(len(samples) - 40_000) <= 4, "the gap must occupy real time in the copy"
    # And the filled span really is silence.
    silent = samples[16_000 + 10 : 24_000 - 10]
    assert set(silent) == {0}


def test_the_recorded_gap_is_serialisable_for_the_database(tmp_path: Path) -> None:
    first = _write_wav(
        tmp_path / "src" / "c0.wav", _tone(8_000, rate=16_000), rate=16_000, channels=1
    )
    result = normalize_recording(
        chunk_paths=[first],
        chunk_start_frames=[0],
        chunk_frame_counts=[8_000],
        target_path=tmp_path / "working" / "out.wav",
        data_root=tmp_path,
    )
    payload = result.to_dict()
    assert payload["gap_count"] == 0
    assert payload["gaps"] == []
    assert payload["sample_rate_hz"] == WORKING_SAMPLE_RATE
    assert payload["channels"] == WORKING_CHANNELS
    assert payload["sample_format"] == "int16"


def test_an_absent_chunk_becomes_a_recorded_gap_rather_than_a_failure(tmp_path: Path) -> None:
    """One lost chunk must not cost the operator the rest of the meeting."""
    first = _write_wav(
        tmp_path / "src" / "c0.wav", _tone(16_000, rate=16_000), rate=16_000, channels=1
    )
    third = _write_wav(
        tmp_path / "src" / "c2.wav", _tone(16_000, rate=16_000), rate=16_000, channels=1
    )
    result = normalize_recording(
        chunk_paths=[first, tmp_path / "src" / "missing.wav", third],
        chunk_start_frames=[0, 16_000, 32_000],
        chunk_frame_counts=[16_000, 16_000, 16_000],
        target_path=tmp_path / "working" / "out.wav",
        data_root=tmp_path,
    )
    assert result.skipped_chunks == ("missing.wav",)
    assert any(gap.reason == "MISSING_CHUNK" for gap in result.gaps)
    assert abs(result.duration_ms - 3000) <= 4, "the timeline must still be 3 seconds"


def test_an_overlapping_chunk_is_reported_rather_than_silently_trimmed(tmp_path: Path) -> None:
    first = _write_wav(
        tmp_path / "src" / "c0.wav", _tone(16_000, rate=16_000), rate=16_000, channels=1
    )
    second = _write_wav(
        tmp_path / "src" / "c1.wav", _tone(16_000, rate=16_000), rate=16_000, channels=1
    )
    result = normalize_recording(
        chunk_paths=[first, second],
        chunk_start_frames=[0, 8_000],
        chunk_frame_counts=[16_000, 16_000],
        target_path=tmp_path / "working" / "out.wav",
        data_root=tmp_path,
    )
    assert any("overlap" in warning for warning in result.warnings)


def test_a_format_change_mid_recording_is_refused(tmp_path: Path) -> None:
    """A capture cannot change format halfway; the recording is inconsistent."""
    first = _write_wav(
        tmp_path / "src" / "c0.wav", _tone(16_000, rate=16_000), rate=16_000, channels=1
    )
    second = _write_wav(
        tmp_path / "src" / "c1.wav", _tone(48_000, rate=48_000), rate=48_000, channels=1
    )
    with pytest.raises(NormalizationError, match="cannot change format"):
        normalize_recording(
            chunk_paths=[first, second],
            chunk_start_frames=[0, 16_000],
            chunk_frame_counts=[16_000, 48_000],
            target_path=tmp_path / "working" / "out.wav",
            data_root=tmp_path,
        )


def test_an_eight_bit_chunk_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "src" / "c0.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(1)
        handle.setframerate(16_000)
        handle.writeframes(b"\x80" * 1000)
    with pytest.raises(NormalizationError, match="int16 only"):
        normalize_recording(
            chunk_paths=[path],
            chunk_start_frames=[0],
            chunk_frame_counts=[1000],
            target_path=tmp_path / "working" / "out.wav",
            data_root=tmp_path,
        )


def test_no_chunks_at_all_is_refused_with_a_next_step(tmp_path: Path) -> None:
    with pytest.raises(NormalizationError, match="audio verify"):
        normalize_recording(
            chunk_paths=[],
            chunk_start_frames=[],
            chunk_frame_counts=[],
            target_path=tmp_path / "working" / "out.wav",
            data_root=tmp_path,
        )


def test_mismatched_chunk_metadata_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NormalizationError, match="disagrees"):
        normalize_recording(
            chunk_paths=[tmp_path / "a.wav"],
            chunk_start_frames=[0, 1],
            chunk_frame_counts=[1],
            target_path=tmp_path / "working" / "out.wav",
            data_root=tmp_path,
        )


# ===========================================================================
# Durability and provenance
# ===========================================================================


def test_no_partial_file_survives_a_success(tmp_path: Path) -> None:
    chunk = _write_wav(
        tmp_path / "src" / "c0.wav", _tone(16_000, rate=16_000), rate=16_000, channels=1
    )
    target = tmp_path / "working" / "out.wav"
    normalize_recording(
        chunk_paths=[chunk],
        chunk_start_frames=[0],
        chunk_frame_counts=[16_000],
        target_path=target,
        data_root=tmp_path,
    )
    assert list(target.parent.glob("*.part")) == []


def test_no_partial_file_survives_a_failure(tmp_path: Path) -> None:
    """A crash must leave a `.part`, never a half-written file that looks complete."""
    first = _write_wav(
        tmp_path / "src" / "c0.wav", _tone(16_000, rate=16_000), rate=16_000, channels=1
    )
    second = _write_wav(
        tmp_path / "src" / "c1.wav", _tone(48_000, rate=48_000), rate=48_000, channels=1
    )
    target = tmp_path / "working" / "out.wav"
    with pytest.raises(NormalizationError):
        normalize_recording(
            chunk_paths=[first, second],
            chunk_start_frames=[0, 16_000],
            chunk_frame_counts=[16_000, 48_000],
            target_path=target,
            data_root=tmp_path,
        )
    assert list(target.parent.glob("*.part")) == []
    assert not target.exists()


def test_the_digest_is_of_the_bytes_on_disk(tmp_path: Path) -> None:
    import hashlib

    chunk = _write_wav(
        tmp_path / "src" / "c0.wav", _tone(16_000, rate=16_000), rate=16_000, channels=1
    )
    result = normalize_recording(
        chunk_paths=[chunk],
        chunk_start_frames=[0],
        chunk_frame_counts=[16_000],
        target_path=tmp_path / "working" / "out.wav",
        data_root=tmp_path,
    )
    assert result.sha256 == hashlib.sha256(result.path.read_bytes()).hexdigest()
    assert result.size_bytes == result.path.stat().st_size


def test_the_stored_path_is_relative_to_the_data_root(tmp_path: Path) -> None:
    """An absolute path in the database would not survive a restored backup."""
    chunk = _write_wav(
        tmp_path / "src" / "c0.wav", _tone(16_000, rate=16_000), rate=16_000, channels=1
    )
    result = normalize_recording(
        chunk_paths=[chunk],
        chunk_start_frames=[0],
        chunk_frame_counts=[16_000],
        target_path=tmp_path / "working" / "out.wav",
        data_root=tmp_path,
    )
    assert result.relative_path == "working/out.wav"
    assert ":" not in result.relative_path
    assert "\\" not in result.relative_path


def test_a_target_outside_the_data_root_is_refused(tmp_path: Path) -> None:
    chunk = _write_wav(
        tmp_path / "src" / "c0.wav", _tone(16_000, rate=16_000), rate=16_000, channels=1
    )
    outside = tmp_path.parent / f"{tmp_path.name}-outside" / "out.wav"
    with pytest.raises(NormalizationError, match="not inside the data root"):
        normalize_recording(
            chunk_paths=[chunk],
            chunk_start_frames=[0],
            chunk_frame_counts=[16_000],
            target_path=outside,
            data_root=tmp_path,
        )


def test_the_master_manifest_digest_is_carried_through(tmp_path: Path) -> None:
    chunk = _write_wav(
        tmp_path / "src" / "c0.wav", _tone(16_000, rate=16_000), rate=16_000, channels=1
    )
    result = normalize_recording(
        chunk_paths=[chunk],
        chunk_start_frames=[0],
        chunk_frame_counts=[16_000],
        target_path=tmp_path / "working" / "out.wav",
        data_root=tmp_path,
        source_manifest_sha256="ab" * 32,
    )
    assert result.source_manifest_sha256 == "ab" * 32


def test_a_frame_count_that_disagrees_with_the_manifest_is_warned_about(
    tmp_path: Path,
) -> None:
    chunk = _write_wav(
        tmp_path / "src" / "c0.wav", _tone(16_000, rate=16_000), rate=16_000, channels=1
    )
    result = normalize_recording(
        chunk_paths=[chunk],
        chunk_start_frames=[0],
        chunk_frame_counts=[16_000],
        target_path=tmp_path / "working" / "out.wav",
        data_root=tmp_path,
        expected_total_frames=48_000,
    )
    assert any("drift" in warning for warning in result.warnings)


def test_levels_are_measured_over_the_working_copy(tmp_path: Path) -> None:
    chunk = _write_wav(
        tmp_path / "src" / "c0.wav",
        _tone(16_000, rate=16_000, amplitude=0.5),
        rate=16_000,
        channels=1,
    )
    result = normalize_recording(
        chunk_paths=[chunk],
        chunk_start_frames=[0],
        chunk_frame_counts=[16_000],
        target_path=tmp_path / "working" / "out.wav",
        data_root=tmp_path,
    )
    assert result.peak_dbfs is not None and -8.0 < result.peak_dbfs < -4.0
    assert result.rms_dbfs is not None and result.rms_dbfs < result.peak_dbfs
    assert result.clipped_samples == 0


def test_clipping_is_counted(tmp_path: Path) -> None:
    chunk = _write_wav(
        tmp_path / "src" / "c0.wav",
        array.array("h", [32767] * 1000),
        rate=16_000,
        channels=1,
    )
    result = normalize_recording(
        chunk_paths=[chunk],
        chunk_start_frames=[0],
        chunk_frame_counts=[1000],
        target_path=tmp_path / "working" / "out.wav",
        data_root=tmp_path,
    )
    assert result.clipped_samples > 0


def test_rebuilding_produces_identical_bytes(tmp_path: Path) -> None:
    """The working copy is a cache, so re-deriving it must be a no-op in effect."""
    chunk = _write_wav(
        tmp_path / "src" / "c0.wav",
        _tone(48_000, rate=48_000, channels=2),
        rate=48_000,
        channels=2,
    )
    first = normalize_recording(
        chunk_paths=[chunk],
        chunk_start_frames=[0],
        chunk_frame_counts=[48_000],
        target_path=tmp_path / "working" / "a.wav",
        data_root=tmp_path,
    )
    second = normalize_recording(
        chunk_paths=[chunk],
        chunk_start_frames=[0],
        chunk_frame_counts=[48_000],
        target_path=tmp_path / "working" / "b.wav",
        data_root=tmp_path,
    )
    assert first.sha256 == second.sha256


def test_an_existing_working_copy_is_replaced_not_appended(tmp_path: Path) -> None:
    chunk = _write_wav(
        tmp_path / "src" / "c0.wav", _tone(16_000, rate=16_000), rate=16_000, channels=1
    )
    target = tmp_path / "working" / "out.wav"
    for _ in range(2):
        result = normalize_recording(
            chunk_paths=[chunk],
            chunk_start_frames=[0],
            chunk_frame_counts=[16_000],
            target_path=target,
            data_root=tmp_path,
        )
    assert abs(result.duration_ms - 1000) <= 2


def test_the_working_copy_never_lands_in_the_recordings_tree(tmp_path: Path) -> None:
    """Structural: a derived file inside the evidence tree doubles every backup."""
    from mom_igd.paths import RuntimePaths

    paths = RuntimePaths(root=tmp_path)
    working = paths.working_copy_path("11111111-1111-4111-8111-111111111111")
    assert paths.recordings_dir not in working.parents
    assert working.parent == paths.working_dir


def test_a_working_copy_path_refuses_a_non_uuid() -> None:
    from mom_igd.paths import PathValidationError, RuntimePaths

    paths = RuntimePaths(root=Path("D:/nowhere"))
    for bad in ("../escape", "Rapat Direksi", "", "11111111"):
        with pytest.raises(PathValidationError, match="lower-case UUID"):
            paths.working_copy_path(bad)
