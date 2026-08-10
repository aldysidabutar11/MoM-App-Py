"""Model provisioning, the manifest hash chain, and the fail-closed guarantee.

The properties asserted here are the ones that would let an unverified or unexpected
model reach a transcript. None of them needs a real model: the manifest layer works on
any directory, so these tests build tiny fake model directories and exercise the real
verification code against them.

The one thing these tests deliberately do **not** do is download anything. Network
access belongs to an explicit operator command, and a test suite that reached out would
make the offline guarantee unverifiable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mom_igd.asr.manifest import (
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA,
    ManifestError,
    ModelFile,
    build_manifest,
    manifest_digest,
    read_manifest,
    sha256_file,
    verify_directory,
    write_manifest,
)
from mom_igd.asr.provision import (
    MODEL_CATALOGUE,
    ProvisionError,
    catalogue_entry,
    model_directory,
    promoted_models,
    verify_model,
)


def _write_model(directory: Path, *, files: dict[str, bytes] | None = None) -> Path:
    """A minimal but structurally real CTranslate2-shaped model directory."""
    directory.mkdir(parents=True, exist_ok=True)
    payload = files or {
        "config.json": b'{"alignment_heads": [[0, 0]]}',
        "model.bin": b"\x00\x01\x02\x03" * 64,
        "tokenizer.json": b'{"version": "1.0"}',
        "vocabulary.txt": b"a\nb\nc\n",
    }
    for name, content in payload.items():
        (directory / name).write_bytes(content)
    return directory


def _manifest_for(directory: Path, **overrides) -> tuple[Path, str]:
    manifest = build_manifest(
        directory,
        provider_slot=overrides.get("provider_slot", "asr"),
        model_name=overrides.get("model_name", "faster-whisper-small"),
        revision=overrides.get("revision", "a" * 40),
        source_repo=overrides.get("source_repo", "Systran/faster-whisper-small"),
        source_revision=overrides.get("source_revision", "a" * 40),
        license_name=overrides.get("license_name", "MIT"),
        license_url=None,
        hardware_profile=overrides.get("hardware_profile", "cpu-int8"),
        provisioned_at="2026-07-30T00:00:00.000Z",
        extra=overrides.get("extra", {"role": "pass1"}),
    )
    return write_manifest(directory, manifest)


# ===========================================================================
# The catalogue is closed
# ===========================================================================


def test_the_catalogue_is_a_closed_set_of_reviewed_models() -> None:
    """Provisioning takes a key, not a repository id.

    If an arbitrary repo id were accepted, an unreviewed or gated artefact could be
    introduced by a command-line argument and would then be loaded with full trust.
    """
    assert set(MODEL_CATALOGUE) == {"asr-pass1", "asr-pass2", "mom-llm"}
    for spec in MODEL_CATALOGUE.values():
        assert spec.license_name, spec.key
        assert spec.repo_id.count("/") == 1, spec.repo_id
        assert spec.hardware_profile.startswith("cpu"), (
            "the target device has no usable GPU path; see ADR-0014"
        )
        assert spec.expected_files, spec.key
        # The weights file, named exactly. An unpinned list would accept whatever the
        # repository happens to hold. CTranslate2 ships `model.bin`; a GGUF is one file
        # whose name carries its quantisation, so the check is per engine rather than a
        # single filename that only one of them uses.
        if spec.kind == "llm":
            assert any(f.endswith(".gguf") for f in spec.expected_files), spec.key
        else:
            assert "model.bin" in spec.expected_files, spec.key


@pytest.mark.parametrize("bad", ["", "asr", "whisper", "Systran/faster-whisper-small", "../x"])
def test_an_unknown_catalogue_key_is_refused(bad: str) -> None:
    with pytest.raises(ProvisionError, match="unknown model key"):
        catalogue_entry(bad)


def test_both_roles_are_declared_exactly_once() -> None:
    roles = [spec.role for spec in MODEL_CATALOGUE.values()]
    assert sorted(roles) == ["mom", "pass1", "pass2"]


def test_the_vad_model_is_not_in_the_catalogue_because_it_ships_in_the_wheel() -> None:
    """Nothing to provision means nothing to get wrong."""
    assert not any(spec.provider_slot == "vad" for spec in MODEL_CATALOGUE.values())
    import faster_whisper

    assets = sorted((Path(faster_whisper.__file__).parent / "assets").glob("*.onnx"))
    assert assets, "the bundled Silero VAD asset is missing from the installed wheel"


def test_the_pass2_catalogue_entry_declares_the_preprocessor_config() -> None:
    """The regression that made a byte-verified model fail at first decode.

    `large-v3` needs 128 mel bins; without `preprocessor_config.json` the feature
    extractor defaults to 80 and the encoder rejects the input shape. Every byte
    verified, and the model was still unusable.
    """
    spec = MODEL_CATALOGUE["asr-pass2"]
    assert "preprocessor_config.json" in spec.optional_files


# ===========================================================================
# The manifest hash chain
# ===========================================================================


def test_a_manifest_describes_every_model_file(tmp_path: Path) -> None:
    model = _write_model(tmp_path / "m")
    _manifest_for(model)
    manifest = read_manifest(model)
    assert manifest.schema == MANIFEST_SCHEMA
    names = {entry.name for entry in manifest.files}
    assert names == {"config.json", "model.bin", "tokenizer.json", "vocabulary.txt"}
    for entry in manifest.files:
        assert len(entry.sha256) == 64
        assert entry.size_bytes == (model / entry.name).stat().st_size


def test_verification_passes_on_an_untouched_model(tmp_path: Path) -> None:
    model = _write_model(tmp_path / "m")
    _path, digest = _manifest_for(model)
    manifest = verify_directory(model, expected_digest=digest, deep=True)
    assert manifest.model_name == "faster-whisper-small"


def test_a_single_flipped_byte_is_detected(tmp_path: Path) -> None:
    """The whole point of the chain."""
    model = _write_model(tmp_path / "m")
    _manifest_for(model)
    weights = model / "model.bin"
    data = bytearray(weights.read_bytes())
    data[7] ^= 0x01
    weights.write_bytes(bytes(data))
    with pytest.raises(ManifestError, match="sha256 mismatch"):
        verify_directory(model, deep=True)


def test_a_truncated_file_is_detected_without_hashing(tmp_path: Path) -> None:
    """Size is checked first: a truncated download is the common failure."""
    model = _write_model(tmp_path / "m")
    _manifest_for(model)
    (model / "model.bin").write_bytes(b"\x00" * 8)
    with pytest.raises(ManifestError, match="expected .* bytes"):
        verify_directory(model, deep=False)


def test_a_missing_file_is_detected(tmp_path: Path) -> None:
    model = _write_model(tmp_path / "m")
    _manifest_for(model)
    (model / "tokenizer.json").unlink()
    with pytest.raises(ManifestError, match="missing file"):
        verify_directory(model, deep=False)


def test_an_undeclared_extra_file_is_reported(tmp_path: Path) -> None:
    """Usually a half-finished re-provision, and worth surfacing."""
    model = _write_model(tmp_path / "m")
    _manifest_for(model)
    (model / "surprise.bin").write_bytes(b"?")
    with pytest.raises(ManifestError, match="undeclared file present"):
        verify_directory(model, deep=False)


def test_a_swapped_manifest_is_detected_by_the_registry_digest(tmp_path: Path) -> None:
    """Rewriting the manifest to match tampered files must not work.

    That is precisely why the registry records the digest of the manifest and not just
    the digest of the weights.
    """
    model = _write_model(tmp_path / "m")
    _path, original_digest = _manifest_for(model)

    (model / "model.bin").write_bytes(b"\xff" * 256)
    _manifest_for(model)  # a manifest that honestly describes the tampered file

    verify_directory(model, deep=True)  # internally consistent, so this passes
    with pytest.raises(ManifestError, match="manifest digest mismatch"):
        verify_directory(model, expected_digest=original_digest, deep=True)


def test_a_missing_manifest_refuses_to_load(tmp_path: Path) -> None:
    model = _write_model(tmp_path / "m")
    with pytest.raises(ManifestError, match=MANIFEST_FILENAME):
        read_manifest(model)


def test_a_manifest_from_a_future_schema_is_refused(tmp_path: Path) -> None:
    model = _write_model(tmp_path / "m")
    _manifest_for(model)
    target = model / MANIFEST_FILENAME
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["schema"] = MANIFEST_SCHEMA + 1
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ManifestError, match="schema"):
        read_manifest(model)


@pytest.mark.parametrize(
    "name", ["../escape.bin", "sub/dir.bin", "..\\escape.bin", "/abs.bin"]
)
def test_a_manifest_entry_cannot_point_outside_the_model_directory(name: str) -> None:
    """Path traversal inside a manifest would let verification check the wrong file."""
    with pytest.raises(ManifestError, match="bare filename"):
        ModelFile.from_dict({"name": name, "size_bytes": 1, "sha256": "a" * 64})


@pytest.mark.parametrize("digest", ["", "z" * 64, "abc", "A" * 63])
def test_a_malformed_digest_in_a_manifest_is_refused(digest: str) -> None:
    with pytest.raises(ManifestError, match="sha256"):
        ModelFile.from_dict({"name": "model.bin", "size_bytes": 1, "sha256": digest})


def test_a_manifest_with_no_files_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ManifestError, match="no model files"):
        _manifest_for(empty)


def test_the_manifest_digest_is_stable_for_identical_content(tmp_path: Path) -> None:
    """Formatting must not change a model's identity."""
    model = _write_model(tmp_path / "m")
    first = build_manifest(
        model,
        provider_slot="asr",
        model_name="m",
        revision="r",
        source_repo="o/r",
        source_revision="r",
        license_name="MIT",
        license_url=None,
        hardware_profile="cpu-int8",
        provisioned_at="2026-01-01T00:00:00.000Z",
    )
    second = build_manifest(
        model,
        provider_slot="asr",
        model_name="m",
        revision="r",
        source_repo="o/r",
        source_revision="r",
        license_name="MIT",
        license_url=None,
        hardware_profile="cpu-int8",
        provisioned_at="2026-01-01T00:00:00.000Z",
    )
    assert manifest_digest(first) == manifest_digest(second)

    # Re-provisioning at a different time is a different manifest, deliberately.
    later = build_manifest(
        model,
        provider_slot="asr",
        model_name="m",
        revision="r",
        source_repo="o/r",
        source_revision="r",
        license_name="MIT",
        license_url=None,
        hardware_profile="cpu-int8",
        provisioned_at="2026-01-02T00:00:00.000Z",
    )
    assert manifest_digest(later) != manifest_digest(first)


