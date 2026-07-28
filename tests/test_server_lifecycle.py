"""Real-socket server lifecycle and the headless smoke test.

Covers Phase 1 test categories 26 and 27. These tests bind a loopback socket,
which is a local operation and involves no external network.
"""

from __future__ import annotations

import threading

import pytest

from mom_igd.api.server import BackgroundServer, ServerStartupError
from mom_igd.config import AppConfig
from mom_igd.offline_policy import OfflinePolicyError
from mom_igd.security import SessionToken
from mom_igd.smoke import run_smoke

pytestmark = pytest.mark.slow


def _threads_named(prefix: str = "mom-igd-api") -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith(prefix)]


# ------------------------------------------------- 26. clean start and stop


def test_server_starts_and_stops_cleanly(app) -> None:
    before = _threads_named()
    server = BackgroundServer(app, host="127.0.0.1", port=0, log_level="warning")

    server.start()
    assert server.started is True
    assert server.is_running is True
    assert server.port > 0
    assert server.base_url.startswith("http://127.0.0.1:")

    server.stop()
    assert server.stopped is True
    assert server.is_running is False
    assert _threads_named() == before, "the serving thread must not be left behind"


def test_context_manager_stops_the_server(app) -> None:
    with BackgroundServer(app, port=0, log_level="warning") as server:
        assert server.is_running
        port = server.port
        assert port > 0
    assert server.stopped
    assert _threads_named() == []


def test_stop_is_idempotent(app) -> None:
    server = BackgroundServer(app, port=0, log_level="warning").start()
    server.stop()
    server.stop()  # must not raise
    assert server.stopped


def test_stopping_a_server_that_never_started_is_safe(app) -> None:
    server = BackgroundServer(app, port=0, log_level="warning")
    server.stop()
    assert server.stopped is True
    assert server.is_running is False


def test_starting_twice_is_refused(app) -> None:
    server = BackgroundServer(app, port=0, log_level="warning").start()
    try:
        with pytest.raises(RuntimeError, match="already been started"):
            server.start()
    finally:
        server.stop()


def test_ephemeral_port_zero_resolves_to_a_real_port(app) -> None:
    with BackgroundServer(app, port=0, log_level="warning") as server:
        assert server.port not in (0, None)
        assert 1024 < server.port <= 65535


def test_describe_reports_the_lifecycle(app) -> None:
    server = BackgroundServer(app, port=0, log_level="warning").start()
    running = server.describe()
    assert running["host"] == "127.0.0.1"
    assert running["requested_port"] == 0
    assert running["bound_port"] > 0
    assert running["is_running"] is True
    server.stop()
    assert server.describe()["stopped"] is True
    assert server.describe()["live_threads"] == []


def test_server_refuses_a_non_loopback_bind_address(app) -> None:
    """Defence in depth: even bypassing configuration validation must fail."""
    for host in ("0.0.0.0", "192.168.1.10", "::"):
        with pytest.raises(OfflinePolicyError):
            BackgroundServer(app, host=host, port=0)


def test_two_servers_can_run_on_distinct_ephemeral_ports(
    config: AppConfig, paths, token: SessionToken
) -> None:
    from mom_igd.api.app import create_app

    first = BackgroundServer(create_app(config, session_token=token, paths=paths), port=0, log_level="warning")
    second = BackgroundServer(create_app(config, session_token=token, paths=paths), port=0, log_level="warning")
    try:
        first.start()
        second.start()
        assert first.port != second.port
    finally:
        first.stop()
        second.stop()
    assert _threads_named() == []


def test_failure_to_bind_is_reported_not_swallowed(
    config: AppConfig, paths, token: SessionToken
) -> None:
    """A port clash must surface as ServerStartupError, not a silent hang.

    Two servers are asked for the same fixed port. The second cannot bind, its
    thread dies with an error, and `start()` must re-raise it rather than spin
    until the timeout.
    """
    import socket

    from mom_igd.api.app import create_app

    # Ask the OS for a free port, then release it so both servers target it.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    first = BackgroundServer(
        create_app(config, session_token=token, paths=paths), port=port, log_level="warning"
    )
    second = BackgroundServer(
        create_app(config, session_token=token, paths=paths),
        port=port,
        log_level="warning",
        startup_timeout_s=5.0,
    )
    try:
        first.start()
        assert first.port == port
        with pytest.raises(ServerStartupError, match="failed to start"):
            second.start()
    finally:
        second.stop()
        first.stop()
    assert _threads_named() == [], "no thread may survive a failed start"


def test_startup_timeout_is_bounded(app) -> None:
    """`start()` must give up on a deadline instead of blocking forever."""

    class _NeverReady:
        """Stands in for uvicorn: runs, but never reports `started`."""

        started = False
        should_exit = False
        servers: list[object] = []

        def run(self) -> None:
            import time

            while not self.should_exit:
                time.sleep(0.01)

    server = BackgroundServer(app, port=0, log_level="warning", startup_timeout_s=0.2)
    server._server = _NeverReady()  # type: ignore[assignment]  # noqa: SLF001
    with pytest.raises(ServerStartupError, match="did not start listening"):
        server.start()
    assert server.stopped is True
    assert _threads_named() == []


# ------------------------------------------------------- 27. headless smoke


def test_headless_smoke_passes_end_to_end(config: AppConfig) -> None:
    result = run_smoke(config, use_temp_data_root=True)
    failures = [step for step in result["steps"] if not step["ok"]]
    assert failures == [], f"smoke steps failed: {failures}"
    assert result["ok"] is True
    assert result["passed"] == result["total"]


def test_headless_smoke_covers_the_required_contracts(config: AppConfig) -> None:
    result = run_smoke(config, use_temp_data_root=True)
    names = {step["name"] for step in result["steps"]}
    assert {
        "database_init",
        "server_start",
        "health_public",
        "version_public",
        "protected_requires_token",
        "protected_accepts_token",
        "token_in_query_rejected",
        "non_loopback_host_rejected",
        "static_ui_local_only",
        "token_never_in_response",
        "clean_shutdown",
    } <= names


def test_headless_smoke_needs_no_gui(config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing pywebview must not be required to run the smoke test."""
    import builtins

    real_import = builtins.__import__

    def _guard(name, *args, **kwargs):
        if name == "webview" or name.startswith("webview."):
            raise AssertionError("the smoke test must not import the GUI toolkit")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _guard)
    assert run_smoke(config, use_temp_data_root=True)["ok"] is True


def test_headless_smoke_uses_a_throwaway_data_root(config: AppConfig, data_root) -> None:
    assert not data_root.exists()
    assert run_smoke(config, use_temp_data_root=True)["ok"] is True
    assert not data_root.exists(), "the configured data root must be untouched"


def test_headless_smoke_leaves_no_thread_behind(config: AppConfig) -> None:
    run_smoke(config, use_temp_data_root=True)
    assert _threads_named() == []


def test_headless_smoke_can_target_the_configured_data_root(config: AppConfig, data_root) -> None:
    result = run_smoke(config, use_temp_data_root=False)
    assert result["ok"] is True
    assert (data_root / "db").is_dir(), "--keep-db must use the configured root"
