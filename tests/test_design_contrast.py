"""The palette, measured against WCAG 2.1 rather than judged by eye.

Contrast is the one part of a visual design that is a number, so it is the one part a
test can hold. Both themes are checked, and the tokens are read out of `app.css` rather
than restated here -- a copy of the palette in a test file describes whatever the palette
was on the day it was written.

Translucent tokens are composited before measuring. Several semantic backgrounds in dark
mode are `rgba(...)` over the page, so measuring them as if they were opaque would report
a ratio nothing on screen ever has.

Not covered, and deliberately: anything built with `color-mix()`. Evaluating it needs a
CSS engine, and a hand-rolled approximation that drifts from the browser would be worse
than an honest gap. Those values are all borders and tints, never text on a background.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS_PATH = (
    Path(__file__).resolve().parents[1] / "mom_igd" / "shell" / "web" / "app.css"
)
CSS = CSS_PATH.read_text(encoding="utf-8")


def tokens(dark: bool) -> dict[str, str]:
    body = re.sub(r"/\*.*?\*/", " ", CSS, flags=re.S)
    if dark:
        # Read from the manual block rather than from inside the media query. The media
        # query is now guarded with `:not([data-theme])` so an explicit choice can beat
        # the system setting, and a regex that spelled the old selector reported the
        # whole dark palette missing. `:root[data-theme="dark"]` is the one block that
        # exists whichever way the palette is reached, and a test asserts the two carry
        # the same tokens.
        block = re.search(r':root\[data-theme="dark"\]\s*\{(.*?)\n\}', body, re.S)
        assert block, "the dark palette must be reachable by explicit choice"
        base = dict(re.findall(r"(--[\w-]+):\s*([^;]+);", re.search(r":root\s*\{(.*?)\n\}", body, re.S).group(1)))
        base.update(dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block.group(1))))
        return base
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", re.search(r":root\s*\{(.*?)\}", body, re.S).group(1)))


def parse(value: str) -> tuple[float, float, float, float] | None:
    value = value.strip()
    if value.startswith("#"):
        h = value[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            return None
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.match(r"rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,/\s]+([\d.]+))?\s*\)", value)
    if m:
        a = float(m.group(4)) if m.group(4) else 1.0
        return (float(m.group(1)), float(m.group(2)), float(m.group(3)), a)
    return None


def over(fg: tuple[float, float, float, float], bg: tuple[float, float, float, float]):
    """Composite a translucent colour onto an opaque one."""
    a = fg[3]
    return tuple(fg[i] * a + bg[i] * (1 - a) for i in range(3)) + (1.0,)


def luminance(c) -> float:
    def channel(v: float) -> float:
        v /= 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = channel(c[0]), channel(c[1]), channel(c[2])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg, bg) -> float:
    a, b = luminance(fg), luminance(bg)
    lo, hi = sorted((a, b))
    return (hi + 0.05) / (lo + 0.05)


PAIRS = [
    ("teks utama / kartu",        "--text",        "--surface",     4.5),
    ("teks utama / latar",        "--text",        "--bg",          4.5),
    ("teks redup / kartu",        "--text-muted",  "--surface",     4.5),
    ("teks redup / permukaan-2",  "--text-muted",  "--surface-2",   4.5),
    ("teks samar / kartu",        "--text-faint",  "--surface",     3.0),
    ("aksen / kartu",             "--accent",      "--surface",     4.5),
    ("label tombol utama",        "--on-accent",   "--accent",      4.5),
    ("OK / latarnya",             "--ok",          "--ok-soft",     4.5),
    ("WARN / latarnya",           "--warn",        "--warn-soft",   4.5),
    ("FAIL / latarnya",           "--fail",        "--fail-soft",   4.5),
]



def _measure(dark: bool) -> list[tuple[str, float, float]]:
    theme = tokens(dark)
    base = parse(theme["--bg"])
    out: list[tuple[str, float, float]] = []
    for label, fg_key, bg_key, need in PAIRS:
        fg, bg = parse(theme.get(fg_key, "")), parse(theme.get(bg_key, ""))
        assert fg and bg, f"{label}: token unreadable ({fg_key} / {bg_key})"
        if bg[3] < 1.0:
            bg = over(bg, base)
        if fg[3] < 1.0:
            fg = over(fg, bg)
        out.append((label, ratio(fg, bg), need))
    return out


def test_body_and_semantic_text_meet_wcag_aa_in_light_mode() -> None:
    failures = [
        f"{label}: {value:.2f}:1 (needs {need})"
        for label, value, need in _measure(dark=False)
        if value < need
    ]
    assert failures == [], failures


def test_body_and_semantic_text_meet_wcag_aa_in_dark_mode() -> None:
    failures = [
        f"{label}: {value:.2f}:1 (needs {need})"
        for label, value, need in _measure(dark=True)
        if value < need
    ]
    assert failures == [], failures


def test_the_measurement_itself_is_not_vacuous() -> None:
    """Negative control: white on white must be reported as failing.

    Without it, a parser that quietly returned nothing would make both tests above
    pass for ever -- which is exactly how the modal cascade control rotted.
    """
    white = (255.0, 255.0, 255.0, 1.0)
    black = (0.0, 0.0, 0.0, 1.0)
    assert ratio(white, white) == 1.0
    assert 20.9 < ratio(white, black) < 21.1, ratio(white, black)
    # And compositing must actually move a colour.
    half = over((255.0, 255.0, 255.0, 0.5), black)
    assert 120 < half[0] < 136, half


def test_body_text_reaches_aaa_where_it_costs_nothing() -> None:
    """Primary and secondary body text clear 7:1 in both themes.

    Not a WCAG requirement at AA, and not chased everywhere -- faint labels and the
    accent are AA by design. But the two colours that carry most of the reading are
    already far past it, and a regression that dropped them to 4.6 would be invisible
    without a number.
    """
    for dark in (False, True):
        measured = dict((label, value) for label, value, _ in _measure(dark))
        for label in ("teks utama / kartu", "teks redup / kartu"):
            assert measured[label] >= 7.0, (
                f"{'dark' if dark else 'light'} {label}: {measured[label]:.2f}:1"
            )


def test_the_two_dark_palettes_cannot_drift_apart() -> None:
    """The system path and the explicit path must define the same tokens.

    Dark is reachable two ways -- `prefers-color-scheme` for the default, and
    `[data-theme="dark"]` for somebody who chose it. Two hand-maintained copies of a
    palette diverge, and the divergence would show only to whichever half of the users
    took the other route.
    """
    body = re.sub(r"/\*.*?\*/", " ", CSS, flags=re.S)
    media = re.search(
        r"@media \(prefers-color-scheme: dark\)\s*\{\s*:root:not\(\[data-theme\]\)\s*\{(.*?)\n  \}",
        body,
        re.S,
    )
    manual = re.search(r':root\[data-theme="dark"\]\s*\{(.*?)\n\}', body, re.S)
    assert media and manual, "both routes into the dark palette must exist"

    def tokens_of(text: str) -> dict[str, str]:
        return {
            name: " ".join(value.split())
            for name, value in re.findall(r"(--[\w-]+):\s*([^;]+);", text)
        }

    from_media, from_manual = tokens_of(media.group(1)), tokens_of(manual.group(1))
    assert from_media == from_manual, {
        name: (from_media.get(name), from_manual.get(name))
        for name in set(from_media) | set(from_manual)
        if from_media.get(name) != from_manual.get(name)
    }


def test_text_stays_readable_over_the_background_wash() -> None:
    """Headings and the lede sit on the page itself, not inside a card.

    Every point of wash opacity is therefore taken off their contrast, and the ceiling
    is not the same in both themes: dark has room because the wash lightens a near-black
    page, while light fails sooner because the same indigo *darkens* a pale page under
    dark text. Measured, the light theme is already below AA at 0.28 while dark holds
    AAA to 0.40.

    This was worth writing down as a test rather than a comment because the wash was
    first tuned so faint the operator could not see it at all, and the obvious fix --
    turn it up -- is exactly the change that quietly costs readability.
    """
    for dark in (False, True):
        theme = tokens(dark)
        base = parse(theme["--bg"])
        text = parse(theme["--text"])
        muted = parse(theme["--text-muted"])
        assert base and text and muted

        for wash_name in ("--wash-1", "--wash-2", "--wash-3"):
            wash = parse(theme[wash_name])
            assert wash, wash_name
            washed = over(wash, base)
            where = f"{'dark' if dark else 'light'} {wash_name}"
            assert ratio(text, washed) >= 7.0, (
                f"{where}: body text falls to {ratio(text, washed):.2f}:1 over the wash"
            )
            assert ratio(muted, washed) >= 4.5, (
                f"{where}: muted text falls to {ratio(muted, washed):.2f}:1 over the wash"
            )


def test_the_wash_is_actually_visible() -> None:
    """The opposite failure, and the one that prompted this.

    A wash faint enough to be invisible is not a subtle background; it is an absent one
    that still costs a compositing layer. The operator looked for it and could not find
    it. A floor keeps the next tuning pass from drifting back below the threshold of
    being noticed at all.
    """
    for dark in (False, True):
        theme = tokens(dark)
        strongest = max(
            parse(theme[name])[3] for name in ("--wash-1", "--wash-2", "--wash-3")
        )
        assert strongest >= 0.15, (
            f"{'dark' if dark else 'light'}: the strongest wash is {strongest}, which is "
            "below the point at which anyone notices it"
        )
