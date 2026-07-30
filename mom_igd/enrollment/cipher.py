"""AES-256-GCM envelope for voiceprint payloads.

**The threat this addresses.** A voiceprint is biometric data: under Indonesia's
UU PDP No. 27/2022 it is *data pribadi bersifat spesifik*. Someone who obtains the
runtime data directory -- a stolen laptop, a copied backup, a support archive --
must not obtain voice templates. So the templates are sealed, and the key that
opens them is protected separately by DPAPI (:mod:`mom_igd.enrollment.keys`).

**Why AES-256-GCM and not "encoded".** GCM is authenticated: it detects a modified
ciphertext instead of returning plausible-looking garbage. Base64 is not
encryption and must never be presented as such. A raw block cipher without a MAC
would let an attacker flip bits in a template and have it silently accepted.

**Why the AAD is not optional.** Confidentiality alone is not enough here. Without
authenticated additional data, an attacker who can write to the data directory
could copy Budi's envelope over Siti's row: the bytes would decrypt perfectly, and
Phase 6 would then confidently identify Budi's voice as Siti. The AAD binds each
ciphertext to:

* the ``voiceprint_uuid`` it was created for,
* the ``participant_id`` it belongs to,
* the envelope schema version,
* the embedding model's name, version and SHA-256.

Moving an envelope between participants, or reusing it after the model changes,
therefore fails to authenticate rather than succeeding wrongly. The model binding
matters independently: embeddings from different models are not comparable, so a
template that survived a model swap would produce meaningless similarity scores.

**Layout on disk.** A single UTF-8 JSON object, so it can be inspected and version
checked without a decrypt attempt. Only ``ciphertext`` and ``nonce`` are opaque;
every other field is the non-secret context that the AAD covers.

    {"schema": 1, "cipher": "AES-256-GCM", "key_id": "...",
     "voiceprint_uuid": "...", "participant_id": 7,
     "model": {"name": "...", "version": "...", "sha256": "..."},
     "nonce": "<24 hex chars>", "ciphertext": "<hex>"}

**Nonce discipline.** 96 bits from the OS CSPRNG, fresh for every seal. Reusing a
nonce under one key is catastrophic for GCM -- it leaks the XOR of two plaintexts
and permits forgery. Nonces are never derived from a counter that could restart
after a crash or a restore from backup.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from typing import Any, Final

from mom_igd.enrollment.keys import MasterKey

__all__ = [
    "ENVELOPE_SCHEMA",
    "CipherError",
    "ModelIdentity",
    "VoiceprintCipher",
    "sealed_sha256",
]

ENVELOPE_SCHEMA: Final[int] = 1
"""Envelope format version. Bump only with a documented migration path."""

CIPHER_SUITE: Final[str] = "AES-256-GCM"
NONCE_BYTES: Final[int] = 12
"""96 bits -- the size GCM is specified and optimised for."""


class CipherError(RuntimeError):
    """Sealing or opening a voiceprint envelope failed.

    Raised for a wrong key, a wrong participant, a changed model, a tampered
    ciphertext and a truncated envelope alike. The message never contains key
    material, plaintext or a filesystem path.
    """


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    """Which embedding model produced a template.

    Part of the AAD, so a template cannot be silently reinterpreted under a
    different model. ``sha256`` is the hash of the model artefact, which is what
    makes "the same version" verifiable rather than asserted.
    """

    name: str
    version: str
    sha256: str

    def __post_init__(self) -> None:
        for field_name in ("name", "version"):
            value = getattr(self, field_name)
            if not value or not str(value).strip():
                raise CipherError(
                    f"Model {field_name} must be a non-empty string; a voiceprint "
                    "without model provenance cannot be compared safely later."
                )
        if len(self.sha256) != 64 or not all(
            c in "0123456789abcdef" for c in self.sha256
        ):
            raise CipherError(
                "Model sha256 must be 64 lower-case hex characters, got "
                f"{self.sha256!r}. The artefact hash is what makes a model "
                "identity verifiable."
            )

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version, "sha256": self.sha256}


def _canonical_aad(
    *,
    schema: int,
    voiceprint_uuid: str,
    participant_id: int,
    model: ModelIdentity,
) -> bytes:
    """Build the authenticated additional data.

    Canonical by construction: a fixed field order with separators that cannot
    appear in any field. ``json.dumps`` was rejected here -- key ordering and
    whitespace would then be part of the security boundary, and a library upgrade
    that changed either would make every existing envelope unopenable.
    """
    parts = (
        "mom-igd/voiceprint-aad/v1",
        str(schema),
        voiceprint_uuid,
        str(participant_id),
        model.name,
        model.version,
        model.sha256,
    )
    for part in parts:
        if "\x1f" in part:
            raise CipherError(
                "AAD component contains the reserved separator byte 0x1f."
            )
    return "\x1f".join(parts).encode("utf-8")


def sealed_sha256(envelope_bytes: bytes) -> str:
    """Hash of the envelope exactly as stored, for the database mirror."""
    return hashlib.sha256(envelope_bytes).hexdigest()


class VoiceprintCipher:
    """Seals and opens voiceprint payloads under one master key."""

    def __init__(self, key: MasterKey) -> None:
        self._key = key

    @property
    def key_id(self) -> str:
        return self._key.key_id

    def seal(
        self,
        payload: dict[str, Any],
        *,
        voiceprint_uuid: str,
        participant_id: int,
        model: ModelIdentity,
    ) -> bytes:
        """Serialise and encrypt ``payload``; return the envelope bytes.

        ``payload`` carries every biometric component -- centroid, dispersion,
        per-sample vectors. None of it is written anywhere else.
        """
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        try:
            plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CipherError(f"Voiceprint payload is not serialisable: {exc}") from None

        nonce = secrets.token_bytes(NONCE_BYTES)
        aad = _canonical_aad(
            schema=ENVELOPE_SCHEMA,
            voiceprint_uuid=voiceprint_uuid,
            participant_id=participant_id,
            model=model,
        )
        try:
            ciphertext = AESGCM(self._key.material).encrypt(nonce, plaintext, aad)
        finally:
            # Best effort: drop our reference promptly. Python cannot guarantee
            # the buffer is wiped, which is why the real mitigation for memory
            # disclosure is a short-lived worker process, not this line.
            del plaintext

        envelope = {
            "schema": ENVELOPE_SCHEMA,
            "cipher": CIPHER_SUITE,
            "key_id": self._key.key_id,
            "voiceprint_uuid": voiceprint_uuid,
            "participant_id": participant_id,
            "model": model.to_dict(),
            "nonce": nonce.hex(),
            "ciphertext": ciphertext.hex(),
        }
        return json.dumps(envelope, indent=2).encode("utf-8")

    def open(
        self,
        envelope_bytes: bytes,
        *,
        voiceprint_uuid: str,
        participant_id: int,
        model: ModelIdentity,
    ) -> dict[str, Any]:
        """Decrypt and return the payload, or raise.

        The caller states which voiceprint, participant and model it *expects*.
        Those expectations become the AAD, so a mismatch fails to authenticate
        instead of returning someone else's template.
        """
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        try:
            envelope = json.loads(envelope_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CipherError(
                f"Voiceprint envelope is not valid JSON ({type(exc).__name__}); it "
                "is truncated or corrupt."
            ) from None
        if not isinstance(envelope, dict):
            raise CipherError("Voiceprint envelope must be a JSON object.")

        schema = envelope.get("schema")
        if schema != ENVELOPE_SCHEMA:
            raise CipherError(
                f"Unsupported voiceprint envelope schema {schema!r}; this build "
                f"understands {ENVELOPE_SCHEMA}."
            )
        if envelope.get("cipher") != CIPHER_SUITE:
            raise CipherError(
                f"Unsupported cipher suite {envelope.get('cipher')!r}; expected "
                f"{CIPHER_SUITE}."
            )
        stored_key_id = str(envelope.get("key_id", ""))
        if stored_key_id and stored_key_id != self._key.key_id:
            raise CipherError(
                "This voiceprint was sealed with a different master key "
                f"(envelope {stored_key_id}, available {self._key.key_id}). It "
                "cannot be decrypted; the participant must be re-enrolled."
            )
        # Cross-check the plaintext header against what the caller expects, so a
        # swapped envelope is reported precisely rather than as a bare tag error.
        if str(envelope.get("voiceprint_uuid")) != voiceprint_uuid:
            raise CipherError(
                "Voiceprint envelope identity does not match the record that "
                "referenced it. The file has been replaced or moved."
            )
        if int(envelope.get("participant_id", -1)) != int(participant_id):
            raise CipherError(
                "Voiceprint envelope belongs to a different participant. Refusing "
                "to decrypt: using it would attribute one person's voice to "
                "another."
            )

        try:
            nonce = bytes.fromhex(str(envelope["nonce"]))
            ciphertext = bytes.fromhex(str(envelope["ciphertext"]))
        except (KeyError, ValueError):
            raise CipherError(
                "Voiceprint envelope is missing a well-formed nonce or ciphertext."
            ) from None
        if len(nonce) != NONCE_BYTES:
            raise CipherError(
                f"Voiceprint nonce must be {NONCE_BYTES} bytes, got {len(nonce)}."
            )

        aad = _canonical_aad(
            schema=ENVELOPE_SCHEMA,
            voiceprint_uuid=voiceprint_uuid,
            participant_id=participant_id,
            model=model,
        )
        try:
            plaintext = AESGCM(self._key.material).decrypt(nonce, ciphertext, aad)
        except InvalidTag:
            raise CipherError(
                "Voiceprint failed authentication. The ciphertext, the model "
                "identity or the binding to this participant has changed. The "
                "template must be treated as unusable and re-enrolled."
            ) from None
        try:
            payload = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CipherError(
                f"Decrypted voiceprint payload is not valid JSON ({type(exc).__name__})."
            ) from None
        finally:
            del plaintext
        if not isinstance(payload, dict):
            raise CipherError("Decrypted voiceprint payload must be a JSON object.")
        return payload


def write_envelope_atomically(path: Any, envelope_bytes: bytes) -> None:
    """Write an envelope so a crash cannot leave a partial one.

    Same ordering as the Phase 2 chunk writer: write a temporary file, ``fsync``
    it, then rename. A half-written envelope would fail to authenticate, which is
    safe but indistinguishable from tampering -- so it is worth preventing.
    """
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(envelope_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
