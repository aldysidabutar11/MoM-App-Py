"""The rules the minutes stage must not break, asserted mechanically.

Every one of these encodes a constraint from CLAUDE.md or an ADR. A comment saying "do not
do X" is a wish; a test that fails when somebody does X is the constraint.
"""

from __future__ import annotations

import ast
import re
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from mom_igd.db.migrator import discover_migrations, split_sql_statements

PACKAGE = Path(__file__).resolve().parents[1] / "mom_igd"
MOM = PACKAGE / "mom"
MIGRATION = PACKAGE / "db" / "migrations" / "0006_minutes.sql"


def _names(nodes) -> set[str]:
    found: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def _module_level_imports(path: Path) -> set[str]:
    """Modules imported when the file is imported. Parsed, never grepped.

    Grepping finds the word inside a docstring explaining why the import is forbidden,
    which is how a rule ends up asserting against its own documentation.

    Module level only, and the distinction is the whole point: a heavy import inside the
    function that needs it is the required pattern here, and the same import at the top of
    the file is the defect. ``ast.walk`` cannot tell them apart, so this reads ``tree.body``
    and the nested variant below reads everything.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _names(tree.body)


def _imported_modules(path: Path) -> set[str]:
    """Every module imported anywhere in the file, nested imports included."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _names(ast.walk(tree))


# ===========================================================================
# Import boundaries
# ===========================================================================


def test_nothing_in_mom_imports_enrollment() -> None:
    """Roster size must never gate a minute, exactly as it never gates capture.

    The roster is read for one purpose -- correcting the spelling of a name the meeting
    already said -- and that is done with two plain SQL columns, not the enrollment
    service. Importing it would put biometrics on the path of a text pipeline.
    """
    for path in sorted(MOM.glob("*.py")):
        offenders = {
            name for name in _imported_modules(path) if name.startswith("mom_igd.enrollment")
        }
        assert not offenders, f"{path.name} imports {offenders}"


def test_nothing_in_asr_imports_mom_at_module_level() -> None:
    """The dependency runs one way: transcription must work with the minutes stage absent.

    `tasks.py` is the shared worker entry point and does import the engine -- inside the
    task body, so an ASR worker never loads llama.cpp. At module level it would, on every
    spawn.
    """
    for path in sorted((PACKAGE / "asr").glob("*.py")):
        offenders = {
            name for name in _module_level_imports(path) if name.startswith("mom_igd.mom")
        }
        assert not offenders, f"{path.name} imports {offenders} at module level"


def test_nothing_in_audio_imports_mom() -> None:
    """Capture owns the microphone and must not depend on anything downstream of it."""
    for path in sorted((PACKAGE / "audio").glob("*.py")):
        offenders = {
            name for name in _module_level_imports(path) if name.startswith("mom_igd.mom")
        }
        assert not offenders, f"{path.name} imports {offenders}"


def test_the_verifier_uses_no_model() -> None:
    """A check performed by the same class of system that produced the claim is not a check."""
    imported = _imported_modules(MOM / "verify.py")
    for banned in ("llama_cpp", "mom_igd.mom.llm", "mom_igd.mom.generator"):
        assert banned not in imported, f"verify.py imports {banned}"


def test_the_document_renderers_use_no_model() -> None:
    for name in ("document.py", "docx.py"):
        imported = _imported_modules(MOM / name)
        assert "llama_cpp" not in imported
        assert "mom_igd.mom.llm" not in imported


def test_llama_cpp_is_imported_lazily_everywhere() -> None:
    """Importing `mom_igd.mom` must not pull a 2.3 GB engine into `doctor` or the API."""
    for path in sorted(MOM.glob("*.py")):
        assert "llama_cpp" not in _module_level_imports(path), (
            f"{path.name} imports llama_cpp at module level; it belongs inside the "
            "function that needs it, so the CLI and doctor still work without it."
        )


