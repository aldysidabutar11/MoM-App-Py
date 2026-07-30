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
            "Phase 3 adds participants, biometric consent and encrypted voice "
            "enrollment on top of the Phase 2 capture engine: still no ASR, no "
            "diarization, no speaker identification, no LLM, no export."
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

    # -- asr ---------------------------------------------------------------
    asr = sub.add_parser(
        "asr",
        parents=[common],
        help="Offline ASR: model provisioning, verification and transcription.",
        description=(
            "Phase 4 tooling. `provision` is the ONLY command in this application "
            "that downloads anything, and it is the only place network access is "
            "expected -- nothing else, including transcription, ever fetches a "
            "model. A missing model fails closed as MODEL_UNAVAILABLE."
        ),
    )
    asr_sub = asr.add_subparsers(dest="asr_command", metavar="SUBCOMMAND")

    asr_models = asr_sub.add_parser(
        "models",
        parents=[common],
        help="List the model catalogue and what is provisioned. Offline.",
    )
    asr_models.add_argument("--json", action="store_true")

    asr_provision = asr_sub.add_parser(
        "provision",
        parents=[common],
        help="DOWNLOAD and verify a catalogue model. Requires network; run once.",
        description=(
            "Downloads into a staging directory, verifies every file's size and "
            "SHA-256, writes a manifest, then promotes atomically and re-verifies "
            "from the promoted path. Idempotent: an already-verified model is left "
            "alone. Takes a catalogue KEY, never a repository id, so an unreviewed "
            "artefact cannot be introduced from the command line."
        ),
    )
    asr_provision.add_argument(
        "key",
        nargs="?",
        default="all",
        help="Catalogue key (`asr-pass1`, `asr-pass2`) or `all`.",
    )
    asr_provision.add_argument("--json", action="store_true")
    asr_provision.add_argument(
        "--force", action="store_true", help="Re-download even if already verified."
    )
    asr_provision.add_argument(
        "--yes",
        action="store_true",
        help="Proceed without the interactive confirmation of size and licence.",
    )

    asr_verify = asr_sub.add_parser(
        "verify",
        parents=[common],
        help="Deep-verify provisioned models against their manifests. Offline.",
    )
    asr_verify.add_argument("--json", action="store_true")

    asr_smoke = asr_sub.add_parser(
        "smoke",
        parents=[common],
        help="Load the real local model and transcribe local audio. Offline.",
        description=(
            "Proves the provisioned model loads from a local path and transcribes "
            "without any network access. With no --audio it uses deterministic "
            "synthetic audio, which needs no microphone and no corpus but proves only "
            "the plumbing -- a tone is not speech. Pass --audio with your own "
            "16 kHz mono PCM16 WAV to exercise the real speech path. Neither mode "
            "measures accuracy; that needs a reference transcript and the consent "
            "metadata 'asr bench --manifest' requires."
        ),
    )
    asr_smoke.add_argument("--json", action="store_true")
    asr_smoke.add_argument(
        "--seconds", type=float, default=8.0, help="Synthetic audio duration."
    )
    asr_smoke.add_argument(
        "--audio",
        default=None,
        help=(
            "Path to a local 16 kHz mono PCM16 WAV to transcribe instead of generated "
            "audio. Read only: never converted, moved or deleted."
        ),
    )

    asr_bench = asr_sub.add_parser(
        "bench",
        parents=[common],
        help="Phase 4A benchmark harness. Real models, real timings.",
    )
    asr_bench.add_argument("--json", action="store_true")
    asr_bench.add_argument(
        "--manifest", default=None, help="Evaluation corpus manifest (JSON)."
    )
    asr_bench.add_argument(
        "--threads",
        default=None,
        help="Comma-separated CPU thread counts to sweep, e.g. 4,6,8,10,12.",
    )
    asr_bench.add_argument(
        "--models", default=None, help="Comma-separated catalogue keys to benchmark."
    )
    asr_bench.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="Synthetic audio duration when no corpus manifest is supplied.",
    )
    asr_bench.add_argument(
        "--out", default=None, help="Write the machine-readable result to this path."
    )

    asr_transcribe = asr_sub.add_parser(
        "transcribe",
        parents=[common],
        help="Run the offline pipeline over one recorded meeting.",
        description=(
            "Normalises the master to a 16 kHz mono working copy, detects speech "
            "regions, transcribes every region, re-transcribes the least confident "
            "ones under a budget, corrects technical spellings, and writes a new "
            "transcript revision. Each heavy stage runs in its own worker process "
            "that exits before the next starts. The master audio is never modified. "
            "A missing model is MODEL_UNAVAILABLE -- nothing is downloaded."
        ),
    )
    asr_transcribe.add_argument("recording_uuid", help="Recording UUID to transcribe.")
    asr_transcribe.add_argument("--json", action="store_true")
    asr_transcribe.add_argument(
        "--no-pass2",
        action="store_true",
        help="Skip the second pass for this run only. Configuration is unchanged.",
    )

    asr_transcript = asr_sub.add_parser(
        "transcript",
        parents=[common],
        help="Show a stored transcript revision. Loads no model.",
    )
    asr_transcript.add_argument("recording_uuid")
    asr_transcript.add_argument("--json", action="store_true")
    asr_transcript.add_argument(
        "--revision", type=int, default=None, help="Defaults to the active revision."
    )
    asr_transcript.add_argument(
        "--flagged",
        action="store_true",
        help="Show only regions a pass-2 selection rule fired on, with the reasons.",
    )

    asr_revisions = asr_sub.add_parser(
        "revisions",
        parents=[common],
        help="List every transcript revision for a recording, newest first.",
    )
    asr_revisions.add_argument("recording_uuid")
    asr_revisions.add_argument("--json", action="store_true")

    # -- participant -------------------------------------------------------
    participant = sub.add_parser(
        "participant",
        parents=[common],
        help="Participants, biometric consent, enrollment and voiceprints.",
        description=(
            "Phase 3 tooling. Every subcommand here is read-only EXCEPT `create`, "
            "`update`, `deactivate`, `consent grant`, `consent revoke`, "
            "`enrollment cancel` and `cleanup-retry`. None of them opens the "
            "microphone, creates the encryption key or loads a model. "
            "`consent grant` and `consent revoke` require an exact typed "
            "confirmation: consent is a decision, not a flag."
        ),
    )
    participant_sub = participant.add_subparsers(
        dest="participant_command", metavar="SUBCOMMAND"
    )

    p_list = participant_sub.add_parser(
        "list", parents=[common], help="List participants with consent state."
    )
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--search", default=None, help="Filter by name or role.")
    p_list.add_argument("--limit", type=int, default=50)

    p_create = participant_sub.add_parser(
        "create",
        parents=[common],
        help="Register a participant. Duplicate names are allowed.",
    )
    p_create.add_argument("name", help="Display name (not an identifier).")
    p_create.add_argument("--role", default=None)
    p_create.add_argument("--json", action="store_true")

    p_update = participant_sub.add_parser(
        "update", parents=[common], help="Edit descriptive fields."
    )
    p_update.add_argument("participant_uuid")
    p_update.add_argument("--name", default=None)
    p_update.add_argument("--role", default=None)
    p_update.add_argument("--json", action="store_true")

    p_deact = participant_sub.add_parser(
        "deactivate",
        parents=[common],
        help="Deactivate a participant. Never deletes the row.",
    )
    p_deact.add_argument("participant_uuid")
    p_deact.add_argument("--reason", default=None)
    p_deact.add_argument(
        "--reactivate",
        action="store_true",
        help="Reactivate instead of deactivating.",
    )
    p_deact.add_argument("--json", action="store_true")

    p_consent = participant_sub.add_parser(
        "consent", parents=[common], help="Biometric consent status, grant and revoke."
    )
    p_consent.add_argument("participant_uuid")
    p_consent.add_argument(
        "--action",
        choices=["status", "grant", "revoke"],
        default="status",
        help="Default `status`, which changes nothing.",
    )
    p_consent.add_argument(
        "--confirm",
        default=None,
        metavar="PHRASE",
        help=(
            'Exact confirmation phrase. Grant requires "SAYA SETUJU"; revoke '
            'requires "CABUT". Without it the command explains and changes nothing.'
        ),
    )
    p_consent.add_argument("--reason", default=None)
    p_consent.add_argument("--limit", type=int, default=20)
    p_consent.add_argument("--json", action="store_true")

    p_enroll = participant_sub.add_parser(
        "enrollment",
        parents=[common],
        help="Enrollment readiness and status. Opens no microphone.",
    )
    p_enroll.add_argument("participant_uuid", nargs="?", default=None)
    p_enroll.add_argument(
        "--cancel", action="store_true", help="Abandon the live enrollment session."
    )
    p_enroll.add_argument("--reason", default=None)
    p_enroll.add_argument("--json", action="store_true")

    p_vp = participant_sub.add_parser(
        "voiceprint",
        parents=[common],
        help="Voiceprint status, and keyless integrity verification.",
    )
    p_vp.add_argument("participant_uuid")
    p_vp.add_argument(
        "--verify",
        action="store_true",
        help="Verify the envelope hash and bindings. Unwraps no key.",
    )
    p_vp.add_argument("--json", action="store_true")

    p_cleanup = participant_sub.add_parser(
        "cleanup",
        parents=[common],
        help="Voiceprints awaiting deletion or finalisation.",
    )
    p_cleanup.add_argument(
        "--retry",
        action="store_true",
        help="Retry deletion for DELETE_PENDING templates. Idempotent.",
    )
    p_cleanup.add_argument("--json", action="store_true")

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
            "the laptop, which loses voices in any meeting with several people around\n"
            "a table, and progressively more as the room gets larger."
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


