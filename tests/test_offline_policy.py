"""Offline policy: dependency audit, endpoint rules, no socket monkey-patching.

Covers Phase 1 test categories 20 and 28.
"""

from __future__ import annotations

import socket

import pytest

from mom_igd import offline_policy
from mom_igd.offline_policy import (
    CLOUD_SDK_DENYLIST,
    CLOUD_SDK_PREFIX_DENYLIST,
    DEFERRED_HEAVY_DISTRIBUTIONS,
    OfflinePolicyError,
    audit_distribution_names,
    audit_installed_distributions,
    installed_distribution_names,
    is_loopback_host,
    normalise_distribution_name,
    offline_env_flags,
    validate_bind_host,
    validate_provider_endpoint,
    worker_environment,
)

# The Phase 1 dependency set, exactly. Anything else in the venv is a finding.
EXPECTED_DIRECT_DEPENDENCIES = {
    "fastapi",
    "uvicorn",
    "psutil",
    "pywebview",
    "pytest",
    "pytest-cov",
    "httpx",
}


# ------------------------------------------- 20. cloud SDK dependency audit


def test_the_installed_environment_has_no_cloud_sdk() -> None:
    audit = audit_installed_distributions()
    assert audit["cloud"] == [], f"cloud SDKs present in the venv: {audit['cloud']}"


def test_the_installed_environment_has_no_ai_or_audio_dependency() -> None:
    audit = audit_installed_distributions()
    assert audit["deferred"] == [], (
        "heavy AI/audio dependencies must not be installed in Phase 1: "
        f"{audit['deferred']}"
    )


def test_the_denylist_catches_the_sdks_this_project_rejects() -> None:
    findings = audit_distribution_names(
        [
            "google-genai",
            "google-generativeai",
            "openai",
            "anthropic",
            "boto3",
            "botocore",
            "azure-ai-ml",
            "azure-storage-blob",
            "google-cloud-storage",
            "google-cloud-speech",
            "cohere",
            "mistralai",
            "replicate",
            "deepgram-sdk",
            "assemblyai",
            "chromadb",
            "qdrant-client",
            "redis",
            "kafka-python",
            "minio",
            "sentry-sdk",
        ]
    )
    assert len(findings["cloud"]) == 21
    assert findings["deferred"] == []


def test_the_legacy_projects_sdk_is_specifically_denied() -> None:
    """The legacy Project APP VTT used google-genai; that architecture is rejected."""
    assert "google-genai" in CLOUD_SDK_DENYLIST
    assert audit_distribution_names(["google-genai"])["cloud"] == ["google-genai"]


def test_deferred_dependencies_are_classified_separately_from_cloud() -> None:
    findings = audit_distribution_names(
        ["faster-whisper", "torch", "openvino", "onnxruntime", "sounddevice", "pyannote.audio"]
    )
    assert findings["cloud"] == []
    assert set(findings["deferred"]) == {
        "faster-whisper",
        "torch",
        "openvino",
        "onnxruntime",
        "sounddevice",
        "pyannote-audio",
    }


def test_prefix_denylist_catches_whole_families() -> None:
    for prefix in CLOUD_SDK_PREFIX_DENYLIST:
        assert audit_distribution_names([f"{prefix}something"])["cloud"]


def test_names_are_normalised_per_pep503() -> None:
    assert normalise_distribution_name("PyAnnote.Audio") == "pyannote-audio"
    assert normalise_distribution_name("Google_GenAI") == "google-genai"
    assert audit_distribution_names(["PyAnnote.Audio"])["deferred"] == ["pyannote-audio"]


def test_denylist_and_deferred_list_do_not_overlap() -> None:
    overlap = CLOUD_SDK_DENYLIST & DEFERRED_HEAVY_DISTRIBUTIONS
    assert overlap == set(), f"a distribution cannot be both: {overlap}"


def test_installed_set_matches_the_declared_phase_1_dependency_intent() -> None:
    """Guard against dependency creep beyond the approved Phase 1 list."""
    installed = set(installed_distribution_names())
    unexpected_top_level = EXPECTED_DIRECT_DEPENDENCIES - installed
    assert unexpected_top_level == set(), f"declared but missing: {unexpected_top_level}"


# --------------------------------------------------- bind host enforcement


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.53", "::1", "localhost"])
def test_loopback_hosts_are_recognised(host: str) -> None:
    assert is_loopback_host(host)
    assert validate_bind_host(host) == host


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "::", "*", "192.168.0.1", "10.1.2.3", "8.8.8.8", "example.com", ""]
)
def test_non_loopback_bind_hosts_are_rejected(host: str) -> None:
    assert not is_loopback_host(host)
    with pytest.raises(OfflinePolicyError):
        validate_bind_host(host)


def test_wildcard_address_gets_a_specific_error_message() -> None:
    with pytest.raises(OfflinePolicyError, match="every network"):
        validate_bind_host("0.0.0.0")


def test_bracketed_ipv6_loopback_is_recognised() -> None:
    assert is_loopback_host("[::1]")


