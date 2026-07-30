"""The transcription panel: every control exists, and nothing leaks or downloads.

Static assertions against the shipped page, because a panel that references an element
id that does not exist fails silently in a browser -- the listener is simply never
attached, and the button does nothing. That is what left the Phase 3 revoke dialog dead.

The `[hidden]` cascade rule is checked again here for the same reason: it is the one
stylesheet fact that, if broken, makes a panel that looks fine swallow every click.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "mom_igd" / "shell" / "web"


@pytest.fixture(scope="module")
def html() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js() -> str:
    return (WEB / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return (WEB / "app.css").read_text(encoding="utf-8")


PANEL_IDS = (
    "transcript-panel",
    "open-transcript-btn",
    "asr-model-kv",
    "asr-model-missing",
    "asr-recording-uuid",
    "asr-run-btn",
    "asr-cancel-btn",
    "asr-load-btn",
    "asr-error",
    "asr-pill",
    "asr-cost-kv",
    "asr-stage-list",
    "asr-pass2-kv",
    "asr-flagged-table",
    "asr-transcript-kv",
    "asr-segment-list",
    "asr-transcript-empty",
)


# ===========================================================================
# The markup and the script agree
# ===========================================================================


@pytest.mark.parametrize("element_id", PANEL_IDS)
def test_every_referenced_element_exists(html: str, element_id: str) -> None:
    assert f'id="{element_id}"' in html, element_id


def test_the_script_looks_up_only_ids_that_exist(html: str, js: str) -> None:
    """A missing id attaches no listener, and the button silently does nothing."""
    block = js[js.index("Phase 4: transcription panel") :]
    for match in re.finditer(r"getElementById\('([^']+)'\)", block):
        assert f'id="{match.group(1)}"' in html, match.group(1)


def test_the_panel_starts_hidden(html: str) -> None:
    assert re.search(r'id="transcript-panel"[^>]*\shidden', html), (
        "the panel must be hidden until the operator opens it"
    )


def test_the_hidden_attribute_still_beats_every_author_display_rule(css: str) -> None:
    """The load-bearing rule. Phase 3's revoke dialog died without it."""
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", css), (
        "app.css must keep the `[hidden] { display: none !important }` rule"
    )


def test_no_class_on_a_toggled_element_sets_display(css: str, html: str, js: str) -> None:
    """The exact defect that killed the revoke dialog, checked on this panel.

    `hidden` works only through the UA stylesheet, so **any** author-level `display`
    declaration on a class of a toggled element defeats it. This resolves the chain for
    real: which `el.X` the script toggles, which element id that is, which classes that
    element carries, and whether any rule for those classes sets `display`.
    """
    block = js[js.index("Phase 4: transcription panel") :]
    toggled_vars = set(re.findall(r"show\(el\.(\w+)", block))
    assert toggled_vars, "the panel must toggle something"

    lookups = dict(re.findall(r"(\w+): document\.getElementById\('([^']+)'\)", block))
    toggled_ids = {lookups[name] for name in toggled_vars if name in lookups}
    assert toggled_ids, toggled_vars

    offenders: list[str] = []
    for element_id in toggled_ids:
        tag = re.search(rf'<[^>]*\bid="{re.escape(element_id)}"[^>]*>', html)
        assert tag, element_id
        classes = re.search(r'class="([^"]*)"', tag.group(0))
        for name in (classes.group(1).split() if classes else []):
            for rule in re.finditer(rf"\.{re.escape(name)}\s*\{{([^}}]*)\}}", css):
                if re.search(r"(^|[;\s])display\s*:", rule.group(1)):
                    offenders.append(f"{element_id} carries .{name} which sets display")
    assert offenders == [], offenders


# ===========================================================================
# Nothing that must not be there
# ===========================================================================


def test_the_panel_has_no_remote_asset(html: str) -> None:
    block = html[html.index("transcript-panel") :]
    for forbidden in ("http://", "https://", "//cdn", "fonts.googleapis", "integrity="):
        assert forbidden not in block, forbidden


def test_the_script_never_touches_the_session_token(js: str) -> None:
    block = js[js.index("Phase 4: transcription panel") :]
    for forbidden in ("session_token", "X-MoM-Session-Token", "Bearer", "localStorage"):
        assert forbidden not in block, forbidden


def test_the_script_reaches_the_api_only_through_the_bridge(js: str) -> None:
    """A direct fetch would need the token in JavaScript."""
    block = js[js.index("Phase 4: transcription panel") :]
    for forbidden in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource"):
        assert forbidden not in block, forbidden
    assert "window.pywebview" in block
    assert "api_get" in block and "api_post" in block


def test_the_script_never_uses_inner_html(js: str) -> None:
    """Transcript text is arbitrary user speech; assigning it as HTML is an injection."""
    block = js[js.index("Phase 4: transcription panel") :]
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert forbidden not in block, forbidden
    assert "textContent" in block


def test_the_script_calls_no_microphone_api(js: str) -> None:
    block = js[js.index("Phase 4: transcription panel") :]
    for forbidden in ("getUserMedia", "AudioContext", "MediaRecorder", "mediaDevices"):
        assert forbidden not in block, forbidden


