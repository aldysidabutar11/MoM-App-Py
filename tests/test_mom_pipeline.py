"""Generation, persistence, rendering and the boundaries the minutes stage must respect.

Every test here runs the **whole** map-reduce with a fake prompt runner. No model is
downloaded, nothing is loaded, and the run takes milliseconds -- which is what makes it
possible to assert on the branches that actually matter: a window that truncated, one that
returned nothing, one whose answer was not JSON, and a summary containing a number nobody
said.
"""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile

import pytest

from mom_igd.mom import store
from mom_igd.mom.document import (
    DRAFT_BANNER,
    UNVERIFIED_MARK,
    build_document,
    render_html,
    render_markdown,
    render_text,
)
from mom_igd.mom.docx import render_docx
from mom_igd.mom.generator import generate_minutes
from mom_igd.mom.schema import MAX_ITEMS_PER_CHUNK

# ===========================================================================
# Fixtures
# ===========================================================================

SEGMENTS = [
    {"seq": 0, "start_ms": 0, "end_ms": 8000,
     "text": "Selamat pagi semua, kita mulai rapat koordinasi hari ini."},
    {"seq": 1, "start_ms": 8000, "end_ms": 16000,
     "text": "Pak Rendra bilang target integrasi SIMRS itu tanggal 20 Agustus."},
    {"seq": 2, "start_ms": 16000, "end_ms": 24000,
     "text": "Kita sepakat menunda go-live ke tanggal 5 September supaya ada waktu testing."},
    {"seq": 3, "start_ms": 24000, "end_ms": 32000,
     "text": "Bu Sinta tolong siapkan dokumen requirement sebelum hari Jumat."},
]


def _extraction(*items: dict) -> str:
    return json.dumps({"items": list(items)})


def _item(**kwargs) -> dict:
    base = {
        "kind": "DECISION",
        "text": "Go-live ditunda ke 5 September.",
        "quote": "Kita sepakat menunda go-live ke tanggal 5 September",
        "segments": [2],
        "owner": None,
        "due": None,
    }
    base.update(kwargs)
    return base


def _runner(chunk_text: str, summary_text: str | None = None, **overrides):
    """A fake prompt runner. Returns the same answer for every extraction window."""

    def run(specs):
        out = []
        for spec in specs:
            if spec.key == "summary":
                if summary_text is None:
                    continue
                out.append({"key": spec.key, "text": summary_text, "prompt_tokens": 90,
                            "completion_tokens": 40, "seconds": 4.0})
            else:
                entry = {"key": spec.key, "text": chunk_text, "prompt_tokens": 800,
                         "completion_tokens": 300, "seconds": 40.0}
                entry.update(overrides)
                out.append(entry)
        return out

    return run


SUMMARY = json.dumps({"title": "Rapat Koordinasi SIMRS",
                      "summary": ["Go-live ditunda ke 5 September."]})


# ===========================================================================
# Generation
# ===========================================================================


def test_a_grounded_item_survives_and_carries_its_evidence() -> None:
    result = generate_minutes(
        SEGMENTS, run_prompts=_runner(_extraction(_item()), SUMMARY)
    )
    assert result.stats.verified_count == 1
    [item] = result.draft.items
    assert item.verification == "VERIFIED"
    assert item.segment_ids == (2,)
    assert item.start_ms == 16000


def test_a_hallucinated_item_is_kept_marked_and_excluded_from_the_summary() -> None:
    """Three separate guarantees, and all three matter.

    Kept: the reviewer must see what the model produced. Marked: it must not read as a
    record. Excluded from the summary: the summary is written from verified items only, so
    an unsupported claim cannot be laundered into the part a reader trusts most.
    """
    captured: list[str] = []

    def run(specs):
        out = []
        for spec in specs:
            if spec.key == "summary":
                captured.append(spec.user)
                out.append({"key": spec.key, "text": SUMMARY})
            else:
                out.append({"key": spec.key, "text": _extraction(
                    _item(),
                    _item(kind="ACTION", text="Menyusun laporan keuangan.",
                          quote="Pak Hendra akan menyusun laporan keuangan triwulan",
                          segments=[1], owner="Pak Hendra", due="akhir bulan"),
                )})
        return out

    result = generate_minutes(SEGMENTS, run_prompts=run)
    assert result.stats.unverified_count == 1
    bad = [i for i in result.draft.items if i.verification == "UNVERIFIED"]
    assert len(bad) == 1
    assert bad[0].owner is None, "an owner the meeting never said must be removed"
    assert bad[0].due is None
    assert any("UNVERIFIED_ITEMS" in warning for warning in result.draft.warnings)
    assert captured, "the summary prompt should have been built"
    assert "laporan keuangan" not in captured[0].lower(), (
        "an unverified item reached the summary prompt"
    )


