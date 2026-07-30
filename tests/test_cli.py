"""CLI surface: help, version, exit codes and command wiring.

Also asserts the repository-hygiene guarantees that belong to Phase 1.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mom_igd.cli import EXIT_CONFIG_ERROR, EXIT_FAILURE, EXIT_OK, build_parser, main
from mom_igd.paths import repo_root
from mom_igd.version import APP_VERSION

REPO = repo_root()


def _run_module(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run `python -m mom_igd ...` in a clean child process."""
    import os

    env = {k: v for k, v in os.environ.items() if not k.startswith("MOM_IGD_")}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "mom_igd", *args],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env=env,
    )


# --------------------------------------------------------------- help/version


def test_parser_builds_and_declares_every_command() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    for command in ("doctor", "db", "config", "registry", "serve", "smoke", "shell"):
        assert command in help_text


def test_no_arguments_prints_help_and_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_OK
    assert "COMMAND" in capsys.readouterr().out


def test_version_flag_reports_the_application_version() -> None:
    result = _run_module("--version")
    assert result.returncode == 0
    assert APP_VERSION in result.stdout


def test_help_flag_exits_zero() -> None:
    assert _run_module("--help").returncode == 0


def test_subcommand_help_exits_zero() -> None:
    for command in ("doctor", "db", "smoke", "serve", "registry", "config"):
        assert _run_module(command, "--help").returncode == 0, command


def test_group_command_without_subcommand_fails_clearly() -> None:
    result = _run_module("db")
    assert result.returncode == EXIT_FAILURE
    assert "requires a subcommand" in result.stderr


# ------------------------------------------------------------------- doctor


def test_doctor_from_the_repository_root_exits_zero(tmp_path: Path) -> None:
    result = _run_module("doctor", env_extra={"MOM_IGD_DATA_DIR": str(tmp_path / "d")})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Summary:" in result.stdout
    assert "0 FAIL" in result.stdout


def test_doctor_strict_exits_two_when_warnings_exist(tmp_path: Path) -> None:
    result = _run_module("doctor", "--strict", env_extra={"MOM_IGD_DATA_DIR": str(tmp_path / "d")})
    assert result.returncode == 2, result.stdout
    assert "Exit code: 2" in result.stdout


def test_doctor_json_output_is_machine_readable(tmp_path: Path) -> None:
    result = _run_module("doctor", "--json", env_extra={"MOM_IGD_DATA_DIR": str(tmp_path / "d")})
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["counts"]["FAIL"] == 0
    assert payload["exit_code"] == 0


def test_doctor_with_an_invalid_data_dir_reports_a_config_failure(tmp_path: Path) -> None:
    result = _run_module("doctor", env_extra={"MOM_IGD_DATA_DIR": "relative-path"})
    assert result.returncode == EXIT_FAILURE
    assert "FAIL" in result.stdout
    assert "absolute" in (result.stdout + result.stderr)


