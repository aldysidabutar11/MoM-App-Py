"""API routes.

Authentication policy, stated explicitly (see also
:data:`mom_igd.config.PUBLIC_ENDPOINTS`):

* ``GET /health`` and ``GET /version`` are **unauthenticated**. They must answer
  before the desktop shell has a token so the shell can distinguish "backend
  down" from "unauthorised", and they disclose only the application name,
  version, phase and coarse booleans -- no path, no hardware inventory, no user
  data. Both are still reachable only from loopback.
* Every other endpoint requires the session token. ``/doctor`` and
  ``/internal/ready`` expose filesystem paths and hardware details, so they are
  not public.
"""

from __future__ import annotations

import platform
import time
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status

from mom_igd import offline_policy
from mom_igd.api.deps import ConfigDep, PathsDep, require_session_token
from mom_igd.diagnostics.doctor import run_doctor
from mom_igd.registry import RegistryError, load_registry, registry_status
from mom_igd.version import APP_NAME, APP_VERSION, CURRENT_PHASE, version_info

__all__ = ["public_router", "protected_router"]

public_router = APIRouter(tags=["public"])
protected_router = APIRouter(
    tags=["protected"], dependencies=[Depends(require_session_token)]
)


def _database_state(config: ConfigDep, paths: PathsDep) -> dict[str, Any]:
    """Coarse database readiness. Never raises; used by health and readiness."""
    from mom_igd.db import current_schema_version, discover_migrations, head_version
    from mom_igd.db.connection import connect, read_pragmas

    db_path = paths.database_path(config.database.filename)
    state: dict[str, Any] = {
        "exists": db_path.exists(),
        "schema_version": None,
        "head_version": None,
        "wal": None,
        "foreign_keys": None,
        "ready": False,
        "error": None,
    }
    try:
        state["head_version"] = head_version(discover_migrations())
    except Exception as exc:  # noqa: BLE001 - health must never raise
        state["error"] = f"migration set invalid: {exc}"
        return state
    if not db_path.exists():
        return state
    try:
        conn = connect(db_path, busy_timeout_ms=config.database.busy_timeout_ms)
    except Exception as exc:  # noqa: BLE001
        state["error"] = str(exc)
        return state
    try:
        pragmas = read_pragmas(conn)
        state["wal"] = pragmas["journal_mode"] == "wal"
        state["foreign_keys"] = pragmas["foreign_keys"] == 1
        state["schema_version"] = current_schema_version(conn)
        state["ready"] = bool(
            state["wal"] and state["foreign_keys"] and state["schema_version"] == state["head_version"]
        )
    except Exception as exc:  # noqa: BLE001
        state["error"] = str(exc)
    finally:
        conn.close()
    return state


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------


@public_router.get("/health", summary="Liveness and coarse readiness (unauthenticated)")
def health(request: Request, config: ConfigDep, paths: PathsDep) -> dict[str, Any]:
    """Report that the backend is alive, plus coarse booleans for the shell.

    Deliberately returns no filesystem path, no hardware inventory and no user
    data, because this endpoint is unauthenticated.
    """
    database = _database_state(config, paths)
    started_at = getattr(request.app.state, "started_at", None)
    return {
        "status": "ok",
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "phase": CURRENT_PHASE,
        "offline": config.offline,
        "runtime_mode": config.runtime_mode,
        "uptime_seconds": round(time.monotonic() - started_at, 3) if started_at else None,
        "database": {
            "exists": database["exists"],
            "ready": database["ready"],
            "schema_version": database["schema_version"],
            "head_version": database["head_version"],
            "wal": database["wal"],
            "foreign_keys": database["foreign_keys"],
        },
        "data_dir": {
            # Booleans only: the path itself is disclosed by /doctor, which
            # requires the session token.
            "configured": True,
            "exists": paths.exists(),
            "writable": paths.is_writable(),
            "complete": not paths.missing_dirs(),
        },
    }


@public_router.get("/version", summary="Application and schema versions (unauthenticated)")
def version() -> dict[str, Any]:
    """Return application identity and schema versions."""
    return {
        **version_info(),
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()}",
    }


# ---------------------------------------------------------------------------
# Protected endpoints
# ---------------------------------------------------------------------------


@protected_router.get("/doctor", summary="Full environment diagnostics (token required)")
def doctor(config: ConfigDep, strict: bool = False) -> dict[str, Any]:
    """Run the diagnostics and return the machine-readable report.

    Requires the session token because it discloses absolute filesystem paths,
    hardware details and the running-process inventory.
    """
    report = run_doctor(config=config, ensure_dirs=False)
    payload = report.to_dict()
    payload["exit_code"] = report.exit_code(strict=strict)
    return payload


@protected_router.get(
    "/internal/ready", summary="Readiness for work (token required)"
)
def ready(config: ConfigDep, paths: PathsDep, response: Response) -> dict[str, Any]:
    """Readiness, as distinct from liveness.

    ``/health`` says the process is up. This says the application could actually
    do work: schema at head, runtime tree writable, model registry parseable, and
    no cloud SDK in the environment. Returns HTTP 503 when not ready, so a
    supervisor or the shell can act on the status code alone.
    """
    database = _database_state(config, paths)
    blockers: list[str] = []

    if not database["ready"]:
        cause = database["error"] or (
            f"schema {database['schema_version']} of {database['head_version']}"
        )
        blockers.append(f"database not ready ({cause})")
    if not paths.is_writable():
        blockers.append(f"runtime data root not writable: {paths.root}")

    registry_info: dict[str, Any] | None = None
    try:
        registry = load_registry(config.model_registry_path)
        registry_info = registry_status(registry, paths.models_dir)
    except RegistryError as exc:
        blockers.append(f"model registry invalid: {exc}")

    audit = offline_policy.audit_installed_distributions()
    if audit["cloud"]:
        blockers.append(f"cloud SDK installed: {audit['cloud']}")

    payload = {
        "ready": not blockers,
        "phase": CURRENT_PHASE,
        "blockers": blockers,
        "database": database,
        "data_dir": paths.describe(),
        "model_registry": registry_info,
        "offline": {
            "runtime_mode": config.runtime_mode,
            "cloud_sdks": audit["cloud"],
            "deferred_dependencies": audit["deferred"],
            "firewall_enforcement": "deferred to Phase 11",
        },
        "capabilities": {
            # Honest capability advertisement: Phase 1 implements none of these.
            "audio_capture": False,
            "asr": False,
            "diarization": False,
            "voice_id": False,
            "mom_extraction": False,
            "export": False,
        },
    }
    if blockers:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return payload