def test_the_manifest_is_written_atomically(tmp_path: Path) -> None:
    """No `.part` may survive a successful write."""
    model = _write_model(tmp_path / "m")
    _manifest_for(model)
    assert (model / MANIFEST_FILENAME).is_file()
    assert not list(model.glob("*.part"))


def test_sha256_file_matches_hashlib(tmp_path: Path) -> None:
    import hashlib

    target = tmp_path / "blob"
    payload = bytes(range(256)) * 5000
    target.write_bytes(payload)
    assert sha256_file(target) == hashlib.sha256(payload).hexdigest()


# ===========================================================================
# Discovery and fail-closed resolution
# ===========================================================================


def test_an_empty_model_store_lists_nothing(tmp_path: Path) -> None:
    assert promoted_models(tmp_path / "models") == []


def test_a_promoted_model_is_discovered_with_its_provenance(tmp_path: Path) -> None:
    models = tmp_path / "models"
    spec = MODEL_CATALOGUE["asr-pass1"]
    directory = model_directory(models, spec, "b" * 40)
    _write_model(directory)
    _manifest_for(directory, model_name=spec.model_name, revision="b" * 40)

    found = promoted_models(models)
    assert len(found) == 1
    entry = found[0]
    assert entry["ok"] is True
    assert entry["model_name"] == spec.model_name
    assert entry["role"] == "pass1"
    assert entry["license"] == "MIT"
    assert entry["provider_slot"] == "asr"