def test_importing_the_api_routes_pulls_in_no_heavy_library() -> None:
    """The property, checked in a fresh interpreter, rather than a list of banned names.

    An earlier version forbade importing `mom_igd.mom.pipeline` by name, as a proxy for
    "do not drag the engine in". That was the wrong test: the pipeline module is pure
    Python and importing it costs nothing, while the proxy would have to be maintained by
    hand and says nothing about what a *future* transitive import might drag in.

    Running it in a subprocess checks what actually matters -- that serving an HTTP route
    never loads 2.3 GB of weights, CTranslate2, ONNX Runtime or NumPy into the API
    process. Those belong in a worker that exits.
    """
    code = textwrap.dedent(
        """
        import sys
        import mom_igd.api.mom_routes  # noqa: F401
        heavy = sorted(
            name for name in sys.modules
            if name.split(".")[0] in {
                "llama_cpp", "ctranslate2", "faster_whisper", "onnxruntime",
                "numpy", "torch", "av", "transformers", "huggingface_hub",
            }
        )
        print(",".join(heavy))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=PACKAGE.parent,
    )
    assert result.returncode == 0, result.stderr[-800:]
    loaded = [name for name in result.stdout.strip().split(",") if name]
    assert loaded == [], f"importing the routes loaded {loaded}"


def test_the_api_route_module_does_not_import_the_engine_directly() -> None:
    imported = _module_level_imports(PACKAGE / "api" / "mom_routes.py")
    assert "llama_cpp" not in imported
    assert "mom_igd.mom.llm" not in imported


# ===========================================================================
# No speaker, anywhere
# ===========================================================================


def test_the_migration_adds_no_speaker_column() -> None:
    """Phases 5 and 6 own attribution. A column sitting NULL invites a guess into it."""
    sql = MIGRATION.read_text(encoding="utf-8")
    # Strip comments first: the migration *explains at length* why there is no speaker
    # column, and the explanation must not fail the check it exists to justify.
    body = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    for banned in ("speaker", "diariz", "voiceprint", "pembicara"):
        assert banned not in body.lower(), f"{banned!r} appears in migration 0006"


def test_no_minutes_table_has_a_speaker_column(conn: sqlite3.Connection) -> None:
    for table in ("minutes", "minute_items", "minute_exports"):
        columns = {row[1].lower() for row in conn.execute(f"PRAGMA table_info({table})")}
        assert not any("speaker" in name for name in columns), table


def test_an_owner_has_no_foreign_key_to_participants(conn: sqlite3.Connection) -> None:
    """Linking it would invite resolving an ambiguous first name to whoever is on the roster."""
    keys = list(conn.execute("PRAGMA foreign_key_list(minute_items)"))
    assert not any(str(row[2]).lower() == "participants" for row in keys), (
        "minute_items must not reference participants: an owner is text the meeting "
        "said, never an inference about who was talking."
    )


# ===========================================================================
# Migration hygiene
# ===========================================================================


def test_the_minutes_migration_is_applied_and_the_head_matches_the_constant() -> None:
    """Contiguous from 1, with 0006 present, and the database agreeing with the code.

    Not pinned to a literal. This asserted ``== 6`` and broke the moment 0007 was added
    legitimately -- the same snapshot mistake the Phase 4 suite made with ``== 5``. What
    matters is that the minutes migration exists and that nothing has drifted, and a real
    disagreement between the discovered head and ``SCHEMA_VERSION_HEAD`` still fails.
    """
    from mom_igd.version import SCHEMA_VERSION_HEAD

    versions = [migration.version for migration in discover_migrations()]
    assert versions == list(range(1, len(versions) + 1)), "migrations must be contiguous"
    assert 6 in versions, "the minutes migration is missing"
    assert max(versions) == SCHEMA_VERSION_HEAD


def test_migrations_one_to_five_are_untouched() -> None:
    """Checksums are recorded and verified. An edited migration breaks every install."""
    import subprocess

    root = Path(__file__).resolve().parents[1]
    for version in range(1, 6):
        [path] = (root / "mom_igd" / "db" / "migrations").glob(f"{version:04d}_*.sql")
        relative = path.relative_to(root).as_posix()
        result = subprocess.run(
            ["git", "diff", "--stat", "HEAD", "--", relative],
            capture_output=True,
            text=True,
            cwd=root,
        )
        if result.returncode != 0:  # pragma: no cover - not a git checkout
            pytest.skip("not a git working tree")
        assert not result.stdout.strip(), f"{relative} was modified: {result.stdout}"


def test_the_migration_splits_into_statements_without_executescript() -> None:
    """`executescript` issues an implicit COMMIT, defeating the transactional guarantee."""
    statements = split_sql_statements(MIGRATION.read_text(encoding="utf-8"))
    assert len(statements) >= 8
    assert all(statement.strip() for statement in statements)


def test_export_paths_are_relative_so_a_data_root_move_does_not_break_them() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "relative_path" in sql
    body = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    assert "absolute" not in body.lower()


# ===========================================================================
# Offline and provisioning
# ===========================================================================


def test_the_catalogue_entry_names_a_licence_and_pins_a_file() -> None:
    from mom_igd.asr.provision import MODEL_CATALOGUE

    spec = MODEL_CATALOGUE["mom-llm"]
    assert spec.kind == "llm"
    assert spec.role == "mom"
    assert spec.license_name
    assert spec.expected_files, "an unpinned file list would accept whatever the repo holds"


def test_only_provisioning_may_reach_the_network() -> None:
    """A missing model is MODEL_UNAVAILABLE. Nothing on a runtime path may fetch one."""
    for path in sorted(MOM.glob("*.py")):
        imported = _imported_modules(path)
        for banned in ("huggingface_hub", "requests", "urllib.request", "httpx"):
            assert banned not in imported, f"{path.name} imports {banned}"


def test_the_offline_flags_are_assigned_not_defaulted() -> None:
    """`setdefault` would let an operator shell carrying HF_HUB_OFFLINE=0 put a worker online."""
    source = (MOM / "llm.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "assert_llm_offline_environment"
    )
    calls = {
        node.func.attr
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "setdefault" not in calls
    assert "pop" in calls, "an inherited Hugging Face token must be deleted, not tolerated"


def test_an_inherited_token_is_removed_from_the_environment(monkeypatch) -> None:
    from mom_igd.mom.llm import assert_llm_offline_environment

    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("HF_TOKEN", "secret")
    assert_llm_offline_environment()
    import os

    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert "HF_TOKEN" not in os.environ


def test_llama_cpp_is_a_declared_pinned_dependency() -> None:
    requirements = (
        Path(__file__).resolve().parents[1] / "requirements.txt"
    ).read_text(encoding="utf-8")
    lines = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # Pinned by direct URL, because there is no PyPI wheel for win_amd64. That is a
    # tighter pin than `==`: it names one artefact rather than a version an index
    # resolves, so nobody can publish a different 0.3.34 under us.
    [llama] = [line for line in lines if line.startswith("llama-cpp-python")]
    assert llama.startswith("llama-cpp-python @ https://github.com/abetlen/"), llama
    assert "v0.3.34" in llama and llama.endswith(".whl"), llama
    assert all(
        "==" in line or "@ https://" in line for line in lines
    ), "every runtime dependency must be pinned"


def test_the_llama_wheel_hash_is_recorded_for_manual_verification() -> None:
    """pip cannot enforce it here, so the value has to be readable and findable.

    Adding `--hash=` to one line switches pip into --require-hashes for the whole file,
    and hash-pinning the full closure is Phase 11's offline-wheelhouse work. Until then
    the expected digest is a comment beside the requirement, which is worth nothing if
    it quietly disappears.
    """
    text = (
        Path(__file__).resolve().parents[1] / "requirements.txt"
    ).read_text(encoding="utf-8")
    assert "6526fff614e5ef7e439e6369e076a78073e45e1d791dbe1d5e5d42661f46ca1a" in text
    assert "Get-FileHash" in text, "the comment must say how to check it"


def test_llama_cpp_is_no_longer_on_the_deferred_list() -> None:
    from mom_igd.offline_policy import DEFERRED_HEAVY_DISTRIBUTIONS

    assert "llama-cpp-python" not in DEFERRED_HEAVY_DISTRIBUTIONS
    # These would drag torch in behind them and are still deferred.
    assert "transformers" in DEFERRED_HEAVY_DISTRIBUTIONS
    assert "torch" in DEFERRED_HEAVY_DISTRIBUTIONS


# ===========================================================================
# Service refusals
# ===========================================================================


def _service(config, paths, connect):
    from mom_igd.mom.service import MinutesService

    return MinutesService(connect, config=config, paths=paths)


@pytest.mark.parametrize("state", ["PREFLIGHT", "ARMED", "RECORDING", "PAUSED",
                                   "STOPPING", "FINALIZING"])
def test_generation_is_refused_while_a_capture_is_live(
    config, paths, conn, db_path, meeting_id, state
) -> None:
    """Every live state, not just RECORDING.

    And a capture is never refused because a minute is running: the asymmetry is
    deliberate, because the operator must always be able to record the next meeting.
    """
    from mom_igd.db.connection import connect
    from mom_igd.mom.service import RecordingInProgressError

    conn.execute(
        "INSERT INTO recordings (meeting_id, recording_uuid, relative_dir, status) "
        "VALUES (?, '22222222-2222-4222-8222-222222222222', 'rec/2', ?)",
        (meeting_id, state),
    )
    conn.commit()
    # A factory, not a shared handle: the service closes what it opens, which is the
    # production contract and the reason it never holds a connection between calls.
    service = _service(config, paths, lambda: connect(db_path))
    assert service.active_capture() == "22222222-2222-4222-8222-222222222222"
    with pytest.raises(RecordingInProgressError):
        service.generate("11111111-1111-4111-8111-111111111111")


def test_the_live_capture_states_match_the_migration_partial_index() -> None:
    """Two lists of "what counts as recording" would drift, and one of them silently."""
    from mom_igd.mom.service import ACTIVE_CAPTURE_STATES

    sql = (PACKAGE / "db" / "migrations" / "0002_audio_capture.sql").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"CREATE UNIQUE INDEX[^;]*?recordings[^;]*?WHERE\s+status\s+IN\s*\(([^)]*)\)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert match, "could not find the partial unique index on recordings in 0002"
    in_sql = set(re.findall(r"'([A-Z_]+)'", match.group(1)))
    assert set(ACTIVE_CAPTURE_STATES) == in_sql, (
        f"service says {sorted(ACTIVE_CAPTURE_STATES)}, migration says {sorted(in_sql)}"
    )


def test_generation_is_refused_when_the_stage_is_switched_off(
    config, paths, conn, db_path
) -> None:
    from mom_igd.mom.service import MinutesServiceError

    disabled = config.model_copy(
        update={"mom": config.mom.model_copy(update={"enabled": False})}
    )
    from mom_igd.db.connection import connect

    service = _service(disabled, paths, lambda: connect(db_path))
    with pytest.raises(MinutesServiceError, match="dimatikan"):
        service.generate("11111111-1111-4111-8111-111111111111")


def test_an_unknown_export_format_is_refused(conn) -> None:
    from mom_igd.mom.pipeline import MinutesPipelineError, export_minute

    with pytest.raises(MinutesPipelineError, match="tidak dikenal"):
        export_minute(conn, paths=None, minute_id=1, export_format="pdf")


# ===========================================================================
# Shell allowlist
# ===========================================================================


def test_every_minutes_path_the_page_uses_is_allowlisted() -> None:
    from mom_igd.shell.launcher import (
        ALLOWED_GET_PATTERNS,
        ALLOWED_POST_PATHS,
        ALLOWED_PROXY_PATHS,
    )

    script = (PACKAGE / "shell" / "web" / "app.js").read_text(encoding="utf-8")
    literals = set(re.findall(r"""['"](/mom/[a-z/]*)['"]""", script))
    templated = set(re.findall(r"""['"](/mom/[a-z]+/)['"]\s*\+""", script))

    for path in literals - templated:
        assert path in ALLOWED_PROXY_PATHS or path in ALLOWED_POST_PATHS, (
            f"the page calls {path}, which the shell proxy would refuse"
        )
    for prefix in templated:
        sample = prefix + "11111111-1111-4111-8111-111111111111"
        assert any(pattern.match(sample) for pattern in ALLOWED_GET_PATTERNS), (
            f"the page calls {prefix}<uuid>, which no allowlist pattern matches"
        )


def test_the_allowlist_has_no_minutes_wildcard() -> None:
    """A prefix wildcard would admit every route added later without thought."""
    from mom_igd.shell.launcher import ALLOWED_GET_PATTERNS, ALLOWED_POST_PATTERNS

    for pattern in (*ALLOWED_GET_PATTERNS, *ALLOWED_POST_PATTERNS):
        if "/mom/" not in pattern.pattern:
            continue
        assert pattern.pattern.startswith("^") and pattern.pattern.endswith("$")
        assert ".*" not in pattern.pattern and "[0-9a-f]{8}" in pattern.pattern
