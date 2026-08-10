"""Merging pass 2 into pass 1: supersede, never overwrite.

The merge is where evidence is most easily destroyed. Replacing a pass-1 segment's text
in place would look identical from the transcript view and would silently remove the only
record of what the first pass actually said -- which is what "pass 2 improved the flagged
subset" has to be checked against, and what Phase 8's evidence chain depends on.
"""

from __future__ import annotations

from typing import Any

from mom_igd.asr.merge import (
    SUPERSEDED_BY_PASS2,
    SUPERSEDED_BY_PASS2_COVERAGE,
    merge_pass2_into_pass1,
)


def _segment(
    seq: int, region_seq: int | None, start_ms: int, end_ms: int, text: str, **extra: Any
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "seq": seq,
        "region_seq": region_seq,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "text": text,
        "asr_pass": 1,
    }
    row.update(extra)
    return row


# ===========================================================================
# Nothing to merge
# ===========================================================================


def test_no_pass2_result_leaves_pass1_untouched_and_active() -> None:
    pass1 = [_segment(0, 0, 0, 1000, "satu"), _segment(1, 1, 2000, 3000, "dua")]
    result = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=[], replaced_region_seqs=[]
    )
    assert len(result.segments) == 2
    assert all(segment["is_active"] for segment in result.segments)
    assert result.superseded_count == 0
    assert result.replacement_count == 0


def test_a_region_pass2_returned_nothing_for_keeps_its_pass1_text() -> None:
    """Retiring evidence in favour of nothing would lose part of the meeting."""
    pass1 = [_segment(0, 0, 0, 1000, "satu")]
    result = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=[], replaced_region_seqs=[0]
    )
    assert result.segments[0]["is_active"] is True
    assert result.segments[0]["superseded_reason"] is None
    assert result.regions_without_replacement == (0,)
    assert result.regions_replaced == ()


def test_an_empty_pass1_with_a_pass2_result_still_merges() -> None:
    replacement = _segment(0, 0, 0, 1000, "baru", asr_pass=2)
    result = merge_pass2_into_pass1(
        pass1_segments=[], pass2_segments=[replacement], replaced_region_seqs=[0]
    )
    assert len(result.segments) == 1
    assert result.segments[0]["asr_pass"] == 2
    assert result.segments[0]["is_active"] is True


# ===========================================================================
# Supersession
# ===========================================================================


def test_a_replaced_region_retires_its_pass1_segment_but_keeps_the_row() -> None:
    pass1 = [_segment(0, 0, 0, 1000, "salah"), _segment(1, 1, 2000, 3000, "benar")]
    pass2 = [_segment(0, 0, 0, 1000, "diperbaiki", asr_pass=2)]
    result = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=pass2, replaced_region_seqs=[0]
    )
    assert len(result.segments) == 3, "both versions must survive"
    retired = [segment for segment in result.segments if not segment["is_active"]]
    assert len(retired) == 1
    assert retired[0]["text"] == "salah"
    assert retired[0]["superseded_reason"] == SUPERSEDED_BY_PASS2
    assert result.superseded_count == 1
    assert result.replacement_count == 1


def test_an_unreplaced_region_stays_active() -> None:
    pass1 = [_segment(0, 0, 0, 1000, "a"), _segment(1, 1, 2000, 3000, "b")]
    pass2 = [_segment(0, 0, 0, 1000, "a2", asr_pass=2)]
    result = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=pass2, replaced_region_seqs=[0]
    )
    survivors = {
        segment["text"] for segment in result.segments if segment["is_active"]
    }
    assert survivors == {"a2", "b"}


def test_every_pass1_segment_of_a_replaced_region_is_retired() -> None:
    """A region can hold several segments; retiring only the first would duplicate text."""
    pass1 = [
        _segment(0, 0, 0, 1000, "satu"),
        _segment(1, 0, 1000, 2000, "dua"),
        _segment(2, 1, 3000, 4000, "tiga"),
    ]
    pass2 = [_segment(0, 0, 0, 2000, "satu dua", asr_pass=2)]
    result = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=pass2, replaced_region_seqs=[0]
    )
    assert result.superseded_count == 2
    active = [segment["text"] for segment in result.segments if segment["is_active"]]
    assert active == ["satu dua", "tiga"]


