"""Split a transcript into windows a 4B model can actually reason over.

A ninety-minute meeting is roughly twelve thousand words. Even where the context window
would hold it, a small model asked to extract decisions from all of it at once returns a
handful of items from the first few minutes and nothing from the rest -- the failure is
not truncation, it is attention thinning out, and it is silent. So the transcript is cut
into windows and each is extracted separately.

**Every line carries its segment number**, rendered as ``[S12]``. That number is the
citation the model is required to emit, and the only reason grounding can be checked
afterwards without a model: :mod:`mom_igd.mom.verify` looks up exactly those segments and
compares text. Without the marker in the prompt there is nothing for a citation to mean.

**Windows overlap.** A decision stated across a window boundary would otherwise be seen
by neither side, and both halves would look like ordinary discussion. The duplicates this
produces are cheaper than the omission it prevents, and deduplication is arithmetic while
recovering a lost decision is not possible at all.

Token counts here are **estimates**. The true count depends on the model's tokeniser,
which lives in the worker process, and a parent that had to load a 2.3 GB model to decide
where to cut would defeat the point of a short-lived worker. The estimate is deliberately
pessimistic; the worker's real context is the backstop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final, Iterable, Mapping, Sequence

__all__ = [
    "DEFAULT_OVERLAP_MS",
    "TranscriptChunk",
    "build_chunks",
    "estimate_tokens",
    "render_segments",
]

#: Characters per token, measured against Qwen's own tokeniser on this machine. Plain
#: Indonesian meeting prose came out at **3.46**; the same text once wrapped in segment
#: markers, timestamps and the instruction block came out at **3.10**, because ``[S12]``
#: and ``(00:04:31)`` tokenise densely. The prompt is what has to fit, so the denser figure
#: is the one that matters, and it is rounded down again to 2.9.
#:
#: Erring low costs an extra model call on a long meeting. Erring high overflows the
#: context window, and llama.cpp does not report that as an error -- the tail of the
#: transcript is simply not there, and the items in it are missing from the minute with no
#: sign that anything was lost. The asymmetry decides the direction.
_CHARS_PER_TOKEN: Final[float] = 2.9

#: Tokens the answer needs, held back from the window. Sixteen items measured at about
#: ninety tokens each once the quote was shortened, plus the JSON scaffolding.
#:
#: The grammar can still, in principle, reach further than this -- sixteen items all at
#: their maximum lengths would not fit. That case is detected (``finish_reason`` is
#: ``length``) and retried on a halved window rather than parsed from a truncated
#: document, because a JSON object cut off mid-string is not a shorter list of items,
#: it is no list at all.
RESERVED_COMPLETION_TOKENS: Final[int] = 2048

#: Tokens the instructions occupy. Measured against the longest prompt in
#: :mod:`mom_igd.mom.prompts`, with headroom. A test tokenises the real instruction block
#: and fails if it grows past this, because the overflow would be silent: llama.cpp drops
#: the tail of the window and the minute simply misses the end of the meeting.
RESERVED_PROMPT_TOKENS: Final[int] = 1400

#: How much of the previous window the next one repeats. Fifteen seconds is about two
#: spoken sentences -- enough to carry a decision across a cut, short enough that the
#: overlap is a rounding error on total cost.
DEFAULT_OVERLAP_MS: Final[int] = 15_000

#: A window smaller than this is folded into its predecessor rather than run on its own.
#: A three-second tail chunk costs a full model call to extract nothing.
_MIN_TAIL_MS: Final[int] = 20_000

_WHITESPACE = re.compile(r"\s+")


def estimate_tokens(text: str) -> int:
    """Estimate the token count of a string. Pessimistic by construction."""
    return int(len(text) / _CHARS_PER_TOKEN) + 1


@dataclass(slots=True, frozen=True)
class ChunkSegment:
    """One transcript segment as the model will see it."""

    seq: int
    start_ms: int
    end_ms: int
    text: str

    @property
    def marker(self) -> str:
        return f"[S{self.seq}]"


@dataclass(slots=True, frozen=True)
class TranscriptChunk:
    """One window of transcript, with the citation ids it makes available."""

    index: int
    segments: tuple[ChunkSegment, ...]
    body: str
    start_ms: int
    end_ms: int
    token_estimate: int
    #: Segments repeated from the previous window. Reported so a caller can tell a
    #: duplicate that came from overlap from one the model emitted twice.
    overlap_count: int = 0
    #: Where this window sits in the meeting, for the operator and the prompt.
    #:
    #: Separate from ``index``, which is an identity: a window split after a truncated
    #: answer takes a derived index (``parent * 100 + half``) so its reply can be matched
    #: back, and telling the model it is reading "part 301 of 9" is nonsense handed to it
    #: at exactly the moment it is already struggling.
    display_index: int | None = None

    @property
    def position(self) -> int:
        """Zero-based place in the meeting. The parent's, for a split window."""
        return self.index if self.display_index is None else self.display_index

    @property
    def segment_ids(self) -> frozenset[int]:
        """The citation ids that are legitimate in this window. Anything else is invented."""
        return frozenset(segment.seq for segment in self.segments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "position": self.position,
            "segment_count": len(self.segments),
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "token_estimate": self.token_estimate,
            "overlap_count": self.overlap_count,
            "first_segment": self.segments[0].seq if self.segments else None,
            "last_segment": self.segments[-1].seq if self.segments else None,
        }