def test_a_number_the_meeting_never_said_is_flagged_on_the_summary() -> None:
    result = generate_minutes(
        SEGMENTS,
        run_prompts=_runner(
            _extraction(_item()),
            json.dumps({"title": "Rapat", "summary": ["Anggaran 99 juta disetujui."]}),
        ),
    )
    assert result.draft.summary_unsupported_numbers == ("99",)
    assert any("SUMMARY_UNSUPPORTED_NUMBERS" in w for w in result.draft.warnings)


def test_a_window_that_returns_no_answer_is_reported_not_ignored() -> None:
    def run(specs):
        return [{"key": spec.key, "text": SUMMARY} for spec in specs if spec.key == "summary"]

    result = generate_minutes(SEGMENTS, run_prompts=run)
    assert result.stats.chunks_failed == 1
    assert any("CHUNK_NO_RESPONSE" in w for w in result.draft.warnings)
    assert result.stats.covered_ms == 0


def test_a_window_whose_answer_is_not_json_is_reported() -> None:
    result = generate_minutes(SEGMENTS, run_prompts=_runner("maaf, saya tidak bisa", SUMMARY))
    assert result.stats.chunks_failed == 1
    assert any("CHUNK_UNPARSEABLE" in w for w in result.draft.warnings)


def test_a_truncated_window_is_retried_on_a_halved_window() -> None:
    calls: list[int] = []

    def run(specs):
        calls.append(len(specs))
        if len(calls) == 1:
            return [{"key": spec.key, "text": "{\"items\": [", "truncated": True}
                    for spec in specs]
        return [
            {"key": spec.key, "text": SUMMARY if spec.key == "summary" else _extraction(_item())}
            for spec in specs
        ]

    result = generate_minutes(SEGMENTS, run_prompts=run)
    assert calls[0] == 1, "one window to start with"
    assert calls[1] == 2, "the truncated window should have been halved and retried"
    assert result.stats.chunks_truncated == 1
    assert result.stats.chunks_failed == 0


def test_a_retried_half_window_still_tells_the_model_where_it_is() -> None:
    """A split window keeps its parent's place in the meeting.

    The split takes a derived index (``parent * 100 + half``) so its answer can be matched
    back, and the prompt used to print that index: window 3 of 9, once halved, announced
    itself as "bagian 301 dari 9". Nonsense handed to the model at exactly the moment it
    is already struggling, since the only reason a window is split is that its first
    answer was truncated.
    """
    from mom_igd.mom.chunking import ChunkSegment, TranscriptChunk, estimate_tokens, render_segments
    from mom_igd.mom.generator import _extraction_spec, _split

    segments = tuple(
        ChunkSegment(seq=i, start_ms=i * 5000, end_ms=(i + 1) * 5000,
                     text=f"kalimat nomor {i} yang cukup panjang untuk dipisah")
        for i in range(6)
    )
    body = render_segments(segments)
    parent = TranscriptChunk(
        index=3, segments=segments, body=body, start_ms=0, end_ms=30000,
        token_estimate=estimate_tokens(body),
    )
    halves = _split(parent)
    assert len(halves) == 2
    assert {half.index for half in halves} == {300, 301}, "identity stays unique"
    for half in halves:
        assert half.position == parent.position == 3
        first_line = _extraction_spec(
            half, chunk_count=9, meeting_title=None
        ).user.splitlines()[0]
        assert first_line == "Potongan transkrip bagian 4 dari 9.", first_line


