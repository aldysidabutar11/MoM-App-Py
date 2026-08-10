"""Application-level offline policy.

This module is the project's offline guarantee expressed as ordinary,
inspectable application logic:

1. **Dependency policy.** A denylist of cloud SDK distributions that must never
   appear in the project environment, checked against the actually installed
   distributions (see ``tests/test_offline_policy.py``).
2. **Endpoint policy.** Provider endpoints may only be local filesystem paths or
   loopback URLs. Any other host is rejected at configuration load time.
3. **Bind policy.** The API may only bind to a loopback address.
4. **Environment flags.** The environment variables that put future offline
   model libraries into offline mode, provided now so worker processes inherit
   them later. None of those libraries is installed yet.

Deliberate non-goals (ADR-0002):

* **No global ``socket.socket`` monkey-patching.** Patching the socket layer
  process-wide is rejected: it breaks loopback IPC, hides real bugs behind an
  import-order dependency, and gives a false sense of enforcement.
* **No operating-system firewall changes.** Windows Firewall hardening is
  deferred to Phase 11.
* **No deliberate outbound connection to prove a connection is blocked.** The
  policy is asserted by construction and by tests, never by dialling out.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import PurePath
from typing import Final, Iterable
from urllib.parse import urlsplit

__all__ = [
    "CLOUD_SDK_DENYLIST",
    "CLOUD_SDK_PREFIX_DENYLIST",
    "DEFERRED_HEAVY_DISTRIBUTIONS",
    "LOOPBACK_HOSTNAMES",
    "OfflinePolicyError",
    "audit_distribution_names",
    "audit_installed_distributions",
    "installed_distribution_names",
    "is_loopback_host",
    "normalise_distribution_name",
    "offline_env_flags",
    "validate_bind_host",
    "validate_provider_endpoint",
    "worker_environment",
]


class OfflinePolicyError(ValueError):
    """Raised when a value would let the application reach a non-local service."""


# ---------------------------------------------------------------------------
# Dependency policy
# ---------------------------------------------------------------------------

CLOUD_SDK_DENYLIST: Final[frozenset[str]] = frozenset(
    {
        # Google / Gemini  (the legacy Project APP VTT used google-genai; that
        # is precisely the architecture this project rejects)
        "google-genai",
        "google-generativeai",
        "google-api-python-client",
        "google-auth",
        "google-auth-oauthlib",
        "vertexai",
        # OpenAI / Azure OpenAI
        "openai",
        "tiktoken",
        # Anthropic
        "anthropic",
        # AWS
        "boto3",
        "botocore",
        "aiobotocore",
        "s3transfer",
        "awscli",
        # Other hosted inference / hosted ASR providers
        "cohere",
        "mistralai",
        "replicate",
        "together",
        "groq",
        "deepgram-sdk",
        "assemblyai",
        "elevenlabs",
        "revai",
        "speechmatics-python",
        # Agent frameworks that default to hosted models
        "langchain",
        "langchain-community",
        "langchain-openai",
        "llama-index",
        "haystack-ai",
        # Vector databases / brokers excluded by architecture decision
        "chromadb",
        "qdrant-client",
        "pinecone-client",
        "weaviate-client",
        "faiss-cpu",
        "redis",
        "kafka-python",
        "confluent-kafka",
        "minio",
        "celery",
        # Telemetry / crash reporting that phones home
        "sentry-sdk",
        "posthog",
        "segment-analytics-python",
    }
)
"""Distributions that must never be installed in this project."""

CLOUD_SDK_PREFIX_DENYLIST: Final[tuple[str, ...]] = (
    "azure-",
    "google-cloud-",
    "opentelemetry-exporter-otlp",
    "aliyun-",
    "tencentcloud-",
)
"""Distribution name prefixes that are denied wholesale."""

DEFERRED_HEAVY_DISTRIBUTIONS: Final[frozenset[str]] = frozenset(
    {
        # Not forbidden forever -- forbidden until the phase that needs it. Each
        # arrives in its own phase after the Phase 4A benchmark selects a
        # provider. `sounddevice` left this list in Phase 2, when it became a
        # required runtime dependency; `faster-whisper`, `ctranslate2`, `av`,
        # `numpy`, `onnxruntime` and `tokenizers` left it in Phase 4 for the same
        # reason. See the note below the set.
        "openai-whisper",
        "whisper",
        "whispercpp",
        "pywhispercpp",
        "pyannote.audio",
        "pyannote-audio",
        "speechbrain",
        "torch",
        "torchaudio",
        "torchvision",
        # OpenVINO stays deferred: probed on this machine and ruled OUT on
        # evidence -- the Khronos OpenCL\Vendors registry key is absent, so the
        # ICD loader has no vendor to dispatch to, and the Intel compute runtime
        # DLLs are not on PATH. Adopting it would mean installing the toolkit for
        # an unmeasured benefit. See ADR-0014.
        "openvino",
        "openvino-genai",
        "openvino-telemetry",
        "optimum-intel",
        "onnxruntime-openvino",
        "onnxruntime-directml",
        "onnxruntime-gpu",
        "sherpa-onnx",
        "nemo-toolkit",
        # NOTE: `sounddevice` is deliberately NOT listed here. It became a real
        # runtime dependency in Phase 2 (PortAudio/WASAPI capture) and is
        # declared in requirements.txt. Everything below is still deferred:
        # Phase 2 writes WAV with the standard-library `wave` module and needs no
        # third-party audio codec, resampler or VAD.
        "soundfile",
        "pyaudio",
        "librosa",
        "soxr",
        "webrtcvad",
        "silero-vad",
        # NOTE: `llama-cpp-python` is deliberately NOT listed here any more. It
        # became a real runtime dependency when minutes generation landed, and it
        # is declared in requirements.txt. `transformers` and
        # `sentence-transformers` stay deferred: GGUF needs neither, and pulling
        # either in would drag `torch` behind it.
        "sentence-transformers",
        "transformers",
    }
)
"""Heavy AI/audio distributions that must not be present in the current phase.

