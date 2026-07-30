"""Doctor classification and exit codes.

Covers Phase 1 test categories 18, 19 and 29.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mom_igd.config import AppConfig, load_config
from mom_igd.diagnostics.doctor import (
    CheckResult,
    DoctorReport,
    Status,
    format_report,
    run_doctor,
)
from mom_igd.paths import DEFAULT_DATA_ROOT
from mom_igd.version import APP_VERSION, CURRENT_PHASE


def _by_key(report: DoctorReport) -> dict[str, CheckResult]:
    return {result.key: result for result in report.results}


@pytest.fixture
def report(config: AppConfig) -> DoctorReport:
    return run_doctor(config=config)


# ------------------------------------------------------- required reporting


def test_report_covers_every_required_topic(report: DoctorReport) -> None:
    required_keys = {
        "application",          # name, version, phase
        "python_version",       # executable and version
        "python_not_store_shim",
        "configuration",
        "operating_system",     # Windows version
        "cpu",
        "ram",
        "disk",
        "data_path",            # runtime data root
        "data_path_writable",
        "database",             # SQLite + migration status
        "api_loopback",
        "offline_policy",       # offline mode + dependency audit
        "model_registry",
        "optional_dependencies",  # AI dependencies as future requirement
        "audio_backend",          # Phase 2: sounddevice / PortAudio
        "audio_input_devices",    # Phase 2: at least one usable capture device
        "audio_capture_profile",
        "usb_conference_microphone",
        "audio_stale_recordings",
        "docker_wsl_presence",
        "docker_wsl_memory",
    }
    assert required_keys <= set(_by_key(report))


def test_application_identity_is_reported(report: DoctorReport) -> None:
    result = _by_key(report)["application"]
    assert result.status is Status.PASS
    assert APP_VERSION in result.detail
    assert CURRENT_PHASE in result.detail


def test_python_checks_pass_on_this_interpreter(report: DoctorReport) -> None:
    results = _by_key(report)
    assert results["python_version"].status is Status.PASS
    assert results["python_version"].data["version"].startswith("3.12")
    assert results["python_not_store_shim"].status is Status.PASS
    assert "WindowsApps" not in results["python_not_store_shim"].data["base_prefix"]


def test_hardware_checks_report_numbers(report: DoctorReport) -> None:
    results = _by_key(report)
    assert results["cpu"].data["logical"] >= 1
    assert results["ram"].data["total_mb"] > 0
    assert results["disk"].data["free_gb"] >= 0


def test_loopback_and_single_worker_are_reported_as_pass(report: DoctorReport) -> None:
    result = _by_key(report)["api_loopback"]
    assert result.status is Status.PASS
    assert result.data["host"] == "127.0.0.1"
    assert result.data["max_heavy_workers"] == 1


def test_offline_policy_passes_with_no_cloud_sdk(report: DoctorReport) -> None:
    result = _by_key(report)["offline_policy"]
    assert result.status is Status.PASS
    assert result.data["dependency_audit"]["cloud"] == []
    assert result.data["firewall_enforcement"] == "deferred to Phase 11"


# ---------------------------------------------- 18. PASS / WARN / FAIL logic


def test_status_values_are_exactly_the_three_documented_ones() -> None:
    assert {status.value for status in Status} == {"PASS", "WARN", "FAIL"}


def test_uninitialised_database_is_a_warning_not_a_failure(report: DoctorReport) -> None:
    result = _by_key(report)["database"]
    assert result.status is Status.WARN
    assert "db init" in result.detail
    assert result.data["exists"] is False


def test_initialised_database_becomes_pass(config: AppConfig, paths) -> None:
    from mom_igd.db import initialize_database

    initialize_database(
        paths.database_path(config.database.filename), app_version=config.app_version
    )
    result = _by_key(run_doctor(config=config))["database"]
    assert result.status is Status.PASS
    assert "WAL enabled" in result.detail
    assert "foreign keys enforced" in result.detail


def test_broken_configuration_is_a_failure_and_stops_early(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MOM_IGD_DATA_DIR", "relative-is-invalid")
    report = run_doctor()
    results = _by_key(report)
    assert results["configuration"].status is Status.FAIL
    assert report.ok is False
    assert report.exit_code() == 1
    assert "database" not in results, "checks needing configuration must be skipped"


def test_unwritable_data_root_is_a_failure(config: AppConfig, monkeypatch) -> None:
    from mom_igd import paths as paths_module

    monkeypatch.setattr(paths_module.RuntimePaths, "is_writable", lambda self: False)
    result = _by_key(run_doctor(config=config))["data_path_writable"]
    assert result.status is Status.FAIL


def test_malformed_model_registry_is_a_failure(config: AppConfig, tmp_path: Path) -> None:
    broken = tmp_path / "registry.json"
    broken.write_text('{"registry_schema_version": 1, "models": [{"bad": 1}]}', encoding="utf-8")
    hacked = AppConfig.model_validate({**config.model_dump(), "model_registry_path": broken})
    result = _by_key(run_doctor(config=hacked))["model_registry"]
    assert result.status is Status.FAIL


# ------------------------- 19. future-phase items are WARN, never FAIL


def test_missing_ai_dependencies_are_only_warnings(report: DoctorReport) -> None:
    """A future-phase dependency is always a WARN, never a FAIL.

    The membership list changes as phases land, so this asserts the *classification*
    rather than a fixed set. `faster_whisper`, `ctranslate2` and `onnxruntime` graduated in
    Phase 4 (ADR-0014) and are installed; `torch` and `openvino` belong to later phases and
    are still expected to be missing.
    """
    result = _by_key(report)["optional_dependencies"]
    assert result.status is Status.WARN, (
        "a future-phase dependency must never make doctor FAIL"
    )
    missing = {item["module"] for item in result.data["missing"]}

    # Phase 5+ stacks: still absent, and still only a warning.
    assert {"torch", "openvino"} <= missing, missing

    # Graduated dependencies must no longer be reported as missing "future" ones -- the
    # same rule that removed `sounddevice` from this list in Phase 2.
    for graduated in ("sounddevice", "faster_whisper", "ctranslate2", "onnxruntime"):
        assert graduated not in missing, (
            f"{graduated} is an installed dependency of the current phase and must have "
            "its own check rather than appearing as a missing future one"
        )


def test_audio_backend_is_a_phase_2_requirement(report: DoctorReport) -> None:
    result = _by_key(report)["audio_backend"]
    assert result.status is Status.PASS
    assert result.required_in_phase == "2"
    assert "PortAudio" in result.detail


def test_audio_devices_are_enumerated_without_opening_a_stream(
    report: DoctorReport,
) -> None:
    result = _by_key(report)["audio_input_devices"]
    assert result.status is Status.PASS, result.detail
    assert "No stream was opened" in result.detail
    assert result.data["usable_count"] >= 1
    # Enumeration must also explain what it excluded and why.
    assert all(entry["reason"] for entry in result.data["rejected"])


def test_missing_openvino_is_a_warning(report: DoctorReport) -> None:
    result = _by_key(report)["optional_dependencies"]
    modules = {item["module"] for item in result.data["missing"]}
    assert "openvino" in modules
    assert result.status is not Status.FAIL


def test_usb_microphone_is_a_warning_in_development(report: DoctorReport) -> None:
    """Development tolerates the built-in array; production does not."""
    result = _by_key(report)["usb_conference_microphone"]
    assert result.status in {Status.PASS, Status.WARN}
    if result.status is Status.WARN:
        assert "No USB capture device verified by Windows" in result.detail
        assert "before Phase 2 production acceptance" in result.detail
        assert result.required_in_phase is None, "not a development requirement"
        assert result.data["verified_usb"] == []


def test_usb_microphone_becomes_a_failure_under_the_production_gate(
    config: AppConfig,
) -> None:
    production = run_doctor(config=config, production=True)
    result = _by_key(production)["usb_conference_microphone"]
    development = _by_key(run_doctor(config=config))["usb_conference_microphone"]

    if development.status is Status.WARN:
        assert result.status is Status.FAIL, (
            "without a verified USB conference microphone the production gate must "
            "fail rather than warn"
        )
        assert production.ok is False
        assert production.exit_code() == 1
    else:
        assert result.status is Status.PASS


def test_production_gate_requires_calibration_evidence(config: AppConfig) -> None:
    report = run_doctor(config=config, production=True)
    result = _by_key(report)["audio_calibration_evidence"]
    # No database and no calibration exist in a temporary data root.
    assert result.status is Status.FAIL
    assert "calibrate" in result.detail


def test_calibration_evidence_is_not_checked_in_development(config: AppConfig) -> None:
    assert "audio_calibration_evidence" not in _by_key(run_doctor(config=config))


def test_stale_recording_is_a_warning_in_development(config: AppConfig, paths) -> None:
    from mom_igd.audio.writer import partial_path

    directory = paths.recordings_dir / "meeting" / "recording"
    directory.mkdir(parents=True)
    partial_path(directory, 0).write_bytes(b"\x00\x00\x00\x00")

    development = _by_key(run_doctor(config=config))["audio_stale_recordings"]
    production = _by_key(run_doctor(config=config, production=True))[
        "audio_stale_recordings"
    ]
    assert development.status is Status.WARN
    assert production.status is Status.FAIL
    assert development.data["pending_count"] == 1
    assert "audio recover" in development.detail


def test_empty_model_registry_is_a_warning(report: DoctorReport) -> None:
    result = _by_key(report)["model_registry"]
    assert result.status is Status.WARN
    assert result.data["total"] == 0
    assert result.data["empty"] is True


def test_docker_and_wsl_are_informational_only(report: DoctorReport) -> None:
    for key in ("docker_wsl_presence", "docker_wsl_memory"):
        assert _by_key(report)[key].status is not Status.FAIL
        assert _by_key(report)[key].required_in_phase is None


def test_no_check_fails_on_this_machine(report: DoctorReport) -> None:
    failures = {result.key: result.detail for result in report.failures}
    assert failures == {}, f"Phase 1 requirements must be satisfiable here: {failures}"


def test_phase_1_produces_warnings_by_design(report: DoctorReport) -> None:
    assert report.warnings, "future-phase dependencies must surface as warnings"


# ---------------------------------------------------- exit-code determinism


def test_exit_code_zero_when_no_failure(report: DoctorReport) -> None:
    assert report.ok is True
    assert report.exit_code() == 0


def test_exit_code_two_only_in_strict_mode(report: DoctorReport) -> None:
    assert report.exit_code(strict=False) == 0
    assert report.exit_code(strict=True) == 2


def test_exit_code_one_takes_precedence_over_warnings() -> None:
    results = (
        CheckResult("a", "A", Status.PASS, "ok"),
        CheckResult("b", "B", Status.WARN, "later"),
        CheckResult("c", "C", Status.FAIL, "broken"),
    )
    report = DoctorReport(generated_at="2026-01-01T00:00:00.000Z", app={}, results=results)
    assert report.exit_code() == 1
    assert report.exit_code(strict=True) == 1
    assert report.counts == {"PASS": 1, "WARN": 1, "FAIL": 1}


def test_all_pass_report_exits_zero_even_in_strict_mode() -> None:
    report = DoctorReport(
        generated_at="2026-01-01T00:00:00.000Z",
        app={},
        results=(CheckResult("a", "A", Status.PASS, "ok"),),
    )
    assert report.exit_code(strict=True) == 0


def test_doctor_is_deterministic_for_stable_inputs(config: AppConfig) -> None:
    first = {r.key: r.status for r in run_doctor(config=config).results}
    second = {r.key: r.status for r in run_doctor(config=config).results}
    assert first == second


# ------------------------------------------------------------ serialisation


def test_report_serialises_to_json(report: DoctorReport) -> None:
    payload = report.to_dict()
    json.dumps(payload, default=str)  # must not raise
    assert set(payload) == {"generated_at", "app", "mode", "counts", "ok", "results"}
    assert payload["mode"] == "full", "the full doctor must not be marked reduced"
    assert all(item["status"] in {"PASS", "WARN", "FAIL"} for item in payload["results"])


def test_text_report_is_readable_and_states_the_exit_code(report: DoctorReport) -> None:
    text = format_report(report)
    assert "environment diagnostics" in text
    assert "Summary:" in text
    assert "Exit code: 0" in text
    for result in report.results:
        assert result.key in text


# ------------------------------------- 29. doctor creates no directories


def test_doctor_creates_nothing_by_default(config: AppConfig, data_root: Path) -> None:
    assert not data_root.exists()
    run_doctor(config=config)
    assert not data_root.exists(), "diagnosing a machine must have no filesystem side effect"


def test_doctor_does_not_touch_the_real_default_data_root(config: AppConfig) -> None:
    existed = DEFAULT_DATA_ROOT.exists()
    run_doctor(config=config)
    assert DEFAULT_DATA_ROOT.exists() == existed


def test_ensure_dirs_is_opt_in(config: AppConfig, data_root: Path) -> None:
    assert not data_root.exists()
    run_doctor(config=config, ensure_dirs=True)
    assert data_root.is_dir()
    assert (data_root / "db").is_dir()


def test_doctor_does_not_import_heavy_dependencies() -> None:
    """`doctor` must stay cheap: no FastAPI, uvicorn, webview or httpx."""
    import subprocess
    import sys

    code = (
        "import sys, mom_igd.diagnostics.doctor as d;"
        "heavy=[m for m in sys.modules if m.split('.')[0] in "
        "{'fastapi','uvicorn','webview','starlette','httpx'}];"
        "print(','.join(sorted(heavy)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "", f"doctor pulled in {result.stdout.strip()}"


# ------------------------------------------------- Phase 4 transcription models


def test_a_missing_transcription_model_is_a_warning_not_a_failure(
    report: DoctorReport,
) -> None:
    """Provisioning is a deliberate one-off command that needs network access.

    An operator on a fresh machine has not done anything wrong, and `doctor` must work on
    a machine where no model has ever been downloaded. It becomes a FAIL only when
    accuracy acceptance is granted and `CURRENT_PHASE` advances to 4.
    """
    result = _by_key(report)["asr_models"]
    assert result.status is Status.WARN
    assert result.data["pass1_ready"] is False
    assert result.data["index_readable"] is True
    assert "asr provision" in result.detail
    assert "MODEL_UNAVAILABLE" in result.detail


def test_the_transcription_check_names_the_provisioning_command(
    report: DoctorReport,
) -> None:
    """A diagnostic that says what is wrong without saying what to do is half a message."""
    assert "python -m mom_igd asr provision all" in _by_key(report)["asr_models"].detail


def test_the_transcription_check_claims_nothing_about_accuracy(
    report: DoctorReport,
) -> None:
    """Matched on word boundaries: a substring search for "wer" finds "answer"."""
    import re

    detail = _by_key(report)["asr_models"].detail.lower()
    for forbidden in ("accurate", "wer", "quality", "reliable"):
        assert not re.search(rf"\b{forbidden}\b", detail), forbidden


def test_the_transcription_check_reads_the_readiness_registry_not_a_directory_scan(
    config: AppConfig, paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that hash-verifies but failed its load probe must not read as ready.

    Asserted by handing it a model directory with no readiness record at all: a scan would
    find the directory, and the registry -- correctly -- finds nothing.
    """
    store = paths.models_dir / "faster-whisper-small"
    store.mkdir(parents=True, exist_ok=True)
    (store / "model.bin").write_bytes(b"not really a model")
    result = _by_key(run_doctor(config=config))["asr_models"]
    assert result.status is Status.WARN
    assert result.data["pass1_ready"] is False
    assert result.data["ready_models"] == []


def test_a_corrupt_readiness_registry_fails_closed_with_a_warning(
    config: AppConfig, paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    (paths.models_dir / "installed.json").write_text("{ not json", encoding="utf-8")
    result = _by_key(run_doctor(config=config))["asr_models"]
    assert result.status is Status.WARN
    assert result.data["index_readable"] is False
    assert result.data["pass1_ready"] is False
    assert "fail-closed" in result.detail


def test_doctor_still_imports_nothing_heavy_after_the_transcription_check() -> None:
    """The check must not drag faster-whisper, onnxruntime or numpy into `doctor`."""
    import subprocess
    import sys

    probe = (
        "import sys\n"
        "from mom_igd.diagnostics import doctor\n"
        "heavy = {'fastapi', 'uvicorn', 'webview', 'httpx', 'faster_whisper',\n"
        "         'ctranslate2', 'onnxruntime', 'numpy', 'torch', 'av'}\n"
        "print(sorted({m.split('.')[0] for m in sys.modules} & heavy))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", result.stdout