def test_a_split_of_a_split_would_still_read_sensibly() -> None:
    """Only one retry round happens, but the position must not drift if that changes."""
    from mom_igd.mom.chunking import ChunkSegment, TranscriptChunk, estimate_tokens, render_segments
    from mom_igd.mom.generator import _split

    segments = tuple(
        ChunkSegment(seq=i, start_ms=i * 5000, end_ms=(i + 1) * 5000, text=f"kalimat {i}")
        for i in range(8)
    )
    body = render_segments(segments)
    parent = TranscriptChunk(
        index=2, segments=segments, body=body, start_ms=0, end_ms=40000,
        token_estimate=estimate_tokens(body),
    )
    grandchild = _split(_split(parent)[0])[0]
    assert grandchild.position == 2


def test_a_window_truncated_twice_is_given_up_on_and_named() -> None:
    def run(specs):
        return [
            {"key": spec.key, "text": SUMMARY} if spec.key == "summary"
            else {"key": spec.key, "text": "{", "truncated": True}
            for spec in specs
        ]

    result = generate_minutes(SEGMENTS, run_prompts=run)
    assert result.stats.chunks_failed >= 1
    warning = next(w for w in result.draft.warnings if "CHUNK_TRUNCATED" in w)
    assert ":" in warning, "the warning must name the affected part of the meeting"


def test_hitting_the_item_ceiling_is_reported_because_a_silent_cap_reads_as_complete() -> None:
    items = [
        _item(text=f"Poin nomor {index} yang cukup berbeda satu sama lain", segments=[2])
        for index in range(MAX_ITEMS_PER_CHUNK)
    ]
    result = generate_minutes(SEGMENTS, run_prompts=_runner(_extraction(*items), SUMMARY))
    assert result.stats.chunks_at_item_ceiling == 1
    assert any("CHUNK_ITEM_CEILING" in w for w in result.draft.warnings)


def test_coverage_is_full_when_every_window_parsed() -> None:
    """Regression: window spans against the last timestamp reported 68% on a complete run.

    Segment times are not monotonic -- segment 0 here ends after segment 1 starts -- and
    silence before the first segment belongs to no window. Coverage is segment time.
    """
    segments = [
        {"seq": 0, "start_ms": 0, "end_ms": 16160, "text": "kalimat pertama yang panjang"},
        {"seq": 1, "start_ms": 0, "end_ms": 23980, "text": "kalimat kedua yang lebih panjang"},
        {"seq": 2, "start_ms": 16160, "end_ms": 16260, "text": "penutup singkat rapat"},
    ]
    result = generate_minutes(segments, run_prompts=_runner(_extraction(), SUMMARY))
    assert result.stats.chunks_failed == 0
    assert result.stats.covered_ms == result.stats.transcript_ms, (
        "every window parsed, so coverage must be complete"
    )


def test_an_empty_transcript_completes_with_a_reason() -> None:
    result = generate_minutes([], run_prompts=_runner(_extraction(), SUMMARY))
    assert result.draft.items == ()
    assert any("TRANSCRIPT_EMPTY" in w for w in result.draft.warnings)


def test_a_transcript_with_nothing_worth_recording_says_so() -> None:
    result = generate_minutes(SEGMENTS, run_prompts=_runner(_extraction(), SUMMARY))
    assert result.draft.items == ()
    assert any("NO_ITEMS" in w for w in result.draft.warnings)


def test_no_summary_is_written_when_nothing_could_be_verified() -> None:
    result = generate_minutes(
        SEGMENTS,
        run_prompts=_runner(
            _extraction(_item(quote="kalimat yang tidak pernah diucapkan siapa pun")),
            SUMMARY,
        ),
    )
    assert result.draft.summary == ()
    assert any("SUMMARY_SKIPPED" in w for w in result.draft.warnings)


def test_the_extraction_prompt_never_contains_the_roster() -> None:
    """Handing the model a list of names is handing it names to attach to statements."""
    captured: list[str] = []

    def run(specs):
        out = []
        for spec in specs:
            captured.append(spec.system + spec.user)
            out.append({"key": spec.key,
                        "text": SUMMARY if spec.key == "summary" else _extraction(_item())})
        return out

    generate_minutes(
        SEGMENTS, run_prompts=run, roster={"hendra kusuma": "Hendra Kusuma"}
    )
    assert captured
    assert not any("Hendra" in prompt for prompt in captured)