Graduated out of this set, each in the phase that genuinely needed it:

* `sounddevice` -- Phase 2, PortAudio/WASAPI capture.
* `cryptography` -- Phase 3, AES-256-GCM voiceprint encryption.
* `faster-whisper`, `ctranslate2`, `tokenizers` -- Phase 4, the ASR provider chosen
  by the 4A benchmark (ADR-0014).
* `onnxruntime` -- Phase 4. Required by faster-whisper for the Silero VAD asset that
  ships **inside** the wheel, so the VAD model is local by construction and needs no
  download. Note that this build exposes an `AzureExecutionProvider`; the provider
  boundary pins `CPUExecutionProvider` explicitly and a test asserts it.
* `av` -- Phase 4. FFmpeg bindings, used to decode and resample the master WAV into
  the 16 kHz mono working copy. Reused from the faster-whisper stack rather than
  adding a second audio toolchain.
* `numpy` -- Phase 4. A hard requirement of ctranslate2/faster-whisper. The Phase 2
  capture path and the Phase 3 quality meter still do not use it: capture reads bytes
  from `RawInputStream` and metering uses `array.array`, and that must not change.
* `llama-cpp-python` -- minutes generation. GGUF inference on CPU with
  grammar-constrained sampling. Chosen over the CTranslate2 stack already present
  because converting a model for CTranslate2 requires `transformers` **and** `torch`,
  which are multi-gigabyte and still deferred; GGUF is distributed ready to run. The
  wheel is prebuilt for cp312/win_amd64, so no C++ toolchain is installed. It is not
  on PyPI for this platform and comes from the maintainer's own index -- see
  requirements.txt and ADR-0017. `diskcache` and `jinja2` arrived with it.

Still deferred, and why:

* `openvino*` / `optimum-intel` -- **ruled out on measured evidence**, not deferred by
  default. See the comment inside the set.
* `torch`, `pyannote.audio`, `speechbrain` -- Phase 5 diarization.
* `transformers`, `sentence-transformers` -- still deferred. Minutes generation runs
  GGUF through llama.cpp and needs neither, and either one would pull `torch` in.
