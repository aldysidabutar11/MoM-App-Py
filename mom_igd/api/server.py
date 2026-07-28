"""In-process uvicorn server wrapper.

Used by the desktop shell (which needs the backend inside the same process so
the session token never crosses a process boundary or a URL) and by the headless
smoke test (which must start and stop the real server without opening a GUI).

Startup and shutdown are both explicit and testable: :meth:`BackgroundServer.start`
does not return until the socket is listening, and :meth:`stop` does not return
until the serving thread has exited.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Final

import uvicorn
from fastapi import FastAPI

from mom_igd.logging_setup import get_logger

__all__ = ["BackgroundServer", "ServerStartupError"]

_LOG = get_logger("api.server")
_POLL_INTERVAL_S: Final[float] = 0.02
_THREAD_NAME: Final[str] = "mom-igd-api"


class ServerStartupError(RuntimeError):
    """Raised when the server did not begin listening within the timeout."""


class BackgroundServer:
    """Run a FastAPI application on a loopback socket in a background thread."""

    def __init__(
        self,
        app: FastAPI,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        log_level: str = "info",
        startup_timeout_s: float = 15.0,
        shutdown_timeout_s: float = 10.0,
    ) -> None:
        # Defence in depth: refuse a non-loopback bind even if a caller bypassed
        # configuration validation.
        from mom_igd import offline_policy

        self._host = offline_policy.validate_bind_host(host)
        self._requested_port = port
        self._startup_timeout_s = startup_timeout_s
        self._shutdown_timeout_s = shutdown_timeout_s
        self._config = uvicorn.Config(
            app=app,
            host=self._host,
            port=port,
            log_level=log_level.lower(),
            log_config=None,  # keep our own logging configuration
            access_log=False,  # avoid writing request URLs to the log at all
            lifespan="on",
        )
        self._server = uvicorn.Server(self._config)
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        # Own shutdown bookkeeping. uvicorn never resets Server.started back to
        # False, so that flag cannot be used to prove a clean shutdown.
        self._stop_requested = False
        self._thread_exited = False

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> BackgroundServer:
        """Start serving and block until the socket is listening."""
        if self._thread is not None:
            raise RuntimeError("BackgroundServer has already been started.")

        def _run() -> None:
            try:
                self._server.run()
            except BaseException as exc:  # noqa: BLE001 - surfaced via .start()
                self._error = exc

        self._thread = threading.Thread(target=_run, name=_THREAD_NAME, daemon=True)
        self._thread.start()

        deadline = time.monotonic() + self._startup_timeout_s
        while time.monotonic() < deadline:
            if self._error is not None:
                raise ServerStartupError(
                    f"Backend failed to start: {self._error!r}"
                ) from self._error
            if self._server.started:
                _LOG.info("Backend listening on %s", self.base_url)
                return self
            time.sleep(_POLL_INTERVAL_S)

        self.stop()
        raise ServerStartupError(
            f"Backend did not start listening within {self._startup_timeout_s} s."
        )

    def stop(self) -> None:
        """Request shutdown and wait for the serving thread to exit.

        Idempotent. After it returns, :attr:`stopped` tells you whether the
        thread really exited -- it is never assumed.
        """
        self._stop_requested = True
        self._server.should_exit = True
        thread = self._thread
        if thread is None:
            self._thread_exited = True
            return
        if thread.is_alive():
            thread.join(timeout=self._shutdown_timeout_s)
        if thread.is_alive():  # pragma: no cover - would indicate a hang
            self._thread_exited = False
            _LOG.error(
                "Backend thread did not exit within %.1f s", self._shutdown_timeout_s
            )
        else:
            self._thread_exited = True
        self._thread = None

    def __enter__(self) -> BackgroundServer:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()

    # -- introspection ------------------------------------------------------

    @property
    def started(self) -> bool:
        """Whether uvicorn reported startup. Stays ``True`` after shutdown."""
        return bool(self._server.started)

    @property
    def is_running(self) -> bool:
        """Whether the serving thread is currently alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def stopped(self) -> bool:
        """Whether :meth:`stop` ran and the serving thread actually exited."""
        return self._stop_requested and self._thread_exited and not self.is_running

    @staticmethod
    def live_server_threads() -> list[str]:
        """Names of any backend threads still alive; empty means none leaked."""
        return [t.name for t in threading.enumerate() if t.name.startswith(_THREAD_NAME)]

    @property
    def port(self) -> int:
        """The port actually bound (resolves ``0`` to the OS-assigned port)."""
        for server in getattr(self._server, "servers", []) or []:
            for socket in getattr(server, "sockets", []) or []:
                try:
                    return int(socket.getsockname()[1])
                except (OSError, IndexError, TypeError):  # pragma: no cover
                    continue
        return self._requested_port

    @property
    def host(self) -> str:
        return self._host

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self.port}"

    def describe(self) -> dict[str, Any]:
        return {
            "host": self._host,
            "requested_port": self._requested_port,
            "bound_port": self.port,
            "base_url": self.base_url,
            "started": self.started,
            "is_running": self.is_running,
            "stopped": self.stopped,
            "live_threads": self.live_server_threads(),
        }