# ---------------------------------------------------------------------------
# Phase 3: participants, consent, enrollment, voiceprints
#
# `list`, `status`, `readiness`, `verify` and `cleanup-status` are read-only:
# they open no microphone, create no DPAPI key, decrypt nothing and load no
# model. `consent grant` and `consent revoke` change what the application is
# permitted to do with a person's biometric data, so both demand an explicit
# typed confirmation -- there is deliberately no flag that grants consent
# silently, because consent obtained without someone deciding is not consent.
# ---------------------------------------------------------------------------


def _participant_services(args: argparse.Namespace):
    """Build the Phase 3 services. Imports the enrollment stack lazily."""
    from mom_igd.db.connection import connect
    from mom_igd.enrollment.consent import ConsentService
    from mom_igd.enrollment.participants import ParticipantService

    config = _load(args)
    paths = config.runtime_paths()

    def _connect():
        return connect(
            paths.database_path(config.database.filename),
            busy_timeout_ms=config.database.busy_timeout_ms,
        )

    # `config=` is not optional in practice. Without it the service falls back to
    # its built-in 9/50, so an operator who configured a different default or
    # ceiling would silently get the shipped numbers on every CLI command while the
    # GUI honoured their configuration. Two runtimes disagreeing about the same
    # policy is worse than either answer.
    return (
        config,
        paths,
        ParticipantService(_connect, config=config),
        ConsentService(_connect),
        _connect,
    )


