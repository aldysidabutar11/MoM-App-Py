"""Fold duplicate items produced by overlapping windows. Arithmetic, not a model.

Windows overlap by fifteen seconds so a decision stated across a cut is seen by both
sides. That is the right trade -- but it means the same decision arrives twice, worded
slightly differently, and a minute that lists it twice looks careless in exactly the way
that makes a reader stop trusting the rest.

Merging is done here with string similarity and time proximity, deliberately without a
model. A second model call to decide "are these the same decision?" would cost as much as
the extraction did and would be, itself, unverifiable.

**A merge never loses information.** The survivor takes the union of the citations, the
earliest start and the latest end, and the *stronger* verification state of the two. Where
one copy names an owner and the other does not, the named one wins -- an owner survived
the transcript check in :mod:`mom_igd.mom.verify` before it ever reached this module, so
the disagreement is between "found it" and "did not look there", not between two claims.

Where two copies genuinely conflict -- different owners, different dates -- the conflict is
**recorded on the survivor** and both values are kept in the note. Silently picking one
would be inventing an agreement the meeting did not reach.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Final, Sequence

from mom_igd.mom.schema import MinuteItem
from mom_igd.mom.verify import normalise, tokenise

__all__ = ["SIMILARITY_THRESHOLD", "deduplicate"]

#: How alike two items' **text** must be to count as one. Measured on real phrasings::
#:
#:     0.962  the same decision extracted from two overlapping windows   -> merge
#:     0.857  "Poin ketiga yang dibahas" / "Poin pertama yang dibahas"    -> separate
#:     0.808  "Menunda go-live ke September" / "Menunda UAT ke September" -> separate
#:     0.543  two genuine paraphrases of one decision                     -> see below
#:
#: 0.90 sits in the gap. An earlier 0.82 merged the second pair, which is the expensive
#: direction of error: a wrong merge deletes a distinct point, while a missed merge only
#: lists something twice.
#:
#: Note what the 0.543 row means. Character similarity **does not** recognise a
#: paraphrase, and no threshold would make it -- 0.543 is below "Menunda UAT". Paraphrased
#: duplicates are caught by the quote comparison below instead, which works because a
#: quote is verbatim transcript text: two windows quoting the same sentence produce nearly
#: the same string even when the model summarised it differently.
SIMILARITY_THRESHOLD: Final[float] = 0.90

#: Below this many normalised characters, similarity ratios are inflated: one differing
#: word is a small fraction of the characters and the whole of the meaning. Short texts
#: therefore need to be near-identical, which is what the 0.857 row above required.
_SHORT_TEXT_CHARS: Final[int] = 60
_SHORT_TEXT_THRESHOLD: Final[float] = 0.95

#: Two items further apart than this are different events even if they read alike. A
#: standing agenda point discussed at minute 4 and again at minute 50 is two discussions,
#: and collapsing them would hide that the meeting came back to it.
_MAX_MERGE_DISTANCE_MS: Final[int] = 240_000

#: Verification states, strongest first. A merge keeps the strongest of the two: if either
#: copy's quote was located in the audio, the item is grounded, and the copy that failed
#: only failed because its window happened to cut the sentence.
_STRENGTH: Final[dict[str, int]] = {"VERIFIED": 2, "REBOUND": 1, "UNVERIFIED": 0}


def _contains(left: str, right: str) -> bool:
    """True when one item's whole text sits inside the other's, as a token run.

    Observed: "Keputusan sebelumnya dibatalkan. UAT tetap menunggu perbaikan lantai tiga."
    and "UAT tetap menunggu perbaikan lantai tiga." arrived as two decisions at the same
    timestamp. Character similarity rates them low, because one is twice the length of the
    other -- but the shorter says nothing the longer does not.

    Requires the shorter side to be a real sentence (four tokens or more), so "setuju"
    does not swallow every item it appears inside.
    """
    left_tokens, right_tokens = tokenise(left), tokenise(right)
    if not left_tokens or not right_tokens:
        return False
    short, long = sorted((left_tokens, right_tokens), key=len)
    if len(short) < 4:
        return False
    return f" {' '.join(short)} " in f" {' '.join(long)} "


def _similar(left: str, right: str) -> float:
    """Normalised similarity, with a cheap token-overlap gate in front.

    ``SequenceMatcher`` is quadratic and this runs over every pair in the same kind. The
    gate rejects obviously unrelated pairs on set arithmetic first, which on a real meeting
    removes the great majority before the expensive comparison happens.
    """
    left_tokens, right_tokens = set(tokenise(left)), set(tokenise(right))
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))
    if overlap < 0.5:
        return 0.0
    return SequenceMatcher(None, normalise(left), normalise(right), autojunk=False).ratio()


def _distance_ms(left: MinuteItem, right: MinuteItem) -> int:
    """Gap between two items' spans. Zero when they overlap, and huge when untimed."""
    if left.start_ms is None or right.start_ms is None:
        return 0
    left_end = left.end_ms if left.end_ms is not None else left.start_ms
    right_end = right.end_ms if right.end_ms is not None else right.start_ms
    if left.start_ms <= right_end and right.start_ms <= left_end:
        return 0
    return min(
        abs(right.start_ms - left_end),
        abs(left.start_ms - right_end),
    )