def test_the_panel_offers_no_way_to_download_a_model(html: str, js: str) -> None:
    """Provisioning is a deliberate command-line action, not a button."""
    block = js[js.index("Phase 4: transcription panel") :]
    assert "/asr/provision" not in block
    assert "provision" not in block.lower().replace("provisioning", "")
    panel = html[html.index("transcript-panel") : html.index("FUTURE FEATURES")]
    assert "provision all" in panel, "the panel must tell the operator the command"
    assert "<button" not in panel[panel.index("asr-model-missing") : ][:400]


def test_the_transcribe_button_is_disabled_until_a_model_is_ready(js: str) -> None:
    block = js[js.index("Phase 4: transcription panel") :]
    assert "el.run.disabled = busy || !valid || !modelsReady" in block


def test_the_uuid_is_validated_in_the_page_before_being_sent(js: str) -> None:
    block = js[js.index("Phase 4: transcription panel") :]
    assert "UUID_RE.test" in block
    assert re.search(r"UUID_RE\s*=\s*/\^\[0-9a-f\]\{8\}", block)


# ===========================================================================
# What the panel must say
# ===========================================================================


def test_the_panel_states_that_no_speaker_is_assigned(html: str) -> None:
    panel = html[html.index("transcript-panel") : html.index("FUTURE FEATURES")]
    assert "UNASSIGNED" in panel
    assert "belum" in panel.lower()


def test_the_panel_states_that_accuracy_is_not_measured(html: str) -> None:
    """The claim the whole phase must not overstate."""
    panel = html[html.index("transcript-panel") : html.index("FUTURE FEATURES")]
    assert "belum diukur" in panel.lower()
    assert "pembanding" in panel.lower()


def test_the_panel_states_that_the_master_audio_is_not_modified(html: str) -> None:
    panel = html[html.index("transcript-panel") : html.index("FUTURE FEATURES")]
    assert "tidak pernah diubah" in panel


def test_the_panel_shows_the_speaker_status_from_the_payload(js: str) -> None:
    """Rendered from data, so a later phase that assigns one needs no page change."""
    block = js[js.index("Phase 4: transcription panel") :]
    assert "segment.speaker_status" in block


def test_the_panel_shows_pass2_reason_codes(js: str) -> None:
    block = js[js.index("Phase 4: transcription panel") :]
    assert "reason_codes" in block
    assert "selected_for_pass2" in block


def test_the_feature_card_is_marked_live(html: str) -> None:
    assert 'id="card-transcription"' in html
    card = html[html.index('id="card-transcription"') :][:900]
    assert "phase-tag-live" in card
    assert "Phase 4" in card
    assert "Belum diimplementasikan" not in card


def test_the_later_phase_cards_still_say_they_are_not_implemented(html: str) -> None:
    for heading in ("Diarization", "Review", "Export"):
        index = html.index(f"<h3>{heading}") if f"<h3>{heading}" in html else html.index(heading)
        card = html[index : index + 700]
        assert "Belum diimplementasikan" in card, heading


# ===========================================================================
# The proxy allowlist the panel depends on
# ===========================================================================


def test_every_path_the_panel_calls_is_on_the_allowlist(js: str) -> None:
    from mom_igd.shell.launcher import (
        ALLOWED_GET_PATTERNS,
        ALLOWED_POST_PATHS,
        ALLOWED_PROXY_PATHS,
    )

    block = js[js.index("Phase 4: transcription panel") :]
    literal_gets = set(re.findall(r"get\('(/asr/[\w/]*)'\)", block))
    assert literal_gets <= ALLOWED_PROXY_PATHS, literal_gets - ALLOWED_PROXY_PATHS
    literal_posts = set(re.findall(r"post\('(/asr/[\w/]*)'", block))
    assert literal_posts <= ALLOWED_POST_PATHS, literal_posts - ALLOWED_POST_PATHS

    sample = "12345678-1234-4123-8123-123456789abc"
    for templated in (
        f"/asr/transcript/{sample}",
        f"/asr/flagged/{sample}",
        f"/asr/revisions/{sample}",
    ):
        assert any(
            pattern.match(templated) for pattern in ALLOWED_GET_PATTERNS
        ), templated


def test_the_allowlist_has_no_asr_wildcard() -> None:
    """`/asr/*` would let the page reach any route added later."""
    from mom_igd.shell.launcher import ALLOWED_GET_PATTERNS, ALLOWED_POST_PATTERNS

    for pattern in ALLOWED_GET_PATTERNS + ALLOWED_POST_PATTERNS:
        assert ".*" not in pattern.pattern, pattern.pattern
        assert pattern.pattern.startswith("^") and pattern.pattern.endswith("$")


def test_the_provision_route_is_not_reachable_from_the_page() -> None:
    from mom_igd.shell.launcher import (
        ALLOWED_GET_PATTERNS,
        ALLOWED_POST_PATHS,
        ALLOWED_POST_PATTERNS,
        ALLOWED_PROXY_PATHS,
    )

    everything = ALLOWED_PROXY_PATHS | ALLOWED_POST_PATHS
    assert not any("provision" in path for path in everything)
    for pattern in ALLOWED_GET_PATTERNS + ALLOWED_POST_PATTERNS:
        assert "provision" not in pattern.pattern