def _timestamp(milliseconds: int) -> str:
    total = max(0, int(milliseconds)) // 1000
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def render_segments(segments: Iterable[ChunkSegment]) -> str:
    """Render segments as the prompt body: one marked, timestamped line each.

    The timestamp is included because meetings refer to themselves in time ("as we said
    before the break"), and because it is what the reader will look for when checking a
    citation against the recording.
    """
    return "\n".join(
        f"{segment.marker} ({_timestamp(segment.start_ms)}) {segment.text}"
        for segment in segments
    )


def _to_chunk_segments(rows: Sequence[Mapping[str, Any]]) -> list[ChunkSegment]:
    out: list[ChunkSegment] = []
    for row in rows:
        text = _WHITESPACE.sub(" ", str(row.get("text") or "")).strip()
        if not text:
            continue
        out.append(
            ChunkSegment(
                seq=int(row["seq"]),
                start_ms=int(row["start_ms"]),
                end_ms=int(row["end_ms"]),
                text=text,
            )
        )
    out.sort(key=lambda segment: (segment.start_ms, segment.seq))
    return out


def build_chunks(
    rows: Sequence[Mapping[str, Any]],
    *,
    context_tokens: int = 8192,
    overlap_ms: int = DEFAULT_OVERLAP_MS,
) -> list[TranscriptChunk]:
    """Cut a segment list into overlapping windows that fit the model's context.

    ``rows`` are transcript segment rows -- ``seq``, ``start_ms``, ``end_ms``, ``text``.
    Pass the **active** segments only: a superseded pass-1 row and its pass-2 replacement
    say nearly the same thing, and feeding both would have the model extract each twice
    and the deduplicator merge them back, which is work done to undo work.

    A single segment longer than the whole budget is emitted as its own window rather than
    split mid-sentence. Splitting inside a segment would produce a citation that points at
    text the model never saw in full.
    """
    segments = _to_chunk_segments(rows)
    if not segments:
        return []

    budget = max(
        512, int(context_tokens) - RESERVED_COMPLETION_TOKENS - RESERVED_PROMPT_TOKENS
    )

    chunks: list[TranscriptChunk] = []
    current: list[ChunkSegment] = []
    current_tokens = 0
    overlap_in_current = 0
    index = 0

    def flush(*, carry_overlap: bool) -> list[ChunkSegment]:
        """Emit the accumulated window; return the segments the next one should repeat."""
        nonlocal index, current, current_tokens, overlap_in_current
        if not current:
            return []
        body = render_segments(current)
        chunks.append(
            TranscriptChunk(
                index=index,
                segments=tuple(current),
                body=body,
                start_ms=current[0].start_ms,
                end_ms=current[-1].end_ms,
                token_estimate=estimate_tokens(body),
                overlap_count=overlap_in_current,
            )
        )
        index += 1
        carry: list[ChunkSegment] = []
        if carry_overlap and overlap_ms > 0:
            boundary = current[-1].end_ms - overlap_ms
            carry = [segment for segment in current if segment.end_ms > boundary]
            # Never carry the entire window: that would make no forward progress and, with
            # a single over-long segment, loop for ever.
            if len(carry) >= len(current):
                carry = carry[-1:] if len(current) > 1 else []
        current = list(carry)
        current_tokens = sum(estimate_tokens(segment.text) + 12 for segment in carry)
        overlap_in_current = len(carry)
        return carry

    for segment in segments:
        # +12 for the marker, the timestamp and the newline.
        cost = estimate_tokens(segment.text) + 12
        if current and current_tokens + cost > budget:
            flush(carry_overlap=True)
        current.append(segment)
        current_tokens += cost

    # The tail: fold a very short remainder back into its predecessor rather than spend a
    # whole model call on it. Only when there is something to fold into, and only when the
    # combination still fits.
    if current and chunks and overlap_in_current < len(current):
        span = current[-1].end_ms - current[0].start_ms
        fresh = [segment for segment in current if segment.seq not in chunks[-1].segment_ids]
        if span < _MIN_TAIL_MS and fresh:
            previous = chunks.pop()
            index -= 1
            merged = list(previous.segments) + fresh
            body = render_segments(merged)
            if estimate_tokens(body) <= budget:
                chunks.append(
                    TranscriptChunk(
                        index=previous.index,
                        segments=tuple(merged),
                        body=body,
                        start_ms=merged[0].start_ms,
                        end_ms=merged[-1].end_ms,
                        token_estimate=estimate_tokens(body),
                        overlap_count=previous.overlap_count,
                    )
                )
                index += 1
                current = []
            else:
                chunks.append(previous)
                index += 1
    if current:
        flush(carry_overlap=False)

    return chunks
