"""Reduced, standard-library-only diagnostics.

The property under test: `doctor` must produce a useful report on an interpreter
where the Phase 1 runtime dependencies are absent, instead of raising
`ModuleNotFoundError`. That is what makes `py -3.12 -m mom_igd doctor` usable
straight from the repository root on an unprepared machine.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mom_igd.cli import EXIT_FAILURE, main
from mom_igd.diagnostics.bootstrap import (
    REQUIRED_RUNTIME_MODULES,
    missing_runtime_modules,
    run_bootstrap_doctor,
)
from mom_igd.diagnostics.model import DoctorReport, Status, format_report
from mom_igd.paths import DEFAULT_DATA_ROOT

SIMULATED_MISSING = [
    ("pydantic", "configuration and model-registry validation"),
    ("fastapi", "local loopback API"),
]


def _by_key(report: DoctorReport) -> dict[str, Status]:
    return {result.key: result.status for result in report.results}


# ------------------------------------------------------- module-level purity


def test_the_model_module_imports_nothing_third_party() -> None:
    """`model` and `bootstrap` must stay standard-library only."""
    code = (
        "import sys;"
        "import mom_igd.diagnostics.bootstrap;"
        "third=[m for m in sys.modules if m.split('.')[0] in "
        "{'pydantic','pydantic_core','fastapi','starlette','uvicorn','psutil','webview','httpx'}];"
        "print(','.join(sorted(third)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "", f"bootstrap pulled in {result.stdout.strip()}"


def test_importing_the_diagnostics_package_does_not_import_pydantic() -> None:
    code = (
        "import sys, mom_igd.diagnostics;"
        "print('pydantic' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


# ------------------------------------------------------- dependency probing


def test_no_runtime_dependency_is_missing_in_the_project_venv() -> None:
    assert missing_runtime_modules() == []


def test_the_required_module_list_matches_the_declared_dependencies() -> None:
    modules = {module for module, _ in REQUIRED_RUNTIME_MODULES}
    # `webview` is the import name of the pywebview distribution.
    assert modules == {"pydantic", "psutil", "fastapi", "uvicorn", "webview"}
    for _, purpose in REQUIRED_RUNTIME_MODULES:
        assert purpose, "each dependency must say what it is needed for"


# ------------------------------------------------------------ report content


@pytest.fixture
def bootstrap_report(data_root: Path) -> DoctorReport:
    return run_bootstrap_doctor(data_root, missing=SIMULATED_MISSING)


def test_report_is_marked_as_a_reduced_run(bootstrap_report: DoctorReport) -> None:
    assert bootstrap_report.mode == "bootstrap"
    assert "REDUCED" in format_report(bootstrap_report)


def test_report_covers_what_can_be_checked_without_dependencies(
    bootstrap_report: DoctorReport,
) -> None:
    statuses = _by_key(bootstrap_report)
    assert set(statuses) == {
        "application",
        "python_version",
        "python_not_store_shim",
        "operating_system",
        "cpu",
        "ram",
        "disk",
        "data_path",
        "data_path_writable",
        "runtime_dependencies",
    }


def test_interpreter_checks_still_pass(bootstrap_report: DoctorReport) -> None:
    statuses = _by_key(bootstrap_report)
    assert statuses["python_version"] is Status.PASS
    assert statuses["python_not_store_shim"] is Status.PASS
    assert statuses["data_path_writable"] is Status.PASS


def test_missing_dependencies_are_a_failure_with_an_actionable_fix(
    bootstrap_report: DoctorReport,
) -> None:
    result = next(r for r in bootstrap_report.results if r.key == "runtime_dependencies")
    assert result.status is Status.FAIL
    assert result.required_in_phase == "1"
    assert "pydantic" in result.detail and "fastapi" in result.detail
    assert "requirements.txt" in result.detail
    assert result.data["install_command"].endswith("-r requirements.txt")
    assert {item["module"] for item in result.data["missing"]} == {"pydantic", "fastapi"}


def test_memory_is_reported_as_unmeasured_not_invented(
    bootstrap_report: DoctorReport,
) -> None:
    result = next(r for r in bootstrap_report.results if r.key == "ram")
    assert result.status is Status.WARN
    assert "Not measured" in result.detail


def test_exit_code_is_one_when_dependencies_are_missing(
    bootstrap_report: DoctorReport,
) -> None:
    assert bootstrap_report.ok is False
    assert bootstrap_report.exit_code() == 1
    assert bootstrap_report.exit_code(strict=True) == 1


def test_report_serialises_to_json(bootstrap_report: DoctorReport) -> None:
    payload = bootstrap_report.to_dict()
    json.dumps(payload, default=str)
    assert payload["mode"] == "bootstrap"
    assert payload["ok"] is False


def test_bootstrap_creates_no_directory(data_root: Path) -> None:
    assert not data_root.exists()
    run_bootstrap_doctor(data_root, missing=SIMULATED_MISSING)
    assert not data_root.exists()


def test_bootstrap_does_not_touch_the_real_default_data_root() -> None:
    existed = DEFAULT_DATA_ROOT.exists()
    run_bootstrap_doctor(missing=SIMULATED_MISSING)
    assert DEFAULT_DATA_ROOT.exists() == existed


def test_invalid_data_root_is_reported_as_a_failure_not_a_crash() -> None:
    report = run_bootstrap_doctor("relative-path", missing=SIMULATED_MISSING)
    statuses = _by_key(report)
    assert statuses["data_path"] is Status.FAIL
    assert report.exit_code() == 1


# --------------------------------------------------------------- CLI wiring


def test_cli_falls_back_to_bootstrap_when_dependencies_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import mom_igd.diagnostics.bootstrap as bootstrap_module

    monkeypatch.setattr(
        bootstrap_module, "missing_runtime_modules", lambda: SIMULATED_MISSING
    )
    exit_code = main(["doctor", "--data-dir", str(tmp_path / "runtime")])
    out = capsys.readouterr().out
    assert exit_code == EXIT_FAILURE
    assert "REDUCED" in out
    assert "runtime_dependencies" in out
    assert "Exit code: 1" in out


def test_cli_bootstrap_json_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import mom_igd.diagnostics.bootstrap as bootstrap_module

    monkeypatch.setattr(
        bootstrap_module, "missing_runtime_modules", lambda: SIMULATED_MISSING
    )
    assert main(["doctor", "--json", "--data-dir", str(tmp_path / "runtime")]) == EXIT_FAILURE
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "bootstrap"
    assert payload["exit_code"] == 1
    assert payload["ok"] is False


def test_a_missing_dependency_in_another_command_is_explained_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import mom_igd.cli as cli_module

    def _explode(_args):
        raise ModuleNotFoundError("No module named 'pydantic'", name="pydantic")

    monkeypatch.setitem(cli_module._DISPATCH, ("db", "init"), _explode)
    assert main(["db", "init", "--data-dir", str(tmp_path / "r")]) == EXIT_FAILURE
    err = capsys.readouterr().err
    assert "Missing dependency: pydantic" in err
    assert "requirements.txt" in err
    assert "doctor` works without them" in err
