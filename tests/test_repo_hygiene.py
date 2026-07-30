"""Repository hygiene: source is tracked, runtime artefacts never are.

This file exists because of a specific near-miss. Phase 1's ``.gitignore`` carried
an unanchored ``audio/`` pattern intended for runtime recordings. When Phase 2
added the ``mom_igd/audio/`` package, git silently ignored the entire capture
engine: ``git status`` showed nothing, and a commit would have dropped the code.

A comment cannot prevent that recurring; these tests can.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mom_igd.paths import repo_root

REPO = repo_root()
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "htmlcov"}


def _check_ignored(paths: list[str]) -> set[str]:
    """Return the subset of ``paths`` that git would ignore."""
    if not paths:
        return set()
    result = subprocess.run(
        ["git", "check-ignore", "--"] + paths,
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    return {line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()}


def _is_ignored(path: str) -> bool:
    return (
        subprocess.run(
            ["git", "check-ignore", "-q", "--", path], cwd=str(REPO)
        ).returncode
        == 0
    )


# ------------------------------------------------------- source stays tracked


def test_no_packaged_python_source_is_ignored() -> None:
    sources = sorted(
        p.relative_to(REPO).as_posix()
        for p in (REPO / "mom_igd").rglob("*.py")
        if "__pycache__" not in p.parts
    )
    assert sources, "the package must contain source files"
    ignored = _check_ignored(sources)
    assert ignored == set(), f"packaged source is git-ignored: {sorted(ignored)}"


def test_the_audio_package_is_source_not_runtime_data() -> None:
    package = REPO / "mom_igd" / "audio"
    assert package.is_dir()
    modules = sorted(p.name for p in package.glob("*.py"))
    assert "backend.py" in modules and "session.py" in modules
    assert not _is_ignored("mom_igd/audio/backend.py")
    assert not _is_ignored("mom_igd/audio/")


def test_no_test_module_is_ignored() -> None:
    tests = sorted(
        p.relative_to(REPO).as_posix()
        for p in (REPO / "tests").rglob("*.py")
        if "__pycache__" not in p.parts
    )
    ignored = _check_ignored(tests)
    assert ignored == set(), f"test source is git-ignored: {sorted(ignored)}"


def test_migrations_and_static_assets_are_tracked() -> None:
    assets = [
        "mom_igd/db/migrations/0001_initial.sql",
        "mom_igd/db/migrations/0002_audio_capture.sql",
        "mom_igd/shell/web/index.html",
        "mom_igd/shell/web/app.css",
        "mom_igd/shell/web/app.js",
        "config/default.toml",
        "models/registry.json",
    ]
    for asset in assets:
        assert (REPO / asset).is_file(), asset
    assert _check_ignored(assets) == set()


# ------------------------------------------- runtime artefacts stay ignored


@pytest.mark.parametrize(
    "artefact",
    [
        "recordings/meeting/rec/chunk_000000.wav",
        "mom_igd/audio/accidental.wav",
        "some/deep/path/chunk_000001.wav",
        "r/chunk_000000.pcm.part",
        "r/chunk_000000.wav.tmp",
        "r/chunk_000000.meta.json.tmp",
        "data/mom_igd.db",
        "db/mom_igd.db-wal",
        "logs/mom_igd.log",
        "exports/minutes.mom.pdf",
        "voiceprints/person.emb",
        "temp/scratch.tmp",
        "models/whisper-small.gguf",
        "capture.pcm",
        "capture.raw",
        # Phase 3 biometric material, in and out of its own directory. The
        # anchored `/voiceprints/` rule only covers the repository root, so the
        # extension rules have to carry the rest: a sealed envelope copied
        # somewhere else while debugging was committable until these were added.
        "voiceprints/0189d3f1-1c2e-4a5b-8c7d-9e0f1a2b3c4d.vpx",
        "mom_igd/enrollment/leaked.vpx",
        "some/deep/path/sample.vpx",
        "voiceprints/0189d3f1-1c2e-4a5b-8c7d-9e0f1a2b3c4d.vpx.tmp",
        "keys/voiceprint_master.dpapi",
        "mom_igd/leaked.dpapi",
    ],
)
def test_runtime_artefacts_are_ignored(artefact: str) -> None:
    assert _is_ignored(artefact), f"{artefact} would be committed"


def test_no_audio_or_runtime_artefact_exists_in_the_tree() -> None:
    forbidden = {
        ".wav",
        ".flac",
        ".mp3",
        ".m4a",
        ".ogg",
        ".opus",
        ".pcm",
        ".raw",
        ".part",
        ".db",
        ".db-wal",
        ".db-shm",
        ".sqlite",
        ".sqlite3",
        ".log",
        ".gguf",
        ".onnx",
        ".pt",
        ".safetensors",
        ".vpx",
        ".dpapi",
        ".emb",
        ".embedding",
        ".voiceprint",
    }
    offenders = [
        p.relative_to(REPO).as_posix()
        for p in REPO.rglob("*")
        if p.is_file()
        and not any(part in SKIP_DIRS for part in p.parts)
        and p.suffix.lower() in forbidden
    ]
    assert offenders == [], f"runtime artefacts in the source tree: {offenders}"


def test_no_manifest_or_quarantine_directory_in_the_tree() -> None:
    offenders = [
        p.relative_to(REPO).as_posix()
        for p in REPO.rglob("*")
        if not any(part in SKIP_DIRS for part in p.parts)
        and (p.name in {"manifest.jsonl", "manifest.json", "quarantine"})
    ]
    assert offenders == [], f"recording artefacts in the source tree: {offenders}"


def test_runtime_directory_patterns_stay_anchored() -> None:
    """Anchored so they can never shadow a source package of the same name."""
    rules = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    stripped = [line.strip() for line in rules]
    for name in (
        "audio",
        "recordings",
        "logs",
        "temp",
        "exports",
        "backups",
        "voiceprints",
        "keys",
        "data",
    ):
        assert f"{name}/" not in stripped, (
            f"'{name}/' is unanchored and would match mom_igd/{name}/ as well; "
            f"use '/{name}/'"
        )
        assert f"/{name}/" in stripped, f"'/{name}/' must stay ignored at the repo root"


def test_gitattributes_marks_capture_artefacts_binary() -> None:
    rules = (REPO / ".gitattributes").read_text(encoding="utf-8")
    for pattern in ("*.wav   binary", "*.flac  binary", "*.raw   binary", "*.pcm   binary"):
        assert pattern in rules, pattern


def test_gitattributes_marks_biometric_artefacts_binary() -> None:
    """`* text=auto` would let Git guess, and a guess corrupts ciphertext.

    These files must never be committed at all; this is the layer that makes a
    mistaken ``git add -f`` produce a byte-exact file rather than one silently
    mangled by CRLF translation.
    """
    rules = (REPO / ".gitattributes").read_text(encoding="utf-8")
    for pattern in ("*.vpx     binary", "*.dpapi   binary"):
        assert pattern in rules, pattern


def test_no_license_file_was_added() -> None:
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
        assert not (REPO / name).exists(), name
