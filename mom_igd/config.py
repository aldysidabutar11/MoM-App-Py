"""Versioned application configuration and its validation rules.

Configuration is layered, highest precedence last:

1. ``config/default.toml`` -- tracked in Git, the documented defaults.
2. ``config/local.toml`` -- optional, git-ignored, per-machine overrides.
3. Environment variables (``MOM_IGD_*``).
4. Explicit ``overrides`` passed by the caller (CLI flags, tests).

Validation is not advisory. :func:`load_config` refuses to produce a
configuration object that would let the application bind to a non-loopback
address, run more than one heavy worker, write runtime data into the source
tree, use a relative data path, run in an unsupported runtime mode, or point a
provider at a cloud URL.
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from mom_igd import offline_policy
from mom_igd.paths import (
    ENV_DATA_DIR,
    PathValidationError,
    RuntimePaths,
    repo_root,
    resolve_data_root,
)
from mom_igd.version import (
    APP_NAME,
    APP_VERSION,
    CONFIG_SCHEMA_VERSION,
)

__all__ = [
    "AppConfig",
    "ApiConfig",
    "ConfigError",
    "DatabaseConfig",
    "PUBLIC_ENDPOINTS",
    "ParticipantsConfig",
    "ProvidersConfig",
    "ResourceConfig",
    "SUPPORTED_RUNTIME_MODES",
    "UiConfig",
    "default_config_path",
    "load_config",
]

SUPPORTED_RUNTIME_MODES: Final[frozenset[str]] = frozenset({"offline"})
"""Runtime modes this build supports.

``offline`` is the only one, and not merely for now: there is no cloud or hybrid
mode and no cloud fallback (ADR-0002).
"""

MAX_HEAVY_WORKERS: Final[int] = 1
"""Hard ceiling from ADR-0004: at most one heavy model in one worker process."""

LOG_LEVELS: Final[tuple[str, ...]] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

PUBLIC_ENDPOINTS: Final[tuple[str, ...]] = ("/health", "/version")
"""Endpoints intentionally reachable without the session token.

Rationale (documented policy, see docs/architecture.md):

* Both are already restricted to loopback by the bind address and by the
  ``Host`` header check, so only local processes of the signed-in user can reach
  them at all.
* Both must work *before* the desktop shell has obtained a session token, so the
  shell can report "backend unreachable" instead of "unauthorised".
* Neither returns a filesystem path, hardware inventory, secret or user datum --
  only the application name, version, phase and coarse readiness booleans.

Every other endpoint, including ``/doctor`` and ``/internal/ready``, requires the
session token because it exposes host paths and hardware details.
"""

_ENV_PREFIX: Final[str] = "MOM_IGD_"


class ConfigError(ValueError):
    """Raised when configuration is invalid. Wraps pydantic and path errors."""


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


class ApiConfig(BaseModel):
    """Local HTTP API settings. Loopback only, by construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    port_strategy: Literal["fixed", "ephemeral"] = "ephemeral"
    docs_enabled: bool = True
    startup_timeout_s: float = Field(default=15.0, gt=0, le=120)
    shutdown_timeout_s: float = Field(default=10.0, gt=0, le=120)

    @field_validator("host")
    @classmethod
    def _host_must_be_loopback(cls, value: str) -> str:
        return offline_policy.validate_bind_host(value)

    def effective_port(self) -> int:
        """Return the port to bind: ``0`` requests an ephemeral OS-assigned port."""
        return 0 if self.port_strategy == "ephemeral" else self.port

    def base_url(self, actual_port: int | None = None) -> str:
        port = self.port if actual_port is None else actual_port
        return f"http://{self.host}:{port}"


