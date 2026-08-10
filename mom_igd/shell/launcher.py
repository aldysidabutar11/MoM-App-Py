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
import re
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
    {
        "/health",
        "/version",
        "/doctor",
        "/internal/ready",
        # Phase 2, read-only. None of these opens the microphone.
        "/audio/devices",
        "/audio/preflight",
        "/audio/recordings/status",
        "/audio/quality",
        "/audio/recovery/pending",
        # Phase 3, read-only. None of these opens the microphone, creates a key,
        # decrypts a voiceprint or loads a model.
        "/enrollment/participants",
        "/enrollment/consent/text",
        "/enrollment/sessions/current",
        "/enrollment/cleanup/pending",
        "/enrollment/meetings",
        # Phase 4, read-only. Neither loads a model: `/asr/status` and `/asr/models`
        # read the readiness index, and neither can cause a download.
        "/asr/status",
        "/asr/models",
        "/asr/recordings",
        "/asr/preflight",
        # Minutes, read-only. Neither loads a model: both read the readiness
        # index, and neither can cause a download.
        "/mom/status",
        "/mom/transcripts",
    }
)
"""Explicit GET allowlist. The page cannot ask the proxy to call an arbitrary path."""

ALLOWED_POST_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/audio/devices/select",
        "/audio/open-test",
        "/audio/calibrate",
        "/audio/recordings/start",
        "/audio/recordings/pause",
        "/audio/recordings/resume",
        "/audio/recordings/stop",
        "/audio/recovery/run",
        # Phase 3. `sessions/current/samples` is the one that engages the
        # microphone, and it is reachable only from a wizard button press.
        "/enrollment/participants",
        "/enrollment/sessions",
        "/enrollment/sessions/current/samples",
        "/enrollment/sessions/current/finalize",
        "/enrollment/sessions/current/cancel",
        "/enrollment/cleanup/retry",
        # Phase 4. `transcribe` is the heavy one: it runs the whole pipeline in worker
        # processes and takes minutes, so it is reachable only from a button press. It
        # never downloads a model -- a missing one is MODEL_UNAVAILABLE.
        "/asr/transcribe",
        "/asr/cancel",
        # Minutes. `generate` is the heavy one -- it runs the language model in
        # worker processes and takes minutes -- so it is reachable only from a
        # button press. `export` writes into the exports directory and loads no
        # model; the directory is not nameable from the request and the format
        # comes from a closed set.
        "/mom/generate",
        "/mom/cancel",
        "/mom/export",
    }
)
"""Explicit POST allowlist. ``calibrate``, ``open-test``, ``recordings/start`` and
``sessions/current/samples`` engage the microphone, which is why they are reachable
only from a button press."""

# Templated paths. Each is anchored at both ends and each UUID segment must be a
# canonical lower-case UUID, so a bounded set of shapes is permitted rather than a
# prefix wildcard. `/enrollment/*` would let the page reach any future route,
# including one added later without thinking about the shell.
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _templated(*suffixes: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(f"^{pattern}$") for pattern in suffixes)


ALLOWED_GET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = _templated(
    rf"/audio/recordings/{_UUID}/verify",
    rf"/enrollment/participants/{_UUID}",
    rf"/enrollment/participants/{_UUID}/consent",
    rf"/enrollment/participants/{_UUID}/readiness",
    rf"/enrollment/participants/{_UUID}/voiceprint",
    rf"/enrollment/participants/{_UUID}/eligibility",
    rf"/enrollment/meetings/{_UUID}/participants",
    rf"/enrollment/meetings/{_UUID}/roster",
    rf"/asr/transcript/{_UUID}",
    rf"/asr/revisions/{_UUID}",
    rf"/asr/flagged/{_UUID}",
    rf"/mom/minute/{_UUID}",
    rf"/mom/revisions/{_UUID}",
)
"""Templated GET paths the page may call."""

ALLOWED_POST_PATTERNS: Final[tuple[re.Pattern[str], ...]] = _templated(
    rf"/enrollment/participants/{_UUID}/deactivate",
    rf"/enrollment/participants/{_UUID}/reactivate",
    rf"/enrollment/participants/{_UUID}/consent/grant",
    rf"/enrollment/participants/{_UUID}/consent/revoke",
    rf"/enrollment/meetings/{_UUID}/participants",
    rf"/enrollment/voiceprints/{_UUID}/verify",
)
"""Templated POST paths the page may call."""