def test_every_extraction_prompt_carries_the_grammar() -> None:
    seen: list[object] = []

    def run(specs):
        out = []
        for spec in specs:
            seen.append(spec.grammar)
            out.append({"key": spec.key,
                        "text": SUMMARY if spec.key == "summary" else _extraction(_item())})
        return out

    generate_minutes(SEGMENTS, run_prompts=run)
    assert seen and all(grammar for grammar in seen), (
        "an unconstrained prompt would need a repair step, which is where an invented "
        "field gets in"
    )


# ===========================================================================
# Persistence
# ===========================================================================


@pytest.fixture
def transcript_id(conn: sqlite3.Connection, meeting_id: int) -> int:
    conn.execute(
        "INSERT INTO recordings (id, meeting_id, recording_uuid, relative_dir, status) "
        "VALUES (1, ?, '11111111-1111-4111-8111-111111111111', 'rec/1', 'RECORDED')",
        (meeting_id,),
    )
    conn.execute(
        "INSERT INTO audio_working_copies (id, recording_id, relative_path, sha256, "
        "size_bytes, frames, duration_ms) VALUES (1, 1, 'w.wav', ?, 10, 10, 32000)",
        ("a" * 64,),
    )
    conn.execute(
        "INSERT INTO transcripts (id, recording_id, working_copy_id, revision, status, "
        "is_active, language) VALUES (1, 1, 1, 1, 'COMPLETE', 1, 'id')"
    )
    conn.commit()
    return 1


def test_a_second_run_writes_a_new_revision_and_deactivates_the_first(
    conn: sqlite3.Connection, transcript_id: int, meeting_id: int
) -> None:
    first = store.create_minute(
        conn, transcript_id=transcript_id, meeting_id=meeting_id, job_id=None
    )
    store.activate_minute(conn, minute_id=first)
    second = store.create_minute(
        conn, transcript_id=transcript_id, meeting_id=meeting_id, job_id=None
    )
    store.activate_minute(conn, minute_id=second)
    conn.commit()

    rows = {int(r["id"]): dict(r) for r in conn.execute("SELECT * FROM minutes")}
    assert rows[first]["revision"] == 1 and rows[first]["is_active"] == 0
    assert rows[second]["revision"] == 2 and rows[second]["is_active"] == 1