class ResourceConfig(BaseModel):
    """Resource guardrails for a 16 GB single-device machine (ADR-0004)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_heavy_workers: int = Field(default=1, ge=1)
    min_free_ram_mb: int = Field(default=2048, ge=0)
    min_free_disk_gb: int = Field(default=10, ge=0)

    @field_validator("max_heavy_workers")
    @classmethod
    def _at_most_one_heavy_worker(cls, value: int) -> int:
        if value > MAX_HEAVY_WORKERS:
            raise ValueError(
                f"max_heavy_workers={value} exceeds the hard limit of "
                f"{MAX_HEAVY_WORKERS}. Only one heavy model may be resident at a "
                "time on the target hardware (16 GB RAM, ~4 GB observed free); "
                "see docs/adr/0004-single-heavy-worker-resource-policy.md."
            )
        return value


class DatabaseConfig(BaseModel):
    """SQLite settings. WAL and foreign keys are verified at connect time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    filename: str = "mom_igd.db"
    busy_timeout_ms: int = Field(default=5000, ge=0, le=120_000)

    @field_validator("filename")
    @classmethod
    def _bare_filename(cls, value: str) -> str:
        if not value or "/" in value or "\\" in value or value in {".", ".."}:
            raise ValueError(
                f"database.filename must be a bare file name, got {value!r}. The "
                "directory is decided by the runtime path service, not by config."
            )
        return value


class UiConfig(BaseModel):
    """Desktop shell settings (pywebview / WebView2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    startup_timeout_s: float = Field(default=20.0, gt=0, le=300)
    window_width: int = Field(default=1180, ge=640, le=7680)
    window_height: int = Field(default=780, ge=480, le=4320)
    window_title: str = f"{APP_NAME} - Offline Minutes of Meeting"


class AudioConfig(BaseModel):
    """Phase 2 capture settings.

    ``preferred_device_fingerprint`` is intentionally empty in the tracked
    ``default.toml``: a fingerprint identifies one physical microphone on one
    machine, so committing one would make every other machine start with a
    selection it cannot resolve. It belongs in ``config/local.toml`` or in the
    database, written by an explicit device selection.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    preferred_device_fingerprint: str = ""
    preferred_sample_rate: int | None = None
    max_channels: int = Field(default=2, ge=1, le=2)
    sample_format: Literal["int16"] = "int16"

    chunk_seconds: int = Field(default=30, ge=10, le=120)
    queue_seconds: float = Field(default=5.0, ge=0.25, le=60.0)
    writer_shutdown_timeout_s: float = Field(default=15.0, gt=0, le=120)

    min_free_disk_gb: float = Field(default=5.0, ge=0.0)
    """Recording refuses to start below this, and warns when it is approached."""

    low_disk_abort_gb: float = Field(default=1.0, ge=0.0)
    """A running recording finalises and stops cleanly below this."""

    calibration_seconds: int = Field(default=12, ge=10, le=15)

    too_quiet_dbfs: float = Field(default=-45.0, le=0.0)
    too_loud_peak_dbfs: float = Field(default=-1.0, le=0.0)
    clipping_percent_threshold: float = Field(default=0.01, ge=0.0, le=100.0)
    silence_dbfs_threshold: float = Field(default=-60.0, le=0.0)

    status_poll_hz: float = Field(default=3.0, ge=1.0, le=4.0)
    meter_stride: int = Field(default=1, ge=1, le=64)

    auto_recover_on_start: bool = True
    quarantine_ambiguous_partials: bool = True
    production_requires_usb: bool = True

    @field_validator("preferred_sample_rate")
    @classmethod
    def _supported_rate(cls, value: int | None) -> int | None:
        if value is None:
            return None
        from mom_igd.audio.backend import SUPPORTED_SAMPLE_RATES

        if value not in SUPPORTED_SAMPLE_RATES:
            raise ValueError(
                f"audio.preferred_sample_rate={value} is not supported. Allowed: "
                f"{list(SUPPORTED_SAMPLE_RATES)}. Leave it unset to use the "
                "device's native rate, which avoids resampling entirely."
            )
        return value

    @field_validator("preferred_device_fingerprint")
    @classmethod
    def _fingerprint_shape(cls, value: str) -> str:
        text = (value or "").strip().lower()
        if not text:
            return ""
        if len(text) != 32 or any(c not in "0123456789abcdef" for c in text):
            raise ValueError(
                f"audio.preferred_device_fingerprint={value!r} is not a 32-character "
                "hex fingerprint. Obtain one from `python -m mom_igd audio devices`; "
                "a PortAudio index is not a device identity."
            )
        return text

    @model_validator(mode="after")
    def _thresholds_are_ordered(self) -> AudioConfig:
        if self.low_disk_abort_gb > self.min_free_disk_gb:
            raise ValueError(
                f"audio.low_disk_abort_gb={self.low_disk_abort_gb} must not exceed "
                f"audio.min_free_disk_gb={self.min_free_disk_gb}: a recording cannot "
                "abort at a level above the one it refuses to start at."
            )
        if self.silence_dbfs_threshold >= self.too_quiet_dbfs:
            raise ValueError(
                f"audio.silence_dbfs_threshold={self.silence_dbfs_threshold} must be "
                f"below audio.too_quiet_dbfs={self.too_quiet_dbfs}, otherwise every "
                "quiet signal is reported as no signal at all."
            )
        if self.too_quiet_dbfs >= self.too_loud_peak_dbfs:
            raise ValueError(
                f"audio.too_quiet_dbfs={self.too_quiet_dbfs} must be below "
                f"audio.too_loud_peak_dbfs={self.too_loud_peak_dbfs}."
            )
        return self

    def capture_profile(self, *, sample_rate: int, channels: int):
        """Build a validated capture profile from this configuration."""
        from mom_igd.audio.backend import CaptureProfile, SampleFormat

        return CaptureProfile(
            sample_rate=self.preferred_sample_rate or sample_rate,
            channels=min(channels, self.max_channels),
            sample_format=SampleFormat.INT16,
            chunk_seconds=self.chunk_seconds,
        )


