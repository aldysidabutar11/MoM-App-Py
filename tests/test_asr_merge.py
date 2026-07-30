"""Merging pass 2 into pass 1: supersede, never overwrite.

The merge is where evidence is most easily destroyed. Replacing a pass-1 segment's text
in place would look identical from the transcript view and would silently remove the only
record of what the first pass actually said -- which is what "pass 2 improved the flagged
subset" has to be checked against, and what Phase 8's evidence chain depends on.
"""

from __future__ import annotations

from typing import Any

from mom_igd.asr.merge import SUPERSEDED_BY_PASS2, merge_pass2_into_pass1


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