def test_doctor_data_dir_flag_beats_the_environment(tmp_path: Path) -> None:
    chosen = tmp_path / "from-flag"
    result = _run_module(
        "doctor",
        "--json",
        "--data-dir",
        str(chosen),
        env_extra={"MOM_IGD_DATA_DIR": str(tmp_path / "from-env")},
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    data_path = next(r for r in payload["results"] if r["key"] == "data_path")
    assert data_path["data"]["root"] == str(chosen)


def test_doctor_does_not_create_the_data_directory(tmp_path: Path) -> None:
    target = tmp_path / "untouched"
    assert _run_module("doctor", "--data-dir", str(target)).returncode == 0
    assert not target.exists()


def test_relative_data_dir_is_refused_by_the_cli(tmp_path: Path) -> None:
    result = _run_module("config", "show", "--data-dir", "still-relative")
    assert result.returncode == EXIT_CONFIG_ERROR
    assert "Configuration error" in result.stderr


# ----------------------------------------------------------------------- db


def test_db_init_creates_the_tree_and_migrates(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    result = _run_module("db", "init", "--json", "--data-dir", str(root))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["created"] is True
    assert payload["pragmas"]["journal_mode"] == "wal"
    assert payload["pragmas"]["foreign_keys"] == 1
    assert payload["status"]["up_to_date"] is True
    for name in ("db", "recordings", "exports", "logs", "models", "temp", "backups"):
        assert (root / name).is_dir()


def test_db_init_is_idempotent_across_processes(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    assert _run_module("db", "init", "--data-dir", str(root)).returncode == 0
    second = _run_module("db", "init", "--json", "--data-dir", str(root))
    assert second.returncode == 0
    assert json.loads(second.stdout)["already_up_to_date"] is True


def test_db_version_before_and_after_init(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    before = _run_module("db", "version", "--data-dir", str(root))
    assert before.returncode == EXIT_FAILURE
    assert "db init" in before.stdout

    _run_module("db", "init", "--data-dir", str(root))
    after = _run_module("db", "version", "--json", "--data-dir", str(root))
    assert after.returncode == 0
    assert json.loads(after.stdout)["up_to_date"] is True


def test_db_verify_reports_a_healthy_database(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    _run_module("db", "init", "--data-dir", str(root))
    result = _run_module("db", "verify", "--json", "--data-dir", str(root))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["audit_chain_ok"] is True
    assert payload["problems"] == []


def test_db_verify_without_a_database_fails(tmp_path: Path) -> None:
    result = _run_module("db", "verify", "--data-dir", str(tmp_path / "nope"))
    assert result.returncode == EXIT_FAILURE
    assert "does not exist" in result.stderr


# ------------------------------------------------------- config / registry


def test_config_show_prints_the_effective_configuration(tmp_path: Path) -> None:
    result = _run_module("config", "show", "--json", "--data-dir", str(tmp_path / "d"))
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["api"]["host"] == "127.0.0.1"
    assert payload["resources"]["max_heavy_workers"] == 1
    assert payload["runtime_mode"] == "offline"
    assert payload["providers"]["endpoints"] == {}


def test_config_show_contains_no_secret(tmp_path: Path) -> None:
    result = _run_module("config", "show", "--data-dir", str(tmp_path / "d"))
    assert "token" not in result.stdout.lower()


def test_registry_show_reports_the_empty_registry(tmp_path: Path) -> None:
    result = _run_module("registry", "show", "--data-dir", str(tmp_path / "d"))
    assert result.returncode == 0
    assert "Declared models   : 0" in result.stdout
    assert "Phase 4A" in result.stdout


def test_registry_show_json(tmp_path: Path) -> None:
    result = _run_module("registry", "show", "--json", "--data-dir", str(tmp_path / "d"))
    payload = json.loads(result.stdout)
    assert payload["total"] == 0
    assert payload["empty"] is True


# ----------------------------------------------------------------- smoke


def test_smoke_command_passes(tmp_path: Path) -> None:
    result = _run_module("smoke", "--json", "--data-dir", str(tmp_path / "d"))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["passed"] == payload["total"]


def test_smoke_text_output_lists_every_step(tmp_path: Path) -> None:
    result = _run_module("smoke", "--data-dir", str(tmp_path / "d"))
    assert result.returncode == 0
    assert "Smoke test: PASS" in result.stdout
    assert "clean_shutdown" in result.stdout


def test_smoke_does_not_write_into_the_repository(tmp_path: Path) -> None:
    before = {p.name for p in REPO.iterdir()}
    assert _run_module("smoke", "--data-dir", str(tmp_path / "d")).returncode == 0
    assert {p.name for p in REPO.iterdir()} == before


# ------------------------------------------------------ repository hygiene


def test_no_license_file_was_created() -> None:
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
        assert not (REPO / name).exists(), f"{name} must not exist until a licence is chosen"


def test_required_documentation_exists() -> None:
    for relative in (
        "README.md",
        "CLAUDE.md",
        "AGENTS.md",
        "docs/architecture.md",
        "docs/phase-0-summary.md",
        "docs/phase-2-audio-capture.md",
        "docs/adr/0001-native-windows-runtime.md",
        "docs/adr/0002-offline-runtime-definition.md",
        "docs/adr/0003-sqlite-and-external-runtime-data.md",
        "docs/adr/0004-single-heavy-worker-resource-policy.md",
        "docs/adr/0005-ai-provider-selection-deferred-to-phase-4a.md",
        "docs/adr/0006-capture-format-pcm16-device-native.md",
        "docs/adr/0007-chunking-checksums-and-crash-recovery.md",
        "docs/adr/0008-device-identity-and-no-silent-fallback.md",
        "docs/phase-3-participants-enrollment.md",
        "docs/phase-3-speaker-model-selection.md",
        "docs/adr/0009-participant-identity-and-append-only-consent.md",
        "docs/adr/0010-voiceprint-encryption-aes-gcm-under-dpapi.md",
        "docs/adr/0011-voiceprint-storage-crash-consistency-and-deletion.md",
        "docs/adr/0012-enrollment-capture-in-python-no-raw-audio-retention.md",
    ):
        assert (REPO / relative).is_file(), f"missing {relative}"


def test_the_capture_versus_ai_separation_is_documented_and_cross_referenced() -> None:
    """The rule spans two ADRs, so the link between them is what makes it findable.

    No separate ADR was added for it: ADR-0004 already decides that the recorder
    loads no model and never runs beside a worker, and ADR-0006 already decides
    that capture never resamples or transcribes. What was missing was the pointer
    between them.
    """
    adr = REPO / "docs/adr"
    capture = (adr / "0006-capture-format-pcm16-device-native.md").read_text(
        encoding="utf-8"
    )
    worker = (adr / "0004-single-heavy-worker-resource-policy.md").read_text(
        encoding="utf-8"
    )

    assert "Relationship to the capture / AI separation" in capture
    assert "ADR-0004" in capture, "ADR-0006 must point at the worker-isolation rule"
    assert "ADR-0006" in worker, "ADR-0004 must point back at the capture rule"
    for rule in (
        "never resamples",
        "never run concurrently",
        "normalize_audio",
    ):
        assert rule in capture, f"the separation table must state: {rule}"


def test_the_packaged_version_matches_the_code() -> None:
    """`pip show` and `--version` disagreeing is worse than having no version."""
    import tomllib

    from mom_igd.version import APP_VERSION

    packaged = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert packaged["project"]["version"] == APP_VERSION, (
        f"pyproject.toml says {packaged['project']['version']!r} but "
        f"mom_igd/version.py says {APP_VERSION!r}"
    )


def test_the_default_config_declares_the_schema_version_this_build_accepts() -> None:
    """`default.toml` is tracked, so a stale value here breaks every fresh clone."""
    import tomllib

    from mom_igd.version import CONFIG_SCHEMA_VERSION

    declared = tomllib.loads((REPO / "config/default.toml").read_text(encoding="utf-8"))
    assert declared["config_schema_version"] == CONFIG_SCHEMA_VERSION


def test_the_documented_phase_matches_the_code() -> None:
    """A stale README is how a reader learns the wrong boundary."""
    from mom_igd.version import CURRENT_PHASE

    for relative in ("README.md", "CLAUDE.md", "docs/architecture.md"):
        text = (REPO / relative).read_text(encoding="utf-8")
        assert f"phase: {CURRENT_PHASE}" in text.lower(), (
            f"{relative} does not state 'phase: {CURRENT_PHASE}'; "
            f"mom_igd/version.py says CURRENT_PHASE = {CURRENT_PHASE!r}"
        )


def test_dependency_lock_files_exist_and_are_pinned() -> None:
    for name in ("requirements.txt", "requirements-dev.txt"):
        target = REPO / name
        assert target.is_file(), f"missing {name}"
        lines = [
            line.strip()
            for line in target.read_text(encoding="utf-8").splitlines()
            if line.strip()
            and not line.strip().startswith("#")
            and not line.strip().startswith("-r ")  # include directive, not a pin
        ]
        assert lines, f"{name} declares nothing"
        for line in lines:
            assert "==" in line, f"{name} must pin exact versions, found: {line}"


def test_dev_requirements_include_the_runtime_lock() -> None:
    text = (REPO / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "-r requirements.txt" in text


def test_no_forbidden_dependency_is_pinned_in_the_lock_files() -> None:
    from mom_igd.offline_policy import audit_distribution_names

    for name in ("requirements.txt", "requirements-dev.txt"):
        pinned = [
            line.split("==")[0].strip()
            for line in (REPO / name).read_text(encoding="utf-8").splitlines()
            if "==" in line and not line.strip().startswith("#")
        ]
        findings = audit_distribution_names(pinned)
        assert findings["cloud"] == [], f"{name} pins a cloud SDK: {findings['cloud']}"
        assert findings["deferred"] == [], (
            f"{name} pins a deferred AI/audio dependency: {findings['deferred']}"
        )


def test_gitignore_protects_every_required_category() -> None:
    rules = (REPO / ".gitignore").read_text(encoding="utf-8")
    for pattern in (
        ".venv/",
        "__pycache__/",
        ".pytest_cache/",
        ".coverage",
        ".env",
        "*.log",
        "*.db",
        "*.wav",
        "*.flac",
        "*.gguf",
        "*.onnx",
        "exports/",
        "recordings/",
        "voiceprints/",
        "temp/",
        "build/",
        "dist/",
    ):
        assert pattern in rules, f".gitignore is missing {pattern}"


def test_gitattributes_protects_binary_formats() -> None:
    rules = (REPO / ".gitattributes").read_text(encoding="utf-8")
    for pattern in (
        "*.wav   binary",
        "*.flac  binary",
        "*.mp3   binary",
        "*.opus  binary",
        "*.gguf        binary",
        "*.onnx        binary",
        "*.db        binary",
        "*.png   binary",
        "*.pdf   binary",
        "*.docx  binary",
        "*.zip   binary",
    ):
        assert pattern in rules, f".gitattributes is missing {pattern!r}"
    assert "tests/fixtures/binary/**  binary" in rules


def test_no_phase_5_or_later_module_was_created() -> None:
    """Scope boundary, moved forward by one phase rather than removed.

    Phase 1 asserted that no capture package existed. Phase 2 implements capture, so
    ``mom_igd/audio`` became expected; Phase 3 added ``mom_igd/enrollment``; Phase 4 adds
    ``mom_igd/asr``. Everything downstream of it must still be absent. Deleting this test
    each time a package appeared would have thrown the guard away instead of advancing it.

    Note that VAD does **not** get its own package: it is a stage inside
    ``mom_igd/asr/vad.py`` using the Silero asset bundled in the faster-whisper wheel.
    """
    package = REPO / "mom_igd"
    allowed_now = {
        "api",
        "asr",          # Phase 4
        "audio",        # Phase 2
        "db",
        "diagnostics",
        "enrollment",   # Phase 3
        "jobs",
        "shell",
    }
    forbidden_until_later_phases = {
        "vad",              # a stage in mom_igd/asr, never its own package
        "diarization",      # Phase 5
        "speaker",          # Phase 6 -- identification, distinct from enrollment
        "reconciliation",   # Phase 7
        "llm",              # Phase 8
        "mom",              # Phase 8
        "exporters",        # Phase 10
        "review",           # Phase 9
        "providers",        # Phase 4A onwards
        "encryption",       # Phase 11 -- at-rest encryption for everything else
    }
    present = {p.name for p in package.iterdir() if p.is_dir() and not p.name.startswith("_")}

    overlap = present & forbidden_until_later_phases
    assert overlap == set(), f"packages from a later phase exist: {sorted(overlap)}"
    unexpected = present - allowed_now - forbidden_until_later_phases
    assert unexpected == set(), (
        f"unrecognised package(s) {sorted(unexpected)}: add them to `allowed_now` "
        "with the phase that introduced them, so this boundary keeps meaning "
        "something"
    )


def test_phase_3_enrollment_contains_no_identification_or_ai_runtime() -> None:
    """Phase 3 *creates* voice templates; Phase 6 compares them.

    The distinction matters because it is easy to blur: an "is this the same
    speaker?" helper added here would quietly become speaker identification without
    the calibrated thresholds, the injective assignment or the UNKNOWN outcome that
    Phase 6 is required to have. Intra-speaker consistency *within one enrollment*
    is allowed and necessary -- that is a quality check on five samples from one
    consenting person, not a decision about who spoke in a meeting.
    """
    import re

    enrollment = REPO / "mom_igd" / "enrollment"
    assert enrollment.is_dir(), "Phase 3 package is missing"

    forbidden = re.compile(
        r"\b(?:import\s+(?:torch|openvino|onnxruntime|faster_whisper|ctranslate2"
        r"|pyannote|transformers|numpy)"
        r"|from\s+(?:torch|openvino|onnxruntime|faster_whisper|ctranslate2"
        r"|pyannote|transformers|numpy)"
        r"|transcrib\w*\s*\(|diariz\w*\s*\(|identify_speaker|match_speaker"
        r"|assign_speaker|cluster_speakers)",
        re.IGNORECASE,
    )
    offenders: list[str] = []
    for path in sorted(enrollment.rglob("*.py")):
        for match in forbidden.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.name}: {match.group(0)}")
    assert offenders == [], f"identification or AI runtime in enrollment: {offenders}"


def test_phase_2_capture_engine_contains_no_speech_or_ai_code() -> None:
    """Phase 2 captures audio; it must not try to understand it.

    Level metering is allowed (it measures signal quality). Anything that decides
    *whether someone is speaking* or *who is speaking* is a later phase.
    """
    import re

    audio_package = REPO / "mom_igd" / "audio"
    forbidden = re.compile(
        r"\b(?:import\s+(?:torch|openvino|onnxruntime|faster_whisper|ctranslate2|pyannote)"
        r"|from\s+(?:torch|openvino|onnxruntime|faster_whisper|ctranslate2|pyannote)"
        r"|transcrib\w*\s*\(|diariz\w*\s*\(|speaker_id\w*\s*\(|embed_speaker)",
        re.IGNORECASE,
    )
    offenders: list[str] = []
    for path in sorted(audio_package.rglob("*.py")):
        for match in forbidden.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.name}: {match.group(0)}")
    assert offenders == [], f"speech/AI code in the capture engine: {offenders}"


def test_no_scratch_or_test_artefact_is_left_in_the_source_tree() -> None:
    skip = {".git", ".venv", "__pycache__", ".pytest_cache"}
    offenders = [
        str(p.relative_to(REPO))
        for p in REPO.rglob("*")
        if p.is_file()
        and not any(part in skip for part in p.parts)
        and (
            p.name.startswith(".mom_igd_write_probe_")
            or p.suffix in {".orig", ".rej", ".bak", ".tmp"}
            or p.name in {"probe.py", "scratch.py", "test.db"}
        )
    ]
    assert offenders == [], f"scratch files left behind: {offenders}"
