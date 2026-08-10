"""The grounding layer: schema, chunking, verification, deduplication.

**No model is loaded by any test in this file, and none may be.** That is the point of
the design: the language model proposes, and everything that decides what survives is
ordinary code. If a test here needed a 2.3 GB download to run, the branches that matter
-- a hallucinated quote, an invented owner, a truncated window -- would not be tested at
all, because a model-dependent test is too slow and too non-deterministic to assert on.
"""

from __future__ import annotations

import pytest

from mom_igd.mom.chunking import (
    RESERVED_COMPLETION_TOKENS,
    RESERVED_PROMPT_TOKENS,
    ChunkSegment,
    TranscriptChunk,
    build_chunks,
    estimate_tokens,
    render_segments,
)
from mom_igd.mom.dedupe import deduplicate
from mom_igd.mom.schema import (
    EXTRACTION_GRAMMAR,
    ITEM_KINDS,
    MAX_ITEMS_PER_CHUNK,
    QUOTE_CHAR_LIMIT,
    SUMMARY_GRAMMAR,
    MinuteItem,
    coerce_extraction,
    coerce_summary,
)
from mom_igd.mom.verify import (
    TranscriptIndex,
    check_summary_numbers,
    mark_superseded,
    normalise,
    verify_items,
)


# ===========================================================================
# Schema and grammar
# ===========================================================================


@pytest.mark.parametrize("grammar", [EXTRACTION_GRAMMAR, SUMMARY_GRAMMAR])
def test_every_grammar_rule_is_on_one_line(grammar: str) -> None:
    """llama.cpp's GBNF parser ends a rule at the newline.

    A rule wrapped for readability parses as garbage, and it fails *late*:
    ``LlamaGrammar.from_string`` stores the string without parsing it, so the first
    symptom is an ``OSError`` from the sampler with no mention of a grammar. This test is
    the cheap version of the hour that cost.
    """
    for line in grammar.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        assert "::=" in stripped, (
            f"grammar line {stripped!r} continues a rule onto a new line. Every GBNF "
            "rule must be complete on one line."
        )


def test_grammar_item_ceiling_matches_the_declared_constant() -> None:
    """The generator warns when a window returns exactly the ceiling. It must be right."""
    assert f"{{0,{MAX_ITEMS_PER_CHUNK - 1}}}" in EXTRACTION_GRAMMAR, (
        f"MAX_ITEMS_PER_CHUNK is {MAX_ITEMS_PER_CHUNK}, so the grammar's item repetition "
        f"must be {{0,{MAX_ITEMS_PER_CHUNK - 1}}} (the first item is outside it)."
    )


def test_quote_limit_matches_the_grammar() -> None:
    assert QUOTE_CHAR_LIMIT == 12 * 16 + 15
    assert "quotestr ::= " in EXTRACTION_GRAMMAR
    assert 'c16{0,12} c{0,15}' in EXTRACTION_GRAMMAR


def test_every_item_field_appears_in_the_grammar() -> None:
    """A dataclass field the grammar cannot produce is a field that is always empty."""
    for name in ("kind", "text", "quote", "segments", "owner", "due"):
        assert f'\\"{name}\\"' in EXTRACTION_GRAMMAR, f"{name} is not in the grammar"


def test_no_grammar_mentions_a_speaker() -> None:
    """Phases 5 and 6 own attribution. Nothing here may invent it."""
    for grammar in (EXTRACTION_GRAMMAR, SUMMARY_GRAMMAR):
        lowered = grammar.lower()
        for banned in ("speaker", "pembicara", "penutur"):
            assert banned not in lowered


def test_coerce_treats_the_string_null_as_absent() -> None:
    """A small model writes ``"null"`` where the schema wants ``null``. Observed."""
    items = coerce_extraction(
        {
            "items": [
                {
                    "kind": "ACTION",
                    "text": "Menyiapkan dokumen.",
                    "quote": "tolong siapkan dokumen requirement",
                    "segments": [3],
                    "owner": "null",
                    "due": "belum ditentukan",
                }
            ]
        }
    )
    assert len(items) == 1
    assert items[0].owner is None
    assert items[0].due is None


