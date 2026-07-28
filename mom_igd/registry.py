"""Model registry: a versioned *declaration* of the models the app may load.

The registry file (``models/registry.json``) is tracked in Git because it is a
reviewable declaration: what model, which version, which SHA-256, which licence,
which hardware profile. The model **binaries** it describes live under
``<data_root>/models`` and are never committed.

Phase 1 ships an **empty** registry. That is the correct state: no ASR,
diarization, speaker-embedding or LLM provider has been selected yet -- the
choice is deferred to the Phase 4A benchmark (ADR-0005). An empty registry is
valid and produces a doctor *warning*, never a failure.

Nothing in this module downloads anything. Provisioning is a separate, explicit,
one-time online step that arrives with the phase that needs it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from mom_igd import offline_policy
from mom_igd.version import REGISTRY_SCHEMA_VERSION

__all__ = [
    "HARDWARE_PROFILES",
    "LOGICAL_PROVIDERS",
    "ModelEntry",
    "ModelRegistry",
    "RegistryError",
    "load_registry",
    "registry_status",
]

LOGICAL_PROVIDERS: Final[tuple[str, ...]] = (
    "vad",
    "asr",
    "diarization",
    "speaker_embedding",
    "llm",
    "text_embedding",
)
"""Logical provider slots. A slot may hold several entries (e.g. two ASR passes)."""

HARDWARE_PROFILES: Final[tuple[str, ...]] = (
    "cpu-fp32",
    "cpu-int8",
    "cpu-q4",
    "cpu-q5",
    "openvino-cpu",
    "openvino-gpu",
    "directml-gpu",
)
"""Execution profiles a model artefact is built for.

