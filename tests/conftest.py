"""Shared fixtures and the production-data-directory guard.

Two invariants are enforced here for the whole suite:

* **Every test is isolated from the developer's environment.** All ``MOM_IGD_*``
  variables are removed and ``MOM_IGD_DATA_DIR`` is pointed at a per-test
  temporary directory, so no test can depend on machine state or on the order in
  which tests run.
* **No test may touch the real runtime data directory.** A session-scoped guard
  snapshots ``D:\\MoM-IGD-Data`` before the suite and compares it afterwards. If
  anything appears, disappears or changes, the whole session fails.

Tests never need the internet, a microphone, an AI model, Docker or OpenVINO.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Iterator

import pytest

from mom_igd.config import AppConfig, load_config
from mom_igd.paths import DEFAULT_DATA_ROOT, RuntimePaths
from mom_igd.security import SessionToken

# ---------------------------------------------------------------------------
# Production data directory guard
# ---------------------------------------------------------------------------


def _snapshot(root: Path) -> tuple[Any, ...]:
    """Cheap, comparable fingerprint of a directory tree."""
    if not root.exists():
        return ("absent",)
    entries: list[tuple[str, int, int]] = []
    for path in sorted(root.rglob("*")):
        try:
            stat = path.stat()
        except OSError:  # pragma: no cover - race on an unrelated process
            continue
        entries.append(
            (str(path.relative_to(root)), stat.st_size if path.is_file() else -1, int(stat.st_mtime))
        )
    return ("present", tuple(entries))


@pytest.fixture(scope="session", autouse=True)
def production_data_dir_guard() -> Iterator[tuple[Any, ...]]:
    """Fail the session if the real runtime data directory was modified."""
    before = _snapshot(DEFAULT_DATA_ROOT)
    yield before
    after = _snapshot(DEFAULT_DATA_ROOT)
    assert after == before, (
        f"A test modified the real runtime data directory {DEFAULT_DATA_ROOT}. "
        "Tests must only ever use temporary directories."
    )


# ---------------------------------------------------------------------------
# Per-test isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="session")
def quiet_application_logging() -> Iterator[None]:
    """Keep the application's log records out of the test runner's stderr.

    Without a handler, ``logging`` falls back to writing WARNING and above
    straight to stderr. The Phase 2 capture engine legitimately logs warnings
    (dropped frames, quarantined partials, a stream that failed to stop), so a
    run would emit thousands of stderr lines. That is noise in the report, and on
    Windows it makes any shell that post-processes stderr pathologically slow.

    A ``NullHandler`` silences the fallback without suppressing ``caplog``, which
    attaches its own handler when a test asks for it.
    """
    import logging

    logger = logging.getLogger("mom_igd")
    handler = logging.NullHandler()
    logger.addHandler(handler)
    previous = logger.propagate
    logger.propagate = False
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.propagate = previous


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Strip MOM_IGD_* from the environment and point the data root at tmp_path."""
    for name in list(os.environ):
        if name.startswith("MOM_IGD_"):
            monkeypatch.delenv(name, raising=False)
    root = tmp_path / "data"
    monkeypatch.setenv("MOM_IGD_DATA_DIR", str(root))
    yield root


@pytest.fixture
def data_root(isolated_environment: Path) -> Path:
    """The temporary runtime data root for this test (not yet created)."""
    return isolated_environment


@pytest.fixture
def config(data_root: Path) -> AppConfig:
    """Validated configuration pointed at the temporary data root.

    ``use_local_file=False`` keeps the suite deterministic: a developer's
    ``config/local.toml`` must never change test outcomes.
    """
    return load_config(data_root=data_root, use_local_file=False)


@pytest.fixture
def paths(config: AppConfig) -> RuntimePaths:
    """Runtime paths with the tree actually created."""
    return config.runtime_paths().ensure()


@pytest.fixture
def db_path(config: AppConfig, paths: RuntimePaths) -> Path:
    return paths.database_path(config.database.filename)


@pytest.fixture
def conn(config: AppConfig, db_path: Path) -> Iterator[sqlite3.Connection]:
    """A migrated, WAL-enabled connection on a temporary database."""
    from mom_igd.db import apply_migrations
    from mom_igd.db.connection import connect

    connection = connect(db_path, busy_timeout_ms=config.database.busy_timeout_ms)
    apply_migrations(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def meeting_id(conn: sqlite3.Connection) -> int:
    cursor = conn.execute("INSERT INTO meetings (title) VALUES ('Rapat uji')")
    return int(cursor.lastrowid or 0)


@pytest.fixture
def token() -> SessionToken:
    return SessionToken()


@pytest.fixture
def app(config: AppConfig, paths: RuntimePaths, token: SessionToken):
    from mom_igd.api.app import create_app

    return create_app(config, session_token=token, paths=paths)


@pytest.fixture
def client(app) -> Iterator[Any]:
    """TestClient bound to a loopback base URL.

    The base URL matters: the application rejects any request whose ``Host``
    header is not a loopback name, so the default ``http://testserver`` would be
    refused -- correctly.
    """
    from starlette.testclient import TestClient

    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


@pytest.fixture
def temp_migrations(tmp_path: Path):
    """Factory for a throwaway migrations directory.

    Usage::

        directory = temp_migrations({1: "CREATE TABLE a (x INTEGER);"})
    """

    def _build(files: dict[int, str], *, names: dict[int, str] | None = None) -> Path:
        directory = tmp_path / "migrations"
        directory.mkdir(parents=True, exist_ok=True)
        for version, sql in files.items():
            name = (names or {}).get(version, f"m{version}")
            (directory / f"{version:04d}_{name}.sql").write_text(sql, encoding="utf-8")
        return directory

    return _build
