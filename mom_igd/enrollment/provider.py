"""The speaker-embedding boundary.

**No embedding model has been approved or provisioned.** `models/registry.json`
declares zero models, and this module does not pretend otherwise: a production
enrollment fails with :class:`ModelUnavailableError` and a message that says what
is missing. That is deliberate. Substituting a stand-in would produce templates
that look real, get encrypted, get marked eligible, and then fail to identify
anybody -- a silent wrong answer instead of a loud missing dependency.

**Why the boundary is this narrow.** Everything downstream (Phase 6 matching) is
only as trustworthy as the guarantee that two embeddings are comparable. That
guarantee rests on three things this contract makes explicit and verifiable:

* **model identity** -- name, version and the SHA-256 of the artefact, so "same
  model" is checkable rather than assumed;
* **preprocessing identity** -- the same waveform must become the same input
  tensor, or the embeddings differ for reasons that have nothing to do with the
  speaker;
* **output validation** -- a NaN, a zero vector or an unnormalised vector poisons
  every cosine comparison it later takes part in, and it is far cheaper to reject
  one here than to explain a wrong identification later.

**Checksum before load, always.** A model artefact is executable input. The hash
recorded in the registry is verified against the file before the runtime is asked
to open it, and a mismatch refuses to load rather than warning.

**The runtime never downloads anything.** Provisioning is a separate, deliberate,
operator-initiated step; there is no code path here that reaches the network.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol, Sequence, runtime_checkable

__all__ = [
    "EMBEDDING_TOLERANCE",
    "EmbeddingValidationError",
    "ModelUnavailableError",
    "ProviderError",
    "SpeakerEmbeddingProvider",
    "SpeakerModelSpec",
    "cosine_similarity",
    "validate_embedding",
    "verify_artifact_sha256",
]

EMBEDDING_TOLERANCE: Final[float] = 1e-3
"""How far an L2 norm may sit from 1.0 and still count as normalised.

Loose enough for float32 accumulation over a few hundred dimensions, tight enough
that an *unnormalised* vector -- which would silently distort every cosine
comparison -- is still caught.
"""

_MIN_DIM: Final[int] = 16
_MAX_DIM: Final[int] = 4096


class ProviderError(RuntimeError):
    """A provider could not be prepared or used."""


class ModelUnavailableError(ProviderError):
    """No usable embedding model is provisioned.

    Its own type because callers must be able to distinguish "not set up yet" --
    an honest, actionable state -- from "set up and broken".
    """


class EmbeddingValidationError(ProviderError):
    """A provider returned a vector that must not be used."""


@dataclass(frozen=True, slots=True)
class SpeakerModelSpec:
    """Everything needed to identify, verify and use one embedding model.

    ``license`` and ``source`` are carried because a biometric model's provenance
    is part of the compliance story, not decoration: an operator has to be able to
    answer "what is this, where did it come from, and are we allowed to use it?"
    """

    name: str
    version: str
    sha256: str
    embedding_dim: int
    sample_rate_hz: int
    channels: int
    preprocessing_id: str
    license: str
    source: str
    artifact_path: Path | None = None

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.version.strip():
            raise ProviderError("Model name and version must both be non-empty.")
        if len(self.sha256) != 64 or any(
            c not in "0123456789abcdef" for c in self.sha256
        ):
            raise ProviderError(
                f"Model sha256 must be 64 lower-case hex characters, got {self.sha256!r}."
            )
        if not _MIN_DIM <= self.embedding_dim <= _MAX_DIM:
            raise ProviderError(
                f"embedding_dim must be between {_MIN_DIM} and {_MAX_DIM}, got "
                f"{self.embedding_dim}."
            )
        if self.sample_rate_hz <= 0:
            raise ProviderError(
                f"sample_rate_hz must be positive, got {self.sample_rate_hz}."
            )
        if self.channels not in (1, 2):
            raise ProviderError(
                f"channels must be 1 or 2, got {self.channels}. Speaker embedding "
                "operates on mono or stereo capture only."
            )
        if not self.preprocessing_id.strip():
            raise ProviderError(
                "preprocessing_id must be set. Without it, two embeddings cannot be "
                "shown to have been produced the same way."
            )

    def to_dict(self) -> dict[str, Any]:
        """Non-secret description. Safe for the API and for `doctor`."""
        return {
            "name": self.name,
            "version": self.version,
            "sha256": self.sha256,
            "embedding_dim": self.embedding_dim,
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "preprocessing_id": self.preprocessing_id,
            "license": self.license,
            "source": self.source,
        }

    def model_identity(self):
        """Bind this model into the voiceprint envelope's AAD."""
        from mom_igd.enrollment.cipher import ModelIdentity

        return ModelIdentity(name=self.name, version=self.version, sha256=self.sha256)


