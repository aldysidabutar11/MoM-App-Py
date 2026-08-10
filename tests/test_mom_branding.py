"""Letterhead, filing reference and signature block.

Presentation, but with three things that are not presentation and must survive it: the
draft banner, the per-item verification marks, and the fact that the application signs
nothing. A letterhead makes a document look official, which is precisely why those three
have to be checked *after* the branding is applied rather than before.
"""

from __future__ import annotations

import io
import sqlite3
import struct
import zipfile
import zlib
from pathlib import Path
from xml.dom.minidom import parseString

import pytest

from mom_igd.mom import store
from mom_igd.mom.document import (
    DRAFT_BANNER,
    UNVERIFIED_MARK,
    Letterhead,
    Signatures,
    build_document,
    render_html,
    render_markdown,
    render_text,
)
from mom_igd.mom.docx import _image_size, render_docx
from mom_igd.mom.pipeline import resolve_branding


def png(width: int = 120, height: int = 40) -> bytes:
    """A real PNG, built by hand so the suite needs no image library."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes([1, 2, 3] * width) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xc0\x00\x11\x08\x00\x64\x00\xc8\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    b"\xff\xd9"
)

MINUTE = {
    "title": "Koordinasi SIMRS",
    "revision": 1,
    "document_number": "NOT/2026/08/007",
    "transcript_ms": 60_000,
    "covered_ms": 60_000,
    "summary": ["Go-live ditunda."],
    "summary_unsupported_numbers": [],
    "warnings": [],
    "created_at": "2026-08-10T09:00:00Z",
}
ITEMS = [
    {
        "kind": "DECISION",
        "text": "Go-live ditunda.",
        "quote": "kita tunda go-live",
        "start_ms": 1000,
        "end_ms": 5000,
        "owner": None,
        "due_text": None,
        "verification": "VERIFIED",
        "verification_notes": [],
        "segment_seqs": [1],
    },
    {
        "kind": "ISSUE",
        "text": "Anggaran belum jelas.",
        "quote": "anggaran belum",
        "start_ms": None,
        "end_ms": None,
        "owner": None,
        "due_text": None,
        "verification": "UNVERIFIED",
        "verification_notes": ["QUOTE_NOT_FOUND"],
        "segment_seqs": [],
    },
]
BRANDING = {
    "organisation": "PT INTRAMEDIKA",
    "subtitle": "Instalasi Gawat Darurat",
    "logo": png(),
    "logo_media_type": "image/png",
    "show_signature_block": True,
    "signature_roles": ("Notulis", "Pemimpin Rapat", "Mengetahui"),
    "footer_note": "Dokumen internal",
    "place": "Jakarta",
}


def _document(**overrides):
    branding = {**BRANDING, **overrides.pop("branding", {})}
    return build_document(
        minute={**MINUTE, **overrides.pop("minute", {})},
        items=overrides.pop("items", ITEMS),
        meeting={"title": "Rapat Mingguan", "scheduled_at": "2026-08-10 09:00 WIB"},
        branding=branding,
        **overrides,
    )


def _rendered(document) -> dict[str, str]:
    blob = render_docx(document)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        docx = "".join(
            archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.endswith(".xml")
        )
    return {
        "markdown": render_markdown(document),
        "html": render_html(document),
        "txt": render_text(document),
        "docx": docx,
    }


# ===========================================================================
# The letterhead must not displace what protects the reader
# ===========================================================================


@pytest.mark.parametrize("fmt", ["markdown", "html", "txt", "docx"])
def test_a_branded_document_still_carries_the_draft_banner(fmt: str) -> None:
    """The whole risk of a letterhead: it makes a machine draft look like a filed record."""
    assert "DRAF OTOMATIS" in _rendered(_document())[fmt]


@pytest.mark.parametrize("fmt", ["markdown", "html", "txt", "docx"])
def test_a_branded_document_still_marks_unverified_items(fmt: str) -> None:
    assert UNVERIFIED_MARK in _rendered(_document())[fmt]


def test_the_draft_banner_comes_after_the_letterhead_not_before_it() -> None:
    """The correction has to reach the reader before the content does."""
    blocks = _document().blocks
    kinds = [type(block).__name__ for block in blocks]
    assert kinds[0] == "Letterhead"
    banner = next(
        index
        for index, block in enumerate(blocks)
        if getattr(block, "text", "") == DRAFT_BANNER
    )
    assert banner < 4, "the banner must be near the top, not below the metadata"


@pytest.mark.parametrize("fmt", ["markdown", "html", "txt", "docx"])
def test_the_organisation_name_appears_in_every_format(fmt: str) -> None:
    assert "PT INTRAMEDIKA" in _rendered(_document())[fmt]


def test_no_letterhead_is_drawn_when_no_organisation_is_configured() -> None:
    document = _document(branding={"organisation": ""})
    assert not any(isinstance(block, Letterhead) for block in document.blocks)
    assert "DRAF OTOMATIS" in render_text(document)


# ===========================================================================
# Filing reference
# ===========================================================================


@pytest.mark.parametrize("fmt", ["markdown", "html", "txt", "docx"])
def test_the_filing_reference_appears_in_every_format(fmt: str) -> None:
    assert "NOT/2026/08/007" in _rendered(_document())[fmt]


def test_the_reference_is_in_the_word_running_header() -> None:
    """Page four, held on its own, must still say which document it is."""
    blob = render_docx(_document())
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        header = archive.read("word/header1.xml").decode("utf-8")
    assert "NOT/2026/08/007" in header


def test_word_gets_live_page_numbers_rather_than_a_frozen_count() -> None:
    blob = render_docx(_document())
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        footer = archive.read("word/footer1.xml").decode("utf-8")
    assert "PAGE" in footer and "NUMPAGES" in footer


def test_a_minute_without_a_reference_renders_without_one() -> None:
    document = _document(minute={"document_number": None})
    rendered = render_text(document)
    assert "Nomor notulen" not in rendered
    assert "DRAF OTOMATIS" in rendered


# ===========================================================================
# Signatures
# ===========================================================================


def test_the_signature_block_says_the_application_approved_nothing() -> None:
    rendered = render_text(_document())
    assert "tidak menyetujui" in rendered
    assert "tetap berstatus draf" in rendered


def test_no_name_is_ever_printed_on_a_signature_line() -> None:
    """A signature block is a place to take responsibility, not a record that anybody has."""
    document = _document()
    [block] = [b for b in document.blocks if isinstance(b, Signatures)]
    assert block.roles == ("Notulis", "Pemimpin Rapat", "Mengetahui")
    rendered = render_text(document)
    for name in ("Andi", "Sinta", "Rendra"):
        assert name not in rendered.split("Pengesahan")[1]


def test_the_place_and_date_come_from_the_meeting_not_the_clock() -> None:
    assert "Jakarta, 10 Agustus 2026" in render_text(_document())


def test_an_unparseable_meeting_date_leaves_a_blank_to_fill_in() -> None:
    document = build_document(
        minute={**MINUTE, "created_at": ""},
        items=ITEMS,
        meeting={"title": "R", "scheduled_at": "kapan-kapan"},
        branding=BRANDING,
    )
    rendered = render_text(document)
    assert "Jakarta, ______________" in rendered, "no date beats a wrong date"


def test_the_signature_block_can_be_switched_off() -> None:
    document = _document(branding={"show_signature_block": False})
    assert not any(isinstance(block, Signatures) for block in document.blocks)


def test_the_docx_signature_table_has_one_column_per_role() -> None:
    blob = render_docx(_document())
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        body = archive.read("word/document.xml").decode("utf-8")
    assert body.count("(..........................)") == 3


# ===========================================================================
# The logo
# ===========================================================================


def test_a_png_is_measured_from_its_own_bytes() -> None:
    assert _image_size(png(180, 60)) == (180, 60)


def test_a_jpeg_is_measured_from_its_own_bytes() -> None:
    assert _image_size(JPEG) == (200, 100)


def test_something_that_is_not_an_image_measures_as_nothing() -> None:
    assert _image_size(b"GIF89a not really") is None
    assert _image_size(b"") is None


def test_the_logo_becomes_a_real_media_part_in_the_docx() -> None:
    blob = render_docx(_document())
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = archive.namelist()
        assert "word/media/logo.png" in names
        assert archive.read("word/media/logo.png") == BRANDING["logo"]
        rels = archive.read("word/_rels/document.xml.rels").decode("utf-8")
        assert "media/logo.png" in rels
        assert "<w:drawing>" in archive.read("word/document.xml").decode("utf-8")


def test_the_logo_is_inlined_in_the_html_never_linked() -> None:
    """A linked file would be a filesystem dependency in a document meant to stand alone."""
    html = render_html(_document())
    assert "data:image/png;base64," in html
    assert 'src="http' not in html
    assert 'src="file' not in html


def test_a_document_with_no_logo_still_renders_its_letterhead() -> None:
    document = _document(branding={"logo": None, "logo_media_type": ""})
    assert "PT INTRAMEDIKA" in render_text(document)
    blob = render_docx(document)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert not any(name.startswith("word/media/") for name in archive.namelist())
        for name in archive.namelist():
            parseString(archive.read(name))


def test_a_branded_docx_is_still_byte_deterministic() -> None:
    document = _document()
    assert render_docx(document) == render_docx(document)


def test_every_part_of_a_branded_docx_is_well_formed() -> None:
    with zipfile.ZipFile(io.BytesIO(render_docx(_document()))) as archive:
        assert archive.testzip() is None
        for name in archive.namelist():
            if name.endswith(".xml") or name.endswith(".rels"):
                parseString(archive.read(name))


# ===========================================================================
# Resolving branding: the one place a file is read
# ===========================================================================


def test_a_configured_logo_is_read_from_the_branding_directory(config, paths) -> None:
    paths.ensure()
    (paths.branding_dir / "kop.png").write_bytes(png())
    branded = config.model_copy(
        update={
            "mom": config.mom.model_copy(
                update={
                    "document": config.mom.document.model_copy(
                        update={"organisation": "PT X", "logo_filename": "kop.png"}
                    )
                }
            )
        }
    )
    resolved = resolve_branding(branded, paths)
    assert resolved["logo"] == png()
    assert resolved["logo_media_type"] == "image/png"


@pytest.mark.parametrize(
    "content,reason",
    [
        (b"not an image at all", "unrecognised format"),
        (b"", "empty file"),
    ],
)
def test_an_unusable_logo_is_ignored_rather_than_failing_the_export(
    config, paths, content, reason
) -> None:
    """An export must not fail because somebody put the wrong file in a folder."""
    paths.ensure()
    (paths.branding_dir / "kop.png").write_bytes(content)
    branded = config.model_copy(
        update={
            "mom": config.mom.model_copy(
                update={
                    "document": config.mom.document.model_copy(
                        update={"organisation": "PT X", "logo_filename": "kop.png"}
                    )
                }
            )
        }
    )
    resolved = resolve_branding(branded, paths)
    assert "logo" not in resolved, reason
    assert resolved["organisation"] == "PT X", "the text letterhead still stands"


def test_a_missing_logo_file_is_ignored(config, paths) -> None:
    paths.ensure()
    branded = config.model_copy(
        update={
            "mom": config.mom.model_copy(
                update={
                    "document": config.mom.document.model_copy(
                        update={"organisation": "PT X", "logo_filename": "tidak-ada.png"}
                    )
                }
            )
        }
    )
    assert "logo" not in resolve_branding(branded, paths)


def test_an_oversized_logo_is_refused(config, paths) -> None:
    paths.ensure()
    (paths.branding_dir / "kop.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (3 * 1024 * 1024))
    branded = config.model_copy(
        update={
            "mom": config.mom.model_copy(
                update={
                    "document": config.mom.document.model_copy(
                        update={"organisation": "PT X", "logo_filename": "kop.png"}
                    )
                }
            )
        }
    )
    assert "logo" not in resolve_branding(branded, paths)


def test_a_logo_filename_cannot_escape_the_branding_directory(config) -> None:
    """A configured string is still input from outside the code."""
    for attempt in ("../keys/master.bin", "C:/Windows/win.ini", "sub/dir.png"):
        with pytest.raises(ValueError):
            config.mom.document.model_copy(update={"logo_filename": attempt})
            type(config.mom.document)(logo_filename=attempt)


def test_a_broken_number_format_is_rejected_by_configuration(config) -> None:
    with pytest.raises(ValueError):
        type(config.mom.document)(document_number_format="NOT/{tahun}")


# ===========================================================================
# The reference is assigned once and inherited
# ===========================================================================


@pytest.fixture
def transcript(conn: sqlite3.Connection, meeting_id: int) -> int:
    conn.execute(
        "INSERT INTO recordings (id, meeting_id, recording_uuid, relative_dir, status) "
        "VALUES (1, ?, '11111111-1111-4111-8111-111111111111', 'rec/1', 'RECORDED')",
        (meeting_id,),
    )
    conn.execute(
        "INSERT INTO audio_working_copies (id, recording_id, relative_path, sha256, "
        "size_bytes, frames, duration_ms) VALUES (1, 1, 'w.wav', ?, 10, 10, 1000)",
        ("a" * 64,),
    )
    conn.execute(
        "INSERT INTO transcripts (id, recording_id, working_copy_id, revision, status, "
        "is_active, language) VALUES (1, 1, 1, 1, 'COMPLETE', 1, 'id')"
    )
    conn.commit()
    return 1


def _mint(conn, minute_id, transcript_id, stamp="2026-08-10"):
    return store.assign_document_number(
        conn,
        minute_id=minute_id,
        transcript_id=transcript_id,
        number_format="NOT/{year}/{month}/{seq:03d}",
        stamp=stamp,
    )


def test_a_reference_is_minted_from_the_month_and_a_sequence(
    conn, transcript, meeting_id
) -> None:
    minute_id = store.create_minute(
        conn, transcript_id=transcript, meeting_id=meeting_id, job_id=None
    )
    assert _mint(conn, minute_id, transcript) == "NOT/2026/08/001"


def test_a_second_call_returns_the_same_reference(conn, transcript, meeting_id) -> None:
    """Nothing recomputes an issued reference. It is on paper somewhere."""
    minute_id = store.create_minute(
        conn, transcript_id=transcript, meeting_id=meeting_id, job_id=None
    )
    first = _mint(conn, minute_id, transcript)
    assert _mint(conn, minute_id, transcript, stamp="2026-12-31") == first


def test_a_later_revision_inherits_rather_than_renumbering(
    conn, transcript, meeting_id
) -> None:
    """One reference per meeting, with revisions under it -- not a new filing number."""
    first = store.create_minute(
        conn, transcript_id=transcript, meeting_id=meeting_id, job_id=None
    )
    reference = _mint(conn, first, transcript)
    store.activate_minute(conn, minute_id=first)

    second = store.create_minute(
        conn, transcript_id=transcript, meeting_id=meeting_id, job_id=None
    )
    assert _mint(conn, second, transcript) == reference


def test_a_different_meeting_gets_the_next_number(conn, transcript, meeting_id) -> None:
    first = store.create_minute(
        conn, transcript_id=transcript, meeting_id=meeting_id, job_id=None
    )
    assert _mint(conn, first, transcript) == "NOT/2026/08/001"

    conn.execute(
        "INSERT INTO recordings (id, meeting_id, recording_uuid, relative_dir, status) "
        "VALUES (2, ?, '22222222-2222-4222-8222-222222222222', 'rec/2', 'RECORDED')",
        (meeting_id,),
    )
    conn.execute(
        "INSERT INTO audio_working_copies (id, recording_id, relative_path, sha256, "
        "size_bytes, frames, duration_ms) VALUES (2, 2, 'w2.wav', ?, 10, 10, 1000)",
        ("b" * 64,),
    )
    conn.execute(
        "INSERT INTO transcripts (id, recording_id, working_copy_id, revision, status, "
        "is_active, language) VALUES (2, 2, 2, 1, 'COMPLETE', 1, 'id')"
    )
    other = store.create_minute(
        conn, transcript_id=2, meeting_id=meeting_id, job_id=None
    )
    assert _mint(conn, other, 2) == "NOT/2026/08/002"


def test_an_empty_format_turns_numbering_off(conn, transcript, meeting_id) -> None:
    minute_id = store.create_minute(
        conn, transcript_id=transcript, meeting_id=meeting_id, job_id=None
    )
    assert (
        store.assign_document_number(
            conn,
            minute_id=minute_id,
            transcript_id=transcript,
            number_format="",
            stamp="2026-08-10",
        )
        is None
    )


def test_two_current_minutes_cannot_share_a_reference(conn, transcript, meeting_id) -> None:
    """A database guarantee, because a reference that is not unique is not a reference."""
    first = store.create_minute(
        conn, transcript_id=transcript, meeting_id=meeting_id, job_id=None
    )
    _mint(conn, first, transcript)
    store.activate_minute(conn, minute_id=first)

    conn.execute(
        "INSERT INTO recordings (id, meeting_id, recording_uuid, relative_dir, status) "
        "VALUES (3, ?, '33333333-3333-4333-8333-333333333333', 'rec/3', 'RECORDED')",
        (meeting_id,),
    )
    conn.execute(
        "INSERT INTO audio_working_copies (id, recording_id, relative_path, sha256, "
        "size_bytes, frames, duration_ms) VALUES (3, 3, 'w3.wav', ?, 10, 10, 1000)",
        ("c" * 64,),
    )
    conn.execute(
        "INSERT INTO transcripts (id, recording_id, working_copy_id, revision, status, "
        "is_active, language) VALUES (3, 3, 3, 1, 'COMPLETE', 1, 'id')"
    )
    other = store.create_minute(conn, transcript_id=3, meeting_id=meeting_id, job_id=None)
    conn.execute(
        "UPDATE minutes SET document_number = 'NOT/2026/08/001', status = 'DRAFT' "
        "WHERE id = ?",
        (other,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE minutes SET is_active = 1 WHERE id = ?", (other,))
