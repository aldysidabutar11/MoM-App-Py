"""One document model, four renderers.

Markdown, HTML, DOCX and plain text are built from the **same** block list. Writing four
renderers against four hand-assembled layouts is how three of them end up missing the
draft banner, or showing an unverified item without its mark -- and the one that drifts is
always the format nobody on the team opened during testing but everybody forwards
afterwards.

So the layout is decided once, in :func:`build_document`, and a renderer's only job is to
put blocks on a page.

Three things every rendering must carry, enforced here rather than trusted to each writer:

* **The draft banner.** These minutes were written by a language model and reviewed by
  nobody. A document that does not say so will be read as if a person wrote it.
* **The verification mark** on any item whose quote could not be located in the recording.
* **The coverage line**, when part of the meeting produced no items. A minute covering
  seventy of ninety minutes is not a minute of that meeting, and the reader must not have
  to infer that from a gap in the timestamps.

**Nothing is fetched.** The HTML has no external stylesheet, script, font or image -- a
remote asset would be a network call from a document produced by an offline system, and it
would break the moment the file left the machine. DOCX is assembled from stdlib
``zipfile`` and hand-written OOXML: a Word document is a zip of XML, and writing it
directly costs about two hundred lines and avoids adding a dependency to a project that
scrutinises every one.
"""

from __future__ import annotations

import base64
import html as _html
import re
from dataclasses import dataclass, field
from typing import Any, Final, Iterable, Mapping, Sequence

from mom_igd.mom.schema import ITEM_KINDS, kind_label

__all__ = [
    "Block",
    "Bullets",
    "Callout",
    "Heading",
    "Letterhead",
    "Signatures",
    "MinuteDocument",
    "Paragraph",
    "Table",
    "build_document",
    "render_html",
    "render_markdown",
    "render_text",
]

#: Shown at the top of every rendering, in every format, always.
DRAFT_BANNER: Final[str] = (
    "DRAF OTOMATIS — notulen ini disusun oleh model bahasa lokal dari transkrip "
    "rekaman, dan BELUM diperiksa manusia. Setiap poin mencantumkan kutipan dan waktu "
    "asalnya; periksa poin yang ditandai sebelum notulen ini dipakai atau dibagikan."
)

#: Appended to an item whose quote could not be found in the recording.
UNVERIFIED_MARK: Final[str] = "BELUM TERVERIFIKASI"

#: Appended to an item whose quote was found, but not where the model said it was.
REBOUND_MARK: Final[str] = "kutipan ditemukan di segmen lain"

#: Notes worth showing a reader. The rest are diagnostics that belong in the database,
#: not in a document somebody has to read -- a minute covered in machine codes teaches
#: the reader to skip the marks that matter.
_READER_NOTES: Final[Mapping[str, str]] = {
    "OWNER_NOT_IN_TRANSCRIPT": "nama PIC yang diusulkan model tidak terdengar di rekaman, jadi dihapus",
    "OWNER_NOT_A_NAME": "PIC yang diusulkan model bukan nama orang, jadi dihapus",
    "DUE_NOT_IN_TRANSCRIPT": "tenggat yang diusulkan model tidak terdengar di rekaman, jadi dihapus",
    "QUOTE_NEAR_MATCH": "kutipan tidak persis sama dengan transkrip",
    "CITATION_OUT_OF_RANGE": "model merujuk segmen di luar bagian yang dibacanya",
}

#: Indonesian month names, for the date above a signature. A four-line lookup rather
#: than a locale: ``locale.setlocale`` is process-global, is not thread-safe, and depends
#: on the operating system having an Indonesian locale installed -- three ways for an
#: export to depend on something outside this application.
_MONTHS_ID: Final[tuple[str, ...]] = (
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
)

_CONFLICT = re.compile(r"^(OWNER|DUE)_CONFLICT:(.*)\|(.*)$")
_SUPERSEDED = re.compile(r"^POSSIBLY_SUPERSEDED:(.*)$")


# ===========================================================================
# Blocks
# ===========================================================================


@dataclass(frozen=True, slots=True)
class Heading:
    level: int
    text: str