@runtime_checkable
class SpeakerEmbeddingProvider(Protocol):
    """Turns enrollment audio into a speaker embedding.

    Implementations must be usable as a context manager, because the model has to
    be released when enrollment finishes -- a resident model is exactly what
    ADR-0004 forbids.
    """

    @property
    def spec(self) -> SpeakerModelSpec:
        """Identity, shape and provenance of the loaded model."""

    def embed(self, pcm: bytes, *, sample_rate_hz: int, channels: int) -> list[float]:
        """Return one embedding for ``pcm`` (signed 16-bit little-endian).

        Must be deterministic: the same bytes and the same declared format produce
        the same vector, or intra-enrollment consistency measures noise instead of
        the speaker.
        """

    def close(self) -> None:
        """Release the model. Safe to call more than once."""

    def __enter__(self) -> SpeakerEmbeddingProvider: ...

    def __exit__(self, *exc: object) -> None: ...


def verify_artifact_sha256(path: Path, expected: str, *, chunk: int = 1 << 20) -> str:
    """Hash a model artefact and refuse to proceed on a mismatch.

    Read in chunks so a large artefact does not have to be held in memory, and
    hashed **from disk** -- certifying a buffer would certify what we meant to
    load rather than what is actually there.
    """
    if not path.is_file():
        raise ModelUnavailableError(
            f"Model artefact is missing: {path.name}. Provision it under the runtime "
            "models directory; the runtime never downloads one."
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    actual = digest.hexdigest()
    if actual != expected:
        raise ProviderError(
            f"Model artefact {path.name} has SHA-256 {actual}, but the registry "
            f"declares {expected}. Refusing to load: an unverified model would "
            "produce templates nobody can reproduce, and it is executable input."
        )
    return actual


def validate_embedding(vector: Sequence[float], *, spec: SpeakerModelSpec) -> list[float]:
    """Reject any vector that would poison a later comparison.

    Each check corresponds to a real failure mode:

    * wrong dimension -- a shape mismatch means the wrong model or wrong
      preprocessing ran;
    * NaN / infinity -- propagates through every cosine it touches, and comparisons
      involving NaN silently return false;
    * all zeros -- cosine similarity is undefined (division by zero); usually a
      sign the model received silence;
    * not unit length -- the whole pipeline assumes cosine on normalised vectors.
    """
    values = list(vector)
    if len(values) != spec.embedding_dim:
        raise EmbeddingValidationError(
            f"Embedding has {len(values)} dimensions, but model "
            f"{spec.name} {spec.version} declares {spec.embedding_dim}."
        )
    for index, value in enumerate(values):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise EmbeddingValidationError(
                f"Embedding component {index} is {type(value).__name__}, not a number."
            )
        if math.isnan(value) or math.isinf(value):
            raise EmbeddingValidationError(
                f"Embedding component {index} is {value!r}. A non-finite value "
                "propagates through every similarity computed from it."
            )
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:
        raise EmbeddingValidationError(
            "Embedding is the zero vector, so cosine similarity is undefined. This "
            "usually means the model was handed silence."
        )
    if abs(norm - 1.0) > EMBEDDING_TOLERANCE:
        raise EmbeddingValidationError(
            f"Embedding L2 norm is {norm:.6f}, expected 1.0 +/- "
            f"{EMBEDDING_TOLERANCE}. The pipeline compares normalised vectors, so an "
            "unnormalised one distorts every comparison it takes part in."
        )
    return [float(v) for v in values]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two equal-length vectors.

    Used **only** for intra-enrollment consistency: do five samples from one
    consenting person agree with each other? Comparing across participants is
    speaker identification and belongs to Phase 6.
    """
    if len(a) != len(b):
        raise EmbeddingValidationError(
            f"Cannot compare vectors of different lengths ({len(a)} vs {len(b)})."
        )
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        raise EmbeddingValidationError(
            "Cosine similarity is undefined for a zero vector."
        )
    # Clamp: floating-point accumulation can push a self-comparison a hair past 1.
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


def load_provider_from_registry(config: Any, paths: Any) -> SpeakerEmbeddingProvider:
    """Resolve the configured production provider, or explain what is missing.

    Currently always raises :class:`ModelUnavailableError`, because no speaker
    embedding model has been selected, approved or provisioned. This is the only
    production entry point, and it deliberately has **no** fallback to the test
    provider: a fake template that reached storage would be worse than no template
    at all.
    """
    from mom_igd.registry import load_registry

    try:
        registry = load_registry(config.model_registry_path)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        raise ModelUnavailableError(
            f"The model registry could not be read ({type(exc).__name__}). Speaker "
            "enrollment cannot proceed."
        ) from None

    speaker_models = [
        entry
        for entry in getattr(registry, "models", [])
        if str(getattr(entry, "slot", "")).lower() in {"speaker", "speaker_embedding"}
    ]
    if not speaker_models:
        raise ModelUnavailableError(
            "No speaker-embedding model is declared in models/registry.json, so a "
            "voiceprint cannot be produced. This is the expected state: the model "
            "has not been selected or approved yet -- see "
            "docs/phase-3-speaker-model-selection.md. Enrollment is blocked "
            "deliberately rather than falling back to a stand-in, because a "
            "stand-in template would be encrypted, stored and marked eligible "
            "while identifying nobody."
        )
    raise ModelUnavailableError(
        f"{len(speaker_models)} speaker-embedding model(s) are declared but no "
        "runtime adapter is implemented yet. Implementing one is the first task of "
        "the model-provisioning step; it must verify the artefact SHA-256 before "
        "loading and release the model when enrollment finishes."
    )
