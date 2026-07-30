"""The on-disk manifest that makes a provisioned model verifiable.

**Why a manifest and not a single checksum.** A CTranslate2 model is a *directory*:
``model.bin``, ``config.json``, ``tokenizer.json`` and a vocabulary file. One digest
over one file would leave the others unverified, and swapping a tokenizer changes what
the model outputs just as surely as swapping the weights.

So provisioning writes ``model.manifest.json`` next to the artefacts, listing every
file with its size and SHA-256. The registry then records the SHA-256 of *the manifest*,
which gives a two-link chain:

1. the registry digest proves the manifest is the one that was reviewed;
2. each manifest entry proves a file is the one that was downloaded.

Breaking either link is detected before the model is loaded. There is deliberately no
"repair" path: a mismatch means the model is refused, not fixed.

The manifest is also the record of *where the artefact came from* -- repository id,
resolved commit, licence -- so a model on disk can always be traced back to a source
that a human approved.

Standard library only. This module is imported by ``doctor``, which must stay
import-light and must not pull in a model runtime to answer "is the model intact?".
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable

__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA",
    "ManifestError",
    "ModelFile",
    "ModelManifest",
    "manifest_digest",
    "read_manifest",
    "sha256_file",
    "write_manifest",
]

MANIFEST_FILENAME: Final[str] = "model.manifest.json"
MANIFEST_SCHEMA: Final[int] = 1

#: Read in 1 MiB blocks. A CTranslate2 ``model.bin`` is ~1.5 GB; reading it whole to
#: hash it would cost more resident memory than the model itself.
_CHUNK: Final[int] = 1 << 20

#: Files that are documentation or version-control metadata rather than model content.
#: Excluded from the manifest because they are not loaded and their presence or absence
#: must not invalidate a model.
_IGNORED_NAMES: Final[frozenset[str]] = frozenset({".gitattributes", "README.md"})


class ManifestError(RuntimeError):
    """A manifest is missing, malformed, or does not describe what is on disk."""


def sha256_file(path: Path) -> str:
    """SHA-256 of a file, read in bounded blocks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ModelFile:
    """One file inside a model directory."""

    name: str
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "size_bytes": self.size_bytes, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ModelFile:
        try:
            name = str(payload["name"])
            size = int(payload["size_bytes"])
            digest = str(payload["sha256"]).lower()
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError(f"malformed file entry: {payload!r} ({exc})") from None
        if not name or name != Path(name).name:
            # A manifest entry must be a bare filename. `../weights.bin` inside a
            # manifest would let a crafted archive point verification at a file
            # outside the model directory.
            raise ManifestError(
                f"file name {name!r} must be a bare filename with no path separator"
            )
        if size < 0:
            raise ManifestError(f"file {name!r} has a negative size")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ManifestError(f"file {name!r} has a malformed sha256: {digest!r}")
        return cls(name=name, size_bytes=size, sha256=digest)


