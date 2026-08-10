"""How the working copy is read, held and sliced — the code that decides the real-time factor.

Three versions of this were wrong, each in a way that was invisible on a short recording
and ruinous on a real meeting. All three are pinned here.

1. **One decode per region.** Whisper pads every window to 30 seconds, so decoding a
   two-second region costs what decoding thirty costs. Measured RTF 2.8 against a target
   of 1.0; batching gave 0.31.
2. **The file path passed per region.** faster-whisper then re-read and re-converted the
   *whole file* on every call.
3. **The file read once per `transcribe` call.** Better, and still O(windows x duration),
   because the pipeline calls `transcribe` once per window: 144 windows on a 90-minute
   meeting, 7.6 s each, **18 minutes of waste against a 13-minute decode**.

Every one of them survived a 24-second end-to-end test, which produces exactly one window.
So these tests assert the *property* — how many times the file is read, and how many
windows a region list collapses into — rather than a duration, which would be flaky.
"""

from __future__ import annotations

import array
import gc
import math
import struct
import wave
import weakref
from pathlib import Path
from typing import Any

import pytest

from mom_igd.asr import faster_whisper_provider as fwp
from mom_igd.asr.faster_whisper_provider import (
    WHISPER_WINDOW_SECONDS,
    WORKING_SAMPLE_RATE,
    FasterWhisperProvider,
    _audio_fingerprint,
    _load_working_copy,
    attribute_to_region,
    group_regions_into_windows,
)
from mom_igd.asr.provider import SpeechRegion


