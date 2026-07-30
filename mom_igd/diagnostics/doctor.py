"""Environment diagnostics with a strict PASS / WARN / FAIL contract.

Classification rules -- these are the whole point of the command:

* ``PASS``  -- required for the current phase, and satisfied.
* ``WARN``  -- optional, informational, or required only in a *future* phase.
* ``FAIL``  -- required for the current phase, and not satisfied.

Consequences of that contract in Phase 2: a missing AI library, a missing model and
a missing OpenVINO installation are still ``WARN`` -- none of them can fail the
build, because Phase 2 implements none of those features. What *changed* in Phase 2
is that the audio backend and a usable capture device are now required by the
current phase, so their absence is a ``FAIL``. A non-loopback API host, a data root
inside the repository, a broken database or an installed cloud SDK remain ``FAIL``,
because they violate an invariant that already applies.

A missing *USB conference microphone* is the one check that is deliberately
graded twice: ``WARN`` in the default run, because the built-in array is fine for
development, and ``FAIL`` under ``--production``, because it is not fine for
recording a room full of people. See :mod:`mom_igd.diagnostics.audio_checks`.

Exit codes (deterministic, tested):

* ``0`` -- no ``FAIL``. Warnings are expected: they name future-phase work.
* ``1`` -- at least one ``FAIL``.
* ``2`` -- ``--strict`` was requested and there is at least one ``WARN`` (no ``FAIL``).

The doctor never mutates the system: it does not create the runtime tree unless
explicitly asked, does not install anything, does not download anything, does not
stop Docker/WSL/browser processes, and makes no network request.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Final

from mom_igd import offline_policy
from mom_igd.config import AppConfig, ConfigError, load_config
from mom_igd.diagnostics.model import (
    CheckResult,
    DoctorReport,
    Status,
    format_report,
    nearest_existing,
    utc_now_iso,
)
from mom_igd.paths import RuntimePaths
from mom_igd.registry import RegistryError, load_registry, registry_status
from mom_igd.version import APP_NAME, APP_VERSION, CURRENT_PHASE, version_info

__all__ = ["CheckResult", "DoctorReport", "Status", "format_report", "run_doctor"]

_REQUIRED_PYTHON: Final[tuple[int, int]] = (3, 12)
_STORE_SHIM_MARKERS: Final[tuple[str, ...]] = (
    "windowsapps",
    "microsoft\\windowsapps",
    "packages\\pythonsoftwarefoundation.python",
)

# Dependencies that belong to a later phase. Missing => WARN, never FAIL.
_FUTURE_OPTIONAL_MODULES: Final[tuple[tuple[str, str, str], ...]] = (
    # `sounddevice` moved out of this list in Phase 2: it is now a real runtime
    # dependency with its own check (see mom_igd/diagnostics/audio_checks.py).
    # `soundfile` is not listed at all -- Phase 2 writes WAV with the standard
    # library, so it is not a future dependency, it is simply not needed.
    ("faster_whisper", "ASR (candidate, pending Phase 4A benchmark)", "4"),
    ("ctranslate2", "ASR inference backend (candidate)", "4"),
    ("openvino", "Intel acceleration (benchmark candidate only)", "4A"),
    ("onnxruntime", "ONNX inference (candidate)", "4A"),
    ("pyannote.audio", "diarization (candidate)", "5"),
    ("torch", "diarization/embedding backend (candidate)", "5"),
    ("llama_cpp", "local LLM runtime (candidate)", "8"),
)

_DOCKER_WSL_PROCESS_MARKERS: Final[tuple[str, ...]] = (
    "vmmem",
    "vmmemwsl",
    "docker",
    "com.docker",
    "wsl",
    "wslservice",
    "wslhost",
    "wslrelay",
)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _looks_like_store_shim(executable: str) -> bool:
    lowered = executable.replace("/", "\\").lower()
    return any(marker in lowered for marker in _STORE_SHIM_MARKERS)


def _check_application() -> CheckResult:
    return CheckResult(
        key="application",
        title="Application identity",
        status=Status.PASS,
        detail=f"{APP_NAME} {APP_VERSION} (roadmap phase {CURRENT_PHASE})",
        required_in_phase="1",
        data=version_info(),
    )


def _check_python() -> CheckResult:
    actual = sys.version_info[:2]
    detail = (
        f"{platform.python_version()} at {sys.executable} "
        f"({platform.architecture()[0]})"
    )
    if actual != _REQUIRED_PYTHON:
        return CheckResult(
            key="python_version",
            title="Python interpreter version",
            status=Status.FAIL,
            detail=(
                f"{detail} -- this build requires Python "
                f"{_REQUIRED_PYTHON[0]}.{_REQUIRED_PYTHON[1]}.x. Python 3.14 is "
                "too new for the AI wheels required from Phase 4 onwards, and "
                "Python 3.11 is only available here as a Microsoft Store shim."
            ),
            required_in_phase="1",
            data={"version": platform.python_version(), "executable": sys.executable},
        )
    return CheckResult(
        key="python_version",
        title="Python interpreter version",
        status=Status.PASS,
        detail=detail,
        required_in_phase="1",
        data={
            "version": platform.python_version(),
            "executable": sys.executable,
            "base_prefix": sys.base_prefix,
            "in_virtualenv": sys.prefix != sys.base_prefix,
        },
    )


def _check_store_shim() -> CheckResult:
    executable = sys.executable or ""
    is_shim = _looks_like_store_shim(executable)
    base_is_shim = _looks_like_store_shim(sys.base_prefix or "")
    if is_shim or base_is_shim:
        return CheckResult(
            key="python_not_store_shim",
            title="Interpreter is not a Microsoft Store shim",
            status=Status.FAIL,
            detail=(
                f"Interpreter resolves through WindowsApps ({executable}, base "
                f"{sys.base_prefix}). The Store distribution applies filesystem "
                "redirection and app-container sandboxing that breaks PyInstaller "
                "packaging and native library loading. Use the official "
                "python.org per-user installation."
            ),
            required_in_phase="1",
            data={"executable": executable, "base_prefix": sys.base_prefix},
        )
    return CheckResult(
        key="python_not_store_shim",
        title="Interpreter is not a Microsoft Store shim",
        status=Status.PASS,
        detail=f"Official distribution (base prefix {sys.base_prefix})",
        required_in_phase="1",
        data={"executable": executable, "base_prefix": sys.base_prefix},
    )


def _check_operating_system() -> CheckResult:
    system = platform.system()
    if system != "Windows":
        return CheckResult(
            key="operating_system",
            title="Operating system",
            status=Status.WARN,
            detail=(
                f"Running on {system} {platform.release()}. The production target "
                "is Windows 11; WASAPI capture (Phase 2) and WebView2 are "
                "Windows-only. Non-Windows is acceptable for running the test "
                "suite only."
            ),
            required_in_phase="2",
            data={"system": system, "release": platform.release()},
        )
    return CheckResult(
        key="operating_system",
        title="Operating system",
        status=Status.PASS,
        detail=f"{platform.system()} {platform.release()} build {platform.version()}",
        required_in_phase="1",
        data={
            "system": system,
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
    )


def _check_cpu() -> CheckResult:
    logical = os.cpu_count() or 0
    physical: int | None = None
    try:
        import psutil

        physical = psutil.cpu_count(logical=False)
    except Exception:  # pragma: no cover - psutil is a required dependency
        pass
    processor = platform.processor() or "unknown"
    return CheckResult(
        key="cpu",
        title="CPU",
        status=Status.PASS,
        detail=(
            f"{processor}; {logical} logical processor(s)"
            + (f", {physical} physical core(s)" if physical else "")
        ),
        required_in_phase="1",
        data={"processor": processor, "logical": logical, "physical": physical},
    )


def _check_ram(config: AppConfig) -> CheckResult:
    try:
        import psutil
    except Exception as exc:  # pragma: no cover
        return CheckResult(
            key="ram",
            title="System memory",
            status=Status.FAIL,
            detail=f"psutil is required for memory diagnostics but is unavailable: {exc}",
            required_in_phase="1",
        )
    memory = psutil.virtual_memory()
    total_mb = memory.total // (1024 * 1024)
    available_mb = memory.available // (1024 * 1024)
    threshold = config.resources.min_free_ram_mb
    data = {
        "total_mb": total_mb,
        "available_mb": available_mb,
        "percent_used": memory.percent,
        "warn_below_mb": threshold,
    }
    if available_mb < threshold:
        return CheckResult(
            key="ram",
            title="System memory",
            status=Status.WARN,
            detail=(
                f"{available_mb} MB available of {total_mb} MB total, below the "
                f"{threshold} MB warning threshold. Capture does not need it, but "
                "a heavy stage from Phase 4 onwards will page. Close browsers and "
                "stop Docker Desktop before processing."
            ),
            required_in_phase="4",
            data=data,
        )
    return CheckResult(
        key="ram",
        title="System memory",
        status=Status.PASS,
        detail=f"{available_mb} MB available of {total_mb} MB total",
        required_in_phase="1",
        data=data,
    )


# `nearest_existing` and `utc_now_iso` live in .model so the standard-library-only
# bootstrap diagnostics can share them.
_nearest_existing = nearest_existing
_utc_now_iso = utc_now_iso


def _check_disk(config: AppConfig) -> CheckResult:
    anchor = _nearest_existing(config.data_root)
    try:
        usage = shutil.disk_usage(anchor)
    except OSError as exc:
        return CheckResult(
            key="disk",
            title="Free disk space on the runtime data volume",
            status=Status.FAIL,
            detail=f"Cannot read disk usage for {anchor}: {exc}",
            required_in_phase="1",
            data={"probe_path": str(anchor)},
        )
    free_gb = usage.free / (1024**3)
    threshold = config.resources.min_free_disk_gb
    data = {
        "probe_path": str(anchor),
        "total_gb": round(usage.total / (1024**3), 1),
        "free_gb": round(free_gb, 1),
        "warn_below_gb": threshold,
    }
    if free_gb < threshold:
        return CheckResult(
            key="disk",
            title="Free disk space on the runtime data volume",
            status=Status.WARN,
            detail=(
                f"{free_gb:.1f} GB free at {anchor}, below the {threshold} GB "
                "warning threshold. A two-hour meeting needs roughly 1.3 GB "
                "including intermediate artefacts."
            ),
            required_in_phase="2",
            data=data,
        )
    return CheckResult(
        key="disk",
        title="Free disk space on the runtime data volume",
        status=Status.PASS,
        detail=f"{free_gb:.1f} GB free at {anchor}",
        required_in_phase="1",
        data=data,
    )


def _check_data_path(paths: RuntimePaths) -> CheckResult:
    described = paths.describe()
    missing = described["missing"]
    detail = f"{paths.root}"
    if not paths.exists():
        detail += " (not created yet; run `python -m mom_igd db init`)"
    elif missing:
        detail += f" (exists; {len(missing)} subdirectory/ies not created yet)"
    else:
        detail += " (all runtime subdirectories present)"
    return CheckResult(
        key="data_path",
        title="Runtime data directory",
        status=Status.PASS,
        detail=detail,
        required_in_phase="1",
        data=described,
    )


def _check_data_path_writable(paths: RuntimePaths) -> CheckResult:
    writable = paths.is_writable()
    probe = _nearest_existing(paths.root)
    if not writable:
        return CheckResult(
            key="data_path_writable",
            title="Runtime data directory is writable",
            status=Status.FAIL,
            detail=(
                f"Cannot write under {probe}. The application cannot store "
                "recordings, the database or exports. Check the drive letter, "
                "permissions and available space."
            ),
            required_in_phase="1",
            data={"probe_path": str(probe), "writable": False},
        )
    return CheckResult(
        key="data_path_writable",
        title="Runtime data directory is writable",
        status=Status.PASS,
        detail=f"Write probe succeeded at {probe}",
        required_in_phase="1",
        data={"probe_path": str(probe), "writable": True},
    )


def _check_database(config: AppConfig, paths: RuntimePaths) -> CheckResult:
    from mom_igd.db import (
        MigrationError,
        current_schema_version,
        discover_migrations,
        head_version,
        migration_status,
    )
    from mom_igd.db.connection import PragmaVerificationError, connect

    db_path = paths.database_path(config.database.filename)
    try:
        expected_head = head_version(discover_migrations())
    except MigrationError as exc:
        return CheckResult(
            key="database",
            title="Database and migrations",
            status=Status.FAIL,
            detail=f"Migration set is invalid: {exc}",
            required_in_phase="1",
            data={"database_path": str(db_path)},
        )

    if not db_path.exists():
        return CheckResult(
            key="database",
            title="Database and migrations",
            status=Status.WARN,
            detail=(
                f"Database not created yet at {db_path}. Run "
                "`python -m mom_igd db init`. "
                f"{expected_head} migration(s) are available."
            ),
            required_in_phase="1",
            data={
                "database_path": str(db_path),
                "exists": False,
                "head_version": expected_head,
            },
        )

    try:
        conn = connect(db_path, busy_timeout_ms=config.database.busy_timeout_ms)
    except (PragmaVerificationError, sqlite3.Error, OSError) as exc:
        return CheckResult(
            key="database",
            title="Database and migrations",
            status=Status.FAIL,
            detail=f"Cannot open {db_path}: {exc}",
            required_in_phase="1",
            data={"database_path": str(db_path), "exists": True},
        )
    try:
        from mom_igd.db.connection import read_pragmas

        pragmas = read_pragmas(conn)
        version = current_schema_version(conn)
        status_info = migration_status(conn)
        wal_ok = pragmas["journal_mode"] == "wal"
        fk_ok = pragmas["foreign_keys"] == 1
        data = {
            "database_path": str(db_path),
            "exists": True,
            "schema_version": version,
            "head_version": expected_head,
            "pragmas": pragmas,
            "pending": status_info["pending"],
        }
        if not wal_ok or not fk_ok:
            return CheckResult(
                key="database",
                title="Database and migrations",
                status=Status.FAIL,
                detail=(
                    f"Required pragmas not confirmed (journal_mode="
                    f"{pragmas['journal_mode']}, foreign_keys={pragmas['foreign_keys']})."
                ),
                required_in_phase="1",
                data=data,
            )
        if version != expected_head:
            return CheckResult(
                key="database",
                title="Database and migrations",
                status=Status.FAIL,
                detail=(
                    f"Schema is at version {version} but this build expects "
                    f"{expected_head}. Pending: {status_info['pending']}. Run "
                    "`python -m mom_igd db init`."
                ),
                required_in_phase="1",
                data=data,
            )
        return CheckResult(
            key="database",
            title="Database and migrations",
            status=Status.PASS,
            detail=(
                f"Schema version {version} (head), WAL enabled, foreign keys "
                f"enforced, busy_timeout {pragmas['busy_timeout']} ms, "
                f"SQLite {pragmas['sqlite_version']}"
            ),
            required_in_phase="1",
            data=data,
        )
    finally:
        conn.close()


def _check_api_configuration(config: AppConfig) -> CheckResult:
    problems: list[str] = []
    if not offline_policy.is_loopback_host(config.api.host):
        problems.append(f"api.host={config.api.host!r} is not loopback")
    if config.resources.max_heavy_workers > 1:
        problems.append(
            f"resources.max_heavy_workers={config.resources.max_heavy_workers} exceeds 1"
        )
    data = {
        "host": config.api.host,
        "port": config.api.port,
        "port_strategy": config.api.port_strategy,
        "docs_enabled": config.api.docs_enabled,
        "max_heavy_workers": config.resources.max_heavy_workers,
    }
    if problems:
        return CheckResult(
            key="api_loopback",
            title="Local API is loopback-only",
            status=Status.FAIL,
            detail="; ".join(problems),
            required_in_phase="1",
            data=data,
        )
    port_text = (
        "ephemeral port assigned by the OS"
        if config.api.port_strategy == "ephemeral"
        else f"fixed port {config.api.port}"
    )
    return CheckResult(
        key="api_loopback",
        title="Local API is loopback-only",
        status=Status.PASS,
        detail=(
            f"Binds {config.api.host} with {port_text}; at most "
            f"{config.resources.max_heavy_workers} heavy worker"
        ),
        required_in_phase="1",
        data=data,
    )


def _check_docker_wsl_presence() -> CheckResult:
    found = {
        name: shutil.which(name)
        for name in ("docker", "docker-compose", "wsl")
        if shutil.which(name)
    }
    if not found:
        return CheckResult(
            key="docker_wsl_presence",
            title="Docker / WSL2 presence (informational)",
            status=Status.PASS,
            detail="Neither Docker nor WSL is on PATH. Neither is a production dependency.",
            required_in_phase=None,
            data={"found": {}},
        )
    return CheckResult(
        key="docker_wsl_presence",
        title="Docker / WSL2 presence (informational)",
        status=Status.WARN,
        detail=(
            f"Found on PATH: {', '.join(sorted(found))}. Informational only -- "
            "Docker Desktop and WSL2 are NOT production dependencies (ADR-0001). "
            "This application does not start, stop or configure them, and will "
            "never do so automatically."
        ),
        required_in_phase=None,
        data={"found": found},
    )


def _check_docker_wsl_memory() -> CheckResult:
    try:
        import psutil
    except Exception as exc:  # pragma: no cover
        return CheckResult(
            key="docker_wsl_memory",
            title="Docker / WSL2 memory usage (informational)",
            status=Status.WARN,
            detail=f"psutil unavailable, cannot inspect processes: {exc}",
            required_in_phase=None,
        )

    holders: list[dict[str, Any]] = []
    total_mb = 0
    for process in psutil.process_iter(["name", "memory_info"]):
        try:
            name = (process.info.get("name") or "").lower()
            if not name:
                continue
            stem = name[:-4] if name.endswith(".exe") else name
            if not any(marker in stem for marker in _DOCKER_WSL_PROCESS_MARKERS):
                continue
            info = process.info.get("memory_info")
            rss_mb = int(info.rss // (1024 * 1024)) if info else 0
            total_mb += rss_mb
            holders.append({"name": name, "rss_mb": rss_mb})
        except (psutil.NoSuchProcess, psutil.AccessDenied):  # pragma: no cover
            continue

    if not holders:
        return CheckResult(
            key="docker_wsl_memory",
            title="Docker / WSL2 memory usage (informational)",
            status=Status.PASS,
            detail="No Docker or WSL process is currently resident.",
            required_in_phase=None,
            data={"processes": [], "total_rss_mb": 0},
        )

    holders.sort(key=lambda item: item["rss_mb"], reverse=True)
    top = ", ".join(f"{item['name']} {item['rss_mb']} MB" for item in holders[:4])
    return CheckResult(
        key="docker_wsl_memory",
        title="Docker / WSL2 memory usage (informational)",
        status=Status.WARN,
        detail=(
            f"{len(holders)} Docker/WSL process(es) resident, {total_mb} MB total "
            f"({top}). Informational only: this application never stops user "
            "processes. Note that a WSL2 VM reserves memory beyond its reported "
            "working set, which matters from Phase 4 onwards on a 16 GB machine."
        ),
        required_in_phase=None,
        data={"processes": holders, "total_rss_mb": total_mb},
    )


def _check_optional_dependencies() -> CheckResult:
    present: list[str] = []
    missing: list[dict[str, str]] = []
    for module, purpose, phase in _FUTURE_OPTIONAL_MODULES:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):  # pragma: no cover - odd namespace pkgs
            found = False
        if found:
            present.append(module)
        else:
            missing.append({"module": module, "purpose": purpose, "phase": phase})

    data = {"present": present, "missing": missing}
    if not missing:
        return CheckResult(
            key="optional_dependencies",
            title="Future optional dependencies",
            status=Status.PASS,
            detail="All future optional dependencies are present.",
            required_in_phase=None,
            data=data,
        )
    listed = ", ".join(f"{item['module']} (Phase {item['phase']})" for item in missing)
    return CheckResult(
        key="optional_dependencies",
        title="Future optional dependencies",
        status=Status.WARN,
        detail=(
            f"{len(missing)} of {len(_FUTURE_OPTIONAL_MODULES)} future dependencies "
            f"are not installed: {listed}. This is the expected Phase "
            f"{CURRENT_PHASE} state -- each is installed by the phase that needs "
            "it, and the AI provider choice is still pending the Phase 4A benchmark."
        ),
        required_in_phase=None,
        data=data,
    )


def _check_model_registry(config: AppConfig, paths: RuntimePaths) -> CheckResult:
    try:
        registry = load_registry(config.model_registry_path)
    except RegistryError as exc:
        return CheckResult(
            key="model_registry",
            title="Model registry",
            status=Status.FAIL,
            detail=str(exc),
            required_in_phase="1",
            data={"path": str(config.model_registry_path)},
        )
    status_info = registry_status(registry, paths.models_dir)
    data = {"path": str(config.model_registry_path), **status_info}
    if registry.is_empty:
        return CheckResult(
            key="model_registry",
            title="Model registry",
            status=Status.WARN,
            detail=(
                f"Registry at {config.model_registry_path} is valid (schema v"
                f"{registry.registry_schema_version}) and declares 0 models. That "
                f"is the correct Phase {CURRENT_PHASE} state: no provider has been "
                "selected and no model has been downloaded."
            ),
            required_in_phase=None,
            data=data,
        )
    if status_info["declared_but_missing_on_disk"]:
        return CheckResult(
            key="model_registry",
            title="Model registry",
            status=Status.FAIL,
            detail=(
                "Registry declares provisioned models whose files are missing: "
                f"{status_info['declared_but_missing_on_disk']}"
            ),
            required_in_phase="1",
            data=data,
        )
    return CheckResult(
        key="model_registry",
        title="Model registry",
        status=Status.PASS,
        detail=(
            f"{status_info['total']} model(s) declared, "
            f"{status_info['provisioned']} provisioned, "
            f"{status_info['offline_ready']} offline-ready"
        ),
        required_in_phase="1",
        data=data,
    )


def _check_asr_models(config: AppConfig, paths: RuntimePaths) -> CheckResult:
    """Whether transcription can run, answered without loading anything.

    Reads the installed-model registry, which records only models that passed a
    load-and-decode probe -- so this cannot report a byte-perfect model that cannot
    actually decode as ready (ADR-0015).

    **Always a WARN when absent, never a FAIL.** Provisioning is a deliberate one-off
    command that needs network access, and an operator on a fresh machine has not done
    anything wrong. It becomes a `FAIL` only when accuracy acceptance is granted and
    `CURRENT_PHASE` advances to 4.
    """
    from mom_igd.asr.installed import load_index

    index = load_index(paths.models_dir)
    ready = {entry.role: entry for entry in index.ready(paths.models_dir)}
    data = {
        "models_dir": str(paths.models_dir),
        "index_readable": index.readable,
        "problem": index.problem,
        "pass1_ready": "pass1" in ready,
        "pass2_ready": "pass2" in ready,
        "ready_models": sorted(
            f"{entry.role}:{entry.model_name}@{entry.revision[:12]}"
            for entry in ready.values()
        ),
    }
    if not index.readable:
        return CheckResult(
            key="asr_models",
            title="Transcription models",
            status=Status.WARN,
            detail=(
                f"The installed-model registry could not be read: {index.problem}. "
                "Nothing is treated as ready, which is the intended fail-closed "
                "behaviour. Re-run `asr provision all` to rebuild it."
            ),
            required_in_phase="4",
            data=data,
        )
    if "pass1" not in ready:
        return CheckResult(
            key="asr_models",
            title="Transcription models",
            status=Status.WARN,
            detail=(
                "No transcription model is provisioned, so `asr transcribe` will answer "
                "MODEL_UNAVAILABLE. Provision once, with network access: "
                "`python -m mom_igd asr provision all`. Nothing else in this "
                "application ever downloads a model."
            ),
            required_in_phase="4",
            data=data,
        )
    if "pass2" not in ready:
        return CheckResult(
            key="asr_models",
            title="Transcription models",
            status=Status.WARN,
            detail=(
                f"Pass 1 is ready ({ready['pass1'].model_name}) and pass 2 is not. "
                "Transcription will run and record PASS2_MODEL_UNAVAILABLE, keeping the "
                "first-pass result. Provision it with `asr provision asr-pass2`."
            ),
            required_in_phase="4",
            data=data,
        )
    return CheckResult(
        key="asr_models",
        title="Transcription models",
        status=Status.PASS,
        detail=(
            f"pass 1 {ready['pass1'].model_name}@{ready['pass1'].revision[:12]} and "
            f"pass 2 {ready['pass2'].model_name}@{ready['pass2'].revision[:12]} are "
            "verified and probe-passed. Accuracy is not measured by this check and is "
            "not claimed anywhere."
        ),
        required_in_phase="4",
        data=data,
    )


def _check_offline_configuration(config: AppConfig) -> CheckResult:
    audit = offline_policy.audit_installed_distributions()
    endpoints = dict(config.providers.endpoints)
    data = {
        "runtime_mode": config.runtime_mode,
        "offline": config.offline,
        "provider_endpoints": endpoints,
        "dependency_audit": audit,
        "offline_env_flags": sorted(offline_policy.offline_env_flags()),
        "firewall_enforcement": "deferred to Phase 11",
    }
    if audit["cloud"]:
        return CheckResult(
            key="offline_policy",
            title="Offline policy",
            status=Status.FAIL,
            detail=(
                "Cloud SDK distribution(s) installed in this environment: "
                f"{audit['cloud']}. The application has no cloud fallback and no "
                "cloud dependency is permitted. Remove them from the virtual "
                "environment."
            ),
            required_in_phase="1",
            data=data,
        )
    if audit["deferred"]:
        return CheckResult(
            key="offline_policy",
            title="Offline policy",
            status=Status.WARN,
            detail=(
                f"Runtime mode {config.runtime_mode}, {len(endpoints)} provider "
                f"endpoint(s) configured, no cloud SDK present. Heavy dependencies "
                f"already installed ahead of their phase: {audit['deferred']}."
            ),
            required_in_phase="1",
            data=data,
        )
    return CheckResult(
        key="offline_policy",
        title="Offline policy",
        status=Status.PASS,
        detail=(
            f"Runtime mode {config.runtime_mode}; no cloud SDK installed; "
            f"{len(endpoints)} provider endpoint(s), all local or loopback. "
            "OS firewall enforcement is deferred to Phase 11."
        ),
        required_in_phase="1",
        data=data,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_doctor(
    config: AppConfig | None = None,
    *,
    data_root: str | os.PathLike[str] | None = None,
    ensure_dirs: bool = False,
    production: bool = False,
) -> DoctorReport:
    """Run every diagnostic and return the report.

    Args:
        config: Pre-loaded configuration; loaded from disk when omitted.
        data_root: Data root override, used only when ``config`` is omitted.
        ensure_dirs: When ``True``, create the runtime tree before checking. The
            default is ``False`` so that diagnosing a machine never has a side
            effect on the filesystem.
        production: Apply the production gate. A built-in microphone, an
            unrecovered recording and a missing calibration are warnings in the
            default run and failures here, because a laptop array cannot record a
            multi-participant meeting usefully. No audio stream is opened either way.
    """
    results: list[CheckResult] = [_check_application(), _check_python(), _check_store_shim()]

    try:
        resolved = config if config is not None else load_config(data_root=data_root)
    except ConfigError as exc:
        results.append(
            CheckResult(
                key="configuration",
                title="Configuration",
                status=Status.FAIL,
                detail=str(exc),
                required_in_phase="1",
            )
        )
        return DoctorReport(
            generated_at=_utc_now_iso(),
            app=version_info(),
            results=tuple(results),
        )

    results.append(
        CheckResult(
            key="configuration",
            title="Configuration",
            status=Status.PASS,
            detail=(
                f"Schema v{resolved.config_schema_version}, mode "
                f"{resolved.runtime_mode}, log level {resolved.log_level}"
            ),
            required_in_phase="1",
            data=resolved.summary(),
        )
    )

    paths = resolved.runtime_paths()
    if ensure_dirs:
        paths.ensure()

    results += [
        _check_operating_system(),
        _check_cpu(),
        _check_ram(resolved),
        _check_disk(resolved),
        _check_data_path(paths),
        _check_data_path_writable(paths),
        _check_database(resolved, paths),
        _check_api_configuration(resolved),
        _check_offline_configuration(resolved),
        _check_model_registry(resolved, paths),
        _check_asr_models(resolved, paths),
        _check_optional_dependencies(),
    ]

    from mom_igd.diagnostics.audio_checks import audio_checks

    results += audio_checks(resolved, paths, production=production)

    # Phase 3. Imported here for the same reason as the audio checks: neither the
    # cipher nor the enrollment stack should load merely because `doctor` was asked
    # about the interpreter version.
    from mom_igd.diagnostics.enrollment_checks import enrollment_checks

    results += enrollment_checks(resolved, paths, production=production)
    results += [
        _check_docker_wsl_presence(),
        _check_docker_wsl_memory(),
    ]

    return DoctorReport(
        generated_at=_utc_now_iso(),
        app=version_info(),
        results=tuple(results),
    )