def test_a_pass2_segment_is_never_marked_superseded() -> None:
    pass2 = [_segment(0, 0, 0, 1000, "baru", asr_pass=2)]
    result = merge_pass2_into_pass1(
        pass1_segments=[_segment(0, 0, 0, 1000, "lama")],
        pass2_segments=pass2,
        replaced_region_seqs=[0],
    )
    for segment in result.segments:
        if segment["asr_pass"] == 2:
            assert segment["is_active"] is True
            assert segment["superseded_reason"] is None


def test_a_pass2_segment_with_no_region_is_ignored_rather_than_replacing_everything() -> None:
    result = merge_pass2_into_pass1(
        pass1_segments=[_segment(0, 0, 0, 1000, "lama")],
        pass2_segments=[_segment(0, None, 0, 1000, "tanpa region", asr_pass=2)],
        replaced_region_seqs=[0],
    )
    assert result.replacement_count == 0
    assert result.segments[0]["is_active"] is True
    assert result.regions_without_replacement == (0,)


# ===========================================================================
# Ordering and renumbering
# ===========================================================================


def test_the_merged_list_is_in_time_order() -> None:
    pass1 = [
        _segment(0, 0, 5000, 6000, "akhir"),
        _segment(1, 1, 0, 1000, "awal"),
    ]
    result = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=[], replaced_region_seqs=[]
    )
    starts = [segment["start_ms"] for segment in result.segments]
    assert starts == sorted(starts)


def test_a_replacement_sorts_before_the_segment_it_replaced() -> None:
    """Same span, so only the pass tie-break can order them."""
    pass1 = [_segment(0, 0, 1000, 2000, "lama")]
    pass2 = [_segment(0, 0, 1000, 2000, "baru", asr_pass=2)]
    result = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=pass2, replaced_region_seqs=[0]
    )
    assert result.segments[0]["asr_pass"] == 2
    assert result.segments[0]["text"] == "baru"


def test_sequence_numbers_are_contiguous_from_zero_after_merging() -> None:
    pass1 = [_segment(index, index, index * 1000, index * 1000 + 900, f"s{index}") for index in range(5)]
    pass2 = [_segment(0, 2, 2000, 2900, "baru", asr_pass=2)]
    result = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=pass2, replaced_region_seqs=[2]
    )
    assert [segment["seq"] for segment in result.segments] == list(range(6))


def test_merging_is_deterministic_whatever_the_input_order() -> None:
    pass1 = [
        _segment(0, 1, 2000, 3000, "b"),
        _segment(1, 0, 0, 1000, "a"),
        _segment(2, 2, 4000, 5000, "c"),
    ]
    pass2 = [
        _segment(0, 2, 4000, 5000, "c2", asr_pass=2),
        _segment(1, 0, 0, 1000, "a2", asr_pass=2),
    ]
    first = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=pass2, replaced_region_seqs=[0, 2]
    )
    second = merge_pass2_into_pass1(
        pass1_segments=list(reversed(pass1)),
        pass2_segments=list(reversed(pass2)),
        replaced_region_seqs=[2, 0],
    )
    assert [(s["seq"], s["text"], s["is_active"]) for s in first.segments] == [
        (s["seq"], s["text"], s["is_active"]) for s in second.segments
    ]


# ===========================================================================
# Did pass 2 actually change anything?
# ===========================================================================


def test_an_identical_re_transcription_is_reported_as_unchanged() -> None:
    """The number that says whether pass 2 was worth its budget."""
    pass1 = [_segment(0, 0, 0, 1000, "sama saja")]
    pass2 = [_segment(0, 0, 0, 1000, "sama saja", asr_pass=2)]
    result = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=pass2, replaced_region_seqs=[0]
    )
    assert result.text_changed_regions == ()
    assert result.regions_replaced == (0,)