def _participant_id(connect_fn, participant_uuid: str) -> int:
    conn = connect_fn()
    try:
        row = conn.execute(
            "SELECT id FROM participants WHERE uuid = ?", (participant_uuid,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise SystemExit(f"No participant with uuid={participant_uuid!r}.")
    return int(row["id"])


def _voiceprint_store(config, paths):
    from mom_igd.db.connection import connect
    from mom_igd.enrollment.store import VoiceprintStore

    def _connect():
        return connect(
            paths.database_path(config.database.filename),
            busy_timeout_ms=config.database.busy_timeout_ms,
        )

    return VoiceprintStore(paths.voiceprints_dir, _connect)


def _cmd_participant_list(args: argparse.Namespace) -> int:
    config, paths, people, consent, connect_fn = _participant_services(args)
    listing = people.list(
        search=args.search, include_inactive=True, limit=args.limit, offset=0
    )
    rows = []
    conn = connect_fn()
    try:
        for entry in listing["participants"]:
            pid = int(
                conn.execute(
                    "SELECT id FROM participants WHERE uuid = ?", (entry["uuid"],)
                ).fetchone()["id"]
            )
            state = consent.state(conn, pid)
            entry["consent_active"] = state.active
            rows.append(entry)
    finally:
        conn.close()

    if args.json:
        _emit({"total": listing["total"], "participants": rows}, as_json=True, text="")
        return EXIT_OK

    lines = [
        f"{listing['total']} participant(s) registered",
        "",
        f"{'UUID':38s} {'NAME':26s} {'ROLE':16s} {'ACTIVE':7s} CONSENT",
        "-" * 100,
    ]
    for entry in rows:
        lines.append(
            f"{entry['uuid']:38s} {(entry['display_name'] or '')[:25]:26s} "
            f"{(entry['role'] or '-')[:15]:16s} "
            f"{'yes' if entry['is_active'] else 'no':7s} "
            f"{'active' if entry['consent_active'] else 'none/revoked'}"
        )
    if not rows:
        lines.append("(none)")
    _emit(None, as_json=False, text="\n".join(lines))
    return EXIT_OK


def _cmd_participant_create(args: argparse.Namespace) -> int:
    _config, _paths, people, _consent, _connect = _participant_services(args)
    person = people.create(display_name=args.name, role=args.role)
    _emit(
        person.to_dict(),
        as_json=args.json,
        text=f"Created participant {person.uuid} ({person.display_name}).",
    )
    return EXIT_OK


def _cmd_participant_update(args: argparse.Namespace) -> int:
    _config, _paths, people, _consent, _connect = _participant_services(args)
    person = people.update(
        args.participant_uuid.lower(), display_name=args.name, role=args.role
    )
    _emit(
        person.to_dict(),
        as_json=args.json,
        text=f"Updated participant {person.uuid} ({person.display_name}).",
    )
    return EXIT_OK


def _cmd_participant_deactivate(args: argparse.Namespace) -> int:
    _config, _paths, people, _consent, _connect = _participant_services(args)
    person = people.set_active(
        args.participant_uuid.lower(),
        active=bool(args.reactivate),
        reason=args.reason,
    )
    verb = "Reactivated" if person.is_active else "Deactivated"
    _emit(
        person.to_dict(),
        as_json=args.json,
        text=(
            f"{verb} participant {person.uuid} ({person.display_name}). "
            "The row is never deleted: history references it."
        ),
    )
    return EXIT_OK


def _cmd_participant_consent_status(args: argparse.Namespace) -> int:
    _config, _paths, _people, consent, connect_fn = _participant_services(args)
    pid = _participant_id(connect_fn, args.participant_uuid.lower())
    conn = connect_fn()
    try:
        state = consent.state(conn, pid)
        history = consent.history(conn, pid, limit=args.limit)
    finally:
        conn.close()
    if args.json:
        _emit({"consent": state.to_dict(), "history": history}, as_json=True, text="")
        return EXIT_OK
    lines = [
        f"Consent for {args.participant_uuid.lower()}",
        f"  active          : {state.active}",
        f"  version         : {state.consent_version or '-'}",
        f"  text sha256     : {(state.consent_text_sha256 or '-')[:16]}",
        f"  matches current : {state.text_matches_current}",
        f"  purpose         : {state.purpose or '-'}",
        f"  recorded        : {state.occurred_at or '-'}",
        "",
        "History (newest first, append-only):",
    ]
    for event in history:
        lines.append(
            f"  {event['occurred_at']}  {event['action']:8s} "
            f"v{event['consent_version']}  {event['confirmation_method']}"
        )
    if not history:
        lines.append("  (no consent event recorded)")
    _emit(None, as_json=False, text="\n".join(lines))
    return EXIT_OK


_CONSENT_GRANT_PHRASE = "SAYA SETUJU"
_CONSENT_REVOKE_PHRASE = "CABUT"


def _cmd_participant_consent_grant(args: argparse.Namespace) -> int:
    """Record consent. Requires the operator to type an exact phrase.

    Consent is not a flag. A ``--yes`` switch would let a script grant biometric
    permission for someone who never agreed, which is the one thing this whole
    subsystem exists to prevent.
    """
    from mom_igd.enrollment.consent import (
        CONSENT_PURPOSE,
        CONSENT_TEXT_SHA256,
        CONSENT_TEXT_V1,
        CONSENT_VERSION,
        ConfirmationMethod,
    )

    _config, _paths, _people, consent, connect_fn = _participant_services(args)
    pid = _participant_id(connect_fn, args.participant_uuid.lower())

    if args.confirm != _CONSENT_GRANT_PHRASE:
        print(CONSENT_TEXT_V1)
        print("-" * 78)
        print(f"Version : {CONSENT_VERSION}")
        print(f"Purpose : {CONSENT_PURPOSE}")
        print(f"SHA-256 : {CONSENT_TEXT_SHA256}")
        print("-" * 78)
        print(
            "The text above must be read to the participant, by them or with them.\n"
            "To record their consent, re-run this command with:\n"
            f'    --confirm "{_CONSENT_GRANT_PHRASE}"\n'
            "Nothing has been recorded.",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    result = consent.grant(
        pid,
        confirmation_method=ConfirmationMethod.OPERATOR_CONFIRMED_IN_PERSON,
        acknowledged_text_sha256=CONSENT_TEXT_SHA256,
    )
    _emit(
        result,
        as_json=args.json,
        text=(
            f"Consent recorded for {args.participant_uuid.lower()} "
            f"(event {result['event_uuid']}, version {CONSENT_VERSION})."
            + (" Already active; no duplicate event was appended."
               if result.get("already_active") else "")
        ),
    )
    return EXIT_OK


def _cmd_participant_consent_revoke(args: argparse.Namespace) -> int:
    """Withdraw consent and destroy every voiceprint for that participant."""
    from mom_igd.audio.service import RecordingService
    from mom_igd.enrollment.service import EnrollmentService

    config, paths, _people, _consent, connect_fn = _participant_services(args)
    _participant_id(connect_fn, args.participant_uuid.lower())

    if args.confirm != _CONSENT_REVOKE_PHRASE:
        print(
            "Revoking consent will:\n"
            "  - delete this participant's encrypted voiceprint;\n"
            "  - make future speaker identification report UNKNOWN for them;\n"
            "  - NOT delete existing minutes or meeting recordings;\n"
            "  - require a completely new enrollment if they consent again.\n"
            "To proceed, re-run with:\n"
            f'    --confirm "{_CONSENT_REVOKE_PHRASE}"\n'
            "Nothing has been changed.",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    service = EnrollmentService(
        config, paths, recording_service=RecordingService(config, paths)
    )
    result = service.revoke_consent_and_delete(
        args.participant_uuid.lower(), reason=args.reason or "revoked via CLI"
    )
    deletion = result["deletion"]
    lines = [
        f"Consent revoked for {args.participant_uuid.lower()}.",
        f"  voiceprints deleted      : {len(deletion['deleted'])}",
        f"  deletion still pending   : {len(deletion['delete_pending'])}",
        f"  eligible for identification: {result['eligible']}",
    ]
    if deletion["delete_pending"]:
        lines.append(
            "  The pending templates are already unusable. Run "
            "`participant cleanup-retry` to finish removing the files."
        )
    _emit(result, as_json=args.json, text="\n".join(lines))
    return EXIT_OK


def _cmd_participant_enrollment_status(args: argparse.Namespace) -> int:
    """Report enrollment readiness and any live session. Opens no device."""
    from mom_igd.audio.service import RecordingService
    from mom_igd.enrollment.service import EnrollmentService

    config, paths, _people, _consent, connect_fn = _participant_services(args)
    service = EnrollmentService(
        config, paths, recording_service=RecordingService(config, paths)
    )
    payload: dict[str, Any] = {"session": service.status()}
    if args.participant_uuid:
        uuid_value = args.participant_uuid.lower()
        _participant_id(connect_fn, uuid_value)
        payload["readiness"] = service.readiness(uuid_value)
        payload["eligibility"] = service.eligibility(uuid_value)

    if args.json:
        _emit(payload, as_json=True, text="")
        return EXIT_OK

    session = payload["session"]
    lines = [
        "Enrollment session:",
        f"  active         : {session['active']}",
        f"  state          : {session['state']}",
        f"  samples        : {session['samples_accepted']} / {session['samples_target']}",
    ]
    readiness = payload.get("readiness")
    if readiness:
        model = readiness["model"]
        device = readiness["device"]
        lines += [
            "",
            f"Readiness for {args.participant_uuid.lower()}:",
            f"  can start      : {readiness['can_start']}",
            f"  blockers       : {', '.join(readiness['blockers']) or 'none'}",
            f"  consent active : {readiness['consent']['active']}",
            f"  model ready    : {model['ready']}",
            f"  device         : {device['detail']} ({device['transport']})",
            f"  USB verified   : {device['production_eligible_device']}",
            f"  calibration    : {readiness['calibration']['verdict']} "
            f"({readiness['calibration']['age_days']} days)",
        ]
        if not model["ready"]:
            lines.append(
                "  NOTE: no speaker embedding model is provisioned, so enrollment "
                "cannot start and the microphone will not be opened."
            )
    _emit(None, as_json=False, text="\n".join(lines))
    return EXIT_OK


def _cmd_participant_enrollment_cancel(args: argparse.Namespace) -> int:
    from mom_igd.audio.service import RecordingService
    from mom_igd.enrollment.service import EnrollmentService

    config, paths, _people, _consent, _connect = _participant_services(args)
    service = EnrollmentService(
        config, paths, recording_service=RecordingService(config, paths)
    )
    status = service.cancel(reason=args.reason or "cancelled via CLI")
    _emit(
        status,
        as_json=args.json,
        text=(
            f"Enrollment state: {status['state']}. Any captured audio has been "
            "discarded and the shared capture lock is released."
        ),
    )
    return EXIT_OK


def _cmd_participant_voiceprint(args: argparse.Namespace) -> int:
    """Voiceprint status, and optionally a keyless integrity verification."""
    config, paths, _people, _consent, connect_fn = _participant_services(args)
    store = _voiceprint_store(config, paths)
    pid = _participant_id(connect_fn, args.participant_uuid.lower())
    payload = store.status_for_participant(pid)

    if args.verify and payload["current"]:
        payload["verification"] = store.verify(
            payload["current"]["voiceprint_uuid"]
        ).to_dict()

    if args.json:
        _emit(payload, as_json=True, text="")
        return EXIT_OK

    current = payload["current"]
    lines = [
        f"Voiceprint for {args.participant_uuid.lower()}:",
        f"  usable            : {payload['has_usable_voiceprint']}",
        f"  production eligible: {payload['production_eligible']}",
    ]
    if current:
        lines += [
            f"  uuid              : {current['voiceprint_uuid']}",
            f"  status            : {current['status']}",
            f"  model             : {current['model']['name']} {current['model']['version']}",
            f"  quality           : {current['quality_verdict']}",
            f"  min pair cosine   : {current['min_pair_cosine']}",
            f"  device transport  : {current['device_transport']}",
        ]
    else:
        lines.append("  (no voiceprint has been created)")
    verification = payload.get("verification")
    if verification:
        lines += [
            "",
            f"Integrity: {'OK' if verification['ok'] else 'PROBLEM'} "
            f"(status {verification['status']})",
        ]
        for problem in verification["problems"]:
            lines.append(f"  - {problem}")
    lines.append("")
    lines.append(
        f"History: {len(payload['history'])} record(s). No biometric payload is ever "
        "printed."
    )
    _emit(None, as_json=False, text="\n".join(lines))
    return EXIT_OK


def _cmd_participant_cleanup_status(args: argparse.Namespace) -> int:
    config, paths, _people, _consent, connect_fn = _participant_services(args)
    conn = connect_fn()
    try:
        rows = conn.execute(
            "SELECT voiceprint_uuid, status, delete_error FROM voiceprints "
            "WHERE status IN ('DELETE_PENDING','PENDING_WRITE','INTEGRITY_FAILED') "
            "ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()
    payload = {
        "pending": [
            {
                "voiceprint_uuid": str(r["voiceprint_uuid"]),
                "status": str(r["status"]),
                "delete_error": r["delete_error"],
            }
            for r in rows
        ],
        "count": len(rows),
    }
    lines = [f"{len(rows)} voiceprint(s) need attention:"]
    for entry in payload["pending"]:
        lines.append(
            f"  {entry['voiceprint_uuid']}  {entry['status']}"
            + (f"  ({entry['delete_error']})" if entry["delete_error"] else "")
        )
    if not rows:
        lines = ["No voiceprint is awaiting deletion or finalisation."]
    _emit(payload, as_json=args.json, text="\n".join(lines))
    return EXIT_OK


def _cmd_participant_consent(args: argparse.Namespace) -> int:
    """Route `consent` by its --action. Default `status` changes nothing."""
    if args.action == "grant":
        return _cmd_participant_consent_grant(args)
    if args.action == "revoke":
        return _cmd_participant_consent_revoke(args)
    return _cmd_participant_consent_status(args)


def _cmd_participant_enrollment(args: argparse.Namespace) -> int:
    if args.cancel:
        return _cmd_participant_enrollment_cancel(args)
    return _cmd_participant_enrollment_status(args)


def _cmd_participant_cleanup(args: argparse.Namespace) -> int:
    if args.retry:
        return _cmd_participant_cleanup_retry(args)
    return _cmd_participant_cleanup_status(args)


def _cmd_participant_cleanup_retry(args: argparse.Namespace) -> int:
    """Retry deletion for DELETE_PENDING voiceprints. Idempotent."""
    config, paths, _people, _consent, _connect = _participant_services(args)
    report = _voiceprint_store(config, paths).retry_pending_cleanup()
    payload = report.to_dict()
    _emit(
        payload,
        as_json=args.json,
        text=(
            f"Cleanup retried: {payload['cleanup_retried']} deleted, "
            f"{payload['cleanup_still_pending']} still pending. Safe to run again."
        ),
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# asr -- Phase 4 offline speech recognition
#
# `provision` is the only command in this application that reaches the network.
# Everything else here is offline, and `models`/`verify` are import-light enough to
# run before any heavy dependency is installed.
# ---------------------------------------------------------------------------


def _asr_paths(args: argparse.Namespace):
    config = _load(args)
    return config, config.runtime_paths()


def _cmd_asr_models(args: argparse.Namespace) -> int:
    from mom_igd.asr.provision import MODEL_CATALOGUE, promoted_models

    _config, paths = _asr_paths(args)
    promoted = promoted_models(paths.models_dir)
    by_name = {(e["model_name"], e["revision"]): e for e in promoted}

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "models_dir": str(paths.models_dir),
                    "catalogue": [
                        {
                            "key": s.key,
                            "provider_slot": s.provider_slot,
                            "model_name": s.model_name,
                            "source_repo": s.repo_id,
                            "license": s.license_name,
                            "hardware_profile": s.hardware_profile,
                            "role": s.role,
                            "approximate_bytes": s.approximate_bytes,
                        }
                        for s in MODEL_CATALOGUE.values()
                    ],
                    "provisioned": promoted,
                },
                indent=2,
            )
        )
        return EXIT_OK

    print(f"Model store        : {paths.models_dir}")
    print("\nCatalogue (what this build is willing to provision):")
    for spec in MODEL_CATALOGUE.values():
        print(f"  {spec.key:11} {spec.model_name:34} {spec.license_name:4} "
              f"~{spec.approximate_mib:7.0f} MiB  role={spec.role}")
        print(f"              source {spec.repo_id}")
    if not promoted:
        print("\nProvisioned        : none")
        print("Transcription will answer MODEL_UNAVAILABLE until a model is provisioned.")
        print(f"Provision with     : {_PROG} asr provision all")
        return EXIT_OK
    print("\nProvisioned:")
    for entry in promoted:
        mark = "OK  " if entry.get("ok") else "BAD "
        size = entry.get("total_bytes") or 0
        print(f"  {mark}{entry['model_name']:34} rev {entry['revision'][:12]}  "
              f"{size / 2**20:7.0f} MiB  role={entry.get('role')}")
        if entry.get("problem"):
            print(f"       problem: {entry['problem']}")
    del by_name
    return EXIT_OK if all(e.get("ok") for e in promoted) else EXIT_FAILURE


def _cmd_asr_provision(args: argparse.Namespace) -> int:
    from mom_igd.asr.provision import (
        MODEL_CATALOGUE,
        ProvisionError,
        catalogue_entry,
        provision_model,
    )

    _config, paths = _asr_paths(args)
    paths.ensure()

    key = getattr(args, "key", "all") or "all"
    keys = list(MODEL_CATALOGUE) if key == "all" else [key]
    try:
        specs = [catalogue_entry(k) for k in keys]
    except ProvisionError as exc:
        print(f"{exc}", file=sys.stderr)
        return EXIT_FAILURE

    total = sum(s.approximate_bytes for s in specs)
    print("This command DOWNLOADS model artefacts. It is the only part of this")
    print("application that uses the network; the runtime never does.\n")
    for spec in specs:
        print(f"  {spec.key:11} {spec.repo_id}")
        print(f"              licence {spec.license_name}  ~{spec.approximate_mib:.0f} MiB"
              f"  profile {spec.hardware_profile}")
    print(f"\n  destination  {paths.models_dir}")
    print(f"  total        ~{total / 2**30:.2f} GiB")
    print("  no access token is used, and no gated artefact is accepted\n")

    if not getattr(args, "yes", False) and sys.stdin is not None and sys.stdin.isatty():
        try:
            answer = input("Proceed with the download? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in {"y", "yes"}:
            print("Nothing was downloaded.", file=sys.stderr)
            return EXIT_FAILURE

    results = []
    for spec in specs:
        print(f"\n--- {spec.key} ---")
        try:
            result = provision_model(
                spec.key,
                paths.models_dir,
                force=getattr(args, "force", False),
                progress=lambda message: print(f"  {message}"),
            )
        except ProvisionError as exc:
            print(f"  FAILED: {exc}", file=sys.stderr)
            return EXIT_FAILURE
        results.append(result)
        print(f"  {'already present' if result.already_present else 'provisioned'}: "
              f"{result.spec.model_name} rev {result.revision[:12]}")
        print(f"  files {len(result.manifest.files)}  "
              f"{result.total_bytes / 2**20:.0f} MiB  "
              f"manifest sha256 {result.manifest_digest[:16]}...")

    if getattr(args, "json", False):
        print(json.dumps([r.describe() for r in results], indent=2))
        return EXIT_OK

    print("\nAll requested models are provisioned and verified.")
    print(f"Declare them in the registry with : {_PROG} asr models --json")
    print(f"Re-verify at any time with        : {_PROG} asr verify")
    return EXIT_OK


def _cmd_asr_verify(args: argparse.Namespace) -> int:
    from pathlib import Path

    from mom_igd.asr.manifest import ManifestError
    from mom_igd.asr.provision import promoted_models, verify_model

    _config, paths = _asr_paths(args)
    promoted = promoted_models(paths.models_dir)
    if not promoted:
        message = "No model is provisioned, so there is nothing to verify."
        if getattr(args, "json", False):
            print(json.dumps({"models": [], "ok": False, "detail": message}, indent=2))
        else:
            print(message)
            print(f"Provision with: {_PROG} asr provision all")
        return EXIT_FAILURE

    report = []
    ok_all = True
    for entry in promoted:
        row = {k: entry[k] for k in ("provider_slot", "model_name", "revision")}
        try:
            manifest = verify_model(Path(entry["directory"]))
        except ManifestError as exc:
            ok_all = False
            row.update(ok=False, problem=str(exc))
        else:
            row.update(
                ok=True,
                problem=None,
                files=len(manifest.files),
                total_bytes=manifest.total_bytes,
                license=manifest.license_name,
                source_repo=manifest.source_repo,
            )
        report.append(row)

    if getattr(args, "json", False):
        print(json.dumps({"models": report, "ok": ok_all}, indent=2))
        return EXIT_OK if ok_all else EXIT_FAILURE

    for row in report:
        mark = "OK  " if row["ok"] else "FAIL"
        print(f"  {mark}{row['model_name']:34} rev {row['revision'][:12]}")
        if row["ok"]:
            print(f"       {row['files']} file(s), {row['total_bytes'] / 2**20:.0f} MiB, "
                  f"{row['license']}, from {row['source_repo']}")
        else:
            print(f"       {row['problem']}")
    print("\nEvery byte was re-hashed from disk." if ok_all
          else "\nAt least one model failed verification and will not be loaded.")
    return EXIT_OK if ok_all else EXIT_FAILURE


def _cmd_asr_smoke(args: argparse.Namespace) -> int:
    from mom_igd.asr.smoke import run_asr_smoke

    config, paths = _asr_paths(args)
    result = run_asr_smoke(
        config,
        paths,
        seconds=float(getattr(args, "seconds", 8.0) or 8.0),
        audio_path=getattr(args, "audio", None),
    )
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
        return EXIT_OK if result["ok"] else EXIT_FAILURE

    for step in result["steps"]:
        mark = "ok  " if step["ok"] else "FAIL"
        print(f"[{mark}] {step['name']}: {step['detail']}")
    total = len(result["steps"])
    passed = sum(1 for s in result["steps"] if s["ok"])
    print(f"\nASR offline smoke: {'PASS' if result['ok'] else 'FAIL'} ({passed}/{total} steps)")
    print(f"Mode: {result.get('mode', 'synthetic')} -- {result.get('claim', '')}")
    if not result["ok"] and result.get("error"):
        print(f"Reason: {result['error']}", file=sys.stderr)
    return EXIT_OK if result["ok"] else EXIT_FAILURE


def _cmd_asr_bench(args: argparse.Namespace) -> int:
    from mom_igd.asr.benchmark import BenchmarkError, run_benchmark

    config, paths = _asr_paths(args)
    threads = None
    if getattr(args, "threads", None):
        try:
            threads = [int(t) for t in str(args.threads).split(",") if t.strip()]
        except ValueError:
            print("--threads must be a comma-separated list of integers", file=sys.stderr)
            return EXIT_FAILURE
    models = None
    if getattr(args, "models", None):
        models = [m.strip() for m in str(args.models).split(",") if m.strip()]

    try:
        report = run_benchmark(
            config,
            paths,
            corpus_manifest=getattr(args, "manifest", None),
            thread_counts=threads,
            model_keys=models,
            synthetic_seconds=getattr(args, "seconds", None),
            progress=lambda message: print(f"  {message}", flush=True),
        )
    except BenchmarkError as exc:
        print(f"Benchmark refused: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    out = getattr(args, "out", None)
    if out:
        from pathlib import Path

        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\nMachine-readable result written to {target}")
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
        return EXIT_OK

    print("\n" + report["table"])
    for note in report.get("notes", []):
        print(f"\nNOTE: {note}")
    return EXIT_OK


def _asr_service(args: argparse.Namespace, *, no_pass2: bool = False):
    """Build the transcription service for a CLI run.

    ``--no-pass2`` rewrites the loaded configuration for this process only, through
    pydantic's own copy so validation still applies. It never touches a file: a
    command-line flag that edited configuration would change the next run too.
    """
    from mom_igd.asr.service import AsrService
    from mom_igd.db.connection import connect

    config, paths = _asr_paths(args)
    if no_pass2:
        config = config.model_copy(
            update={"asr": config.asr.model_copy(update={"pass2_enabled": False})}
        )

    def _connect():
        return connect(
            paths.database_path(config.database.filename),
            busy_timeout_ms=config.database.busy_timeout_ms,
        )

    return AsrService(_connect, config=config, paths=paths), config, paths


def _format_stamp(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _cmd_asr_transcribe(args: argparse.Namespace) -> int:
    from mom_igd.asr.service import AsrServiceError

    service, config, _paths = _asr_service(args, no_pass2=bool(args.no_pass2))
    models = service.status()["models"]
    if not models["pass1_ready"]:
        print(
            "MODEL_UNAVAILABLE: no pass-1 model is provisioned and verified.\n"
            "  Provision one with: python -m mom_igd asr provision asr-pass1\n"
            "  Transcription never downloads a model by itself.",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    try:
        result = service.transcribe(
            args.recording_uuid, progress=lambda message: print(f"  {message}", flush=True)
        )
    except AsrServiceError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    payload = result.to_dict()
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
        return EXIT_OK if result.ok else EXIT_FAILURE

    print()
    for stage in result.stages:
        print(f"[{'ok  ' if stage['ok'] else 'FAIL'}] {stage['name']}: {stage['detail']}")
    print()
    if not result.ok:
        print(f"Transcription FAILED: {result.error}", file=sys.stderr)
        return EXIT_FAILURE
    print(
        f"Transcript revision {result.revision}: {result.segment_count} segment(s), "
        f"{result.word_count} word(s) over {result.audio_ms / 1000:.1f}s of audio "
        f"({result.speech_ms / 1000:.1f}s of speech in {result.region_count} region(s))"
    )
    print(
        f"Cost: {result.wall_ms / 1000:.1f}s wall, RTF {result.rtf}, peak worker "
        f"{payload['peak_rss_mib']} MiB"
    )
    if result.pass2_skipped_reason:
        print(f"Pass 2: skipped -- {result.pass2_skipped_reason}")
    else:
        print(
            f"Pass 2: {result.pass2_region_count} region(s), "
            f"{result.pass2_selected_ms / 1000:.1f}s of a "
            f"{result.pass2_budget_ms / 1000:.1f}s budget"
            + (" (budget exhausted)" if result.pass2_budget_exhausted else "")
        )
    print(
        "Accuracy is NOT measured by this command. No reference transcript exists, and "
        "accuracy is never derived from the model's own output."
    )
    return EXIT_OK


def _cmd_asr_transcript(args: argparse.Namespace) -> int:
    from mom_igd.asr.service import AsrServiceError

    service, _config, _paths = _asr_service(args)
    try:
        if getattr(args, "flagged", False):
            payload: Any = {"flagged": service.flagged_regions(args.recording_uuid)}
        else:
            payload = service.get_transcript(
                args.recording_uuid, revision=getattr(args, "revision", None)
            )
    except AsrServiceError as exc:
        print(f"{exc}", file=sys.stderr)
        return EXIT_FAILURE

    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, default=str))
        return EXIT_OK

    if getattr(args, "flagged", False):
        rows = payload["flagged"]
        if not rows:
            print("No region tripped a pass-2 selection rule.")
            return EXIT_OK
        print(f"{'region':>6} {'pass':>4} {'start':>9} {'sel':>4} {'rank':>4}  reasons")
        for row in rows:
            print(
                f"{row['region_seq']!s:>6} {row['asr_pass']:>4} "
                f"{_format_stamp(row['start_ms']):>9} "
                f"{'yes' if row['selected_for_pass2'] else 'no':>4} "
                f"{row['rank'] if row['rank'] is not None else '-':>4}  "
                f"{', '.join(row['reason_codes'])}"
            )
        return EXIT_OK

    transcript = payload["transcript"]
    print(
        f"Revision {transcript['revision']} ({transcript['status']}"
        f"{', active' if transcript['is_active'] else ''}) -- "
        f"{transcript['segment_count']} segment(s), {transcript['word_count']} word(s)"
    )
    print(
        f"  pass 1: {transcript['pass1_model_name']} @ "
        f"{str(transcript['pass1_model_revision'] or '')[:12]} "
        f"(beam {transcript['pass1_beam_size']})"
    )
    if transcript["pass2_model_name"]:
        print(
            f"  pass 2: {transcript['pass2_model_name']} @ "
            f"{str(transcript['pass2_model_revision'] or '')[:12]} "
            f"(beam {transcript['pass2_beam_size']}, "
            f"{transcript['pass2_region_count']} region(s))"
        )
    else:
        print(f"  pass 2: not run -- {transcript['pass2_skipped_reason']}")
    if transcript["glossary_version"]:
        print(
            f"  glossary v{transcript['glossary_version']}: "
            f"{transcript['glossary_replacements']} correction(s)"
        )
    print()
    for segment in payload["segments"]:
        marker = "*" if segment["asr_pass"] == 2 else " "
        print(
            f"{marker}[{_format_stamp(segment['start_ms'])}] "
            f"({segment['speaker_status']}) {segment['text']}"
        )
    if any(segment["asr_pass"] == 2 for segment in payload["segments"]):
        print("\n* = re-transcribed by the second pass")
    return EXIT_OK


def _cmd_asr_revisions(args: argparse.Namespace) -> int:
    from mom_igd.asr.service import AsrServiceError

    service, _config, _paths = _asr_service(args)
    try:
        rows = service.list_revisions(args.recording_uuid)
    except AsrServiceError as exc:
        print(f"{exc}", file=sys.stderr)
        return EXIT_FAILURE
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, default=str))
        return EXIT_OK
    if not rows:
        print("No transcript revision exists for this recording yet.")
        return EXIT_OK
    print(f"{'rev':>4} {'status':>10} {'active':>6} {'segs':>5} {'words':>6}  created")
    for row in rows:
        print(
            f"{row['revision']:>4} {row['status']:>10} "
            f"{'yes' if row['is_active'] else '':>6} {row['segment_count']:>5} "
            f"{row['word_count']:>6}  {row['created_at']}"
        )
    return EXIT_OK


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
    ("asr", "models"): _cmd_asr_models,
    ("asr", "provision"): _cmd_asr_provision,
    ("asr", "verify"): _cmd_asr_verify,
    ("asr", "smoke"): _cmd_asr_smoke,
    ("asr", "bench"): _cmd_asr_bench,
    ("asr", "transcribe"): _cmd_asr_transcribe,
    ("asr", "transcript"): _cmd_asr_transcript,
    ("asr", "revisions"): _cmd_asr_revisions,
    ("participant", "list"): _cmd_participant_list,
    ("participant", "create"): _cmd_participant_create,
    ("participant", "update"): _cmd_participant_update,
    ("participant", "deactivate"): _cmd_participant_deactivate,
    ("participant", "consent"): _cmd_participant_consent,
    ("participant", "enrollment"): _cmd_participant_enrollment,
    ("participant", "voiceprint"): _cmd_participant_voiceprint,
    ("participant", "cleanup"): _cmd_participant_cleanup,
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
    elif args.command == "participant":
        sub_name = getattr(args, "participant_command", None)
    elif args.command == "asr":
        sub_name = getattr(args, "asr_command", None)

    if (
        args.command in {"db", "config", "registry", "audio", "participant", "asr"}
        and not sub_name
    ):
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