def test_coerce_drops_an_unknown_kind_and_an_empty_quote() -> None:
    items = coerce_extraction(
        {
            "items": [
                {"kind": "RUMOUR", "text": "x", "quote": "y", "segments": [1]},
                {"kind": "ACTION", "text": "x", "quote": "", "segments": [1]},
                {"kind": "ACTION", "text": "", "quote": "y", "segments": [1]},
                {"kind": "action", "text": "x", "quote": "y", "segments": [1]},
            ]
        }
    )
    assert [item.kind for item in items] == ["ACTION"]


def test_coerce_summary_strips_blank_lines() -> None:
    title, lines = coerce_summary({"title": "  Rapat  ", "summary": ["a", "   ", "b"]})
    assert title == "Rapat"
    assert lines == ("a", "b")


# ===========================================================================
# Chunking
# ===========================================================================


def _rows(count: int, *, words: int = 12, seconds: int = 5) -> list[dict[str, object]]:
    text = " ".join(f"kata{index}" for index in range(words))
    return [
        {
            "seq": index,
            "start_ms": index * seconds * 1000,
            "end_ms": (index + 1) * seconds * 1000,
            "text": text,
        }
        for index in range(count)
    ]


def test_no_window_exceeds_the_context_budget() -> None:
    context = 6144
    budget = context - RESERVED_COMPLETION_TOKENS - RESERVED_PROMPT_TOKENS
    chunks = build_chunks(_rows(600), context_tokens=context)
    assert chunks
    for chunk in chunks:
        assert chunk.token_estimate <= budget, (
            f"window {chunk.index} estimates {chunk.token_estimate} tokens against a "
            f"budget of {budget}. Overflow is silent: llama.cpp drops the tail."
        )


def test_windows_overlap_so_a_boundary_decision_is_seen_twice() -> None:
    chunks = build_chunks(_rows(400), context_tokens=6144, overlap_ms=15_000)
    assert len(chunks) > 1
    for previous, following in zip(chunks, chunks[1:]):
        shared = previous.segment_ids & following.segment_ids
        assert shared, (
            "consecutive windows share no segment, so a decision stated across the cut "
            "would be seen by neither."
        )
        assert following.overlap_count == len(shared)


def test_every_segment_appears_in_at_least_one_window() -> None:
    rows = _rows(400)
    chunks = build_chunks(rows, context_tokens=6144)
    covered = set().union(*(chunk.segment_ids for chunk in chunks))
    assert covered == {int(row["seq"]) for row in rows}


def test_a_single_oversized_segment_terminates() -> None:
    """A segment larger than the whole budget must not loop the carry-forward for ever."""
    rows = [
        {"seq": 0, "start_ms": 0, "end_ms": 600_000, "text": "kata " * 20_000},
        {"seq": 1, "start_ms": 600_000, "end_ms": 605_000, "text": "penutup rapat"},
    ]
    chunks = build_chunks(rows, context_tokens=6144)
    assert 1 <= len(chunks) <= 3
    assert 1 in set().union(*(chunk.segment_ids for chunk in chunks))


def test_empty_and_whitespace_only_segments_are_dropped() -> None:
    rows = [
        {"seq": 0, "start_ms": 0, "end_ms": 1000, "text": "   "},
        {"seq": 1, "start_ms": 1000, "end_ms": 2000, "text": ""},
        {"seq": 2, "start_ms": 2000, "end_ms": 3000, "text": "ada isinya"},
    ]
    chunks = build_chunks(rows, context_tokens=6144)
    assert len(chunks) == 1
    assert chunks[0].segment_ids == frozenset({2})


def test_no_chunks_from_an_empty_transcript() -> None:
    assert build_chunks([], context_tokens=6144) == []


def test_render_includes_the_citation_marker_and_a_timestamp() -> None:
    body = render_segments(
        [ChunkSegment(seq=7, start_ms=3_661_000, end_ms=3_665_000, text="halo")]
    )
    assert body.startswith("[S7] (01:01:01) halo")