def test_a_different_re_transcription_is_reported_as_changed() -> None:
    pass1 = [_segment(0, 0, 0, 1000, "kira kira")]
    pass2 = [_segment(0, 0, 0, 1000, "kira-kira begitu", asr_pass=2)]
    result = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=pass2, replaced_region_seqs=[0]
    )
    assert result.text_changed_regions == (0,)


def test_whitespace_and_case_alone_do_not_count_as_a_change() -> None:
    """Otherwise every region would look improved and the metric would be worthless."""
    pass1 = [_segment(0, 0, 0, 1000, "Rapat  mingguan")]
    pass2 = [_segment(0, 0, 0, 1000, "rapat mingguan\n", asr_pass=2)]
    result = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=pass2, replaced_region_seqs=[0]
    )
    assert result.text_changed_regions == ()


def test_several_segments_are_compared_as_one_region() -> None:
    pass1 = [_segment(0, 0, 0, 900, "satu"), _segment(1, 0, 900, 1800, "dua")]
    pass2 = [_segment(0, 0, 0, 1800, "satu dua", asr_pass=2)]
    result = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=pass2, replaced_region_seqs=[0]
    )
    assert result.text_changed_regions == ()


# ===========================================================================
# The report
# ===========================================================================


def test_the_summary_carries_counts_and_no_transcript_text() -> None:
    result = merge_pass2_into_pass1(
        pass1_segments=[_segment(0, 0, 0, 1000, "rahasia")],
        pass2_segments=[_segment(0, 0, 0, 1000, "juga rahasia", asr_pass=2)],
        replaced_region_seqs=[0],
    )
    payload = result.to_dict()
    assert payload["segment_count"] == 2
    assert payload["active_segment_count"] == 1
    assert payload["superseded_count"] == 1
    assert "rahasia" not in repr(payload)


def test_the_active_view_excludes_superseded_segments() -> None:
    result = merge_pass2_into_pass1(
        pass1_segments=[_segment(0, 0, 0, 1000, "lama")],
        pass2_segments=[_segment(0, 0, 0, 1000, "baru", asr_pass=2)],
        replaced_region_seqs=[0],
    )
    assert [segment["text"] for segment in result.active_segments] == ["baru"]


def test_the_input_segments_are_not_mutated() -> None:
    """The caller may still need the pass-1 list, for example to report on selection."""
    pass1 = [_segment(0, 0, 0, 1000, "lama")]
    original = dict(pass1[0])
    merge_pass2_into_pass1(
        pass1_segments=pass1,
        pass2_segments=[_segment(0, 0, 0, 1000, "baru", asr_pass=2)],
        replaced_region_seqs=[0],
    )
    assert pass1[0] == original


# ===========================================================================
# A pass-2 segment that spans more than the region it was filed under
# ===========================================================================


def test_a_pass1_segment_covered_by_a_neighbours_replacement_is_retired() -> None:
    """The duplication measured on a real meeting.

    Pass 2 groups regions into thirty-second windows, so the engine emits segments that
    cross region boundaries and `attribute_to_region` files each under one region.
    Observed: a pass-2 segment filed under region 3 spanned 7.74s..32.96s and covered
    regions 0, 1, 3, 4 and 5. Retiring by region alone left four pass-1 segments active
    inside it, so the same sentences appeared twice -- and would have become duplicate
    points in the minutes.
    """
    pass1 = [
        {"seq": 0, "region_seq": 0, "start_ms": 7_740, "end_ms": 12_220, "text": "a"},
        {"seq": 1, "region_seq": 1, "start_ms": 13_020, "end_ms": 16_360, "text": "b"},
        {"seq": 2, "region_seq": 3, "start_ms": 17_080, "end_ms": 23_660, "text": "c"},
        {"seq": 3, "region_seq": 4, "start_ms": 27_180, "end_ms": 29_980, "text": "d"},
        {"seq": 4, "region_seq": 5, "start_ms": 30_420, "end_ms": 32_900, "text": "e"},
        # Well clear of the replacement: nothing re-transcribed this.
        {"seq": 5, "region_seq": 9, "start_ms": 53_100, "end_ms": 59_700, "text": "f"},
    ]
    pass2 = [
        {"seq": 0, "region_seq": 3, "start_ms": 7_740, "end_ms": 32_960, "text": "abcde"},
    ]

    result = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=pass2, replaced_region_seqs=[3]
    )
    active = {segment["text"] for segment in result.active_segments}
    assert active == {"abcde", "f"}, (
        "every pass-1 segment inside the replacement must be retired, and the one "
        "outside it must survive"
    )
    assert result.coverage_supersessions == 4