def test_two_active_minutes_are_impossible(
    conn: sqlite3.Connection, transcript_id: int, meeting_id: int
) -> None:
    """A database guarantee, not a convention: two concurrent runs must not both be current."""
    first = store.create_minute(
        conn, transcript_id=transcript_id, meeting_id=meeting_id, job_id=None
    )
    store.activate_minute(conn, minute_id=first)
    second = store.create_minute(
        conn, transcript_id=transcript_id, meeting_id=meeting_id, job_id=None
    )
    conn.execute("UPDATE minutes SET status = 'DRAFT' WHERE id = ?", (second,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE minutes SET is_active = 1 WHERE id = ?", (second,))


def test_a_building_minute_cannot_be_marked_current(
    conn: sqlite3.Connection, transcript_id: int, meeting_id: int
) -> None:
    """A run that crashed halfway must not be left behind as the meeting's minute."""
    minute_id = store.create_minute(
        conn, transcript_id=transcript_id, meeting_id=meeting_id, job_id=None
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE minutes SET is_active = 1 WHERE id = ?", (minute_id,))


def test_a_verified_item_must_carry_a_citation(
    conn: sqlite3.Connection, transcript_id: int, meeting_id: int
) -> None:
    """Otherwise a bug that lost the citations leaves rows that look checked."""
    minute_id = store.create_minute(
        conn, transcript_id=transcript_id, meeting_id=meeting_id, job_id=None
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO minute_items (minute_id, seq, kind, text, quote, segment_seqs, "
            "verification) VALUES (?, 0, 'DECISION', 't', 'q', '[]', 'VERIFIED')",
            (minute_id,),
        )


def test_saving_items_writes_the_header_counts_from_what_was_inserted(
    conn: sqlite3.Connection, transcript_id: int, meeting_id: int
) -> None:
    minute_id = store.create_minute(
        conn, transcript_id=transcript_id, meeting_id=meeting_id, job_id=None
    )
    count, verified = store.save_items(
        conn,
        minute_id=minute_id,
        items=[
            {"kind": "DECISION", "text": "a", "quote": "b", "segment_ids": [1],
             "verification": "VERIFIED", "verification_notes": []},
            {"kind": "ACTION", "text": "c", "quote": "d", "segment_ids": [],
             "verification": "UNVERIFIED",
             "verification_notes": ["OWNER_NOT_IN_TRANSCRIPT"]},
        ],
    )
    conn.commit()
    assert (count, verified) == (2, 1)
    row = store.get_minute(conn, minute_id=minute_id)
    assert row["item_count"] == 2
    assert row["verified_count"] == 1
    assert row["unverified_count"] == 1
    assert row["owners_dropped"] == 1


def test_saving_items_twice_replaces_rather_than_accumulates(
    conn: sqlite3.Connection, transcript_id: int, meeting_id: int
) -> None:
    minute_id = store.create_minute(
        conn, transcript_id=transcript_id, meeting_id=meeting_id, job_id=None
    )
    payload = [{"kind": "DECISION", "text": "a", "quote": "b", "segment_ids": [1],
                "verification": "VERIFIED", "verification_notes": []}]
    store.save_items(conn, minute_id=minute_id, items=payload)
    store.save_items(conn, minute_id=minute_id, items=payload)
    conn.commit()
    assert len(store.load_items(conn, minute_id=minute_id)) == 1


def test_an_unknown_column_name_raises_instead_of_being_ignored(
    conn: sqlite3.Connection, transcript_id: int, meeting_id: int
) -> None:
    """A typo that silently did nothing is how recorded model provenance becomes NULL."""
    minute_id = store.create_minute(
        conn, transcript_id=transcript_id, meeting_id=meeting_id, job_id=None
    )
    with pytest.raises(store.MinuteStoreError):
        store.update_minute(conn, minute_id, modelname="oops")


def test_an_audit_event_never_carries_minute_text(
    conn: sqlite3.Connection, transcript_id: int, meeting_id: int
) -> None:
    """Audit rows are read in support contexts where a meeting's words do not belong."""
    minute_id = store.create_minute(
        conn, transcript_id=transcript_id, meeting_id=meeting_id, job_id=None
    )
    secret = "rahasia perusahaan yang tidak boleh bocor"
    store.save_items(
        conn,
        minute_id=minute_id,
        items=[{"kind": "DECISION", "text": secret, "quote": secret,
                "segment_ids": [1], "verification": "VERIFIED",
                "verification_notes": []}],
    )
    store.activate_minute(conn, minute_id=minute_id)
    conn.commit()
    blob = " ".join(
        str(row[0]) for row in conn.execute("SELECT detail_json FROM audit_events")
    )
    assert "rahasia" not in blob


# ===========================================================================
# Rendering
# ===========================================================================

MINUTE = {
    "title": "Rapat Koordinasi SIMRS",
    "revision": 1,
    "transcript_ms": 32000,
    "covered_ms": 32000,
    "owners_dropped": 1,
    "model_name": "qwen3-4b-instruct",
    "quantisation": "Q4_K_M",
    "summary": ["Go-live ditunda ke 5 September."],
    "summary_unsupported_numbers": [],
    "warnings": [],
}
ITEMS = [
    {"kind": "DECISION", "text": "Go-live ditunda.", "quote": "menunda go-live",
     "start_ms": 16000, "end_ms": 24000, "verification": "VERIFIED",
     "verification_notes": [], "segment_seqs": [2], "owner": None, "due_text": None},
    {"kind": "ACTION", "text": "Siapkan dokumen.", "quote": "siapkan dokumen",
     "start_ms": 24000, "end_ms": 32000, "verification": "VERIFIED",
     "verification_notes": [], "segment_seqs": [3], "owner": "Sinta", "due_text": "Jumat"},
    {"kind": "ISSUE", "text": "Anggaran belum jelas.", "quote": "anggaran belum",
     "start_ms": None, "end_ms": None, "verification": "UNVERIFIED",
     "verification_notes": ["QUOTE_NOT_FOUND"], "segment_seqs": [], "owner": None,
     "due_text": None},
]


def _rendered() -> dict[str, str]:
    document = build_document(minute=MINUTE, items=ITEMS)
    blob = render_docx(document)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        docx_text = archive.read("word/document.xml").decode("utf-8")
    return {
        "markdown": render_markdown(document),
        "html": render_html(document),
        "txt": render_text(document),
        "docx": docx_text,
    }


@pytest.mark.parametrize("fmt", ["markdown", "html", "txt", "docx"])
def test_every_format_carries_the_draft_banner(fmt: str) -> None:
    """A document that does not say a machine wrote it will be read as if a person did."""
    assert "DRAF OTOMATIS" in _rendered()[fmt]


@pytest.mark.parametrize("fmt", ["markdown", "html", "txt", "docx"])
def test_every_format_marks_an_unverified_item(fmt: str) -> None:
    assert UNVERIFIED_MARK in _rendered()[fmt]


@pytest.mark.parametrize("fmt", ["markdown", "html", "txt", "docx"])
def test_every_format_says_no_speaker_was_identified(fmt: str) -> None:
    assert "tidak melakukan pengenalan suara" in _rendered()[fmt]


def test_the_action_table_says_what_an_empty_pic_means() -> None:
    text = _rendered()["txt"]
    assert "tidak disebutkan" in text
    assert "bukan berarti belum ditentukan" in text


def test_hiding_unverified_items_still_reports_how_many_were_hidden() -> None:
    document = build_document(minute=MINUTE, items=ITEMS, include_unverified=False)
    rendered = render_text(document)
    assert "Anggaran belum jelas" not in rendered
    assert "disembunyikan dari dokumen ini" in rendered
    assert document.has_unverified is False


def test_a_partial_run_produces_a_visible_coverage_warning() -> None:
    minute = {**MINUTE, "covered_ms": 16000}
    rendered = render_text(build_document(minute=minute, items=ITEMS))
    assert "50%" in rendered


def test_the_html_fetches_nothing() -> None:
    """A remote asset would be a network call from a document an offline system produced."""
    html = _rendered()["html"]
    for marker in ("http://", "https://", "//cdn", "<script", "@import"):
        assert marker not in html, f"{marker!r} in the exported HTML"


def test_the_docx_is_a_valid_package_and_byte_identical_between_runs() -> None:
    document = build_document(minute=MINUTE, items=ITEMS)
    first, second = render_docx(document), render_docx(document)
    assert first == second, "a changing timestamp would make the recorded SHA-256 useless"
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml",
                "word/styles.xml"} <= names
        from xml.dom.minidom import parseString

        for name in names:
            parseString(archive.read(name))


def test_the_docx_paragraph_properties_are_in_schema_order() -> None:
    """Word rejects an out-of-order w:pPr with an unreadable-content prompt and no reason."""
    import re

    sequence = [
        "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl",
        "numPr", "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens",
        "kinsoku", "wordWrap", "overflowPunct", "topLinePunct", "autoSpaceDE",
        "autoSpaceDN", "bidi", "adjustRightInd", "snapToGrid", "spacing", "ind",
        "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc", "textDirection",
        "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr",
        "sectPr", "pPrChange",
    ]
    blob = render_docx(build_document(minute=MINUTE, items=ITEMS))
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for name in archive.namelist():
            xml = archive.read(name).decode("utf-8")
            for body in re.findall(r"<w:pPr>(.*?)</w:pPr>", xml, re.S):
                tags = [t for t in re.findall(r"<w:(\w+)", body) if t in sequence]
                positions = [sequence.index(tag) for tag in tags]
                assert positions == sorted(positions), f"{name}: {tags}"


def test_a_minute_with_no_items_still_renders_a_document() -> None:
    rendered = render_text(build_document(minute={**MINUTE, "summary": []}, items=[]))
    assert "DRAF OTOMATIS" in rendered
    assert "Tidak ada poin" in rendered
