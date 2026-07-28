"""Versioned, transactional SQLite migrations.

Guarantees this module provides:

* **Deterministic order.** Migrations are ``NNNN_name.sql`` files; versions must
  start at 1 and be contiguous. A gap or a duplicate is an error, not something
  to sort around.
* **Transactional.** SQLite makes DDL transactional, so each migration runs
  inside ``BEGIN IMMEDIATE`` together with the row that records it. A failure
  rolls back both, so the schema version can never advance past a migration that
  did not fully apply.
* **Idempotent.** Re-running is a no-op; already-applied migrations are skipped.
* **Tamper-evident.** The SHA-256 of each migration's SQL is recorded. Editing an
  applied migration is detected instead of silently diverging. Hashing normalises
  line endings first, because the development machine has ``core.autocrlf=true``.

Deliberately **not** implemented: a production downgrade path. A ``down``
migration on this application would mean dropping tables that hold meeting
recordings metadata, transcripts and approvals. Recovery is by restore from
``<data_root>/backups``, not by destructive rollback. The only rollback here is
the transactional rollback of a migration that fails while applying.

``sqlite3.Connection.executescript`` is intentionally avoided: it issues an
implicit ``COMMIT`` before running, which would defeat the explicit transaction.
Statements are therefore split by :func:`split_sql_statements`.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Iterable, Sequence

from mom_igd.db.connection import PragmaVerificationError, connect, verify_pragmas

__all__ = [
    "MIGRATIONS_DIR",
    "Migration",
    "MigrationError",
    "apply_migrations",
    "current_schema_version",
    "discover_migrations",
    "head_version",
    "initialize_database",
    "migration_status",
    "split_sql_statements",
    "verify_applied_checksums",
]

MIGRATIONS_DIR: Final[Path] = Path(__file__).resolve().parent / "migrations"

_FILENAME_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")

_META_TABLE_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    checksum    TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0
)
"""

_TRIGGER_START_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP\s+|TEMPORARY\s+)?TRIGGER\b",
    re.IGNORECASE,
)


class MigrationError(RuntimeError):
    """Raised when migration discovery, verification or application fails."""


# ---------------------------------------------------------------------------
# SQL statement splitting
# ---------------------------------------------------------------------------


def split_sql_statements(sql: str) -> list[str]:
    """Split a migration script into individual executable statements.

    Handles ``--`` line comments, ``/* */`` block comments, single-quoted string
    literals (with ``''`` escapes), and ``"``/``` ` ```/``[]`` quoted
    identifiers. ``BEGIN ... END`` blocks inside ``CREATE TRIGGER`` are kept
    together rather than split on their inner semicolons.
    """
    statements: list[str] = []
    buffer: list[str] = []
    word: list[str] = []
    trigger_depth = 0
    in_trigger = False
    index = 0
    length = len(sql)

    def flush_word() -> None:
        nonlocal trigger_depth, in_trigger
        if not word:
            return
        token = "".join(word).upper()
        word.clear()
        if not in_trigger:
            return
        if token == "BEGIN":
            trigger_depth += 1
        elif token == "END" and trigger_depth > 0:
            trigger_depth -= 1

    def flush_statement() -> None:
        nonlocal in_trigger, trigger_depth
        text = "".join(buffer).strip()
        buffer.clear()
        in_trigger = False
        trigger_depth = 0
        if text:
            statements.append(text)

    while index < length:
        char = sql[index]
        nxt = sql[index + 1] if index + 1 < length else ""

        if char == "-" and nxt == "-":
            flush_word()
            end = sql.find("\n", index)
            index = length if end == -1 else end + 1
            buffer.append("\n")
            continue

        if char == "/" and nxt == "*":
            flush_word()
            end = sql.find("*/", index + 2)
            index = length if end == -1 else end + 2
            buffer.append(" ")
            continue

        if char == "'":
            flush_word()
            buffer.append(char)
            index += 1
            while index < length:
                buffer.append(sql[index])
                if sql[index] == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        buffer.append(sql[index + 1])
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue

        if char in '"`':
            flush_word()
            closing = char
            buffer.append(char)
            index += 1
            while index < length:
                buffer.append(sql[index])
                if sql[index] == closing:
                    index += 1
                    break
                index += 1
            continue

        if char == "[":
            flush_word()
            buffer.append(char)
            index += 1
            while index < length:
                buffer.append(sql[index])
                if sql[index] == "]":
                    index += 1
                    break
                index += 1
            continue

        if char.isalnum() or char == "_":
            word.append(char)
            buffer.append(char)
            index += 1
            continue

        flush_word()

        if char == ";":
            if trigger_depth > 0:
                buffer.append(char)
                index += 1
                continue
            flush_statement()
            index += 1
            continue

        buffer.append(char)
        index += 1

        # Detect the start of a trigger body only once the statement prefix is
        # unambiguous, so BEGIN/END tracking never fires on a CASE expression.
        if not in_trigger and char.isspace():
            prefix = "".join(buffer)
            if _TRIGGER_START_RE.match(prefix):
                in_trigger = True

    flush_word()
    flush_statement()
    return statements


