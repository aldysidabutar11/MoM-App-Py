"""Pass-2 selection: which regions get a second pass, why, and what the budget buys.

Selection decides where a slower model spends its time, and it is the stage most able to
be quietly wrong: a rule that never fires, a budget that is silently unspendable, or a
reason code that does not match the rule that produced it all look like success from the
outside.

Four defects found by running this against a real recording are pinned here as named
tests, because every one of them passed a plausible-looking earlier test:

* a region with no *attributed* segment was called empty even when a segment covered its
  span, which flagged nine regions falsely and spent the whole budget on audio that had
  text;
* one over-budget region at the top of the ranking stopped the loop and blocked every
  region behind it, so nothing at all ran;
* segments arrived with no region attribution at all, which made every region look empty;
* "nothing needed re-transcribing" and "the budget could not cover anything" were
  reported as the same outcome.
"""

from __future__ import annotations

from typing import Any

import pytest

from mom_igd.asr.selection import (
    REASON_CODES,
    SelectionPolicy,
    select_regions_for_pass2,
)


def _region(seq: int, start_ms: int, end_ms: int) -> dict[str, Any]:
    return {"seq": seq, "start_ms": start_ms, "end_ms": end_ms}


def _segment(
    seq: int,
    region_seq: int | None,
    *,
    start_ms: int = 0,
    end_ms: int = 1000,
    text: str = "kata",
    avg_logprob: float | None = -0.3,
    no_speech_prob: float | None = 0.05,
    compression_ratio: float | None = 1.5,
    temperature: float | None = 0.0,
    word_probability: float | None = 0.9,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "region_seq": region_seq,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "text": text,
        "avg_logprob": avg_logprob,
        "no_speech_prob": no_speech_prob,
        "compression_ratio": compression_ratio,
        "temperature": temperature,
        "words": (
            [{"text": "kata", "probability": word_probability}]
            if word_probability is not None
            else []
        ),
    }


GENEROUS = SelectionPolicy(budget_ratio=1.0)


# ===========================================================================
# The rules
# ===========================================================================


def test_a_confident_region_is_not_selected() -> None:
    result = select_regions_for_pass2(
        segments=[_segment(0, 0)], regions=[_region(0, 0, 1000)], policy=GENEROUS
    )
    assert result.flagged == ()
    assert result.selected == ()
    assert result.regions[0].reason_codes == ()


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("avg_logprob", -1.5, "LOW_AVG_LOGPROB"),
        ("compression_ratio", 3.0, "REPETITION_SUSPECTED"),
        ("temperature", 0.4, "DECODER_FELL_BACK"),
        ("word_probability", 0.2, "LOW_WORD_CONFIDENCE"),
        ("no_speech_prob", 0.9, "HIGH_NO_SPEECH_PROB"),
    ],
)
def test_each_rule_fires_on_its_own_signal(field: str, value: float, expected: str) -> None:
    segment = _segment(0, 0, **{field: value})
    result = select_regions_for_pass2(
        segments=[segment], regions=[_region(0, 0, 1000)], policy=GENEROUS
    )
    assert expected in result.regions[0].reason_codes
    assert result.regions[0].selected is True


def test_a_region_with_nothing_covering_it_is_flagged_as_empty() -> None:
    result = select_regions_for_pass2(
        segments=[], regions=[_region(0, 0, 2000)], policy=GENEROUS
    )
    assert result.regions[0].reason_codes == ("EMPTY_IN_SPEECH_REGION",)


def test_a_region_covered_by_a_segment_attributed_elsewhere_is_not_empty() -> None:
    """The nine-false-flags defect.

    Regions are decoded in batched 30-second windows, so one long segment can span
    several regions and is attributed to the one it overlaps most. The others are not
    empty -- there is text over them -- and flagging them wasted the entire budget on
    re-transcribing audio that had already been transcribed.
    """
    segments = [_segment(0, 2, start_ms=0, end_ms=24_000)]
    regions = [_region(seq, seq * 2500, seq * 2500 + 2000) for seq in range(10)]
    result = select_regions_for_pass2(
        segments=segments, regions=regions, policy=GENEROUS
    )
    empty = [
        region.region_seq
        for region in result.regions
        if "EMPTY_IN_SPEECH_REGION" in region.reason_codes
    ]
    assert empty == [], f"regions {empty} were called empty while a segment covered them"


def test_a_region_outside_every_segment_is_still_flagged_as_empty() -> None:
    """The coverage check must not become a blanket excuse."""
    segments = [_segment(0, 0, start_ms=0, end_ms=2000)]
    regions = [_region(0, 0, 2000), _region(1, 50_000, 52_000)]
    result = select_regions_for_pass2(
        segments=segments, regions=regions, policy=GENEROUS
    )
    verdicts = {region.region_seq: region.reason_codes for region in result.regions}
    assert verdicts[1] == ("EMPTY_IN_SPEECH_REGION",)
    assert verdicts[0] == ()