def _merge(keeper: MinuteItem, other: MinuteItem) -> MinuteItem:
    """Fold ``other`` into ``keeper``, keeping the more informative of each field."""
    notes = list(keeper.verification_notes)

    owner = keeper.owner
    if other.owner and not owner:
        owner = other.owner
    elif owner and other.owner and normalise(owner) != normalise(other.owner):
        notes.append(f"OWNER_CONFLICT:{owner}|{other.owner}")

    due = keeper.due
    if other.due and not due:
        due = other.due
    elif due and other.due and normalise(due) != normalise(other.due):
        notes.append(f"DUE_CONFLICT:{due}|{other.due}")

    # The longer text is kept: an extraction that captured more of the sentence is more
    # useful to a reader than one that captured less, and neither can exceed what the
    # quote and the cited segments support.
    text = keeper.text if len(keeper.text) >= len(other.text) else other.text

    stronger = keeper if _STRENGTH[keeper.verification] >= _STRENGTH[other.verification] else other
    if stronger is other:
        notes.extend(note for note in other.verification_notes if note not in notes)
        quote = other.quote
    else:
        quote = keeper.quote

    starts = [value for value in (keeper.start_ms, other.start_ms) if value is not None]
    ends = [value for value in (keeper.end_ms, other.end_ms) if value is not None]

    return MinuteItem(
        kind=keeper.kind,
        text=text,
        quote=quote,
        segment_ids=tuple(dict.fromkeys(keeper.segment_ids + other.segment_ids)),
        owner=owner,
        due=due,
        verification=stronger.verification,
        verification_notes=tuple(dict.fromkeys(notes)),
        start_ms=min(starts) if starts else None,
        end_ms=max(ends) if ends else None,
        chunk_index=min(keeper.chunk_index, other.chunk_index),
        merged_count=keeper.merged_count + other.merged_count,
    )


def deduplicate(items: Sequence[MinuteItem]) -> list[MinuteItem]:
    """Merge near-identical items, returning them in meeting order.

    Only items of the **same kind** are ever merged. A decision and the discussion that led
    to it are worded almost identically and are not the same entry; collapsing them would
    delete the decision or demote it, and which of those happened would depend on input
    order.

    Output is ordered by when the item was said, so a minute reads in the order the meeting
    happened. Untimed items -- which means their quote was never located -- sort last,
    where they read as what they are: unconfirmed.
    """
    survivors: list[MinuteItem] = []
    for item in items:
        merged_into: int | None = None
        best = SIMILARITY_THRESHOLD
        for position, existing in enumerate(survivors):
            if existing.kind != item.kind:
                continue
            if _distance_ms(existing, item) > _MAX_MERGE_DISTANCE_MS:
                continue
            bar = best
            if min(len(normalise(existing.text)), len(normalise(item.text))) < _SHORT_TEXT_CHARS:
                bar = max(bar, _SHORT_TEXT_THRESHOLD)
            # Containment first: it is exact, and the ratio would miss it whenever the
            # two lengths differ much.
            if _contains(existing.text, item.text):
                merged_into = position
                break
            score = _similar(existing.text, item.text)
            # A shared citation plus matching quotes is how a *paraphrased* duplicate is
            # caught: the summaries diverge, but the quotes are verbatim transcript and
            # barely can. Requiring the shared citation is what keeps this from merging
            # two different points that happen to quote nearby text.
            if score < bar and set(existing.segment_ids) & set(item.segment_ids):
                score = max(score, _similar(existing.quote, item.quote))
            if score >= bar:
                best = score
                merged_into = position
        if merged_into is None:
            survivors.append(item)
        else:
            survivors[merged_into] = _merge(survivors[merged_into], item)

    survivors.sort(
        key=lambda entry: (
            entry.start_ms is None,
            entry.start_ms if entry.start_ms is not None else 0,
            entry.chunk_index,
        )
    )
    return survivors