@dataclass(frozen=True, slots=True)
class ModelManifest:
    """Everything needed to prove a model directory is the one provisioned."""

    schema: int
    provider_slot: str
    model_name: str
    revision: str
    source_repo: str
    source_revision: str
    license_name: str
    license_url: str | None
    hardware_profile: str
    provisioned_at: str
    files: tuple[ModelFile, ...]
    notes: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": self.schema,
            "provider_slot": self.provider_slot,
            "model_name": self.model_name,
            "revision": self.revision,
            "source_repo": self.source_repo,
            "source_revision": self.source_revision,
            "license_name": self.license_name,
            "license_url": self.license_url,
            "hardware_profile": self.hardware_profile,
            "provisioned_at": self.provisioned_at,
            "total_bytes": self.total_bytes,
            "files": [f.to_dict() for f in self.files],
        }
        if self.notes:
            payload["notes"] = self.notes
        if self.extra:
            payload["extra"] = self.extra
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ModelManifest:
        try:
            schema = int(payload["schema"])
        except (KeyError, TypeError, ValueError):
            raise ManifestError("manifest has no usable 'schema'") from None
        if schema != MANIFEST_SCHEMA:
            raise ManifestError(
                f"manifest schema {schema} is not supported by this build "
                f"(expected {MANIFEST_SCHEMA}). Re-provision the model rather than "
                "editing the manifest."
            )
        files = payload.get("files")
        if not isinstance(files, list) or not files:
            raise ManifestError("manifest lists no files")
        required = (
            "provider_slot",
            "model_name",
            "revision",
            "source_repo",
            "source_revision",
            "license_name",
            "hardware_profile",
            "provisioned_at",
        )
        missing = [key for key in required if not payload.get(key)]
        if missing:
            raise ManifestError(f"manifest is missing required keys: {missing}")
        return cls(
            schema=schema,
            provider_slot=str(payload["provider_slot"]),
            model_name=str(payload["model_name"]),
            revision=str(payload["revision"]),
            source_repo=str(payload["source_repo"]),
            source_revision=str(payload["source_revision"]),
            license_name=str(payload["license_name"]),
            license_url=payload.get("license_url"),
            hardware_profile=str(payload["hardware_profile"]),
            provisioned_at=str(payload["provisioned_at"]),
            files=tuple(ModelFile.from_dict(entry) for entry in files),
            notes=payload.get("notes"),
            extra=dict(payload.get("extra") or {}),
        )

    # -- verification -------------------------------------------------------

    def verify(self, directory: Path, *, deep: bool = True) -> list[str]:
        """Return a list of problems. An empty list means the model is intact.

        ``deep=False`` checks presence and size only, which is what a fast readiness
        probe wants; ``deep=True`` hashes every byte, which is what provisioning and
        ``asr verify`` do. Sizes are checked first because a truncated download is the
        common failure and finding it costs no I/O.
        """
        problems: list[str] = []
        for entry in self.files:
            path = directory / entry.name
            if not path.is_file():
                problems.append(f"missing file: {entry.name}")
                continue
            actual_size = path.stat().st_size
            if actual_size != entry.size_bytes:
                problems.append(
                    f"{entry.name}: expected {entry.size_bytes} bytes, found {actual_size}"
                )
                continue
            if deep:
                actual = sha256_file(path)
                if actual != entry.sha256:
                    problems.append(
                        f"{entry.name}: sha256 mismatch "
                        f"(expected {entry.sha256[:16]}..., found {actual[:16]}...)"
                    )
        # An unexpected extra file is reported but is not fatal on its own: the loader
        # only reads what the manifest names. It is still worth surfacing, because it
        # usually means a half-finished re-provision.
        declared = {f.name for f in self.files} | {MANIFEST_FILENAME}
        for child in sorted(directory.iterdir()):
            if child.is_file() and child.name not in declared and child.name not in _IGNORED_NAMES:
                problems.append(f"undeclared file present: {child.name}")
        return problems


