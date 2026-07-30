"""Voiceprint key protection and the AES-256-GCM envelope.

The property under test throughout: an attacker who obtains the runtime data
directory obtains no voice template, and an attacker who can *write* to it cannot
make one participant's template be read as another's.

No test here calls DPAPI. `FakeKeyProtector` is used deliberately -- the suite must
run on any machine, and asserting that a *wrong* key fails closed requires a key
the test controls. DPAPI itself is covered by `dpapi_available()`, which loads the
library without protecting anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mom_igd.enrollment.cipher import (
    ENVELOPE_SCHEMA,
    CipherError,
    ModelIdentity,
    VoiceprintCipher,
    sealed_sha256,
    write_envelope_atomically,
)
from mom_igd.enrollment.keys import (
    MASTER_KEY_BYTES,
    FakeKeyProtector,
    KeyProtectionError,
    KeyProtector,
    dpapi_available,
)

MODEL = ModelIdentity(name="test-embed", version="1.0", sha256="c" * 64)
MODEL_V2 = ModelIdentity(name="test-embed", version="2.0", sha256="d" * 64)
VP_A = "33333333-3333-4333-8333-333333333333"
VP_B = "55555555-5555-4555-8555-555555555555"
PAYLOAD = {"centroid": [0.11, 0.22, 0.33], "dim": 3, "sample_count": 5}


@pytest.fixture
def protector(tmp_path: Path) -> FakeKeyProtector:
    return FakeKeyProtector(tmp_path / "keys")


@pytest.fixture
def cipher(protector: FakeKeyProtector) -> VoiceprintCipher:
    return VoiceprintCipher(protector.create_if_missing(created_utc="2026-07-28T00:00:00Z"))


@pytest.fixture
def envelope(cipher: VoiceprintCipher) -> bytes:
    return cipher.seal(PAYLOAD, voiceprint_uuid=VP_A, participant_id=7, model=MODEL)


# =============================================================== key lifecycle


def test_load_never_creates_a_key(protector: FakeKeyProtector) -> None:
    """The invariant that keeps `doctor` and imports side-effect free."""
    with pytest.raises(KeyProtectionError, match="never generated implicitly|fake protector"):
        protector.load()
    assert not protector.exists()


def test_create_if_missing_is_idempotent(protector: FakeKeyProtector) -> None:
    first = protector.create_if_missing(created_utc="2026-07-28T00:00:00Z")
    second = protector.create_if_missing(created_utc="2026-07-29T00:00:00Z")
    assert first.key_id == second.key_id
    assert first.material == second.material


def test_key_material_is_the_right_length(protector: FakeKeyProtector) -> None:
    key = protector.create_if_missing(created_utc="x")
    assert len(key.material) == MASTER_KEY_BYTES == 32


def test_the_key_file_holds_no_plaintext_material(protector: FakeKeyProtector) -> None:
    key = protector.create_if_missing(created_utc="x")
    raw = protector.key_path.read_text(encoding="utf-8")
    assert key.material.hex() not in raw
    assert key.material not in protector.key_path.read_bytes()


@pytest.mark.parametrize("render", [repr, str, lambda k: f"{k}", lambda k: format(k)])
def test_the_key_never_renders_its_material(protector: FakeKeyProtector, render) -> None:
    """An accidental log line or f-string must not leak the key."""
    key = protector.create_if_missing(created_utc="x")
    text = render(key)
    assert key.material.hex() not in text
    assert "<redacted>" in text
    assert key.key_id in text, "the non-secret label may appear, and is useful"


def test_a_corrupt_key_envelope_fails_closed(protector: FakeKeyProtector) -> None:
    protector.create_if_missing(created_utc="x")
    protector.key_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(Exception):
        protector.load()


def test_a_wrong_envelope_version_is_refused(protector: FakeKeyProtector) -> None:
    protector.create_if_missing(created_utc="x")
    payload = json.loads(protector.key_path.read_text(encoding="utf-8"))
    payload["version"] = 99
    protector.key_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(KeyProtectionError, match="version"):
        KeyProtector(protector.key_path.parent).load()


def test_describe_reports_without_unwrapping(protector: FakeKeyProtector) -> None:
    """`doctor` calls this; it must not bring key material into the process."""
    before = protector.describe()
    assert before["key_present"] is False
    protector.create_if_missing(created_utc="2026-07-28T00:00:00Z")
    after = protector.describe()
    assert after["key_present"] is True
    assert after["readable"] is True
    assert after["key_id"]
    # Whatever it reports, it is not the key.
    assert "material" not in json.dumps(after)
    assert "protected" not in after


def test_the_real_protector_writes_and_reloads_its_key_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cover the real `KeyProtector` write path, not just the fake override.

    `FakeKeyProtector` overrides `create_if_missing` wholesale, so the real
    atomic-write path had no coverage at all -- and it contained a genuine bug
    (reopening the file read-only to `fsync`, which fails on Windows with EBADF).
    DPAPI itself is stubbed here so the test runs anywhere; the stub is reversible
    so the write, fsync, rename and reload are all real.
    """
    from mom_igd.enrollment import keys as keys_module

    monkeypatch.setattr(keys_module, "_protect", lambda data: b"WRAP" + data)
    monkeypatch.setattr(keys_module, "_unprotect", lambda blob: blob[4:])
    monkeypatch.setattr(keys_module, "dpapi_available", lambda: (True, "stubbed"))

    protector = KeyProtector(tmp_path / "keys")
    key = protector.create_if_missing(created_utc="2026-07-28T00:00:00.000Z")
    assert protector.key_path.is_file()
    assert list(protector.key_path.parent.glob("*.tmp")) == [], "temp file left behind"
    assert key.material not in protector.key_path.read_bytes()
    assert protector.load().material == key.material