ALLOWED_PATCH_PATTERNS: Final[tuple[re.Pattern[str], ...]] = _templated(
    rf"/enrollment/participants/{_UUID}",
    rf"/enrollment/meetings/{_UUID}/capacity",
)
"""The two PATCHes the page may issue: a participant's descriptive fields, and one
meeting's roster capacity.

Capacity is a PATCH rather than a POST because it replaces one field of an existing
meeting. It cannot create a meeting, cannot touch a roster membership, and cannot
reach any other meeting attribute -- the path is anchored and the body carries a
single integer."""

ALLOWED_DELETE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = _templated(
    rf"/enrollment/meetings/{_UUID}/participants/{_UUID}",
)
"""The only DELETE the page may issue: removing a participant from a meeting.

Note what this cannot reach: there is no route that deletes a participant, and no
route that deletes a voiceprint directly. Deactivation and consent revocation are
the supported paths, and both are POSTs.
"""

_PROXY_TIMEOUT_S: Final[float] = 60.0


def _permitted(
    path: str,
    exact: frozenset[str],
    patterns: tuple[re.Pattern[str], ...],
) -> bool:
    """Whether the page may call ``path``.

    Exact match or one anchored template, never a prefix. A query string is refused
    outright rather than stripped: the caller passes query values through the
    ``query`` argument, so a ``?`` in the path means something is constructing URLs
    by hand -- and that is how a credential ends up in one.
    """
    if "?" in path or "#" in path:
        return False
    if path in exact:
        return True
    return any(pattern.match(path) for pattern in patterns)


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

    def api_get(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform an authenticated loopback GET on behalf of the page.

        Returns an envelope ``{"ok": bool, "status": int, "data"|"error": ...}``
        rather than raising, so the page can render a degraded state instead of
        breaking on an exception crossing the bridge.
        """
        if not _permitted(path, ALLOWED_PROXY_PATHS, ALLOWED_GET_PATTERNS):
            return {
                "ok": False,
                "status": 0,
                "error": f"Path {path!r} is not in the shell proxy allowlist.",
            }
        url = f"{self._base_url}{path}"
        if query:
            from urllib.parse import urlencode

            # Only scalar values, and never a credential: the token travels in a
            # header, and the API rejects a credential in a query string outright.
            safe = {k: v for k, v in query.items() if isinstance(v, (str, int, float, bool))}
            if safe:
                url = f"{url}?{urlencode(safe)}"
        return self._send(url, method="GET")

    def api_post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform an authenticated loopback POST on behalf of the page.

        Separate from :meth:`api_get` and separately allowlisted, because these are
        the calls that change state or engage the microphone.
        """
        if not _permitted(path, ALLOWED_POST_PATHS, ALLOWED_POST_PATTERNS):
            return {
                "ok": False,
                "status": 0,
                "error": f"Path {path!r} is not in the shell proxy POST allowlist.",
            }
        body = json.dumps(payload or {}).encode("utf-8")
        return self._send(url=f"{self._base_url}{path}", method="POST", body=body)

    def api_patch(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform an authenticated loopback PATCH. Separately allowlisted.

        Only participant edits need it, so the pattern list has exactly one entry.
        """
        if not _permitted(path, frozenset(), ALLOWED_PATCH_PATTERNS):
            return {
                "ok": False,
                "status": 0,
                "error": f"Path {path!r} is not in the shell proxy PATCH allowlist.",
            }
        body = json.dumps(payload or {}).encode("utf-8")
        return self._send(url=f"{self._base_url}{path}", method="PATCH", body=body)

    def api_delete(self, path: str) -> dict[str, Any]:
        """Perform an authenticated loopback DELETE. Separately allowlisted.

        Only meeting-membership removal needs it. Nothing reachable here deletes a
        participant or a voiceprint: those are deactivation and consent revocation,
        and both are POSTs.
        """
        if not _permitted(path, frozenset(), ALLOWED_DELETE_PATTERNS):
            return {
                "ok": False,
                "status": 0,
                "error": f"Path {path!r} is not in the shell proxy DELETE allowlist.",
            }
        return self._send(url=f"{self._base_url}{path}", method="DELETE")

    def _send(self, url: str, *, method: str, body: bytes | None = None) -> dict[str, Any]:
        headers = {
            **self._token.header(),
            "Accept": "application/json",
            # Explicit loopback Host so the LoopbackHostMiddleware accepts it.
            "Host": self._base_url.split("//", 1)[-1],
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(  # noqa: S310 - loopback URL, fixed scheme
            url=url, method=method, headers=headers, data=body
        )
        try:
            with urllib.request.urlopen(request, timeout=_PROXY_TIMEOUT_S) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
                return {"ok": True, "status": int(response.status), "data": payload}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                detail: Any = json.loads(raw)
            except json.JSONDecodeError:
                detail = raw[:500]
            if isinstance(detail, dict) and "detail" in detail:
                detail = detail["detail"]
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
