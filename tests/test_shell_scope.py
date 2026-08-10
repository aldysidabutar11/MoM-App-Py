"""Every function a shell module calls must be defined where it can reach it.

`app.js` is a sequence of independent IIFEs with no build step, no bundler and no
linter. A module that calls a helper belonging to a *different* closure parses fine,
loads fine, and throws `ReferenceError` the moment the operator clicks the button.

That is not hypothetical. The voice-to-text panel called `show()`, which lives in the
enrollment module, on the fifth line of its click handler -- immediately after disabling
its own button. The result on screen was a greyed-out button and nothing else: identical
to a feature that had not been wired up at all, and impossible to distinguish from one
by looking. Two rounds of manual testing missed it because there is nothing to see.

So the scopes are checked mechanically. This is the cheapest possible substitute for the
type checker this file does not have.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "mom_igd" / "shell" / "web" / "app.js"

#: Names available everywhere: language built-ins, browser APIs, and the two objects the
#: shell itself injects. Anything outside this list has to be defined in the module that
#: calls it -- which is the whole point.
_GLOBALS: frozenset[str] = frozenset(
    {
        # language
        "Array", "Boolean", "Date", "Error", "JSON", "Map", "Math", "Number", "Object",
        "Promise", "RegExp", "Set", "String", "parseFloat", "parseInt", "isNaN",
        "encodeURIComponent", "decodeURIComponent", "Intl",
        "isFinite", "Symbol", "WeakMap", "Infinity",
        # browser. `fetch` is legitimate here and used exactly once, for a relative URL
        # to the loopback backend that served the page -- not for a remote host.
        "document", "window", "console", "setTimeout", "clearTimeout", "setInterval",
        "clearInterval", "requestAnimationFrame", "alert", "navigator", "location",
        "CustomEvent", "Event", "URL", "URLSearchParams", "AbortController", "fetch",
        "queueMicrotask", "structuredClone", "atob", "btoa", "performance",
        # control flow keywords that the crude call regex can pick up
        "if", "for", "while", "switch", "catch", "return", "typeof", "function",
        "await", "new", "throw", "else", "do", "delete", "in", "of", "case",
    }
)

_IIFE_START = re.compile(r"^\(function \(\) \{", re.M)
_IIFE_END = re.compile(r"^\}\)\(\);", re.M)


def _modules() -> list[tuple[int, str]]:
    """Each top-level IIFE, as (first line number, body)."""
    source = APP_JS.read_text(encoding="utf-8")
    starts = [m.start() for m in _IIFE_START.finditer(source)]
    ends = [m.start() for m in _IIFE_END.finditer(source)]
    assert len(starts) == len(ends), "unbalanced IIFEs in app.js"
    return [
        (source[:start].count("\n") + 1, source[start:end])
        for start, end in zip(starts, ends)
    ]


def _defined_in(body: str) -> set[str]:
    """Names a module binds: declarations, function statements, and parameters."""
    names: set[str] = set()
    names.update(re.findall(r"\bfunction\s+(\w+)\s*\(", body))
    names.update(re.findall(r"\b(?:var|let|const)\s+(\w+)\s*=", body))
    names.update(re.findall(r"\b(?:var|let|const)\s+(\w+)\s*[;,]", body))
    # Parameters of every function, including arrow-free callbacks.
    for params in re.findall(r"function\s*\w*\s*\(([^)]*)\)", body):
        names.update(part.strip() for part in params.split(",") if part.strip().isidentifier())
    # `catch (exc)` and `for (var x ...)` bindings.
    names.update(re.findall(r"catch\s*\(\s*(\w+)\s*\)", body))
    return names


def _called_in(body: str) -> set[str]:
    """Bare identifiers invoked as functions: `name(` but not `.name(` or `new name(`."""
    without_strings = re.sub(r"'[^'\n]*'|\"[^\"\n]*\"", "''", body)
    without_comments = re.sub(r"/\*.*?\*/|//[^\n]*", "", without_strings, flags=re.S)
    return {
        match.group(1)
        for match in re.finditer(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", without_comments)
    }


def test_every_module_defines_what_it_calls() -> None:
    """The check that would have caught the voice panel's dead button before shipping."""
    offenders: list[str] = []
    for line, body in _modules():
        unresolved = _called_in(body) - _defined_in(body) - _GLOBALS
        for name in sorted(unresolved):
            offenders.append(
                f"app.js module at line {line} calls {name}() but nothing in that "
                f"closure defines it"
            )
    assert offenders == [], "\n".join(offenders)


def test_the_voice_panel_uses_only_its_own_helpers() -> None:
    """Named separately, because this is the module the rule was written for."""
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("voice to text")
    block = source[start : source.index("addEventListener('click', function () { runVoiceCheck", start)]
    assert "voiceShow(" in block, "the panel must use its own show helper"
    # `_called_in` strips comments and strings before matching, which matters here: the
    # block *documents* why `show()` must not be called from it, and a plain search finds
    # that explanation and fails against it. Third time this project has written a test
    # that accuses its own prose.
    assert "show" not in _called_in(block), (
        "the panel must not call the enrollment module's show()"
    )


def test_the_scope_checker_actually_catches_a_borrowed_helper() -> None:
    """A test that cannot fail is worse than no test. This proves the detector works."""
    borrowed = "(function () {\n  function localThing() {}\n  borrowedThing();\n})();"
    body = borrowed[borrowed.index("{") :]
    unresolved = _called_in(body) - _defined_in(body) - _GLOBALS
    assert "borrowedThing" in unresolved
    assert "localThing" not in unresolved


def test_the_shell_script_parses(tmp_path) -> None:
    """A syntax error in `app.js` takes the whole interface down, silently.

    The static checks above read the file as text, which catches scope mistakes but not a
    stray bracket. Node is not a dependency of this project and never will be -- the shell
    has no build step on purpose -- so this skips when it is absent rather than demanding
    it. On a developer machine that happens to have it, it is the cheapest possible guard
    against shipping a blank window.
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the static checks above still apply")

    result = subprocess.run(  # noqa: S603 - fixed executable, repository-owned path
        [node, "--check", str(APP_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"app.js does not parse:\n{result.stderr}"
