"""The organisation's mark, in the letterhead and in the window, from one file.

Two rules meet here and both are checked.

`resolve_branding` is the only place a branding file is read -- renderers are handed
bytes so none of them can be pointed at a path by a document. The shell is not an
exception: it asks the same function, which is why replacing one PNG changes the
letterhead and the top bar together instead of leaving them to drift.

And a letterhead never displaces what protects the reader. A logo makes a document look
official, which is exactly the state in which somebody circulates a draft without reading
it, so the draft banner must still sit directly beneath the heading.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "mom_igd" / "shell" / "web"


@pytest.fixture(scope="module")
def js() -> str:
    return (WEB / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


def test_the_shell_does_not_open_the_branding_file_itself() -> None:
    """One reader, or the two consumers diverge the first time somebody edits one."""
    import ast
    import inspect

    from mom_igd.shell.launcher import ShellApi

    source = Path(inspect.getfile(ShellApi)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "get_branding"
    )
    body = ast.unparse(function)
    assert "resolve_branding" in body, "the shell must read branding the same way exporters do"
    for banned in ("open(", "read_bytes", "read_text", "branding_dir", "/ branding"):
        assert banned not in body, f"the shell must not reach for the file itself ({banned})"


def test_a_missing_logo_leaves_the_built_in_mark() -> None:
    """A clone of this tool is not any particular company's, and must not look like it."""
    import inspect

    from mom_igd.shell.launcher import ShellApi

    source = inspect.getsource(ShellApi.get_branding)
    assert '"logo": None' in source, "no branding must resolve to no logo, not an error"

    js_source = (WEB / "app.js").read_text(encoding="utf-8")
    block = js_source[js_source.index("ORGANISATION MARK") :]
    assert "if (!result || !result.logo) return;" in block, (
        "the page must keep its own mark when nothing is configured"
    )


def test_the_logo_never_stops_the_window_opening() -> None:
    import inspect

    from mom_igd.shell.launcher import ShellApi

    source = inspect.getsource(ShellApi.get_branding)
    assert "except Exception" in source
    assert '"ok": True' in source, (
        "an unreadable logo is a normal outcome, not a failure the page must handle"
    )


def test_the_mark_is_placed_by_height_so_any_shape_works(js: str, html: str) -> None:
    """A wide wordmark and a square emblem must both land at the same optical weight."""
    css = (WEB / "app.css").read_text(encoding="utf-8")
    rule = re.search(r"\.brand-logo\s*\{([^}]*)\}", css)
    assert rule, ".brand-logo must be styled"
    assert re.search(r"height:\s*\d", rule.group(1))
    assert "width: auto" in rule.group(1)
    assert "object-fit: contain" in rule.group(1), (
        "a logo must never be stretched to fit a box"
    )
    assert 'id="brand-logo"' in html and 'id="brand-mark"' in html


def test_the_letterhead_does_not_displace_the_draft_banner() -> None:
    """The rule a logo is most likely to break.

    Built from the document model rather than by exporting, so this holds for every
    renderer at once and needs no model on the machine running the suite.
    """
    from mom_igd.mom.document import build_document

    _PNG_HEADER = bytes([137, 80, 78, 71, 13, 10, 26, 10])

    document = build_document(
        minute={
            "title": "Rapat Uji",
            "status": "DRAFT",
            "revision": 1,
            "summary": [],
            "document_number": "NOT/2026/08/001",
        },
        items=(),
        meeting={"title": "Rapat Uji"},
        branding={
            "organisation": "CONTOH",
            "subtitle": "Minutes of Meeting",
            "logo": _PNG_HEADER + bytes(32),
            "logo_media_type": "image/png",
            "show_signature_block": True,
            "signature_roles": ("Notulis",),
        },
    )

    texts = [str(getattr(block, "text", "")) for block in document.blocks]
    banner_at = next(
        (index for index, text in enumerate(texts) if "DRAF" in text.upper()), None
    )
    assert banner_at is not None, "the draft banner must survive a letterhead"
    assert banner_at <= 4, (
        f"the draft banner sits at position {banner_at}, pushed below the letterhead it "
        f"must follow immediately: {texts[:6]}"
    )