* `soundfile`, `librosa`, `soxr`, `pyaudio`, `webrtcvad`, `silero-vad` -- never
  needed: `av` covers decode/resample and the VAD asset is bundled.
"""

NETWORK_CAPABLE_REQUIRES_JUSTIFICATION: Final[frozenset[str]] = frozenset(
    {
        # Legitimate for one-time controlled provisioning (Phase 4A onwards),
        # but must never be imported on a runtime code path. Listed for
        # visibility, not denied.
        "huggingface-hub",
        "requests",
        "httpx",
        "urllib3",
        "aiohttp",
    }
)


def normalise_distribution_name(name: str) -> str:
    """Normalise a distribution name per PEP 503 (lowercase, ``-`` separators)."""
    return "".join("-" if ch in "._" else ch for ch in name.strip().lower())


def audit_distribution_names(names: Iterable[str]) -> dict[str, list[str]]:
    """Classify distribution names against the offline dependency policy.

    Returns a mapping with three keys: ``cloud`` (hard policy violation),
    ``deferred`` (heavy dependency that belongs to a later phase) and
    ``network_capable`` (informational).
    """
    cloud: list[str] = []
    deferred: list[str] = []
    network: list[str] = []

    for raw in names:
        name = normalise_distribution_name(raw)
        if not name:
            continue
        if name in CLOUD_SDK_DENYLIST or name.startswith(CLOUD_SDK_PREFIX_DENYLIST):
            cloud.append(name)
        elif name in DEFERRED_HEAVY_DISTRIBUTIONS:
            deferred.append(name)
        elif name in NETWORK_CAPABLE_REQUIRES_JUSTIFICATION:
            network.append(name)

    return {
        "cloud": sorted(set(cloud)),
        "deferred": sorted(set(deferred)),
        "network_capable": sorted(set(network)),
    }


def installed_distribution_names() -> list[str]:
    """Return normalised names of every distribution installed in this env."""
    from importlib.metadata import distributions

    names: set[str] = set()
    for dist in distributions():
        raw = dist.metadata["Name"] if dist.metadata else None
        if raw:
            names.add(normalise_distribution_name(raw))
    return sorted(names)


def audit_installed_distributions() -> dict[str, list[str]]:
    """Run :func:`audit_distribution_names` against the live environment."""
    return audit_distribution_names(installed_distribution_names())


# ---------------------------------------------------------------------------
# Endpoint and bind policy
# ---------------------------------------------------------------------------

LOOPBACK_HOSTNAMES: Final[frozenset[str]] = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost"}
)
"""Hostnames treated as loopback without a DNS lookup (never resolved)."""

_ALLOWED_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https", "file"})

# Only used to produce a clearer error message; the loopback rule already
# rejects these hosts.
_CLOUD_HOST_MARKERS: Final[tuple[str, ...]] = (
    "openai.com",
    "anthropic.com",
    "googleapis.com",
    "google.com",
    "amazonaws.com",
    "azure.com",
    "azurewebsites.net",
    "cognitiveservices",
    "huggingface.co",
    "replicate.com",
    "deepgram.com",
    "assemblyai.com",
    "elevenlabs.io",
    "groq.com",
    "mistral.ai",
    "cohere.ai",
    "cohere.com",
)


def is_loopback_host(host: str) -> bool:
    """Return ``True`` if ``host`` is a loopback literal or loopback hostname.

    ``0.0.0.0`` and ``::`` are wildcard bind addresses, not loopback, and are
    therefore rejected. No DNS resolution is performed -- resolving a name would
    itself be a network operation.
    """
    if not host:
        return False
    candidate = host.strip().strip("[]").lower()
    if not candidate:
        return False
    if candidate in LOOPBACK_HOSTNAMES:
        return True
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    if address.is_unspecified:  # 0.0.0.0 / ::
        return False
    return address.is_loopback


def validate_bind_host(host: str) -> str:
    """Validate an API bind address; return it unchanged when acceptable.

    Raises:
        OfflinePolicyError: For wildcard, LAN or public bind addresses.
    """
    candidate = (host or "").strip()
    if not candidate:
        raise OfflinePolicyError("API bind host must not be empty.")
    if candidate in {"0.0.0.0", "::", "*"}:
        raise OfflinePolicyError(
            f"API bind host {candidate!r} exposes the backend on every network "
            "interface. Only loopback (127.0.0.1 or ::1) is permitted."
        )
    if not is_loopback_host(candidate):
        raise OfflinePolicyError(
            f"API bind host {candidate!r} is not a loopback address. The backend "
            "must never be reachable from the LAN or the internet; use "
            "127.0.0.1."
        )
    return candidate


def validate_provider_endpoint(name: str, value: str) -> str:
    """Validate one provider endpoint.

    Accepted forms:

    * ``""`` -- unset (the Phase 1 state: no provider selected).
    * ``http://127.0.0.1:8080`` / ``https://localhost:9000`` -- loopback URL.
    * ``file:///D:/MoM-IGD-Data/models/x.gguf`` -- absolute ``file://`` URL.
    * ``D:\\MoM-IGD-Data\\models\\x.gguf`` -- absolute filesystem path.
    * ``models/asr/x.gguf`` -- relative path (resolved against the models dir).

    Everything else is rejected, including bare hostnames, because a value with
    no scheme and no path separator is ambiguous and must not be guessed at.
    """
    raw = (value or "").strip()
    if not raw:
        return ""

    if "://" in raw:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        if scheme not in _ALLOWED_URL_SCHEMES:
            raise OfflinePolicyError(
                f"Provider {name!r} endpoint uses unsupported scheme "
                f"{parts.scheme!r}. Allowed: http/https to loopback, or file://."
            )
        if scheme == "file":
            if not parts.path:
                raise OfflinePolicyError(
                    f"Provider {name!r} file:// endpoint has no path: {raw!r}."
                )
            return raw
        host = parts.hostname or ""
        if any(marker in host.lower() for marker in _CLOUD_HOST_MARKERS):
            raise OfflinePolicyError(
                f"Provider {name!r} endpoint {raw!r} points at the cloud service "
                f"{host!r}. This application has no cloud fallback: providers "
                "must be local files or loopback services."
            )
        if not is_loopback_host(host):
            raise OfflinePolicyError(
                f"Provider {name!r} endpoint {raw!r} resolves to non-loopback "
                f"host {host!r}. Only 127.0.0.1, ::1 and localhost are allowed."
            )
        return raw

    candidate = PurePath(raw)
    has_separator = ("/" in raw) or ("\\" in raw)
    if not candidate.is_absolute() and not has_separator:
        raise OfflinePolicyError(
            f"Provider {name!r} endpoint {raw!r} is neither an absolute path, a "
            "path containing a separator, nor a loopback URL. A bare name is "
            "ambiguous (it could be a remote host) and is therefore rejected."
        )
    return raw


def validate_provider_endpoints(endpoints: dict[str, str]) -> dict[str, str]:
    """Validate every provider endpoint, returning the normalised mapping."""
    return {name: validate_provider_endpoint(name, value) for name, value in endpoints.items()}


# ---------------------------------------------------------------------------
# Offline environment flags
# ---------------------------------------------------------------------------

_OFFLINE_ENV_FLAGS: Final[dict[str, str]] = {
    # Hugging Face stack (installed in a later phase; harmless until then).
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
    # Generic opt-out honoured by many tools.
    "DO_NOT_TRACK": "1",
    # Application marker so a worker can assert it was launched by us.
    "MOM_IGD_OFFLINE": "1",
}


def offline_env_flags() -> dict[str, str]:
    """Return the offline environment flags for future model libraries.

    These have no effect in Phase 1 because none of the target libraries are
    installed. They are defined now so the worker-spawn path is correct from the
    first day a model library appears.
    """
    return dict(_OFFLINE_ENV_FLAGS)


def worker_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Build the environment for a future heavy-worker subprocess."""
    env = dict(os.environ if base is None else base)
    env.update(offline_env_flags())
    return env