def test_the_real_protector_detects_a_tampered_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A modified key file must fail closed, never mint a replacement key."""
    from mom_igd.enrollment import keys as keys_module

    monkeypatch.setattr(keys_module, "_protect", lambda data: b"WRAP" + data)

    def _unprotect(blob: bytes) -> bytes:
        if not blob.startswith(b"WRAP"):
            raise KeyProtectionError("stub: blob was altered")
        return blob[4:]

    monkeypatch.setattr(keys_module, "_unprotect", _unprotect)
    monkeypatch.setattr(keys_module, "dpapi_available", lambda: (True, "stubbed"))

    protector = KeyProtector(tmp_path / "keys")
    protector.create_if_missing(created_utc="x")
    payload = json.loads(protector.key_path.read_text(encoding="utf-8"))
    payload["protected"] = ("00" * 16) + str(payload["protected"])[32:]
    protector.key_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(KeyProtectionError):
        protector.load()
    # Crucially, the failure did not replace the key with a usable new one.
    assert protector.exists()


def test_a_key_id_mismatch_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded key_id is cross-checked against the recovered material."""
    from mom_igd.enrollment import keys as keys_module

    monkeypatch.setattr(keys_module, "_protect", lambda data: b"WRAP" + data)
    monkeypatch.setattr(keys_module, "_unprotect", lambda blob: blob[4:])
    monkeypatch.setattr(keys_module, "dpapi_available", lambda: (True, "stubbed"))

    protector = KeyProtector(tmp_path / "keys")
    protector.create_if_missing(created_utc="x")
    payload = json.loads(protector.key_path.read_text(encoding="utf-8"))
    payload["key_id"] = "0" * 16
    protector.key_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(KeyProtectionError, match="integrity check failed"):
        protector.load()


def test_dpapi_availability_is_reported_without_being_used() -> None:
    available, detail = dpapi_available()
    assert isinstance(available, bool)
    assert detail
    import platform

    if platform.system() == "Windows":
        assert available is True, detail
        assert "CryptProtectData" in detail


# ============================================================ confidentiality


def test_the_envelope_contains_no_plaintext_payload(envelope: bytes) -> None:
    text = envelope.decode("utf-8")
    assert "centroid" not in text
    for value in ("0.11", "0.22", "0.33"):
        assert value not in text
    stored = json.loads(text)
    assert set(stored) >= {"schema", "cipher", "nonce", "ciphertext", "model"}
    assert stored["cipher"] == "AES-256-GCM"
    assert stored["schema"] == ENVELOPE_SCHEMA


def test_the_same_payload_seals_to_different_ciphertext(cipher: VoiceprintCipher) -> None:
    """A fixed nonce would leak plaintext relationships and permit forgery."""
    first = json.loads(cipher.seal(PAYLOAD, voiceprint_uuid=VP_A, participant_id=1, model=MODEL))
    second = json.loads(cipher.seal(PAYLOAD, voiceprint_uuid=VP_A, participant_id=1, model=MODEL))
    assert first["nonce"] != second["nonce"]
    assert first["ciphertext"] != second["ciphertext"]


def test_round_trip_preserves_the_payload_exactly(
    cipher: VoiceprintCipher, envelope: bytes
) -> None:
    assert cipher.open(envelope, voiceprint_uuid=VP_A, participant_id=7, model=MODEL) == PAYLOAD


# =============================================================== rejections


def test_a_wrong_participant_is_refused(cipher: VoiceprintCipher, envelope: bytes) -> None:
    """The core threat: one person's template must never be read as another's."""
    with pytest.raises(CipherError, match="different participant"):
        cipher.open(envelope, voiceprint_uuid=VP_A, participant_id=8, model=MODEL)


def test_a_wrong_voiceprint_identity_is_refused(
    cipher: VoiceprintCipher, envelope: bytes
) -> None:
    with pytest.raises(CipherError, match="does not match the record"):
        cipher.open(envelope, voiceprint_uuid=VP_B, participant_id=7, model=MODEL)


def test_an_envelope_cannot_be_moved_between_participants(cipher: VoiceprintCipher) -> None:
    a = cipher.seal({"centroid": [1.0]}, voiceprint_uuid=VP_A, participant_id=1, model=MODEL)
    with pytest.raises(CipherError):
        cipher.open(a, voiceprint_uuid=VP_B, participant_id=2, model=MODEL)


