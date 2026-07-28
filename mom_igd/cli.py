"""Command-line interface.

Import weight matters here. ``doctor``, ``db`` and ``config`` must work on a
machine where only the Phase 1 runtime dependencies are installed, and they must
not pay for importing FastAPI, uvicorn or pywebview. Every heavy import is
therefore performed *inside* the subcommand that needs it, never at module level.

Exit codes:

===== ============================================================
   0  success
   1  a required check failed / the command could not complete
   2  ``doctor --strict`` and at least one WARN (no FAIL)
   3  configuration is invalid
===== ============================================================
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Sequence

from mom_igd.version import APP_NAME, APP_VERSION, CURRENT_PHASE

__all__ = ["build_parser", "main"]

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_STRICT_WARN = 2
EXIT_CONFIG_ERROR = 3

_PROG = "python -m mom_igd"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            f"{APP_NAME} {APP_VERSION} - fully offline Minutes of Meeting "
            f"application (roadmap phase {CURRENT_PHASE}). "
            "Phase 1 implements the foundation only: no audio capture, no ASR, "
            "no diarization, no LLM, no export."
        ),
        epilog=(
            "Runtime data lives outside this repository (default "
            "D:\\MoM-IGD-Data, override with MOM_IGD_DATA_DIR). "
            "The application makes no outbound network request."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{APP_NAME} {APP_VERSION} (phase {CURRENT_PHASE})",
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        metavar="PATH",
        default=None,
        help="Runtime data root; highest precedence, above MOM_IGD_DATA_DIR.",
    )
    common.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Configuration file (default: config/default.toml).",
    )
    common.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Override the configured log level.",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # -- doctor ------------------------------------------------------------
    doctor = sub.add_parser(
        "doctor",
        parents=[common],
        help="Report environment readiness as PASS / WARN / FAIL.",
        description=(
            "Environment diagnostics. PASS = required by the current phase and "
            "satisfied. WARN = optional, informational, or required only in a "
            "future phase (missing microphone, audio library, AI library, model "
            "or OpenVINO are all WARN in Phase 1). FAIL = required now and not "
            "satisfied. Exit 0 when there is no FAIL; 1 on any FAIL; 2 with "
            "--strict when there is a WARN. Creates nothing and changes nothing."
        ),
    )
    doctor.add_argument("--json", action="store_true", help="Machine-readable output on stdout.")
    doctor.add_argument(
        "--strict", action="store_true", help="Exit 2 when any check reports WARN."
    )

    # -- db ----------------------------------------------------------------
    database = sub.add_parser("db", parents=[common], help="Database initialisation and inspection.")
    db_sub = database.add_subparsers(dest="db_command", metavar="SUBCOMMAND")
    db_init = db_sub.add_parser(
        "init",
        parents=[common],
        help="Create the runtime tree and migrate the database to head.",
        description=(
            "The only command that creates the runtime data directory tree. "
            "Idempotent: safe to run repeatedly."
        ),
    )
    db_init.add_argument("--json", action="store_true")
    db_version = db_sub.add_parser(
        "version", parents=[common], help="Show the applied schema version."
    )
    db_version.add_argument("--json", action="store_true")
    db_verify = db_sub.add_parser(
        "verify",
        parents=[common],
        help="Verify pragmas, migration checksums and the audit hash chain.",
    )
    db_verify.add_argument("--json", action="store_true")

    # -- config ------------------------------------------------------------
    config_cmd = sub.add_parser("config", parents=[common], help="Configuration inspection.")
    config_sub = config_cmd.add_subparsers(dest="config_command", metavar="SUBCOMMAND")
    config_show = config_sub.add_parser(
        "show", parents=[common], help="Print the effective, validated configuration."
    )
    config_show.add_argument("--json", action="store_true")

    # -- registry ----------------------------------------------------------
    registry_cmd = sub.add_parser("registry", parents=[common], help="Model registry inspection.")
    registry_sub = registry_cmd.add_subparsers(dest="registry_command", metavar="SUBCOMMAND")
    registry_show = registry_sub.add_parser(
        "show", parents=[common], help="Validate and summarise models/registry.json."
    )
    registry_show.add_argument("--json", action="store_true")

    # -- serve -------------------------------------------------------------
    serve = sub.add_parser(
        "serve",
        parents=[common],
        help="Run the loopback backend in the foreground (no GUI).",
        description=(
            "Binds 127.0.0.1 only. There is no option to bind another address: a "
            "non-loopback host is rejected by configuration validation."
        ),
    )
    serve.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind this fixed port instead of an OS-assigned ephemeral port.",
    )

    # -- smoke -------------------------------------------------------------
    smoke = sub.add_parser(
        "smoke",
        parents=[common],
        help="Headless backend smoke test; opens no GUI.",
        description=(
            "Starts the real backend on an ephemeral loopback port, calls "
            "/health, /version, an unauthenticated protected endpoint (expecting "
            "401) and an authenticated one (expecting 200), then shuts down and "
            "verifies the serving thread exited. Exit 0 only if every step "
            "passes. Requires no GUI, no microphone, no model and no network."
        ),
    )
    smoke.add_argument("--json", action="store_true")
    smoke.add_argument(
        "--keep-db",
        action="store_true",
        help="Use the configured data root instead of a temporary directory.",
    )

    # -- shell -------------------------------------------------------------
    sub.add_parser(
        "shell",
        parents=[common],
        help="Open the desktop window (pywebview/WebView2). Blocks until closed.",
    )

    return parser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load(args: argparse.Namespace):
    """Load configuration from CLI arguments. Raises ConfigError on failure."""
    from mom_igd.config import load_config

    overrides: dict[str, Any] = {}
    if getattr(args, "log_level", None):
        overrides["log_level"] = args.log_level
    if getattr(args, "port", None) is not None:
        overrides["api"] = {"port": args.port, "port_strategy": "fixed"}
    return load_config(
        config_path=getattr(args, "config", None),
        data_root=getattr(args, "data_dir", None),
        overrides=overrides or None,
    )


def _emit(payload: dict[str, Any], *, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(text)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _cmd_doctor(args: argparse.Namespace) -> int:
    # The most useful moment to run `doctor` is on a machine that is not set up
    # yet. On such a machine pydantic/psutil are missing and the full doctor
    # cannot even be imported, so fall back to the standard-library-only report
    # rather than emitting a traceback. This is what makes
    # `py -3.12 -m mom_igd doctor` usable straight from the repository root.
    from mom_igd.diagnostics.bootstrap import missing_runtime_modules, run_bootstrap_doctor
    from mom_igd.diagnostics.model import format_report as format_bootstrap_report

    absent = missing_runtime_modules()
    if absent:
        report = run_bootstrap_doctor(args.data_dir, missing=absent)
        if args.json:
            payload = report.to_dict()
            payload["exit_code"] = report.exit_code(strict=args.strict)
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        else:
            print(format_bootstrap_report(report, strict=args.strict))
        return report.exit_code(strict=args.strict)

    from mom_igd.config import ConfigError
    from mom_igd.diagnostics.doctor import format_report, run_doctor

    try:
        config = _load(args)
    except ConfigError as exc:
        # Still produce a report: a broken configuration is itself a FAIL that the
        # doctor is supposed to explain rather than crash on.
        report = run_doctor(config=None, data_root=args.data_dir)
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
        else:
            print(format_report(report, strict=args.strict))
            print(f"\nConfiguration error: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    report = run_doctor(config=config, ensure_dirs=False)
    if args.json:
        payload = report.to_dict()
        payload["exit_code"] = report.exit_code(strict=args.strict)
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(format_report(report, strict=args.strict))
    return report.exit_code(strict=args.strict)


def _cmd_db_init(args: argparse.Namespace) -> int:
    from mom_igd.db import initialize_database

    config = _load(args)
    paths = config.runtime_paths().ensure()
    result = initialize_database(
        paths.database_path(config.database.filename),
        busy_timeout_ms=config.database.busy_timeout_ms,
        app_version=config.app_version,
    )
    status = result["status"]
    text = "\n".join(
        [
            f"Runtime data root : {paths.root}",
            f"Database          : {result['database_path']}",
            f"Created now       : {result['created']}",
            f"Applied now       : {result['applied_now'] or '(none; already up to date)'}",
            f"Schema version    : {status['current_version']} of {status['head_version']}",
            f"journal_mode      : {result['pragmas']['journal_mode']}",
            f"foreign_keys      : {result['pragmas']['foreign_keys']}",
            f"busy_timeout      : {result['pragmas']['busy_timeout']} ms",
            f"SQLite            : {result['pragmas']['sqlite_version']}",
        ]
    )
    _emit(result, as_json=args.json, text=text)
    return EXIT_OK if status["up_to_date"] else EXIT_FAILURE


def _cmd_db_version(args: argparse.Namespace) -> int:
    from mom_igd.db import current_schema_version, discover_migrations, head_version, migration_status
    from mom_igd.db.connection import connect

    config = _load(args)
    paths = config.runtime_paths()
    db_path = paths.database_path(config.database.filename)
    head = head_version(discover_migrations())
    if not db_path.exists():
        payload = {
            "database_path": str(db_path),
            "exists": False,
            "current_version": None,
            "head_version": head,
        }
        _emit(
            payload,
            as_json=args.json,
            text=(
                f"Database          : {db_path}\n"
                f"Exists            : no (run `{_PROG} db init`)\n"
                f"Head version      : {head}"
            ),
        )
        return EXIT_FAILURE

    conn = connect(db_path, busy_timeout_ms=config.database.busy_timeout_ms)
    try:
        status = migration_status(conn)
        payload = {"database_path": str(db_path), "exists": True, **status}
        lines = [
            f"Database          : {db_path}",
            f"Schema version    : {status['current_version']} of {status['head_version']}",
            f"Up to date        : {status['up_to_date']}",
            f"Pending           : {status['pending'] or '(none)'}",
            "Applied migrations:",
        ]
        for row in status["applied"]:
            lines.append(
                f"  {int(row['version']):04d} {row['name']:<24} "
                f"{row['applied_at']}  {row['duration_ms']} ms  "
                f"{str(row['checksum'])[:12]}..."
            )
        _emit(payload, as_json=args.json, text="\n".join(lines))
        return EXIT_OK if status["up_to_date"] else EXIT_FAILURE
    finally:
        conn.close()
        _ = current_schema_version  # referenced for API clarity


def _cmd_db_verify(args: argparse.Namespace) -> int:
    from mom_igd.audit import count_events, verify_chain
    from mom_igd.db import discover_migrations, migration_status, verify_applied_checksums
    from mom_igd.db.connection import connect, verify_pragmas

    config = _load(args)
    db_path = config.runtime_paths().database_path(config.database.filename)
    if not db_path.exists():
        print(f"Database does not exist: {db_path}", file=sys.stderr)
        return EXIT_FAILURE

    problems: list[str] = []
    conn = connect(db_path, busy_timeout_ms=config.database.busy_timeout_ms, verify=False)
    try:
        try:
            pragmas = verify_pragmas(conn)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            pragmas = {}
            problems.append(f"pragmas: {exc}")
        try:
            verify_applied_checksums(conn, discover_migrations())
        except Exception as exc:  # noqa: BLE001
            problems.append(f"migration checksums: {exc}")
        chain_ok, bad_id, reason = verify_chain(conn)
        if not chain_ok:
            problems.append(f"audit chain broken at id={bad_id}: {reason}")
        events = count_events(conn)
        status = migration_status(conn)
    finally:
        conn.close()

    payload = {
        "database_path": str(db_path),
        "pragmas": pragmas,
        "schema_version": status["current_version"],
        "head_version": status["head_version"],
        "audit_events": events,
        "audit_chain_ok": chain_ok,
        "problems": problems,
        "ok": not problems,
    }
    text = "\n".join(
        [
            f"Database          : {db_path}",
            f"Schema version    : {status['current_version']} of {status['head_version']}",
            f"journal_mode      : {pragmas.get('journal_mode', '?')}",
            f"foreign_keys      : {pragmas.get('foreign_keys', '?')}",
            f"Audit events      : {events}",
            f"Audit chain       : {'intact' if chain_ok else 'BROKEN'}",
            "",
            "OK" if not problems else "PROBLEMS:\n  - " + "\n  - ".join(problems),
        ]
    )
    _emit(payload, as_json=args.json, text=text)
    return EXIT_OK if not problems else EXIT_FAILURE


def _cmd_config_show(args: argparse.Namespace) -> int:
    config = _load(args)
    summary = config.summary()
    lines = [f"{APP_NAME} {APP_VERSION} - effective configuration", ""]

    def _walk(node: dict[str, Any], indent: int = 0) -> None:
        for key, value in node.items():
            if isinstance(value, dict):
                lines.append(f"{'  ' * indent}{key}:")
                _walk(value, indent + 1)
            else:
                lines.append(f"{'  ' * indent}{key:<24} {value}")

    _walk(summary)
    _emit(summary, as_json=args.json, text="\n".join(lines))
    return EXIT_OK


def _cmd_registry_show(args: argparse.Namespace) -> int:
    from mom_igd.registry import RegistryError, load_registry, registry_status

    config = _load(args)
    try:
        registry = load_registry(config.model_registry_path)
    except RegistryError as exc:
        print(f"Model registry invalid: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    status = registry_status(registry, config.runtime_paths().models_dir)
    payload = {"path": str(config.model_registry_path), **status}
    lines = [
        f"Registry          : {config.model_registry_path}",
        f"Schema version    : {status['registry_schema_version']}",
        f"Declared models   : {status['total']}",
        f"Provisioned       : {status['provisioned']}",
        f"Offline ready     : {status['offline_ready']}",
    ]
    if registry.is_empty:
        lines += [
            "",
            "The registry is empty. That is the correct Phase 1 state: no ASR, "
            "diarization,",
            "speaker-embedding or LLM provider has been selected and no model has "
            "been downloaded.",
            "Provider selection is deferred to the Phase 4A benchmark.",
        ]
    _emit(payload, as_json=args.json, text="\n".join(lines))
    return EXIT_OK


def _cmd_serve(args: argparse.Namespace) -> int:
    # Heavy imports live here so `doctor` never pays for them.
    import uvicorn

    from mom_igd.api.app import create_app
    from mom_igd.logging_setup import setup_logging
    from mom_igd.security import SessionToken

    config = _load(args)
    paths = config.runtime_paths().ensure()
    token = SessionToken()
    setup_logging(
        config.log_level, log_file=paths.log_file(), session_token=token
    )
    app = create_app(config, session_token=token, paths=paths)
    print(
        f"{APP_NAME} {APP_VERSION} backend on {config.api.host} "
        f"({'ephemeral port' if config.api.port_strategy == 'ephemeral' else f'port {config.api.port}'})"
        "\nLoopback only. Press Ctrl+C to stop.",
        file=sys.stderr,
    )
    uvicorn.run(
        app,
        host=config.api.host,
        port=config.api.effective_port(),
        log_level=config.log_level.lower(),
        log_config=None,
        access_log=False,
    )
    return EXIT_OK


def _cmd_smoke(args: argparse.Namespace) -> int:
    from mom_igd.smoke import run_smoke

    config = _load(args)
    result = run_smoke(config, use_temp_data_root=not args.keep_db)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        for step in result["steps"]:
            mark = "ok  " if step["ok"] else "FAIL"
            print(f"[{mark}] {step['name']}: {step['detail']}")
        print()
        print(
            f"Smoke test: {'PASS' if result['ok'] else 'FAIL'} "
            f"({result['passed']}/{result['total']} steps)"
        )
    return EXIT_OK if result["ok"] else EXIT_FAILURE


def _cmd_shell(args: argparse.Namespace) -> int:
    from mom_igd.logging_setup import setup_logging
    from mom_igd.shell.launcher import run_shell

    config = _load(args)
    paths = config.runtime_paths().ensure()
    setup_logging(config.log_level, log_file=paths.log_file())
    return run_shell(config)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_DISPATCH: dict[tuple[str, str | None], Any] = {
    ("doctor", None): _cmd_doctor,
    ("db", "init"): _cmd_db_init,
    ("db", "version"): _cmd_db_version,
    ("db", "verify"): _cmd_db_verify,
    ("config", "show"): _cmd_config_show,
    ("registry", "show"): _cmd_registry_show,
    ("serve", None): _cmd_serve,
    ("smoke", None): _cmd_smoke,
    ("shell", None): _cmd_shell,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.command:
        parser.print_help()
        return EXIT_OK

    sub_name: str | None = None
    if args.command == "db":
        sub_name = getattr(args, "db_command", None)
    elif args.command == "config":
        sub_name = getattr(args, "config_command", None)
    elif args.command == "registry":
        sub_name = getattr(args, "registry_command", None)

    if args.command in {"db", "config", "registry"} and not sub_name:
        print(f"`{_PROG} {args.command}` requires a subcommand.", file=sys.stderr)
        print(f"Try `{_PROG} {args.command} --help`.", file=sys.stderr)
        return EXIT_FAILURE

    handler = _DISPATCH.get((args.command, sub_name))
    if handler is None:  # pragma: no cover - argparse rejects unknown commands
        parser.error(f"Unknown command: {args.command} {sub_name or ''}")

    # `mom_igd.paths` is standard-library only, so importing it is always safe.
    # `mom_igd.config` needs pydantic, which may legitimately be absent on a
    # bare interpreter -- hence the conditional import rather than a hard one.
    from mom_igd.paths import PathValidationError

    config_errors: tuple[type[BaseException], ...] = (PathValidationError,)
    try:
        from mom_igd.config import ConfigError
    except ModuleNotFoundError:  # pragma: no cover - only on an unprepared env
        pass
    else:
        config_errors = (PathValidationError, ConfigError)

    try:
        return int(handler(args))
    except config_errors as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_FAILURE
    except ModuleNotFoundError as exc:
        print(
            f"Missing dependency: {exc.name}. This command needs the Phase 1 "
            "runtime dependencies. Install them into the project virtual "
            "environment:\n"
            r"    py -3.12 -m venv .venv" "\n"
            r"    .venv\Scripts\python.exe -m pip install -r requirements.txt" "\n"
            "Then run the command with .venv\\Scripts\\python.exe. "
            "`doctor` works without them and will tell you what is missing.",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    except Exception as exc:  # noqa: BLE001 - top-level CLI guard
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        if os.environ.get("MOM_IGD_TRACEBACK"):
            raise
        print(
            "Set MOM_IGD_TRACEBACK=1 for a full traceback.",
            file=sys.stderr,
        )
        return EXIT_FAILURE