def build_manifest(
    directory: Path,
    *,
    provider_slot: str,
    model_name: str,
    revision: str,
    source_repo: str,
    source_revision: str,
    license_name: str,
    license_url: str | None,
    hardware_profile: str,
    provisioned_at: str,
    notes: str | None = None,
    extra: dict[str, Any] | None = None,
    include: Iterable[str] | None = None,
) -> ModelManifest:
    """Hash every model file in ``directory`` and describe it.

    ``include`` restricts the manifest to a known set of names; without it every file
    except documentation and VCS metadata is described.

    The **file list** is deterministic: sorted by name, so the same artefacts always
    produce the same entries in the same order. The manifest as a whole is not
    byte-identical across runs, because ``provisioned_at`` records when the artefacts
    were fetched -- so re-provisioning the same revision yields a different manifest
    digest. That is deliberate: the digest's job is to prove the manifest on disk is
    the one the registry recorded, and re-provisioning is a new event worth recording.
    Whoever re-provisions must therefore refresh the registry digest too, and
    ``asr verify`` reports the mismatch if they do not.
    """
    wanted = set(include) if include is not None else None
    files: list[ModelFile] = []
    for child in sorted(directory.iterdir(), key=lambda p: p.name):
        if not child.is_file():
            continue
        if child.name == MANIFEST_FILENAME or child.name in _IGNORED_NAMES:
            continue
        if wanted is not None and child.name not in wanted:
            continue
        files.append(
            ModelFile(
                name=child.name,
                size_bytes=child.stat().st_size,
                sha256=sha256_file(child),
            )
        )
    if not files:
        raise ManifestError(f"no model files found in {directory}")
    return ModelManifest(
        schema=MANIFEST_SCHEMA,
        provider_slot=provider_slot,
        model_name=model_name,
        revision=revision,
        source_repo=source_repo,
        source_revision=source_revision,
        license_name=license_name,
        license_url=license_url,
        hardware_profile=hardware_profile,
        provisioned_at=provisioned_at,
        files=tuple(files),
        notes=notes,
        extra=dict(extra or {}),
    )


def _canonical_bytes(manifest: ModelManifest) -> bytes:
    """Deterministic serialisation, so the digest depends only on content.

    ``sort_keys`` plus a fixed separator and no trailing newline: the same manifest
    must hash the same on every machine and every Python version.
    """
    return json.dumps(
        manifest.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def manifest_digest(manifest: ModelManifest) -> str:
    """SHA-256 of the canonical manifest. This is what the registry records."""
    return hashlib.sha256(_canonical_bytes(manifest)).hexdigest()


def write_manifest(directory: Path, manifest: ModelManifest) -> tuple[Path, str]:
    """Write the manifest atomically and return its path and digest.

    The digest is computed from the canonical form, not from the pretty-printed bytes
    on disk, so reformatting the file does not change the identity of the model. The
    write is ``.part`` -> ``fsync`` -> ``os.replace`` for the same reason the audio
    writer is: a half-written manifest must never be readable as a whole one.
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / MANIFEST_FILENAME
    partial = directory / f"{MANIFEST_FILENAME}.part"
    payload = json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n"
    with open(partial, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, target)
    return target, manifest_digest(manifest)


def read_manifest(directory: Path) -> ModelManifest:
    """Load and validate the manifest in ``directory``."""
    target = directory / MANIFEST_FILENAME
    if not target.is_file():
        raise ManifestError(
            f"no {MANIFEST_FILENAME} in {directory}. A model directory without a "
            "manifest cannot be verified and will not be loaded. Re-run "
            "`python -m mom_igd asr provision`."
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{target} is not readable JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise ManifestError(f"{target} does not contain a JSON object")
    return ModelManifest.from_dict(payload)


def verify_directory(directory: Path, *, expected_digest: str | None = None,
                     deep: bool = True) -> ModelManifest:
    """Read the manifest, optionally check it against a recorded digest, verify files.

    Raises :class:`ManifestError` on the first failure rather than returning a partial
    verdict: a caller about to load a model needs a yes or a no.
    """
    manifest = read_manifest(directory)
    if expected_digest is not None:
        actual = manifest_digest(manifest)
        if actual != expected_digest.lower():
            raise ManifestError(
                f"manifest digest mismatch for {manifest.model_name}@{manifest.revision}: "
                f"registry records {expected_digest[:16]}..., manifest hashes to "
                f"{actual[:16]}.... The model directory or the registry has been "
                "changed since provisioning; refusing to load it."
            )
    problems = manifest.verify(directory, deep=deep)
    if problems:
        raise ManifestError(
            f"model {manifest.model_name}@{manifest.revision} failed verification: "
            + "; ".join(problems[:6])
            + ("" if len(problems) <= 6 else f" (+{len(problems) - 6} more)")
        )
    return manifest
