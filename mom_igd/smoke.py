"""Headless backend smoke test.

Starts the *real* backend on an ephemeral loopback port and exercises the
contracts that must hold before anything else can be trusted:

1. the server starts and reports a bound port;
2. ``/health`` answers unauthenticated;
3. ``/version`` answers unauthenticated;
4. a protected endpoint refuses an anonymous request (401);
5. the same endpoint accepts the session token (200);
6. the token is refused when presented in a query string (400);
7. a request with a non-loopback ``Host`` header is refused (403);
8. the static UI is served and contains no remote asset;
9. no response body contains the session token;
10. shutdown is clean and the serving thread has exited.

Opens no GUI, needs no microphone, no model, no Docker and no network beyond
loopback. Uses ``urllib`` from the standard library so the runtime dependency set
stays free of an HTTP client.
"""

from __future__ import annotations

import json
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from mom_igd.config import AppConfig
from mom_igd.security import SESSION_TOKEN_HEADER, SessionToken

__all__ = ["SmokeStep", "run_smoke"]

_TIMEOUT_S: Final[float] = 20.0

# Anything that would pull a byte from outside this machine.
_REMOTE_ASSET_RE: Final[re.Pattern[str]] = re.compile(
    r"""(?:src|href)\s*=\s*["']\s*(?:https?:)?//|@import\s+url\(\s*["']?\s*(?:https?:)?//""",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SmokeStep:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def _request(
    url: str,
    *,
    token: str | None = None,
    host_header: str | None = None,
    accept: str = "application/json",
) -> tuple[int, str]:
    """Perform a loopback GET, returning ``(status, body)`` without raising."""
    headers: dict[str, str] = {"Accept": accept}
    if token is not None:
        headers[SESSION_TOKEN_HEADER] = token
    if host_header is not None:
        headers["Host"] = host_header
    request = urllib.request.Request(url=url, method="GET", headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310
            return int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def _temp_config(config: AppConfig, temp_root: Path) -> AppConfig:
    """Return ``config`` with its data root replaced, fully re-validated."""
    payload = config.model_dump(mode="python")
    payload["data_root"] = temp_root
    return AppConfig.model_validate(payload)


def run_smoke(config: AppConfig, *, use_temp_data_root: bool = True) -> dict[str, Any]:
    """Run the smoke test and return a JSON-serialisable result.

    Args:
        config: Validated configuration.
        use_temp_data_root: When ``True`` (the default) the test runs against a
            throwaway data root, so it never touches real meeting data.
    """
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if use_temp_data_root:
        temp_dir = tempfile.TemporaryDirectory(prefix="mom_igd_smoke_")
        config = _temp_config(config, Path(temp_dir.name) / "data")

    try:
        return _run(config)
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def _run(config: AppConfig) -> dict[str, Any]:
    # Imported here so `python -m mom_igd doctor` never pays for FastAPI/uvicorn.
    from mom_igd.api.app import create_app
    from mom_igd.api.server import BackgroundServer, ServerStartupError
    from mom_igd.db import initialize_database

    steps: list[SmokeStep] = []
    secret = SessionToken()

    paths = config.runtime_paths().ensure()
    try:
        db_info = initialize_database(
            paths.database_path(config.database.filename),
            busy_timeout_ms=config.database.busy_timeout_ms,
            app_version=config.app_version,
        )
        steps.append(
            SmokeStep(
                "database_init",
                db_info["status"]["up_to_date"] and db_info["pragmas"]["journal_mode"] == "wal",
                f"schema {db_info['status']['current_version']} of "
                f"{db_info['status']['head_version']}, "
                f"journal_mode={db_info['pragmas']['journal_mode']}, "
                f"foreign_keys={db_info['pragmas']['foreign_keys']}",
            )
        )
    except Exception as exc:  # noqa: BLE001 - reported as a failed step
        steps.append(SmokeStep("database_init", False, f"{type(exc).__name__}: {exc}"))
        return _summarise(steps)

    app = create_app(config, session_token=secret, paths=paths)
    server = BackgroundServer(
        app,
        host=config.api.host,
        port=0,  # always ephemeral: a smoke test must not fight for a port
        log_level="warning",
        startup_timeout_s=config.api.startup_timeout_s,
        shutdown_timeout_s=config.api.shutdown_timeout_s,
    )

    bodies: list[str] = []
    try:
        try:
            server.start()
        except ServerStartupError as exc:
            steps.append(SmokeStep("server_start", False, str(exc)))
            return _summarise(steps)

        base = server.base_url
        steps.append(
            SmokeStep(
                "server_start",
                server.started and server.port > 0,
                f"listening on {base}",
            )
        )

        # 2. /health, unauthenticated
        status, body = _request(f"{base}/health")
        bodies.append(body)
        health_ok = status == 200
        detail = f"HTTP {status}"
        if health_ok:
            try:
                payload = json.loads(body)
                health_ok = payload.get("status") == "ok"
                detail = (
                    f"HTTP 200, status={payload.get('status')}, "
                    f"offline={payload.get('offline')}, "
                    f"db_ready={payload.get('database', {}).get('ready')}"
                )
                # A public endpoint must not disclose filesystem paths.
                if str(paths.root) in body:
                    health_ok = False
                    detail += " -- LEAK: response contains the data root path"
            except json.JSONDecodeError as exc:
                health_ok = False
                detail = f"HTTP 200 but invalid JSON: {exc}"
        steps.append(SmokeStep("health_public", health_ok, detail))

        # 3. /version, unauthenticated
        status, body = _request(f"{base}/version")
        bodies.append(body)
        version_ok = status == 200
        detail = f"HTTP {status}"
        if version_ok:
            try:
                payload = json.loads(body)
                version_ok = bool(payload.get("app_version"))
                detail = (
                    f"HTTP 200, app_version={payload.get('app_version')}, "
                    f"phase={payload.get('phase')}"
                )
            except json.JSONDecodeError as exc:
                version_ok = False
                detail = f"HTTP 200 but invalid JSON: {exc}"
        steps.append(SmokeStep("version_public", version_ok, detail))

        # 4. protected endpoint, no token -> 401
        status, body = _request(f"{base}/doctor")
        bodies.append(body)
        steps.append(
            SmokeStep(
                "protected_requires_token",
                status == 401,
                f"HTTP {status} (expected 401 without a token)",
            )
        )

        # 5. protected endpoint, correct token -> 200
        status, body = _request(f"{base}/doctor", token=secret.value)
        bodies.append(body)
        doctor_ok = status == 200
        detail = f"HTTP {status}"
        if doctor_ok:
            try:
                payload = json.loads(body)
                counts = payload.get("counts", {})
                doctor_ok = payload.get("ok") is True
                detail = (
                    f"HTTP 200, {counts.get('PASS')} PASS / {counts.get('WARN')} WARN "
                    f"/ {counts.get('FAIL')} FAIL"
                )
                if not doctor_ok:
                    failing = [
                        r["key"] for r in payload.get("results", []) if r.get("status") == "FAIL"
                    ]
                    detail += f", failing: {failing}"
            except json.JSONDecodeError as exc:
                doctor_ok = False
                detail = f"HTTP 200 but invalid JSON: {exc}"
        steps.append(SmokeStep("protected_accepts_token", doctor_ok, detail))

        # 6. token in a query string -> 400, even though the value is correct
        status, body = _request(f"{base}/doctor?token={secret.value}")
        bodies.append(body)
        steps.append(
            SmokeStep(
                "token_in_query_rejected",
                status == 400,
                f"HTTP {status} (expected 400: a credential in a query string is "
                "refused even when correct)",
            )
        )

        # 7. non-loopback Host header -> 403 (DNS-rebinding defence)
        status, body = _request(f"{base}/health", host_header="evil.example.com")
        bodies.append(body)
        steps.append(
            SmokeStep(
                "non_loopback_host_rejected",
                status == 403,
                f"HTTP {status} (expected 403 for Host: evil.example.com)",
            )
        )

        # 8. static UI served, and free of remote assets
        status, body = _request(f"{base}/ui/", accept="text/html")
        bodies.append(body)
        ui_ok = status == 200 and "MoM-IGD" in body
        remote = _REMOTE_ASSET_RE.findall(body)
        if remote:
            ui_ok = False
        steps.append(
            SmokeStep(
                "static_ui_local_only",
                ui_ok,
                f"HTTP {status}, {len(body)} bytes, remote assets found: {len(remote)}",
            )
        )

        # 9. the token must not appear in any response body
        leaked = [index for index, text in enumerate(bodies) if secret.value in text]
        steps.append(
            SmokeStep(
                "token_never_in_response",
                not leaked,
                "no response body contains the session token"
                if not leaked
                else f"LEAK: token present in response(s) {leaked}",
            )
        )
    finally:
        server.stop()

    # 10. clean shutdown: *this* server's thread must actually have exited.
    #     Scoped to our own server on purpose -- a process-wide thread scan would
    #     make this step report someone else's leak as our failure.
    steps.append(
        SmokeStep(
            "clean_shutdown",
            server.stopped,
            f"stopped={server.stopped}, own thread running={server.is_running}",
        )
    )

    return _summarise(steps)


def _summarise(steps: list[SmokeStep]) -> dict[str, Any]:
    passed = sum(1 for step in steps if step.ok)
    return {
        "ok": passed == len(steps) and bool(steps),
        "passed": passed,
        "total": len(steps),
        "steps": [step.to_dict() for step in steps],
    }