def test_a_corrupt_model_is_reported_rather_than_hidden(tmp_path: Path) -> None:
    """"No model" and "a corrupt model" need different actions from an operator."""
    models = tmp_path / "models"
    spec = MODEL_CATALOGUE["asr-pass1"]
    directory = model_directory(models, spec, "c" * 40)
    _write_model(directory)
    _manifest_for(directory, model_name=spec.model_name, revision="c" * 40)
    (directory / "model.bin").write_bytes(b"short")

    found = promoted_models(models)
    assert len(found) == 1
    assert found[0]["ok"] is False
    assert "model.bin" in (found[0]["problem"] or "")


def test_resolving_with_no_model_is_model_unavailable(tmp_path: Path) -> None:
    """The fail-closed path. No download, no fallback."""
    from mom_igd.asr.faster_whisper_provider import resolve_model
    from mom_igd.asr.provider import ModelUnavailableError

    with pytest.raises(ModelUnavailableError, match="MODEL_UNAVAILABLE"):
        resolve_model(tmp_path / "models", role="pass1")


def _install_ready(models: Path, spec, revision: str, *, role: str) -> Path:
    """A promoted, manifested and readiness-recorded model. Standing in for a probe pass."""
    from mom_igd.asr.installed import record_ready

    directory = model_directory(models, spec, revision)
    _write_model(directory)
    _manifest_for(
        directory, model_name=spec.model_name, revision=revision, extra={"role": role}
    )
    record_ready(models, directory=directory, role=role, probe_detail="synthetic")
    return directory


