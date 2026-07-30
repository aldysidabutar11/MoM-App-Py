"""Deterministic speaker-embedding provider for automated tests.

**This is not a model and must never be reachable from production.** It computes a
hash-derived vector, so it is perfectly deterministic and useless for recognising
anyone. It exists so the enrollment pipeline -- state machine, quality gates,
encryption, storage, recovery, API and UI -- can be tested end to end on a machine
with no model and no microphone.

**How it is kept out of production.** Three independent barriers, because one would
eventually be bypassed by accident:

1. The only production entry point,
   :func:`mom_igd.enrollment.provider.load_provider_from_registry`, never returns
   this class. It raises :class:`ModelUnavailableError` instead.
2. :attr:`FakeSpeakerEmbeddingProvider.is_test_double` is ``True``, and the
   enrollment service refuses a test double unless it was explicitly injected.
3. Its model name is prefixed ``FAKE-`` and its declared SHA-256 is a constant of
   ``f`` characters, so a stored voiceprint made with it is instantly identifiable
   as non-production -- it cannot masquerade as a real model in the registry, in an
   audit event, or in a voiceprint envelope.

The vector is derived from the audio bytes, so two different recordings produce
different embeddings and two identical recordings produce identical ones. That is
enough to exercise the intra-enrollment consistency check honestly: the
:class:`DriftingFakeProvider` variant deliberately produces inconsistent samples so
the rejection path can be tested too.
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Final

from mom_igd.enrollment.provider import (
    SpeakerEmbeddingProvider,
    SpeakerModelSpec,
    validate_embedding,
)

__all__ = [
    "FAKE_MODEL_NAME",
    "FAKE_MODEL_SHA256",
    "BrokenFakeProvider",
    "DriftingFakeProvider",
    "FakeSpeakerEmbeddingProvider",
    "StableSpeakerFakeProvider",
    "fake_model_spec",
]

FAKE_MODEL_NAME: Final[str] = "FAKE-test-embed"
"""Prefixed so a template built with it can never be mistaken for a real one."""

FAKE_MODEL_SHA256: Final[str] = "f" * 64
"""Obviously synthetic. A real artefact hash is never all-f."""

_DEFAULT_DIM: Final[int] = 64


def fake_model_spec(
    *, dim: int = _DEFAULT_DIM, version: str = "0.0.0-test"
) -> SpeakerModelSpec:
    return SpeakerModelSpec(
        name=FAKE_MODEL_NAME,
        version=version,
        sha256=FAKE_MODEL_SHA256,
        embedding_dim=dim,
        sample_rate_hz=48_000,
        channels=1,
        preprocessing_id="fake-passthrough-v1",
        license="NOT A MODEL -- test double, no license applies",
        source="mom_igd.enrollment.fake_provider",
    )


class FakeSpeakerEmbeddingProvider(SpeakerEmbeddingProvider):
    """Hash-derived, deterministic, and openly labelled as a test double."""

    is_test_double: Final[bool] = True

    def __init__(self, *, spec: SpeakerModelSpec | None = None, salt: bytes = b"") -> None:
        self._spec = spec or fake_model_spec()
        self._salt = salt
        self._closed = False
        self.embed_calls = 0

    @property
    def spec(self) -> SpeakerModelSpec:
        return self._spec

    @property
    def closed(self) -> bool:
        return self._closed

    def _vector(self, seed: bytes) -> list[float]:
        """Expand a seed into a unit vector of the declared dimension."""
        dim = self._spec.embedding_dim
        raw = bytearray()
        counter = 0
        while len(raw) < dim * 4:
            raw += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            counter += 1
        values = [
            # Map each 4-byte word into [-1, 1); the exact mapping is irrelevant,
            # only that it is deterministic and spreads values either side of zero.
            (struct.unpack_from(">I", bytes(raw), 4 * i)[0] / 2**31) - 1.0
            for i in range(dim)
        ]
        norm = math.sqrt(sum(v * v for v in values))
        if norm == 0.0:  # pragma: no cover - astronomically unlikely
            values[0] = 1.0
            norm = 1.0
        return [v / norm for v in values]

    def embed(self, pcm: bytes, *, sample_rate_hz: int, channels: int) -> list[float]:
        if self._closed:
            raise RuntimeError("Provider is closed; embed() must not be called after close().")
        if sample_rate_hz != self._spec.sample_rate_hz:
            raise ValueError(
                f"This provider expects {self._spec.sample_rate_hz} Hz, got "
                f"{sample_rate_hz}. A rate mismatch would change the embedding for "
                "reasons unrelated to the speaker."
            )
        if channels != self._spec.channels:
            raise ValueError(
                f"This provider expects {self._spec.channels} channel(s), got {channels}."
            )
        self.embed_calls += 1
        # Derived from the audio itself, so identical input gives an identical
        # vector and different input gives a different one.
        return validate_embedding(
            self._vector(self._salt + hashlib.sha256(pcm).digest()), spec=self._spec
        )

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> FakeSpeakerEmbeddingProvider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class StableSpeakerFakeProvider(FakeSpeakerEmbeddingProvider):
    """Models the one property a real embedding model has: same voice, similar vector.

    :class:`FakeSpeakerEmbeddingProvider` derives its vector purely from the audio
    bytes, so two genuinely different recordings of the same person produce unrelated
    vectors. That is fine for testing determinism, and useless for testing the
    intra-speaker consistency gate -- which is exactly what an end-to-end enrollment
    exercises. Feeding byte-identical audio to work around it would test nothing.

    So this variant returns a fixed per-speaker direction plus a small
    audio-dependent perturbation. Pairwise cosine lands comfortably above the 0.80
    floor without being exactly 1.0, which is what a real model does for one speaker
    across several samples.

    Still a test double, still labelled ``FAKE-``, still unreachable from production.
    """

    def __init__(
        self,
        *,
        speaker_id: str = "speaker-a",
        variation: float = 0.25,
        spec: SpeakerModelSpec | None = None,
    ) -> None:
        super().__init__(spec=spec)
        if not 0.0 <= variation < 1.0:
            raise ValueError("variation must be in [0, 1).")
        self._speaker_id = speaker_id
        self._variation = variation
        self._base = self._vector(f"stable-base-{speaker_id}".encode())

    def embed(self, pcm: bytes, *, sample_rate_hz: int, channels: int) -> list[float]:
        if self._closed:
            raise RuntimeError("Provider is closed.")
        if sample_rate_hz != self._spec.sample_rate_hz:
            raise ValueError(
                f"This provider expects {self._spec.sample_rate_hz} Hz, got "
                f"{sample_rate_hz}."
            )
        if channels != self._spec.channels:
            raise ValueError(
                f"This provider expects {self._spec.channels} channel(s), got {channels}."
            )
        self.embed_calls += 1
        jitter = self._vector(hashlib.sha256(pcm).digest())
        mixed = [
            (1.0 - self._variation) * b + self._variation * j
            for b, j in zip(self._base, jitter)
        ]
        norm = math.sqrt(sum(v * v for v in mixed))
        return validate_embedding([v / norm for v in mixed], spec=self._spec)


class DriftingFakeProvider(FakeSpeakerEmbeddingProvider):
    """Returns a *different* direction on every call, so samples disagree.

    Used to test the intra-enrollment consistency rejection: without it, the cosine
    floor would never be exercised and a broken threshold would look fine.
    """

    def embed(self, pcm: bytes, *, sample_rate_hz: int, channels: int) -> list[float]:
        self.embed_calls += 1
        if self._closed:
            raise RuntimeError("Provider is closed.")
        return validate_embedding(
            self._vector(f"drift-{self.embed_calls}".encode()), spec=self._spec
        )


class BrokenFakeProvider(FakeSpeakerEmbeddingProvider):
    """Emits a deliberately invalid vector, to prove validation actually runs."""

    def __init__(self, *, mode: str, spec: SpeakerModelSpec | None = None) -> None:
        super().__init__(spec=spec)
        self._mode = mode

    def embed(self, pcm: bytes, *, sample_rate_hz: int, channels: int) -> list[float]:
        self.embed_calls += 1
        dim = self._spec.embedding_dim
        if self._mode == "wrong_dim":
            return [1.0] + [0.0] * (dim - 2)
        if self._mode == "nan":
            return [float("nan")] + [0.0] * (dim - 1)
        if self._mode == "inf":
            return [float("inf")] + [0.0] * (dim - 1)
        if self._mode == "zero":
            return [0.0] * dim
        if self._mode == "unnormalised":
            return [1.0] * dim
        raise AssertionError(f"unknown broken mode {self._mode!r}")
