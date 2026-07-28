"""Command-line interface.

Import weight matters here. ``doctor``, ``db`` and ``config`` must work on a
machine where only the core runtime dependencies are installed, and they must not
pay for importing FastAPI, uvicorn, pywebview or the audio stack. Every heavy
import is therefore performed *inside* the subcommand that needs it, never at
module level -- including ``sounddevice``, so ``audio devices`` can still explain
that PortAudio is missing instead of failing to start.

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
            "Phase 2 implements the foundation and offline audio capture: still "
            "no ASR, no diarization, no speaker identification, no LLM, no export."
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
            "future phase (a missing AI library, model or OpenVINO is always a "
            "WARN). FAIL = required now and not satisfied -- in Phase 2 that "
            "includes the audio backend and a usable capture device. Exit 0 when "
            "there is no FAIL; 1 on any FAIL; 2 with --strict when there is a "
            "WARN. Creates nothing, changes nothing, opens no microphone."
        ),
    )
    doctor.add_argument("--json", action="store_true", help="Machine-readable output on stdout.")
    doctor.add_argument(
        "--strict", action="store_true", help="Exit 2 when any check reports WARN."
    )
    doctor.add_argument(
        "--production",
        action="store_true",
        help=(
            "Apply the production gate: a built-in microphone, an unrecovered "
            "recording or a missing calibration become FAIL instead of WARN. "
            "Opens no audio stream."
        ),
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

    # -- audio -------------------------------------------------------------
    audio = sub.add_parser(
        "audio",
        parents=[common],
        help="Offline audio capture: devices, calibration, verification, recovery.",
        description=(
            "Phase 2 capture tooling. `devices`, `verify`, `recover` and `smoke` "
            "never open the microphone. `probe` and `calibrate` do, and only when "
            "you run them."
        ),
    )
    audio_sub = audio.add_subparsers(dest="audio_command", metavar="SUBCOMMAND")

    audio_devices = audio_sub.add_parser(
        "devices", parents=[common], help="List capture devices. Opens no stream."
    )
    audio_devices.add_argument("--json", action="store_true")
    audio_devices.add_argument(
        "--all", action="store_true", help="Include rejected devices and why."
    )

    audio_probe = audio_sub.add_parser(
        "probe",
        parents=[common],
        help="Preflight, plus an optional brief microphone open test.",
    )
    audio_probe.add_argument("--json", action="store_true")
    audio_probe.add_argument(
        "--open-test",
        action="store_true",
        help="Briefly OPEN THE MICROPHONE to prove it delivers audio.",
    )
    audio_probe.add_argument("--minutes", type=float, default=120.0)

    audio_cal = audio_sub.add_parser(
        "calibrate",
        parents=[common],
        help="Microphone level test. OPENS THE MICROPHONE for 10-15 s.",
    )
    audio_cal.add_argument("--json", action="store_true")
    audio_cal.add_argument("--seconds", type=float, default=None)

    audio_verify = audio_sub.add_parser(
        "verify", parents=[common], help="Verify a recording's chunks and manifest."
    )
    audio_verify.add_argument("recording_uuid", nargs="?", default=None)
    audio_verify.add_argument("--json", action="store_true")

    audio_recover = audio_sub.add_parser(
        "recover",
        parents=[common],
        help="Recover interrupted recordings. Idempotent; opens no stream.",
    )
    audio_recover.add_argument("--json", action="store_true")

    audio_smoke = audio_sub.add_parser(
        "smoke",
        parents=[common],
        help="Fake-backend capture + recovery smoke test. No microphone, no GUI.",
    )
    audio_smoke.add_argument("--json", action="store_true")

    audio_bench = audio_sub.add_parser(
        "bench",
        parents=[common],
        help="Accelerated fake-backend capture benchmark. No microphone.",
    )
    audio_bench.add_argument("--json", action="store_true")
    audio_bench.add_argument(
        "--minutes", type=float, default=10.0, help="Simulated audio minutes."
    )
    audio_bench.add_argument(
        "--speed", type=float, default=60.0, help="Times faster than real time."
    )

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

    production = bool(getattr(args, "production", False))
    report = run_doctor(config=config, ensure_dirs=False, production=production)
    if args.json:
        payload = report.to_dict()
        payload["production_gate"] = production
        payload["exit_code"] = report.exit_code(strict=args.strict)
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(format_report(report, strict=args.strict))
        if production:
            print(
                "\nProduction gate applied: a built-in microphone, an unrecovered "
                "recording or a missing calibration is a FAIL here."
            )
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
            f"The registry is empty. That is the correct Phase {CURRENT_PHASE} "
            "state: no ASR, diarization,",
            "speaker-embedding or LLM provider has been selected and no model has "
            "been downloaded.",
            "Provider selection is deferred to the Phase 4A benchmark.",
        ]
    _emit(payload, as_json=args.json, text="\n".join(lines))
    return EXIT_OK


def _audio_service(args: argparse.Namespace, *, ensure: bool = False):
    """Build a recording service. Imports the audio stack lazily."""
    from mom_igd.audio.service import RecordingService

    config = _load(args)
    paths = config.runtime_paths()
    if ensure:
        paths.ensure()
    return config, paths, RecordingService(config, paths)


def _cmd_audio_devices(args: argparse.Namespace) -> int:
    from mom_igd.audio.devices import format_device_table

    _config, _paths, service = _audio_service(args)
    payload = service.list_devices(refresh=True)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return EXIT_OK if payload["devices"] else EXIT_FAILURE

    devices = service._discovery.input_devices(refresh=False)  # noqa: SLF001
    print(format_device_table(devices))
    if args.all:
        print("\nExcluded devices:")
        for entry in payload["rejected"]:
            print(f"  {entry['name']!r} [{entry['host_api']}]\n      {entry['reason']}")
    print()
    if payload["selected_fingerprint"]:
        print(f"Selected: {payload['selected_fingerprint']}")
    else:
        print("No device selected yet. Pick one in the desktop shell, or set")
        print("audio.preferred_device_fingerprint in config/local.toml.")
    if not payload["verified_usb_available"]:
        print(
            "\nNo USB conference microphone verified by Windows. The built-in array is\n"
            "development only: its beamforming suppresses speakers who are not facing\n"
            "the laptop, which loses voices in a nine-person meeting."
        )
    return EXIT_OK if payload["devices"] else EXIT_FAILURE


def _cmd_audio_probe(args: argparse.Namespace) -> int:
    _config, _paths, service = _audio_service(args, ensure=True)
    report = service.preflight(planned_minutes=args.minutes)
    payload = report.to_dict()
    if args.open_test:
        try:
            payload["open_test"] = service.open_test()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            payload["open_test"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(f"Preflight for a {args.minutes:.0f}-minute meeting\n")
        for item in report.items:
            print(f"[{item.status.value:<4}] {item.key:<20} {item.detail}")
        if "open_test" in payload:
            test = payload["open_test"]
            print(f"\n[{'ok  ' if test.get('ok') else 'FAIL'}] open_test           {test.get('detail')}")
        print()
        print("READY TO RECORD" if report.can_start else "NOT READY -- fix the FAIL items above")
    ok = report.can_start and payload.get("open_test", {}).get("ok", True)
    return EXIT_OK if ok else EXIT_FAILURE


def _cmd_audio_calibrate(args: argparse.Namespace) -> int:
    _config, _paths, service = _audio_service(args, ensure=True)
    print(
        "Opening the microphone. Speak normally from where people will sit, or let "
        "the room be quiet to measure the noise floor.",
        file=sys.stderr,
    )
    result = service.calibrate(seconds=args.seconds)
    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        levels = result.snapshot
        print(f"Device      : {result.device.get('name')} [{result.device.get('transport')}]")
        print(f"Format      : {result.profile['sample_rate']} Hz / "
              f"{result.profile['channels']} ch / {result.profile['sample_format']}")
        print(f"Duration    : {result.seconds:.1f} s ({result.frames} frames)")
        print(f"RMS         : {levels.rms_dbfs:.1f} dBFS")
        print(f"Peak        : {levels.peak_dbfs:.1f} dBFS")
        print(f"Clipping    : {levels.clipping_percent:.3f} %")
        print(f"Silence     : {levels.silence_percent:.1f} %")
        print(f"Noise floor : {levels.noise_floor_dbfs:.1f} dBFS")
        for channel in levels.channels:
            state = "active" if channel.active else "INACTIVE"
            print(f"Channel {channel.channel}   : {channel.rms_dbfs:.1f} dBFS rms, {state}")
        print(f"xruns       : {result.xrun_callbacks}")
        print(f"\nVerdict     : {result.verdict.value}")
        print(f"             {result.verdict.advice}")
        if result.error:
            print(f"\nError: {result.error}", file=sys.stderr)
    return EXIT_OK if result.ok else EXIT_FAILURE


def _cmd_audio_verify(args: argparse.Namespace) -> int:
    from mom_igd.audio.manifest import verify_manifest

    config, paths, service = _audio_service(args)
    if args.recording_uuid:
        payload = service.verify(args.recording_uuid.lower())
        reports = [payload]
    else:
        reports = []
        root = paths.recordings_dir
        for manifest in sorted(root.rglob("manifest.jsonl")):
            report = verify_manifest(manifest.parent)
            entry = report.to_dict()
            entry["recording"] = f"{manifest.parent.parent.name}/{manifest.parent.name}"
            reports.append(entry)
        payload = {"recordings": len(reports), "reports": reports}

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        if not reports:
            print("No recordings found.")
            return EXIT_OK
        for entry in reports:
            label = entry.get("recording", entry.get("directory", "?"))
            mark = "ok  " if entry.get("ok") else "FAIL"
            print(
                f"[{mark}] {label}  chunks={entry.get('verified_chunks')}/"
                f"{entry.get('chunk_count')}  frames={entry.get('total_frames')}  "
                f"chain={str(entry.get('chain_sha256'))[:12]}"
            )
            for problem in entry.get("problems", []):
                print(f"        {problem}")
            for problem in entry.get("database_mismatches", []):
                print(f"        DB: {problem}")
    return EXIT_OK if all(r.get("ok") for r in reports) else EXIT_FAILURE


def _cmd_audio_recover(args: argparse.Namespace) -> int:
    _config, _paths, service = _audio_service(args, ensure=True)
    payload = service.recover_all()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(f"Scanned            : {payload['scanned']} interrupted recording(s)")
        print(f"Chunks recovered   : {payload['recovered_chunks']}")
        print(f"Chunks quarantined : {payload['quarantined_chunks']}")
        for report in payload["reports"]:
            print(f"\n  {report['directory']}")
            for chunk in report["chunks"]:
                detail = chunk.get("reason") or (
                    f"{chunk['frames_recovered']} frames, "
                    f"{chunk['trailing_bytes_discarded']} trailing byte(s) discarded"
                )
                print(f"    chunk {chunk['seq']}: {chunk['outcome']} -- {detail}")
            for problem in report["problems"]:
                print(f"    problem: {problem}")
        if payload["quarantined_chunks"]:
            print(
                "\nQuarantined files are kept as evidence under each recording's "
                "quarantine/ directory; nothing was deleted."
            )
    return EXIT_OK if all(r["ok"] for r in payload["reports"]) else EXIT_FAILURE


def _cmd_audio_smoke(args: argparse.Namespace) -> int:
    from mom_igd.audio.bench import run_capture_smoke

    config = _load(args)
    result = run_capture_smoke(config)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        for step in result["steps"]:
            print(f"[{'ok  ' if step['ok'] else 'FAIL'}] {step['name']}: {step['detail']}")
        print()
        print(
            f"Audio capture smoke: {'PASS' if result['ok'] else 'FAIL'} "
            f"({result['passed']}/{result['total']} steps)"
        )
    return EXIT_OK if result["ok"] else EXIT_FAILURE


def _cmd_audio_bench(args: argparse.Namespace) -> int:
    from mom_igd.audio.bench import run_capture_benchmark

    config = _load(args)
    result = run_capture_benchmark(
        config, audio_minutes=args.minutes, speed=args.speed
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(f"Fake-backend capture benchmark ({args.minutes:g} simulated minutes "
              f"at {args.speed:g}x)\n")
        for key, value in result["measured"].items():
            print(f"  {key:<26} {value}")
        print("\n  Targets:")
        for key, verdict in result["targets"].items():
            print(f"  {key:<26} {verdict}")
        print()
        # Print the note the benchmark actually produced, rather than a fixed
        # sentence: it is the only place an incomplete run announces itself.
        print(f"NOTE: {result['note']}")
        if not result.get("coverage_complete", True):
            print()
            print("Coverage was incomplete, so this run is not a full-length soak.")
    return EXIT_OK if result["ok"] else EXIT_FAILURE


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
    ("audio", "devices"): _cmd_audio_devices,
    ("audio", "probe"): _cmd_audio_probe,
    ("audio", "calibrate"): _cmd_audio_calibrate,
    ("audio", "verify"): _cmd_audio_verify,
    ("audio", "recover"): _cmd_audio_recover,
    ("audio", "smoke"): _cmd_audio_smoke,
    ("audio", "bench"): _cmd_audio_bench,
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
    elif args.command == "audio":
        sub_name = getattr(args, "audio_command", None)

    if args.command in {"db", "config", "registry", "audio"} and not sub_name:
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
            f"Missing dependency: {exc.name}. This command needs the project's "
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