def test_resolving_a_corrupt_model_is_model_unavailable_not_a_fallback(
    tmp_path: Path,
) -> None:
    """A broken pass-1 must never silently resolve to the pass-2 model.

    Both are recorded as ready first, so the refusal comes from pass-1 actually being
    corrupt -- not merely from a missing readiness record, which would prove nothing
    about the no-fallback rule.
    """
    from mom_igd.asr.faster_whisper_provider import resolve_model
    from mom_igd.asr.provider import ModelUnavailableError

    models = tmp_path / "models"
    pass1 = MODEL_CATALOGUE["asr-pass1"]
    pass2 = MODEL_CATALOGUE["asr-pass2"]

    broken = _install_ready(models, pass1, "d" * 40, role="pass1")
    _install_ready(models, pass2, "e" * 40, role="pass2")

    # Corrupt pass-1 *after* it was recorded ready: the digest recorded at probe time no
    # longer describes what is on disk.
    (broken / "model.bin").write_bytes(b"x")

    with pytest.raises(ModelUnavailableError, match="MODEL_UNAVAILABLE"):
        resolve_model(models, role="pass1")
    # The healthy pass-2 model is still resolvable for its own role, and pass-1 did not
    # borrow it.
    assert resolve_model(models, role="pass2").model_name == pass2.model_name


def test_a_model_without_a_readiness_record_is_not_ready(tmp_path: Path) -> None:
    """A manifest-valid directory is not a ready model.

    This is the `preprocessor_config.json` lesson turned into a rule: every byte can
    verify while the model is still unusable, so readiness is the recorded verdict of a
    load-and-decode probe, never the presence of a directory.
    """
    from mom_igd.asr.faster_whisper_provider import resolve_model
    from mom_igd.asr.provider import ModelUnavailableError

    models = tmp_path / "models"
    spec = MODEL_CATALOGUE["asr-pass1"]
    directory = model_directory(models, spec, "f" * 40)
    _write_model(directory)
    _manifest_for(directory, model_name=spec.model_name, revision="f" * 40)

    # Byte verification passes...
    verify_model(directory)
    # ...and the model is still not resolvable, because nothing probed it.
    with pytest.raises(ModelUnavailableError, match="installed AND probe-verified"):
        resolve_model(models, role="pass1")


def test_a_corrupt_readiness_index_means_nothing_is_ready(tmp_path: Path) -> None:
    """Fail closed. A registry that cannot be parsed must not fall back to a scan."""
    from mom_igd.asr.faster_whisper_provider import resolve_model
    from mom_igd.asr.installed import INDEX_FILENAME, load_index
    from mom_igd.asr.provider import ModelUnavailableError

    models = tmp_path / "models"
    _install_ready(models, MODEL_CATALOGUE["asr-pass1"], "a" * 40, role="pass1")
    assert resolve_model(models, role="pass1").model_name  # sanity: it worked

    (models / INDEX_FILENAME).write_text("{ not json", encoding="utf-8")
    index = load_index(models)
    assert index.readable is False
    assert index.ready(models) == []
    with pytest.raises(ModelUnavailableError, match="cannot be trusted"):
        resolve_model(models, role="pass1")