@dataclass(frozen=True, slots=True)
class Paragraph:
    text: str
    #: ``normal`` | ``muted`` -- muted is evidence and metadata, set smaller and grey.
    style: str = "normal"


@dataclass(frozen=True, slots=True)
class Bullets:
    items: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Callout:
    """A boxed notice. Used for the draft banner and for coverage warnings."""

    text: str
    #: ``warning`` | ``info``
    tone: str = "warning"


@dataclass(frozen=True, slots=True)
class Table:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class Letterhead:
    """The organisation's own heading, above everything the model produced.

    ``logo`` is raw image bytes read from the branding directory, or ``None``. It is
    carried as bytes rather than as a path because a renderer must never open a file: the
    DOCX writer embeds it, the HTML writer inlines it as a data URI, and the text writers
    ignore it. Passing a path would put filesystem access into four places instead of one.
    """

    organisation: str
    subtitle: str = ""
    logo: bytes | None = None
    logo_media_type: str = ""


@dataclass(frozen=True, slots=True)
class Signatures:
    """Blank columns for people to sign by hand.

    Deliberately blank. The application approves nothing -- that is Phase 9 and has its
    own audit requirements -- so what this offers is a place for a person to take
    responsibility, not a record that anybody has.
    """

    roles: tuple[str, ...]
    place_and_date: str = ""


Block = Heading | Paragraph | Bullets | Callout | Table | Letterhead | Signatures


@dataclass(slots=True)
class MinuteDocument:
    title: str
    blocks: list[Block] = field(default_factory=list)
    #: True when at least one item in the document could not be verified. Recorded on the
    #: export row, so "did this file contain unverified items?" is answerable later.
    has_unverified: bool = False
    #: Left-hand side of the page footer, typically the filing reference.
    footer_left: str = ""
    #: Right-hand side. Page numbering is appended by renderers that have pages.
    footer_right: str = ""
    #: Filing reference, also shown in the metadata list. Empty when numbering is off.
    document_number: str = ""

    # There is deliberately no title-derived filename stem here. An export is named by the
    # meeting's UUID (ADR-0009): a display name is never a path component, two meetings
    # called "Rapat Mingguan" must not collide, and a helper that turns a title into a
    # filename is an invitation to do exactly that.


# ===========================================================================
# Building
# ===========================================================================


