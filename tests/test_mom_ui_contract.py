"""What the minutes panel must show, and must never do. Static checks over the shell.

The shell is three static files with no build step, so the only thing standing between a
careless edit and a panel that silently misleads an operator is a test that reads them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "mom_igd" / "shell" / "web"


@pytest.fixture(scope="module")
def html() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return (WEB / "app.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js() -> str:
    return (WEB / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def block(js: str) -> str:
    """Just the minutes module."""
    return js[js.index("Minutes panel") :]


# ===========================================================================
# What the operator must always be told
# ===========================================================================


def test_the_panel_says_the_result_is_an_unreviewed_draft(html: str) -> None:
    """A page that does not say a machine wrote it will be read as if a person did."""
    panel = html[html.index('id="mom-panel"') : html.index("FUTURE FEATURES")].lower()
    assert "draf" in panel
    # Either accepted phrasing of "no human has checked this". Listed rather than
    # pattern-matched so widening it stays a deliberate edit.
    assert any(
        phrase in panel
        for phrase in ("belum diperiksa", "belum ada manusia yang memeriksa")
    ), "the panel must say no human has reviewed the result"


def test_the_panel_says_no_voice_is_recognised(html: str) -> None:
    """The roster is on screen elsewhere; the operator must not infer we matched voices."""
    panel = html[html.index('id="mom-panel"') : html.index("FUTURE FEATURES")]
    assert "tidak mengenali suara" in panel


def test_the_panel_explains_that_a_pic_comes_only_from_the_recording(html: str) -> None:
    panel = html[html.index('id="mom-panel"') : html.index("FUTURE FEATURES")]
    assert "disebut di rekaman" in panel


def test_hiding_unverified_points_says_they_are_still_stored(html: str) -> None:
    """Otherwise the checkbox reads as "delete these", which is not what it does."""
    panel = html[html.index('id="mom-panel"') : html.index("FUTURE FEATURES")]
    assert "tetap tersimpan" in panel


def test_the_renderer_marks_every_verification_state(block: str) -> None:
    assert "BELUM TERVERIFIKASI" in block
    assert "VERIFIED" in block and "REBOUND" in block and "UNVERIFIED" in block


def test_a_reversed_decision_is_explained_in_the_panel(block: str) -> None:
    """A cancelled decision shown beside its cancellation, unmarked, is the worst case."""
    assert "POSSIBLY_SUPERSEDED" in block
    assert "dibatalkan atau diubah" in block


def test_an_absent_pic_is_rendered_as_not_stated_not_as_undecided(block: str) -> None:
    assert "tidak disebutkan" in block


def test_the_panel_warns_about_an_unsupported_number_in_the_summary(block: str) -> None:
    assert "summary_unsupported_numbers" in block
    assert "tidak ada di poin manapun" in block


def test_coverage_is_shown_so_a_partial_run_is_visible(block: str) -> None:
    assert "covered_ms" in block and "transcript_ms" in block


# ===========================================================================
# The rules that have already caused a defect once
# ===========================================================================


def test_no_class_on_a_toggled_element_sets_display(css: str, html: str, block: str) -> None:
    """The defect that killed the revoke dialog, checked on this panel.

    `hidden` works only through the UA stylesheet, so **any** author-level `display`
    declaration on a class of a toggled element defeats it. This resolves the chain: which
    `el.X` the script toggles, which element id that is, which classes it carries, and
    whether any rule for those sets `display`. It caught `.mom-stats` during development.
    """
    toggled = set(re.findall(r"show\(el\.(\w+)", block))
    lookups = dict(re.findall(r"(\w+): document\.getElementById\('([^']+)'\)", block))
    ids = {lookups[name] for name in toggled if name in lookups}
    assert ids, "the panel must toggle something"

    offenders: list[str] = []
    for element_id in ids:
        tag = re.search(rf'<[^>]*\bid="{re.escape(element_id)}"[^>]*>', html)
        assert tag, element_id
        classes = re.search(r'class="([^"]*)"', tag.group(0))
        for name in (classes.group(1).split() if classes else []):
            for rule in re.finditer(rf"\.{re.escape(name)}\s*\{{([^}}]*)\}}", css):
                if re.search(r"(^|[;\s])display\s*:", rule.group(1)):
                    offenders.append(f"{element_id} carries .{name} which sets display")
    assert offenders == [], offenders


def test_the_panel_uses_no_repeating_timer(block: str) -> None:
    """Every other panel re-arms a `setTimeout`.

    A `setInterval` keeps firing while an async handler is still in flight, so a slow
    bridge call piles requests up behind itself.
    """
    assert "setInterval" not in block
    assert "tickElapsed" in block and "schedulePoll" in block


def test_every_element_the_script_looks_up_exists(html: str, block: str) -> None:
    wanted = set(re.findall(r"getElementById\('(mom-[a-z0-9-]+|open-mom-btn)'\)", block))
    present = set(re.findall(r'id="(mom-[a-z0-9-]+|open-mom-btn)"', html))
    assert wanted - present == set()


def test_the_panel_fetches_nothing_from_a_network(html: str, css: str, block: str) -> None:
    assert not re.findall(r'(?:src|href)="https?:', html)
    assert not re.findall(r"url\(\s*['\"]?https?:", css)
    for banned in ("fetch(", "XMLHttpRequest", "WebSocket", "import(", "cdn."):
        assert banned not in block, banned


def test_every_call_goes_through_the_bridge(block: str) -> None:
    """The session token never enters JavaScript; the proxy holds it."""
    assert "api_get" in block and "api_post" in block
    # The page must never name the header or hold a credential of its own. The shell
    # proxy attaches it in Python, which is the whole reason the proxy exists.
    for banned in ("X-MoM-Session-Token", "Authorization", "Bearer"):
        assert banned not in block, banned


def test_the_generate_button_starts_disabled(html: str) -> None:
    """Eligibility is the server's answer, not the page's guess."""
    tag = re.search(r'<button[^>]*id="mom-run-btn"[^>]*>', html)
    assert tag and "disabled" in tag.group(0)


def test_eligibility_comes_from_the_server(block: str) -> None:
    assert "row.eligible" in block
    assert "row.reason" in block


def test_the_progress_bar_is_indeterminate(css: str) -> None:
    """The run has no honest percentage, and a bar that sits at 90 % lies smoothly."""
    assert "mom-indeterminate" in css
    assert "prefers-reduced-motion" in css


def test_colour_is_never_the_only_signal(block: str) -> None:
    """A kind is shown as a heading in words; a verification state as text in a badge."""
    assert "KIND_LABELS[kind]" in block
    assert "cocok dengan rekaman" in block


def test_item_text_is_written_as_text_never_as_markup(block: str) -> None:
    """Transcript content is untrusted input: it is whatever was said in the room."""
    assert "innerHTML" not in block
    assert "textContent" in block