def test_the_two_supersession_reasons_stay_distinguishable() -> None:
    """Filed-under-this-region and covered-by-a-neighbour are different facts."""
    pass1 = [
        {"seq": 0, "region_seq": 1, "start_ms": 0, "end_ms": 2_000, "text": "a"},
        {"seq": 1, "region_seq": 2, "start_ms": 2_000, "end_ms": 4_000, "text": "b"},
    ]
    pass2 = [{"seq": 0, "region_seq": 1, "start_ms": 0, "end_ms": 4_000, "text": "ab"}]

    result = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=pass2, replaced_region_seqs=[1]
    )
    reasons = {
        segment["text"]: segment["superseded_reason"]
        for segment in result.segments
        if not segment["is_active"]
    }
    assert reasons == {
        "a": SUPERSEDED_BY_PASS2,
        "b": SUPERSEDED_BY_PASS2_COVERAGE,
    }


def test_a_barely_touched_pass1_segment_survives() -> None:
    """Overlapping by a sliver is not "this audio has a newer transcription"."""
    pass1 = [{"seq": 0, "region_seq": 5, "start_ms": 10_000, "end_ms": 20_000, "text": "keep"}]
    pass2 = [{"seq": 0, "region_seq": 1, "start_ms": 8_000, "end_ms": 11_000, "text": "x"}]

    result = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=pass2, replaced_region_seqs=[1]
    )
    assert [segment["text"] for segment in result.active_segments] == ["x", "keep"]
    assert result.coverage_supersessions == 0


def test_a_region_pass_two_ignored_entirely_keeps_its_text() -> None:
    """The original guarantee, unchanged: nothing is retired in favour of nothing."""
    pass1 = [{"seq": 0, "region_seq": 7, "start_ms": 0, "end_ms": 5_000, "text": "only"}]
    result = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=[], replaced_region_seqs=[7]
    )
    assert [segment["text"] for segment in result.active_segments] == ["only"]
    assert result.regions_without_replacement == (7,)
    assert result.coverage_supersessions == 0


def test_overlapping_pass2_segments_do_not_double_count_coverage() -> None:
    """Summing raw intersections would exceed 100% and retire on meaningless arithmetic."""
    pass1 = [{"seq": 0, "region_seq": 4, "start_ms": 0, "end_ms": 10_000, "text": "keep"}]
    # Two pass-2 segments covering the same 3 seconds: 30% of the pass-1 span, not 60%.
    pass2 = [
        {"seq": 0, "region_seq": 1, "start_ms": 0, "end_ms": 3_000, "text": "x"},
        {"seq": 1, "region_seq": 1, "start_ms": 0, "end_ms": 3_000, "text": "y"},
    ]
    result = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=pass2, replaced_region_seqs=[1]
    )
    assert "keep" in {segment["text"] for segment in result.active_segments}


def test_a_retired_segment_is_kept_not_deleted() -> None:
    """What makes the majority rule safe: nothing is destroyed, only deactivated."""
    pass1 = [{"seq": 0, "region_seq": 2, "start_ms": 0, "end_ms": 4_000, "text": "old"}]
    pass2 = [{"seq": 0, "region_seq": 1, "start_ms": 0, "end_ms": 4_000, "text": "new"}]
    result = merge_pass2_into_pass1(
        pass1_segments=pass1, pass2_segments=pass2, replaced_region_seqs=[1]
    )
    assert len(result.segments) == 2
    retired = [segment for segment in result.segments if not segment["is_active"]]
    assert len(retired) == 1 and retired[0]["text"] == "old"
