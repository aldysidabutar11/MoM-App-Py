"""The shape of a minute, and the grammar that forces the model to produce it.

Two things live here and they are deliberately in the same file: the dataclasses that
describe a minute, and the **GBNF grammars** that constrain the decoder to emit exactly
those shapes. Keeping them together is what stops the pair drifting -- a field added to
the dataclass and not to the grammar is a field the model can never fill, and a field in
the grammar with nowhere to go is silently discarded. A test parses the grammars and
round-trips a sample of each, so a divergence fails the suite rather than a meeting.

**Why a grammar rather than "reply in JSON".** Asking a 4-billion-parameter model for JSON
gets valid JSON most of the time. The remainder needs a repair step, and a repair step is
where a truncated object quietly becomes a shorter list, or a mangled field becomes an
invented one. With a grammar the malformed tokens are never sampled: the sampler's logits
are masked to the tokens the grammar permits at that position. Malformed output is not
unlikely, it is unreachable.

**What the grammar cannot do** is make the content true. It guarantees an item *has* a
quote and a citation; whether the quote is real is decided by :mod:`mom_igd.mom.verify`,
which does not use a model.

**Codes are English, prose is Indonesian.** The item kinds are stable identifiers stored
in the database and matched in code; translating them would make a stored row depend on
the interface language. What the reader sees is rendered at the edge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final, Mapping, Sequence

__all__ = [
    "EXTRACTION_GRAMMAR",
    "MAX_ITEMS_PER_CHUNK",
    "QUOTE_CHAR_LIMIT",
    "ITEM_KINDS",
    "KIND_LABELS_ID",
    "MINUTE_STATUSES",
    "MinuteItem",
    "MinuteDraft",
    "SUMMARY_GRAMMAR",
    "VERIFICATION_STATES",
    "coerce_extraction",
    "coerce_summary",
    "kind_label",
]


#: The kinds of thing a minute records. A closed set: an unknown kind cannot be sampled,
#: and a row carrying one could not be rendered.
#:
#: ``ISSUE`` covers an open question, a risk and a blocker together. Splitting them was
#: considered and rejected: a 4B model distinguishes them unreliably, and a misfiled risk
#: is worse than a correctly filed issue, because the reader stops trusting the filing.
ITEM_KINDS: Final[tuple[str, ...]] = ("DECISION", "ACTION", "DISCUSSION", "ISSUE")

#: Must equal the repetition ceiling in :data:`EXTRACTION_GRAMMAR`. A window that comes
#: back holding exactly this many items was probably cut short by the grammar rather than
#: by the meeting, and the caller says so.
MAX_ITEMS_PER_CHUNK: Final[int] = 16

#: Must equal ``quotestr`` in :data:`EXTRACTION_GRAMMAR` (12 blocks of 16, plus 15).
QUOTE_CHAR_LIMIT: Final[int] = 12 * 16 + 15

#: What the operator sees. Rendered at the edge; never stored.
KIND_LABELS_ID: Final[Mapping[str, str]] = {
    "DECISION": "Keputusan",
    "ACTION": "Tindak Lanjut",
    "DISCUSSION": "Pembahasan",
    "ISSUE": "Isu / Pertanyaan Terbuka",
}

#: Per-item outcome of the non-LLM verifier.
#:
#: ``VERIFIED``   the quote was found in a cited segment, unchanged.
#: ``REBOUND``    the quote was real but cited to the wrong segment; the citation was
#:                corrected to where the text actually is.
#: ``UNVERIFIED`` no quote could be located. Kept, shown, and marked -- never silently
#:                dropped, because a reviewer deciding what to trust needs to see what the
#:                model produced, and never silently kept either.
VERIFICATION_STATES: Final[tuple[str, ...]] = ("VERIFIED", "REBOUND", "UNVERIFIED")

#: A minute is a draft until a human approves it. Approval is not implemented here and no
#: code path in this package writes ``APPROVED``.
MINUTE_STATUSES: Final[tuple[str, ...]] = (
    "BUILDING",
    "DRAFT",
    "FAILED",
    "CANCELLED",
)


def kind_label(kind: str) -> str:
    return KIND_LABELS_ID.get(kind, kind)


# ===========================================================================
# Grammar
# ===========================================================================

#: Shared JSON primitives.
#:
#: **Strings are bounded**, and not for tidiness: an unbounded string is the one place a
#: constrained decoder can still run away, repeating a phrase until ``max_tokens``, and one
#: runaway item costs the whole window its remaining budget. The bound makes the closing
#: quote mandatory rather than merely likely.
#:
#: **Lengths are built from sixteen-character blocks** rather than a flat ``c{0,400}``.
#: Both accept exactly the same strings; the block form leaves the grammar automaton about
#: forty states deep instead of four hundred, and llama.cpp re-examines those states for
#: every candidate token of a 151 000-token vocabulary on every single step. Same
#: guarantee, an order of magnitude less work per token.
#:
#: **One rule per line, always.** llama.cpp's GBNF parser ends a rule at the newline, so a
#: definition wrapped across lines for readability fails to parse -- and it fails *late*:
#: ``LlamaGrammar.from_string`` does not parse anything, it stores the string, and the
#: error only surfaces as an ``OSError`` from the sampler once a model is loaded. That is
#: why :meth:`mom_igd.mom.llm.LocalLlm.validate_grammar` exists and why these lines are
#: long.
_JSON_PRIMITIVES: Final[str] = r"""
ws ::= [ \t\n]*
c ::= [^"\\\x00-\x1F] | "\\" (["\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F])
c16 ::= c c c c c c c c c c c c c c c c
string ::= "\"" c16{0,24} c{0,15} "\""
quotestr ::= "\"" c16{0,12} c{0,15} "\""
shortstr ::= "\"" c16{0,7} c{0,15} "\""
nullstr ::= "null" | shortstr
posint ::= [0-9] | [1-9] [0-9]{0,4}
intlist ::= "[" ws posint (ws "," ws posint){0,7} ws "]"
"""

#: Extraction: one call over one chunk of transcript, producing grounded candidate items.
#:
#: ``segments`` is required and non-empty by construction -- ``intlist`` has no empty
#: alternative. An item that cannot say where it came from cannot be emitted at all, which
#: is a stronger guarantee than validating citations after the fact.
#:
#: At most 16 items per window. Not a truth about meetings -- a bound on damage, since a
#: model that starts enumerating every sentence would otherwise consume the whole budget.
#: The caller reports when the ceiling is reached, because a silent cap reads as "that is
#: everything that was said". Sixteen items at the lengths below also keeps the longest
#: grammatically reachable answer close to the completion budget, so truncation is rare
#: rather than routine -- and it is handled when it happens, never ignored.
#:
#: ``quote`` is bounded shorter than ``text`` (207 against 399 characters). The quote is
#: transcript text the model has to re-emit token by token, which measured at roughly forty
#: of the hundred and fourteen tokens an item costs; a shorter quote is both cheaper and
#: easier for a reviewer to check against the recording.
EXTRACTION_GRAMMAR: Final[str] = (
    r"""