def test_an_empty_text_with_a_segment_present_still_fires() -> None:
    result = select_regions_for_pass2(
        segments=[_segment(0, 0, text="   ")],
        regions=[_region(0, 0, 1000)],
        policy=GENEROUS,
    )
    assert "EMPTY_IN_SPEECH_REGION" in result.regions[0].reason_codes


def test_high_no_speech_probability_with_no_text_is_not_double_reported() -> None:
    """An empty segment is already the stronger finding; two codes would double-count."""
    result = select_regions_for_pass2(
        segments=[_segment(0, 0, text="", no_speech_prob=0.95)],
        regions=[_region(0, 0, 1000)],
        policy=GENEROUS,
    )
    assert "HIGH_NO_SPEECH_PROB" not in result.regions[0].reason_codes


def test_a_missing_signal_does_not_fire_a_rule() -> None:
    """`None` means the decoder did not report it, not that it reported something bad."""
    result = select_regions_for_pass2(
        segments=[
            _segment(
                0,
                0,
                avg_logprob=None,
                no_speech_prob=None,
                compression_ratio=None,
                temperature=None,
                word_probability=None,
            )
        ],
        regions=[_region(0, 0, 1000)],
        policy=GENEROUS,
    )
    assert result.regions[0].reason_codes == ()


def test_a_threshold_is_exclusive_at_the_boundary() -> None:
    policy = SelectionPolicy(budget_ratio=1.0, min_avg_logprob=-1.0)
    at = select_regions_for_pass2(
        segments=[_segment(0, 0, avg_logprob=-1.0)],
        regions=[_region(0, 0, 1000)],
        policy=policy,
    )
    below = select_regions_for_pass2(
        segments=[_segment(0, 0, avg_logprob=-1.0001)],
        regions=[_region(0, 0, 1000)],
        policy=policy,
    )
    assert at.regions[0].reason_codes == ()
    assert "LOW_AVG_LOGPROB" in below.regions[0].reason_codes


def test_several_rules_can_fire_on_one_region() -> None:
    result = select_regions_for_pass2(
        segments=[_segment(0, 0, avg_logprob=-2.0, compression_ratio=5.0, temperature=0.6)],
        regions=[_region(0, 0, 1000)],
        policy=GENEROUS,
    )
    assert set(result.regions[0].reason_codes) == {
        "LOW_AVG_LOGPROB",
        "REPETITION_SUSPECTED",
        "DECODER_FELL_BACK",
    }


def test_reason_codes_come_back_in_table_order_whatever_the_segment_order() -> None:
    """Two runs over the same audio must produce byte-identical reason lists."""
    forward = select_regions_for_pass2(
        segments=[
            _segment(0, 0, avg_logprob=-2.0, end_ms=500),
            _segment(1, 0, temperature=0.5, start_ms=500),
        ],
        regions=[_region(0, 0, 1000)],
        policy=GENEROUS,
    )
    reversed_order = select_regions_for_pass2(
        segments=[
            _segment(0, 0, temperature=0.5, start_ms=500),
            _segment(1, 0, avg_logprob=-2.0, end_ms=500),
        ],
        regions=[_region(0, 0, 1000)],
        policy=GENEROUS,
    )
    assert forward.regions[0].reason_codes == reversed_order.regions[0].reason_codes
    assert list(forward.regions[0].reason_codes) == [
        code for code in REASON_CODES if code in forward.regions[0].reason_codes
    ]


def test_every_reason_code_has_a_weight_and_an_explanation() -> None:
    for code, (weight, explanation) in REASON_CODES.items():
        assert code.isupper(), code
        assert weight > 0, code
        assert len(explanation) > 20, code


# ===========================================================================
# Ranking and budget
# ===========================================================================


def test_the_worst_region_is_ranked_first() -> None:
    regions = [_region(0, 0, 1000), _region(1, 2000, 3000)]
    segments = [
        _segment(0, 0, start_ms=0, end_ms=1000, no_speech_prob=0.9),
        _segment(1, 1, start_ms=2000, end_ms=3000, temperature=0.5, compression_ratio=9.0),
    ]
    result = select_regions_for_pass2(
        segments=segments, regions=regions, policy=GENEROUS
    )
    by_seq = {region.region_seq: region for region in result.regions}
    assert by_seq[1].rank == 0
    assert by_seq[0].rank == 1
    assert by_seq[1].score > by_seq[0].score


