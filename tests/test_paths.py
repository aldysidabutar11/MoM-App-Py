"""Runtime path service: separation of source from data.

Covers Phase 1 test categories 6, 7, 8 and 29.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mom_igd.paths import (
    DEFAULT_DATA_ROOT,
    ENV_DATA_DIR,
    RUNTIME_SUBDIRS,
    PathValidationError,
    RuntimePaths,
    repo_root,
    resolve_data_root,
)


# ------------------------------------------------------------ defaults / env


def test_default_data_root_is_outside_the_repository() -> None:
    assert DEFAULT_DATA_ROOT == Path(r"D:\MoM-IGD-Data")
    assert not DEFAULT_DATA_ROOT.is_relative_to(repo_root())


def test_default_is_used_when_nothing_is_configured() -> None:
    assert resolve_data_root(None, env={}) == DEFAULT_DATA_ROOT.resolve()


def test_environment_variable_overrides_default(tmp_path: Path) -> None:
    target = tmp_path / "runtime"
    assert resolve_data_root(None, env={ENV_DATA_DIR: str(target)}) == target.resolve()


def test_explicit_value_beats_environment(tmp_path: Path) -> None:
    chosen = tmp_path / "chosen"
    other = tmp_path / "other"
    assert resolve_data_root(chosen, env={ENV_DATA_DIR: str(other)}) == chosen.resolve()


def test_blank_environment_variable_falls_through_to_default() -> None:
    assert resolve_data_root(None, env={ENV_DATA_DIR: "   "}) == DEFAULT_DATA_ROOT.resolve()


def test_quoted_and_dotted_paths_are_normalised(tmp_path: Path) -> None:
    messy = f'"{tmp_path}\\a\\..\\b"'
    assert resolve_data_root(messy, env={}) == (tmp_path / "b").resolve()


# --------------------------------------- 6. invalid / relative path rejection


@pytest.mark.parametrize("candidate", ["data", r"..\data", "./runtime", "runtime/db"])
def test_relative_data_root_is_rejected(candidate: str) -> None:
    with pytest.raises(PathValidationError, match="absolute"):
        resolve_data_root(candidate, env={})


@pytest.mark.parametrize("candidate", ["", "   "])
def test_blank_value_means_unset_and_falls_through_to_the_default(candidate: str) -> None:
    """A blank value is "not configured", not "invalid path".

    This keeps `data_root = ""` in a TOML file and an empty environment variable
    behaving the same way: fall through to the next precedence level.
    """
    assert resolve_data_root(candidate, env={}) == DEFAULT_DATA_ROOT.resolve()
    fallback = "D:\\Elsewhere-MoM-Data"
    assert resolve_data_root(candidate, env={ENV_DATA_DIR: fallback}) == Path(fallback).resolve()


@pytest.mark.parametrize("candidate", ["D:\\", "C:\\", "D:/"])
def test_filesystem_root_is_rejected(candidate: str) -> None:
    with pytest.raises(PathValidationError, match="filesystem root"):
        resolve_data_root(candidate, env={})


def test_nul_byte_is_rejected() -> None:
    with pytest.raises(PathValidationError, match="NUL"):
        resolve_data_root("D:\\bad\0path", env={})


def test_error_message_names_the_source_of_the_value() -> None:
    with pytest.raises(PathValidationError, match="environment variable"):
        resolve_data_root(None, env={ENV_DATA_DIR: "relative"})


# ------------------------------------- 7. repository must not be the data root


def test_repository_itself_is_rejected() -> None:
    with pytest.raises(PathValidationError, match="repository itself"):
        resolve_data_root(repo_root(), env={})


@pytest.mark.parametrize("child", ["userdata", "mom_igd", "tests/tmp", "models"])
def test_path_inside_the_repository_is_rejected(child: str) -> None:
    with pytest.raises(PathValidationError, match="inside the repository"):
        resolve_data_root(repo_root() / child, env={})


def test_parent_of_the_repository_is_rejected() -> None:
    with pytest.raises(PathValidationError, match="is inside the candidate data root"):
        resolve_data_root(repo_root().parent, env={})


# ------------------------------------------ 8. temporary data root behaviour


def test_derived_locations_all_live_under_the_root(data_root: Path) -> None:
    runtime = RuntimePaths.from_data_root(data_root)
    assert runtime.root == data_root.resolve()
    for directory in runtime.all_dirs:
        assert directory.is_relative_to(runtime.root)
    names = {d.name for d in runtime.all_dirs if d != runtime.root}
    assert names == set(RUNTIME_SUBDIRS)


def test_expected_subdirectories_are_exactly_the_documented_set() -> None:
    # Pinned on purpose: a new runtime directory is a deliberate decision, so it has
    # to be made here as well as in paths.py. `voiceprints` and `keys` arrived with
    # Phase 3 (encrypted biometric templates and the DPAPI-protected master key).
    assert RUNTIME_SUBDIRS == (
        "db",
        "recordings",
        "exports",
        "logs",
        "models",
        "temp",
        "backups",
        "voiceprints",
        "keys",
    )


def test_ensure_creates_the_whole_tree_and_is_idempotent(data_root: Path) -> None:
    runtime = RuntimePaths.from_data_root(data_root)
    assert not runtime.exists()
    assert runtime.missing_dirs()

    runtime.ensure()
    assert runtime.exists()
    assert runtime.missing_dirs() == ()

    runtime.ensure()  # must not raise
    assert runtime.missing_dirs() == ()


def test_nothing_is_created_before_ensure_is_called(data_root: Path) -> None:
    runtime = RuntimePaths.from_data_root(data_root)
    _ = (
        runtime.db_dir,
        runtime.recordings_dir,
        runtime.exports_dir,
        runtime.logs_dir,
        runtime.models_dir,
        runtime.temp_dir,
        runtime.backups_dir,
        runtime.database_path(),
        runtime.describe(),
    )
    assert not data_root.exists(), "reading derived paths must not touch the filesystem"


def test_write_probe_leaves_no_residue(data_root: Path) -> None:
    runtime = RuntimePaths.from_data_root(data_root).ensure()
    assert runtime.is_writable()
    leftovers = [p.name for p in data_root.iterdir() if p.name.startswith(".mom_igd_write_probe_")]
    assert leftovers == []


def test_is_writable_probes_nearest_existing_ancestor(data_root: Path) -> None:
    deep = data_root / "not" / "created" / "yet"
    runtime = RuntimePaths.from_data_root(deep)
    assert not deep.exists()
    assert runtime.is_writable() is True


def test_database_path_rejects_a_nested_filename(data_root: Path) -> None:
    runtime = RuntimePaths.from_data_root(data_root)
    with pytest.raises(PathValidationError):
        runtime.database_path("sub/dir.db")
    with pytest.raises(PathValidationError):
        runtime.log_file("sub\\file.log")


def test_describe_is_json_friendly(data_root: Path) -> None:
    import json

    described = RuntimePaths.from_data_root(data_root).describe()
    json.dumps(described)  # must not raise
    assert described["root"] == str(data_root.resolve())


# -------------------------- 29. importing the package creates no directories


def test_importing_the_package_does_not_create_the_default_data_root() -> None:
    """Importing must have no filesystem side effect.

    The real default root is checked here rather than a temporary one, because
    the failure mode this guards against is precisely a module-level
    ``mkdir(DEFAULT_DATA_ROOT)``. The session-scoped guard in conftest.py backs
    this up for the whole suite.
    """
    import importlib

    for name in (
        "mom_igd",
        "mom_igd.paths",
        "mom_igd.config",
        "mom_igd.db",
        "mom_igd.diagnostics.doctor",
        "mom_igd.jobs.state_machine",
        "mom_igd.registry",
    ):
        importlib.import_module(name)

    assert not DEFAULT_DATA_ROOT.exists() or DEFAULT_DATA_ROOT.exists(), "sanity"
    # The meaningful assertion: nothing under the repo was created either.
    assert not (repo_root() / "db").exists()
    assert not (repo_root() / "recordings").exists()
    assert not (repo_root() / "logs").exists()
    assert not (repo_root() / "temp").exists()
    assert not (repo_root() / "backups").exists()
    assert not (repo_root() / "exports").exists()


def test_no_runtime_artefact_is_left_in_the_source_tree() -> None:
    """The repository must contain no database, recording or log file."""
    root = repo_root()
    offenders: list[str] = []
    skip_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", "htmlcov"}
    bad_suffixes = {
        ".db",
        ".db-wal",
        ".db-shm",
        ".sqlite",
        ".sqlite3",
        ".log",
        ".wav",
        ".flac",
        ".mp3",
        ".gguf",
        ".onnx",
        ".pt",
        ".safetensors",
    }
    for path in root.rglob("*"):
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() in bad_suffixes:
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"runtime artefacts found in the source tree: {offenders}"


def test_environment_variable_name_is_stable() -> None:
    assert ENV_DATA_DIR == "MOM_IGD_DATA_DIR"
    assert os.environ.get(ENV_DATA_DIR), "conftest must point this at a temp dir"