def test_a_rewritten_manifest_does_not_inherit_an_old_readiness_verdict(
    tmp_path: Path,
) -> None:
    """The case only the recorded digest can catch.

    Tamper with the weights *and* regenerate the manifest to match. The directory is now
    internally consistent, so byte verification passes on its own terms -- and it is a
    different model from the one that passed the probe. The readiness record pins the
    manifest digest precisely so that this is refused.
    """
    from mom_igd.asr.faster_whisper_provider import resolve_model
    from mom_igd.asr.installed import load_index
    from mom_igd.asr.provider import ModelUnavailableError

    models = tmp_path / "models"
    spec = MODEL_CATALOGUE["asr-pass1"]
    directory = _install_ready(models, spec, "a" * 40, role="pass1")
    recorded = load_index(models).models[0].manifest_sha256
    assert resolve_model(models, role="pass1").model_name == spec.model_name

    # Swap the weights and re-manifest, so nothing is internally inconsistent.
    (directory / "model.bin").write_bytes(b"\xff" * 128)
    _manifest_for(directory, model_name=spec.model_name, revision="a" * 40)
    verify_model(directory)  # internally consistent: passes on its own terms
    assert load_index(models).models[0].manifest_sha256 == recorded

    # The readiness record no longer describes what is on disk.
    assert load_index(models).ready(models, role="pass1") == []
    with pytest.raises(ModelUnavailableError):
        resolve_model(models, role="pass1")


def test_the_readiness_index_filters_by_exact_role(tmp_path: Path) -> None:
    """The role filter is its own layer and must be covered on its own.

    `resolve_model` also intersects with the catalogue names for the requested role, so
    removing this filter is not observable through the resolver. That defence in depth is
    good -- and it means the layer needs a direct test, or it could rot unnoticed.
    """
    from mom_igd.asr.installed import load_index

    models = tmp_path / "models"
    _install_ready(models, MODEL_CATALOGUE["asr-pass1"], "a" * 40, role="pass1")
    _install_ready(models, MODEL_CATALOGUE["asr-pass2"], "b" * 40, role="pass2")

    index = load_index(models)
    assert len(index.models) == 2

    pass1 = index.ready(models, role="pass1")
    pass2 = index.ready(models, role="pass2")
    assert [entry.role for entry in pass1] == ["pass1"]
    assert [entry.role for entry in pass2] == ["pass2"]
    assert pass1[0].model_name == MODEL_CATALOGUE["asr-pass1"].model_name
    assert pass2[0].model_name == MODEL_CATALOGUE["asr-pass2"].model_name

    # An unknown role matches nothing, rather than returning everything.
    assert index.ready(models, role="pass3") == []
    # No role means no filter, which is what a listing wants.
    assert len(index.ready(models)) == 2


def test_an_index_whose_problem_is_cleared_still_reports_nothing_ready(
    tmp_path: Path,
) -> None:
    """`ready()` must not depend on the caller having checked `readable` first.

    Defence in depth: even if a future refactor forgot the `readable` guard in the
    resolver, an index built from no entries yields no ready models.
    """
    from mom_igd.asr.installed import InstalledIndex

    empty = InstalledIndex(models=(), problem=None)
    assert empty.readable is True
    assert empty.ready(tmp_path / "models") == []
    assert empty.ready(tmp_path / "models", role="pass1") == []


def test_a_readiness_record_for_a_deleted_directory_is_not_ready(tmp_path: Path) -> None:
    """A record whose model is gone must not resolve."""
    import shutil

    from mom_igd.asr.faster_whisper_provider import resolve_model
    from mom_igd.asr.installed import load_index
    from mom_igd.asr.provider import ModelUnavailableError

    models = tmp_path / "models"
    directory = _install_ready(models, MODEL_CATALOGUE["asr-pass1"], "a" * 40, role="pass1")
    shutil.rmtree(directory)

    assert load_index(models).ready(models, role="pass1") == []
    with pytest.raises(ModelUnavailableError):
        resolve_model(models, role="pass1")


def test_a_readiness_record_cannot_point_outside_the_model_store() -> None:
    """An edited registry must not be able to aim the loader anywhere on disk."""
    from mom_igd.asr.installed import InstalledModel

    escapes = (
        "../../etc/model",
        # POSIX-absolute. On Windows `Path(...).is_absolute()` is False for this, so a
        # naive check let it through -- which is why the validator tests both flavours.
        "/abs/model",
        r"\\server\share\model",  # UNC root
        "C:/abs/model",
        r"C:\abs\model",
        "a/../../b",
        "   ",  # whitespace-only, which strips to nothing
    )

    def entry(relative: str) -> dict[str, object]:
        return {
            "provider_slot": "asr",
            "model_name": "x",
            "revision": "r",
            "manifest_sha256": "a" * 64,
            "relative_path": relative,
        }

    for escape in escapes:
        with pytest.raises(ValueError, match="escapes the model store"):
            InstalledModel.from_dict(entry(escape))

    # An empty path is refused too, by the required-field check rather than the escape
    # check. Both are correct rejections; the message differs.
    with pytest.raises(ValueError, match="missing"):
        InstalledModel.from_dict(entry(""))

    # And a genuine relative path inside the store is accepted.
    accepted = InstalledModel.from_dict(entry("asr/faster-whisper-small/abc123"))
    assert accepted.relative_path == "asr/faster-whisper-small/abc123"


