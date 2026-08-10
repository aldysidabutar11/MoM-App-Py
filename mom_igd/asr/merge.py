"""Fold pass-2 results into a pass-1 transcript without losing either.

**Supersede, never overwrite.** A pass-2 segment does not replace a pass-1 segment's text;
it becomes a new segment, and the pass-1 segments covering the same region are marked
inactive and pointed at their replacement. Both survive.

That costs rows, and it buys three things nothing else does. A reviewer can see what the
second pass changed, which is the only way to judge whether it helped. The evidence chain
Phase 8 verifies stays intact -- a quotation can be traced to the decode that produced it,
not to whatever the latest write happened to be. And "pass 2 improved the flagged subset",
which is one of the Phase 4 acceptance targets, becomes a checkable claim instead of an
assertion.

**Time is the unit, not the region.** This said the opposite, and asserted that "partial
overlap does not arise, because both passes segment inside the same region boundaries". It
does arise, and the assertion is what let it through.

Pass 2 groups regions into thirty-second windows before decoding, because Whisper pads
every window to thirty seconds regardless (ADR-0016 §3). The engine therefore emits
segments across the whole window, and `attribute_to_region` files each one under a single
region. Measured on a real 135-second meeting: one pass-2 segment attributed to region 3
spanned 7.74s..32.96s, covering regions 0, 1, 3, 4 and 5. Retiring only region 3 left the
pass-1 segments for 0, 1, 4 and 5 active -- so the same sentences appeared twice in the
transcript, and would have reached the minutes as duplicate points.

So a pass-1 segment is retired when pass-2 output covers the majority of its span,
whatever region either of them was filed under. Two reason codes keep the two situations
apart: `SUPERSEDED_BY_PASS2` for a region that was genuinely re-transcribed, and
`SUPERSEDED_BY_PASS2_COVERAGE` for one whose audio was re-transcribed under a neighbour's
name.

**Retiring is not deleting.** The row stays, inactive, pointing at what replaced it, which
is what makes the majority rule safe: the worst case is that a partially-covered segment's
tail is no longer in the active transcript, and it remains one query away. The opposite
error -- the same decision recorded twice in a minute -- is not recoverable by a reader who
was not in the room.

**Order is by time, not by pass.** The merged transcript is renumbered in ascending start
time so a reader gets the meeting in order. Two segments starting at the same millisecond
break the tie on the pass number, so a pass-2 result never sorts after the pass-1 segment it
replaced.

**Nothing is dropped silently.** If pass 2 returned nothing for a region it was asked
about, *and nothing else covers that audio*, the pass-1 segments stay active and the region
is reported in ``regions_without_replacement``. Retiring evidence in favour of nothing would
lose part of the meeting; retiring it in favour of a pass-2 segment that covers the same
seconds is not that, and conflating the two is what produced the duplicates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Mapping, Sequence

from mom_igd.logging_setup import get_logger

__all__ = ["MergeResult", "merge_pass2_into_pass1"]

_LOG = get_logger("asr.merge")

#: Why a pass-1 segment stopped being part of the transcript. Stored as a code so
#: Phase 7's reviewer edits can add their own without changing the shape of the data.
#:
#: The two are kept apart because they answer different questions. The first says pass 2
#: was asked about this region and delivered. The second says pass 2 never filed anything
#: under this region, yet re-transcribed its audio anyway under a neighbouring one -- and
#: a run where the second dominates means region attribution is drifting, which is worth
#: seeing rather than averaging away.
SUPERSEDED_BY_PASS2: Final[str] = "SUPERSEDED_BY_PASS2"
SUPERSEDED_BY_PASS2_COVERAGE: Final[str] = "SUPERSEDED_BY_PASS2_COVERAGE"

#: How much of a pass-1 segment must be covered by pass-2 output before it is retired.
#: A majority, because that is what "this audio has a newer transcription" means. Every
#: duplicate measured was covered completely; the threshold matters only for the ragged
#: edges, and there the retired row is still on disk.
_COVERAGE_TO_SUPERSEDE: Final[float] = 0.5


@dataclass(slots=True)
class MergeResult:
    """The merged segment list, plus what the merge did and did not manage to do."""

    segments: tuple[dict[str, Any], ...] = ()
    superseded_count: int = 0
    replacement_count: int = 0
    regions_replaced: tuple[int, ...] = ()
    regions_without_replacement: tuple[int, ...] = ()
    text_changed_regions: tuple[int, ...] = ()
    #: Pass-1 segments retired because a pass-2 segment filed under a *different* region
    #: covered their audio. Non-zero is normal; large is a signal about attribution.
    coverage_supersessions: int = 0

    @property
    def active_segments(self) -> tuple[dict[str, Any], ...]:
        return tuple(segment for segment in self.segments if segment["is_active"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_count": len(self.segments),
            "active_segment_count": len(self.active_segments),
            "superseded_count": self.superseded_count,
            "replacement_count": self.replacement_count,
            "regions_replaced": list(self.regions_replaced),
            "regions_without_replacement": list(self.regions_without_replacement),
            "text_changed_regions": list(self.text_changed_regions),
            "coverage_supersessions": self.coverage_supersessions,
        }


def _normalised(text: str) -> str:
    """Whitespace-insensitive comparison, for deciding whether pass 2 changed anything."""
    return " ".join(str(text or "").split()).casefold()


def merge_pass2_into_pass1(
    *,
    pass1_segments: Sequence[Mapping[str, Any]],
    pass2_segments: Sequence[Mapping[str, Any]],
    replaced_region_seqs: Sequence[int],
) -> MergeResult:
    """Merge, renumber and report.

    ``replaced_region_seqs`` is what pass 2 was *asked* to re-transcribe. It is passed
    explicitly rather than inferred from ``pass2_segments`` because a region pass 2
    returned nothing for is exactly the case that must not silently retire pass-1 work --
    and an empty result is indistinguishable from "never asked" if the request is not
    recorded.
    """
    asked = {int(seq) for seq in replaced_region_seqs}
    produced: dict[int, list[dict[str, Any]]] = {}
    for segment in pass2_segments:
        region = segment.get("region_seq")
        if region is None:
            continue
        produced.setdefault(int(region), []).append(dict(segment))

    replaced = sorted(region for region in asked if produced.get(region))
    orphaned = sorted(region for region in asked if not produced.get(region))

    merged: list[dict[str, Any]] = []
    changed: list[int] = []

    # What pass 2 actually produced, as a timeline. Its own segments can overlap each
    # other, so the spans are merged before anything is measured against them.
    covered_spans = _merged_spans(
        (int(segment.get("start_ms", 0)), int(segment.get("end_ms", 0)))
        for segments in produced.values()
        for segment in segments
    )
    replaced_regions = set(replaced)
    coverage_supersessions = 0

    # Pass-1 segments first, retiring the ones pass 2 has genuinely superseded -- by
    # region where it was filed there, and by clock where it was not.
    for segment in pass1_segments:
        row = dict(segment)
        region = row.get("region_seq")
        by_region = region is not None and int(region) in replaced_regions
        by_coverage = False
        if not by_region:
            by_coverage = (
                _covered_fraction(
                    int(row.get("start_ms", 0)), int(row.get("end_ms", 0)), covered_spans
                )
                >= _COVERAGE_TO_SUPERSEDE
            )
            if by_coverage:
                coverage_supersessions += 1
        row["asr_pass"] = int(row.get("asr_pass", 1))
        row["is_active"] = not (by_region or by_coverage)
        row["superseded_reason"] = (
            SUPERSEDED_BY_PASS2
            if by_region
            else SUPERSEDED_BY_PASS2_COVERAGE
            if by_coverage
            else None
        )
        merged.append(row)

    for region in replaced:
        before = _normalised(
            " ".join(
                str(segment.get("text") or "")
                for segment in pass1_segments
                if segment.get("region_seq") is not None
                and int(segment["region_seq"]) == region
            )
        )
        after = _normalised(
            " ".join(str(segment.get("text") or "") for segment in produced[region])
        )
        if before != after:
            changed.append(region)
        for segment in produced[region]:
            segment["asr_pass"] = 2
            segment["is_active"] = True
            segment["superseded_reason"] = None
            merged.append(segment)

    # Time order, with the pass number as the tie-break so a replacement never sorts
    # behind what it replaced. `seq` is the last key so the result is fully determined
    # even for two identical spans.
    merged.sort(
        key=lambda segment: (
            int(segment["start_ms"]),
            int(segment["end_ms"]),
            -int(segment.get("asr_pass", 1)),
            int(segment.get("seq", 0)),
        )
    )
    for index, segment in enumerate(merged):
        segment["seq"] = index

    result = MergeResult(
        segments=tuple(merged),
        superseded_count=sum(1 for segment in merged if not segment["is_active"]),
        replacement_count=sum(
            1 for segment in merged if int(segment.get("asr_pass", 1)) == 2
        ),
        regions_replaced=tuple(replaced),
        regions_without_replacement=tuple(orphaned),
        text_changed_regions=tuple(changed),
        coverage_supersessions=coverage_supersessions,
    )
    _LOG.info(
        "asr.merge",
        extra={
            "segments": len(merged),
            "superseded": result.superseded_count,
            "replacements": result.replacement_count,
            "regions_replaced": len(replaced),
            "regions_without_replacement": len(orphaned),
            "text_changed": len(changed),
            "coverage_supersessions": coverage_supersessions,
        },
    )
    return result


def _merged_spans(spans: Any) -> tuple[tuple[int, int], ...]:
    """Union of possibly-overlapping intervals, in order.

    Pass-2 segments overlap one another often enough that summing raw intersections
    would report more than 100% coverage of a pass-1 segment and retire things on
    arithmetic that does not mean anything.
    """
    ordered = sorted(
        (int(start), int(end)) for start, end in spans if int(end) > int(start)
    )
    if not ordered:
        return ()
    out: list[list[int]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return tuple((start, end) for start, end in out)


def _covered_fraction(
    start_ms: int, end_ms: int, spans: tuple[tuple[int, int], ...]
) -> float:
    """How much of `start_ms..end_ms` lies inside `spans`, as a fraction of its length."""
    duration = end_ms - start_ms
    if duration <= 0:
        # A zero-length segment cannot be "mostly covered"; treat it as covered only if
        # it sits inside a span at all, so it does not survive as a stray duplicate.
        return 1.0 if any(a <= start_ms <= b for a, b in spans) else 0.0
    inside = sum(
        max(0, min(end_ms, span_end) - max(start_ms, span_start))
        for span_start, span_end in spans
    )
    return inside / duration