def test_the_instruction_block_fits_the_reserve_held_back_for_it() -> None:
    """Otherwise the overflow is silent and the minute misses the end of the meeting.

    The chunker subtracts a fixed reserve for the instructions before deciding how much
    transcript fits. If the instructions outgrow it, every window is over budget by the
    difference -- llama.cpp drops the tail without an error, so the only symptom is items
    missing from the end of each window.
    """
    from mom_igd.mom.prompts import EXTRACTION_SYSTEM, SUMMARY_SYSTEM, build_extraction_user

    segments = (ChunkSegment(seq=0, start_ms=0, end_ms=1000, text="x"),)
    body = render_segments(segments)
    chunk = TranscriptChunk(
        index=0, segments=segments, body=body, start_ms=0, end_ms=1000, token_estimate=1
    )
    scaffold = EXTRACTION_SYSTEM + build_extraction_user(
        chunk, chunk_count=99, meeting_title="Rapat Dengan Judul Yang Cukup Panjang Sekali"
    )
    assert estimate_tokens(scaffold) <= RESERVED_PROMPT_TOKENS, (
        f"the extraction instructions estimate {estimate_tokens(scaffold)} tokens against "
        f"a reserve of {RESERVED_PROMPT_TOKENS}. Raise RESERVED_PROMPT_TOKENS or shorten "
        "the prompt -- do not leave them disagreeing."
    )
    assert estimate_tokens(SUMMARY_SYSTEM) <= RESERVED_PROMPT_TOKENS


def test_token_estimate_is_pessimistic_against_the_measured_ratio() -> None:
    """Measured 3.10 chars/token on a marked-up prompt; the estimate must not exceed it.

    Erring low costs an extra call. Erring high overflows the context window silently,
    and the tail of the meeting is simply missing from the minute.
    """
    text = "a" * 3100
    assert estimate_tokens(text) >= 1000


# ===========================================================================
# Verification
# ===========================================================================

SEGMENTS = (
    ChunkSegment(seq=0, start_ms=0, end_ms=8000, text="Selamat pagi semua, kita mulai rapatnya."),
    ChunkSegment(seq=1, start_ms=8000, end_ms=16000, text="Pak Rendra bilang target integrasi tanggal 20 Agustus."),
    ChunkSegment(seq=2, start_ms=16000, end_ms=24000, text="Kita sepakat menunda go-live ke tanggal 5 September."),
    ChunkSegment(seq=3, start_ms=24000, end_ms=32000, text="Bu Sinta tolong siapkan dokumen requirement sebelum hari Jumat."),
)


def _chunk(segments=SEGMENTS) -> TranscriptChunk:
    body = render_segments(segments)
    return TranscriptChunk(
        index=0,
        segments=tuple(segments),
        body=body,
        start_ms=segments[0].start_ms,
        end_ms=segments[-1].end_ms,
        token_estimate=estimate_tokens(body),
    )


def _verify(items, *, roster=None):
    chunk = _chunk()
    index = TranscriptIndex(
        [{"seq": segment.seq, "text": segment.text} for segment in SEGMENTS]
    )
    times = {segment.seq: (segment.start_ms, segment.end_ms) for segment in SEGMENTS}
    return verify_items(
        items, chunk=chunk, transcript_index=index, segment_times=times, roster=roster
    )


def _item(**kwargs) -> MinuteItem:
    base = {
        "kind": "ACTION",
        "text": "teks",
        "quote": "kutipan",
        "segment_ids": (0,),
    }
    base.update(kwargs)
    return MinuteItem(**base)  # type: ignore[arg-type]


def test_an_exact_quote_in_the_cited_segment_verifies() -> None:
    [result] = _verify(
        [_item(quote="Kita sepakat menunda go-live ke tanggal 5 September", segment_ids=(2,))]
    )
    assert result.verification == "VERIFIED"
    assert result.segment_ids == (2,)
    assert result.start_ms == 16000 and result.end_ms == 24000
    assert result.verification_notes == ()


def test_a_quote_cited_to_the_wrong_segment_is_rebound_not_dropped() -> None:
    [result] = _verify(
        [_item(quote="Kita sepakat menunda go-live ke tanggal 5 September", segment_ids=(0,))]
    )
    assert result.verification == "REBOUND"
    assert result.segment_ids == (2,)
    assert "QUOTE_FOUND_IN_OTHER_SEGMENT" in result.verification_notes


def test_a_fabricated_quote_is_unverified_and_kept() -> None:
    """Kept and marked. Dropping it hides that the model produced it."""
    [result] = _verify(
        [_item(quote="Anggaran tambahan sudah disetujui direksi kemarin", segment_ids=(2,))]
    )
    assert result.verification == "UNVERIFIED"
    assert "QUOTE_NOT_FOUND" in result.verification_notes


def test_words_scattered_across_the_window_do_not_count_as_a_quote() -> None:
    """Every word is present; the sentence is not. That is what a hallucination looks like."""
    [result] = _verify(
        [_item(quote="pagi rapatnya tanggal dokumen Jumat semua", segment_ids=(0,))]
    )
    assert result.verification == "UNVERIFIED"


