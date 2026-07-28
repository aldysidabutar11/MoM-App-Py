"""Configuration validation.

Covers Phase 1 test categories 1-5: the default configuration is valid,
environment overrides work, and every rejection rule actually rejects.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mom_igd.config import (
    MAX_HEAVY_WORKERS,
    PUBLIC_ENDPOINTS,
    SUPPORTED_RUNTIME_MODES,
    AppConfig,
    ConfigError,
    default_config_path,
    load_config,
)
from mom_igd.paths import repo_root
from mom_igd.version import APP_NAME, APP_VERSION, CONFIG_SCHEMA_VERSION


# --------------------------------------------------------------- 1. defaults


def test_default_config_file_exists_and_is_tracked() -> None:
    assert default_config_path().is_file()
    assert default_config_path() == repo_root() / "config" / "default.toml"


def test_default_config_loads_and_is_valid(config: AppConfig) -> None:
    assert config.config_schema_version == CONFIG_SCHEMA_VERSION
    assert config.app_name == APP_NAME
    assert config.app_version == APP_VERSION
    assert config.runtime_mode == "offline"
    assert config.offline is True
    assert config.log_level == "INFO"


def test_default_config_enforces_phase_1_invariants(config: AppConfig) -> None:
    assert config.api.host == "127.0.0.1"
    assert config.resources.max_heavy_workers == MAX_HEAVY_WORKERS == 1
    assert config.resources.min_free_ram_mb > 0
    assert config.resources.min_free_disk_gb > 0
    assert config.database.busy_timeout_ms > 0
    assert config.ui.startup_timeout_s > 0
    assert config.api.startup_timeout_s > 0
    assert config.providers.endpoints == {}, "Phase 1 selects no AI provider"


def test_summary_is_serialisable_and_secret_free(config: AppConfig) -> None:
    summary = config.summary()
    assert summary["api"]["host"] == "127.0.0.1"
    assert "token" not in repr(summary).lower()


def test_public_endpoint_policy_is_declared() -> None:
    # The documented authentication policy must be explicit, not implied.
    assert PUBLIC_ENDPOINTS == ("/health", "/version")


def test_port_strategy_controls_effective_port(config: AppConfig) -> None:
    ephemeral = AppConfig.model_validate(
        {**config.model_dump(), "api": {**config.api.model_dump(), "port_strategy": "ephemeral"}}
    )
    fixed = AppConfig.model_validate(
        {
            **config.model_dump(),
            "api": {**config.api.model_dump(), "port_strategy": "fixed", "port": 8765},
        }
    )
    assert ephemeral.api.effective_port() == 0
    assert fixed.api.effective_port() == 8765


# ------------------------------------------------- 2. environment overrides


def test_environment_variable_overrides_config_file(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOM_IGD_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("MOM_IGD_API_PORT", "9123")
    monkeypatch.setenv("MOM_IGD_API_PORT_STRATEGY", "fixed")
    monkeypatch.setenv("MOM_IGD_DB_BUSY_TIMEOUT_MS", "7500")
    config = load_config(use_local_file=False)
    assert config.log_level == "DEBUG"
    assert config.api.port == 9123
    assert config.api.port_strategy == "fixed"
    assert config.database.busy_timeout_ms == 7500


def test_data_dir_environment_variable_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "elsewhere"
    monkeypatch.setenv("MOM_IGD_DATA_DIR", str(target))
    config = load_config(use_local_file=False)
    assert config.data_root == target.resolve()


def test_explicit_argument_beats_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOM_IGD_DATA_DIR", str(tmp_path / "from-env"))
    chosen = tmp_path / "from-argument"
    config = load_config(data_root=chosen, use_local_file=False)
    assert config.data_root == chosen.resolve()


def test_non_integer_environment_override_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOM_IGD_API_PORT", "not-a-number")
    with pytest.raises(ConfigError, match="must be an integer"):
        load_config(use_local_file=False)


# ------------------------------------------- 3. non-loopback host rejection


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "*", "192.168.1.10", "10.0.0.5", "8.8.8.8", "example.com", ""],
)
def test_non_loopback_api_host_is_rejected(data_root: Path, host: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            data_root=data_root, overrides={"api": {"host": host}}, use_local_file=False
        )
    assert "loopback" in str(excinfo.value).lower() or "empty" in str(excinfo.value).lower()


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_loopback_hosts_are_accepted(data_root: Path, host: str) -> None:
    config = load_config(
        data_root=data_root, overrides={"api": {"host": host}}, use_local_file=False
    )
    assert config.api.host == host


# ------------------------------------------- 4. heavy worker count rejection


@pytest.mark.parametrize("count", [2, 3, 16])
def test_more_than_one_heavy_worker_is_rejected(data_root: Path, count: int) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            data_root=data_root,
            overrides={"resources": {"max_heavy_workers": count}},
            use_local_file=False,
        )
    assert "max_heavy_workers" in str(excinfo.value)


def test_zero_heavy_workers_is_also_rejected(data_root: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            data_root=data_root,
            overrides={"resources": {"max_heavy_workers": 0}},
            use_local_file=False,
        )


# --------------------------------------------- 5. cloud provider URL rejection


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.openai.com/v1",
        "https://api.anthropic.com",
        "https://generativelanguage.googleapis.com",
        "https://huggingface.co/models",
        "https://api.deepgram.com/v1/listen",
        "http://10.0.0.7:8080",
        "http://0.0.0.0:8080",
        "ftp://127.0.0.1/model",
        "api.openai.com",
    ],
)
def test_cloud_or_remote_provider_endpoint_is_rejected(data_root: Path, endpoint: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            data_root=data_root,
            overrides={"providers": {"endpoints": {"llm": endpoint}}},
            use_local_file=False,
        )
    message = str(excinfo.value)
    assert "llm" in message


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:8080",
        "http://localhost:11434",
        "http://[::1]:8080",
        r"D:\MoM-IGD-Data\models\asr\model.bin",
        "asr/whisper/model.bin",
        "file:///D:/MoM-IGD-Data/models/x.gguf",
        "",
    ],
)
def test_local_or_loopback_provider_endpoint_is_accepted(data_root: Path, endpoint: str) -> None:
    config = load_config(
        data_root=data_root,
        overrides={"providers": {"endpoints": {"llm": endpoint}}},
        use_local_file=False,
    )
    assert config.providers.endpoints["llm"] == endpoint


# ------------------------------------------------------- other reject rules


@pytest.mark.parametrize("mode", ["online", "hybrid", "cloud", "OFFLINE_PLUS", ""])
def test_unsupported_runtime_mode_is_rejected(data_root: Path, mode: str) -> None:
    with pytest.raises(ConfigError, match="runtime_mode"):
        load_config(data_root=data_root, overrides={"runtime_mode": mode}, use_local_file=False)


def test_only_offline_runtime_mode_is_supported() -> None:
    assert SUPPORTED_RUNTIME_MODES == frozenset({"offline"})


def test_offline_flag_cannot_be_disabled(data_root: Path) -> None:
    with pytest.raises(ConfigError, match="offline"):
        load_config(data_root=data_root, overrides={"offline": False}, use_local_file=False)


def test_unknown_log_level_is_rejected(data_root: Path) -> None:
    with pytest.raises(ConfigError, match="log_level"):
        load_config(data_root=data_root, overrides={"log_level": "CHATTY"}, use_local_file=False)


def test_unknown_config_schema_version_is_rejected(data_root: Path) -> None:
    with pytest.raises(ConfigError, match="config_schema_version"):
        load_config(
            data_root=data_root, overrides={"config_schema_version": 99}, use_local_file=False
        )


def test_unknown_configuration_key_is_rejected(data_root: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            data_root=data_root, overrides={"totally_unknown_key": 1}, use_local_file=False
        )


def test_database_filename_must_be_bare(data_root: Path) -> None:
    with pytest.raises(ConfigError, match="filename"):
        load_config(
            data_root=data_root,
            overrides={"database": {"filename": "sub/dir/mom.db"}},
            use_local_file=False,
        )


def test_missing_config_file_reports_clearly(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(config_path=tmp_path / "nope.toml", use_local_file=False)


def test_malformed_config_file_reports_clearly(tmp_path: Path) -> None:
    broken = tmp_path / "broken.toml"
    broken.write_text("this is [not valid toml", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(config_path=broken, use_local_file=False)


def test_model_registry_relative_path_resolves_against_repo(config: AppConfig) -> None:
    assert config.model_registry_path.is_absolute()
    assert config.model_registry_path == repo_root() / "models" / "registry.json"
