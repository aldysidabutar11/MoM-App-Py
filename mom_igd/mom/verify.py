"""Grounding checks. **No model runs in this module, and none ever may.**

This is the part that decides whether an extracted item is allowed to appear in a minute.
It uses string matching and nothing else, which is the point: a check performed by the
same class of system that produced the claim is not a check. Asking a language model
whether a language model's output is faithful produces a confident yes at a rate that
tracks how fluent the output is, not how true it is.

Four things are verified, in descending order of how much damage getting them wrong does:

* **The owner.** An action item with an invented person attached is the worst thing this
  system can emit. Worse than a missing item, which whoever was in the room will notice,
  and worse than a wrong date, which is checkable against reality. So an owner survives
  only if a distinctive part of the name was actually *spoken* -- honorifics stripped,
  because "Pak" grounds nothing. Ungrounded, it is removed and the removal is recorded.
* **The quote.** Located in the cited segments, or located elsewhere and re-cited
  (``REBOUND``), or not located at all (``UNVERIFIED``). An unverified item is kept and
  labelled rather than deleted: a reviewer choosing what to trust needs to see what the
  model produced, and the label is what stops it being read as fact.
* **The due date.** Every word that actually names a date must appear in the window --
  not merely one of them, and not the scaffolding ("hari", "sebelum", "paling lambat")
  that appears in every meeting. Otherwise it is dropped, and the drop is recorded.
* **Numbers in the summary.** Every digit-string in the summary must exist in the items it
  was written from. A fabricated figure in an executive summary is read by people who
  never reach the detail.

Matching tolerates the difference between what a model quotes and what a transcript says
-- punctuation, casing, an inserted filler -- and does not tolerate a different sentence.
The near-match threshold is a **contiguous** token-overlap ratio, so a quote assembled
from words scattered across a chunk fails even when every word is present. That case is
not hypothetical: it is what a hallucinated quote looks like.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Final, Iterable, Mapping, Sequence

from mom_igd.mom.chunking import ChunkSegment, TranscriptChunk
from mom_igd.mom.schema import MinuteItem

__all__ = [
    "NEAR_MATCH_RATIO",
    "REVERSAL_MARKERS",
    "mark_superseded",
    "TranscriptIndex",
    "check_summary_numbers",
    "normalise",
    "tokenise",
    "verify_items",
]

#: How much of a quote must be found, contiguously, for it to count as the same sentence.
#: 0.85 accepts one changed word in seven and rejects a rewritten sentence. Chosen to sit
#: above the rate at which two genuinely different sentences about the same topic overlap.
NEAR_MATCH_RATIO: Final[float] = 0.85

#: How far the match may spread relative to the quote's own length. A quote of ten tokens
#: matched across a forty-token span is not that quote; it is those words, elsewhere.
_MAX_SPAN_FACTOR: Final[float] = 2.0

#: A quote shorter than this is not evidence of anything -- "ya", "setuju", "oke" appear
#: everywhere in a meeting and would match by accident.
_MIN_QUOTE_TOKENS: Final[int] = 3

#: Stripped before deciding whether a name was spoken. None of these identifies anybody,
#: and leaving them in would let "Pak" alone ground an invented surname.
_HONORIFICS: Final[frozenset[str]] = frozenset(
    {
        "pak", "bapak", "bu", "ibu", "mas", "mbak", "kak", "bang", "om", "tante",
        "dr", "drg", "prof", "ir", "drs", "dra", "sdr", "sdri", "mr", "mrs", "ms",
        "tim", "team", "bagian", "divisi", "departemen", "unit", "staff", "staf",
        "semua", "semuanya", "kita", "saya", "anda", "beliau", "yang", "dan",
    }
)

#: Scaffolding words in an Indonesian date phrase. They carry no date on their own, so a
#: due date must not be accepted on the strength of one.
#:
#: This closed a real hole. The rule was "any token of the due text appears in the window",
#: and a 4-billion-parameter model at 4-bit mis-sampled "hari Kamis" as **"hari Kam4"** --
#: which passed, because "hari" is in every meeting. The transcript says Thursday; the
#: minute would have asserted a deadline that does not exist in any language.
_DATE_SCAFFOLDING: Final[frozenset[str]] = frozenset(
    {
        "hari", "tanggal", "tgl", "bulan", "minggu", "pekan", "tahun", "jam", "pukul",
        "paling", "lambat", "sebelum", "setelah", "sesudah", "akhir", "awal", "pertengahan",
        "depan", "ini", "besok", "nanti", "pada", "di", "ke", "dalam", "waktu", "batas",
    }
)

_NON_WORD = re.compile(r"[^0-9a-z]+")
_DIGITS = re.compile(r"\d[\d.,]*")


def normalise(text: str) -> str:
    """Casefold, strip punctuation and accents-free-fold to a single spaced token run."""
    lowered = str(text or "").casefold()
    return _NON_WORD.sub(" ", lowered).strip()


def tokenise(text: str) -> list[str]:
    normalised = normalise(text)
    return normalised.split() if normalised else []


# ===========================================================================
# The searchable transcript
# ===========================================================================


@dataclass(slots=True)
class _Located:
    """Where a quote was found."""

    segment_ids: tuple[int, ...]
    ratio: float
    exact: bool


class TranscriptIndex:
    """A token-level index over a set of segments, with a map back to segment ids.

    Built once per window and once for the whole transcript. Tokens are flattened across
    segment boundaries deliberately: a spoken sentence is routinely split across two
    segments by the decoder, and a quote that spans the split is the normal case, not an
    anomaly.
    """

    __slots__ = ("_tokens", "_owners", "_token_set", "_joined")

    def __init__(self, segments: Iterable[ChunkSegment | Mapping[str, object]]) -> None:
        self._tokens: list[str] = []
        self._owners: list[int] = []
        for segment in segments:
            if isinstance(segment, ChunkSegment):
                seq, text = segment.seq, segment.text
            else:
                seq, text = int(segment["seq"]), str(segment.get("text") or "")  # type: ignore[index]
            for token in tokenise(text):
                self._tokens.append(token)
                self._owners.append(seq)
        self._token_set = frozenset(self._tokens)
        self._joined = " ".join(self._tokens)

    def __len__(self) -> int:
        return len(self._tokens)

    @property
    def tokens(self) -> Sequence[str]:
        return self._tokens

    def contains_token(self, token: str) -> bool:
        return token in self._token_set

    def _segments_for(self, start: int, end: int) -> tuple[int, ...]:
        seen: list[int] = []
        for owner in self._owners[start:end]:
            if owner not in seen:
                seen.append(owner)
        return tuple(seen)

    def locate(self, quote: str) -> _Located | None:
        """Find a quote. Exact contiguous match first, then a graded near match.

        Returns ``None`` when the quote is absent, too short to mean anything, or matched
        only as scattered words.
        """
        needle = tokenise(quote)
        if len(needle) < _MIN_QUOTE_TOKENS or not self._tokens:
            return None

        # Exact: a contiguous run of the same tokens. Done on the joined string, which is
        # far faster than a list scan and, because tokens are space-joined and padded,
        # cannot match across a token boundary.
        joined_needle = " ".join(needle)
        position = f" {self._joined} ".find(f" {joined_needle} ")
        if position >= 0:
            prefix = f" {self._joined} "[:position].split()
            start = len(prefix)
            return _Located(
                segment_ids=self._segments_for(start, start + len(needle)),
                ratio=1.0,
                exact=True,
            )

        # Near: the largest set of in-order matching blocks, required to be contiguous
        # enough that it is the same sentence rather than the same vocabulary.
        matcher = SequenceMatcher(None, needle, self._tokens, autojunk=False)
        blocks = [block for block in matcher.get_matching_blocks() if block.size > 0]
        if not blocks:
            return None
        matched = sum(block.size for block in blocks)
        ratio = matched / len(needle)
        if ratio < NEAR_MATCH_RATIO:
            return None
        start = min(block.b for block in blocks)
        end = max(block.b + block.size for block in blocks)
        if (end - start) > max(len(needle) * _MAX_SPAN_FACTOR, len(needle) + 4):
            return None
        return _Located(segment_ids=self._segments_for(start, end), ratio=ratio, exact=False)


# ===========================================================================
# Owner and due grounding
# ===========================================================================


def _distinctive_tokens(value: str) -> list[str]:
    """The parts of a name that could identify somebody. Honorifics are not among them."""
    return [
        token
        for token in tokenise(value)
        if len(token) >= 3 and token not in _HONORIFICS and not token.isdigit()
    ]


def _canonicalise_owner(owner: str, roster: Mapping[str, str]) -> str:
    """Prefer the roster's spelling of a name that was genuinely spoken.

    The roster is used **only** here, and only after grounding has already succeeded
    against the transcript. It cannot introduce a name -- it can only correct the spelling
    of one the meeting actually said, which is what makes an action item searchable by the
    name the organisation uses.
    """
    tokens = set(_distinctive_tokens(owner))
    if not tokens:
        return owner
    for normalised_name, display in roster.items():
        candidate = set(_distinctive_tokens(normalised_name))
        if candidate and candidate <= tokens:
            return display
        if candidate and tokens <= candidate:
            return display
    return owner


# ===========================================================================
# The pass
# ===========================================================================


def verify_items(
    items: Sequence[MinuteItem],
    *,
    chunk: TranscriptChunk,
    transcript_index: TranscriptIndex,
    segment_times: Mapping[int, tuple[int, int]],
    roster: Mapping[str, str] | None = None,
) -> list[MinuteItem]:
    """Verify one window's items against that window and the whole transcript.

    Two indexes on purpose. The **quote** must be in this window -- the model was shown
    nothing else, so a quote found only elsewhere in the meeting was not copied, it was
    recalled, and a recalled quote is a coincidence. An **owner**, by contrast, is grounded
    against the whole transcript, because a name introduced at the start of a meeting is
    routinely referred to later by an item three windows away.

    Items are returned in input order with their verification fields filled. Nothing is
    dropped here: dropping is a decision for the caller, which knows the policy.
    """
    window = TranscriptIndex(chunk.segments)
    legal_ids = chunk.segment_ids
    names = dict(roster or {})
    out: list[MinuteItem] = []

    for item in items:
        notes: list[str] = []

        cited = tuple(seq for seq in item.segment_ids if seq in legal_ids)
        if len(cited) != len(item.segment_ids):
            notes.append("CITATION_OUT_OF_RANGE")

        # 1. The quote, first against what was cited, then against the whole window.
        verification = "UNVERIFIED"
        resolved: tuple[int, ...] = cited
        if cited:
            subset = TranscriptIndex(
                [segment for segment in chunk.segments if segment.seq in cited]
            )
            located = subset.locate(item.quote)
            if located is not None:
                verification = "VERIFIED"
                resolved = located.segment_ids or cited
                if not located.exact:
                    notes.append("QUOTE_NEAR_MATCH")
        if verification == "UNVERIFIED":
            located = window.locate(item.quote)
            if located is not None:
                verification = "REBOUND"
                resolved = located.segment_ids
                notes.append("QUOTE_FOUND_IN_OTHER_SEGMENT")
                if not located.exact:
                    notes.append("QUOTE_NEAR_MATCH")
            else:
                notes.append("QUOTE_NOT_FOUND")
                resolved = cited

        # 2. The owner. Grounded against the whole transcript or removed.
        owner = item.owner
        if owner:
            distinctive = _distinctive_tokens(owner)
            if not distinctive:
                owner = None
                notes.append("OWNER_NOT_A_NAME")
            elif not any(transcript_index.contains_token(token) for token in distinctive):
                owner = None
                notes.append("OWNER_NOT_IN_TRANSCRIPT")
            else:
                owner = _canonicalise_owner(owner, names)

        # 3. The due date. Grounded against this window only -- a date is stated where the
        #    commitment is made, and accepting one from elsewhere in the meeting would
        #    attach another item's deadline to this one.
        due = item.due
        if due:
            tokens = [token for token in tokenise(due) if len(token) >= 2]
            # The parts that actually name a date. Every one of them must have been said:
            # requiring only *one* let "hari Kam4" through on the strength of "hari".
            naming = [token for token in tokens if token not in _DATE_SCAFFOLDING]
            required = naming or tokens
            if not required or not all(window.contains_token(token) for token in required):
                due = None
                notes.append("DUE_NOT_IN_TRANSCRIPT")

        # 4. Timestamps, from wherever the quote actually landed.
        times = [segment_times[seq] for seq in resolved if seq in segment_times]
        start_ms = min((pair[0] for pair in times), default=None)
        end_ms = max((pair[1] for pair in times), default=None)

        out.append(
            MinuteItem(
                kind=item.kind,
                text=item.text,
                quote=item.quote,
                segment_ids=resolved,
                owner=owner,
                due=due,
                verification=verification,
                verification_notes=tuple(dict.fromkeys(notes)),
                start_ms=start_ms,
                end_ms=end_ms,
                chunk_index=item.chunk_index,
                merged_count=item.merged_count,
            )
        )
    return out


#: Words an Indonesian meeting uses when it takes something back. Observed in a real
#: run: a decision to move UAT to the spare server, reversed sixty-six seconds later.
#:
#: Whole words only, and the list stays short deliberately. A marker is the *gate* for
#: the check below, so a loose one would attach cautions to unrelated decisions and the
#: caution would stop meaning anything.
REVERSAL_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "batal", "batalkan", "dibatalkan", "membatalkan",
        "ralat", "diralat", "koreksi", "dikoreksi",
        "revisi", "direvisi", "diubah", "berubah",
        "gantinya", "diganti", "menggantikan",
    }
)

#: Words a speaker uses to point back at something already decided. **This is the pattern
#: that actually occurs**, and finding that out changed the rule below.
#:
#: The first version linked a reversal to the decision it reversed by counting shared
#: distinctive words. Measured against a real run, the two shared exactly one -- and that
#: one was the filler "begitu". People do not restate a decision in order to cancel it;
#: they refer to it: *"keputusan tadi kita batalkan"*. The subject words live in the
#: earlier sentence and the reversal words in the later one, so word overlap is close to
#: the worst available signal for this.
_BACK_REFERENCE: Final[frozenset[str]] = frozenset(
    {"tadi", "sebelumnya", "barusan", "awal", "semula", "sebelum", "tersebut"}
)

#: Tokens too common to link two items. Not a general stopword list -- only words that
#: appear in most meeting sentences and therefore carry no evidence of shared subject.
_COMMON: Final[frozenset[str]] = frozenset(
    {
        "yang", "untuk", "dengan", "dari", "pada", "akan", "sudah", "belum", "tidak",
        "kita", "saya", "kami", "ini", "itu", "dan", "atau", "juga", "bisa", "harus",
        "jadi", "kalau", "karena", "tapi", "sama", "lebih", "masih", "saja", "dulu",
        "nanti", "sekarang", "kalo", "gitu", "aja", "rapat", "hari", "minggu",
        "begitu", "berarti", "soal", "tentang", "sini", "situ", "ada", "kita",
        "keputusan", "memutuskan", "diputuskan", "sepakat", "menyepakati",
    }
)

#: How many distinctive words two items must share before a reversal marker in the later
#: one is taken to be about the earlier one, when there is no back-reference.
#:
#: Three characters, not four: "UAT" is exactly the kind of token that links two sentences
#: about the same thing, and a four-character floor drops every acronym in a technical
#: meeting.
_MIN_SHARED_CONTENT_WORDS: Final[int] = 3
_MIN_CONTENT_WORD_LENGTH: Final[int] = 3


def _content_words(text: str) -> set[str]:
    return {
        token
        for token in tokenise(text)
        if len(token) >= _MIN_CONTENT_WORD_LENGTH
        and token not in _COMMON
        and not token.isdigit()
    }


def mark_superseded(items: Sequence[MinuteItem]) -> list[MinuteItem]:
    """Flag a decision the meeting later took back. **Adds a caution, never deletes.**

    A meeting reverses itself, and a minute that lists the reversed decision beside its
    reversal -- both unmarked -- is worse than one that lists neither: the reader skims the
    decisions section and actions whichever they see first. Observed on a real run, where
    "UAT will run on the spare server" and "that decision is cancelled" both appeared as
    plain decisions sixty-six seconds apart.

    Lexical and deterministic. A **later** item must contain a reversal word, and then be
    linked to the decision by one of two patterns:

    * **Back-reference** -- it also says "tadi", "sebelumnya" or similar, in which case the
      referent is the **nearest preceding decision**. This is how the reversal is actually
      phrased in practice: *"keputusan tadi kita batalkan"*.
    * **Restated subject** -- it shares at least three distinctive words with the decision.
      This catches the less common *"kita batalkan rencana pindah ke server cadangan"*.

    The reversal word is the gate in both cases; the link only decides *which* decision.

    The failure modes are deliberately asymmetric. A false positive costs a caution on a
    decision that was not reversed, which a reviewer settles in seconds from the quote
    already printed beside it. A false negative leaves the minute exactly as it would have
    been without the check. Nothing is removed either way, and no model is involved.
    """
    out = list(items)
    ordered = sorted(
        range(len(out)),
        key=lambda index: (
            out[index].start_ms is None,
            out[index].start_ms if out[index].start_ms is not None else 0,
        ),
    )
    decisions = [index for index in ordered if out[index].kind == "DECISION"]

    for position, index in enumerate(ordered):
        item = out[index]
        if item.kind != "DECISION":
            continue
        subject = _content_words(item.text) | _content_words(item.quote)
        # The nearest decision *after* this one. A back-reference in a reversal points at
        # the last thing decided, which is this decision only if nothing was decided in
        # between -- otherwise the caution would land on the wrong row.
        following = [other for other in decisions if ordered.index(other) > position]
        next_decision = following[0] if following else None

        for later_index in ordered[position + 1 :]:
            later = out[later_index]
            words = set(tokenise(later.text)) | set(tokenise(later.quote))
            if not (words & REVERSAL_MARKERS):
                continue
            shared = subject & (_content_words(later.text) | _content_words(later.quote))
            refers_back = bool(words & _BACK_REFERENCE) and later_index == next_decision
            if len(shared) < _MIN_SHARED_CONTENT_WORDS and not refers_back:
                continue
            stamp = (
                "--:--"
                if later.start_ms is None
                else f"{later.start_ms // 3600000:02d}:"
                f"{later.start_ms % 3600000 // 60000:02d}:"
                f"{later.start_ms % 60000 // 1000:02d}"
            )
            out[index] = MinuteItem(
                kind=item.kind,
                text=item.text,
                quote=item.quote,
                segment_ids=item.segment_ids,
                owner=item.owner,
                due=item.due,
                verification=item.verification,
                verification_notes=tuple(
                    dict.fromkeys((*item.verification_notes, f"POSSIBLY_SUPERSEDED:{stamp}"))
                ),
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                chunk_index=item.chunk_index,
                merged_count=item.merged_count,
            )
            break
    return out


def check_summary_numbers(
    summary: Sequence[str], *, sources: Sequence[str]
) -> tuple[str, ...]:
    """Return digit-strings in the summary with no counterpart in its sources.

    Digits only. Indonesian number words were considered and left out: "beberapa" and
    "sebagian" are hedges rather than figures, and flagging them would train the reader to
    ignore the flag. A figure written in digits is a figure somebody will act on.

    Comparison strips thousands separators, so "1.500" in a summary matches "1500" spoken
    in the transcript, and a year matches a year.
    """
    def digits(text: str) -> set[str]:
        found: set[str] = set()
        for match in _DIGITS.findall(text):
            stripped = match.rstrip(".,")
            if not stripped:
                continue
            found.add(stripped)
            found.add(re.sub(r"[.,]", "", stripped))
        return found

    available: set[str] = set()
    for source in sources:
        available |= digits(source)

    unsupported: list[str] = []
    for line in summary:
        for match in _DIGITS.findall(line):
            stripped = match.rstrip(".,")
            plain = re.sub(r"[.,]", "", stripped)
            if not plain:
                continue
            if stripped in available or plain in available:
                continue
            if plain not in unsupported:
                unsupported.append(plain)
    return tuple(unsupported)