def test_a_tie_breaks_on_position_in_the_meeting() -> None:
    regions = [_region(0, 5000, 6000), _region(1, 0, 1000)]
    segments = [
        _segment(0, 0, start_ms=5000, end_ms=6000, avg_logprob=-2.0),
        _segment(1, 1, start_ms=0, end_ms=1000, avg_logprob=-2.0),
    ]
    result = select_regions_for_pass2(
        segments=segments, regions=regions, policy=GENEROUS
    )
    ranks = {region.region_seq: region.rank for region in result.regions}
    assert ranks[1] == 0, "the earlier region must win a tie"
    assert ranks[0] == 1


def test_the_budget_is_a_fraction_of_detected_speech() -> None:
    regions = [_region(seq, seq * 10_000, seq * 10_000 + 8000) for seq in range(4)]
    result = select_regions_for_pass2(
        segments=[], regions=regions, policy=SelectionPolicy(budget_ratio=0.25)
    )
    assert result.speech_ms == 32_000
    assert result.budget_ms == 8000


def test_the_budget_is_not_exceeded() -> None:
    regions = [_region(seq, seq * 5000, seq * 5000 + 4000) for seq in range(10)]
    result = select_regions_for_pass2(
        segments=[], regions=regions, policy=SelectionPolicy(budget_ratio=0.25)
    )
    assert result.budget_ms == 10_000
    assert result.selected_ms <= result.budget_ms
    assert len(result.selected) == 2


def test_exhausting_the_budget_is_reported_not_hidden() -> None:
    regions = [_region(seq, seq * 5000, seq * 5000 + 4000) for seq in range(10)]
    result = select_regions_for_pass2(
        segments=[], regions=regions, policy=SelectionPolicy(budget_ratio=0.25)
    )
    assert result.budget_exhausted is True
    assert len(result.flagged) == 10
    assert len(result.selected) < len(result.flagged)


def test_an_over_budget_region_does_not_block_the_ones_behind_it() -> None:
    """The blocked-pass defect.

    The first-ranked region was 6.0 s against a 5.3 s budget. An earlier version stopped
    at the first region that did not fit, so nothing ran at all -- while nine smaller
    flagged regions waited behind it. Priority cannot promise that a region larger than
    the whole budget will run.
    """
    regions = [_region(0, 0, 9000), _region(1, 10_000, 11_000), _region(2, 12_000, 13_000)]
    segments = [
        _segment(0, 0, start_ms=0, end_ms=9000, temperature=0.9, compression_ratio=9.0),
        _segment(1, 1, start_ms=10_000, end_ms=11_000, avg_logprob=-2.0),
        _segment(2, 2, start_ms=12_000, end_ms=13_000, avg_logprob=-2.0),
    ]
    result = select_regions_for_pass2(
        segments=segments, regions=regions, policy=SelectionPolicy(budget_ratio=0.25)
    )
    assert result.budget_ms == 2750
    selected = {region.region_seq for region in result.selected}
    assert selected == {1, 2}, "the smaller flagged regions must still run"
    assert result.budget_exhausted is True


def test_a_zero_budget_selects_nothing_but_still_flags() -> None:
    """The distinction the pipeline needs to tell the operator what to change."""
    regions = [_region(0, 0, 4000)]
    segments = [_segment(0, 0, end_ms=4000, avg_logprob=-2.0)]
    result = select_regions_for_pass2(
        segments=segments, regions=regions, policy=SelectionPolicy(budget_ratio=0.0)
    )
    assert result.selected == ()
    assert len(result.flagged) == 1
    assert result.budget_exhausted is True


def test_only_flagged_regions_are_ever_selected() -> None:
    regions = [_region(0, 0, 1000), _region(1, 2000, 3000)]
    segments = [
        _segment(0, 0, start_ms=0, end_ms=1000),
        _segment(1, 1, start_ms=2000, end_ms=3000, avg_logprob=-2.0),
    ]
    result = select_regions_for_pass2(
        segments=segments, regions=regions, policy=GENEROUS
    )
    assert {region.region_seq for region in result.selected} == {1}


# ===========================================================================
# Determinism
# ===========================================================================


def test_the_same_input_selects_the_same_regions_every_time() -> None:
    regions = [_region(seq, seq * 3000, seq * 3000 + 2000) for seq in range(8)]
    segments = [
        _segment(
            seq,
            seq,
            start_ms=seq * 3000,
            end_ms=seq * 3000 + 2000,
            avg_logprob=-0.5 - seq * 0.2,
            word_probability=0.9 - seq * 0.1,
        )
        for seq in range(8)
    ]
    first = select_regions_for_pass2(
        segments=segments, regions=regions, policy=SelectionPolicy(budget_ratio=0.3)
    )
    second = select_regions_for_pass2(
        segments=list(reversed(segments)),
        regions=list(reversed(regions)),
        policy=SelectionPolicy(budget_ratio=0.3),
    )
    assert [region.to_dict() for region in first.selected] == [
        region.to_dict() for region in second.selected
    ]