class ParticipantsConfig(BaseModel):
    """Meeting roster capacity: the default, and the safety ceiling.

    **This is the single source of truth for both numbers.** Before Phase 3's
    corrective pass a module constant held a fixed nine and every meeting was
    forced to it. Nine is the *default* the first deployment needs, not a property
    of the product, so it lives here where an operator can change it -- while the
    number actually in force for a given meeting is stored per meeting in the
    database (migration 0004), so a later change to this default never retunes a
    meeting recorded before it.

    ``maximum_meeting_participant_capacity`` is a guard rail against a typo turning
    one meeting into a thousand-row roster. It is **not** a claim that this many
    speakers can be told apart: that needs a real embedding model, a USB conference
    microphone and acceptance in the real room, none of which exist yet.

    Neither number decides whether audio is recorded. Capture always takes the
    whole room signal; the roster only decides who the *known* speaker candidates
    are, and anyone outside it becomes ``UNKNOWN`` from Phase 6 onwards.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    default_meeting_participant_capacity: int = Field(default=9, ge=1)
    maximum_meeting_participant_capacity: int = Field(default=50, ge=1)

    @model_validator(mode="after")
    def _default_within_ceiling(self) -> ParticipantsConfig:
        if self.default_meeting_participant_capacity > (
            self.maximum_meeting_participant_capacity
        ):
            raise ValueError(
                "default_meeting_participant_capacity="
                f"{self.default_meeting_participant_capacity} exceeds "
                "maximum_meeting_participant_capacity="
                f"{self.maximum_meeting_participant_capacity}. The default must be "
                "a value the ceiling actually permits; raise the ceiling or lower "
                "the default."
            )
        return self


class AsrConfig(BaseModel):
    """Offline transcription: thread counts, decode settings and the pass-2 policy.

    Every default here was **measured** on the target device in the Phase 4A sweep
    (``docs/benchmarks.md``), not guessed. The thread counts in particular are
    machine-specific: a different CPU has a different knee, and the honest way to
    retune is to re-run ``asr bench`` rather than to reason about core counts.

    ``pass2_budget_ratio`` is the fraction of detected *speech* time pass 2 may
    re-transcribe. It exists because pass 2 costs roughly twice pass 1 per second of
    audio, so an unbounded pass 2 would put total RTF over the target on a long
    meeting. Selection spends the budget on the least confident regions first.

    No key here selects a provider implementation, and none can enable a fake one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    language: str = "id"
    """Primary language hint. Indonesian, with English technical terms mixed in --
    Whisper handles the code-switching within one language setting better than it
    handles being told the language is English."""

    pass1_cpu_threads: int = Field(default=12, ge=0, le=256)
    pass2_cpu_threads: int = Field(default=12, ge=0, le=256)
    """0 lets CTranslate2 choose. 12 is the measured optimum on the i7-1260P: RTF
    improves monotonically to 12 and then flattens (14 and 16 were measured and are
    not better)."""

    pass1_beam_size: int = Field(default=1, ge=1, le=16)
    pass2_beam_size: int = Field(default=5, ge=1, le=16)
    """Beam size dominates throughput -- a 2.5x swing between 1 and 5 on identical
    audio. Pass 1 runs over every region and needs the speed; pass 2 runs over a
    budgeted subset where quality is the entire point. **This split is provisional:
    it trades accuracy for speed on pass 1 and no accuracy measurement exists yet to
    say whether that trade is acceptable.**"""

    compute_type: Literal["int8", "int8_float32", "int16", "float32"] = "int8"

    initial_prompt_max_chars: int = Field(default=400, ge=0, le=2000)
    """Cap on the terminology prompt handed to the decoder. Whisper's prompt window
    is finite and a long prompt evicts the audio context it is meant to help; it can
    also be echoed into the transcript, which is why this is bounded rather than
    "as many terms as we have"."""

    pass2_enabled: bool = True
    pass2_budget_ratio: float = Field(default=0.25, ge=0.0, le=1.0)

    pass2_min_avg_logprob: float = Field(default=-1.0, le=0.0)
    pass2_max_no_speech_prob: float = Field(default=0.6, ge=0.0, le=1.0)
    pass2_min_word_probability: float = Field(default=0.45, ge=0.0, le=1.0)
    pass2_max_compression_ratio: float = Field(default=2.4, gt=0.0)
    """Selection thresholds. ``2.4`` is Whisper's own repetition heuristic, kept
    rather than invented so a well-understood number is not replaced by a new one."""

    vad_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    vad_min_speech_ms: int = Field(default=250, ge=0, le=10_000)
    vad_min_silence_ms: int = Field(default=500, ge=0, le=10_000)
    vad_speech_pad_ms: int = Field(default=200, ge=0, le=5_000)
    vad_merge_gap_ms: int = Field(default=150, ge=0, le=10_000)
    vad_max_region_seconds: float = Field(default=30.0, gt=0.0, le=120.0)

    glossary_enabled: bool = True
    glossary_filename: str = "glossary.id-en.toml"

    worker_timeout_seconds: float = Field(default=3 * 60 * 60, gt=0.0)
    """Wall-clock ceiling for one heavy stage. Three hours is generous on purpose:
    the measured RTF is about 0.15, so this only fires on something genuinely stuck."""

    @field_validator("glossary_filename")
    @classmethod
    def _glossary_is_a_bare_filename(cls, value: str) -> str:
        text = value.strip()
        if not text or "/" in text or "\\" in text or ".." in text or ":" in text:
            raise ValueError(
                f"glossary_filename must be a bare file name inside config/, got "
                f"{value!r}. A path here would let configuration read a file from "
                "anywhere on the machine."
            )
        return text

    @model_validator(mode="after")
    def _pass2_must_be_worth_running(self) -> AsrConfig:
        if self.pass2_enabled and self.pass2_budget_ratio == 0.0:
            raise ValueError(
                "pass2_enabled is true but pass2_budget_ratio is 0.0, so pass 2 "
                "could never transcribe anything. Set pass2_enabled=false to turn "
                "it off, or give it a budget -- a stage that is enabled and can "
                "never run is a configuration that lies about what happens."
            )
        return self


