"""The Phase 3 screens, checked as static assets.

These are cheap, fast assertions about the shipped HTML/CSS/JS, and they exist to
catch the failures that a Python test cannot see:

* **the page must never capture audio** -- no `getUserMedia`, no `AudioContext`, no
  `MediaRecorder`, no upload of PCM (ADR-0012);
* **consent must never be pre-checked**, and the draft/legal-review status must be
  visible;
* **enrollment must be disabled while no model is provisioned**, with an honest
  explanation rather than a broken-looking button;
* **user-supplied text must be rendered safely** -- a participant's display name is
  the obvious injection vector on this screen;
* **no token in browser storage, no remote asset, no leaked timer.**
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mom_igd.api.app import WEB_DIR


@pytest.fixture(scope="module")
def sources() -> dict[str, str]:
    return {
        name: (Path(WEB_DIR) / name).read_text(encoding="utf-8")
        for name in ("index.html", "app.js", "app.css")
    }


def _strip_comments(script: str) -> str:
    """Remove /* */ and // comments so a rule stated in prose is not a hit.

    Necessary because the source documents its own prohibitions -- "no
    getUserMedia, no AudioContext" -- and a naive substring search would flag the
    sentence forbidding the thing as an instance of the thing.
    """
    script = re.sub(r"/\*.*?\*/", "", script, flags=re.S)
    return re.sub(r"//.*$", "", script, flags=re.M)


@pytest.fixture(scope="module")
def phase3_js(sources: dict[str, str]) -> str:
    """The Phase 3 block with comments already removed.

    Comments are stripped from the *whole* file before slicing. Slicing first would
    cut the header comment in half, leaving a fragment with no opening ``/*`` that
    the comment remover cannot recognise -- and the prose stating the rules would
    then read as code violating them.
    """
    marker = "PHASE 3 -- participants"
    assert marker in sources["app.js"], "the Phase 3 UI block is missing"
    stripped = _strip_comments(sources["app.js"])
    # After stripping, locate the block by a distinctive identifier it defines.
    anchor = "READINESS_POLL_MS"
    assert anchor in stripped, "the Phase 3 block did not survive comment stripping"
    return stripped[stripped.index(anchor) :]


@pytest.fixture(scope="module")
def code_only(sources: dict[str, str]) -> dict[str, str]:
    """Both scripts with comments removed, for whole-file prohibitions."""
    return {
        "app.js": _strip_comments(sources["app.js"]),
        # HTML comments state rules too (the ADR-0012 note above the panel).
        "index.html": re.sub(r"<!--.*?-->", "", sources["index.html"], flags=re.S),
    }


# ===================================================== no audio in the browser


@pytest.mark.parametrize(
    "forbidden",
    [
        "getUserMedia",
        "mediaDevices",
        "MediaRecorder",
        "AudioContext",
        "webkitAudioContext",
        "createMediaStreamSource",
        "AudioWorklet",
        "ScriptProcessorNode",
        "MediaStream",
    ],
)
def test_the_page_never_captures_audio(code_only: dict[str, str], forbidden: str) -> None:
    """Capture runs in Python; the browser has no microphone access at all."""
    for name, code in code_only.items():
        assert forbidden not in code, f"{name} references {forbidden}"


def test_the_page_never_uploads_or_decodes_audio(phase3_js: str) -> None:
    for forbidden in ("FormData", "Blob(", "atob(", "btoa(", "ArrayBuffer", "Int16Array"):
        assert forbidden not in phase3_js, f"the Phase 3 UI references {forbidden}"


def test_the_wizard_only_sends_a_command_to_record(phase3_js: str) -> None:
    """The page asks Python to record; it never supplies samples itself."""
    assert "/enrollment/sessions/current/samples" in phase3_js
    # The only field sent is a duration.
    assert "seconds: 10" in phase3_js
    assert "pcm" not in phase3_js.lower()


# ============================================================ safe rendering


def test_the_phase_3_block_uses_no_inner_html(phase3_js: str) -> None:
    """A display name is operator-supplied text; innerHTML would execute it."""
    code = phase3_js
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert forbidden not in code, f"the Phase 3 UI uses {forbidden}"


def test_participant_text_is_rendered_with_text_content(phase3_js: str) -> None:
    code = phase3_js
    assert "textContent" in code
    assert "createElement" in code
    # The badge state is a fixed keyword on dataset, never spliced into a class.
    assert "span.dataset.state = state" in code
    assert "className = 'state-badge'" in code


def test_no_eval_or_dynamic_code(phase3_js: str) -> None:
    code = phase3_js
    for forbidden in ("eval(", "new Function", "setTimeout('", 'setTimeout("'):
        assert forbidden not in code, f"the Phase 3 UI uses {forbidden}"


# ================================================================== consent


def test_the_consent_checkbox_is_not_prechecked(sources: dict[str, str]) -> None:
    """A prechecked box is not consent."""
    html = sources["index.html"]
    match = re.search(r'<input[^>]*id="consent-agree"[^>]*>', html)
    assert match, "the consent checkbox is missing"
    assert "checked" not in match.group(0), "the consent checkbox ships pre-checked"


def test_the_consent_checkbox_is_cleared_and_gates_the_button(phase3_js: str) -> None:
    """The gate moved into syncDialogButtons(); the requirement did not change.

    It used to be one inline assignment on the checkbox's change event. That was
    fragile for the same reason the revoke button broke: `once()` re-enabled every
    action button in its `finally`, so any unrelated action left "Saya setuju"
    clickable with the box unticked. The gate is now stated in one place and
    re-applied after every action.
    """
    code = phase3_js
    assert "el.consentAgree.checked = false" in code
    assert "el.consentConfirm.disabled = true" in code
    assert "el.consentAgree.addEventListener('change', syncDialogButtons)" in code
    assert "!el.consentAgree.checked" in code, "the checkbox must gate confirm"
    assert "!el.consentAgree.checked) return" in code
    # And the gate must be re-applied after any action finishes.
    assert "syncDialogButtons()" in code


def test_the_consent_dialog_shows_version_purpose_and_hash(phase3_js: str) -> None:
    code = phase3_js
    for field in ("consentBundle.version", "consentBundle.purpose", "text_sha256"):
        assert field in code, f"the consent dialog does not show {field}"
    assert "consentBundle.text" in code, "the full wording is not displayed"


def test_the_draft_and_legal_review_status_is_visible(
    sources: dict[str, str], phase3_js: str
) -> None:
    """Claiming legal compliance would be worse than an honest gap."""
    code = phase3_js
    assert "review_pending" in code
    assert "review_note" in code
    assert 'id="consent-draft-warning"' in sources["index.html"]


def test_the_revoke_dialog_explains_every_consequence(sources: dict[str, str]) -> None:
    html = sources["index.html"]
    block = html.split('id="revoke-backdrop"', 1)[1].split("</div>", 1)[0]
    lowered = block.lower()
    for phrase in ("dihapus", "unknown", "tidak</strong> otomatis", "dari awal"):
        assert phrase.lower() in lowered, f"the revoke dialog omits: {phrase}"


def test_revocation_requires_an_explicit_confirmation(phase3_js: str) -> None:
    """Still two steps, and the second one now names the participant.

    `confirmRevoke` was renamed to `submitRevoke` when the dialog gained its own
    in-flight state; the behavioural detail lives in tests/test_revoke_modal.py.
    """
    code = phase3_js
    assert "openRevokeDialog" in code
    assert "revokeConfirm.addEventListener" in code
    # It is never a single unguarded click from the table.
    assert "submitRevoke" in code
    assert "revokeTarget" in code, "the dialog must hold an identified target"


# ======================================================= model unavailability


def test_enrollment_start_ships_disabled(sources: dict[str, str]) -> None:
    html = sources["index.html"]
    match = re.search(r'<button[^>]*id="start-enrollment-btn"[^>]*>', html)
    assert match, "the start button is missing"
    assert "disabled" in match.group(0), "start must ship disabled"


def test_the_ui_surfaces_model_unavailable_honestly(
    sources: dict[str, str], phase3_js: str
) -> None:
    """Not a crash, not a silent no-op: an explanation and a next step."""
    assert 'id="model-unavailable-notice"' in sources["index.html"]
    assert "MODEL_UNAVAILABLE" in sources["index.html"]
    code = phase3_js
    assert "model.ready" in code
    assert "bukan" in code and "kerusakan aplikasi" in code, (
        "the message must say this is not an application fault"
    )
    assert "Mikrofon tidak akan dibuka" in code


def test_start_is_enabled_only_when_readiness_says_so(phase3_js: str) -> None:
    code = phase3_js
    assert "el.startEnroll.disabled = !r.can_start" in code


def test_there_is_no_fake_provider_control(sources: dict[str, str]) -> None:
    """No UI affordance may select a test double."""
    for name in ("index.html", "app.js"):
        lowered = sources[name].lower()
        for forbidden in ("fake", "test_double", "test double", "stub provider"):
            assert forbidden not in lowered, f"{name} offers {forbidden!r}"


def test_the_internal_microphone_warning_is_present(phase3_js: str) -> None:
    code = phase3_js
    assert "DEVELOPMENT_ONLY" in code
    assert "production_eligible_device" in code


# ================================================ double submit and timers


def test_every_action_runs_through_the_single_flight_guard(phase3_js: str) -> None:
    """A double-clicked button must not submit twice."""
    code = phase3_js
    assert "if (busy) return null" in code
    assert "setActionsDisabled(true)" in code
    for handler in (
        "once(saveParticipant)",
        "once(confirmConsent)",
        "once(loadParticipants)",
        "once(saveCapacity)",
    ):
        assert handler in code, f"missing single-flight guard: {handler}"


def test_the_revoke_dialog_guards_itself_rather_than_sharing_busy(
    phase3_js: str,
) -> None:
    """Deliberately NOT wrapped in once(), and that is the fix, not a regression.

    `once()` shares one `busy` flag across every action, so a dialog reading it
    cannot distinguish "another action is running" from "my own submit is running" --
    and its Batal button would go dead for reasons that have nothing to do with the
    dialog. The dialog therefore owns `revokeSubmitting`, which guards exactly one
    request. tests/test_revoke_modal.py proves the guard ordering.
    """
    code = phase3_js
    assert "once(submitRevoke)" not in code
    assert "el.revokeConfirm.addEventListener('click', submitRevoke)" in code
    assert "if (revokeSubmitting) return" in code


def test_the_poll_timer_is_cleared_when_the_window_goes_away(phase3_js: str) -> None:
    code = phase3_js
    assert "window.clearTimeout(pollTimer)" in code
    assert "pagehide" in code and "beforeunload" in code
    assert "pollStopped = true" in code


def test_polling_stops_after_a_finished_enrollment(phase3_js: str) -> None:
    """An idle panel must not keep calling the bridge forever."""
    code = phase3_js
    assert code.count("stopPolling()") >= 3


# ====================================================== storage and network


def test_no_browser_storage_is_used(phase3_js: str) -> None:
    code = phase3_js
    for forbidden in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
        assert forbidden not in code, f"the Phase 3 UI uses {forbidden}"


def test_the_token_never_appears_in_the_page(sources: dict[str, str]) -> None:
    for name in ("index.html", "app.js"):
        lowered = sources[name].lower()
        assert "x-mom-session-token" not in lowered
        assert "session_token" not in lowered
        assert "token=" not in lowered


def test_every_phase_3_call_goes_through_the_python_bridge(phase3_js: str) -> None:
    code = phase3_js
    assert "api_get" in code and "api_post" in code and "api_patch" in code
    # No bare fetch, so no request can escape the allowlist or omit the token.
    assert "fetch(" not in code, "the Phase 3 UI calls fetch directly"
    assert "XMLHttpRequest" not in code


def test_no_remote_asset_is_referenced(sources: dict[str, str]) -> None:
    for name, text in sources.items():
        for forbidden in ("http://", "https://", "//cdn", "@import url(http"):
            # Allow the CSP and generator comments in the HTML head only.
            occurrences = [
                line
                for line in text.splitlines()
                if forbidden in line and "Content-Security-Policy" not in line
            ]
            assert occurrences == [], f"{name} references {forbidden}: {occurrences[:2]}"


# ============================================================ markup wiring


def test_the_participants_card_is_enabled_and_opens_the_panel(
    sources: dict[str, str],
) -> None:
    html = sources["index.html"]
    # Match the whole opening tag: the class attribute precedes the id, so slicing
    # after the id would look past what is being asserted.
    match = re.search(r'<article[^>]*id="card-participants"[^>]*>', html)
    assert match, "the participants card is missing"
    assert "feature-card-enabled" in match.group(0), match.group(0)
    assert 'aria-disabled="true"' not in match.group(0), "the card is still disabled"
    assert 'id="open-participants-btn"' in html
    assert 'id="participants-panel"' in html


def test_every_element_the_script_looks_up_exists_in_the_markup(
    sources: dict[str, str], phase3_js: str
) -> None:
    """A typo in an id is a silently dead control."""
    html = sources["index.html"]
    ids = set(re.findall(r"getElementById\('([^']+)'\)", phase3_js))
    assert len(ids) > 25, f"only {len(ids)} ids found; the block may be truncated"
    missing = [name for name in ids if f'id="{name}"' not in html]
    assert missing == [], f"the script looks up ids that do not exist: {missing}"


def test_the_participant_table_declares_the_expected_columns(
    sources: dict[str, str],
) -> None:
    html = sources["index.html"]
    block = html.split('id="participant-table"', 1)[1].split("</thead>", 1)[0]
    for column in ("Nama", "Peran", "Status", "Consent", "Voiceprint", "Tindakan"):
        assert f">{column}<" in block, f"the table has no {column} column"


def test_the_panel_explains_the_privacy_posture(sources: dict[str, str]) -> None:
    html = sources["index.html"]
    block = html.split('id="participants-panel"', 1)[1]
    assert "terenkripsi" in block
    assert "DPAPI" in block
    assert "tidak pernah ditulis ke disk" in block


def test_uuid_is_the_identity_not_the_display_name(phase3_js: str) -> None:
    """A name must never become a DOM id or a path component."""
    code = phase3_js
    assert "entry.uuid" in code
    assert "selected.uuid" in code
    # No id is ever built from a name.
    assert "id = entry.display_name" not in code
    assert "getElementById(entry" not in code


def test_the_phase_3_css_does_not_override_the_offline_badge(
    sources: dict[str, str],
) -> None:
    """The Phase 3 badge is `.state-badge`; `.badge` belongs to the topbar."""
    css = sources["app.css"]
    assert ".state-badge" in css
    # Exactly one top-level `.badge {` rule, the original one.
    assert len(re.findall(r"^\.badge \{", css, flags=re.M)) == 1