def test_the_verdict_list_covers_every_region_in_meeting_order() -> None:
    """Canonical order, not the caller's: a serialised result must not vary with it."""
    regions = [_region(seq, seq * 2000, seq * 2000 + 1000) for seq in range(5)]
    result = select_regions_for_pass2(segments=[], regions=regions, policy=GENEROUS)
    assert [region.region_seq for region in result.regions] == [0, 1, 2, 3, 4]
    shuffled = select_regions_for_pass2(
        segments=[], regions=list(reversed(regions)), policy=GENEROUS
    )
    assert [region.region_seq for region in shuffled.regions] == [0, 1, 2, 3, 4]


# ===========================================================================
# Off, and empty
# ===========================================================================


def test_a_disabled_policy_selects_nothing_and_says_so() -> None:
    result = select_regions_for_pass2(
        segments=[_segment(0, 0, avg_logprob=-9.0)],
        regions=[_region(0, 0, 1000)],
        policy=SelectionPolicy(enabled=False),
    )
    assert result.selected == ()
    assert result.skipped_reason and "disabled" in result.skipped_reason


def test_no_regions_means_nothing_to_re_transcribe() -> None:
    result = select_regions_for_pass2(segments=[], regions=[], policy=GENEROUS)
    assert result.selected == ()
    assert result.speech_ms == 0
    assert result.skipped_reason and "no speech regions" in result.skipped_reason


def test_an_unattributed_segment_still_contributes_its_signals() -> None:
    """The attribution defect made every segment look like this.

    Attribution is an optimisation, not a correctness dependency: a segment that could
    not be attributed to a region still overlaps one, and its confidence signals are
    read from there rather than thrown away.
    """
    result = select_regions_for_pass2(
        segments=[_segment(0, None, end_ms=1000, avg_logprob=-2.0)],
        regions=[_region(0, 0, 1000)],
        policy=GENEROUS,
    )
    assert "LOW_AVG_LOGPROB" in result.regions[0].reason_codes
    assert "EMPTY_IN_SPEECH_REGION" not in result.regions[0].reason_codes


def test_a_region_the_unattributed_segment_does_not_reach_is_still_empty() -> None:
    result = select_regions_for_pass2(
        segments=[_segment(0, None, end_ms=1000)],
        regions=[_region(0, 0, 1000), _region(1, 90_000, 91_000)],
        policy=GENEROUS,
    )
    verdicts = {region.region_seq: region.reason_codes for region in result.regions}
    assert verdicts[1] == ("EMPTY_IN_SPEECH_REGION",)


# ===========================================================================
# The policy comes from configuration, never from defaults here
# ===========================================================================


def test_the_policy_is_built_from_configuration() -> None:
    from mom_igd.config import load_config

    config = load_config(use_local_file=False)
    policy = SelectionPolicy.from_config(config)
    assert policy.enabled == config.asr.pass2_enabled
    assert policy.budget_ratio == config.asr.pass2_budget_ratio
    assert policy.min_avg_logprob == config.asr.pass2_min_avg_logprob
    assert policy.max_no_speech_prob == config.asr.pass2_max_no_speech_prob
    assert policy.min_word_probability == config.asr.pass2_min_word_probability
    assert policy.max_compression_ratio == config.asr.pass2_max_compression_ratio


def test_the_builtin_defaults_match_the_shipped_configuration() -> None:
    """Two runtimes must not disagree about the same policy."""
    from mom_igd.config import load_config

    config = load_config(use_local_file=False)
    assert SelectionPolicy() == SelectionPolicy.from_config(config)


def test_the_serialised_policy_is_reportable() -> None:
    payload = SelectionPolicy().to_dict()
    assert set(payload) == {
        "enabled",
        "budget_ratio",
        "min_avg_logprob",
        "max_no_speech_prob",
        "min_word_probability",
        "max_compression_ratio",
    }


def test_the_result_carries_no_transcript_text() -> None:
    """It goes into a job payload and a status response."""
    result = select_regions_for_pass2(
        segments=[_segment(0, 0, text="rahasia perusahaan", avg_logprob=-2.0)],
        regions=[_region(0, 0, 1000)],
        policy=GENEROUS,
    )
    assert "rahasia" not in repr(result.to_dict())