def _canonical_sql(sql: str) -> str:
    """Normalise for hashing: strip BOM, unify line endings, drop trailing space."""
    text = sql.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip() + "\n"


def _sha256(sql: str) -> str:
    return hashlib.sha256(_canonical_sql(sql).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Migration:
    """One migration file, parsed and checksummed."""

    version: int
    name: str
    path: Path
    sql: str
    checksum: str = field(compare=False)

    @property
    def statements(self) -> list[str]:
        return split_sql_statements(self.sql)

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    """Load and validate every migration in ``directory``.

    Raises:
        MigrationError: On a malformed filename, duplicate version, or a gap in
            the version sequence.
    """
    target = MIGRATIONS_DIR if directory is None else Path(directory)
    if not target.is_dir():
        raise MigrationError(f"Migrations directory not found: {target}")

    found: dict[int, Migration] = {}
    for path in sorted(target.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if match is None:
            raise MigrationError(
                f"Migration filename {path.name!r} does not match NNNN_name.sql "
                "(four digits, underscore, lowercase name)."
            )
        version = int(match.group("version"))
        if version < 1:
            raise MigrationError(f"Migration version must be >= 1, got {version} in {path.name}.")
        if version in found:
            raise MigrationError(
                f"Duplicate migration version {version}: {found[version].path.name} "
                f"and {path.name}."
            )
        sql = path.read_text(encoding="utf-8")
        found[version] = Migration(
            version=version,
            name=match.group("name"),
            path=path,
            sql=sql,
            checksum=_sha256(sql),
        )

    if not found:
        return []

    ordered = [found[key] for key in sorted(found)]
    expected = list(range(1, len(ordered) + 1))
    actual = [m.version for m in ordered]
    if actual != expected:
        raise MigrationError(
            f"Migration versions must be contiguous starting at 1. Expected "
            f"{expected}, found {actual}."
        )
    return ordered


def head_version(migrations: Sequence[Migration] | None = None) -> int:
    """Highest migration version available on disk (0 when there are none)."""
    items = discover_migrations() if migrations is None else migrations
    return max((m.version for m in items), default=0)


# ---------------------------------------------------------------------------
# State inspection
# ---------------------------------------------------------------------------


def _meta_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    return row is not None


def _ensure_meta_table(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_META_TABLE_SQL)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def current_schema_version(conn: sqlite3.Connection) -> int:
    """Return the highest fully-applied migration version (0 if none)."""
    if not _meta_table_exists(conn):
        return 0
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations").fetchone()
    return int(row["v"])


def applied_migrations(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Return the recorded migration rows, oldest first."""
    if not _meta_table_exists(conn):
        return []
    rows = conn.execute(
        "SELECT version, name, checksum, applied_at, duration_ms "
        "FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [dict(row) for row in rows]


def verify_applied_checksums(
    conn: sqlite3.Connection, migrations: Sequence[Migration] | None = None
) -> None:
    """Detect an applied migration whose file has been edited since.

    Raises:
        MigrationError: If a recorded checksum no longer matches the file, or a
            recorded version has no corresponding file.
    """
    items = discover_migrations() if migrations is None else migrations
    by_version = {m.version: m for m in items}
    for row in applied_migrations(conn):
        version = int(row["version"])
        migration = by_version.get(version)
        if migration is None:
            raise MigrationError(
                f"Database records migration {version} ({row['name']}) but no such "
                "migration file exists. The database was created by a different "
                "build; refusing to continue."
            )
        if migration.checksum != row["checksum"]:
            raise MigrationError(
                f"Migration {migration.label} was modified after it was applied "
                f"(recorded {str(row['checksum'])[:12]}..., file "
                f"{migration.checksum[:12]}...). Never edit an applied migration; "
                "add a new one instead."
            )


def migration_status(
    conn: sqlite3.Connection, migrations: Sequence[Migration] | None = None
) -> dict[str, object]:
    """Summarise migration state for diagnostics and the CLI."""
    items = discover_migrations() if migrations is None else migrations
    current = current_schema_version(conn)
    head = head_version(items)
    return {
        "current_version": current,
        "head_version": head,
        "up_to_date": current == head,
        "pending": [m.label for m in items if m.version > current],
        "applied": applied_migrations(conn),
    }


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def apply_migrations(
    conn: sqlite3.Connection,
    migrations: Sequence[Migration] | None = None,
) -> list[Migration]:
    """Apply every pending migration in order.

    Each migration runs in its own ``BEGIN IMMEDIATE`` transaction together with
    the ``schema_migrations`` row that records it, so a failure leaves the
    recorded version untouched.

    Returns:
        The migrations that were applied by this call (empty when up to date).

    Raises:
        MigrationError: If a migration fails; the message names the migration and
            the failing statement.
    """
    items = list(discover_migrations() if migrations is None else migrations)
    _ensure_meta_table(conn)
    verify_applied_checksums(conn, items)

    already = {int(row["version"]) for row in applied_migrations(conn)}
    newly: list[Migration] = []

    for migration in items:
        if migration.version in already:
            continue

        statements = migration.statements
        if not statements:
            raise MigrationError(f"Migration {migration.label} contains no statements.")

        started = time.perf_counter()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for position, statement in enumerate(statements, start=1):
                try:
                    conn.execute(statement)
                except sqlite3.Error as exc:
                    raise MigrationError(
                        f"Migration {migration.label} failed at statement "
                        f"{position}/{len(statements)}: {exc}. "
                        f"Statement: {statement.splitlines()[0][:160]}"
                    ) from exc
            duration_ms = int((time.perf_counter() - started) * 1000)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at, duration_ms) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    _utc_now_iso(),
                    duration_ms,
                ),
            )
            # Redundant marker so an external tool can read the version cheaply.
            conn.execute(f"PRAGMA user_version={int(migration.version)}")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        newly.append(migration)

    return newly


def initialize_database(
    db_path: Path,
    *,
    busy_timeout_ms: int = 5000,
    migrations: Sequence[Migration] | None = None,
    app_version: str | None = None,
) -> dict[str, object]:
    """Create (if needed) and migrate the database, then report its state.

    The parent directory must already exist -- call
    :meth:`mom_igd.paths.RuntimePaths.ensure` first.

    Returns:
        A dictionary with the database path, verified pragmas, applied
        migrations and final status.
    """
    existed_before = Path(db_path).exists()
    conn = connect(db_path, busy_timeout_ms=busy_timeout_ms)
    try:
        pragmas = verify_pragmas(conn)
        applied = apply_migrations(conn, migrations)
        if app_version:
            _seed_settings(conn, app_version)
        status = migration_status(conn, migrations)
        return {
            "database_path": str(db_path),
            # True only when this call brought the database file into existence.
            "created": not existed_before,
            "already_up_to_date": not applied,
            "pragmas": pragmas,
            "applied_now": [m.label for m in applied],
            "status": status,
        }
    finally:
        conn.close()


def _seed_settings(conn: sqlite3.Connection, app_version: str) -> None:
    """Record immutable provenance in ``app_settings`` (insert-if-absent)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('db_created_at', ?)",
            (_utc_now_iso(),),
        )
        conn.execute(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('created_by_app_version', ?)",
            (app_version,),
        )
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('last_opened_by_version', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (app_version, _utc_now_iso()),
        )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def open_migrated(
    db_path: Path,
    *,
    busy_timeout_ms: int = 5000,
    migrations: Sequence[Migration] | None = None,
) -> sqlite3.Connection:
    """Open a connection and assert the schema is at head.

    Raises:
        MigrationError: If the schema is behind head.
        PragmaVerificationError: If WAL or foreign keys are not confirmed.
    """
    conn = connect(db_path, busy_timeout_ms=busy_timeout_ms)
    try:
        verify_pragmas(conn)
        items = list(discover_migrations() if migrations is None else migrations)
        current = current_schema_version(conn)
        expected = head_version(items)
        if current != expected:
            raise MigrationError(
                f"Database schema is at version {current} but this build expects "
                f"{expected}. Run `python -m mom_igd db init`."
            )
        verify_applied_checksums(conn, items)
    except (MigrationError, PragmaVerificationError):
        conn.close()
        raise
    return conn


def iter_statement_counts(migrations: Iterable[Migration]) -> dict[str, int]:
    """Statement count per migration; used by tests and diagnostics."""
    return {m.label: len(m.statements) for m in migrations}