def _timestamp(milliseconds: Any) -> str:
    if milliseconds is None:
        return "--:--:--"
    total = max(0, int(milliseconds)) // 1000
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _duration(milliseconds: Any) -> str:
    total = max(0, int(milliseconds or 0)) // 1000
    hours, minutes = divmod(total // 60, 60)
    return f"{hours} jam {minutes} menit" if hours else f"{minutes} menit"


def _indonesian_date(value: Any) -> str:
    """Render an ISO-ish timestamp as ``10 Agustus 2026``. Empty when unparseable.

    Deliberately forgiving: the value arrives from a database column that has held
    several shapes over the project's life, and a signature block is not worth failing an
    export over. No date is better than a wrong one.
    """
    text = str(value or "").strip()
    if len(text) < 10:
        return ""
    try:
        year, month, day = int(text[0:4]), int(text[5:7]), int(text[8:10])
    except ValueError:
        return ""
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{day} {_MONTHS_ID[month - 1]} {year}"


def _item_notes(notes: Iterable[str]) -> list[str]:
    """Turn stored note codes into sentences a reader can act on."""
    out: list[str] = []
    for note in notes:
        reversed_at = _SUPERSEDED.match(str(note))
        if reversed_at is not None:
            out.append(
                "PERHATIAN: keputusan ini tampaknya dibatalkan atau diubah pada "
                f"{reversed_at.group(1)} -- periksa keputusan yang berlaku"
            )
            continue
        match = _CONFLICT.match(str(note))
        if match is not None:
            field_name = "PIC" if match.group(1) == "OWNER" else "tenggat"
            out.append(
                f"dua bagian rekaman menyebut {field_name} berbeda: "
                f"“{match.group(2)}” dan “{match.group(3)}”"
            )
            continue
        readable = _READER_NOTES.get(str(note))
        if readable:
            out.append(readable)
    return out


def _evidence_line(item: Mapping[str, Any]) -> str:
    start = _timestamp(item.get("start_ms"))
    quote = str(item.get("quote") or "").strip()
    marks: list[str] = []
    state = str(item.get("verification") or "UNVERIFIED")
    if state == "UNVERIFIED":
        marks.append(UNVERIFIED_MARK)
    elif state == "REBOUND":
        marks.append(REBOUND_MARK)
    marks.extend(_item_notes(item.get("verification_notes") or ()))
    suffix = f" — {'; '.join(marks)}" if marks else ""
    return f"[{start}] “{quote}”{suffix}"


def build_document(
    *,
    minute: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    meeting: Mapping[str, Any] | None = None,
    recording: Mapping[str, Any] | None = None,
    participants: Sequence[str] = (),
    include_unverified: bool = True,
    include_evidence: bool = True,
    branding: Mapping[str, Any] | None = None,
) -> MinuteDocument:
    """Lay out a minute once, for every format.

    ``include_unverified=False`` removes unverified items from the *document* and says so
    in the coverage note. It never removes them from the database, and the document still
    reports how many were left out -- an export that quietly contained less than the minute
    would misrepresent the minute.

    ``branding`` is the resolved ``[mom.document]`` block: ``organisation``, ``subtitle``,
    ``logo`` (bytes or ``None``), ``logo_media_type``, ``signature_roles``,
    ``show_signature_block`` and ``footer_note``. Resolved by the caller, because reading
    a file is not this function's business and a renderer's even less.
    """
    summary = list(minute.get("summary") or [])
    warnings = list(minute.get("warnings") or [])
    unsupported = list(minute.get("summary_unsupported_numbers") or [])

    shown = [
        item
        for item in items
        if include_unverified or str(item.get("verification")) != "UNVERIFIED"
    ]
    hidden = len(items) - len(shown)
    has_unverified = any(
        str(item.get("verification")) == "UNVERIFIED" for item in shown
    )

    title = str(minute.get("title") or "").strip() or "Notulen Rapat"
    brand = dict(branding or {})
    reference = str(minute.get("document_number") or "").strip()

    document = MinuteDocument(
        title=title,
        has_unverified=has_unverified,
        document_number=reference,
        footer_left=reference,
        footer_right=str(brand.get("footer_note") or ""),
    )
    blocks = document.blocks

    # The letterhead sits above the title, and the draft banner immediately below it.
    # That order is deliberate: an official-looking heading is exactly what makes a
    # reader assume a document was written by a person, so the correction has to arrive
    # before they reach the content, not after.
    organisation = str(brand.get("organisation") or "").strip()
    if organisation:
        blocks.append(
            Letterhead(
                organisation=organisation,
                subtitle=str(brand.get("subtitle") or "").strip(),
                logo=brand.get("logo") if isinstance(brand.get("logo"), bytes) else None,
                logo_media_type=str(brand.get("logo_media_type") or ""),
            )
        )

    blocks.append(Heading(1, title))
    blocks.append(Callout(DRAFT_BANNER, tone="warning"))

    # --- metadata ---------------------------------------------------------
    facts: list[str] = []
    if reference:
        facts.append(f"Nomor notulen: {reference}")
    if meeting:
        if meeting.get("title"):
            facts.append(f"Rapat: {meeting['title']}")
        if meeting.get("scheduled_at"):
            facts.append(f"Jadwal: {meeting['scheduled_at']}")
        if meeting.get("location"):
            facts.append(f"Lokasi: {meeting['location']}")
    if recording and recording.get("started_at"):
        facts.append(f"Rekaman dimulai: {recording['started_at']}")
    if minute.get("transcript_ms"):
        # Total speech in the transcript, not wall-clock meeting length: it is the
        # quantity the coverage figure below is a fraction of, and labelling it
        # "durasi rapat" would invite the reader to compare it with the clock.
        facts.append(f"Total durasi bicara di transkrip: {_duration(minute['transcript_ms'])}")
    facts.append(f"Revisi notulen: {minute.get('revision', 1)}")
    if facts:
        blocks.append(Bullets(tuple(facts)))

    if participants:
        blocks.append(Heading(2, "Peserta Terdaftar"))
        blocks.append(Bullets(tuple(participants)))
        # Said plainly, because a roster next to a list of PICs invites the reader to
        # assume the system matched one to the other. It did not.
        blocks.append(
            Paragraph(
                "Daftar ini berasal dari roster rapat, bukan dari pengenalan suara. "
                "Sistem tidak mengetahui siapa yang berbicara pada bagian mana.",
                style="muted",
            )
        )

    # --- summary ----------------------------------------------------------
    if summary:
        blocks.append(Heading(2, "Ringkasan"))
        blocks.append(Bullets(tuple(summary)))
        if unsupported:
            blocks.append(
                Callout(
                    "Ringkasan memuat angka yang tidak ditemukan di poin manapun: "
                    f"{', '.join(unsupported)}. Periksa angka ini terhadap rekaman.",
                    tone="warning",
                )
            )

    # --- the four sections ------------------------------------------------
    for kind in ITEM_KINDS:
        section = [item for item in shown if str(item.get("kind")) == kind]
        if not section:
            continue
        blocks.append(Heading(2, kind_label(kind)))
        if kind == "ACTION":
            rows = tuple(
                (
                    str(number),
                    str(item.get("text") or ""),
                    str(item.get("owner") or "tidak disebutkan"),
                    str(item.get("due_text") or item.get("due") or "tidak disebutkan"),
                    _timestamp(item.get("start_ms")),
                )
                for number, item in enumerate(section, start=1)
            )
            blocks.append(
                Table(
                    headers=("No", "Tindak lanjut", "PIC", "Tenggat", "Waktu"),
                    rows=rows,
                )
            )
            # The PIC column is the one a reader will act on, so the rule that produced
            # it is stated next to it rather than in a footnote.
            blocks.append(
                Paragraph(
                    "PIC dan tenggat hanya diisi bila disebut langsung di rekaman. "
                    "“tidak disebutkan” berarti rapat tidak menyebutkannya — bukan "
                    "berarti belum ditentukan.",
                    style="muted",
                )
            )
            if include_evidence:
                for number, item in enumerate(section, start=1):
                    blocks.append(Paragraph(f"{number}. {_evidence_line(item)}", "muted"))
        else:
            for number, item in enumerate(section, start=1):
                blocks.append(Paragraph(f"{number}. {item.get('text') or ''}"))
                if include_evidence:
                    blocks.append(Paragraph(_evidence_line(item), "muted"))

    if not shown:
        blocks.append(Heading(2, "Isi"))
        blocks.append(
            Paragraph(
                "Tidak ada poin yang dapat dicatat dari transkrip ini."
                if not items
                else "Semua poin yang ditemukan belum terverifikasi dan disembunyikan "
                "dari dokumen ini."
            )
        )

    # --- what the reader has to know about this document ------------------
    blocks.append(Heading(2, "Catatan Pemeriksaan"))
    checks: list[str] = []

    transcript_ms = int(minute.get("transcript_ms") or 0)
    covered_ms = int(minute.get("covered_ms") or 0)
    if transcript_ms and covered_ms < transcript_ms:
        percent = 100.0 * covered_ms / transcript_ms
        blocks.append(
            Callout(
                f"Notulen ini hanya mencakup {percent:.0f}% dari isi transkrip "
                f"({_duration(covered_ms)} dari {_duration(transcript_ms)} durasi "
                "bicara). Sebagian transkrip tidak berhasil diproses -- lihat "
                "peringatan proses di bawah.",
                tone="warning",
            )
        )
    elif transcript_ms:
        checks.append(
            f"Seluruh transkrip diproses ({_duration(transcript_ms)} durasi bicara)."
        )

    verified = sum(1 for item in shown if str(item.get("verification")) == "VERIFIED")
    rebound = sum(1 for item in shown if str(item.get("verification")) == "REBOUND")
    unverified = sum(1 for item in shown if str(item.get("verification")) == "UNVERIFIED")
    checks.append(
        f"{len(shown)} poin: {verified} terverifikasi terhadap rekaman, "
        f"{rebound} terverifikasi setelah kutipan dicari ulang, "
        f"{unverified} tidak dapat diverifikasi."
    )
    if hidden:
        checks.append(
            f"{hidden} poin yang tidak terverifikasi disembunyikan dari dokumen ini "
            "atas permintaan, tetapi tetap tersimpan di basis data."
        )
    if minute.get("owners_dropped"):
        checks.append(
            f"{minute['owners_dropped']} nama PIC yang diusulkan model dihapus karena "
            "tidak terdengar di rekaman."
        )
    checks.append(
        "Sistem ini tidak melakukan pengenalan suara, sehingga tidak ada poin yang "
        "dikaitkan dengan pembicara tertentu."
    )
    if minute.get("model_name"):
        provenance = f"Model: {minute['model_name']}"
        if minute.get("quantisation"):
            provenance += f" ({minute['quantisation']})"
        if minute.get("model_revision"):
            provenance += f", revisi {str(minute['model_revision'])[:12]}"
        checks.append(provenance + ", dijalankan sepenuhnya di komputer ini.")
    blocks.append(Bullets(tuple(checks)))

    if warnings:
        blocks.append(Heading(3, "Peringatan proses"))
        blocks.append(Bullets(tuple(warnings)))

    roles = tuple(
        str(role).strip() for role in (brand.get("signature_roles") or ()) if str(role).strip()
    )
    if brand.get("show_signature_block") and roles:
        blocks.append(Heading(2, "Pengesahan"))
        # Said before the columns, not after. A signature block is the most official-
        # looking thing on the page, and it must not be read as evidence that the
        # application checked anything.
        blocks.append(
            Paragraph(
                "Tanda tangan di bawah diisi manual oleh manusia. Aplikasi ini tidak "
                "menyetujui dan tidak mengesahkan apa pun; sampai kolom ini terisi, "
                "notulen ini tetap berstatus draf.",
                style="muted",
            )
        )
        place = str(brand.get("place") or "").strip()
        when = _indonesian_date(
            (meeting or {}).get("scheduled_at") or minute.get("created_at")
        )
        if brand.get("place_and_date"):
            stamp = str(brand["place_and_date"])
        elif place and when:
            stamp = f"{place}, {when}"
        elif place:
            stamp = f"{place}, ______________"
        else:
            stamp = ""
        blocks.append(Signatures(roles=roles, place_and_date=stamp))

    return document


# ===========================================================================
# Markdown
# ===========================================================================


def render_markdown(document: MinuteDocument) -> str:
    lines: list[str] = []
    for block in document.blocks:
        if isinstance(block, Letterhead):
            # No image: Markdown cannot carry one without an external file, and an
            # external file is the one thing an offline export must not depend on.
            lines.append(f"**{block.organisation}**")
            if block.subtitle:
                lines.append(f"*{block.subtitle}*")
            lines.append("\n---\n")
        elif isinstance(block, Signatures):
            if block.place_and_date:
                lines.append(f"{block.place_and_date}\n")
            lines.append("| " + " | ".join(block.roles) + " |")
            lines.append("|" + "|".join(" --- " for _ in block.roles) + "|")
            lines.append("| " + " | ".join("&nbsp;" for _ in block.roles) + " |")
            lines.append("| " + " | ".join("(............)" for _ in block.roles) + " |")
            lines.append("")
        elif isinstance(block, Heading):
            lines.append(f"\n{'#' * max(1, min(6, block.level))} {block.text}\n")
        elif isinstance(block, Callout):
            prefix = "> **PERHATIAN**" if block.tone == "warning" else "> **Catatan**"
            lines.append(f"{prefix}\n>\n> {block.text}\n")
        elif isinstance(block, Bullets):
            lines.extend(f"- {entry}" for entry in block.items)
            lines.append("")
        elif isinstance(block, Table):
            lines.append("| " + " | ".join(block.headers) + " |")
            lines.append("|" + "|".join(" --- " for _ in block.headers) + "|")
            for row in block.rows:
                lines.append(
                    "| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |"
                )
            lines.append("")
        else:
            lines.append(f"*{block.text}*\n" if block.style == "muted" else f"{block.text}\n")
    footer = " · ".join(part for part in (document.footer_left, document.footer_right) if part)
    if footer:
        lines.append(f"\n---\n\n*{footer}*")
    return "\n".join(lines).strip() + "\n"


# ===========================================================================
# Plain text
# ===========================================================================


def render_text(document: MinuteDocument) -> str:
    lines: list[str] = []
    for block in document.blocks:
        if isinstance(block, Letterhead):
            lines.append(block.organisation.upper())
            if block.subtitle:
                lines.append(block.subtitle)
            lines.append("=" * 70)
        elif isinstance(block, Signatures):
            if block.place_and_date:
                lines.extend(["", block.place_and_date])
            lines.append("")
            width = max(22, 70 // max(1, len(block.roles)))
            lines.append("".join(role.ljust(width) for role in block.roles).rstrip())
            lines.extend(["", "", ""])
            lines.append("".join("(..................)".ljust(width) for _ in block.roles).rstrip())
            lines.append("")
        elif isinstance(block, Heading):
            underline = "=" if block.level == 1 else "-"
            lines.extend(["", block.text, underline * len(block.text), ""])
        elif isinstance(block, Callout):
            lines.extend(["", "!! " + block.text, ""])
        elif isinstance(block, Bullets):
            lines.extend(f"  * {entry}" for entry in block.items)
            lines.append("")
        elif isinstance(block, Table):
            # Column widths from the content, so the table survives being pasted into an
            # email in a proportional font at least as well as it can.
            widths = [
                max(len(block.headers[index]), *(len(row[index]) for row in block.rows))
                if block.rows
                else len(block.headers[index])
                for index in range(len(block.headers))
            ]
            widths = [min(width, 48) for width in widths]

            def line(cells: Sequence[str]) -> str:
                return "  ".join(
                    cell[: widths[index]].ljust(widths[index])
                    for index, cell in enumerate(cells)
                ).rstrip()

            lines.append(line(block.headers))
            lines.append("  ".join("-" * width for width in widths))
            lines.extend(line(row) for row in block.rows)
            lines.append("")
        else:
            lines.append(block.text)
            lines.append("")
    footer = " | ".join(part for part in (document.footer_left, document.footer_right) if part)
    if footer:
        lines.extend(["", "-" * 70, footer])
    return "\n".join(lines).strip() + "\n"


# ===========================================================================
# HTML
# ===========================================================================

#: Inline, and it has to stay inline. A linked stylesheet or a web font would be a
#: network request from a document an offline system produced, and it would also stop
#: working the moment the file was copied anywhere.
_CSS: Final[str] = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0 auto; padding: 2.5rem 1.5rem 4rem; max-width: 52rem;
  font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif; line-height: 1.6;
  color: #1a1a1a; background: #ffffff; }
h1 { font-size: 1.7rem; margin: 0 0 1rem; }
h2 { font-size: 1.25rem; margin: 2.2rem 0 .6rem; padding-bottom: .3rem;
  border-bottom: 2px solid #e3e6ea; }
h3 { font-size: 1.05rem; margin: 1.4rem 0 .4rem; }
p { margin: .45rem 0; }
p.muted { color: #5b6470; font-size: .88rem; margin: .15rem 0 .8rem 1.2rem; }
ul { margin: .4rem 0 .9rem; padding-left: 1.3rem; }
li { margin: .18rem 0; }
.callout { border-left: 4px solid #b8860b; background: #fdf6e3; padding: .8rem 1rem;
  margin: 1rem 0; border-radius: 3px; }
.callout.info { border-left-color: #3a6ea5; background: #eef4fb; }
table { border-collapse: collapse; width: 100%; margin: .6rem 0 1rem; font-size: .93rem; }
th, td { border: 1px solid #d7dbe0; padding: .45rem .6rem; text-align: left;
  vertical-align: top; }
th { background: #f2f4f7; font-weight: 600; }
.letterhead { display: flex; align-items: center; gap: 1rem; padding-bottom: .8rem;
  margin-bottom: 1.4rem; border-bottom: 3px double #b0b6bd; }
.letterhead-logo { height: 56px; width: auto; flex: 0 0 auto; }
.letterhead-name { font-size: 1.2rem; font-weight: 700; letter-spacing: .02em; }
.letterhead-sub { font-size: .85rem; color: #5b6470; margin-top: .15rem; }
.sign-date { margin-top: 1.6rem; }
.sign-table { width: 100%; border-collapse: collapse; margin-top: .4rem; font-size: .9rem;
  break-inside: avoid; }
.sign-table th { border: none; background: none; text-align: center; font-weight: 600;
  padding-bottom: .2rem; }
.sign-table td { border: none; text-align: center; }
.sign-table td.sign-space { height: 62px; }
.sign-table td.sign-rule { border-top: 1px solid #1a1a1a; padding-top: .3rem; color: #5b6470; }
.page-footer { margin-top: 2.5rem; padding-top: .6rem; border-top: 1px solid #d7dbe0;
  font-size: .8rem; color: #5b6470; }
@media print {
  body { padding: 0; max-width: none; }
  .callout { break-inside: avoid; }
  .letterhead { break-after: avoid; }
  /* Page numbers are left to the print dialog. CSS `@page` counters are not
     dependably supported across the engines this file might be opened in, and a
     footer that says "1 of 1" on a six-page document is worse than none. */
  .page-footer { position: fixed; bottom: 0; }
}
"""


def render_html(document: MinuteDocument) -> str:
    def esc(text: str) -> str:
        return _html.escape(str(text), quote=False)

    parts: list[str] = []
    for block in document.blocks:
        if isinstance(block, Letterhead):
            logo = ""
            if block.logo and block.logo_media_type:
                # Inlined as a data URI. A linked file would be a network or filesystem
                # dependency in a document whose whole point is that it stands alone.
                encoded = base64.b64encode(block.logo).decode("ascii")
                logo = (
                    f'<img class="letterhead-logo" alt="" '
                    f'src="data:{block.logo_media_type};base64,{encoded}">'
                )
            subtitle = (
                f'<div class="letterhead-sub">{esc(block.subtitle)}</div>'
                if block.subtitle
                else ""
            )
            parts.append(
                f'<header class="letterhead">{logo}<div>'
                f'<div class="letterhead-name">{esc(block.organisation)}</div>'
                f"{subtitle}</div></header>"
            )
        elif isinstance(block, Signatures):
            head = "".join(f"<th>{esc(role)}</th>" for role in block.roles)
            space = "".join('<td class="sign-space"></td>' for _ in block.roles)
            rule = "".join('<td class="sign-rule">(............)</td>' for _ in block.roles)
            stamp = (
                f'<p class="sign-date">{esc(block.place_and_date)}</p>'
                if block.place_and_date
                else ""
            )
            parts.append(
                f'{stamp}<table class="sign-table"><thead><tr>{head}</tr></thead>'
                f"<tbody><tr>{space}</tr><tr>{rule}</tr></tbody></table>"
            )
        elif isinstance(block, Heading):
            level = max(1, min(6, block.level))
            parts.append(f"<h{level}>{esc(block.text)}</h{level}>")
        elif isinstance(block, Callout):
            css = "callout" if block.tone == "warning" else "callout info"
            parts.append(f'<div class="{css}">{esc(block.text)}</div>')
        elif isinstance(block, Bullets):
            entries = "".join(f"<li>{esc(entry)}</li>" for entry in block.items)
            parts.append(f"<ul>{entries}</ul>")
        elif isinstance(block, Table):
            head = "".join(f"<th>{esc(cell)}</th>" for cell in block.headers)
            body = "".join(
                "<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>"
                for row in block.rows
            )
            parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
        else:
            css = ' class="muted"' if block.style == "muted" else ""
            parts.append(f"<p{css}>{esc(block.text)}</p>")

    footer = " · ".join(
        part for part in (document.footer_left, document.footer_right) if part
    )
    if footer:
        parts.append(f'<footer class="page-footer">{esc(footer)}</footer>')
    body = "\n".join(parts)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="id">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(document.title)}</title>\n"
        f"<style>{_CSS}</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )
