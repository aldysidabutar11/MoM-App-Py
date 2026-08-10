"""Write a Word document from stdlib. No dependency, because a .docx is a zip of XML.

The team this is for works in Word, and a minute they cannot edit and circulate is a
minute they will retype. The realistic alternatives were `python-docx` -- another
dependency to vet, pin and carry offline, for a document with four block types -- or
handing them HTML and telling them to save-as. This is about two hundred lines and it
produces a file Word opens natively.

**What is written is the OOXML subset the document model needs**: headings, body text,
muted text, bullets, a bordered table and a shaded callout. Not a general Word writer, and
it should not become one. Anything richer belongs in a dependency, and needing it would be
a sign the document had grown beyond what a minute should be.

The parts are the minimum a conforming package requires -- content types, a package
relationship, a document, and the styles the document refers to. `docProps` is included
because a file that leaves the building should say what produced it.

**Deterministic output.** Every entry gets a fixed timestamp and the parts are written in
a fixed order, so the same minute exports byte-identical every time. That is what lets the
SHA-256 on the export row mean "this file", and it is why the timestamp is not `now`.
"""

from __future__ import annotations

import zipfile
from typing import Final, Iterable, Sequence
from xml.sax.saxutils import escape

from mom_igd.mom.document import (
    Bullets,
    Callout,
    Heading,
    Letterhead,
    MinuteDocument,
    Paragraph,
    Signatures,
    Table,
)

__all__ = ["render_docx"]

#: A fixed date in every zip entry. Word neither reads nor displays it, and a real
#: timestamp would make two exports of the same minute differ in bytes -- which would make
#: the recorded SHA-256 useless for telling one document from another.
_FIXED_DATE: Final[tuple[int, int, int, int, int, int]] = (2026, 1, 1, 0, 0, 0)

_W: Final[str] = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_CONTENT_TYPES: Final[str] = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>
<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
<Default Extension="png" ContentType="image/png"/>
<Default Extension="jpeg" ContentType="image/jpeg"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

_RELS: Final[str] = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

_REL_NS: Final[str] = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _document_rels(*, has_logo: bool, logo_extension: str) -> str:
    """Relationships from the document to its styles, header, footer and logo."""
    logo = (
        f'<Relationship Id="rId4" Type="{_REL_NS}/image" Target="media/logo.{logo_extension}"/>'
        if has_logo
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{_REL_NS}/styles" Target="styles.xml"/>'
        f'<Relationship Id="rId2" Type="{_REL_NS}/header" Target="header1.xml"/>'
        f'<Relationship Id="rId3" Type="{_REL_NS}/footer" Target="footer1.xml"/>'
        f"{logo}"
        "</Relationships>"
    )

#: Sizes are half-points (``w:sz``), so 28 is 14 pt. Colours are hex without a hash.
#:
#: **Child order inside ``w:pPr`` is a schema sequence, not a preference**: pBdr, shd,
#: spacing, ind, outlineLvl. Word rejects a file whose properties are out of order with a
#: "content is unreadable" repair prompt and no indication of which element was wrong --
#: which for the operator is indistinguishable from a corrupt export.
_STYLES: Final[str] = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{_W}">
<w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/><w:sz w:val="22"/>
</w:rPr></w:rPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>
<w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>
<w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="240"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="40"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>
<w:basedOn w:val="Normal"/><w:pPr>
<w:pBdr><w:bottom w:val="single" w:sz="6" w:color="C8CDD3"/></w:pBdr>
<w:spacing w:before="360" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>
<w:basedOn w:val="Normal"/><w:pPr>
<w:spacing w:before="240" w:after="80"/><w:outlineLvl w:val="1"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Muted"><w:name w:val="Muted"/>
<w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="80"/><w:ind w:left="360"/></w:pPr>
<w:rPr><w:i/><w:color w:val="5B6470"/><w:sz w:val="19"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Bullet"><w:name w:val="Bullet"/>
<w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="40"/>
<w:ind w:left="360" w:hanging="180"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Callout"><w:name w:val="Callout"/>
<w:basedOn w:val="Normal"/><w:pPr>
<w:pBdr><w:left w:val="single" w:sz="18" w:space="6" w:color="B8860B"/></w:pBdr>
<w:shd w:val="clear" w:fill="FDF6E3"/>
<w:spacing w:before="160" w:after="160"/><w:ind w:left="120"/></w:pPr>
<w:rPr><w:b/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="LetterheadName"><w:name w:val="Letterhead Name"/>
<w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="0"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="30"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="LetterheadSub"><w:name w:val="Letterhead Subtitle"/>
<w:basedOn w:val="Normal"/><w:pPr>
<w:pBdr><w:bottom w:val="double" w:sz="6" w:space="6" w:color="B0B6BD"/></w:pBdr>
<w:spacing w:after="240"/></w:pPr>
<w:rPr><w:color w:val="5B6470"/><w:sz w:val="19"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="PageMeta"><w:name w:val="Page Meta"/>
<w:basedOn w:val="Normal"/><w:pPr>
<w:tabs><w:tab w:val="right" w:pos="9360"/></w:tabs><w:spacing w:after="0"/></w:pPr>
<w:rPr><w:color w:val="5B6470"/><w:sz w:val="17"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="SignRole"><w:name w:val="Signature Role"/>
<w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="0"/><w:jc w:val="center"/></w:pPr>
<w:rPr><w:b/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="SignRule"><w:name w:val="Signature Rule"/>
<w:basedOn w:val="Normal"/><w:pPr>
<w:pBdr><w:top w:val="single" w:sz="6" w:color="1A1A1A"/></w:pBdr>
<w:spacing w:before="60" w:after="0"/><w:jc w:val="center"/></w:pPr>
<w:rPr><w:color w:val="5B6470"/><w:sz w:val="19"/></w:rPr></w:style>
<w:style w:type="table" w:styleId="MomTable"><w:name w:val="MoM Table"/>
<w:tblPr><w:tblBorders>
<w:top w:val="single" w:sz="4" w:color="D7DBE0"/>
<w:left w:val="single" w:sz="4" w:color="D7DBE0"/>
<w:bottom w:val="single" w:sz="4" w:color="D7DBE0"/>
<w:right w:val="single" w:sz="4" w:color="D7DBE0"/>
<w:insideH w:val="single" w:sz="4" w:color="D7DBE0"/>
<w:insideV w:val="single" w:sz="4" w:color="D7DBE0"/>
</w:tblBorders></w:tblPr></w:style>
</w:styles>"""

#: Page width minus margins, in twentieths of a point: 12240 - 2 * 1440.
_CONTENT_WIDTH: Final[int] = 9360


#: One inch in English Metric Units, the unit OOXML measures drawings in.
_EMU_PER_INCH: Final[int] = 914_400

#: How tall the letterhead logo is drawn, in inches. Width follows the aspect ratio.
_LOGO_HEIGHT_INCHES: Final[float] = 0.62


def _image_size(blob: bytes) -> tuple[int, int] | None:
    """Pixel dimensions of a PNG or JPEG, read from the bytes themselves.

    Written out rather than pulling in Pillow: this needs two integers, and Pillow is a
    large dependency with a native build. Unrecognised or truncated data returns ``None``,
    and the caller then draws no logo -- an export must not fail because somebody put a
    BMP in the branding folder.
    """
    if blob[:8] == b"\x89PNG\r\n\x1a\n" and len(blob) >= 24:
        # IHDR is always the first chunk, and width/height are its first eight bytes.
        return (
            int.from_bytes(blob[16:20], "big"),
            int.from_bytes(blob[20:24], "big"),
        )
    if blob[:2] == b"\xff\xd8":
        offset = 2
        while offset + 9 < len(blob):
            if blob[offset] != 0xFF:
                offset += 1
                continue
            marker = blob[offset + 1]
            # SOF0..SOF15, excluding the four that are not frame headers.
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height = int.from_bytes(blob[offset + 5 : offset + 7], "big")
                width = int.from_bytes(blob[offset + 7 : offset + 9], "big")
                return (width, height) if width and height else None
            length = int.from_bytes(blob[offset + 2 : offset + 4], "big")
            if length <= 0:
                return None
            offset += 2 + length
    return None


def _logo_drawing(blob: bytes) -> str:
    """A single inline image run, sized from the file's own aspect ratio."""
    size = _image_size(blob)
    if size is None:
        return ""
    width_px, height_px = size
    height = int(_LOGO_HEIGHT_INCHES * _EMU_PER_INCH)
    width = int(height * (width_px / height_px)) if height_px else height
    return (
        "<w:r><w:drawing>"
        f'<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{width}" cy="{height}"/>'
        '<wp:docPr id="1" name="Logo"/>'
        "<a:graphic><a:graphicData "
        'uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        "<pic:pic>"
        '<pic:nvPicPr><pic:cNvPr id="0" name="Logo"/><pic:cNvPicPr/></pic:nvPicPr>'
        '<pic:blipFill><a:blip r:embed="rId4"/><a:stretch><a:fillRect/></a:stretch>'
        "</pic:blipFill>"
        "<pic:spPr><a:xfrm><a:off x=\"0\" y=\"0\"/>"
        f'<a:ext cx="{width}" cy="{height}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        "</pic:pic></a:graphicData></a:graphic></wp:inline>"
        "</w:drawing></w:r>"
    )


def _core_properties(title: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<cp:coreProperties '
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{escape(title)}</dc:title>"
        "<dc:creator>MoM-IGD</dc:creator>"
        "<cp:lastModifiedBy>MoM-IGD</cp:lastModifiedBy>"
        # Stated in the file's own properties, not only in its text: a document that
        # travels should carry the fact that a machine wrote it and nobody checked it.
        "<cp:keywords>draf otomatis; belum diperiksa</cp:keywords>"
        "</cp:coreProperties>"
    )


_APP_PROPERTIES: Final[str] = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
    'extended-properties">'
    "<Application>MoM-IGD (offline)</Application>"
    "</Properties>"
)


def _runs(text: str, *, bold: bool = False) -> str:
    """Text runs, with newlines turned into breaks and whitespace preserved."""
    pieces: list[str] = []
    for index, line in enumerate(str(text).split("\n")):
        if index:
            pieces.append("<w:br/>")
        pieces.append(f'<w:t xml:space="preserve">{escape(line)}</w:t>')
    properties = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f"<w:r>{properties}{''.join(pieces)}</w:r>"


def _paragraph(text: str, *, style: str | None = None, bold: bool = False) -> str:
    properties = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{properties}{_runs(text, bold=bold)}</w:p>"


def _cell(text: str, width: int, *, header: bool) -> str:
    shading = '<w:shd w:val="clear" w:fill="F2F4F7"/>' if header else ""
    return (
        "<w:tc>"
        f'<w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{shading}</w:tcPr>'
        f"<w:p>{_runs(text, bold=header)}</w:p>"
        "</w:tc>"
    )


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    # Weighted columns: the action text needs the room, the numbers do not. Falls back to
    # equal widths for any table that is not the action list.
    weights = (5, 46, 17, 17, 15) if len(headers) == 5 else None
    if weights is None:
        widths = [_CONTENT_WIDTH // max(1, len(headers))] * len(headers)
    else:
        widths = [_CONTENT_WIDTH * weight // 100 for weight in weights]

    grid = "".join(f'<w:gridCol w:w="{width}"/>' for width in widths)
    header_row = (
        '<w:tr><w:trPr><w:tblHeader/></w:trPr>'
        + "".join(
            _cell(cell, widths[index], header=True) for index, cell in enumerate(headers)
        )
        + "</w:tr>"
    )
    body = "".join(
        "<w:tr>"
        + "".join(_cell(cell, widths[index], header=False) for index, cell in enumerate(row))
        + "</w:tr>"
        for row in rows
    )
    return (
        "<w:tbl>"
        '<w:tblPr><w:tblStyle w:val="MomTable"/>'
        f'<w:tblW w:w="{_CONTENT_WIDTH}" w:type="dxa"/>'
        "<w:tblBorders>"
        '<w:top w:val="single" w:sz="4" w:color="D7DBE0"/>'
        '<w:left w:val="single" w:sz="4" w:color="D7DBE0"/>'
        '<w:bottom w:val="single" w:sz="4" w:color="D7DBE0"/>'
        '<w:right w:val="single" w:sz="4" w:color="D7DBE0"/>'
        '<w:insideH w:val="single" w:sz="4" w:color="D7DBE0"/>'
        '<w:insideV w:val="single" w:sz="4" w:color="D7DBE0"/>'
        "</w:tblBorders></w:tblPr>"
        f"<w:tblGrid>{grid}</w:tblGrid>{header_row}{body}</w:tbl>"
        # A table must be followed by a paragraph, or Word merges it with whatever comes
        # next and the file opens with a repair prompt.
        "<w:p/>"
    )


#: Namespaces the header, footer and drawing markup need. Declared on every part that
#: uses them: an OOXML part is validated on its own, not as part of the package.
_NS: Final[str] = (
    f'xmlns:w="{_W}" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
)


def _page_field() -> str:
    """A live page number. Word recalculates it; the file does not hard-code a count."""
    return (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        "<w:r><w:t>1</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        '<w:r><w:t xml:space="preserve"> / </w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> NUMPAGES </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        "<w:r><w:t>1</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    )


def _header_part(document: MinuteDocument) -> str:
    """A running header carrying the filing reference and the meeting title.

    Deliberately not the letterhead. The letterhead with its logo belongs on page one
    only; repeating it on every page turns a minute into a brochure and pushes the
    content down. What repeats is what somebody needs when they are holding page four
    with the first page elsewhere: which document this is.
    """
    left = document.document_number or document.title
    right = document.title if document.document_number else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:hdr {_NS}>"
        '<w:p><w:pPr><w:pStyle w:val="PageMeta"/>'
        '<w:pBdr><w:bottom w:val="single" w:sz="4" w:color="D7DBE0"/></w:pBdr>'
        "</w:pPr>"
        f'<w:r><w:t xml:space="preserve">{escape(left)}</w:t></w:r>'
        + (
            f'<w:r><w:tab/><w:t xml:space="preserve">{escape(right)}</w:t></w:r>'
            if right
            else ""
        )
        + "</w:p></w:hdr>"
    )


def _footer_part(document: MinuteDocument) -> str:
    note = document.footer_right
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:ftr {_NS}>"
        '<w:p><w:pPr><w:pStyle w:val="PageMeta"/>'
        '<w:pBdr><w:top w:val="single" w:sz="4" w:color="D7DBE0"/></w:pBdr>'
        "</w:pPr>"
        f'<w:r><w:t xml:space="preserve">{escape(note or "DRAF - belum diperiksa manusia")}'
        "</w:t></w:r>"
        "<w:r><w:tab/></w:r>" + _page_field() + "</w:p></w:ftr>"
    )


def _signature_table(block: Signatures) -> str:
    """Blank columns, with a rule to sign above. No name is printed on any of them."""
    columns = max(1, len(block.roles))
    width = _CONTENT_WIDTH // columns
    grid = "".join(f'<w:gridCol w:w="{width}"/>' for _ in range(columns))

    def row(cells: str) -> str:
        return f"<w:tr>{cells}</w:tr>"

    roles = "".join(
        f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>'
        f'<w:p><w:pPr><w:pStyle w:val="SignRole"/></w:pPr>'
        f'<w:r><w:t xml:space="preserve">{escape(role)}</w:t></w:r></w:p></w:tc>'
        for role in block.roles
    )
    # Three empty paragraphs of signing room, then the rule.
    space = "".join(
        f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>'
        "<w:p/><w:p/><w:p/></w:tc>"
        for _ in block.roles
    )
    rules = "".join(
        f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>'
        f'<w:p><w:pPr><w:pStyle w:val="SignRule"/></w:pPr>'
        '<w:r><w:t>(..........................)</w:t></w:r></w:p></w:tc>'
        for _ in block.roles
    )
    return (
        "<w:tbl>"
        f'<w:tblPr><w:tblW w:w="{_CONTENT_WIDTH}" w:type="dxa"/></w:tblPr>'
        f"<w:tblGrid>{grid}</w:tblGrid>"
        f"{row(roles)}{row(space)}{row(rules)}"
        "</w:tbl><w:p/>"
    )


def _body(document: MinuteDocument) -> str:
    parts: list[str] = []
    for index, block in enumerate(document.blocks):
        if isinstance(block, Letterhead):
            if block.logo:
                drawing = _logo_drawing(block.logo)
                if drawing:
                    parts.append(f'<w:p><w:pPr><w:pStyle w:val="Normal"/>'
                                 f"<w:spacing w:after=\"0\"/></w:pPr>{drawing}</w:p>")
            parts.append(_paragraph(block.organisation, style="LetterheadName"))
            # The rule under the letterhead lives on the subtitle style, so an
            # organisation with no subtitle still gets the line.
            parts.append(_paragraph(block.subtitle, style="LetterheadSub"))
        elif isinstance(block, Signatures):
            if block.place_and_date:
                parts.append(_paragraph(block.place_and_date))
            parts.append(_signature_table(block))
        elif isinstance(block, Heading):
            style = "Title" if (block.level == 1 and index == 0) else f"Heading{min(2, block.level)}"
            parts.append(_paragraph(block.text, style=style))
        elif isinstance(block, Callout):
            parts.append(_paragraph(block.text, style="Callout"))
        elif isinstance(block, Bullets):
            parts.extend(_paragraph(f"•  {entry}", style="Bullet") for entry in block.items)
        elif isinstance(block, Table):
            parts.append(_table(block.headers, block.rows))
        elif isinstance(block, Paragraph):
            parts.append(
                _paragraph(block.text, style="Muted" if block.style == "muted" else None)
            )
    section = (
        "<w:sectPr>"
        '<w:headerReference w:type="default" r:id="rId2"/>'
        '<w:footerReference w:type="default" r:id="rId3"/>'
        '<w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
        'w:header="576" w:footer="576" w:gutter="0"/>'
        "</w:sectPr>"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {_NS}><w:body>{''.join(parts)}{section}</w:body></w:document>"
    )


def render_docx(document: MinuteDocument) -> bytes:
    """Serialise a document to .docx bytes. Deterministic for a given input."""
    import io

    logo: bytes | None = None
    extension = "png"
    for block in document.blocks:
        if isinstance(block, Letterhead) and block.logo and _image_size(block.logo):
            logo = block.logo
            extension = "jpeg" if block.logo[:2] == b"\xff\xd8" else "png"
            break

    buffer = io.BytesIO()
    parts: list[tuple[str, bytes]] = [
        ("[Content_Types].xml", _CONTENT_TYPES.encode("utf-8")),
        ("_rels/.rels", _RELS.encode("utf-8")),
        ("docProps/core.xml", _core_properties(document.title).encode("utf-8")),
        ("docProps/app.xml", _APP_PROPERTIES.encode("utf-8")),
        (
            "word/_rels/document.xml.rels",
            _document_rels(has_logo=logo is not None, logo_extension=extension).encode("utf-8"),
        ),
        ("word/styles.xml", _STYLES.encode("utf-8")),
        ("word/header1.xml", _header_part(document).encode("utf-8")),
        ("word/footer1.xml", _footer_part(document).encode("utf-8")),
        ("word/document.xml", _body(document).encode("utf-8")),
    ]
    if logo is not None:
        parts.append((f"word/media/logo.{extension}", logo))

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts:
            info = zipfile.ZipInfo(name, date_time=_FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, content)
    return buffer.getvalue()