def test_a_near_match_verifies_and_says_so() -> None:
    [result] = _verify(
        [_item(quote="Kita sepakat menunda go live ke tanggal 5 September ya", segment_ids=(2,))]
    )
    assert result.verification in ("VERIFIED", "REBOUND")
    assert "QUOTE_NEAR_MATCH" in result.verification_notes


def test_a_citation_outside_the_window_is_reported() -> None:
    [result] = _verify(
        [_item(quote="Kita sepakat menunda go-live ke tanggal 5 September", segment_ids=(99,))]
    )
    assert "CITATION_OUT_OF_RANGE" in result.verification_notes


def test_an_owner_the_meeting_never_named_is_removed() -> None:
    """The single most damaging output this system can produce."""
    [result] = _verify(
        [
            _item(
                quote="Bu Sinta tolong siapkan dokumen requirement sebelum hari Jumat",
                segment_ids=(3,),
                owner="Pak Hendra",
            )
        ]
    )
    assert result.owner is None
    assert "OWNER_NOT_IN_TRANSCRIPT" in result.verification_notes


def test_an_owner_the_meeting_named_survives() -> None:
    [result] = _verify(
        [
            _item(
                quote="Bu Sinta tolong siapkan dokumen requirement sebelum hari Jumat",
                segment_ids=(3,),
                owner="Bu Sinta",
            )
        ]
    )
    assert result.owner == "Bu Sinta"


def test_an_honorific_alone_cannot_ground_a_name() -> None:
    """"Pak" appears in the transcript and identifies nobody."""
    [result] = _verify(
        [
            _item(
                quote="Pak Rendra bilang target integrasi tanggal 20 Agustus",
                segment_ids=(1,),
                owner="Pak",
            )
        ]
    )
    assert result.owner is None
    assert "OWNER_NOT_A_NAME" in result.verification_notes


def test_the_roster_corrects_the_spelling_of_a_spoken_name() -> None:
    [result] = _verify(
        [
            _item(
                quote="Bu Sinta tolong siapkan dokumen requirement sebelum hari Jumat",
                segment_ids=(3,),
                owner="Bu Sinta",
            )
        ],
        roster={"sinta wijaya": "Sinta Wijaya"},
    )
    assert result.owner == "Sinta Wijaya"


def test_the_roster_cannot_introduce_a_name_the_meeting_never_said() -> None:
    """The roster canonicalises; it never grounds. Otherwise it is a list to guess from."""
    [result] = _verify(
        [
            _item(
                quote="Kita sepakat menunda go-live ke tanggal 5 September",
                segment_ids=(2,),
                owner="Hendra Kusuma",
            )
        ],
        roster={"hendra kusuma": "Hendra Kusuma"},
    )
    assert result.owner is None


def test_a_due_date_the_window_never_stated_is_removed() -> None:
    [result] = _verify(
        [
            _item(
                quote="Kita sepakat menunda go-live ke tanggal 5 September",
                segment_ids=(2,),
                due="akhir Desember",
            )
        ]
    )
    assert result.due is None
    assert "DUE_NOT_IN_TRANSCRIPT" in result.verification_notes


def test_a_due_date_the_window_stated_survives() -> None:
    [result] = _verify(
        [
            _item(
                quote="Bu Sinta tolong siapkan dokumen requirement sebelum hari Jumat",
                segment_ids=(3,),
                due="hari Jumat",
            )
        ]
    )
    assert result.due == "hari Jumat"


def test_a_garbled_due_date_is_dropped_not_asserted() -> None:
    """Observed: the model emitted "hari Kam4" for "hari Kamis".

    The old rule accepted any single matching token, and "hari" appears in every meeting,
    so a deadline that exists in no language would have been printed as fact.
    """
    [result] = _verify(
        [
            _item(
                quote="Bu Sinta tolong siapkan dokumen requirement sebelum hari Jumat",
                segment_ids=(3,),
                due="hari Kam4",
            )
        ]
    )
    assert result.due is None
    assert "DUE_NOT_IN_TRANSCRIPT" in result.verification_notes


