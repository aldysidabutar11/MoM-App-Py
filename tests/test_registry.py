"""Model registry: the empty registry is valid, and validation is strict.

Covers Phase 1 test category 21.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mom_igd.config import AppConfig
from mom_igd.registry import (
    HARDWARE_PROFILES,
    LOGICAL_PROVIDERS,
    ModelEntry,
    ModelRegistry,
    RegistryError,
    load_registry,
    registry_status,
)
from mom_igd.version import REGISTRY_SCHEMA_VERSION

VALID_SHA = "a" * 64


def _entry(**overrides) -> dict:
    base = {
        "provider": "asr",
        "name": "example",
        "version": "1.0.0",
        "path": "asr/example/model.bin",
        "sha256": VALID_SHA,
        "size_bytes": 1024,
        "license_name": "Apache-2.0",
    }
    base.update(overrides)
    return base


def _write(tmp_path: Path, payload: dict) -> Path:
    target = tmp_path / "registry.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


# ---------------------------------------- 21. the shipped registry is empty


def test_shipped_registry_exists_and_is_valid(config: AppConfig) -> None:
    registry = load_registry(config.model_registry_path)
    assert registry.registry_schema_version == REGISTRY_SCHEMA_VERSION
    assert registry.is_empty
    assert registry.models == []


def test_shipped_registry_declares_no_fake_models(config: AppConfig) -> None:
    raw = json.loads(config.model_registry_path.read_text(encoding="utf-8"))
    assert raw["models"] == [], "Phase 1 must not invent placeholder model entries"
    assert "description" in raw, "the empty state must be explained in the file itself"


def test_empty_registry_status_is_all_zeroes(config: AppConfig, paths) -> None:
    status = registry_status(load_registry(config.model_registry_path), paths.models_dir)
    assert status["total"] == 0
    assert status["empty"] is True
    assert status["provisioned"] == 0
    assert status["offline_ready"] == 0
    assert status["total_declared_bytes"] == 0
    assert status["declared_but_missing_on_disk"] == []
    assert set(status["by_provider"]) == set(LOGICAL_PROVIDERS)


def test_an_empty_registry_object_is_constructible() -> None:
    registry = ModelRegistry()
    assert registry.is_empty
    assert registry.registry_schema_version == REGISTRY_SCHEMA_VERSION


def test_registry_status_is_json_serialisable(config: AppConfig, paths) -> None:
    json.dumps(registry_status(load_registry(config.model_registry_path), paths.models_dir))


# --------------------------------------------------------- file-level errors


def test_missing_registry_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="not found"):
        load_registry(tmp_path / "absent.json")


def test_invalid_json_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "registry.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(RegistryError, match="not valid JSON"):
        load_registry(target)


def test_non_object_json_is_reported(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="must contain a JSON object"):
        load_registry(_write(tmp_path, ["not", "an", "object"]))  # type: ignore[arg-type]


def test_unknown_schema_version_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RegistryError):
        load_registry(_write(tmp_path, {"registry_schema_version": 99, "models": []}))


def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RegistryError):
        load_registry(
            _write(tmp_path, {"registry_schema_version": 1, "models": [], "surprise": 1})
        )


# ------------------------------------------------------- entry validation


def test_a_well_formed_entry_validates(tmp_path: Path) -> None:
    registry = load_registry(
        _write(tmp_path, {"registry_schema_version": 1, "models": [_entry()]})
    )
    assert len(registry.models) == 1
    assert registry.by_provider("asr")[0].name == "example"


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="logical provider"):
        ModelEntry(**_entry(provider="crystal_ball"))


def test_all_declared_providers_are_accepted() -> None:
    for provider in LOGICAL_PROVIDERS:
        assert ModelEntry(**_entry(provider=provider)).provider == provider


def test_unknown_hardware_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="hardware_profile"):
        ModelEntry(**_entry(hardware_profile="tpu-v5"))


def test_there_is_no_cuda_hardware_profile() -> None:
    """The target device has Intel Iris Xe and no NVIDIA GPU."""
    assert not any("cuda" in profile.lower() for profile in HARDWARE_PROFILES)
    with pytest.raises(ValueError, match="no NVIDIA GPU"):
        ModelEntry(**_entry(hardware_profile="cuda-fp16"))


@pytest.mark.parametrize("digest", ["", "abc", "z" * 64, "a" * 63, "a" * 65, "  " + "a" * 62])
def test_malformed_sha256_is_rejected(digest: str) -> None:
    with pytest.raises(ValueError, match="sha256"):
        ModelEntry(**_entry(sha256=digest))


def test_uppercase_sha256_is_normalised_rather_than_rejected() -> None:
    """Some checksum tools emit upper case; normalise instead of failing."""
    entry = ModelEntry(**_entry(sha256="A" * 64))
    assert entry.sha256 == "a" * 64


@pytest.mark.parametrize(
    "path", ["https://huggingface.co/model.bin", "http://example.com/m.gguf", "model.bin"]
)
def test_remote_or_ambiguous_model_path_is_rejected(path: str) -> None:
    with pytest.raises(ValueError):
        ModelEntry(**_entry(path=path))


def test_negative_size_is_rejected() -> None:
    with pytest.raises(ValueError):
        ModelEntry(**_entry(size_bytes=-1))


def test_offline_ready_requires_provisioned() -> None:
    with pytest.raises(ValueError, match="offline_ready but not provisioned"):
        ModelEntry(**_entry(offline_ready=True, provisioned=False))
    assert ModelEntry(**_entry(offline_ready=True, provisioned=True)).offline_ready


def test_duplicate_entries_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="Duplicate"):
        load_registry(
            _write(tmp_path, {"registry_schema_version": 1, "models": [_entry(), _entry()]})
        )


def test_same_name_different_version_is_allowed(tmp_path: Path) -> None:
    registry = load_registry(
        _write(
            tmp_path,
            {
                "registry_schema_version": 1,
                "models": [_entry(version="1.0.0"), _entry(version="2.0.0")],
            },
        )
    )
    assert len(registry.models) == 2


def test_unknown_entry_key_is_rejected() -> None:
    with pytest.raises(ValueError):
        ModelEntry(**_entry(mystery_field="x"))


# ------------------------------------------------------- path resolution


def test_relative_path_resolves_against_the_runtime_models_dir(paths) -> None:
    entry = ModelEntry(**_entry(path="asr/example/model.bin"))
    resolved = entry.resolve_path(paths.models_dir)
    assert resolved == paths.models_dir / "asr" / "example" / "model.bin"
    assert not resolved.is_relative_to(Path(__file__).resolve().parent.parent), (
        "model binaries must resolve outside the repository"
    )


def test_absolute_path_is_left_alone(paths) -> None:
    entry = ModelEntry(**_entry(path=r"D:\MoM-IGD-Data\models\x.gguf"))
    assert entry.resolve_path(paths.models_dir) == Path(r"D:\MoM-IGD-Data\models\x.gguf")


def test_provisioned_but_absent_file_is_surfaced(tmp_path: Path, paths) -> None:
    registry = load_registry(
        _write(
            tmp_path,
            {"registry_schema_version": 1, "models": [_entry(provisioned=True)]},
        )
    )
    status = registry_status(registry, paths.models_dir)
    assert status["declared_but_missing_on_disk"] == ["asr/example@1.0.0"]


def test_status_does_not_read_or_hash_model_files(tmp_path: Path, paths) -> None:
    """A diagnostic must stay fast; hashing gigabytes belongs to verify-models."""
    artefact = paths.models_dir / "asr" / "example" / "model.bin"
    artefact.parent.mkdir(parents=True, exist_ok=True)
    artefact.write_bytes(b"not the declared content")
    registry = load_registry(
        _write(tmp_path, {"registry_schema_version": 1, "models": [_entry(provisioned=True)]})
    )
    status = registry_status(registry, paths.models_dir)
    assert status["declared_but_missing_on_disk"] == []


# -------------------------------------------------------- repository hygiene


def test_no_model_binary_is_committed_in_the_models_directory(config: AppConfig) -> None:
    models_dir = config.model_registry_path.parent
    allowed = {"registry.json", "README.md", ".gitignore"}
    present = {p.name for p in models_dir.iterdir() if p.is_file()}
    assert present <= allowed, f"unexpected files in models/: {sorted(present - allowed)}"


def test_models_directory_ignores_everything_but_the_declaration(config: AppConfig) -> None:
    rules = (config.model_registry_path.parent / ".gitignore").read_text(encoding="utf-8")
    assert rules.strip().splitlines()[-4:] == ["*", "!.gitignore", "!README.md", "!registry.json"]
