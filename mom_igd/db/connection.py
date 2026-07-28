"""SQLite connections with verified pragmas.

Every connection is opened the same way and then *checked*: if SQLite does not
confirm WAL journalling and foreign-key enforcement, the connection is closed
and an error is raised. Silently running without WAL would remove the crash
resilience the recording pipeline depends on; silently running without foreign
keys would let orphaned chunks and stages accumulate.

Transactions are managed explicitly (``isolation_level=None``) because the
migration runner needs precise control over its own ``BEGIN``/``COMMIT``.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final, Iterator

__all__ = [
    "PragmaVerificationError",
    "connect",
    "maybe_transaction",
    "read_pragmas",
    "transaction",
    "verify_pragmas",
]

_REQUIRED_JOURNAL_MODE: Final[str] = "wal"
_DEFAULT_BUSY_TIMEOUT_MS: Final[int] = 5000


class PragmaVerificationError(RuntimeError):
    """Raised when SQLite does not confirm a required pragma."""


def read_pragmas(conn: sqlite3.Connection) -> dict[str, Any]:
    """Read back the pragmas that matter for correctness."""
    cursor = conn.cursor()
    try:
        journal_mode = cursor.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = cursor.execute("PRAGMA foreign_keys").fetchone()[0]
        busy_timeout = cursor.execute("PRAGMA busy_timeout").fetchone()[0]
        synchronous = cursor.execute("PRAGMA synchronous").fetchone()[0]
        user_version = cursor.execute("PRAGMA user_version").fetchone()[0]
    finally:
        cursor.close()
    return {
        "journal_mode": str(journal_mode).lower(),
        "foreign_keys": int(foreign_keys),
        "busy_timeout": int(busy_timeout),
        "synchronous": int(synchronous),
        "user_version": int(user_version),
        "sqlite_version": sqlite3.sqlite_version,
    }


def verify_pragmas(conn: sqlite3.Connection) -> dict[str, Any]:
    """Verify WAL and foreign keys are actually active.

    Returns:
        The pragma readout, so callers can log or expose it.

    Raises:
        PragmaVerificationError: If WAL or foreign keys are not confirmed.
    """
    pragmas = read_pragmas(conn)
    problems: list[str] = []
    if pragmas["journal_mode"] != _REQUIRED_JOURNAL_MODE:
        problems.append(
            f"journal_mode is {pragmas['journal_mode']!r}, expected "
            f"{_REQUIRED_JOURNAL_MODE!r} (in-memory databases cannot use WAL; "
            "use a file-backed database)"
        )
    if pragmas["foreign_keys"] != 1:
        problems.append("foreign_keys is disabled")
    if problems:
        raise PragmaVerificationError(
            "SQLite did not confirm required settings: " + "; ".join(problems)
        )
    return pragmas


def connect(
    db_path: str | Path,
    *,
    busy_timeout_ms: int = _DEFAULT_BUSY_TIMEOUT_MS,
    verify: bool = True,
) -> sqlite3.Connection:
    """Open a connection with WAL, foreign keys and a busy timeout.

    The parent directory must already exist: creating it here would let any code
    path quietly materialise part of the runtime tree, which
    :class:`mom_igd.paths.RuntimePaths` is meant to own.

    Raises:
        FileNotFoundError: If the parent directory is missing.
        PragmaVerificationError: If ``verify`` and the pragmas are not confirmed.
    """
    path = Path(db_path)
    if not path.parent.is_dir():
        raise FileNotFoundError(
            f"Database directory {path.parent} does not exist. Call "
            "RuntimePaths.ensure() before opening the database."
        )

    conn = sqlite3.connect(
        path,
        timeout=max(busy_timeout_ms, 0) / 1000.0,
        isolation_level=None,  # explicit transaction control
        check_same_thread=False,  # the API serves requests from a worker thread
    )
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        try:
            # Order matters: journal_mode returns the resulting mode.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={int(max(busy_timeout_ms, 0))}")
            # NORMAL is the recommended durability level under WAL: a crash
            # cannot corrupt the database, only lose the last transaction.
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA temp_store=MEMORY")
        finally:
            cursor.close()
        if verify:
            verify_pragmas(conn)
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """Run a block inside an explicit transaction, rolling back on error."""
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - rollback of a dead txn
            pass
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def maybe_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Join the caller's transaction if one is open, otherwise open our own.

    This is what lets a state transition and its audit record be written
    atomically without every helper having to know who owns the transaction.
    """
    if conn.in_transaction:
        yield conn
        return
    with transaction(conn) as active:
        yield active
