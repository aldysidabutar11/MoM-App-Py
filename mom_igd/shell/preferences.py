"""Shell display preferences, stored beside every other application setting.

Small on purpose. This is not application data -- nothing here describes a meeting, a
participant or a recording -- so it gets the plainest possible storage and no service
layer of its own.
"""

from __future__ import annotations

from typing import Final

from mom_igd.db.connection import connect, maybe_transaction
from mom_igd.logging_setup import get_logger

__all__ = ["THEME_KEY", "THEME_CHOICES", "read_theme", "write_theme"]

_LOG = get_logger("shell.preferences")

THEME_KEY: Final[str] = "ui_theme"

#: `system` follows Windows, which already knows whether it is night. It is the default
#: and it is never removed: a two-way switch would make somebody re-choose every time
#: the machine changes on its own.
THEME_CHOICES: Final[tuple[str, ...]] = ("system", "light", "dark")


def read_theme(database_path, *, busy_timeout_ms: int = 5000) -> str:
    """The stored choice, or `system`.

    Never raises. A missing database, an unmigrated one, or a value written by a newer
    version all resolve to `system` -- a window that will not open because it could not
    read a colour preference would be a worse bug than the wrong colour.
    """
    try:
        conn = connect(database_path, busy_timeout_ms=busy_timeout_ms)
    except Exception as exc:  # noqa: BLE001 - a preference must never block startup
        _LOG.debug("shell.preferences.unreadable", extra={"reason": type(exc).__name__})
        return "system"
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (THEME_KEY,)
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("shell.preferences.unreadable", extra={"reason": type(exc).__name__})
        return "system"
    finally:
        conn.close()
    value = str(row["value"]) if row else "system"
    return value if value in THEME_CHOICES else "system"


def write_theme(database_path, theme: str, *, busy_timeout_ms: int = 5000) -> str:
    """Store a choice and return what was stored.

    An unknown value is refused rather than written: the page is the only caller, but a
    stored value outside `THEME_CHOICES` would come back on the next launch and have to
    be defended against there instead.
    """
    if theme not in THEME_CHOICES:
        raise ValueError(
            f"theme must be one of {', '.join(THEME_CHOICES)}, got {theme!r}"
        )
    # Imported here rather than at module level, the same way `enrollment/service.py`
    # reaches for it: the timestamp helper lives under `mom_igd.audio`, and opening a
    # window should not drag the capture engine into memory to write one row.
    from mom_igd.audio.manifest import utc_now_iso

    conn = connect(database_path, busy_timeout_ms=busy_timeout_ms)
    try:
        with maybe_transaction(conn):
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (THEME_KEY, theme, utc_now_iso()),
            )
    finally:
        conn.close()
    return theme
