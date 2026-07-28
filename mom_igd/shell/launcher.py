"""Desktop shell launcher.

Token handling, which is the only subtle part:

The session token is **never given to JavaScript**. The window is loaded over
loopback HTTP so the page can call the two public endpoints directly, and
everything that needs the token goes through :meth:`ShellApi.api_get`, a Python
method exposed to the page by pywebview. The Python side attaches the token and
returns parsed JSON. Consequently the token never appears in a URL, in
``localStorage``, in ``sessionStorage``, in a cookie or in the DOM.

``urllib.request`` from the standard library is used for the loopback call on
purpose: adding an HTTP client to the runtime dependencies just to talk to
ourselves would be gratuitous, and ``httpx`` stays a test-only dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Final

from mom_igd.api.app import create_app
from mom_igd.api.server import BackgroundServer
from mom_igd.config import AppConfig
from mom_igd.logging_setup import get_logger
from mom_igd.security import SessionToken
from mom_igd.version import APP_NAME, APP_VERSION, CURRENT_PHASE

__all__ = ["ALLOWED_PROXY_PATHS", "ShellApi", "manual_launch_command", "run_shell"]

_LOG = get_logger("shell")

ALLOWED_PROXY_PATHS: Final[frozenset[str]] = frozenset(
    {"/health", "/version", "/doctor", "/internal/ready"}
)
"""Explicit allowlist. The page cannot ask the proxy to call an arbitrary path."""

_PROXY_TIMEOUT_S: Final[float] = 30.0


class ShellApi:
    """Python API exposed to the page as ``window.pywebview.api``."""

    def __init__(self, base_url: str, token: SessionToken, config: AppConfig) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._config = config

    def bootstrap(self) -> dict[str, Any]:
        """Identity and backend location. Contains no secret."""
        return {
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "phase": CURRENT_PHASE,
            "offline": self._config.offline,
            "runtime_mode": self._config.runtime_mode,
            "base_url": self._base_url,
            "proxy_available": True,
        }

    def api_get(self, path: str) -> dict[str, Any]:
        """Perform an authenticated loopback GET on behalf of the page.

        Returns a envelope ``{"ok": bool, "status": int, "data"|"error": ...}``
        rather than raising, so the page can render a degraded state instead of
        breaking on an exception crossing the bridge.
        """
        if path not in ALLOWED_PROXY_PATHS:
            return {
                "ok": False,
                "status": 0,
                "error": f"Path {path!r} is not in the shell proxy allowlist.",
            }
        request = urllib.request.Request(  # noqa: S310 - loopback URL, fixed scheme
            url=f"{self._base_url}{path}",
            method="GET",
            headers={
                **self._token.header(),
                "Accept": "application/json",
                # Explicit loopback Host so the LoopbackHostMiddleware accepts it.
                "Host": self._base_url.split("//", 1)[-1],
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_PROXY_TIMEOUT_S) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
                return {"ok": True, "status": int(response.status), "data": payload}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                detail: Any = json.loads(body)
            except json.JSONDecodeError:
                detail = body[:500]
            return {"ok": False, "status": int(exc.code), "error": detail}
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            return {"ok": False, "status": 0, "error": f"{type(exc).__name__}: {exc}"}


def manual_launch_command() -> str:
    """The exact command an operator runs to open the window."""
    return r".venv\Scripts\python.exe -m mom_igd shell"


def run_shell(config: AppConfig) -> int:
    """Start the backend in-process and open the WebView2 window.

    Blocks until the user closes the window, then shuts the backend down.

    Returns:
        Process exit code: ``0`` on clean close, ``1`` if pywebview is missing.
    """
    try:
        import webview  # noqa: PLC0415 - optional GUI dependency, imported lazily
    except ImportError as exc:
        _LOG.error(
            "pywebview is not installed (%s). Install the Phase 1 requirements "
            "into the project virtual environment.",
            exc,
        )
        return 1

    paths = config.runtime_paths().ensure()
    token = SessionToken()
    app = create_app(config, session_token=token, paths=paths)

    server = BackgroundServer(
        app,
        host=config.api.host,
        port=config.api.effective_port(),
        log_level=config.log_level.lower(),
        startup_timeout_s=config.api.startup_timeout_s,
        shutdown_timeout_s=config.api.shutdown_timeout_s,
    ).start()

    try:
        api = ShellApi(server.base_url, token, config)
        webview.create_window(
            title=config.ui.window_title,
            url=f"{server.base_url}/ui/",
            js_api=api,
            width=config.ui.window_width,
            height=config.ui.window_height,
            min_size=(900, 600),
            text_select=True,
        )
        # gui=None lets pywebview pick its best Windows backend (EdgeChromium /
        # WebView2 when available). Nothing is fetched from the network.
        webview.start(debug=False)
        return 0
    finally:
        server.stop()