def test_recording_readiness_twice_replaces_rather_than_duplicates(tmp_path: Path) -> None:
    """Re-provisioning the same revision must leave one record, not two."""
    from mom_igd.asr.installed import load_index, record_ready

    models = tmp_path / "models"
    directory = _install_ready(models, MODEL_CATALOGUE["asr-pass1"], "b" * 40, role="pass1")
    record_ready(models, directory=directory, role="pass1", probe_detail="again")

    index = load_index(models)
    assert len(index.models) == 1
    assert index.models[0].probe_detail == "again"


def test_quarantine_moves_a_model_out_of_the_load_path(tmp_path: Path) -> None:
    """A model that verifies but cannot run must not sit where the resolver looks."""
    from mom_igd.asr.installed import QUARANTINE_DIRNAME, quarantine_directory
    from mom_igd.asr.provision import promoted_models

    models = tmp_path / "models"
    spec = MODEL_CATALOGUE["asr-pass1"]
    directory = model_directory(models, spec, "c" * 40)
    _write_model(directory)
    _manifest_for(directory, model_name=spec.model_name, revision="c" * 40)
    assert len(promoted_models(models)) == 1

    moved = quarantine_directory(models, directory, reason="probe failed in a test")
    assert not directory.exists()
    assert moved.is_dir()
    assert QUARANTINE_DIRNAME in moved.parts
    assert (moved / "QUARANTINE_REASON.txt").read_text(encoding="utf-8").count("probe failed")
    assert promoted_models(models) == [], "a quarantined model must leave the load path"


def test_verify_model_deep_hashes_every_byte(tmp_path: Path) -> None:
    model = _write_model(tmp_path / "m")
    _manifest_for(model)
    manifest = verify_model(model)
    assert len(manifest.files) == 4


# ===========================================================================
# No implicit download, ever
# ===========================================================================


def test_importing_the_asr_package_pulls_in_nothing_heavy_or_networked() -> None:
    """`doctor`, the CLI and the API must not pay for CTranslate2 or a hub client."""
    import subprocess

    code = (
        "import sys;"
        "import mom_igd.asr, mom_igd.asr.manifest, mom_igd.asr.provision,"
        " mom_igd.asr.provider;"
        "bad=[m for m in sys.modules if m.split('.')[0] in "
        "('ctranslate2','faster_whisper','onnxruntime','av','torch','huggingface_hub',"
        "'requests','httpx','urllib3','numpy','tokenizers')];"
        "print(sorted(bad))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", (
        f"importing mom_igd.asr pulled in heavy or network-capable modules: "
        f"{result.stdout.strip()}"
    )


def test_only_the_provisioning_module_mentions_a_download_api() -> None:
    """A grep-level guard: no runtime module may reference the hub download API."""
    import ast

    repo = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for path in sorted((repo / "mom_igd").rglob("*.py")):
        relative = path.relative_to(repo).as_posix()
        if relative == "mom_igd/asr/provision.py":
            continue  # the one deliberate exception
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in {"huggingface_hub", "requests", "aiohttp"}:
                    offenders.append(f"{relative}:{node.lineno} imports {name}")
    assert offenders == [], offenders


def test_the_offline_environment_flags_are_set_before_any_engine_import() -> None:
    from mom_igd.asr.faster_whisper_provider import assert_offline_environment

    flags = assert_offline_environment()
    import os

    for key, value in flags.items():
        assert os.environ.get(key) == value, key
    assert "HF_HUB_OFFLINE" in flags
    assert flags["HF_HUB_OFFLINE"] == "1"


def test_a_model_path_is_never_a_url() -> None:
    """The registry rejects a remote path, which is what keeps loading local."""
    from mom_igd.registry import ModelEntry

    for bad in ("http://example.com/model.bin", "https://hf.co/x/model.bin"):
        with pytest.raises(ValueError):
            ModelEntry(
                provider="asr",
                name="x",
                version="1",
                path=bad,
                sha256="a" * 64,
                size_bytes=1,
                license_name="MIT",
            )
