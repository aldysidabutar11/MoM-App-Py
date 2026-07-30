"""Master-key protection through Windows DPAPI.

**Why DPAPI and not a passphrase.** A passphrase would have to be typed before
every enrollment, or stored somewhere -- and "stored somewhere" is the problem it
was meant to solve. DPAPI ties the protected blob to the current Windows user
account, so the key is unwrappable by that operator on that machine and by nobody
else, with no secret for anyone to remember or leak.

**Why ``ctypes`` and not pywin32.** ``CryptProtectData`` and
``CryptUnprotectData`` live in ``crypt32.dll`` and take two flat structures. Adding
a dependency for two calls would grow the offline closure for nothing.

**What DPAPI does not protect against, stated plainly.** Anything running *as that
same user* can call ``CryptUnprotectData`` too. DPAPI defends against another
account and against a stolen copy of the file; it does not defend against malware
already executing as the operator. The mitigation for a stolen disk is full-volume
encryption, which is Phase 11 (see ADR-0010).

**Key creation is never implicit.** :meth:`KeyProtector.load` refuses to create a
key. Only :meth:`create_if_missing`, called from an explicit enrollment, may
generate one -- so importing a module, listing participants or running ``doctor``
can never bring a key into existence. And once encrypted voiceprints exist, a
missing key is a hard failure rather than an invitation to silently mint a new one
that cannot decrypt anything.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import hashlib
import json
import os
import platform
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "KEY_FILENAME",
    "MASTER_KEY_BYTES",
    "FakeKeyProtector",
    "KeyProtectionError",
    "KeyProtector",
    "dpapi_available",
]

MASTER_KEY_BYTES: Final[int] = 32
"""256 bits, because the payload cipher is AES-256-GCM."""

KEY_FILENAME: Final[str] = "voiceprint_master.dpapi"
"""Name of the protected key file inside ``<data_root>/keys``."""

_KEY_ENVELOPE_VERSION: Final[int] = 1
_DPAPI_DESCRIPTION: Final[str] = "MoM-IGD voiceprint master key"
_CRYPTPROTECT_UI_FORBIDDEN: Final[int] = 0x1
"""Never show a UI prompt. A dialog from a background call would hang the app."""

# Entropy mixed into the DPAPI blob. It is not a secret -- it is a domain
# separator, so a blob protected for this purpose cannot be unwrapped by a
# different feature that also happens to call DPAPI as the same user.
_DPAPI_ENTROPY: Final[bytes] = b"mom-igd/voiceprint-master-key/v1"


class KeyProtectionError(RuntimeError):
    """Key material could not be created, protected or recovered.

    Always fail closed: every caller treats this as "no biometric operation is
    possible", never as "carry on without encryption".
    """


class _DataBlob(ctypes.Structure):
    """``DATA_BLOB`` from ``wincrypt.h``."""

    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    """Wrap ``data`` in a DATA_BLOB.

    The buffer is returned alongside the structure and must be kept alive by the
    caller: the structure only holds a pointer, and letting Python collect the
    buffer would leave DPAPI reading freed memory.
    """
    buffer = ctypes.create_string_buffer(data, len(data))
    return (
        _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))),
        buffer,
    )


def _read_and_free(blob: _DataBlob) -> bytes:
    """Copy a DPAPI output blob into Python bytes and release its memory."""
    try:
        if not blob.pbData or blob.cbData == 0:
            raise KeyProtectionError("DPAPI returned an empty blob.")
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        if blob.pbData:
            ctypes.WinDLL("kernel32").LocalFree(blob.pbData)


def dpapi_available() -> tuple[bool, str]:
    """Report whether DPAPI can be used here, without protecting anything.

    Safe for ``doctor``: it loads the library and checks the entry points exist.
    It does not call them, so it cannot create or touch key material.
    """
    if platform.system() != "Windows":
        return False, f"DPAPI is Windows-only; this host reports {platform.system()}."
    try:
        crypt32 = ctypes.WinDLL("crypt32")
        for symbol in ("CryptProtectData", "CryptUnprotectData"):
            if not hasattr(crypt32, symbol):
                return False, f"crypt32.dll does not export {symbol}."
    except OSError as exc:
        return False, f"crypt32.dll could not be loaded: {exc}"
    return True, "crypt32.dll exports CryptProtectData and CryptUnprotectData."


def _protect(plaintext: bytes) -> bytes:
    ok_lib, detail = dpapi_available()
    if not ok_lib:
        raise KeyProtectionError(f"DPAPI is not usable: {detail}")
    crypt32 = ctypes.WinDLL("crypt32")
    data_in, keep_in = _blob(plaintext)
    entropy, keep_entropy = _blob(_DPAPI_ENTROPY)
    data_out = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(data_in),
        _DPAPI_DESCRIPTION,
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(data_out),
    )
    del keep_in, keep_entropy
    if not ok:
        raise KeyProtectionError(
            "CryptProtectData failed with Windows error "
            f"{ctypes.get_last_error() or ctypes.GetLastError()}."
        )
    return _read_and_free(data_out)


def _unprotect(ciphertext: bytes) -> bytes:
    ok_lib, detail = dpapi_available()
    if not ok_lib:
        raise KeyProtectionError(f"DPAPI is not usable: {detail}")
    crypt32 = ctypes.WinDLL("crypt32")
    data_in, keep_in = _blob(ciphertext)
    entropy, keep_entropy = _blob(_DPAPI_ENTROPY)
    data_out = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(data_in),
        None,
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(data_out),
    )
    del keep_in, keep_entropy
    if not ok:
        raise KeyProtectionError(
            "CryptUnprotectData failed. The key file belongs to a different Windows "
            "user or machine, or it is corrupt. Encrypted voiceprints cannot be "
            "read; they must be re-enrolled after consent is confirmed again."
        )
    return _read_and_free(data_out)


@dataclass(frozen=True)
class MasterKey:
    """A master key in memory, with the identifier recorded beside ciphertexts.

    ``key_id`` is a truncated hash of the key, not the key. It exists so an
    envelope can say which key sealed it without revealing anything: a wrong-key
    decrypt then fails with a clear diagnosis instead of an opaque tag error.
    """

    material: bytes
    key_id: str

    def __post_init__(self) -> None:
        if len(self.material) != MASTER_KEY_BYTES:
            raise KeyProtectionError(
                f"Master key must be {MASTER_KEY_BYTES} bytes, got "
                f"{len(self.material)}."
            )

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        return f"<MasterKey key_id={self.key_id} material=<redacted>>"

    __str__ = __repr__

    def __format__(self, _spec: str) -> str:
        return self.__repr__()


def _key_id(material: bytes) -> str:
    """A stable, non-reversible label for a key."""
    return hashlib.sha256(b"mom-igd/key-id/v1" + material).hexdigest()[:16]


class KeyProtector:
    """Creates, protects and recovers the voiceprint master key.

    One instance per data root. Construction touches nothing on disk.
    """

    def __init__(self, keys_dir: Path) -> None:
        self._keys_dir = Path(keys_dir)

    @property
    def key_path(self) -> Path:
        return self._keys_dir / KEY_FILENAME

    def exists(self) -> bool:
        """Whether a protected key file is present. Creates nothing."""
        return self.key_path.is_file()

    def describe(self) -> dict[str, object]:
        """Non-secret facts for ``doctor``. Never unwraps the key.

        Deliberately does not call DPAPI: a diagnostic must not be able to bring
        plaintext key material into the process.
        """
        available, detail = dpapi_available()
        payload: dict[str, object] = {
            "dpapi_available": available,
            "dpapi_detail": detail,
            "key_present": self.exists(),
            "keys_dir_present": self._keys_dir.is_dir(),
        }
        if not self.exists():
            return payload
        try:
            raw = json.loads(self.key_path.read_text(encoding="utf-8"))
            payload["envelope_version"] = raw.get("version")
            payload["key_id"] = raw.get("key_id")
            payload["created_utc"] = raw.get("created_utc")
            payload["protected_bytes"] = len(str(raw.get("protected", "")))
            payload["readable"] = True
        except (OSError, ValueError, TypeError) as exc:
            payload["readable"] = False
            payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload

    def create_if_missing(self, *, created_utc: str) -> MasterKey:
        """Return the master key, generating one only if none exists.

        Called from an explicit enrollment and nowhere else. The generated key is
        random, protected immediately, and written atomically so a crash cannot
        leave a truncated key file that would strand every existing voiceprint.
        """
        if self.exists():
            return self.load()
        self._keys_dir.mkdir(parents=True, exist_ok=True)
        material = secrets.token_bytes(MASTER_KEY_BYTES)
        protected = _protect(material)
        envelope = {
            "version": _KEY_ENVELOPE_VERSION,
            "cipher": "DPAPI-CurrentUser",
            "key_id": _key_id(material),
            "created_utc": created_utc,
            "protected": protected.hex(),
        }
        # Write and fsync through ONE writable handle. Reopening read-only to
        # fsync fails on Windows with EBADF, and skipping the fsync would let a
        # power loss leave a truncated key file -- which would strand every
        # voiceprint sealed under this key.
        temporary = self.key_path.with_suffix(".tmp")
        with temporary.open("wb") as handle:
            handle.write(json.dumps(envelope, indent=2).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.key_path)
        return MasterKey(material=material, key_id=str(envelope["key_id"]))

    def load(self) -> MasterKey:
        """Recover the master key. **Never creates one.**

        A missing key when voiceprints exist is a real failure, not something to
        paper over by minting a replacement: a new key cannot decrypt anything, so
        silently creating one would turn a recoverable diagnosis into permanent,
        invisible data loss.
        """
        if not self.exists():
            raise KeyProtectionError(
                f"No voiceprint master key at {self.key_path}. A key is created "
                "only by an explicit enrollment; it is never generated implicitly. "
                "If encrypted voiceprints already exist and this key is gone, they "
                "cannot be recovered and the participants must be re-enrolled."
            )
        try:
            envelope = json.loads(self.key_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise KeyProtectionError(
                f"Voiceprint master key at {self.key_path} is unreadable "
                f"({type(exc).__name__}). Refusing to continue: creating a "
                "replacement would strand every existing voiceprint."
            ) from None
        if envelope.get("version") != _KEY_ENVELOPE_VERSION:
            raise KeyProtectionError(
                f"Unsupported key envelope version {envelope.get('version')!r}; "
                f"this build understands {_KEY_ENVELOPE_VERSION}."
            )
        try:
            protected = bytes.fromhex(str(envelope["protected"]))
        except (KeyError, ValueError):
            raise KeyProtectionError(
                "Key envelope is missing a well-formed 'protected' field."
            ) from None
        material = _unprotect(protected)
        recomputed = _key_id(material)
        stored = str(envelope.get("key_id", ""))
        if stored and stored != recomputed:
            raise KeyProtectionError(
                "Key envelope integrity check failed: the recovered key does not "
                "match the recorded key_id. The file has been altered."
            )
        return MasterKey(material=material, key_id=recomputed)


class FakeKeyProtector(KeyProtector):
    """Deterministic protector for automated tests.

    **Test-only.** It writes the "protected" key with a fixed XOR mask, which is
    obfuscation and not encryption. That is acceptable precisely because it never
    touches production data: the suite must run on any machine, including one
    where DPAPI is unavailable, and it must be able to assert that a *wrong* key
    fails closed -- which needs a key the test can control.

    It is never reachable from a production code path; the service constructs a
    real :class:`KeyProtector` unless a test injects this one.
    """

    _MASK: Final[bytes] = bytes(range(MASTER_KEY_BYTES))

    def __init__(self, keys_dir: Path, *, material: bytes | None = None) -> None:
        super().__init__(keys_dir)
        self._material = material or bytes(
            (i * 7 + 11) % 256 for i in range(MASTER_KEY_BYTES)
        )

    def create_if_missing(self, *, created_utc: str) -> MasterKey:
        if not self.exists():
            self._keys_dir.mkdir(parents=True, exist_ok=True)
            masked = bytes(a ^ b for a, b in zip(self._material, self._MASK))
            self.key_path.write_text(
                json.dumps(
                    {
                        "version": _KEY_ENVELOPE_VERSION,
                        "cipher": "FAKE-XOR-TEST-ONLY",
                        "key_id": _key_id(self._material),
                        "created_utc": created_utc,
                        "protected": masked.hex(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        return self.load()

    def load(self) -> MasterKey:
        if not self.exists():
            raise KeyProtectionError(
                f"No voiceprint master key at {self.key_path} (fake protector)."
            )
        envelope = json.loads(self.key_path.read_text(encoding="utf-8"))
        masked = bytes.fromhex(str(envelope["protected"]))
        material = bytes(a ^ b for a, b in zip(masked, self._MASK))
        return MasterKey(material=material, key_id=_key_id(material))