class ProvidersConfig(BaseModel):
    """Future AI provider endpoints.

    Empty by default, and correctly so. The ASR provider is **not** selected here:
    it is a code-level decision recorded in ADR-0014, loaded from a hash-verified
    local model directory, precisely so that no configuration value can redirect it.
    Any value present must be a local filesystem path or a loopback URL.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoints: dict[str, str] = Field(default_factory=dict)

    @field_validator("endpoints")
    @classmethod
    def _endpoints_must_be_local(cls, value: dict[str, str]) -> dict[str, str]:
        try:
            return offline_policy.validate_provider_endpoints(value)
        except offline_policy.OfflinePolicyError as exc:
            raise ValueError(str(exc)) from None


# ---------------------------------------------------------------------------
# Root configuration
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    """Fully validated application configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    config_schema_version: int = CONFIG_SCHEMA_VERSION
    app_name: str = APP_NAME
    app_version: str = APP_VERSION

    runtime_mode: str = "offline"
    offline: bool = True
    log_level: str = "INFO"

    data_root: Path
    model_registry_path: Path

    api: ApiConfig = Field(default_factory=ApiConfig)
    resources: ResourceConfig = Field(default_factory=ResourceConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    participants: ParticipantsConfig = Field(default_factory=ParticipantsConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)

    # -- validators ---------------------------------------------------------

    @field_validator("config_schema_version")
    @classmethod
    def _known_schema_version(cls, value: int) -> int:
        if value != CONFIG_SCHEMA_VERSION:
            raise ValueError(
                f"config_schema_version={value} is not supported by this build "
                f"(expected {CONFIG_SCHEMA_VERSION}). Refusing to guess how to "
                "interpret an unknown configuration schema."
            )
        return value

    @field_validator("runtime_mode")
    @classmethod
    def _supported_runtime_mode(cls, value: str) -> str:
        mode = (value or "").strip().lower()
        if mode not in SUPPORTED_RUNTIME_MODES:
            raise ValueError(
                f"runtime_mode={value!r} is not supported. Allowed: "
                f"{sorted(SUPPORTED_RUNTIME_MODES)}. There is no cloud or hybrid "
                "mode: the application has no cloud fallback by design."
            )
        return mode

    @field_validator("offline")
    @classmethod
    def _offline_must_stay_true(cls, value: bool) -> bool:
        if not value:
            raise ValueError(
                "offline=false is rejected. Offline runtime is an architectural "
                "invariant (ADR-0002), not a toggle."
            )
        return value

    @field_validator("log_level")
    @classmethod
    def _known_log_level(cls, value: str) -> str:
        level = (value or "").strip().upper()
        if level not in LOG_LEVELS:
            raise ValueError(f"log_level={value!r} is not one of {list(LOG_LEVELS)}.")
        return level

    @field_validator("data_root", mode="before")
    @classmethod
    def _validated_data_root(cls, value: Any) -> Path:
        # env={} so that constructing AppConfig directly never silently picks up
        # MOM_IGD_DATA_DIR; precedence is resolved once, in load_config().
        try:
            return resolve_data_root(value, env={})
        except PathValidationError as exc:
            raise ValueError(str(exc)) from None

    @field_validator("model_registry_path", mode="before")
    @classmethod
    def _resolved_registry_path(cls, value: Any) -> Path:
        raw = Path(os.fspath(value)) if value is not None else Path("models/registry.json")
        if not raw.is_absolute():
            raw = repo_root() / raw
        return Path(os.path.normpath(raw))

    # -- derived helpers ----------------------------------------------------

    def runtime_paths(self) -> RuntimePaths:
        """Return the runtime path service for this configuration."""
        return RuntimePaths(root=self.data_root)

    def database_path(self) -> Path:
        return self.runtime_paths().database_path(self.database.filename)

    def summary(self) -> dict[str, Any]:
        """Serialisable, secret-free summary for diagnostics and the shell."""
        return {
            "app_name": self.app_name,
            "app_version": self.app_version,
            "config_schema_version": self.config_schema_version,
            "runtime_mode": self.runtime_mode,
            "offline": self.offline,
            "log_level": self.log_level,
            "data_root": str(self.data_root),
            "model_registry_path": str(self.model_registry_path),
            "api": {
                "host": self.api.host,
                "port": self.api.port,
                "port_strategy": self.api.port_strategy,
                "docs_enabled": self.api.docs_enabled,
            },
            "resources": {
                "max_heavy_workers": self.resources.max_heavy_workers,
                "min_free_ram_mb": self.resources.min_free_ram_mb,
                "min_free_disk_gb": self.resources.min_free_disk_gb,
            },
            "database": {
                "filename": self.database.filename,
                "busy_timeout_ms": self.database.busy_timeout_ms,
            },
            "ui": {"startup_timeout_s": self.ui.startup_timeout_s},
            "audio": {
                "preferred_device_fingerprint": self.audio.preferred_device_fingerprint,
                "preferred_sample_rate": self.audio.preferred_sample_rate,
                "max_channels": self.audio.max_channels,
                "sample_format": self.audio.sample_format,
                "chunk_seconds": self.audio.chunk_seconds,
                "queue_seconds": self.audio.queue_seconds,
                "min_free_disk_gb": self.audio.min_free_disk_gb,
                "low_disk_abort_gb": self.audio.low_disk_abort_gb,
                "calibration_seconds": self.audio.calibration_seconds,
                "status_poll_hz": self.audio.status_poll_hz,
                "auto_recover_on_start": self.audio.auto_recover_on_start,
                "production_requires_usb": self.audio.production_requires_usb,
            },
            "participants": {
                "default_meeting_participant_capacity": (
                    self.participants.default_meeting_participant_capacity
                ),
                "maximum_meeting_participant_capacity": (
                    self.participants.maximum_meeting_participant_capacity
                ),
            },
            "providers": {"endpoints": dict(self.providers.endpoints)},
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def default_config_path() -> Path:
    """Path to the tracked default configuration file."""
    return repo_root() / "config" / "default.toml"


def local_config_path() -> Path:
    """Path to the optional, git-ignored per-machine override file."""
    return repo_root() / "config" / "local.toml"


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"Configuration file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Configuration file {path} is not valid TOML: {exc}") from None


def _env_overrides(env: dict[str, str]) -> dict[str, Any]:
    """Translate ``MOM_IGD_*`` variables into a nested override mapping."""
    overrides: dict[str, Any] = {}

    def _set(path: tuple[str, ...], value: Any) -> None:
        cursor = overrides
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[path[-1]] = value

    simple: dict[str, tuple[str, ...]] = {
        f"{_ENV_PREFIX}LOG_LEVEL": ("log_level",),
        f"{_ENV_PREFIX}RUNTIME_MODE": ("runtime_mode",),
        f"{_ENV_PREFIX}MODEL_REGISTRY": ("model_registry_path",),
        f"{_ENV_PREFIX}API_HOST": ("api", "host"),
        f"{_ENV_PREFIX}API_PORT_STRATEGY": ("api", "port_strategy"),
        f"{_ENV_PREFIX}DB_FILENAME": ("database", "filename"),
    }
    for name, target in simple.items():
        raw = env.get(name)
        if raw is not None and raw.strip():
            _set(target, raw.strip())

    integer: dict[str, tuple[str, ...]] = {
        f"{_ENV_PREFIX}API_PORT": ("api", "port"),
        f"{_ENV_PREFIX}DB_BUSY_TIMEOUT_MS": ("database", "busy_timeout_ms"),
        f"{_ENV_PREFIX}MIN_FREE_RAM_MB": ("resources", "min_free_ram_mb"),
        f"{_ENV_PREFIX}MIN_FREE_DISK_GB": ("resources", "min_free_disk_gb"),
        f"{_ENV_PREFIX}MAX_HEAVY_WORKERS": ("resources", "max_heavy_workers"),
    }
    for name, target in integer.items():
        raw = env.get(name)
        if raw is not None and raw.strip():
            try:
                _set(target, int(raw.strip()))
            except ValueError:
                raise ConfigError(
                    f"Environment variable {name}={raw!r} must be an integer."
                ) from None

    return overrides


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    *,
    data_root: str | os.PathLike[str] | None = None,
    overrides: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    use_local_file: bool = True,
) -> AppConfig:
    """Load, layer and validate the application configuration.

    Args:
        config_path: Defaults file to read; defaults to ``config/default.toml``.
        data_root: Highest-precedence runtime data root (CLI ``--data-dir``).
        overrides: Nested mapping merged last, above environment variables.
        env: Environment mapping; defaults to ``os.environ``.
        use_local_file: Read ``config/local.toml`` when it exists.

    Raises:
        ConfigError: For any validation failure, with a message that names the
            offending setting and why it is rejected.
    """
    environ = dict(os.environ if env is None else env)

    base_path = Path(config_path) if config_path is not None else default_config_path()
    data = _read_toml(base_path)

    if use_local_file:
        local = local_config_path()
        if local.is_file():
            data = _deep_merge(data, _read_toml(local))

    data = _deep_merge(data, _env_overrides(environ))
    if overrides:
        data = _deep_merge(data, overrides)

    # Data-root precedence: explicit argument > env var > config file > default.
    chosen_root: str | os.PathLike[str] | None = data_root
    if chosen_root is None:
        from_env = environ.get(ENV_DATA_DIR)
        if from_env and from_env.strip():
            chosen_root = from_env
    if chosen_root is None:
        from_file = data.get("data_root")
        if isinstance(from_file, str) and from_file.strip():
            chosen_root = from_file
    try:
        data["data_root"] = resolve_data_root(chosen_root, env=environ)
    except PathValidationError as exc:
        raise ConfigError(str(exc)) from None

    data.setdefault("model_registry_path", "models/registry.json")

    try:
        return AppConfig(**data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigError(f"Invalid configuration ({base_path}): {details}") from None