def _working_copy(path: Path, seconds: float = 2.0, hz: float = 220.0) -> Path:
    """A real 16 kHz mono PCM16 file. Arithmetic, never a recording of anybody."""
    frames = array.array("h")
    for index in range(int(seconds * WORKING_SAMPLE_RATE)):
        frames.append(int(8000 * math.sin(2 * math.pi * hz * index / WORKING_SAMPLE_RATE)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(WORKING_SAMPLE_RATE)
        handle.writeframes(frames.tobytes())
    return path


def _bare_provider() -> FasterWhisperProvider:
    """A provider with no model. `_audio_for` never touches one."""
    return FasterWhisperProvider(object())


# ===========================================================================
# The file is read once, and never the wrong one
# ===========================================================================


def test_the_working_copy_is_read_once_across_many_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect: 144 reads on a 90-minute meeting, 18 minutes of pure waste."""
    audio = _working_copy(tmp_path / "wc.wav")
    reads = {"n": 0}
    real = fwp._load_working_copy

    def counting(path: Path, *, fingerprint: Any) -> Any:
        reads["n"] += 1
        return real(path, fingerprint=fingerprint)

    monkeypatch.setattr(fwp, "_load_working_copy", counting)

    provider = _bare_provider()
    for _ in range(144):
        provider._audio_for(audio)

    assert reads["n"] == 1, f"the file was read {reads['n']} times"


def test_a_different_file_is_read_rather_than_served_from_the_cache(
    tmp_path: Path,
) -> None:
    first = _working_copy(tmp_path / "a.wav", 1.0, hz=200.0)
    second = _working_copy(tmp_path / "b.wav", 2.0, hz=400.0)
    provider = _bare_provider()
    assert provider._audio_for(first).seconds == pytest.approx(1.0)
    assert provider._audio_for(second).seconds == pytest.approx(2.0)


def test_a_rewritten_file_at_the_same_path_is_re_read(tmp_path: Path) -> None:
    """The dangerous case: a stale cache would transcribe a different meeting.

    The result would look entirely plausible, which is exactly why the cache key is the
    file's identity and not only its name.
    """
    audio = tmp_path / "wc.wav"
    _working_copy(audio, 1.0)
    provider = _bare_provider()
    assert provider._audio_for(audio).seconds == pytest.approx(1.0)

    _working_copy(audio, 3.0)
    # A rewrite within the same clock tick would leave mtime unchanged; the size differs
    # here, and both are part of the key.
    assert provider._audio_for(audio).seconds == pytest.approx(3.0)


def test_the_fingerprint_covers_path_size_and_modification_time(tmp_path: Path) -> None:
    audio = _working_copy(tmp_path / "wc.wav", 1.0)
    path, size, mtime = _audio_fingerprint(audio)
    assert path == str(audio)
    assert size == audio.stat().st_size
    assert mtime == audio.stat().st_mtime_ns


def test_the_previous_audio_is_released_before_the_next_is_loaded(
    tmp_path: Path,
) -> None:
    """Holding two working copies at once is the difference between fitting and not."""
    first = _working_copy(tmp_path / "a.wav", 1.0)
    second = _working_copy(tmp_path / "b.wav", 1.0)
    provider = _bare_provider()
    # Weak-referencing the sample array itself, not its wrapper: the array is the 165 MB
    # on a 90-minute meeting, and it is the thing that has to be gone.
    dead = weakref.ref(provider._audio_for(first)._samples)
    provider._audio_for(second)
    gc.collect()
    assert dead() is None, "the previous working copy was still referenced"


def test_closing_the_provider_releases_the_audio(tmp_path: Path) -> None:
    audio = _working_copy(tmp_path / "wc.wav", 1.0)
    provider = _bare_provider()
    dead = weakref.ref(provider._audio_for(audio)._samples)
    provider.close()
    gc.collect()
    assert dead() is None
    assert provider._audio is None


def test_closing_twice_is_harmless(tmp_path: Path) -> None:
    provider = _bare_provider()
    provider._audio_for(_working_copy(tmp_path / "wc.wav", 0.5))
    provider.close()
    provider.close()


# ===========================================================================
# What is held, and what a window yields
# ===========================================================================


def test_the_working_copy_is_held_as_int16(tmp_path: Path) -> None:
    """A three-hour copy is 172 MB as int16 and 345 MB as float32, and the pass-2 model
    already needs 1.9 GB of the 2.5 GB budget."""
    audio = _load_working_copy(
        _working_copy(tmp_path / "wc.wav", 1.0),
        fingerprint=("x", 0, 0),
    )
    assert len(audio) == WORKING_SAMPLE_RATE
    assert audio.nbytes == WORKING_SAMPLE_RATE * 2, "two bytes per sample, not four"


def test_a_window_is_float32_in_the_range_the_engine_expects(tmp_path: Path) -> None:
    audio = _load_working_copy(
        _working_copy(tmp_path / "wc.wav", 1.0), fingerprint=("x", 0, 0)
    )
    clip = audio.window(0, WORKING_SAMPLE_RATE)
    assert clip.dtype.name == "float32"
    assert len(clip) == WORKING_SAMPLE_RATE
    assert -1.0 <= float(clip.min()) and float(clip.max()) <= 1.0


def test_a_window_holds_exactly_the_requested_span(tmp_path: Path) -> None:
    audio = _load_working_copy(
        _working_copy(tmp_path / "wc.wav", 2.0), fingerprint=("x", 0, 0)
    )
    assert len(audio.window(0, 16_000)) == 16_000
    assert len(audio.window(16_000, 32_000)) == 16_000
    assert len(audio.window(0, 0)) == 0


def test_a_window_past_the_end_is_truncated_rather_than_padded(tmp_path: Path) -> None:
    """Padding would invent audio; the caller clamps, and this must not undo that."""
    audio = _load_working_copy(
        _working_copy(tmp_path / "wc.wav", 1.0), fingerprint=("x", 0, 0)
    )
    assert len(audio.window(0, 999_999)) == WORKING_SAMPLE_RATE


def test_the_fast_path_matches_the_library_sample_for_sample(tmp_path: Path) -> None:
    """The claim in the docstring, checked rather than asserted.

    Reading the WAV directly instead of handing it to an FFmpeg build is only defensible
    if it produces the same numbers.
    """
    from faster_whisper import decode_audio

    path = _working_copy(tmp_path / "wc.wav", 1.0)
    mine = _load_working_copy(path, fingerprint=("x", 0, 0)).window(
        0, WORKING_SAMPLE_RATE
    )
    theirs = decode_audio(str(path), sampling_rate=WORKING_SAMPLE_RATE)
    assert len(mine) == len(theirs)
    assert float(abs(mine - theirs).max()) == 0.0


def test_a_file_that_is_not_a_working_copy_falls_back_to_the_library(
    tmp_path: Path,
) -> None:
    """The benchmark may be pointed at a corpus file in another format."""
    path = tmp_path / "odd.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44_100)
        handle.writeframes(struct.pack("<h", 1000) * 2 * 44_100)
    audio = _load_working_copy(path, fingerprint=("x", 0, 0))
    clip = audio.window(0, 16_000)
    assert clip.dtype.name == "float32", "the library path already yields float32"
    assert len(clip) == 16_000


# ===========================================================================
# Windowing: the change that took RTF from 2.8 to 0.31
# ===========================================================================


def test_consecutive_short_regions_collapse_into_one_window() -> None:
    regions = [SpeechRegion(index=i, start=i * 3.0, end=i * 3.0 + 2.0) for i in range(9)]
    windows = group_regions_into_windows(regions)
    assert len(windows) == 1
    assert [region.index for region in windows[0][2]] == list(range(9))


def test_no_window_exceeds_whispers_analysis_window() -> None:
    """The whole point: the encoder always consumes 30 s and pads the rest."""
    regions = [SpeechRegion(index=i, start=i * 2.5, end=i * 2.5 + 2.0) for i in range(60)]
    for start, end, _covered in group_regions_into_windows(regions):
        assert end - start <= WHISPER_WINDOW_SECONDS + 1e-9


def test_every_region_lands_in_exactly_one_window() -> None:
    """A dropped region is a silently missing piece of the meeting."""
    regions = [SpeechRegion(index=i, start=i * 4.0, end=i * 4.0 + 3.0) for i in range(40)]
    covered = [
        region.index for _start, _end, group in group_regions_into_windows(regions)
        for region in group
    ]
    assert sorted(covered) == list(range(40))
    assert len(covered) == len(set(covered)), "a region was placed in two windows"


def test_a_long_silence_ends_the_window() -> None:
    regions = [
        SpeechRegion(index=0, start=0.0, end=2.0),
        SpeechRegion(index=1, start=100.0, end=102.0),
    ]
    windows = group_regions_into_windows(regions)
    assert len(windows) == 2


def test_a_region_longer_than_the_window_gets_its_own() -> None:
    """VAD bounds regions at 30 s, so this is the boundary case, not the normal one."""
    regions = [
        SpeechRegion(index=0, start=0.0, end=45.0),
        SpeechRegion(index=1, start=46.0, end=48.0),
    ]
    windows = group_regions_into_windows(regions)
    assert len(windows) == 2
    assert [r.index for r in windows[0][2]] == [0]


def test_windows_are_contiguous_spans_not_concatenated_speech() -> None:
    """Concatenating only the speech would be cheaper and would corrupt every timestamp.

    The span runs from the first region's start to the last one's end, silence included,
    so the returned times still map linearly onto the recording.
    """
    regions = [
        SpeechRegion(index=0, start=1.0, end=3.0),
        SpeechRegion(index=1, start=10.0, end=12.0),
    ]
    (start, end, _covered), = group_regions_into_windows(regions)
    assert (start, end) == (1.0, 12.0)


def test_regions_are_grouped_in_time_order_whatever_order_they_arrive() -> None:
    forward = [SpeechRegion(index=i, start=i * 2.0, end=i * 2.0 + 1.5) for i in range(12)]
    assert group_regions_into_windows(forward) == group_regions_into_windows(
        list(reversed(forward))
    )


def test_no_regions_yields_no_windows() -> None:
    assert group_regions_into_windows([]) == []


# ===========================================================================
# Attribution: which region a decoded segment belongs to
# ===========================================================================


def test_a_segment_is_attributed_to_the_region_it_overlaps_most() -> None:
    covered = (
        SpeechRegion(index=0, start=0.0, end=2.0),
        SpeechRegion(index=1, start=3.0, end=9.0),
    )
    assert attribute_to_region(2.5, 8.0, covered) == 1
    assert attribute_to_region(0.1, 2.4, covered) == 0


def test_a_segment_over_the_silence_between_regions_takes_the_nearest() -> None:
    """Every segment must get an answer: an unattributed one makes its region look empty."""
    covered = (
        SpeechRegion(index=0, start=0.0, end=2.0),
        SpeechRegion(index=1, start=20.0, end=22.0),
    )
    assert attribute_to_region(2.5, 3.0, covered) == 0
    assert attribute_to_region(18.0, 19.0, covered) == 1


def test_a_whole_file_decode_has_no_region_to_attribute_to() -> None:
    assert attribute_to_region(0.0, 10.0, ()) is None


def test_attribution_is_stable_when_two_regions_overlap_equally() -> None:
    covered = (
        SpeechRegion(index=0, start=0.0, end=4.0),
        SpeechRegion(index=1, start=4.0, end=8.0),
    )
    assert attribute_to_region(2.0, 6.0, covered) == attribute_to_region(2.0, 6.0, covered)


# ===========================================================================
# Structure
# ===========================================================================


def test_the_provider_reads_the_audio_through_the_cache_and_nowhere_else() -> None:
    """A second call site would reintroduce the defect quietly."""
    import ast

    source = Path(fwp.__file__).read_text(encoding="utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_load_working_copy"
    ]
    assert len(calls) == 1, "the working copy must be loaded from exactly one place"

    tree = ast.parse(source)
    holder = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_audio_for"
    )
    assert "_load_working_copy" in ast.unparse(holder)
