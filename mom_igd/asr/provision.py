"""Controlled, explicit model provisioning. **The only code that downloads.**

Runtime is offline. This module is the single, deliberate exception, and it is
reachable only from ``python -m mom_igd asr provision`` -- never from an import, never
from ``doctor``, never from the API, never from the shell, and never from the
processing pipeline. A missing model is answered with ``MODEL_UNAVAILABLE``; it is
never quietly fetched, and there is no fallback to a smaller model that happens to be
present.

**Order of operations**, and why it is this order:

1. **Resolve the revision first.** A branch name is not an identity. The provisioner
   resolves ``main`` to a commit sha and pins everything to that, so re-running the
   command later either reproduces the same bytes or tells you the upstream moved.
2. **Download into a staging directory** under the model store, never on top of a
   promoted model. A failed or interrupted download therefore cannot leave a
   half-replaced model that loads.
3. **Verify in staging**: every expected file present, every size right, every
   SHA-256 recomputed from disk.
4. **Write the manifest**, then compute its digest.
5. **Promote atomically** with ``os.replace`` of the directory. On Windows that
   requires the destination not to exist, so an existing model is moved aside to a
   timestamped ``.superseded`` directory first and removed only after the new one is
   in place.
6. **Re-verify after promotion**, from the promoted path. Verifying only in staging
   would leave the actual load path unchecked.

Nothing here writes to the database, and nothing here touches the Phase 2 recordings.

``huggingface_hub`` is imported **inside** the download function. Importing this
module must not pull a network-capable library into the process, so that a test can
assert no runtime path can reach one.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final

from mom_igd.asr.installed import (
    load_index,
    quarantine_directory,
    record_ready,
    remove_entry,
)
from mom_igd.asr.manifest import (
    ManifestError,
    ModelManifest,
    build_manifest,
    manifest_digest,
    read_manifest,
    verify_directory,
    write_manifest,
)
from mom_igd.logging_setup import get_logger

__all__ = [
    "MODEL_CATALOGUE",
    "ModelSpec",
    "ProvisionError",
    "ProvisionResult",
    "catalogue_entry",
    "model_directory",
    "promoted_models",
    "provision_model",
    "verify_model",
]

_LOG = get_logger("asr.provision")

#: Subdirectory of ``<data_root>/models`` that holds ASR/VAD artefacts.
_SLOT_DIRS: Final[dict[str, str]] = {"asr": "asr", "vad": "vad", "llm": "llm"}

_STAGING_DIRNAME: Final[str] = ".staging"
_SUPERSEDED_SUFFIX: Final[str] = ".superseded"


class ProvisionError(RuntimeError):
    """Provisioning was refused or failed. The message names what to do next."""


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """A model this build is willing to provision.

    Deliberately a **closed catalogue**. The command takes a catalogue key, not a
    repository id, so no operator and no script can point provisioning at an arbitrary
    remote repository -- which is how an unreviewed or gated artefact would arrive.
    """

    key: str
    provider_slot: str
    model_name: str
    repo_id: str
    license_name: str
    license_url: str | None
    hardware_profile: str
    expected_files: tuple[str, ...]
    approximate_bytes: int
    role: str
    notes: str
    #: What kind of model this is, and therefore what "it works" means when the
    #: readiness probe runs. Byte verification is necessary and not sufficient
    #: (ADR-0015), and the sufficient test differs: an ASR model has to decode audio,
    #: a language model has to generate a token. One catalogue, two probes.
    kind: str = "asr" 
    #: Files fetched and manifested **when the source has them**, and skipped without
    #: complaint when it does not.
    #:
    #: This exists because of a real failure. ``preprocessor_config.json`` is a few
    #: hundred bytes and declares the mel-bin count; ``large-v3`` needs 128 where the
    #: default is 80. Leaving it out of the required list let a model provision and
    #: verify byte-for-byte, then fail at the first decode with
    #: ``expected (1, 128, 3000), got (1, 80, 3000)``. Small models do not ship one at
    #: all, so it cannot simply be required either.
    optional_files: tuple[str, ...] = ()

    @property
    def approximate_mib(self) -> float:
        return self.approximate_bytes / (1 << 20)


#: The models the Phase 4A benchmark evaluated and Phase 4 may load.
#:
#: Both are CTranslate2 conversions of OpenAI Whisper, MIT licensed, public and
#: ungated -- verified with the hub API before they were added here. Neither needs an
#: access token, and nothing in this project ever supplies one.
#:
#: The Silero VAD model is deliberately **absent**: it ships inside the
#: ``faster-whisper`` wheel as ``assets/silero_vad_v6.onnx``, so it is already local
#: and needs no provisioning at all.
MODEL_CATALOGUE: Final[dict[str, ModelSpec]] = {
    "asr-pass1": ModelSpec(
        key="asr-pass1",
        provider_slot="asr",
        model_name="faster-whisper-small",
        repo_id="Systran/faster-whisper-small",
        license_name="MIT",
        license_url="https://opensource.org/license/mit",
        hardware_profile="cpu-int8",
        expected_files=("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"),
        optional_files=("preprocessor_config.json",),
        approximate_bytes=487_000_000,
        role="pass1",
        notes=(
            "First-pass transcription over every VAD speech region. Chosen for "
            "throughput: it is what keeps the whole-meeting real-time factor inside "
            "budget on a CPU-only machine."
        ),
    ),
    "asr-pass2": ModelSpec(
        key="asr-pass2",
        provider_slot="asr",
        model_name="faster-whisper-large-v3-turbo",
        repo_id="deepdml/faster-whisper-large-v3-turbo-ct2",
        license_name="MIT",
        license_url="https://opensource.org/license/mit",
        hardware_profile="cpu-int8",
        expected_files=("config.json", "model.bin", "tokenizer.json", "vocabulary.json"),
        optional_files=("preprocessor_config.json",),
        approximate_bytes=1_622_000_000,
        role="pass2",
        notes=(
            "Selective second pass over flagged regions only, under a duration "
            "budget. Never runs over a whole meeting: at this size that would blow "
            "the real-time factor."
        ),
    ),
    "mom-llm": ModelSpec(
        key="mom-llm",
        provider_slot="llm",
        model_name="qwen3-4b-instruct",
        repo_id="Qwen/Qwen3-4B-GGUF",
        license_name="Apache-2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0",
        hardware_profile="cpu-q4_k_m",
        expected_files=("Qwen3-4B-Q4_K_M.gguf",),
        approximate_bytes=2_500_000_000,
        role="mom",
        kind="llm",
        notes=(
            "Minutes extraction. Chosen at 4B and Q4_K_M because the measured free "
            "memory on the target machine is about 5 GB and the one-heavy-worker "
            "policy means it never shares that with an ASR model. Apache-2.0 and "
            "first-party: Llama and Gemma licences carry use restrictions this "
            "deployment cannot accept. It writes no prose of its own into a minute "
            "without a quote from the transcript behind it."
        ),
    ),
}


def catalogue_entry(key: str) -> ModelSpec:
    try:
        return MODEL_CATALOGUE[key]
    except KeyError:
        raise ProvisionError(
            f"unknown model key {key!r}. Available: {sorted(MODEL_CATALOGUE)}. "
            "Provisioning takes a catalogue key, not a repository id, so an "
            "unreviewed artefact cannot be introduced by a command-line argument."
        ) from None


def model_directory(models_dir: Path, spec: ModelSpec, revision: str) -> Path:
    """Where a promoted model lives: ``<models>/<slot>/<name>/<revision>``."""
    slot = _SLOT_DIRS.get(spec.provider_slot, spec.provider_slot)
    return models_dir / slot / spec.model_name / revision


def _staging_directory(models_dir: Path, spec: ModelSpec, revision: str) -> Path:
    slot = _SLOT_DIRS.get(spec.provider_slot, spec.provider_slot)
    return models_dir / _STAGING_DIRNAME / slot / spec.model_name / revision


@dataclass(frozen=True, slots=True)
class ProvisionResult:
    """Outcome of one provisioning run. Safe to print: no secrets, no paths outside
    the model store."""

    spec: ModelSpec
    revision: str
    directory: Path
    manifest: ModelManifest
    manifest_digest: str
    total_bytes: int
    already_present: bool

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.spec.key,
            "provider_slot": self.spec.provider_slot,
            "model_name": self.spec.model_name,
            "revision": self.revision,
            "source_repo": self.spec.repo_id,
            "license": self.spec.license_name,
            "hardware_profile": self.spec.hardware_profile,
            "role": self.spec.role,
            "files": len(self.manifest.files),
            "total_bytes": self.total_bytes,
            "manifest_sha256": self.manifest_digest,
            "already_present": self.already_present,
            "directory": str(self.directory),
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _resolve_revision(repo_id: str) -> tuple[str, dict[str, int]]:
    """Resolve the default branch to a commit sha, and get each file's size.

    Network access happens here and in :func:`_download`, and nowhere else in the
    project. The import is local so that merely importing this module does not put a
    network-capable library into the interpreter.
    """
    from huggingface_hub import HfApi

    api = HfApi()
    try:
        info = api.model_info(repo_id, files_metadata=True)
    except Exception as exc:  # noqa: BLE001 - the hub raises a wide family
        raise ProvisionError(
            f"could not read metadata for {repo_id}: {type(exc).__name__}. "
            "Provisioning needs network access; the runtime does not."
        ) from None
    if getattr(info, "gated", False):
        raise ProvisionError(
            f"{repo_id} is gated. This project does not use access tokens or accept "
            "click-through licences during provisioning; choose an ungated artefact."
        )
    if getattr(info, "private", False):
        raise ProvisionError(f"{repo_id} is private and cannot be provisioned here.")
    sha = getattr(info, "sha", None)
    if not sha:
        raise ProvisionError(f"{repo_id} returned no commit sha to pin to")
    sizes = {
        s.rfilename: int(s.size or 0)
        for s in (info.siblings or [])
        if s.rfilename
    }
    return str(sha), sizes


def _download(repo_id: str, revision: str, filenames: tuple[str, ...], target: Path) -> None:
    """Fetch exactly the named files at exactly the pinned revision."""
    from huggingface_hub import hf_hub_download

    target.mkdir(parents=True, exist_ok=True)
    for name in filenames:
        _LOG.info("provision.download", extra={"repo": repo_id, "file": name})
        try:
            fetched = hf_hub_download(
                repo_id=repo_id,
                filename=name,
                revision=revision,
                local_dir=str(target),
                # No symlinks: a promoted model must be self-contained bytes, not a
                # pointer into a cache that another command could clear.
                local_dir_use_symlinks=False,
            )
        except TypeError:
            # Newer huggingface-hub removed `local_dir_use_symlinks`; it also stopped
            # creating symlinks, so the guarantee holds without the argument.
            fetched = hf_hub_download(
                repo_id=repo_id, filename=name, revision=revision, local_dir=str(target)
            )
        except Exception as exc:  # noqa: BLE001
            raise ProvisionError(
                f"download of {name} from {repo_id}@{revision[:12]} failed: "
                f"{type(exc).__name__}"
            ) from None
        fetched_path = Path(fetched)
        if fetched_path.resolve() != (target / name).resolve():
            # Guard against a filename that resolves outside the staging directory.
            raise ProvisionError(
                f"{name} was written to an unexpected location; refusing to promote"
            )


def _strip_download_bookkeeping(directory: Path) -> None:
    """Remove downloader scratch directories from a staged model.

    Only directories are removed, and only ones the downloader is known to create.
    A model file is never touched: they are all flat files inside ``directory``.
    """
    for name in (".cache", ".huggingface", ".locks"):
        candidate = directory / name
        if candidate.is_dir():
            shutil.rmtree(candidate, ignore_errors=True)


def _prune_empty_parents(leaf: Path, stop: Path) -> None:
    """Remove empty directories from ``leaf`` up to but not including ``stop``.

    Keeps the model store tidy after a promotion: the staging tree is created per
    model and per revision, and leaving the skeleton behind makes it look as though a
    download is still in progress.
    """
    current = leaf
    while current != stop and stop in current.parents:
        try:
            next(current.iterdir())
        except StopIteration:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent
        except (FileNotFoundError, NotADirectoryError):
            current = current.parent
        else:
            return


def _promote(staging: Path, final: Path) -> None:
    """Move a verified staging directory into place, atomically enough for Windows.

    ``os.replace`` on a directory fails on Windows when the destination exists, so an
    existing model is moved aside first and deleted only once the new one is in place.
    If the promotion fails after the move-aside, the old model is restored -- a failed
    re-provision must not leave the operator with no model at all.
    """
    final.parent.mkdir(parents=True, exist_ok=True)
    aside: Path | None = None
    if final.exists():
        aside = final.with_name(
            f"{final.name}{_SUPERSEDED_SUFFIX}."
            + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        )
        os.replace(final, aside)
    try:
        os.replace(staging, final)
    except OSError:
        if aside is not None and not final.exists():
            os.replace(aside, final)  # put the working model back
        raise
    if aside is not None:
        shutil.rmtree(aside, ignore_errors=True)


def provision_model(
    key: str,
    models_dir: Path,
    *,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> ProvisionResult:
    """Provision one catalogue model into ``models_dir``. Downloads if needed.

    Idempotent: an already-promoted model that verifies is left exactly as it is and
    reported as ``already_present``, so re-running the command is cheap and safe.
    ``force=True`` re-downloads and re-promotes.
    """
    spec = catalogue_entry(key)
    say = progress or (lambda _message: None)

    say(f"resolving {spec.repo_id}")
    revision, remote_sizes = _resolve_revision(spec.repo_id)
    say(f"pinned to revision {revision[:12]}")

    final = model_directory(models_dir, spec, revision)
    if final.exists() and not force:
        try:
            manifest = verify_directory(final, deep=True)
        except ManifestError as exc:
            raise ProvisionError(
                f"{spec.model_name}@{revision[:12]} is already present but does not "
                f"verify: {exc}. Re-run with --force to replace it."
            ) from None
        # Verified bytes are not readiness. If the index has no ready record for this
        # exact manifest digest -- because a previous run was interrupted between
        # promotion and the probe, or because the registry was rebuilt -- probe now and
        # record it, rather than reporting a model as ready on the strength of a
        # directory listing.
        digest = manifest_digest(manifest)
        already_ready = any(
            entry.manifest_sha256 == digest
            for entry in load_index(models_dir).ready(models_dir, role=spec.role)
        )
        if not already_ready:
            say("present but not recorded as ready; probing")
            probe = _probe_promoted_model(final, spec)
            if not probe["ok"]:
                raise ProvisionError(
                    f"{spec.model_name}@{revision[:12]} is present and verifies, but "
                    f"failed its load-and-decode probe: {probe['detail']}. Re-run with "
                    "--force to replace it."
                )
            record_ready(
                models_dir,
                directory=final,
                role=spec.role,
                probe_detail=str(probe.get("detail") or ""),
                probe_peak_rss_bytes=int(probe.get("peak_rss_bytes") or 0),
            )
            say("probe passed; recorded in the installed-model registry")
        say("already present and verified")
        return ProvisionResult(
            spec=spec,
            revision=revision,
            directory=final,
            manifest=manifest,
            manifest_digest=manifest_digest(manifest),
            total_bytes=manifest.total_bytes,
            already_present=True,
        )

    missing = [name for name in spec.expected_files if name not in remote_sizes]
    if missing:
        raise ProvisionError(
            f"{spec.repo_id}@{revision[:12]} does not contain the expected files "
            f"{missing}. The upstream layout changed; the catalogue entry needs "
            "review rather than a looser check."
        )
    present_optional = tuple(
        name for name in spec.optional_files if name in remote_sizes
    )
    wanted = spec.expected_files + present_optional
    expected_total = sum(remote_sizes[name] for name in wanted)
    say(f"downloading {len(wanted)} file(s), ~{expected_total / 2**20:.0f} MiB")
    if present_optional:
        say(f"including optional: {', '.join(present_optional)}")

    staging = _staging_directory(models_dir, spec, revision)
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    try:
        _download(spec.repo_id, revision, wanted, staging)

        say("verifying staged files")
        for name in wanted:
            path = staging / name
            if not path.is_file():
                raise ProvisionError(f"{name} is missing after download")
            actual = path.stat().st_size
            if actual != remote_sizes[name]:
                raise ProvisionError(
                    f"{name} is {actual} bytes but the source declares "
                    f"{remote_sizes[name]}. Refusing to promote a truncated artefact."
                )

        # The downloader leaves its own bookkeeping (`.cache/huggingface/...`) beside
        # the artefacts. Strip it before the manifest is built so a promoted model
        # directory contains exactly the verified model files plus the manifest --
        # which is what makes the "undeclared file present" check meaningful, and what
        # keeps a provisioned model free of anything that could point back at a cache.
        _strip_download_bookkeeping(staging)

        manifest = build_manifest(
            staging,
            provider_slot=spec.provider_slot,
            model_name=spec.model_name,
            revision=revision,
            source_repo=spec.repo_id,
            source_revision=revision,
            license_name=spec.license_name,
            license_url=spec.license_url,
            hardware_profile=spec.hardware_profile,
            provisioned_at=_utc_now(),
            notes=spec.notes,
            extra={"role": spec.role, "catalogue_key": spec.key},
            include=wanted,
        )
        write_manifest(staging, manifest)
        # Verify from staging before promoting: a bad artefact must never occupy the
        # load path even briefly.
        verify_directory(staging, expected_digest=manifest_digest(manifest), deep=True)

        say("promoting")
        _promote(staging, final)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        _prune_empty_parents(staging.parent, models_dir / _STAGING_DIRNAME)
        root_staging = models_dir / _STAGING_DIRNAME
        if root_staging.is_dir():
            _prune_empty_parents(root_staging, models_dir)

    # Verify again from the promoted path. Staging and the load path are different
    # directories, and only the second one matters at runtime.
    promoted = verify_directory(final, deep=True)
    digest = manifest_digest(promoted)
    say("verified after promotion")

    # Byte-verification is necessary but not sufficient. A model whose files all hash
    # correctly can still be unloadable or undecodable -- that is exactly what happened
    # when `preprocessor_config.json` was omitted: every byte verified and the first
    # decode then failed with a mel-bin shape error. So provisioning proves the model
    # can be constructed and can encode one frame, in an isolated worker, before it is
    # reported as ready.
    say("probing that the model actually loads and encodes")
    probe = _probe_promoted_model(final, spec)
    if not probe["ok"]:
        # Quarantine, do not leave it in the load path. A model that verifies but cannot
        # run is worse than a missing one: it looks ready to a directory scan. Moving it
        # aside means `resolve_model` reports MODEL_UNAVAILABLE, which is the truth.
        reason = (
            f"{spec.model_name}@{revision} verified byte-for-byte but failed its "
            f"load-and-decode probe: {probe['detail']}"
        )
        try:
            quarantined = quarantine_directory(models_dir, final, reason=reason)
            where = f" It has been quarantined at {quarantined.name} for inspection."
        except OSError as exc:  # pragma: no cover - needs a locked directory
            where = f" It could not be quarantined ({type(exc).__name__}); remove it by hand."
        remove_entry(models_dir, model_name=spec.model_name, revision=revision)
        raise ProvisionError(
            f"{spec.model_name}@{revision[:12]} verified byte-for-byte but could not "
            f"be used: {probe['detail']}. The artefact is intact, so this is a "
            "configuration or compatibility problem -- most often a missing "
            f"preprocessor/feature-extractor file.{where}"
        )
    say(f"load-and-decode probe passed in {probe['seconds']:.2f}s")

    # Only now is the model recorded as installed and ready. This record -- not the
    # presence of a directory -- is what the runtime resolver consults.
    record_ready(
        models_dir,
        directory=final,
        role=spec.role,
        probe_detail=str(probe.get("detail") or ""),
        probe_peak_rss_bytes=int(probe.get("peak_rss_bytes") or 0),
    )
    say("recorded in the installed-model registry")
    _LOG.info(
        "provision.complete",
        extra={
            "model": spec.model_name,
            "revision": revision[:12],
            "files": len(promoted.files),
            "bytes": promoted.total_bytes,
        },
    )
    return ProvisionResult(
        spec=spec,
        revision=revision,
        directory=final,
        manifest=promoted,
        manifest_digest=digest,
        total_bytes=promoted.total_bytes,
        already_present=False,
    )


def _probe_promoted_llm(directory: Path, spec: ModelSpec) -> dict[str, Any]:
    """Load the GGUF in an isolated worker and generate a few tokens.

    A GGUF that hashes correctly can still be unloadable: a quantisation the installed
    llama.cpp build does not know, a truncated tensor, an architecture newer than the
    runtime. The only way to find out is to load it, so provisioning does that here
    rather than leaving the operator to discover it during a meeting.

    Tiny on purpose -- a handful of tokens with a two-token context is enough to prove
    the weights load and the sampler runs.
    """
    import time

    from mom_igd.asr.worker import WorkerError, run_in_worker

    started = time.perf_counter()
    try:
        outcome = run_in_worker(
            "probe_llm",
            {"directory": str(directory), "filename": spec.expected_files[0]},
            timeout_seconds=900,
        )
    except WorkerError as exc:
        return {"ok": False, "detail": str(exc), "seconds": time.perf_counter() - started}
    if not outcome.ok:
        return {
            "ok": False,
            "detail": outcome.error or "the language model could not be loaded",
            "seconds": time.perf_counter() - started,
        }
    payload = outcome.payload or {}
    return {
        "ok": True,
        "seconds": time.perf_counter() - started,
        "detail": (
            f"loaded in {payload.get('load_seconds', 0):.1f}s and generated "
            f"{payload.get('tokens', 0)} token(s); context {payload.get('n_ctx', 0)}"
        ),
        "peak_rss_bytes": outcome.peak_rss_bytes,
    }


def _probe_promoted_model(directory: Path, spec: ModelSpec) -> dict[str, Any]:
    """Load the promoted model in an isolated worker and make it do its actual job.

    Runs in a spawned child so a crash inside the native engine cannot take the
    provisioning command down with it, and so the memory is returned when it exits.
    Deliberately tiny: the question is "can this model run at all", not "how well".

    What counts as running differs by kind, which is the whole reason `ModelSpec.kind`
    exists. Byte verification is necessary and not sufficient (ADR-0015) -- and the
    sufficient test for an ASR model is a decode, while for a language model it is
    generating a token.
    """
    if spec.kind == "llm":
        return _probe_promoted_llm(directory, spec)

    import tempfile
    import time

    from mom_igd.asr.smoke import generate_speech_like_wav
    from mom_igd.asr.worker import WorkerError, run_in_worker

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="momigd-probe-") as scratch:
        audio = Path(scratch) / "probe.wav"
        generate_speech_like_wav(audio, 2.0)
        try:
            outcome = run_in_worker(
                # `probe_directory`, not `transcribe`: the probe addresses the promoted
                # directory it just created. Going through the readiness-gated resolver
                # would be circular -- it would require the record this probe exists to
                # earn.
                "probe_directory",
                {
                    "directory": str(directory),
                    "audio_path": str(audio),
                    "cpu_threads": 4,
                    "language": "id",
                },
                timeout_seconds=600,
            )
        except WorkerError as exc:
            return {"ok": False, "detail": str(exc), "seconds": time.perf_counter() - started}
    if not outcome.ok:
        return {
            "ok": False,
            "detail": outcome.error or "worker returned no result",
            "seconds": time.perf_counter() - started,
        }
    return {
        "ok": True,
        "detail": (
            f"decoded {outcome.payload.get('audio_seconds')}s, "
            f"peak RSS {outcome.peak_rss_bytes / (1 << 20):.0f} MiB"
        ),
        "seconds": time.perf_counter() - started,
        "peak_rss_bytes": outcome.peak_rss_bytes,
    }


def _models_root_for(directory: Path, spec: ModelSpec) -> Path:
    """Recover the ``<data_root>/models`` root from a promoted model directory.

    The layout is ``<models>/<slot>/<name>/<revision>``, so the root is three levels
    up. Derived rather than passed in, so the probe cannot be pointed somewhere else.
    """
    return directory.parent.parent.parent


def promoted_models(models_dir: Path) -> list[dict[str, Any]]:
    """Every promoted model on disk, newest revision first, with its state.

    Read-only and network-free: this is what ``doctor`` and the readiness probe use.
    A directory that fails to verify is reported as such rather than omitted, because
    "no model" and "a corrupt model" need different actions from an operator.
    """
    found: list[dict[str, Any]] = []
    if not models_dir.is_dir():
        return found
    for slot in sorted(_SLOT_DIRS.values()):
        slot_dir = models_dir / slot
        if not slot_dir.is_dir():
            continue
        for name_dir in sorted(p for p in slot_dir.iterdir() if p.is_dir()):
            for revision_dir in sorted(
                (p for p in name_dir.iterdir() if p.is_dir()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            ):
                if revision_dir.name.count(_SUPERSEDED_SUFFIX):
                    continue
                entry: dict[str, Any] = {
                    "provider_slot": slot,
                    "model_name": name_dir.name,
                    "revision": revision_dir.name,
                    "directory": str(revision_dir),
                }
                try:
                    manifest = read_manifest(revision_dir)
                except ManifestError as exc:
                    entry.update(ok=False, problem=str(exc), role=None)
                    found.append(entry)
                    continue
                problems = manifest.verify(revision_dir, deep=False)
                entry.update(
                    ok=not problems,
                    problem="; ".join(problems[:3]) or None,
                    role=manifest.extra.get("role"),
                    license=manifest.license_name,
                    hardware_profile=manifest.hardware_profile,
                    total_bytes=manifest.total_bytes,
                    manifest_sha256=manifest_digest(manifest),
                    source_repo=manifest.source_repo,
                    provisioned_at=manifest.provisioned_at,
                )
                found.append(entry)
    return found


def verify_model(directory: Path, *, expected_digest: str | None = None) -> ModelManifest:
    """Deep-verify one promoted model directory. Raises on any problem."""
    return verify_directory(Path(directory), expected_digest=expected_digest, deep=True)
