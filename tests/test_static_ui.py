"""Static desktop-shell assets: local only, no framework, no leakage.

Covers Phase 1 test category 30.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mom_igd.api.app import WEB_DIR
from mom_igd.shell.launcher import ALLOWED_PROXY_PATHS, ShellApi, manual_launch_command
from mom_igd.security import SessionToken

ASSETS = ("index.html", "app.css", "app.js")


@pytest.fixture(scope="module")
def sources() -> dict[str, str]:
    return {name: (WEB_DIR / name).read_text(encoding="utf-8") for name in ASSETS}


def _css_block(css: str, opener: str) -> str:
    """The body of the rule starting with `opener`, ending at its matching brace.

    Slicing to the end of the file was the earlier shape, and it made every rule added
    afterwards part of whatever block was being examined. A block ends where its brace
    closes.
    """
    start = css.index(opener)
    depth = 0
    for position in range(css.index("{", start), len(css)):
        if css[position] == "{":
            depth += 1
        elif css[position] == "}":
            depth -= 1
            if depth == 0:
                return css[start : position + 1]
    raise AssertionError(f"unbalanced braces after {opener!r}")


def test_the_expected_assets_exist_and_nothing_else() -> None:
    assert WEB_DIR.is_dir()
    present = {p.name for p in WEB_DIR.iterdir() if p.is_file()}
    assert present == set(ASSETS)


# ------------------------------------------ 30. no remote asset of any kind


@pytest.mark.parametrize("name", ASSETS)
def test_no_absolute_remote_url_appears_in_an_asset(sources: dict[str, str], name: str) -> None:
    text = sources[name]
    for pattern in ("http://", "https://", "//cdn", "//unpkg", "//fonts.", "//ajax."):
        assert pattern not in text, f"{name} references a remote resource: {pattern}"


@pytest.mark.parametrize("name", ASSETS)
def test_no_protocol_relative_or_external_reference(sources: dict[str, str], name: str) -> None:
    remote_ref = re.compile(
        r"""(?:src|href)\s*=\s*["']\s*(?:https?:)?//|@import\s+url\(\s*["']?\s*(?:https?:)?//""",
        re.IGNORECASE,
    )
    assert remote_ref.findall(sources[name]) == []