def test_a_changed_model_is_refused(cipher: VoiceprintCipher, envelope: bytes) -> None:
    """Embeddings from different models are not comparable."""
    with pytest.raises(CipherError, match="failed authentication"):
        cipher.open(envelope, voiceprint_uuid=VP_A, participant_id=7, model=MODEL_V2)


def test_tampered_ciphertext_is_refused(cipher: VoiceprintCipher, envelope: bytes) -> None:
    payload = json.loads(envelope)
    raw = bytearray(bytes.fromhex(payload["ciphertext"]))
    raw[0] ^= 0x01
    payload["ciphertext"] = raw.hex()
    with pytest.raises(CipherError, match="failed authentication"):
        cipher.open(
            json.dumps(payload).encode(), voiceprint_uuid=VP_A, participant_id=7, model=MODEL
        )


def test_truncated_ciphertext_is_refused(cipher: VoiceprintCipher, envelope: bytes) -> None:
    payload = json.loads(envelope)
    payload["ciphertext"] = payload["ciphertext"][:-4]
    with pytest.raises(CipherError):
        cipher.open(
            json.dumps(payload).encode(), voiceprint_uuid=VP_A, participant_id=7, model=MODEL
        )


def test_a_truncated_envelope_is_refused(cipher: VoiceprintCipher, envelope: bytes) -> None:
    with pytest.raises(CipherError, match="truncated or corrupt"):
        cipher.open(
            envelope[: len(envelope) // 2],
            voiceprint_uuid=VP_A,
            participant_id=7,
            model=MODEL,
        )


def test_an_unknown_envelope_schema_is_refused(
    cipher: VoiceprintCipher, envelope: bytes
) -> None:
    payload = json.loads(envelope)
    payload["schema"] = 99
    with pytest.raises(CipherError, match="schema"):
        cipher.open(
            json.dumps(payload).encode(), voiceprint_uuid=VP_A, participant_id=7, model=MODEL
        )


def test_a_wrong_master_key_is_refused(tmp_path: Path, envelope: bytes) -> None:
    other = FakeKeyProtector(
        tmp_path / "other-keys", material=bytes((i * 3 + 5) % 256 for i in range(32))
    ).create_if_missing(created_utc="x")
    with pytest.raises(CipherError, match="different master key"):
        VoiceprintCipher(other).open(
            envelope, voiceprint_uuid=VP_A, participant_id=7, model=MODEL
        )


def test_security_does_not_rest_on_the_plaintext_key_id_header(
    tmp_path: Path, envelope: bytes
) -> None:
    """Strip the diagnostic header: the AEAD tag must still reject a wrong key.

    `key_id` exists to turn an opaque tag failure into a clear message. If removing
    it let a wrong key through, the header would be load-bearing security -- and an
    attacker can edit a plaintext field.
    """
    payload = json.loads(envelope)
    payload.pop("key_id")
    stripped = json.dumps(payload).encode()
    other = FakeKeyProtector(
        tmp_path / "other-keys", material=bytes(range(50, 82))
    ).create_if_missing(created_utc="x")
    with pytest.raises(CipherError, match="failed authentication"):
        VoiceprintCipher(other).open(
            stripped, voiceprint_uuid=VP_A, participant_id=7, model=MODEL
        )


# ============================================================ model identity


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "", "version": "1", "sha256": "c" * 64},
        {"name": "m", "version": "", "sha256": "c" * 64},
        {"name": "m", "version": "1", "sha256": "short"},
        {"name": "m", "version": "1", "sha256": "C" * 64},  # upper case
        {"name": "m", "version": "1", "sha256": "z" * 64},  # not hex
    ],
)
def test_model_identity_rejects_unverifiable_provenance(kwargs) -> None:
    """A template without a verifiable model identity cannot be compared safely."""
    with pytest.raises(CipherError):
        ModelIdentity(**kwargs)


# =================================================================== on disk


def test_envelope_writes_atomically_and_leaves_no_temp(
    tmp_path: Path, envelope: bytes
) -> None:
    target = tmp_path / "voiceprints" / f"{VP_A}.vpx"
    write_envelope_atomically(target, envelope)
    assert target.is_file()
    assert target.read_bytes() == envelope
    assert list(target.parent.glob("*.tmp")) == []
    assert sealed_sha256(target.read_bytes()) == sealed_sha256(envelope)


def test_the_filename_is_a_uuid_and_never_a_name(paths) -> None:
    """A display name in a path leaks into backups, pickers and error messages."""
    from mom_igd.paths import PathValidationError

    assert paths.voiceprint_path(VP_A).name == f"{VP_A}.vpx"
    for bad in ("Budi Santoso", "../escape", "a/b", "", "NOT-A-UUID"):
        with pytest.raises(PathValidationError):
            paths.voiceprint_path(bad)
