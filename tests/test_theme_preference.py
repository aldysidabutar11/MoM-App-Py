"""A light/dark choice that survives a restart, without browser storage.

The operator reads this screen outdoors, where a dark interface disappears in daylight.
Following `prefers-color-scheme` alone cannot serve that: it means changing the whole
operating system to read one application.

Two constraints shaped the implementation and both are checked here. The page may not
use `localStorage`, `sessionStorage`, cookies or IndexedDB -- a page that stores nothing
cannot accidentally store the session token -- so the choice is kept by Python. And the
proxy allowlist is closed and reviewed, so no HTTP route was added: this is how the shell
paints itself, not application data, and the shell owns it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mom_igd.shell.preferences import THEME_CHOICES, read_theme, write_theme

WEB = Path(__file__).resolve().parents[1] / "mom_igd" / "shell" / "web"


# ===========================================================================
# Storage
# ===========================================================================


def test_the_default_is_to_follow_the_system(config, paths, conn) -> None:
    """Windows already knows whether it is night."""
    database = paths.database_path(config.database.filename)
    assert read_theme(database) == "system"
    assert THEME_CHOICES[0] == "system"


def test_a_choice_survives(config, paths, conn) -> None:
    database = paths.database_path(config.database.filename)
    for theme in ("dark", "light", "system"):
        assert write_theme(database, theme) == theme
        assert read_theme(database) == theme


def test_an_unknown_theme_is_refused_rather_than_stored(config, paths, conn) -> None:
    """A value outside the set would come back on the next launch and have to be
    defended against there instead."""
    database = paths.database_path(config.database.filename)
    write_theme(database, "dark")
    with pytest.raises(ValueError, match="must be one of"):
        write_theme(database, "neon")
    assert read_theme(database) == "dark", "the stored choice must survive a bad write"


def test_reading_never_raises(tmp_path: Path) -> None:
    """A window that will not open because it could not read a colour preference is a
    worse fault than the wrong colour."""
    assert read_theme(tmp_path / "does-not-exist.db") == "system"


# ===========================================================================
# The page
# ===========================================================================


@pytest.fixture(scope="module")
def css() -> str:
    return re.sub(r"/\*.*?\*/", " ", (WEB / "app.css").read_text(encoding="utf-8"), flags=re.S)


@pytest.fixture(scope="module")
def js() -> str:
    return (WEB / "app.js").read_text(encoding="utf-8")


def test_an_explicit_choice_beats_the_system_setting(css: str) -> None:
    """The whole point, and the easiest thing to get wrong.

    An unqualified `@media (prefers-color-scheme: dark)` keeps firing on a machine set
    to dark, so choosing "Terang" would change nothing -- precisely the situation the
    feature was asked for. The guard is what makes the switch real.
    """
    assert "@media (prefers-color-scheme: dark) {" in css
    guarded = re.search(
        r"@media \(prefers-color-scheme: dark\)\s*\{\s*(:root[^\s{]*)", css
    )
    assert guarded, "the dark media query must exist"
    assert guarded.group(1) == ":root:not([data-theme])", (
        f"the media query targets {guarded.group(1)}, so an explicit choice cannot "
        "override the system setting"
    )


def test_native_controls_follow_the_choice(css: str) -> None:
    """Scrollbars and dropdowns are painted from `color-scheme`, not from the tokens.

    Without pinning it, a white scrollbar appears beside a dark interface whenever the
    choice disagrees with Windows.
    """
    assert ':root[data-theme="dark"] { color-scheme: dark; }' in css
    assert ':root[data-theme="light"] { color-scheme: light; }' in css


def test_system_is_expressed_by_removing_the_attribute(js: str) -> None:
    """`data-theme="system"` would keep the override on and never hand control back."""
    block = js[js.index("THEME\n") :]
    assert "removeAttribute('data-theme')" in block


def test_the_page_stores_nothing_itself(js: str) -> None:
    block = js[js.index("THEME\n") :]
    for banned in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
        assert f"{banned}." not in block, banned
    assert "set_theme" in block and "get_theme" in block


def test_the_switch_paints_before_it_stores(js: str) -> None:
    """A switch has to feel instant, and a failed write must not leave the operator
    looking at a theme they did not pick."""
    block = js[js.index("function choose(") :]
    block = block[: block.index("\n  }")]
    assert block.index("paint(theme)") < block.index("set_theme"), (
        "the paint must happen before the round-trip, not after it"
    )


def test_the_pressed_state_is_the_announced_state(css: str, js: str) -> None:
    """One source of truth: the style reads `aria-pressed` rather than a parallel
    class, so the selected look cannot disagree with what a screen reader says."""
    assert '.theme-option[aria-pressed="true"]' in css
    assert "aria-pressed" in js