def test_a_due_date_of_pure_scaffolding_falls_back_to_the_plain_check() -> None:
    """"minggu ini" is all scaffolding and still a real answer if the meeting said it."""
    segments = SEGMENTS + (
        ChunkSegment(seq=4, start_ms=32000, end_ms=40000, text="Kerjakan minggu ini ya."),
    )
    chunk_body = render_segments(segments)
    chunk = TranscriptChunk(
        index=0,
        segments=segments,
        body=chunk_body,
        start_ms=0,
        end_ms=40000,
        token_estimate=estimate_tokens(chunk_body),
    )
    index = TranscriptIndex(
        [{"seq": segment.seq, "text": segment.text} for segment in segments]
    )
    times = {segment.seq: (segment.start_ms, segment.end_ms) for segment in segments}
    [result] = verify_items(
        [
            _item(
                quote="Bu Sinta tolong siapkan dokumen requirement sebelum hari Jumat",
                segment_ids=(3,),
                due="minggu ini",
            )
        ],
        chunk=chunk,
        transcript_index=index,
        segment_times=times,
    )
    assert result.due == "minggu ini"


def test_a_multi_word_due_date_needs_every_naming_word() -> None:
    [result] = _verify(
        [
            _item(
                quote="Bu Sinta tolong siapkan dokumen requirement sebelum hari Jumat",
                segment_ids=(3,),
                due="hari Jumat depan",
            )
        ]
    )
    # "jumat" was said; "depan" is scaffolding, so this still holds.
    assert result.due == "hari Jumat depan"


def test_a_two_word_quote_is_too_short_to_be_evidence() -> None:
    [result] = _verify([_item(quote="selamat pagi", segment_ids=(0,))])
    assert result.verification == "UNVERIFIED"


def test_a_quote_spanning_two_segments_verifies_across_the_boundary() -> None:
    index = TranscriptIndex(
        [{"seq": 0, "text": "kita akan menunda"}, {"seq": 1, "text": "go-live ke September"}]
    )
    located = index.locate("kita akan menunda go-live ke September")
    assert located is not None
    assert located.segment_ids == (0, 1)


def test_normalise_folds_punctuation_and_case() -> None:
    assert normalise("Go-Live, ke 5 September!") == "go live ke 5 september"


# ===========================================================================
# Summary numbers
# ===========================================================================


def test_a_number_absent_from_every_item_is_flagged() -> None:
    unsupported = check_summary_numbers(
        ["Anggaran 99 juta disetujui."], sources=["Go-live ditunda ke 5 September."]
    )
    assert unsupported == ("99",)


def test_a_number_present_in_a_source_is_not_flagged() -> None:
    assert check_summary_numbers(
        ["Go-live pada 5 September."], sources=["Menunda go-live ke tanggal 5 September."]
    ) == ()


def test_thousands_separators_do_not_create_a_false_positive() -> None:
    assert check_summary_numbers(
        ["Biaya 1.500 unit."], sources=["totalnya 1500 unit saja"]
    ) == ()


# ===========================================================================
# Deduplication
# ===========================================================================


def _dupe(text: str, *, kind: str = "DECISION", start: int = 1000, **kwargs) -> MinuteItem:
    base = {
        "kind": kind,
        "text": text,
        "quote": text,
        "segment_ids": (1,),
        "verification": "VERIFIED",
        "start_ms": start,
        "end_ms": None if start is None else start + 5000,
    }
    base.update(kwargs)
    return MinuteItem(**base)  # type: ignore[arg-type]


def test_two_phrasings_of_one_decision_merge() -> None:
    merged = deduplicate(
        [
            _dupe("Kita sepakat menunda go-live ke tanggal 5 September"),
            _dupe("Kita sepakat menunda go-live ke tanggal 5 September itu", start=2000),
        ]
    )
    assert len(merged) == 1
    assert merged[0].merged_count == 2


def test_a_decision_and_a_discussion_never_merge() -> None:
    """Collapsing them would delete the decision, or demote it, depending on order."""
    merged = deduplicate(
        [
            _dupe("Menunda go-live ke 5 September", kind="DECISION"),
            _dupe("Menunda go-live ke 5 September", kind="DISCUSSION"),
        ]
    )
    assert len(merged) == 2


def test_the_same_topic_far_apart_in_time_stays_separate() -> None:
    merged = deduplicate(
        [
            _dupe("Membahas anggaran lisensi tahunan", kind="DISCUSSION", start=60_000),
            _dupe("Membahas anggaran lisensi tahunan", kind="DISCUSSION", start=3_000_000),
        ]
    )
    assert len(merged) == 2


