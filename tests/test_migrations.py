"""Migration runner: determinism, idempotency, rollback, tamper detection.

Covers Phase 1 test categories 11 and 12.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mom_igd.db.connection import connect
from mom_igd.db.migrator import (
    MIGRATIONS_DIR,
    MigrationError,
    apply_migrations,
    current_schema_version,
    discover_migrations,
    head_version,
    initialize_database,
    migration_status,
    open_migrated,
    split_sql_statements,
    verify_applied_checksums,
)
from mom_igd.version import SCHEMA_VERSION_HEAD


@pytest.fixture
def blank_conn(paths, config) -> sqlite3.Connection:
    connection = connect(
        paths.database_path("blank.db"), busy_timeout_ms=config.database.busy_timeout_ms
    )
    yield connection
    connection.close()


# ------------------------------------------------------------------ discovery


def test_shipped_migrations_are_discovered_and_contiguous() -> None:
    migrations = discover_migrations()
    assert migrations, "at least one migration must ship"
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert head_version(migrations) == SCHEMA_VERSION_HEAD


def test_shipped_migration_directory_is_inside_the_package() -> None:
    assert MIGRATIONS_DIR.is_dir()
    assert MIGRATIONS_DIR.name == "migrations"


def test_checksum_ignores_line_ending_differences(tmp_path: Path, temp_migrations) -> None:
    """core.autocrlf=true must not change a migration checksum."""
    lf = temp_migrations({1: "CREATE TABLE a (x INTEGER);\n"})
    lf_checksum = discover_migrations(lf)[0].checksum
    crlf_dir = tmp_path / "crlf"
    crlf_dir.mkdir()
    (crlf_dir / "0001_m1.sql").write_bytes(b"CREATE TABLE a (x INTEGER);\r\n")
    assert discover_migrations(crlf_dir)[0].checksum == lf_checksum


def test_malformed_filename_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "m"
    directory.mkdir()
    (directory / "initial.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="does not match"):
        discover_migrations(directory)


def test_version_gap_is_rejected(temp_migrations) -> None:
    directory = temp_migrations({1: "CREATE TABLE a (x INTEGER);", 3: "CREATE TABLE b (x INTEGER);"})
    with pytest.raises(MigrationError, match="contiguous"):
        discover_migrations(directory)


def test_duplicate_version_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "m"
    directory.mkdir()
    (directory / "0001_a.sql").write_text("CREATE TABLE a (x INTEGER);", encoding="utf-8")
    (directory / "0001_b.sql").write_text("CREATE TABLE b (x INTEGER);", encoding="utf-8")
    with pytest.raises(MigrationError, match="Duplicate migration version"):
        discover_migrations(directory)


def test_missing_directory_is_reported(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="not found"):
        discover_migrations(tmp_path / "nowhere")


# --------------------------------------------------------- 11. idempotency


def test_first_apply_reports_what_it_applied(blank_conn: sqlite3.Connection) -> None:
    applied = apply_migrations(blank_conn)
    assert [m.version for m in applied] == [m.version for m in discover_migrations()]
    assert current_schema_version(blank_conn) == SCHEMA_VERSION_HEAD


def test_reapplying_is_a_noop(blank_conn: sqlite3.Connection) -> None:
    apply_migrations(blank_conn)
    before = migration_status(blank_conn)
    assert apply_migrations(blank_conn) == []
    assert apply_migrations(blank_conn) == []
    after = migration_status(blank_conn)
    assert after["current_version"] == before["current_version"]
    assert after["applied"] == before["applied"], "re-running must not rewrite history"


def test_status_reports_up_to_date(blank_conn: sqlite3.Connection) -> None:
    apply_migrations(blank_conn)
    status = migration_status(blank_conn)
    assert status["up_to_date"] is True
    assert status["pending"] == []
    assert status["current_version"] == status["head_version"]


def test_user_version_mirrors_the_schema_version(blank_conn: sqlite3.Connection) -> None:
    apply_migrations(blank_conn)
    assert blank_conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION_HEAD


def test_initialize_database_is_idempotent(paths, config) -> None:
    target = paths.database_path("init.db")
    first = initialize_database(target, app_version=config.app_version)
    second = initialize_database(target, app_version=config.app_version)
    assert first["created"] is True
    assert second["created"] is False
    assert second["already_up_to_date"] is True
    assert second["status"]["up_to_date"] is True
    assert first["pragmas"]["journal_mode"] == "wal"


def test_initialize_database_seeds_provenance(paths, config) -> None:
    target = paths.database_path("seed.db")
    initialize_database(target, app_version=config.app_version)
    connection = connect(target)
    try:
        rows = dict(
            connection.execute("SELECT key, value FROM app_settings").fetchall()  # type: ignore[arg-type]
        )
        assert rows["created_by_app_version"] == config.app_version
        assert rows["last_opened_by_version"] == config.app_version
        assert rows["db_created_at"].endswith("Z")
    finally:
        connection.close()


# ------------------------------------------------ 12. failure rolls back fully


def test_failing_migration_does_not_advance_the_version(
    blank_conn: sqlite3.Connection, temp_migrations
) -> None:
    directory = temp_migrations(
        {
            1: "CREATE TABLE good_one (x INTEGER);",
            2: "CREATE TABLE this_is_not_valid_sql (((;",
        }
    )
    migrations = discover_migrations(directory)

    with pytest.raises(MigrationError) as excinfo:
        apply_migrations(blank_conn, migrations)

    assert "0002_m2" in str(excinfo.value)
    assert current_schema_version(blank_conn) == 1, "version must stay at the last good migration"
    tables = {
        row["name"]
        for row in blank_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "good_one" in tables
    recorded = {int(row["version"]) for row in migration_status(blank_conn, migrations)["applied"]}
    assert recorded == {1}


def test_partially_failing_migration_rolls_back_its_own_earlier_statements(
    blank_conn: sqlite3.Connection, temp_migrations
) -> None:
    """A migration is atomic: statement 1 must not survive a failure in statement 2."""
    directory = temp_migrations(
        {
            1: (
                "CREATE TABLE first_half (x INTEGER);\n"
                "CREATE TABLE second_half (y INTEGER, FOREIGN KEY (y) REFERENCES nope(id));\n"
                "INSERT INTO second_half (y) VALUES (1);\n"
                "SELECT this_column_does_not_exist FROM first_half;\n"
            )
        }
    )
    migrations = discover_migrations(directory)

    with pytest.raises(MigrationError):
        apply_migrations(blank_conn, migrations)

    tables = {
        row["name"]
        for row in blank_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "first_half" not in tables, "the whole migration must roll back, not just the failure"
    assert "second_half" not in tables
    assert current_schema_version(blank_conn) == 0


def test_empty_migration_is_rejected(blank_conn: sqlite3.Connection, temp_migrations) -> None:
    directory = temp_migrations({1: "-- only a comment\n"})
    with pytest.raises(MigrationError, match="no statements"):
        apply_migrations(blank_conn, discover_migrations(directory))


# ------------------------------------------------------- tamper detection


def test_editing_an_applied_migration_is_detected(
    blank_conn: sqlite3.Connection, temp_migrations, tmp_path: Path
) -> None:
    directory = temp_migrations({1: "CREATE TABLE a (x INTEGER);"})
    apply_migrations(blank_conn, discover_migrations(directory))

    (directory / "0001_m1.sql").write_text(
        "CREATE TABLE a (x INTEGER, y TEXT);", encoding="utf-8"
    )
    edited = discover_migrations(directory)

    with pytest.raises(MigrationError, match="modified after it was applied"):
        verify_applied_checksums(blank_conn, edited)
    with pytest.raises(MigrationError, match="modified after it was applied"):
        apply_migrations(blank_conn, edited)


def test_unknown_recorded_migration_is_detected(
    blank_conn: sqlite3.Connection, temp_migrations
) -> None:
    directory = temp_migrations({1: "CREATE TABLE a (x INTEGER);"})
    apply_migrations(blank_conn, discover_migrations(directory))
    # Simulate a database created by a build that had more migrations.
    blank_conn.execute(
        "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
        "VALUES (2, 'from_the_future', 'x', '2026-01-01T00:00:00.000Z')"
    )
    with pytest.raises(MigrationError, match="no such"):
        verify_applied_checksums(blank_conn, discover_migrations(directory))


def test_open_migrated_refuses_a_stale_schema(paths, config, temp_migrations) -> None:
    target = paths.database_path("stale.db")
    directory = temp_migrations({1: "CREATE TABLE a (x INTEGER);"})
    one = discover_migrations(directory)
    connection = connect(target)
    apply_migrations(connection, one)
    connection.close()

    two = temp_migrations(
        {1: "CREATE TABLE a (x INTEGER);", 2: "CREATE TABLE b (y INTEGER);"}
    )
    with pytest.raises(MigrationError, match="expects"):
        open_migrated(target, migrations=discover_migrations(two))


def test_open_migrated_succeeds_at_head(paths, config) -> None:
    target = paths.database_path("head.db")
    initialize_database(target, app_version=config.app_version)
    connection = open_migrated(target)
    try:
        assert current_schema_version(connection) == SCHEMA_VERSION_HEAD
    finally:
        connection.close()


# --------------------------------------------------- SQL statement splitter


def test_splitter_handles_comments_and_string_literals() -> None:
    sql = """
    -- a leading comment with a ; semicolon
    CREATE TABLE t (a TEXT DEFAULT 'semi;colon', b TEXT DEFAULT 'it''s fine');
    /* block ; comment */
    INSERT INTO t (a) VALUES ('x;y');
    """
    statements = split_sql_statements(sql)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE")
    assert "it''s fine" in statements[0]
    assert statements[1].startswith("INSERT INTO")


def test_splitter_keeps_a_trigger_body_together() -> None:
    sql = """
    CREATE TABLE t (a INTEGER, b INTEGER);
    CREATE TRIGGER trg AFTER INSERT ON t
    BEGIN
        UPDATE t SET b = 1 WHERE a = NEW.a;
        UPDATE t SET b = 2 WHERE a = NEW.a;
    END;
    SELECT 1;
    """
    statements = split_sql_statements(sql)
    assert len(statements) == 3
    assert statements[1].startswith("CREATE TRIGGER")
    assert statements[1].rstrip().endswith("END")
    assert statements[1].count("UPDATE t SET") == 2


def test_splitter_does_not_treat_case_end_as_a_trigger_body() -> None:
    sql = "SELECT CASE WHEN 1 THEN 'a' ELSE 'b' END AS x; SELECT 2;"
    assert len(split_sql_statements(sql)) == 2


def test_splitter_handles_quoted_identifiers() -> None:
    sql = 'CREATE TABLE "odd;name" (x INTEGER); CREATE TABLE [br;acket] (y INTEGER);'
    statements = split_sql_statements(sql)
    assert len(statements) == 2


def test_splitter_ignores_trailing_whitespace_and_empty_statements() -> None:
    assert split_sql_statements("SELECT 1;;;   \n\n") == ["SELECT 1"]
    assert split_sql_statements("   \n  ") == []


def test_shipped_migration_splits_into_executable_statements() -> None:
    migration = discover_migrations()[0]
    statements = migration.statements
    assert len(statements) > 10
    assert all(statement.strip() for statement in statements)
    assert not any(statement.strip().startswith("--") for statement in statements)
    creates = [s for s in statements if s.upper().startswith("CREATE TABLE")]
    assert len(creates) == 8, "0001 must create exactly the eight non-meta Phase 1 tables"
