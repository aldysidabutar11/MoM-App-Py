"""The installed-model index: what is actually *ready*, as opposed to merely present.

**Why a directory scan is not enough.** Provisioning verifies every byte and then probes
that the model loads and decodes. Both matter, and they are different questions -- the
`preprocessor_config.json` incident proved it: every file hashed correctly, the manifest
digest matched, and the first decode failed with a mel-bin shape error. A resolver that
treats "manifest-valid directory" as "ready" would have handed that model to a real job.

So there are three distinct layers, and they must not be conflated:

1. **Approved catalogue** (``provision.MODEL_CATALOGUE``) -- what this build is *willing*
   to provision. A closed set, reviewed in source.
2. **Installed registry** (this module) -- which model/revision/manifest-digest triples
   are on disk *and* passed a load-and-decode probe. Written only after the probe
   succeeds.
3. **Runtime resolver** (``faster_whisper_provider.resolve_model``) -- consults the
   installed registry and re-verifies the manifest before loading. Nothing else.

The registry is one small JSON file in the model store, written atomically. It records
the manifest digest, so a model directory that is later swapped or edited stops matching
its own readiness record and is refused. A corrupt or unreadable registry is treated as
"nothing is ready" rather than as "everything is ready" -- failing closed is the whole
point.

Standard library only, so ``doctor`` can read it without importing an engine.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from mom_igd.asr.manifest import MANIFEST_FILENAME, ManifestError, manifest_digest, read_manifest

__all__ = [
    "INDEX_FILENAME",
    "INDEX_SCHEMA",
    "InstalledIndex",
    "InstalledModel",
    "QUARANTINE_DIRNAME",
    "load_index",
    "quarantine_directory",
    "record_ready",
    "remove_entry",
]

INDEX_FILENAME: Final[str] = "installed.json"
INDEX_SCHEMA: Final[int] = 1

#: Where a model that verified but could not be used is moved. Kept, not deleted: an
#: operator needs to inspect it, and a silent deletion of 1.5 GB they just downloaded
#: would be hostile.
QUARANTINE_DIRNAME: Final[str] = ".quarantine"


def _escapes_store(relative: str) -> bool:
    """Whether a registry ``relative_path`` could point outside the model store.

    Checked under **both** path flavours, not just the running platform's. On Windows
    ``Path("/abs/model").is_absolute()`` is ``False`` -- there is no drive letter -- so a
    POSIX-style absolute path written into the registry by hand, or by a copy of the file
    from a Linux machine, would sail through a naive check. A test caught exactly that.

    Rejects: any anchor (``/``, ``\\``, ``C:``, a UNC root), any ``..`` component, and
    anything empty.
    """
    from pathlib import PurePosixPath, PureWindowsPath

    text = relative.strip()
    if not text:
        return True
    for flavour in (PurePosixPath, PureWindowsPath):
        candidate = flavour(text)
        if candidate.anchor:
            return True
        if ".." in candidate.parts:
            return True
    return False


@dataclass(frozen=True, slots=True)
class InstalledModel:
    """One model that is on disk *and* proved it can load and decode."""

    provider_slot: str
    model_name: str
    revision: str
    role: str
    manifest_sha256: str
    relative_path: str
    probe_ok: bool
    probed_at: str
    probe_detail: str = ""
    probe_peak_rss_bytes: int = 0

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.provider_slot, self.model_name, self.revision)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_slot": self.provider_slot,
            "model_name": self.model_name,
            "revision": self.revision,
            "role": self.role,
            "manifest_sha256": self.manifest_sha256,
            "relative_path": self.relative_path,
            "probe_ok": self.probe_ok,
            "probed_at": self.probed_at,
            "probe_detail": self.probe_detail,
            "probe_peak_rss_bytes": self.probe_peak_rss_bytes,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> InstalledModel:
        required = (
            "provider_slot",
            "model_name",
            "revision",
            "manifest_sha256",
            "relative_path",
        )
        missing = [key for key in required if not payload.get(key)]
        if missing:
            raise ValueError(f"installed entry missing {missing}")
        relative = str(payload["relative_path"])
        if _escapes_store(relative):
            # A registry entry must stay inside the model store. An absolute path or a
            # traversal would let an edited registry point the loader anywhere.
            raise ValueError(f"relative_path {relative!r} escapes the model store")
        digest = str(payload["manifest_sha256"]).lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"malformed manifest_sha256 {digest!r}")
        return cls(
            provider_slot=str(payload["provider_slot"]),
            model_name=str(payload["model_name"]),
            revision=str(payload["revision"]),
            role=str(payload.get("role") or ""),
            manifest_sha256=digest,
            relative_path=relative,
            probe_ok=bool(payload.get("probe_ok", False)),
            probed_at=str(payload.get("probed_at") or ""),
            probe_detail=str(payload.get("probe_detail") or ""),
            probe_peak_rss_bytes=int(payload.get("probe_peak_rss_bytes") or 0),
        )


@dataclass(frozen=True, slots=True)
class InstalledIndex:
    """The parsed installed-model registry."""

    schema: int = INDEX_SCHEMA
    models: tuple[InstalledModel, ...] = ()
    problem: str | None = None
    #: How the index was obtained, so a caller can tell "no models" from "unreadable".
    source: str = "file"

    @property
    def readable(self) -> bool:
        return self.problem is None

    def ready(self, models_dir: Path, *, role: str | None = None) -> list[InstalledModel]:
        """Entries that are marked ready **and** still match what is on disk.

        Re-checks the manifest digest on every call rather than trusting the record:
        the registry says "this exact model passed", so a directory whose manifest now
        hashes differently is a different model and must not inherit that verdict.
        """
        out: list[InstalledModel] = []
        for entry in self.models:
            if not entry.probe_ok:
                continue
            if role is not None and entry.role != role:
                continue
            directory = models_dir / entry.relative_path
            if not (directory / MANIFEST_FILENAME).is_file():
                continue
            try:
                current = manifest_digest(read_manifest(directory))
            except ManifestError:
                continue
            if current != entry.manifest_sha256:
                continue
            out.append(entry)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "models": [entry.to_dict() for entry in self.models],
        }


def _index_path(models_dir: Path) -> Path:
    return models_dir / INDEX_FILENAME


def load_index(models_dir: Path) -> InstalledIndex:
    """Read the installed registry. **Fails closed** on anything unexpected.

    A missing file means nothing has been provisioned yet, which is a normal state. A
    malformed file means the registry cannot be trusted, and the correct response is to
    report zero ready models with the reason -- never to fall back to scanning
    directories, which would restore exactly the behaviour this module exists to remove.
    """
    target = _index_path(models_dir)
    if not target.is_file():
        return InstalledIndex(models=(), problem=None, source="absent")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return InstalledIndex(
            models=(),
            problem=f"{INDEX_FILENAME} is not readable JSON ({exc}); no model is "
            "treated as ready. Re-run `asr provision` to rebuild it.",
            source="corrupt",
        )
    if not isinstance(payload, dict):
        return InstalledIndex(
            models=(), problem=f"{INDEX_FILENAME} does not contain an object", source="corrupt"
        )
    schema = payload.get("schema")
    if schema != INDEX_SCHEMA:
        return InstalledIndex(
            models=(),
            problem=(
                f"{INDEX_FILENAME} declares schema {schema!r}, this build understands "
                f"{INDEX_SCHEMA}. Refusing to guess."
            ),
            source="corrupt",
        )
    entries: list[InstalledModel] = []
    for raw in payload.get("models") or ():
        if not isinstance(raw, dict):
            return InstalledIndex(
                models=(), problem=f"{INDEX_FILENAME} has a malformed entry", source="corrupt"
            )
        try:
            entries.append(InstalledModel.from_dict(raw))
        except ValueError as exc:
            return InstalledIndex(
                models=(),
                problem=f"{INDEX_FILENAME} has an invalid entry: {exc}",
                source="corrupt",
            )
    return InstalledIndex(models=tuple(entries), problem=None, source="file")


def _write_index(models_dir: Path, index: InstalledIndex) -> None:
    """Write atomically: ``.part`` -> fsync -> ``os.replace``.

    A half-written registry must never be readable as a whole one -- it would be
    parsed as "fewer models are ready", which is safe, or as corrupt, which is also
    safe, but only because the write is atomic and the reader fails closed.
    """
    models_dir.mkdir(parents=True, exist_ok=True)
    target = _index_path(models_dir)
    partial = target.with_name(f"{INDEX_FILENAME}.part")
    payload = json.dumps(index.to_dict(), indent=2, sort_keys=True) + "\n"
    with open(partial, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, target)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def record_ready(
    models_dir: Path,
    *,
    directory: Path,
    role: str,
    probe_detail: str = "",
    probe_peak_rss_bytes: int = 0,
) -> InstalledModel:
    """Mark one model directory as ready. **Call only after a probe has passed.**

    Reads the manifest to derive identity, so the record cannot disagree with the
    artefact it describes. Replaces any previous record for the same
    ``(slot, name, revision)``, which is what makes re-provisioning idempotent.
    """
    manifest = read_manifest(directory)
    try:
        relative = directory.resolve().relative_to(models_dir.resolve())
    except ValueError:
        raise ValueError(
            f"{directory} is not inside the model store {models_dir}; refusing to "
            "record it as installed"
        ) from None

    entry = InstalledModel(
        provider_slot=manifest.provider_slot,
        model_name=manifest.model_name,
        revision=manifest.revision,
        role=str(manifest.extra.get("role") or role),
        manifest_sha256=manifest_digest(manifest),
        relative_path=relative.as_posix(),
        probe_ok=True,
        probed_at=_utc_now(),
        probe_detail=probe_detail[:300],
        probe_peak_rss_bytes=int(probe_peak_rss_bytes),
    )
    index = load_index(models_dir)
    # A corrupt index is rebuilt from this single known-good entry rather than being
    # extended: appending to something unparseable would preserve the corruption.
    kept = () if not index.readable else tuple(
        existing for existing in index.models if existing.key != entry.key
    )
    _write_index(models_dir, InstalledIndex(models=(*kept, entry)))
    return entry


def remove_entry(models_dir: Path, *, model_name: str, revision: str) -> bool:
    """Drop a readiness record. Returns whether anything was removed."""
    index = load_index(models_dir)
    if not index.readable:
        return False
    remaining = tuple(
        entry
        for entry in index.models
        if not (entry.model_name == model_name and entry.revision == revision)
    )
    if len(remaining) == len(index.models):
        return False
    _write_index(models_dir, InstalledIndex(models=remaining))
    return True


def quarantine_directory(models_dir: Path, directory: Path, *, reason: str) -> Path:
    """Move a model that verified but could not be used out of the load path.

    Kept rather than deleted, with the reason written beside it. The load path must not
    contain a model that cannot run, and an operator must still be able to look at what
    went wrong without re-downloading gigabytes.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    target = models_dir / QUARANTINE_DIRNAME / f"{directory.name}.{stamp}"
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(directory, target)
    try:
        (target / "QUARANTINE_REASON.txt").write_text(
            f"Quarantined at {_utc_now()}\n\n{reason}\n",
            encoding="utf-8",
            newline="\n",
        )
    except OSError:
        pass
    return target