def test_no_dns_resolution_is_attempted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolving a name would itself be a network operation."""

    def _fail(*_args, **_kwargs):
        raise AssertionError("offline policy must never resolve a hostname")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    monkeypatch.setattr(socket, "gethostbyname", _fail)
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("example.com") is False


# ----------------------------------------------- provider endpoint rules


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:8080",
        "https://127.0.0.1:8443/v1",
        "http://localhost:11434/api",
        "http://[::1]:8080",
        r"D:\MoM-IGD-Data\models\llm\model.gguf",
        "/opt/models/model.gguf",
        "llm/qwen/model.gguf",
        "./models/x.onnx",
        "file:///D:/MoM-IGD-Data/models/x.gguf",
        "",
    ],
)
def test_local_and_loopback_endpoints_are_accepted(endpoint: str) -> None:
    assert validate_provider_endpoint("llm", endpoint) == endpoint


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.openai.com/v1",
        "https://api.anthropic.com/v1/messages",
        "https://generativelanguage.googleapis.com/v1beta",
        "https://api.cohere.ai",
        "https://api.deepgram.com",
        "https://huggingface.co/model",
        "https://api.groq.com",
        "https://api.mistral.ai",
    ],
)
def test_cloud_endpoints_are_rejected_with_a_clear_reason(endpoint: str) -> None:
    with pytest.raises(OfflinePolicyError) as excinfo:
        validate_provider_endpoint("llm", endpoint)
    assert "cloud" in str(excinfo.value).lower() or "loopback" in str(excinfo.value).lower()


@pytest.mark.parametrize("endpoint", ["http://192.168.1.10:8080", "http://0.0.0.0:8080", "https://10.0.0.1"])
def test_non_loopback_lan_endpoints_are_rejected(endpoint: str) -> None:
    with pytest.raises(OfflinePolicyError, match="loopback"):
        validate_provider_endpoint("asr", endpoint)


@pytest.mark.parametrize("endpoint", ["ftp://127.0.0.1/x", "ws://127.0.0.1:8080", "redis://127.0.0.1:6379"])
def test_unsupported_schemes_are_rejected(endpoint: str) -> None:
    with pytest.raises(OfflinePolicyError, match="scheme"):
        validate_provider_endpoint("asr", endpoint)


@pytest.mark.parametrize("endpoint", ["api.openai.com", "model.bin", "somehost:8080"])
def test_ambiguous_bare_names_are_rejected_rather_than_guessed(endpoint: str) -> None:
    with pytest.raises(OfflinePolicyError, match="ambiguous"):
        validate_provider_endpoint("asr", endpoint)


def test_validate_many_endpoints_at_once() -> None:
    good = offline_policy.validate_provider_endpoints(
        {"asr": "http://127.0.0.1:9000", "llm": "models/x.gguf"}
    )
    assert good == {"asr": "http://127.0.0.1:9000", "llm": "models/x.gguf"}
    with pytest.raises(OfflinePolicyError):
        offline_policy.validate_provider_endpoints({"llm": "https://api.openai.com"})


# ------------------------------------------------- offline environment flags


def test_offline_env_flags_cover_the_future_model_libraries() -> None:
    flags = offline_env_flags()
    assert flags["HF_HUB_OFFLINE"] == "1"
    assert flags["TRANSFORMERS_OFFLINE"] == "1"
    assert flags["HF_DATASETS_OFFLINE"] == "1"
    assert flags["MOM_IGD_OFFLINE"] == "1"
    assert all(value == "1" for value in flags.values())


def test_offline_env_flags_are_a_copy() -> None:
    first = offline_env_flags()
    first["HF_HUB_OFFLINE"] = "0"
    assert offline_env_flags()["HF_HUB_OFFLINE"] == "1"


def test_worker_environment_layers_flags_over_a_base() -> None:
    env = worker_environment({"PATH": "x", "HF_HUB_OFFLINE": "0"})
    assert env["PATH"] == "x"
    assert env["HF_HUB_OFFLINE"] == "1", "the offline flag must win"


# ------------------------------------ 28. no global socket monkey-patching


def test_the_socket_module_is_not_patched() -> None:
    """A global socket patch is explicitly rejected (ADR-0002).

    It would break loopback IPC, hide real bugs behind import order, and give a
    false sense of enforcement. Offline-ness is enforced by configuration, the
    dependency audit and the absence of any cloud client.
    """
    assert socket.socket.__module__ == "socket"
    assert socket.socket.__qualname__ == "socket"
    assert not hasattr(socket.socket, "_mom_igd_patched")


def test_importing_the_offline_policy_has_no_side_effect_on_the_environment() -> None:
    """A fresh import must not mutate the process environment.

    Checked in a child process rather than with importlib.reload(): reloading
    rebuilds the module's exception classes, so later tests in the same session
    would catch a stale OfflinePolicyError and fail for the wrong reason.
    """
    import subprocess
    import sys

    code = (
        "import os, json;"
        "before = dict(os.environ);"
        "import mom_igd.offline_policy;"
        "print(json.dumps(dict(os.environ) == before))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "true"


def test_no_cloud_client_module_is_importable_in_this_environment() -> None:
    import importlib.util

    for module in ("openai", "anthropic", "google.generativeai", "boto3", "requests"):
        try:
            spec = importlib.util.find_spec(module)
        except ModuleNotFoundError:
            # A missing parent package (e.g. `google`) is itself proof of absence.
            continue
        assert spec is None, f"{module} must not be installed"
