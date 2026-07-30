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

**A region is the unit.** Pass 2 re-transcribes whole speech regions, so supersession is
per region: every active pass-1 segment whose region was re-run is retired, and the pass-2
segments for that region take its place. Partial overlap does not arise, because both passes
segment inside the same region boundaries.

**Order is by time, not by pass.** The merged transcript is renumbered in ascending start
time so a reader gets the meeting in order. Two segments starting at the same millisecond
break the tie on the pass number, so a pass-2 result never sorts after the pass-1 segment it
replaced.

**Nothing is dropped silently.** If pass 2 returned nothing for a region it was asked
about, the pass-1 segments stay active and the region is reported in ``regions_without_
replacement``. Retiring evidence in favour of nothing would lose part of the meeting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Mapping, Sequence

from mom_igd.logging_setup import get_logger

__all__ = ["MergeResult", "merge_pass2_into_pass1"]

_LOG = get_logger("asr.merge")

#: Why a pass-1 segment stopped being part of the transcript. One value today, because
#: today there is exactly one reason -- but stored as a code so Phase 7's reviewer edits
#: can add their own without changing the shape of the data.
SUPERSEDED_BY_PASS2: Final[str] = "SUPERSEDED_BY_PASS2"


@dataclass(slots=True)
class MergeResult:
    """The merged segment list, plus what the merge did and did not manage to do."""

    segments: tuple[dict[str, Any], ...] = ()
    superseded_count: int = 0
    replacement_count: int = 0
    regions_replaced: tuple[int, ...] = ()
    regions_without_replacement: tuple[int, ...] = ()
    text_changed_regions: tuple[int, ...] = ()

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

    # Pass-1 segments first, retiring the ones whose region was genuinely re-transcribed.
    for segment in pass1_segments:
        row = dict(segment)
        region = row.get("region_seq")
        retired = region is not None and int(region) in set(replaced)
        row["asr_pass"] = int(row.get("asr_pass", 1))
        row["is_active"] = not retired
        row["superseded_reason"] = SUPERSEDED_BY_PASS2 if retired else None
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
        },
    )
    return result