def test_html_only_references_local_sibling_assets(sources: dict[str, str]) -> None:
    html = sources["index.html"]
    references = re.findall(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", html)
    for reference in references:
        if reference.startswith("#"):
            continue
        assert not reference.startswith(("http", "//")), reference
        assert "/" not in reference.strip("/"), f"{reference} is not a sibling asset"
        assert (WEB_DIR / reference).is_file(), f"{reference} does not exist locally"


def test_no_remote_font_is_used(sources: dict[str, str]) -> None:
    css = sources["app.css"]
    assert "@font-face" not in css, "only system fonts may be used"
    assert "fonts.googleapis" not in css
    assert "-apple-system" in css and "Segoe UI" in css


def test_no_frontend_framework_or_build_pipeline(sources: dict[str, str]) -> None:
    """No framework, no bundler, no npm.

    Matched on word boundaries rather than as bare substrings. A plain `in` check
    produced real false positives once the Phase 3 UI arrived: "re**act**ivate"
    contains "react", and "in**vite**d" contains "vite". Those are English words, not
    dependencies, and a check that cannot tell the difference gets weakened or
    deleted the first time it cries wolf.
    """
    import re as _re

    combined = " ".join(sources.values()).lower()
    for banned in (
        "react",
        "vue.js",
        "svelte",
        "angular",
        "jquery",
        "webpack",
        "vite",
        "electron",
    ):
        pattern = r"(?<![a-z])" + _re.escape(banned) + r"(?![a-z])"
        assert not _re.search(pattern, combined), (
            f"the shell must stay framework-free ({banned})"
        )
    # These are unambiguous and need no boundary handling.
    for banned in ("require(", "from 'react'", "import react"):
        assert banned not in combined, f"the shell must stay framework-free ({banned})"


def test_no_npm_manifest_anywhere_in_the_repository() -> None:
    repo = Path(__file__).resolve().parent.parent
    skip = {".git", ".venv", "__pycache__"}
    for name in ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"):
        found = [
            str(p.relative_to(repo))
            for p in repo.rglob(name)
            if not any(part in skip for part in p.parts)
        ]
        assert found == [], f"{name} found: {found}"


def test_content_security_policy_forbids_external_origins(sources: dict[str, str]) -> None:
    html = sources["index.html"]
    assert "Content-Security-Policy" in html
    policy = re.search(r'content="([^"]*default-src[^"]*)"', html)
    assert policy is not None
    directives = policy.group(1)
    for directive in ("default-src 'self'", "script-src 'self'", "style-src 'self'"):
        assert directive in directives
    assert "frame-ancestors 'none'" in directives
    assert "*" not in directives.replace("data:", ""), "no wildcard origin may be allowed"


# --------------------------------------------------- token must not reach JS


def test_the_script_never_uses_persistent_browser_storage(sources: dict[str, str]) -> None:
    script = sources["app.js"]
    for banned in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
        # Only the explanatory comment may mention these names; no call may exist.
        assert f"{banned}." not in script and f"{banned}[" not in script, banned
        assert f"{banned}.setItem" not in script


def test_no_asset_contains_a_hardcoded_credential(sources: dict[str, str]) -> None:
    for name, text in sources.items():
        lowered = text.lower()
        for banned in ("x-mom-session-token", "bearer ", "api_key=", "password"):
            assert banned not in lowered, f"{name} contains {banned!r}"


def test_the_script_never_puts_a_credential_in_a_url(sources: dict[str, str]) -> None:
    script = sources["app.js"]
    assert "?token=" not in script
    assert "session_token=" not in script


def test_protected_data_is_fetched_through_the_python_bridge(sources: dict[str, str]) -> None:
    script = sources["app.js"]
    # The page asks Python to make the authenticated call; it never holds a token.
    assert "pywebview.api.api_get" in script
    assert "getPublic('/health')" in script
    assert "getProtected('/doctor')" in script


def test_the_shell_proxy_allowlist_is_closed() -> None:
    """The page may reach exactly these paths through the token-bearing proxy.

    Phase 2 added the read-only capture endpoints; Phase 3 added the read-only
    participant, consent and enrollment ones. The list stays explicit rather than
    becoming a prefix rule, so a new endpoint is never reachable from the page by
    accident -- including one added later by someone not thinking about the shell.
    """
    assert ALLOWED_PROXY_PATHS == frozenset(
        {
            "/health",
            "/version",
            "/doctor",
            "/internal/ready",
            # Phase 2
            "/audio/devices",
            "/audio/preflight",
            "/audio/recordings/status",
            "/audio/quality",
            "/audio/live",
            "/audio/level",
            "/audio/recovery/pending",
            # Phase 3
            "/enrollment/participants",
            "/enrollment/consent/text",
            "/enrollment/sessions/current",
            "/enrollment/cleanup/pending",
            # Phase 3 corrective: the roster panel needs to know which meetings
            # exist before it can address one. Read-only, bounded, and it returns
            # meeting UUIDs rather than internal row ids.
            "/enrollment/meetings",
            # Phase 4: all four read-only, and none loads a model or can cause a
            # download. `/asr/models` reads the readiness index and `/asr/preflight`
            # every other precondition, so the page can disable the button rather than
            # letting the run fail; `/asr/recordings` is what the operator picks from
            # instead of typing a UUID.
            "/asr/status",
            "/asr/models",
            "/asr/recordings",
            "/asr/preflight",
            # Minutes, read-only. Both read the readiness index; neither can
            # cause a download, and `/mom/transcripts` is what the operator
            # picks from instead of typing a UUID.
            "/mom/status",
            "/mom/transcripts",
        }
    )
    assert "/openapi.json" not in ALLOWED_PROXY_PATHS
    assert "/docs" not in ALLOWED_PROXY_PATHS


def test_the_templated_allowlists_are_bounded_not_wildcards() -> None:
    """A prefix rule like `/enrollment/*` would expose every future route.

    Each template is anchored at both ends and each variable segment must be a
    canonical lower-case UUID, so the set of reachable shapes is enumerable.
    """
    from mom_igd.shell.launcher import (
        ALLOWED_DELETE_PATTERNS,
        ALLOWED_GET_PATTERNS,
        ALLOWED_PATCH_PATTERNS,
        ALLOWED_POST_PATTERNS,
    )

    every = (
        ALLOWED_GET_PATTERNS
        + ALLOWED_POST_PATTERNS
        + ALLOWED_PATCH_PATTERNS
        + ALLOWED_DELETE_PATTERNS
    )
    assert every, "no templated paths are declared"
    for pattern in every:
        text = pattern.pattern
        assert text.startswith("^") and text.endswith("$"), text
        assert "*" not in text and ".+" not in text and ".*" not in text, text
        # Every variable segment is a UUID, never a free-form capture.
        assert "[0-9a-f]{8}-" in text, text

    # Nothing may reach a participant or voiceprint DELETE.
    for pattern in ALLOWED_DELETE_PATTERNS:
        assert "voiceprint" not in pattern.pattern
        assert pattern.pattern.rstrip("$").endswith("/participants/" + "[0-9a-f]{8}-"
            "[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def test_a_query_string_cannot_be_smuggled_into_an_allowlisted_path() -> None:
    """Query values go through the `query` argument, not appended to the path.

    A `?` in the path means something is building URLs by hand, which is how a
    credential ends up in one.
    """
    from mom_igd.shell.launcher import _permitted

    assert _permitted("/enrollment/participants", ALLOWED_PROXY_PATHS, ()) is True
    for bad in (
        "/enrollment/participants?token=abc",
        "/enrollment/participants#frag",
        "/health?x=1",
    ):
        assert _permitted(bad, ALLOWED_PROXY_PATHS, ()) is False, bad


def test_the_proxy_refuses_a_path_outside_the_allowlist() -> None:
    api = ShellApi("http://127.0.0.1:1", SessionToken(), config=None)  # type: ignore[arg-type]
    for path in ("/etc/passwd", "/openapi.json", "http://evil.example.com", "/"):
        result = api.api_get(path)
        assert result["ok"] is False
        assert "allowlist" in result["error"]


def test_bootstrap_payload_contains_no_secret(config) -> None:
    token = SessionToken()
    api = ShellApi("http://127.0.0.1:1234", token, config)
    payload = api.bootstrap()
    assert token.value not in repr(payload)
    assert "token" not in {key.lower() for key in payload}
    assert payload["base_url"] == "http://127.0.0.1:1234"
    assert payload["offline"] is True


def test_manual_launch_command_is_documented() -> None:
    assert "mom_igd shell" in manual_launch_command()
    assert ".venv" in manual_launch_command()


# ------------------------------------------------ honest feature advertising


def test_future_features_are_shown_as_not_implemented(sources: dict[str, str]) -> None:
    """Recording went live in Phase 2, Participants in Phase 3, Transcription in 4.

    Every other card stays disabled and says so. The assertion is that the counts
    agree with each other rather than that they equal a fixed number: an enabled card
    must have an implementation, and a disabled one must say it has none.
    """
    html = sources["index.html"]
    for feature in (
        "Meeting setup",
        "Recording",
        "Participants",
        "Transcription",
        "Review",
        "Export",
    ):
        assert feature in html, f"the {feature} card must be present"
    # Per card, not per document. Counting these words across the whole page was the
    # brittle version: it broke the moment the paragraph above the grid explained what
    # the "Tersedia" badge means, which is prose about the labels rather than a label.
    # Reading each card also checks something the counts never did -- that the card
    # carrying a claim is the card the claim is about.
    # The whole element, opening tag included: `aria-disabled` and
    # `feature-card-enabled` live in the tag, not in the body, so capturing only the
    # body made every card read as neither available nor unavailable.
    cards = re.findall(
        r'<article class="feature-card[^"]*"[^>]*>.*?</article>', html, flags=re.S
    )
    assert len(cards) >= 6, f"expected the capability grid, found {len(cards)} card(s)"

    enabled = 0
    for card in cards:
        is_disabled = 'aria-disabled="true"' in card
        is_enabled = "feature-card-enabled" in card
        assert is_disabled != is_enabled, (
            "a card is either available or it is not; this one claims both or neither: "
            f"{card[:120]!r}"
        )
        if is_enabled:
            enabled += 1
            assert "phase-tag-live" in card and "Tersedia" in card, (
                "an available card must be labelled available"
            )
            assert "Belum diimplementasikan" not in card, (
                "an available card must not also say it is unimplemented"
            )
        else:
            assert "Belum diimplementasikan" in card, (
                "a card with no implementation must say so, in those words"
            )
            assert "phase-tag-live" not in card and "Tersedia" not in card
    for panel in (
        'id="card-recording"',
        'id="card-participants"',
        'id="card-transcription"',
        'id="card-mom"',
    ):
        assert panel in html, f"{panel} is implemented and must be enabled"
    assert enabled == 4, (
        "Recording, Participants, Transcription and Minutes are live"
    )


def test_a_card_that_is_partly_built_says_which_part_works(sources: dict[str, str]) -> None:
    """Two cards described a whole phase as absent while the operator used part of it.

    "Export" read "Belum diimplementasikan" on an install where the operator had just
    produced a Word file from it; what is missing is export from an *approved* minute,
    and approval is Phase 9. "Meeting setup" read the same while meetings were being
    created on every Start. A roadmap phase is not always all-or-nothing, and a card
    that rounds it to nothing tells the reader something false about their own install.
    """
    import re as _re

    html = sources["index.html"]
    cards = {
        _re.search(r"<h3>([^<]+)</h3>", card).group(1): card
        for card in _re.findall(
            r'<article class="feature-card[^"]*"[^>]*>.*?</article>', html, flags=_re.S
        )
        if _re.search(r"<h3>([^<]+)</h3>", card)
    }

    export = cards["Export"]
    assert "Ekspor draf sudah tersedia" in export, (
        "the export card must say draft export works, because it does"
    )
    assert "disetujui" in export, "and must name the part that does not"

    setup = cards["Meeting setup"]
    assert "sudah bisa dibuat" in setup, (
        "the meeting-setup card must say a meeting is created on Start, because it is"
    )


def test_offline_mode_is_displayed(sources: dict[str, str]) -> None:
    assert "Offline Mode" in sources["index.html"]


def test_app_identity_placeholders_exist(sources: dict[str, str]) -> None:
    html = sources["index.html"]
    for element_id in ("app-name", "app-version", "app-phase"):
        assert f'id="{element_id}"' in html


def test_status_cards_exist_for_every_required_area(sources: dict[str, str]) -> None:
    html = sources["index.html"]
    for element_id in ("card-backend", "card-database", "card-datadir", "card-readiness"):
        assert f'id="{element_id}"' in html


# ===========================================================================
# The side rail
# ===========================================================================


def test_every_rail_destination_exists(sources: dict[str, str]) -> None:
    """A rail item pointing at nothing is a button that silently blanks the screen."""
    import re

    html = sources["index.html"]
    wanted = set(re.findall(r'data-view="([a-z-]+)"', html))
    assert wanted, "the rail must have destinations"
    present = set(re.findall(r'<div class="view" id="([a-z-]+)"', html))
    assert wanted - present == set(), f"rail points at missing views: {wanted - present}"
    assert present - wanted == set(), f"views nothing can reach: {present - wanted}"


def test_every_home_shortcut_points_at_a_rail_item(sources: dict[str, str]) -> None:
    """The home cards drive the rail rather than duplicating what it does."""
    import re

    html = sources["index.html"]
    wanted = set(re.findall(r'data-goto="([a-z-]+)"', html))
    assert wanted, "the home screen must offer the steps as cards"
    present = set(re.findall(r'class="nav-item[^"]*" id="([a-z-]+)"', html))
    assert wanted - present == set(), f"home points at missing rail items: {wanted - present}"


def test_exactly_one_view_is_visible_before_the_script_runs(sources: dict[str, str]) -> None:
    """Two visible views stack; none visible is a blank window if the script fails.

    The rail sets this properly at startup, but the page must already be correct
    without it -- `app.js` failing to parse must not leave an empty application.
    """
    import re

    views = re.findall(r'<div class="view" id="[a-z-]+"( hidden)?>', sources["index.html"])
    visible = [marker for marker in views if not marker]
    assert len(visible) == 1, f"{len(visible)} views are visible with no script running"


def test_no_panel_carries_its_own_hidden_flag(sources: dict[str, str]) -> None:
    """Visibility has one owner. Two nested `hidden` flags is how a view opens blank."""
    import re

    html = sources["index.html"]
    for panel in ("recording-panel", "participants-panel", "transcript-panel", "mom-panel"):
        tag = re.search(rf'<section[^>]*id="{panel}"[^>]*>', html)
        assert tag, panel
        assert "hidden" not in tag.group(0), (
            f"{panel} hides itself as well as its view; the rail owns visibility now"
        )


# ===========================================================================
# The shape of the bridge's answer
# ===========================================================================


def test_the_script_only_reads_keys_the_bridge_actually_returns(sources: dict[str, str]) -> None:
    """The minutes panel read `response.body` for its whole life. There is no such key.

    `ShellApi.api_get` and `api_post` answer with `ok`, `status`, and then `data` on
    success or `error` on failure. Reading anything else yields `undefined` in silence,
    which is worse than an exception: the minutes panel rendered "Model: belum tersedia"
    and "Fitur notulen: dimatikan di konfigurasi" on an install where the model was
    present and the feature enabled, and offered an empty transcript list next to a
    completed transcript. Seven reads, no error, three wrong statements on screen.

    The allowed set is derived from the Python, not restated here, so adding a key to
    the envelope cannot leave this test asserting yesterday's shape.
    """
    import ast
    import re

    launcher = (
        Path(__file__).resolve().parents[1] / "mom_igd" / "shell" / "launcher.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(launcher)
    allowed: set[str] = set()
    for node in ast.walk(tree):
        # Every `return {...}` in the proxy is one shape of the envelope.
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys = {
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            if "ok" in keys:
                allowed |= keys
    assert {"ok", "status", "data", "error"} <= allowed, (
        f"the proxy's envelope no longer looks as expected: {sorted(allowed)}"
    )

    # Comments are stripped first. The prose explaining this very defect names
    # `response.body`, and matching it would fail the test on its own documentation --
    # a mistake already made three times on this project.
    script = re.sub(r"/\*.*?\*/", " ", sources["app.js"], flags=re.S)
    script = re.sub(r"(?<!:)//[^\n]*", " ", script)

    # `x.json()` is a method call on a real `fetch` Response -- the bootstrap reads
    # `/health` that way, which is allowed -- not a key off the bridge's envelope. So
    # anything followed by `(` is left alone.
    used = set(re.findall(r"\b(?:response|envelope)\.([a-z_]+)\b(?!\s*\()", script))
    unknown = used - allowed
    assert unknown == set(), (
        f"app.js reads {sorted(unknown)} off a bridge answer that never carries it; "
        f"the envelope has {sorted(allowed)}"
    )


def test_no_class_anywhere_on_a_toggled_element_sets_display(sources: dict[str, str]) -> None:
    """The whole application, not one panel.

    `hidden` works through a single UA-stylesheet rule, so any author `display`
    declaration on a toggled element defeats it. `.modal-backdrop { display: flex }`
    once kept both dialogs laid out from page load; the second painted over the whole
    application and swallowed every click, so the revoke dialog looked open, showed no
    participant, and neither of its buttons did anything.

    The `!important` net in `app.css` catches that. This asserts the net is not needed:
    cards flow with `> * + *` margins, the modal centres by transform, the meter is a
    positioned track. A rule relied upon and never exercised is a rule somebody deletes
    as dead.
    """
    import re

    html, js = sources["index.html"], sources["app.js"]
    css = re.sub(r"/\*.*?\*/", " ", sources["app.css"], flags=re.S)

    toggled = set(re.findall(r"(?:show|voiceShow)\(\s*(?:el|voice)\.(\w+)", js))
    lookups = dict(re.findall(r"(\w+):\s*document\.getElementById\(['\"]([^'\"]+)['\"]\)", js))
    ids = {lookups[name] for name in toggled if name in lookups}
    assert ids, "the script must toggle something"

    classes: set[str] = set()
    for element_id in ids:
        tag = re.search(rf'<[^>]*\bid="{re.escape(element_id)}"[^>]*>', html)
        if not tag:
            continue
        found = re.search(r'class="([^"]*)"', tag.group(0))
        if found:
            classes |= set(found.group(1).split())

    offenders = sorted(
        {
            name
            for name in classes
            for rule in re.finditer(rf"\.{re.escape(name)}\s*(?:,[^{{]*)?\{{([^}}]*)\}}", css)
            if re.search(r"(^|[;\s])display\s*:", rule.group(1))
        }
    )
    assert offenders == [], (
        f"these classes set `display` and sit on elements the script hides: {offenders}. "
        "Lay them out with margins, position or transform instead."
    )


def test_the_roster_never_implies_that_it_identifies_voices(sources: dict[str, str]) -> None:
    """An operator filled a roster with 22 people and asked why every segment still
    said UNASSIGNED.

    The behaviour was correct and the screen was not. Its only sentence about voices
    read "Menambah roster tidak menjamin akurasi pengenalan suara" -- which says voice
    recognition exists and merely is not guaranteed. It does not exist: nothing in this
    build assigns a speaker, `validate_transcription` rejects any result carrying one,
    and `minute_items` has no foreign key to a participant.

    Hedging about the accuracy of an absent capability is worse than silence, because
    it invites exactly the conclusion that was drawn. The roster card must state the
    absence outright, and no asset may hedge about it.
    """
    html = sources["index.html"]
    # Comments stripped first: the prose explaining why this phrasing was removed names
    # the phrasing, and matching it would fail the test on its own documentation.
    js = re.sub(r"/\*.*?\*/", " ", sources["app.js"], flags=re.S)
    js = re.sub(r"(?<!:)//[^\n]*", " ", js)

    hedges = (
        "tidak menjamin akurasi pengenalan suara",
        "belum akurat mengenali suara",
        "akurasi pengenalan suara",
    )
    for phrase in hedges:
        assert phrase not in js, (
            f"app.js hedges about voice recognition ({phrase!r}); this build has none, "
            "so the honest statement is that it does not happen at all"
        )
        assert phrase not in html, f"index.html hedges about voice recognition ({phrase!r})"

    card = html[html.index('id="roster-card"') :]
    card = card[: card.index("</article>")]
    assert "tidak membuat aplikasi mengenali suara" in card, (
        "the roster card must say outright that adding a name does not make the "
        "application recognise a voice"
    )
    assert "UNASSIGNED" in card, (
        "and must name the mark the operator will actually see on every segment"
    )
    assert "diucapkan" in card, (
        "and must say the one thing the roster does do: correct the spelling of a name "
        "the meeting said out loud"
    )


def test_the_moving_background_stays_cheap(sources: dict[str, str]) -> None:
    """A background that moves must animate nothing the compositor cannot handle.

    Every card in this shell uses `backdrop-filter`, so a moving background means the
    blur behind all of them is recomputed each frame -- on an Intel Iris Xe, while
    twelve CPU threads decode audio for forty minutes. Animating `transform` and
    `opacity` keeps that on the GPU. Animating a size, a colour or a gradient would
    force layout or paint on every frame instead, and the cost would land exactly where
    the machine has none to spare.
    """
    css = re.sub(r"/\*.*?\*/", " ", sources["app.css"], flags=re.S)

    frames = re.search(r"@keyframes aurora-drift\s*\{(.*?)\n\}", css, re.S)
    assert frames, "the drifting background must exist"
    animated = set(re.findall(r"(?<![\w-])([a-z-]+)\s*:", frames.group(1)))
    assert animated <= {"transform", "opacity"}, (
        f"the background animates {sorted(animated - {'transform', 'opacity'})}, which "
        "cannot be composited; only transform and opacity may move"
    )

    assert "requestAnimationFrame" not in sources["app.js"], (
        "the background is CSS only -- a script loop would run beside the ASR worker"
    )
    assert "<canvas" not in sources["index.html"]


def test_the_background_stops_while_heavy_work_runs(sources: dict[str, str]) -> None:
    """Motion is a luxury; the transcription is not.

    Keyed off the state the panels already publish, so the background cannot keep
    drifting while the screen says a job is running -- one source of truth, read twice.
    """
    css = re.sub(r"/\*.*?\*/", " ", sources["app.css"], flags=re.S)
    paused = re.search(r"([^{}]*)\{\s*animation-play-state:\s*paused", css)
    assert paused, "the background must pause while something heavy is running"
    selector = paused.group(1)
    for state in ("#rec-pill", "pill-live", 'data-state="live"'):
        assert state in selector, f"the pause must react to {state}"


def test_stillness_is_honoured_without_flattening_the_design(sources: dict[str, str]) -> None:
    """`prefers-reduced-motion` stops the drift and keeps the washes.

    Removing the gradients as well would leave the glass surfaces with nothing to be
    glass against, so somebody who asked for less movement would get a different, worse
    interface rather than the same one holding still.
    """
    css = re.sub(r"/\*.*?\*/", " ", sources["app.css"], flags=re.S)
    body = _css_block(css, "@media (prefers-reduced-motion: reduce)")
    assert re.search(r"body::before\s*\{\s*animation:\s*none", body), (
        "the drift must stop under reduced motion"
    )
    assert "background: none" not in body and "display: none" not in body, (
        "the washes must survive; only the movement stops"
    )


def test_the_setup_guide_tells_a_new_clone_where_its_data_goes() -> None:
    r"""A colleague with no D: drive follows every step correctly and then fails.

    The guide covered Python, the environment, dependencies and models, and stopped. The
    shipped `data_root` is `D:\MoM-IGD-Data`, so the first command on a C:-only laptop
    dies with "The system cannot find the path specified". `doctor` diagnoses it well,
    but being told beforehand beats being diagnosed afterwards -- and the same section
    answers the question that produced it: where the finished minutes end up.
    """
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert "data_root" in readme, "the setup guide must name the setting"
    # Raw: `\l` is not an escape sequence, so this string only meant what it looks
    # like by accident. Python warns about it today and will raise tomorrow.
    assert "config/local.toml" in readme or r"config\local.toml" in readme
    assert "exports/" in readme, "and must say where the finished documents land"
    assert (root / "config" / "local.example.toml").is_file(), (
        "the file the guide tells the reader to copy must exist"
    )

    example = (root / "config" / "local.example.toml").read_text(encoding="utf-8")
    assert "data_root" in example
    import tomllib

    parsed = tomllib.loads(example)
    assert "data_root" in parsed, (
        "the example must be usable as-is after editing one path, not a commented "
        "skeleton the reader has to assemble"
    )
