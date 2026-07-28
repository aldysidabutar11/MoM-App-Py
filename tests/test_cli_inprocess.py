"""In-process CLI tests.

`tests/test_cli.py` drives the CLI through `python -m mom_igd`, which is the real
entry point but runs in a child process — so it proves the wiring works while
contributing nothing to coverage of `mom_igd/cli.py`. These tests call
`main(argv)` directly for the same commands, so the dispatch, argument handling
and output formatting are actually measured.

`serve` and `shell` are deliberately absent: one blocks on uvicorn, the other
opens a window. Both are covered by the modules they delegate to
(`api/server.py`, `smoke.py`) and by the documented manual check.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mom_igd.cli import (
    EXIT_CONFIG_ERROR,
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_STRICT_WARN,
    build_parser,
    main,
)
from mom_igd.version import APP_VERSION


@pytest.fixture
def runtime(tmp_path: Path) -> list[str]:
    """Arguments pinning the CLI to a temporary data root."""
    return ["--data-dir", str(tmp_path / "runtime")]


def _json_out(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out)


# ------------------------------------------------------------------- doctor


def test_doctor_text_output(runtime: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor", *runtime]) == EXIT_OK
    out = capsys.readouterr().out
    assert "environment diagnostics" in out
    assert "0 FAIL" in out
    assert "Exit code: 0" in out


def test_doctor_json_output(runtime: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor", "--json", *runtime]) == EXIT_OK
    payload = _json_out(capsys)
    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert payload["counts"]["FAIL"] == 0
    assert payload["app"]["app_version"] == APP_VERSION


def test_doctor_strict_returns_two_on_warnings(
    runtime: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["doctor", "--strict", *runtime]) == EXIT_STRICT_WARN
    assert "Exit code: 2" in capsys.readouterr().out


def test_doctor_reports_a_broken_configuration_without_crashing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MOM_IGD_DATA_DIR", "not-absolute")
    assert main(["doctor"]) == EXIT_FAILURE
    captured = capsys.readouterr()
    assert "FAIL" in captured.out
    assert "Configuration error" in captured.err


def test_doctor_json_still_emitted_for_a_broken_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MOM_IGD_DATA_DIR", "not-absolute")
    assert main(["doctor", "--json"]) == EXIT_FAILURE
    payload = _json_out(capsys)
    assert payload["ok"] is False
    assert any(r["key"] == "configuration" and r["status"] == "FAIL" for r in payload["results"])


def test_doctor_creates_nothing(tmp_path: Path) -> None:
    target = tmp_path / "untouched"
    assert main(["doctor", "--data-dir", str(target)]) == EXIT_OK
    assert not target.exists()


def test_doctor_accepts_a_log_level_override(runtime: list[str]) -> None:
    assert main(["doctor", "--log-level", "DEBUG", *runtime]) == EXIT_OK


# ----------------------------------------------------------------------- db


def test_db_init_then_version_then_verify(
    runtime: list[str], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert main(["db", "init", "--json", *runtime]) == EXIT_OK
    init = _json_out(capsys)
    assert init["created"] is True
    assert init["pragmas"]["journal_mode"] == "wal"
    assert init["pragmas"]["foreign_keys"] == 1

    assert main(["db", "version", "--json", *runtime]) == EXIT_OK
    version = _json_out(capsys)
    assert version["up_to_date"] is True
    assert version["current_version"] == version["head_version"]

    assert main(["db", "verify", "--json", *runtime]) == EXIT_OK
    verify = _json_out(capsys)
    assert verify["ok"] is True
    assert verify["audit_chain_ok"] is True
    assert verify["problems"] == []


def test_db_init_text_output_lists_the_pragmas(
    runtime: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["db", "init", *runtime]) == EXIT_OK
    out = capsys.readouterr().out
    assert "journal_mode      : wal" in out
    assert "foreign_keys      : 1" in out
    assert "Schema version" in out


def test_db_init_creates_every_runtime_subdirectory(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    assert main(["db", "init", "--data-dir", str(root)]) == EXIT_OK
    for name in ("db", "recordings", "exports", "logs", "models", "temp", "backups"):
        assert (root / name).is_dir(), name


def test_db_init_is_idempotent(runtime: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["db", "init", *runtime]) == EXIT_OK
    capsys.readouterr()
    assert main(["db", "init", "--json", *runtime]) == EXIT_OK
    assert _json_out(capsys)["already_up_to_date"] is True


def test_db_version_before_init_fails_with_guidance(
    runtime: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["db", "version", *runtime]) == EXIT_FAILURE
    assert "db init" in capsys.readouterr().out


def test_db_version_text_lists_applied_migrations(
    runtime: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    main(["db", "init", *runtime])
    capsys.readouterr()
    assert main(["db", "version", *runtime]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Applied migrations:" in out
    assert "0001 initial" in out


def test_db_verify_without_a_database_fails(
    runtime: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["db", "verify", *runtime]) == EXIT_FAILURE
    assert "does not exist" in capsys.readouterr().err


def test_db_verify_detects_a_tampered_audit_chain(
    runtime: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from mom_igd.audit import record_event
    from mom_igd.db.connection import connect

    assert main(["db", "init", *runtime]) == EXIT_OK
    capsys.readouterr()
    db_file = tmp_path / "runtime" / "db" / "mom_igd.db"

    conn = connect(db_file)
    try:
        record_event(conn, category="APP", action="first")
        record_event(conn, category="APP", action="second")
        conn.execute("UPDATE audit_events SET action = 'tampered' WHERE id = 1")
    finally:
        conn.close()

    assert main(["db", "verify", "--json", *runtime]) == EXIT_FAILURE
    payload = _json_out(capsys)
    assert payload["ok"] is False
    assert payload["audit_chain_ok"] is False
    assert any("audit chain broken" in problem for problem in payload["problems"])


# ------------------------------------------------------------ config/registry


def test_config_show_json(runtime: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "show", "--json", *runtime]) == EXIT_OK
    payload = _json_out(capsys)
    assert payload["api"]["host"] == "127.0.0.1"
    assert payload["runtime_mode"] == "offline"
    assert payload["resources"]["max_heavy_workers"] == 1


def test_config_show_text_is_indented_and_secret_free(
    runtime: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["config", "show", *runtime]) == EXIT_OK
    out = capsys.readouterr().out
    assert "effective configuration" in out
    assert "  host" in out
    assert "token" not in out.lower()


def test_registry_show_text_explains_the_empty_state(
    runtime: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["registry", "show", *runtime]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Declared models   : 0" in out
    assert "Phase 4A" in out


def test_registry_show_json(runtime: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["registry", "show", "--json", *runtime]) == EXIT_OK
    payload = _json_out(capsys)
    assert payload["total"] == 0
    assert payload["empty"] is True


def test_registry_show_fails_on_a_broken_registry(
    runtime: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    broken = tmp_path / "registry.json"
    broken.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("MOM_IGD_MODEL_REGISTRY", str(broken))
    assert main(["registry", "show", *runtime]) == EXIT_FAILURE
    assert "Model registry invalid" in capsys.readouterr().err


# ------------------------------------------------------------------- smoke


def test_smoke_json(runtime: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["smoke", "--json", *runtime]) == EXIT_OK
    payload = _json_out(capsys)
    assert payload["ok"] is True
    assert payload["passed"] == payload["total"]


def test_smoke_text_lists_every_step(
    runtime: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["smoke", *runtime]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Smoke test: PASS" in out
    for step in ("database_init", "protected_requires_token", "clean_shutdown"):
        assert step in out


def test_smoke_keep_db_uses_the_configured_root(
    runtime: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["smoke", "--keep-db", "--json", *runtime]) == EXIT_OK
    assert _json_out(capsys)["ok"] is True
    assert (tmp_path / "runtime" / "db" / "mom_igd.db").is_file()


# ------------------------------------------------------- dispatch and errors


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_OK
    assert "COMMAND" in capsys.readouterr().out


@pytest.mark.parametrize("group", ["db", "config", "registry"])
def test_group_without_subcommand_is_refused(
    group: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([group]) == EXIT_FAILURE
    assert "requires a subcommand" in capsys.readouterr().err


def test_invalid_data_dir_returns_the_config_exit_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["config", "show", "--data-dir", "relative"]) == EXIT_CONFIG_ERROR
    assert "Configuration error" in capsys.readouterr().err


def test_repository_as_data_dir_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    from mom_igd.paths import repo_root

    assert main(["config", "show", "--data-dir", str(repo_root())]) == EXIT_CONFIG_ERROR
    assert "repository" in capsys.readouterr().err


def test_unexpected_error_is_reported_not_raised(
    runtime: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import mom_igd.cli as cli_module

    def _explode(_args):
        raise RuntimeError("synthetic failure")

    monkeypatch.setitem(cli_module._DISPATCH, ("doctor", None), _explode)
    monkeypatch.delenv("MOM_IGD_TRACEBACK", raising=False)
    assert main(["doctor", *runtime]) == EXIT_FAILURE
    err = capsys.readouterr().err
    assert "RuntimeError: synthetic failure" in err
    assert "MOM_IGD_TRACEBACK=1" in err


def test_traceback_env_var_reraises(
    runtime: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import mom_igd.cli as cli_module

    def _explode(_args):
        raise RuntimeError("synthetic failure")

    monkeypatch.setitem(cli_module._DISPATCH, ("doctor", None), _explode)
    monkeypatch.setenv("MOM_IGD_TRACEBACK", "1")
    with pytest.raises(RuntimeError, match="synthetic failure"):
        main(["doctor", *runtime])


def test_parser_rejects_an_unknown_command() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["not-a-command"])


def test_parser_rejects_an_unknown_log_level() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["doctor", "--log-level", "CHATTY"])