def test_a_merge_keeps_the_named_owner_over_the_missing_one() -> None:
    merged = deduplicate(
        [
            _dupe("Menyiapkan dokumen requirement", kind="ACTION", owner=None),
            _dupe("Menyiapkan dokumen requirement", kind="ACTION", owner="Sinta", start=2000),
        ]
    )
    assert len(merged) == 1
    assert merged[0].owner == "Sinta"


def test_two_different_owners_are_recorded_as_a_conflict_not_silently_picked() -> None:
    merged = deduplicate(
        [
            _dupe("Menyiapkan dokumen requirement", kind="ACTION", owner="Sinta"),
            _dupe("Menyiapkan dokumen requirement", kind="ACTION", owner="Rendra", start=2000),
        ]
    )
    assert len(merged) == 1
    assert any(
        note.startswith("OWNER_CONFLICT:") for note in merged[0].verification_notes
    ), merged[0].verification_notes


def test_a_merge_keeps_the_stronger_verification_and_unions_the_citations() -> None:
    merged = deduplicate(
        [
            _dupe(
                "Menunda go-live ke 5 September",
                verification="UNVERIFIED",
                segment_ids=(1,),
            ),
            _dupe(
                "Menunda go-live ke 5 September",
                verification="VERIFIED",
                segment_ids=(2,),
                start=2000,
            ),
        ]
    )
    assert len(merged) == 1
    assert merged[0].verification == "VERIFIED"
    assert set(merged[0].segment_ids) == {1, 2}


def test_items_come_back_in_meeting_order_with_untimed_ones_last() -> None:
    merged = deduplicate(
        [
            _dupe("Poin ketiga yang dibahas", start=30_000),
            _dupe("Poin tanpa waktu sama sekali", start=None),
            _dupe("Poin pertama yang dibahas", start=1_000),
        ]
    )
    assert [item.start_ms for item in merged] == [1_000, 30_000, None]


def test_a_paraphrased_duplicate_merges_on_its_quote_not_its_text() -> None:
    """Character similarity does not recognise a paraphrase -- measured at 0.543.

    What catches it is the quote: verbatim transcript text, which two windows quoting the
    same sentence reproduce almost identically even when they summarise it differently.
    The shared citation is required, so two different points that merely quote nearby
    text do not collapse.
    """
    quote = "kita sepakat untuk menunda go-live ke tanggal 5 September"
    merged = deduplicate(
        [
            _dupe("Go-live ditunda ke 5 September untuk memberi waktu testing dua minggu",
                  quote=quote, segment_ids=(4,)),
            _dupe("Keputusan menunda go-live ke tanggal 5 September agar ada waktu testing",
                  quote=quote, segment_ids=(4,), start=3000),
        ]
    )
    assert len(merged) == 1, "a shared verbatim quote should have merged these"


def test_similar_short_texts_about_different_things_stay_separate() -> None:
    """0.857 on characters, and two different agenda points. Short texts need a higher bar."""
    merged = deduplicate(
        [_dupe("Poin ketiga yang dibahas"), _dupe("Poin pertama yang dibahas", start=2000)]
    )
    assert len(merged) == 2


def test_dedupe_of_an_empty_list_is_empty() -> None:
    assert deduplicate([]) == []


def test_every_kind_is_representable() -> None:
    for kind in ITEM_KINDS:
        assert deduplicate([_dupe("x y z", kind=kind)])[0].kind == kind


# ===========================================================================
# Reversed decisions
# ===========================================================================


def _decision(text: str, quote: str, start: int, kind: str = "DECISION") -> MinuteItem:
    return MinuteItem(
        kind=kind,
        text=text,
        quote=quote,
        segment_ids=(1,),
        verification="VERIFIED",
        start_ms=start,
        end_ms=start + 9000,
    )


def _notes(item: MinuteItem) -> list[str]:
    return [note for note in item.verification_notes if note.startswith("POSSIBLY_SUPERSEDED")]