Note the absence of any CUDA profile: the target device has Intel Iris Xe
integrated graphics and no NVIDIA GPU, so a CUDA artefact could never run.
"""

_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class RegistryError(ValueError):
    """Raised when the registry file is missing, malformed or inconsistent."""


class ModelEntry(BaseModel):
    """One declared model artefact."""

    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())

    provider: str = Field(description="Logical provider slot, e.g. 'asr'.")
    name: str = Field(min_length=1, description="Model name, e.g. 'whisper-small'.")
    version: str = Field(min_length=1, description="Model version or revision.")
    path: str = Field(
        min_length=1,
        description="Absolute path, or a path relative to <data_root>/models.",
    )
    sha256: str = Field(
        description="Hex SHA-256 of the artefact; normalised to lower case on load "
        "so a digest pasted from a tool that emits upper case still validates."
    )
    size_bytes: int = Field(ge=0, description="Expected size in bytes.")
    license_name: str = Field(min_length=1, description="SPDX id or licence name.")
    license_url: str | None = None
    license_requires_acceptance: bool = False
    provisioned: bool = False
    offline_ready: bool = False
    hardware_profile: str = "cpu-int8"
    source_url: str | None = Field(
        default=None,
        description="Where the artefact came from during controlled provisioning. "
        "Recorded for auditability; never fetched at runtime.",
    )
    phase_introduced: str | None = None
    notes: str | None = None

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        slot = value.strip().lower()
        if slot not in LOGICAL_PROVIDERS:
            raise ValueError(
                f"provider={value!r} is not a known logical provider. "
                f"Allowed: {list(LOGICAL_PROVIDERS)}."
            )
        return slot

    @field_validator("hardware_profile")
    @classmethod
    def _known_profile(cls, value: str) -> str:
        profile = value.strip().lower()
        if profile not in HARDWARE_PROFILES:
            raise ValueError(
                f"hardware_profile={value!r} is not supported. "
                f"Allowed: {list(HARDWARE_PROFILES)}. There is no CUDA profile: "
                "the target device has no NVIDIA GPU."
            )
        return profile

    @field_validator("sha256")
    @classmethod
    def _valid_sha256(cls, value: str) -> str:
        digest = value.strip().lower()
        if not _SHA256_RE.match(digest):
            raise ValueError(
                f"sha256={value!r} is not a 64-character lower-case hex digest. "
                "Every model artefact must be checksummed."
            )
        return digest

    @field_validator("path")
    @classmethod
    def _local_path_only(cls, value: str) -> str:
        # Reuse the offline endpoint policy: a model path must never be a URL to
        # a remote host.
        try:
            return offline_policy.validate_provider_endpoint("model.path", value)
        except offline_policy.OfflinePolicyError as exc:
            raise ValueError(str(exc)) from None

    @model_validator(mode="after")
    def _offline_ready_implies_provisioned(self) -> ModelEntry:
        if self.offline_ready and not self.provisioned:
            raise ValueError(
                f"Model {self.provider}/{self.name}@{self.version} is marked "
                "offline_ready but not provisioned. A model cannot be ready for "
                "offline use before its artefact exists locally."
            )
        return self

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.provider, self.name, self.version)

    def resolve_path(self, models_dir: Path) -> Path:
        """Resolve ``path`` against the runtime models directory when relative."""
        candidate = Path(self.path)
        if candidate.is_absolute():
            return candidate
        return models_dir / candidate


class ModelRegistry(BaseModel):
    """The registry document."""

    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())

    registry_schema_version: Literal[1] = REGISTRY_SCHEMA_VERSION  # type: ignore[assignment]
    description: str | None = None
    models: list[ModelEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_keys(self) -> ModelRegistry:
        seen: set[tuple[str, str, str]] = set()
        for entry in self.models:
            if entry.key in seen:
                raise ValueError(
                    f"Duplicate registry entry for {entry.provider}/{entry.name}"
                    f"@{entry.version}."
                )
            seen.add(entry.key)
        return self

    @property
    def is_empty(self) -> bool:
        return not self.models

    def by_provider(self, provider: str) -> list[ModelEntry]:
        return [entry for entry in self.models if entry.provider == provider]


def load_registry(path: str | Path) -> ModelRegistry:
    """Read and validate the registry file.

    Raises:
        RegistryError: If the file is missing, not valid JSON, or fails schema
            validation.
    """
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RegistryError(
            f"Model registry not found: {target}. Phase 1 ships an empty registry; "
            "it must exist even when it declares no models."
        ) from None
    except OSError as exc:
        raise RegistryError(f"Model registry {target} could not be read: {exc}") from None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"Model registry {target} is not valid JSON: {exc}") from None

    if not isinstance(payload, dict):
        raise RegistryError(
            f"Model registry {target} must contain a JSON object, got "
            f"{type(payload).__name__}."
        )

    try:
        return ModelRegistry(**payload)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise RegistryError(f"Model registry {target} is invalid: {details}") from None


def registry_status(
    registry: ModelRegistry, models_dir: Path | None = None
) -> dict[str, Any]:
    """Summarise the registry for diagnostics.

    When ``models_dir`` is given, also reports which declared artefacts are
    actually present on disk. Nothing is downloaded and no hash is recomputed
    here: verifying multi-gigabyte artefacts belongs to an explicit
    ``verify-models`` step, not to a diagnostic that must stay fast.
    """
    missing_files: list[str] = []
    if models_dir is not None:
        for entry in registry.models:
            if entry.provisioned and not entry.resolve_path(models_dir).exists():
                missing_files.append(f"{entry.provider}/{entry.name}@{entry.version}")

    return {
        "registry_schema_version": registry.registry_schema_version,
        "total": len(registry.models),
        "empty": registry.is_empty,
        "provisioned": sum(1 for entry in registry.models if entry.provisioned),
        "offline_ready": sum(1 for entry in registry.models if entry.offline_ready),
        "by_provider": {
            provider: len(registry.by_provider(provider)) for provider in LOGICAL_PROVIDERS
        },
        "declared_but_missing_on_disk": missing_files,
        "total_declared_bytes": sum(entry.size_bytes for entry in registry.models),
    }
