"""FastAPI application factory.

Loopback enforcement has two independent layers:

1. **Bind address.** The configuration refuses any host that is not loopback, so
   the socket is never reachable from the LAN or the internet.
2. **Host-header allowlist.** :class:`LoopbackHostMiddleware` rejects requests
   whose ``Host`` header is not a loopback name. Without this, a page on the
   internet could point a DNS name at ``127.0.0.1`` and have the user's browser
   talk to this backend (DNS rebinding). This is also what makes the statement
   "Swagger is not exposed outside loopback" enforceable rather than incidental.

The application never creates the runtime tree as a side effect: the caller
(``serve``, ``shell``, ``smoke``) calls ``RuntimePaths.ensure()`` explicitly.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from mom_igd import offline_policy
from mom_igd.api.routes import protected_router, public_router
from mom_igd.config import AppConfig
from mom_igd.logging_setup import get_logger
from mom_igd.paths import RuntimePaths
from mom_igd.security import SessionToken
from mom_igd.version import APP_NAME, APP_VERSION, CURRENT_PHASE

__all__ = ["LoopbackHostMiddleware", "WEB_DIR", "create_app"]

WEB_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "shell" / "web"

_LOG = get_logger("api")


class LoopbackHostMiddleware(BaseHTTPMiddleware):
    """Reject requests whose ``Host`` header is not a loopback name.

    Mitigates DNS rebinding against a loopback-bound service.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        header = request.headers.get("host", "")
        hostname = header
        if hostname.startswith("["):  # IPv6 literal, e.g. [::1]:8765
            hostname = hostname[1 : hostname.find("]")] if "]" in hostname else hostname[1:]
        elif ":" in hostname:
            hostname = hostname.rsplit(":", 1)[0]

        if not offline_policy.is_loopback_host(hostname):
            _LOG.warning(
                "Rejected request with non-loopback Host header: %s %s",
                request.method,
                request.url.path,
            )
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        "This API accepts loopback requests only. The Host header "
                        f"{header!r} is not a loopback name."
                    )
                },
            )
        return await call_next(request)


def create_app(
    config: AppConfig,
    *,
    session_token: SessionToken | None = None,
    paths: RuntimePaths | None = None,
) -> FastAPI:
    """Build the application.

    Args:
        config: Validated configuration. Its ``api.host`` has already been proven
            to be loopback by configuration validation.
        session_token: Token to enforce on protected endpoints; a fresh one is
            generated when omitted.
        paths: Runtime path service; derived from ``config`` when omitted.

    The returned app exposes ``app.state.config``, ``app.state.paths`` and
    ``app.state.session_token``. The token is held only in process memory.
    """
    token = session_token or SessionToken()
    runtime_paths = paths or config.runtime_paths()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.started_at = time.monotonic()
        _LOG.info(
            "%s %s starting (phase %s, mode %s, data root %s)",
            APP_NAME,
            APP_VERSION,
            CURRENT_PHASE,
            config.runtime_mode,
            runtime_paths.root,
        )
        # Never log the token, not even at DEBUG level.
        _LOG.debug("Session token generated (%d characters, value not logged)", len(token.value))
        try:
            yield
        finally:
            # A recording in progress must be finalised, not dropped: whatever
            # reached the writer is already on disk, and stopping cleanly turns the
            # open partial into a verified chunk instead of leaving it for recovery.
            service = getattr(app.state, "recording_service", None)
            if service is not None:
                try:
                    if service.status().get("recording_active"):
                        _LOG.warning(
                            "Shutting down with a recording in progress; finalising it."
                        )
                        service.stop()
                except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                    _LOG.error("Could not finalise the recording on shutdown: %s", exc)

            # A voice enrollment in progress is ABANDONED rather than finalised --
            # the opposite of a recording, and deliberately so. Enrollment audio is
            # held in memory only and there is no partial voiceprint worth keeping;
            # completing one without the operator present would store biometric data
            # nobody watched being created. What matters is that the stream closes,
            # the buffer is released and the shared capture lock is freed.
            context = getattr(app.state, "enrollment_context", None)
            if context is not None:
                for label, closer in (
                    ("capture controller", context.capture.shutdown),
                    ("enrollment session", context.enrollment.shutdown),
                ):
                    try:
                        closer()
                    except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                        _LOG.error("Could not close the %s cleanly: %s", label, exc)

            _LOG.info("%s shutting down cleanly", APP_NAME)

    app = FastAPI(
        title=f"{APP_NAME} local API",
        version=APP_VERSION,
        summary="Offline Minutes of Meeting backend. Loopback only; no cloud calls.",
        docs_url="/docs" if config.api.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if config.api.docs_enabled else None,
        lifespan=lifespan,
    )

    app.state.config = config
    app.state.paths = runtime_paths
    app.state.session_token = token

    app.add_middleware(LoopbackHostMiddleware)

    app.include_router(public_router)
    app.include_router(protected_router)

    # Phase 2 capture endpoints. Imported here rather than at module level so that
    # building the app does not pull in the audio stack until it is routed.
    from mom_igd.api.audio_routes import audio_router

    app.include_router(audio_router)

    # Phase 3 participants, consent, enrollment and voiceprints. Imported here for
    # the same reason: the enrollment stack pulls in the cipher and the device
    # backend, and neither should load merely because the app object was built.
    from mom_igd.api.enrollment_routes import enrollment_router

    app.include_router(enrollment_router)

    # Phase 4 transcription. Imported here for the same reason again, and with more
    # force: importing the ASR package must not pull in faster-whisper, CTranslate2 or
    # onnxruntime, and routing it must not either. The engine loads inside a spawned
    # worker, and only when a run actually starts.
    from mom_igd.api.asr_routes import asr_router

    app.include_router(asr_router)

    # Minutes. Same discipline once more: importing this must not pull in llama.cpp, and
    # routing it must not either. The 2.3 GB of weights load inside a spawned worker, and
    # only when a run actually starts.
    from mom_igd.api.mom_routes import mom_router

    app.include_router(mom_router)

    @app.get("/", include_in_schema=False)
    def _root() -> RedirectResponse:
        return RedirectResponse(url="/ui/")

    if WEB_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=WEB_DIR, html=True), name="ui")

    return app