root ::= "{" ws "\"items\"" ws ":" ws items ws "}"
items ::= "[" ws "]" | "[" ws item (ws "," ws item){0,15} ws "]"
item ::= "{" ws "\"kind\"" ws ":" ws kind ws "," ws "\"text\"" ws ":" ws string ws "," ws "\"quote\"" ws ":" ws quotestr ws "," ws "\"segments\"" ws ":" ws intlist ws "," ws "\"owner\"" ws ":" ws nullstr ws "," ws "\"due\"" ws ":" ws nullstr ws "}"
kind ::= "\"DECISION\"" | "\"ACTION\"" | "\"DISCUSSION\"" | "\"ISSUE\""
"""
    + _JSON_PRIMITIVES
)

#: Summary: one call over the **already verified** item list, never over raw transcript.
#:
#: That is the whole design. A summary written from verified items cannot introduce a fact
#: that was not already checked against the audio, so the weakest, least checkable output
#: of the model is derived from its most checked one. Writing it from the transcript
#: instead would put an unverifiable paragraph at the top of the document, which is
#: exactly where a reader trusts most.
SUMMARY_GRAMMAR: Final[str] = (
    r"""
root ::= "{" ws "\"title\"" ws ":" ws shortstr ws "," ws "\"summary\"" ws ":" ws lines ws "}"
lines ::= "[" ws string (ws "," ws string){0,7} ws "]"
"""
    + _JSON_PRIMITIVES
)


# ===========================================================================
# Records
# ===========================================================================


@dataclass(slots=True)
class MinuteItem:
    """One grounded line of a minute.

    ``quote`` is what the verifier checks and ``text`` is what the reader reads. They are
    separate on purpose: the model is allowed to tidy a spoken sentence into a written
    one, but it is not allowed to invent the sentence, and only a verbatim span can be
    checked against a transcript.
    """

    kind: str
    text: str
    quote: str
    segment_ids: tuple[int, ...]
    owner: str | None = None
    due: str | None = None
    #: Filled by the verifier, not by the model.
    verification: str = "UNVERIFIED"
    verification_notes: tuple[str, ...] = ()
    start_ms: int | None = None
    end_ms: int | None = None
    chunk_index: int = 0
    #: Set when a later duplicate was folded into this item.
    merged_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "quote": self.quote,
            "segment_ids": list(self.segment_ids),
            "owner": self.owner,
            "due": self.due,
            "verification": self.verification,
            "verification_notes": list(self.verification_notes),
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "chunk_index": self.chunk_index,
            "merged_count": self.merged_count,
        }


@dataclass(slots=True)
class MinuteDraft:
    """A whole minute, before anybody has approved it."""

    title: str
    summary: tuple[str, ...]
    items: tuple[MinuteItem, ...]
    #: Numeric tokens in the summary with no counterpart in the items. Empty is the
    #: expected state; anything here is a hallucinated figure and is reported, not hidden.
    summary_unsupported_numbers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def by_kind(self, kind: str) -> tuple[MinuteItem, ...]:
        return tuple(item for item in self.items if item.kind == kind)

    @property
    def verified_count(self) -> int:
        return sum(1 for item in self.items if item.verification in ("VERIFIED", "REBOUND"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": list(self.summary),
            "items": [item.to_dict() for item in self.items],
            "summary_unsupported_numbers": list(self.summary_unsupported_numbers),
            "warnings": list(self.warnings),
            "item_count": len(self.items),
            "verified_count": self.verified_count,
        }


# ===========================================================================
# Coercion
# ===========================================================================

#: A "null-ish" owner or due date. The model is told to use JSON ``null``, and a small
#: model reaches for a word instead often enough that treating these as absent is worth
#: more than being strict. They are *dropped*, never rendered -- "PIC: belum ditentukan"
#: reads as a recorded fact, and it is the absence of one.
_NULLISH: Final[frozenset[str]] = frozenset(
    {
        "",
        "-",
        "null",
        "none",
        "n/a",
        "na",
        "tidak ada",
        "tidak disebutkan",
        "belum ada",
        "belum ditentukan",
        "belum disebutkan",
        "tbd",
        "unknown",
        "tidak diketahui",
    }
)

_WHITESPACE = re.compile(r"\s+")


def _clean(value: Any, *, limit: int) -> str:
    text = _WHITESPACE.sub(" ", str(value or "")).strip()
    return text[:limit]


def _optional(value: Any, *, limit: int) -> str | None:
    text = _clean(value, limit=limit)
    if not text or text.casefold() in _NULLISH:
        return None
    return text


def coerce_extraction(payload: Mapping[str, Any], *, chunk_index: int = 0) -> list[MinuteItem]:
    """Turn one decoded extraction object into items, dropping what cannot be used.

    The grammar guarantees the *shape*; this guarantees the *content is usable*. An item
    with no text or no quote is discarded here rather than travelling to the verifier,
    which would mark it unverified and put an empty line in front of a reader.
    """
    items: list[MinuteItem] = []
    for entry in payload.get("items") or ():
        if not isinstance(entry, Mapping):
            continue
        kind = str(entry.get("kind") or "").strip().upper()
        if kind not in ITEM_KINDS:
            continue
        text = _clean(entry.get("text"), limit=400)
        quote = _clean(entry.get("quote"), limit=QUOTE_CHAR_LIMIT)
        if not text or not quote:
            continue
        raw_ids = entry.get("segments") or ()
        segment_ids: list[int] = []
        for value in raw_ids if isinstance(raw_ids, Sequence) else ():
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number >= 0 and number not in segment_ids:
                segment_ids.append(number)
        items.append(
            MinuteItem(
                kind=kind,
                text=text,
                quote=quote,
                segment_ids=tuple(segment_ids),
                owner=_optional(entry.get("owner"), limit=120),
                due=_optional(entry.get("due"), limit=120),
                chunk_index=chunk_index,
            )
        )
    return items


def coerce_summary(payload: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Turn one decoded summary object into a title and sentences."""
    title = _clean(payload.get("title"), limit=120)
    lines = [
        cleaned
        for line in (payload.get("summary") or ())
        if (cleaned := _clean(line, limit=400))
    ]
    return title, tuple(lines)
