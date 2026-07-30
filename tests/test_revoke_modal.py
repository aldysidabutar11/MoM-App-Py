"""Regression tests for the revoke-consent dialog.

**Why these tests look the way they do.** The bug being fixed here was invisible to
substring assertions: every handler *was* attached, every id *did* match, and the
markup *did* contain the right buttons. The dialog was dead because of a CSS cascade
defect -- `.modal-backdrop { display: flex }` overrode the UA rule
`[hidden] { display: none }`, so `show(node, false)` never hid anything, the revoke
backdrop covered the viewport from page load, and the dialog was "open" without ever
having been opened. A test that greps for `addEventListener('click'` would have
passed throughout.

So the tests below assert *structure and semantics*, computed from the shipped files:
the cascade relationship, which functions contain which guards, what a handler is
wired to, and which statements can reach a network call. Where a property genuinely
needs a live DOM -- pixel hit-testing, real focus movement -- it is called out in the
module and covered by the manual acceptance steps in
``docs/phase-3-participants-enrollment.md`` instead of being faked here.

No browser or JS runtime is added: adding one to test one dialog would be a
disproportionate dependency for an offline desktop product.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid as uuid_module
from pathlib import Path

import pytest

from mom_igd.api.app import WEB_DIR
from mom_igd.config import AppConfig
from mom_igd.db import initialize_database
from mom_igd.db.connection import connect
from mom_igd.enrollment.consent import ConfirmationMethod, ConsentService
from mom_igd.enrollment.participants import ParticipantService

# ===========================================================================
# helpers: minimal, purpose-built parsers over the shipped assets
# ===========================================================================


@pytest.fixture(scope="module")
def html() -> str:
    return (Path(WEB_DIR) / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return (Path(WEB_DIR) / "app.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js() -> str:
    return (Path(WEB_DIR) / "app.js").read_text(encoding="utf-8")


def _strip_css_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def _strip_js_comments(text: str) -> str:
    """Drop /* */ and // comments.

    The script documents its own prohibitions, so a search for a banned token finds
    the sentence that forbids it. Left unhandled, that false positive is what gets a
    genuine check weakened or deleted.
    """
    without_block = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)//.*$", "", without_block)


def _css_rules(text: str) -> list[tuple[str, str]]:
    """Selector/body pairs, brace-depth aware.

    A naive ``[^{}]+\\{[^{}]*\\}`` scan desynchronises on ``@media (...) { ... }``
    and silently attributes declarations to the wrong selector -- which is how the
    original diagnosis first missed `.modal-backdrop`.
    """
    out: list[tuple[str, str]] = []
    stack: list[str] = []
    start = 0
    for index, char in enumerate(text):
        if char == "{":
            stack.append(text[start:index].strip())
            start = index + 1
        elif char == "}":
            body = text[start:index]
            head = stack.pop() if stack else ""
            if "{" not in body:
                out.append((head, body))
            start = index + 1
    return out


def _display_declarations(css_text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for selector, body in _css_rules(_strip_css_comments(css_text)):
        match = re.search(r"(?<![\w-])display\s*:\s*([^;!}]+)(!important)?", body)
        if match and not selector.startswith("@"):
            for part in selector.split(","):
                part = part.strip()
                if part:
                    found[part] = match.group(1).strip() + (
                        " !important" if match.group(2) else ""
                    )
    return found


def _js_function(js_text: str, name: str) -> str:
    """Return one function body by brace matching.

    Substring windows (``js[start:start+400]``) were the other thing that made the
    original tests weak: the window either clipped the assertion target or ran past
    it into an unrelated function.
    """
    pattern = re.compile(
        r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{"
    )
    match = pattern.search(js_text)
    assert match is not None, f"function {name}() is not defined in app.js"
    depth = 0
    for index in range(match.end() - 1, len(js_text)):
        if js_text[index] == "{":
            depth += 1
        elif js_text[index] == "}":
            depth -= 1
            if depth == 0:
                return js_text[match.end() : index]
    raise AssertionError(f"unbalanced braces in {name}()")


def _listener_for(js_text: str, element: str, event: str = "click") -> str:
    """The handler expression registered for ``el.<element>.addEventListener``."""
    pattern = re.compile(
        r"el\." + re.escape(element) + r"\.addEventListener\(\s*'" + event + r"'\s*,\s*"
    )
    match = pattern.search(js_text)
    assert match is not None, f"no {event} listener is attached to el.{element}"
    depth = 0
    for index in range(match.end(), len(js_text)):
        char = js_text[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                return js_text[match.end() : index].strip()
            depth -= 1
    raise AssertionError(f"unterminated listener for el.{element}")


# ===========================================================================
# 1. the root cause: `hidden` must beat every author `display`
# ===========================================================================


def test_an_important_hidden_rule_exists(css: str) -> None:
    """The single rule the whole dialog depended on.

    Without it, `hidden` is decorative on any element whose class sets `display`,
    and `!important` is what stops the next added `display` rule from silently
    reintroducing the bug through source order.
    """
    declarations = _display_declarations(css)
    assert "[hidden]" in declarations, (
        "app.css must declare `[hidden] { display: none !important }`. The HTML "
        "`hidden` attribute relies on a UA-stylesheet rule that any author "
        "`display` declaration overrides."
    )
    assert declarations["[hidden]"] == "none !important", declarations["[hidden]"]


def test_no_element_that_javascript_hides_can_be_stuck_visible(
    html: str, css: str, js: str
) -> None:
    """Computed from the real files, for every id the script toggles."""
    declarations = _display_declarations(css)
    hidden_rule = declarations.get("[hidden]", "")
    display_classes = {
        selector.lstrip(".")
        for selector in declarations
        if selector.startswith(".") and " " not in selector
    }

    var_to_id = dict(
        re.findall(r"(\w+)\s*:\s*document\.getElementById\('([^']+)'\)", js)
    )
    toggled = {
        var_to_id[var]
        for var in set(re.findall(r"show\(el\.(\w+)\s*,", js))
        | set(re.findall(r"el\.(\w+)\.hidden\s*=", js))
        if var in var_to_id
    }
    assert len(toggled) >= 10, f"the parser found too few toggled ids: {toggled}"

    stuck: list[str] = []
    for element_id in sorted(toggled):
        match = re.search(
            r'<\w+([^>]*\bid="' + re.escape(element_id) + r'"[^>]*)>', html
        )
        if match is None:
            continue
        classes = re.search(r'class="([^"]*)"', match.group(1))
        offenders = [
            name
            for name in (classes.group(1).split() if classes else [])
            if name in display_classes
        ]
        if offenders and "!important" not in hidden_rule:
            stuck.append(f"#{element_id} (.{offenders[0]})")

    assert stuck == [], (
        "these elements set `display` through a class, so the `hidden` attribute "
        f"cannot hide them: {stuck}"
    )


def test_the_cascade_check_actually_detects_the_original_defect(
    html: str, css: str, js: str
) -> None:
    """Negative control: the test above must fail on the code that shipped broken.

    Without this, a parser that silently found nothing would make the check above
    pass forever. Here the `[hidden]` rule is removed from the stylesheet text and
    the same analysis is re-run; it must report the backdrops as stuck.
    """
    without_rule = re.sub(
        r"\[hidden\]\s*\{[^}]*\}", "", _strip_css_comments(css), count=1
    )
    declarations = _display_declarations(without_rule)
    assert "[hidden]" not in declarations, "the control failed to remove the rule"

    display_classes = {
        selector.lstrip(".")
        for selector in declarations
        if selector.startswith(".") and " " not in selector
    }
    assert "modal-backdrop" in display_classes, (
        ".modal-backdrop must still declare `display`, otherwise this control "
        "proves nothing"
    )
    for element_id in ("revoke-backdrop", "consent-backdrop"):
        match = re.search(
            r'<\w+([^>]*\bid="' + re.escape(element_id) + r'"[^>]*)>', html
        )
        assert match is not None
        assert "modal-backdrop" in match.group(1)


def test_the_revoke_backdrop_covers_the_viewport_and_sits_above_the_page(
    css: str,
) -> None:
    """When it IS shown, nothing may be clickable behind it."""
    rules = dict(_css_rules(_strip_css_comments(css)))
    backdrop = rules.get(".modal-backdrop", "")
    assert "position: fixed" in backdrop
    assert "inset: 0" in backdrop
    z_index = re.search(r"z-index:\s*(\d+)", backdrop)
    assert z_index is not None, ".modal-backdrop must declare a z-index"
    others = [
        int(m.group(1))
        for selector, body in _css_rules(_strip_css_comments(css))
        if selector != ".modal-backdrop"
        for m in [re.search(r"z-index:\s*(\d+)", body)]
        if m
    ]
    assert int(z_index.group(1)) > max(others), (
        f"the modal backdrop's z-index {z_index.group(1)} must exceed every other "
        f"stacking value {sorted(others)}, or the page shows through it"
    )


def test_no_pointer_events_rule_can_neutralise_the_dialog(css: str) -> None:
    """`pointer-events: none` on a modal ancestor makes every button inert."""
    for selector, body in _css_rules(_strip_css_comments(css)):
        if "pointer-events" not in body:
            continue
        value = re.search(r"pointer-events\s*:\s*([\w-]+)", body)
        if value and value.group(1) == "none":
            assert "modal" not in selector, (
                f"{selector} disables pointer events on the dialog: {body.strip()}"
            )


# ===========================================================================
# 2. the buttons are wired, and wired to something that exists
# ===========================================================================


def test_both_dialog_buttons_have_a_click_handler(js: str) -> None:
    confirm = _listener_for(js, "revokeConfirm")
    cancel = _listener_for(js, "revokeCancel")
    assert confirm, "the confirm button has an empty handler"
    assert cancel, "the cancel button has an empty handler"


def test_the_confirm_handler_resolves_to_a_defined_function(js: str) -> None:
    handler = _listener_for(js, "revokeConfirm")
    assert handler == "submitRevoke", (
        f"expected the confirm button to call submitRevoke, found {handler!r}"
    )
    assert _js_function(js, "submitRevoke").strip(), "submitRevoke() is empty"


def test_the_cancel_handler_resolves_to_a_defined_function(js: str) -> None:
    handler = _listener_for(js, "revokeCancel")
    assert handler == "closeRevokeDialog", handler
    assert _js_function(js, "closeRevokeDialog").strip()


def test_the_confirm_button_is_not_re_enabled_by_the_global_action_guard(
    js: str,
) -> None:
    """The specific mechanism that could make it clickable with nothing selected.

    `setActionsDisabled(false)` runs in once()'s finally. If the dialog's confirm
    button were in that list it would be enabled regardless of whether a
    participant is selected.
    """
    body = _js_function(js, "setActionsDisabled")
    for forbidden in ("revokeConfirm", "consentConfirm", "revokeCancel"):
        assert forbidden not in body, (
            f"el.{forbidden} must not be in setActionsDisabled(): a blanket "
            "disabled=false there overrides the dialog's own precondition"
        )


def test_dialog_button_state_is_reapplied_after_any_action_completes(js: str) -> None:
    once_body = _js_function(js, "once")
    assert "syncDialogButtons()" in once_body, (
        "once() must restore dialog button state, or an unrelated action leaves the "
        "dialog's buttons in whatever state it found them"
    )
    sync = _js_function(js, "syncDialogButtons")
    assert "revokeTarget" in sync and "revokeSubmitting" in sync


# ===========================================================================
# 3. identity is displayed, and the UUID is what gets sent
# ===========================================================================


def test_the_dialog_has_separate_name_and_role_elements(html: str) -> None:
    assert 'id="revoke-who-name"' in html
    assert 'id="revoke-who-role"' in html


def test_the_dialog_states_whose_consent_is_being_revoked(html: str) -> None:
    section = html[html.index('id="revoke-backdrop"') :]
    section = section[: section.index("</div>", section.index("revoke-error"))]
    assert "Anda akan mencabut persetujuan milik" in section


def test_name_and_role_are_rendered_as_text_not_markup(js: str) -> None:
    body = _js_function(js, "openRevokeDialog")
    assert "revokeWhoName.textContent" in body
    assert "revokeWhoRole.textContent" in body
    assert "innerHTML" not in body


def test_the_dialog_stores_a_uuid_and_never_acts_on_a_name(js: str) -> None:
    opener = _js_function(js, "openRevokeDialog")
    assert "revokeTarget = {" in opener
    assert "uuid: uuid" in opener

    submit = _js_function(js, "submitRevoke")
    assert "target.uuid" in submit, "the request path must be built from the UUID"
    assert "display_name" not in submit, "a display name must never address the API"
    assert ".name" not in submit.replace("target.name", ""), (
        "nothing but the UUID may identify the participant in the request"
    )


def test_an_unidentifiable_participant_disables_confirm_and_explains(js: str) -> None:
    opener = _js_function(js, "openRevokeDialog")
    assert "UUID_RE.test(uuid)" in opener, (
        "the opener must validate the UUID rather than trusting the selection"
    )
    assert "revokeTarget = null" in opener
    assert "revokeError(" in opener, "an invalid target must show a safe message"

    sync = _js_function(js, "syncDialogButtons")
    assert "!revokeTarget" in sync, (
        "confirm must be disabled whenever there is no valid target"
    )


def test_the_uuid_pattern_is_a_canonical_lower_case_uuid(js: str) -> None:
    match = re.search(r"var UUID_RE\s*=\s*\n?\s*(/[^\n]+/)\s*;", js)
    assert match is not None, "UUID_RE must be declared"
    pattern = match.group(1)
    assert pattern.startswith("/^") and pattern.endswith("$/")
    assert "A-F" not in pattern, "an upper-case UUID must not be accepted"


def test_the_confirm_button_starts_disabled_in_the_markup(html: str) -> None:
    """Before any participant has been selected there is nothing to confirm."""
    match = re.search(r'<button[^>]*id="revoke-confirm-btn"[^>]*>', html)
    assert match is not None
    assert "disabled" in match.group(0), match.group(0)


# ===========================================================================
# 4. cancel, double submit, escape, focus
# ===========================================================================


def test_cancel_sends_no_request(js: str) -> None:
    body = _js_function(js, "closeRevokeDialog")
    for call in ("httpPost", "httpGet", "httpPatch", "httpDelete", "api_"):
        assert call not in body, f"closeRevokeDialog() must not call {call}"


def test_cancel_is_usable_before_any_request_has_been_made(js: str) -> None:
    """It may only be disabled while a submit is actually in flight."""
    sync = _js_function(js, "syncDialogButtons")
    match = re.search(r"revokeCancel\.disabled\s*=\s*([^;]+);", sync)
    assert match is not None, "cancel's disabled state must be set explicitly"
    assert match.group(1).strip() == "revokeSubmitting", match.group(1)


def test_a_second_click_cannot_send_a_second_request(js: str) -> None:
    body = _js_function(js, "submitRevoke")
    assert re.search(r"if\s*\(\s*revokeSubmitting\s*\)\s*return", body), (
        "submitRevoke() needs an in-flight guard as its first action"
    )
    guard_at = body.index("revokeSubmitting")
    set_at = body.index("revokeSubmitting = true")
    await_at = body.index("await httpPost")
    assert guard_at < set_at < await_at, (
        "the flag must be set before the await, or two clicks both pass the guard"
    )


def test_the_in_flight_flag_is_always_cleared(js: str) -> None:
    body = _js_function(js, "submitRevoke")
    assert "finally" in body, (
        "without a finally, a thrown error leaves the dialog permanently stuck"
    )
    tail = body[body.index("finally") :]
    assert "revokeSubmitting = false" in tail
    assert "syncDialogButtons()" in tail


def test_the_dialog_cannot_be_closed_while_submitting(js: str) -> None:
    body = _js_function(js, "closeRevokeDialog")
    assert re.search(r"if\s*\(\s*revokeSubmitting\s*\)\s*return", body)


def test_escape_closes_the_dialog_but_not_mid_submit(js: str) -> None:
    match = re.search(
        r"document\.addEventListener\('keydown',\s*function\s*\([^)]*\)\s*\{", js
    )
    assert match is not None, "no keydown listener is registered"
    depth, body = 0, ""
    for index in range(match.end() - 1, len(js)):
        if js[index] == "{":
            depth += 1
        elif js[index] == "}":
            depth -= 1
            if depth == 0:
                body = js[match.end() : index]
                break
    assert "'Escape'" in body
    assert "revokeBackdrop.hidden" in body, (
        "Escape must only act on a dialog that is actually open"
    )
    assert "!revokeSubmitting" in body
    assert "closeRevokeDialog()" in body


def test_focus_enters_the_dialog_and_returns_to_the_trigger(js: str) -> None:
    opener = _js_function(js, "openRevokeDialog")
    assert ".focus()" in opener, "focus must move into the dialog when it opens"
    assert "revokeCancel.focus()" in opener, (
        "focus should land on Batal, not on the destructive button"
    )
    assert "revokeReturnFocus = trigger" in opener

    closer = _js_function(js, "closeRevokeDialog")
    assert "revokeReturnFocus" in closer and ".focus()" in closer, (
        "focus must be returned to whatever opened the dialog"
    )
    assert "revokeReturnFocus = null" in closer, "the stored trigger must be released"


def test_the_trigger_passes_itself_so_focus_can_be_restored(js: str) -> None:
    handler = _listener_for(js, "vpRevoke")
    assert "openRevokeDialog(selected, event.currentTarget)" in handler, handler


def test_a_click_on_the_backdrop_does_not_reach_the_page_behind(js: str) -> None:
    match = re.search(
        r"el\.revokeBackdrop\.addEventListener\('click',\s*function\s*\([^)]*\)\s*\{"
        r"(?P<body>[^}]*)\}",
        js,
    )
    assert match is not None, "the backdrop must intercept its own clicks"
    assert "stopPropagation" in match.group("body")


# ===========================================================================
# 5. success, failure, and the missing-voiceprint case, in the UI copy
# ===========================================================================


def test_success_closes_the_dialog_and_refreshes_the_rows(js: str) -> None:
    body = _js_function(js, "submitRevoke")
    assert "closeRevokeDialog()" in body
    assert "loadParticipants()" in body, "the participant row must be refreshed"
    assert "refreshReadiness()" in body, "enrollment readiness must be refreshed"
    assert "vpResult.textContent" in body, "the operator needs success feedback"


def test_a_missing_voiceprint_is_reported_as_success(js: str) -> None:
    body = _js_function(js, "submitRevoke")
    assert "belum memiliki template suara" in body, (
        "revoking for somebody with no voiceprint must say so plainly rather than "
        "looking like a failure"
    )
    # It must be a branch on the deletion count, not an error path.
    assert "deleted > 0" in body or "deleted.length" in body


def test_a_failure_keeps_the_dialog_open_and_restores_the_buttons(js: str) -> None:
    body = _js_function(js, "submitRevoke")
    failure = body[body.index("if (!envelope.ok)") :]
    assert "revokeError(" in failure
    assert "return" in failure
    assert "closeRevokeDialog" not in failure.split("return")[0], (
        "a failed revoke must not close the dialog"
    )


def test_the_deletion_wording_is_conditional_not_absolute(html: str) -> None:
    """The requirement is explicit: a template is deleted *if one exists*."""
    section = html[html.index('id="revoke-backdrop"') :]
    section = section[: section.index("revoke-confirm-btn")]
    assert "jika sudah tersedia" in section, (
        "the consequence list must not assert that a template is deleted "
        "unconditionally"
    )


def test_the_dialog_still_states_the_unknown_and_retention_consequences(
    html: str,
) -> None:
    section = html[html.index('id="revoke-backdrop"') :]
    section = section[: section.index("revoke-confirm-btn")]
    assert "UNKNOWN" in section
    assert "tidak</strong> otomatis" in section or "tidak otomatis" in section


# ===========================================================================
# 6. nothing unsafe was introduced
# ===========================================================================


def test_the_new_code_uses_no_browser_audio_api(js: str) -> None:
    for name in ("getUserMedia", "AudioContext", "MediaRecorder", "mediaDevices"):
        for function_name in ("openRevokeDialog", "submitRevoke", "closeRevokeDialog",
                              "saveCapacity", "loadRoster", "loadMeetings"):
            assert name not in _js_function(js, function_name), (
                f"{function_name}() must not touch {name}"
            )


def test_no_participant_data_is_put_in_browser_storage(js: str) -> None:
    for function_name in ("openRevokeDialog", "submitRevoke", "closeRevokeDialog"):
        body = _js_function(js, function_name)
        for banned in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
            assert banned not in body, f"{function_name}() must not use {banned}"


def test_no_token_appears_anywhere_in_the_dialog_code(js: str) -> None:
    """Comments are stripped first.

    These functions document what they must not leak -- "no traceback, path, token
    or key" -- and a naive search flags the sentence forbidding the thing as an
    instance of the thing. That false positive is how a real check gets deleted.
    """
    for function_name in ("openRevokeDialog", "submitRevoke", "closeRevokeDialog"):
        body = _strip_js_comments(_js_function(js, function_name)).lower()
        assert "token" not in body, f"{function_name}() mentions a token in code"
        assert "?token=" not in body


def test_the_revoke_endpoint_is_reachable_only_through_the_exact_allowlist() -> None:
    from mom_igd.shell.launcher import (
        ALLOWED_POST_PATHS,
        ALLOWED_POST_PATTERNS,
        ALLOWED_PROXY_PATHS,
        _permitted,
    )

    good = (
        "/enrollment/participants/0189d3f1-1c2e-4a5b-8c7d-9e0f1a2b3c4d/consent/revoke"
    )
    assert _permitted(good, ALLOWED_POST_PATHS, ALLOWED_POST_PATTERNS) is True
    # Not a GET, and not reachable with a malformed or upper-case UUID.
    assert _permitted(good, ALLOWED_PROXY_PATHS, ()) is False
    for bad in (
        "/enrollment/participants/x/consent/revoke",
        "/enrollment/participants/0189D3F1-1C2E-4A5B-8C7D-9E0F1A2B3C4D/consent/revoke",
        "/enrollment/participants//consent/revoke",
        good + "?token=abc",
    ):
        assert _permitted(bad, ALLOWED_POST_PATHS, ALLOWED_POST_PATTERNS) is False, bad


# ===========================================================================
# 7. what the endpoint actually does -- real service, real database
# ===========================================================================


@pytest.fixture
def db_path(config: AppConfig, paths) -> Path:
    initialize_database(
        paths.database_path(config.database.filename),
        busy_timeout_ms=config.database.busy_timeout_ms,
        app_version=config.app_version,
    )
    return paths.database_path(config.database.filename)


@pytest.fixture
def factory(db_path: Path, config: AppConfig):
    def _connect() -> sqlite3.Connection:
        return connect(db_path, busy_timeout_ms=config.database.busy_timeout_ms)

    return _connect


@pytest.fixture
def people(factory, config: AppConfig) -> ParticipantService:
    return ParticipantService(factory, config=config)


@pytest.fixture
def consent(factory) -> ConsentService:
    return ConsentService(factory)


def _pid(factory, participant_uuid: str) -> int:
    conn = factory()
    try:
        return int(
            conn.execute(
                "SELECT id FROM participants WHERE uuid = ?", (participant_uuid,)
            ).fetchone()["id"]
        )
    finally:
        conn.close()


def _granted(people, consent, factory, name: str = "Budi"):
    person = people.create(display_name=name, role="Notulis")
    consent.grant(
        _pid(factory, person.uuid),
        confirmation_method=ConfirmationMethod.PARTICIPANT_CONFIRMED_ON_DEVICE,
    )
    return person


def test_revoking_without_a_voiceprint_succeeds(people, consent, factory) -> None:
    """The exact case the UI must not present as a server error."""
    person = _granted(people, consent, factory)
    result = consent.revoke(_pid(factory, person.uuid), reason="uji")
    assert result["already_revoked"] is False
    assert result["event_uuid"]

    conn = factory()
    try:
        state = consent.state(conn, _pid(factory, person.uuid))
        assert state.action.value == "REVOKED"
        assert state.enrollment_allowed is False
        # No voiceprint ever existed, and none was invented.
        assert (
            conn.execute("SELECT count(*) AS n FROM voiceprints").fetchone()["n"] == 0
        )
    finally:
        conn.close()


def test_revoking_appends_an_event_and_updates_nothing(people, consent, factory) -> None:
    person = _granted(people, consent, factory)
    participant_id = _pid(factory, person.uuid)
    consent.revoke(participant_id, reason="uji")
    conn = factory()
    try:
        rows = conn.execute(
            "SELECT action FROM consent_events WHERE participant_id = ? ORDER BY id",
            (participant_id,),
        ).fetchall()
    finally:
        conn.close()
    assert [r["action"] for r in rows] == ["GRANTED", "REVOKED"], (
        "consent is append-only: the grant must survive alongside the revocation"
    )


def test_revoking_does_not_delete_the_participant(people, consent, factory) -> None:
    person = _granted(people, consent, factory)
    consent.revoke(_pid(factory, person.uuid))
    still_there = people.get(person.uuid)
    assert still_there.display_name == "Budi"
    assert still_there.is_active is True, "revoking consent is not deactivation"


def test_revoking_does_not_touch_meetings_or_recordings(
    people, consent, factory
) -> None:
    person = _granted(people, consent, factory)
    meeting_uuid = str(uuid_module.uuid4())
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO meetings (title, uuid) VALUES ('Rapat lama', ?)",
            (meeting_uuid,),
        )
        conn.commit()
    finally:
        conn.close()
    people.add_to_meeting(meeting_uuid, person.uuid)

    consent.revoke(_pid(factory, person.uuid))

    conn = factory()
    try:
        assert conn.execute("SELECT count(*) AS n FROM meetings").fetchone()["n"] == 1
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM meeting_participants"
            ).fetchone()["n"]
            == 1
        ), "roster history must survive a consent revocation"
    finally:
        conn.close()


def test_revoking_twice_is_idempotent_and_appends_one_event(
    people, consent, factory
) -> None:
    """A double click must not stack two revocation events.

    The service reports `already_revoked` rather than raising, which is the right
    call for a destructive action: the operator's intent is already satisfied, so
    an error would only invite them to retry something that has happened.
    """
    person = _granted(people, consent, factory)
    participant_id = _pid(factory, person.uuid)
    first = consent.revoke(participant_id)
    second = consent.revoke(participant_id)
    assert first["already_revoked"] is False
    assert second["already_revoked"] is True
    assert second["event_id"] == first["event_id"], "no new event may be appended"

    conn = factory()
    try:
        actions = [
            r["action"]
            for r in conn.execute(
                "SELECT action FROM consent_events WHERE participant_id = ? "
                "ORDER BY id",
                (participant_id,),
            )
        ]
    finally:
        conn.close()
    assert actions == ["GRANTED", "REVOKED"], actions


def test_revoking_someone_who_never_consented_is_refused(people, consent, factory) -> None:
    from mom_igd.enrollment.consent import ConsentError

    person = people.create(display_name="Belum pernah setuju")
    with pytest.raises(ConsentError, match="no consent to revoke"):
        consent.revoke(_pid(factory, person.uuid))


def test_a_revocation_is_audited_without_biometric_detail(
    people, consent, factory
) -> None:
    person = _granted(people, consent, factory)
    consent.revoke(_pid(factory, person.uuid), reason="uji")
    conn = factory()
    try:
        rows = [
            json.loads(r["detail_json"] or "{}")
            for r in conn.execute(
                "SELECT detail_json FROM audit_events "
                "WHERE action LIKE 'CONSENT%'"
            )
        ]
    finally:
        conn.close()
    assert rows, "a revocation must be audited"
    blob = json.dumps(rows)
    for banned in ("embedding", "vector", "payload", "plaintext", "nonce", "cipher"):
        assert banned not in blob, f"the audit detail leaks {banned}"
