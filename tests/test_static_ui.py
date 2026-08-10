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
    disabled = html.count('aria-disabled="true"')
    assert disabled == html.count("Belum diimplementasikan"), (
        "every disabled card must say it is not implemented, and no enabled card may"
    )
    enabled = html.count("feature-card-enabled")
    assert enabled == html.count("phase-tag-live") == html.count("Tersedia")
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
