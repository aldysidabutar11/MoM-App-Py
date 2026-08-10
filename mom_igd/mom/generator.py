"""Map-reduce over a transcript: extract per window, verify, merge, then summarise.

The model is reached through one injected callable, ``run_prompts``. Everything else here
-- windowing, parsing, verification, deduplication, the decision about what a truncated
answer means -- is ordinary code that runs in the **parent** process, where no model is
loaded.

That split is the design, not an implementation detail:

* The verifier never runs in the same process as the thing it is checking, so it cannot be
  influenced by it and cannot be quietly bypassed by a future change inside the worker.
* The whole pipeline is testable end to end with a fake ``run_prompts`` -- no 2.3 GB
  download, no minutes of CPU, no model in the test suite at all. Which means the branches
  that matter (a truncated window, a hallucinated quote, an invented owner, a window that
  returned nothing) get tested, and those are exactly the branches a model-dependent test
  would be too slow and too non-deterministic to reach.
* It costs two model loads instead of one, because the summary prompt is built from items
  that only exist after verification. Measured at 2.3 s each against a run of ten to
  fifteen minutes, with the file already in the page cache the second time.

**Nothing is dropped quietly.** A window that fails to parse, a window that hit the item
ceiling, a window truncated twice -- each becomes a warning naming the affected minutes of
the meeting, so the operator can see which part of the recording the minute does not
cover. A gap the reader cannot see is worse than no minute at all.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, fields
from typing import Any, Callable, Final, Mapping, Sequence

from mom_igd.logging_setup import get_logger
from mom_igd.mom.chunking import (
    RESERVED_COMPLETION_TOKENS,
    TranscriptChunk,
    build_chunks,
    estimate_tokens,
    render_segments,
)
from mom_igd.mom.dedupe import deduplicate
from mom_igd.mom.prompts import (
    EXTRACTION_SYSTEM,
    SUMMARY_SYSTEM,
    build_extraction_user,
    build_summary_user,
)
from mom_igd.mom.schema import (
    EXTRACTION_GRAMMAR,
    MAX_ITEMS_PER_CHUNK,
    SUMMARY_GRAMMAR,
    MinuteDraft,
    MinuteItem,
    coerce_extraction,
    coerce_summary,
)
from mom_igd.mom.verify import (
    TranscriptIndex,
    check_summary_numbers,
    mark_superseded,
    verify_items,
)

__all__ = [
    "GenerationStats",
    "MinuteGenerationResult",
    "PromptSpec",
    "PromptRunner",
    "generate_minutes",
]

_LOG = get_logger("mom.generator")

#: Tokens allowed for one window's answer. Matches the reserve the chunker holds back, so
#: the two cannot drift apart into a window that does not leave room for its own reply.
EXTRACTION_MAX_TOKENS: Final[int] = RESERVED_COMPLETION_TOKENS

#: The summary is a title and up to eight sentences. Generous, and far below extraction.
SUMMARY_MAX_TOKENS: Final[int] = 700

#: How many times a truncated window is halved and retried. One. A window that truncates
#: twice is reported rather than retried into an unbounded loop; the meeting is not going
#: to become shorter.
_MAX_TRUNCATION_RETRIES: Final[int] = 1


@dataclass(frozen=True, slots=True)
class PromptSpec:
    """One prompt to run. Crosses a process boundary, so it is JSON-serialisable."""

    key: str
    system: str
    user: str
    grammar: str | None = None
    max_tokens: int = 1024

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "system": self.system,
            "user": self.user,
            "grammar": self.grammar,
            "max_tokens": self.max_tokens,
        }


#: ``(specs) -> [{"key", "text", "truncated", "completion_tokens", ...}, ...]``.
#: Production passes a worker call; tests pass a dictionary lookup.
PromptRunner = Callable[[Sequence[PromptSpec]], Sequence[Mapping[str, Any]]]


@dataclass(slots=True)
class GenerationStats:
    """What the run cost and what it covered. Persisted with the minute."""

    chunk_count: int = 0
    chunks_parsed: int = 0
    chunks_failed: int = 0
    chunks_truncated: int = 0
    chunks_at_item_ceiling: int = 0
    raw_item_count: int = 0
    merged_item_count: int = 0
    verified_count: int = 0
    rebound_count: int = 0
    unverified_count: int = 0
    superseded_count: int = 0
    owners_dropped: int = 0
    dues_dropped: int = 0
    citations_out_of_range: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_seconds: float = 0.0
    total_seconds: float = 0.0
    covered_ms: int = 0
    transcript_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            entry.name: (
                round(value, 3)
                if isinstance(value := getattr(self, entry.name), float)
                else value
            )
            for entry in fields(self)
        }


@dataclass(slots=True)
class MinuteGenerationResult:
    draft: MinuteDraft
    stats: GenerationStats
    model: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft": self.draft.to_dict(),
            "stats": self.stats.to_dict(),
            "model": self.model,
        }


def _timestamp(milliseconds: int | None) -> str:
    total = max(0, int(milliseconds or 0)) // 1000
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _split(chunk: TranscriptChunk) -> list[TranscriptChunk]:
    """Halve a window. Used only after a truncated answer.

    A single-segment window cannot be halved, and is returned unchanged so the caller
    reports it rather than looping.
    """
    if len(chunk.segments) < 2:
        return [chunk]
    middle = len(chunk.segments) // 2
    halves: list[TranscriptChunk] = []
    for offset, part in enumerate((chunk.segments[:middle], chunk.segments[middle:])):
        body = render_segments(part)
        halves.append(
            TranscriptChunk(
                # Fractional indexes keep the ordering of the parent window without
                # colliding with any other window's index.
                index=chunk.index * 100 + offset,
                segments=part,
                body=body,
                start_ms=part[0].start_ms,
                end_ms=part[-1].end_ms,
                token_estimate=estimate_tokens(body),
                overlap_count=chunk.overlap_count if offset == 0 else 0,
                # The prompt still says which part of the meeting this is. The derived
                # index above is a lookup key, not a place in the agenda.
                display_index=chunk.position,
            )
        )
    return halves


def _extraction_spec(
    chunk: TranscriptChunk, *, chunk_count: int, meeting_title: str | None
) -> PromptSpec:
    return PromptSpec(
        key=f"chunk-{chunk.index}",
        system=EXTRACTION_SYSTEM,
        user=build_extraction_user(
            chunk, chunk_count=chunk_count, meeting_title=meeting_title
        ),
        grammar=EXTRACTION_GRAMMAR,
        max_tokens=EXTRACTION_MAX_TOKENS,
    )


def generate_minutes(
    segments: Sequence[Mapping[str, Any]],
    *,
    run_prompts: PromptRunner,
    meeting_title: str | None = None,
    roster: Mapping[str, str] | None = None,
    context_tokens: int = 8192,
    model: Mapping[str, Any] | None = None,
) -> MinuteGenerationResult:
    """Produce a draft minute from a transcript's active segments.

    ``segments`` are transcript segment rows: ``seq``, ``start_ms``, ``end_ms``, ``text``.
    ``roster`` maps a normalised participant name to its official display form, and is used
    **only** to correct the spelling of a name the meeting already said -- see
    :func:`mom_igd.mom.verify.verify_items`.
    """
    started = time.perf_counter()
    stats = GenerationStats()
    warnings: list[str] = []

    chunks = build_chunks(segments, context_tokens=context_tokens)
    stats.chunk_count = len(chunks)

    # Coverage is measured in **segment time**, not in window spans.
    #
    # An earlier version summed `chunk.end_ms - chunk.start_ms` against the transcript's
    # last timestamp, and reported 68 % on a transcript where every segment had in fact
    # been read. Two reasons it could not work: silence before the first segment and
    # between regions belongs to no window and never could, and segment times are not
    # monotonic -- a long segment can end after a later one starts, so the last segment's
    # end is not the window's end. The result was a warning that fires on a complete run,
    # which is exactly the kind of warning that teaches an operator to ignore warnings.
    #
    # Summed per segment id, so the overlap between windows is not counted twice.
    durations = {
        int(row["seq"]): max(0, int(row["end_ms"]) - int(row["start_ms"]))
        for row in segments
    }
    stats.transcript_ms = sum(durations.values())
    covered_seqs: set[int] = set()
    if not chunks:
        stats.total_seconds = time.perf_counter() - started
        return MinuteGenerationResult(
            draft=MinuteDraft(
                title=meeting_title or "Notulen Rapat",
                summary=(),
                items=(),
                warnings=("TRANSCRIPT_EMPTY: transkrip tidak memuat teks apa pun.",),
            ),
            stats=stats,
            model=dict(model or {}),
        )

    transcript_index = TranscriptIndex(
        [
            {"seq": int(row["seq"]), "text": str(row.get("text") or "")}
            for row in segments
        ]
    )
    segment_times = {
        int(row["seq"]): (int(row["start_ms"]), int(row["end_ms"])) for row in segments
    }

    # ---------------------------------------------------------------- map
    collected: list[MinuteItem] = []
    pending = list(chunks)
    attempt = 0

    while pending:
        specs = [
            _extraction_spec(chunk, chunk_count=len(chunks), meeting_title=meeting_title)
            for chunk in pending
        ]
        outputs = {
            str(entry.get("key")): entry for entry in run_prompts(specs) if entry is not None
        }
        retry: list[TranscriptChunk] = []

        for chunk in pending:
            output = outputs.get(f"chunk-{chunk.index}")
            window = f"{_timestamp(chunk.start_ms)}-{_timestamp(chunk.end_ms)}"
            if output is None:
                stats.chunks_failed += 1
                warnings.append(
                    f"CHUNK_NO_RESPONSE: bagian {window} tidak menghasilkan jawaban; "
                    "isi bagian ini tidak masuk ke notulen."
                )
                continue

            stats.prompt_tokens += int(output.get("prompt_tokens") or 0)
            stats.completion_tokens += int(output.get("completion_tokens") or 0)
            stats.model_seconds += float(output.get("seconds") or 0.0)

            if output.get("truncated"):
                stats.chunks_truncated += 1
                halves = _split(chunk)
                if attempt < _MAX_TRUNCATION_RETRIES and len(halves) > 1:
                    retry.extend(halves)
                    continue
                stats.chunks_failed += 1
                warnings.append(
                    f"CHUNK_TRUNCATED: jawaban untuk bagian {window} terpotong dan tidak "
                    "bisa dibaca; isi bagian ini tidak masuk ke notulen."
                )
                continue

            try:
                payload = json.loads(str(output.get("text") or ""))
            except (TypeError, ValueError):
                stats.chunks_failed += 1
                warnings.append(
                    f"CHUNK_UNPARSEABLE: jawaban untuk bagian {window} bukan JSON yang "
                    "valid; isi bagian ini tidak masuk ke notulen."
                )
                continue
            if not isinstance(payload, Mapping):
                stats.chunks_failed += 1
                warnings.append(
                    f"CHUNK_UNPARSEABLE: jawaban untuk bagian {window} tidak berbentuk "
                    "objek; isi bagian ini tidak masuk ke notulen."
                )
                continue

            raw = coerce_extraction(payload, chunk_index=chunk.index)
            if len(payload.get("items") or ()) >= MAX_ITEMS_PER_CHUNK:
                stats.chunks_at_item_ceiling += 1
                warnings.append(
                    f"CHUNK_ITEM_CEILING: bagian {window} mencapai batas "
                    f"{MAX_ITEMS_PER_CHUNK} item; mungkin ada poin lain yang tidak "
                    "tercatat di bagian ini."
                )
            stats.raw_item_count += len(raw)
            stats.chunks_parsed += 1
            covered_seqs.update(segment.seq for segment in chunk.segments)

            collected.extend(
                verify_items(
                    raw,
                    chunk=chunk,
                    transcript_index=transcript_index,
                    segment_times=segment_times,
                    roster=roster,
                )
            )

        pending = retry
        attempt += 1

    stats.covered_ms = sum(durations.get(seq, 0) for seq in covered_seqs)
    for item in collected:
        stats.owners_dropped += 1 if "OWNER_NOT_IN_TRANSCRIPT" in item.verification_notes else 0
        stats.owners_dropped += 1 if "OWNER_NOT_A_NAME" in item.verification_notes else 0
        stats.dues_dropped += 1 if "DUE_NOT_IN_TRANSCRIPT" in item.verification_notes else 0
        stats.citations_out_of_range += (
            1 if "CITATION_OUT_OF_RANGE" in item.verification_notes else 0
        )

    # ------------------------------------------------------------- reduce
    # Deduplicate first, then look for reversals: a decision merged from two windows
    # should be checked once, as one decision, against everything that came after it.
    items = mark_superseded(deduplicate(collected))
    stats.merged_item_count = len(items)
    stats.superseded_count = sum(
        1
        for item in items
        if any(note.startswith("POSSIBLY_SUPERSEDED") for note in item.verification_notes)
    )
    stats.verified_count = sum(1 for item in items if item.verification == "VERIFIED")
    stats.rebound_count = sum(1 for item in items if item.verification == "REBOUND")
    stats.unverified_count = sum(1 for item in items if item.verification == "UNVERIFIED")

    # The summary is written from grounded items only. An unverified item is a claim the
    # audio does not support, and letting one into the summary would launder it into the
    # part of the document a reader trusts most and checks least.
    grounded = [
        item
        for item in items
        if item.verification != "UNVERIFIED"
        and not any(
            note.startswith("POSSIBLY_SUPERSEDED") for note in item.verification_notes
        )
    ]

    title = meeting_title or "Notulen Rapat"
    summary: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()

    if grounded:
        spec = PromptSpec(
            key="summary",
            system=SUMMARY_SYSTEM,
            user=build_summary_user(grounded, meeting_title=meeting_title),
            grammar=SUMMARY_GRAMMAR,
            max_tokens=SUMMARY_MAX_TOKENS,
        )
        outputs = list(run_prompts([spec]))
        output = outputs[0] if outputs else None
        if output is None:
            warnings.append(
                "SUMMARY_MISSING: ringkasan tidak dapat dibuat; daftar poin di bawah "
                "tetap lengkap."
            )
        else:
            stats.prompt_tokens += int(output.get("prompt_tokens") or 0)
            stats.completion_tokens += int(output.get("completion_tokens") or 0)
            stats.model_seconds += float(output.get("seconds") or 0.0)
            try:
                payload = json.loads(str(output.get("text") or ""))
            except (TypeError, ValueError):
                payload = None
            if isinstance(payload, Mapping):
                generated_title, summary = coerce_summary(payload)
                if generated_title:
                    title = generated_title
                unsupported = check_summary_numbers(
                    summary,
                    sources=[item.text for item in grounded]
                    + [item.quote for item in grounded]
                    + [item.due or "" for item in grounded],
                )
                if unsupported:
                    warnings.append(
                        "SUMMARY_UNSUPPORTED_NUMBERS: ringkasan memuat angka yang tidak "
                        f"ada di poin manapun ({', '.join(unsupported)}). Periksa ulang "
                        "sebelum notulen dipakai."
                    )
            else:
                warnings.append(
                    "SUMMARY_UNPARSEABLE: ringkasan tidak dapat dibaca; daftar poin di "
                    "bawah tetap lengkap."
                )
    elif items:
        warnings.append(
            "SUMMARY_SKIPPED: tidak ada poin yang terverifikasi terhadap rekaman, "
            "sehingga ringkasan tidak dibuat."
        )
    else:
        warnings.append(
            "NO_ITEMS: model tidak menemukan keputusan, tindak lanjut, pembahasan, atau "
            "isu di transkrip ini."
        )

    if stats.superseded_count:
        warnings.append(
            f"SUPERSEDED_DECISIONS: {stats.superseded_count} keputusan tampaknya "
            "dibatalkan atau diubah di bagian rapat berikutnya, dan ditandai. Pastikan "
            "keputusan yang berlaku sudah benar sebelum notulen dipakai."
        )
    if stats.unverified_count:
        warnings.append(
            f"UNVERIFIED_ITEMS: {stats.unverified_count} poin tidak dapat dicocokkan "
            "dengan rekaman dan ditandai. Jangan dipakai tanpa diperiksa."
        )

    stats.total_seconds = time.perf_counter() - started
    _LOG.info("mom.generated", extra=stats.to_dict())

    return MinuteGenerationResult(
        draft=MinuteDraft(
            title=title,
            summary=summary,
            items=tuple(items),
            summary_unsupported_numbers=unsupported,
            warnings=tuple(dict.fromkeys(warnings)),
        ),
        stats=stats,
        model=dict(model or {}),
    )