def test_a_decision_cancelled_by_a_back_reference_is_flagged() -> None:
    """The pattern that actually occurs: the reversal points back, it does not restate.

    Measured on a real run, the reversal shared exactly **one** distinctive word with the
    decision it cancelled, and that word was the filler "begitu". Word overlap alone could
    never have found this.
    """
    first, second = mark_superseded(
        [
            _decision(
                "UAT dipindahkan ke server cadangan yang di ruang server utama.",
                "kita pindahkan dulu UAT ke server cadangan yang di ruang server utama",
                187_000,
            ),
            _decision(
                "Keputusan sebelumnya dibatalkan. UAT tetap menunggu perbaikan lantai tiga.",
                "keputusan tadi kita batalkan, UAT tetap tunggu perbaikan lantai tiga",
                253_000,
            ),
        ]
    )
    assert _notes(first) == ["POSSIBLY_SUPERSEDED:00:04:13"]
    assert _notes(second) == [], "the reversal itself is not superseded"


def test_a_decision_cancelled_by_restating_its_subject_is_flagged() -> None:
    first, _ = mark_superseded(
        [
            _decision(
                "Vendor Alpha dipilih untuk pengadaan lisensi antivirus.",
                "kita pakai vendor Alpha untuk pengadaan lisensi antivirus",
                60_000,
            ),
            _decision(
                "Pemilihan vendor Alpha untuk lisensi antivirus dibatalkan.",
                "pemilihan vendor Alpha untuk lisensi antivirus kita batalkan",
                300_000,
            ),
        ]
    )
    assert _notes(first), "a restated subject plus a reversal word must link them"


def test_unrelated_decisions_are_not_flagged() -> None:
    """A caution that fires on everything is a caution nobody reads."""
    items = mark_superseded(
        [
            _decision(
                "Go-live ditunda ke 5 September.", "kita tunda go-live ke 5 September", 10_000
            ),
            _decision(
                "Anggaran lisensi diajukan setelah UAT selesai.",
                "kami ajukan resmi setelah UAT selesai",
                600_000,
            ),
        ]
    )
    assert all(not _notes(item) for item in items)


def test_a_back_reference_lands_on_the_nearest_preceding_decision_only() -> None:
    """"The decision just now" means the last one, not every one before it."""
    first, second, third = mark_superseded(
        [
            _decision("Rapat dipindah ke ruang besar.", "rapat dipindah ke ruang besar", 10_000),
            _decision("Presentasi dibuat oleh tim produk.", "presentasi dibuat tim produk", 60_000),
            _decision(
                "Keputusan tadi kita batalkan.", "keputusan tadi kita batalkan", 120_000
            ),
        ]
    )
    assert not _notes(first), "an older decision must not be caught by a later reversal"
    assert _notes(second), "the nearest preceding decision is the referent"
    assert not _notes(third)


def test_a_reversal_word_alone_flags_nothing() -> None:
    items = mark_superseded(
        [
            _decision("Go-live ditunda ke 5 September.", "tunda go-live ke September", 10_000),
            _decision(
                "Format laporan diubah menjadi bulanan.",
                "format laporan kita ubah jadi bulanan",
                400_000,
            ),
        ]
    )
    assert not _notes(items[0]), (
        "'diubah' describes this decision's own content; without a back-reference or a "
        "shared subject it says nothing about the earlier one"
    )


def test_only_decisions_are_flagged() -> None:
    items = mark_superseded(
        [
            _decision("Menyiapkan dokumen.", "tolong siapkan dokumen", 10_000, kind="ACTION"),
            _decision(
                "Keputusan sebelumnya dibatalkan.", "keputusan tadi kita batalkan", 60_000
            ),
        ]
    )
    assert not _notes(items[0]), "an action item is not superseded, it is completed or not"


# ===========================================================================
# Containment
# ===========================================================================


def test_an_item_wholly_restating_another_is_merged() -> None:
    """Ratio misses this: one text is twice the other's length, and says nothing more."""
    merged = deduplicate(
        [
            _dupe(
                "Keputusan sebelumnya dibatalkan. UAT tetap menunggu perbaikan lantai tiga."
            ),
            _dupe("UAT tetap menunggu perbaikan lantai tiga.", start=2000),
        ]
    )
    assert len(merged) == 1
    assert merged[0].merged_count == 2
    assert "Keputusan sebelumnya" in merged[0].text, "the fuller text is kept"


def test_a_short_fragment_does_not_swallow_the_item_it_appears_inside() -> None:
    merged = deduplicate(
        [
            _dupe("Semua setuju dengan usulan anggaran lisensi tahunan yang baru"),
            _dupe("Semua setuju", start=2000),
        ]
    )
    assert len(merged) == 2
