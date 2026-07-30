"""Voiceprint persistence, crash recovery, verification and revocation deletion.

The filesystem and SQLite cannot be one atomic transaction. These tests therefore
simulate an interruption at each step of the save protocol and assert that recovery
reaches an *honest* conclusion -- and, above all, that **no partially written
voiceprint is ever usable**.

Nothing here opens a microphone or touches the real data root.
"""

from __future__ import annotations

import json
import sqlite3
import uuid as uuid_module
from pathlib import Path
from typing import Any

import pytest

from mom_igd.config import AppConfig
from mom_igd.db import initialize_database
from mom_igd.db.connection import connect
from mom_igd.enrollment.cipher import ModelIdentity, VoiceprintCipher, sealed_sha256
from mom_igd.enrollment.keys import FakeKeyProtector
from mom_igd.enrollment.store import (
    PAYLOAD_SCHEMA,
    PayloadSchemaError,
    VoiceprintStatus,
    VoiceprintStore,
    VoiceprintStoreError,
)

MODEL = ModelIdentity(name="test-embed", version="1.0", sha256="c" * 64)
OTHER_MODEL = ModelIdentity(name="test-embed", version="2.0", sha256="d" * 64)


def _payload(dim: int = 4, samples: int = 5) -> dict[str, Any]:
    return {
        "payload_schema": PAYLOAD_SCHEMA,
        "centroid": [round(0.1 * (i + 1), 4) for i in range(dim)],
        "dispersion": [0.01] * dim,
        "sample_count": samples,
        "embedding_dim": dim,
        "dtype": "float32-json",
    }


@pytest.fixture
def db_path(config: AppConfig, paths) -> Path:
    initialize_database(
        paths.database_path(config.database.filename),
        busy_timeout_ms=config.database.busy_timeout_ms,
        app_version=config.app_version,
    )
    return paths.database_path(config.database.filename)


@pytest.fixture
def factory(db_path: Path, config: AppConfig):
    def _connect() -> sqlite3.Connection:
        return connect(db_path, busy_timeout_ms=config.database.busy_timeout_ms)

    return _connect


@pytest.fixture
def cipher(tmp_path: Path) -> VoiceprintCipher:
    return VoiceprintCipher(
        FakeKeyProtector(tmp_path / "keys").create_if_missing(created_utc="2026-07-29T00:00:00Z")
    )


@pytest.fixture
def store(paths, factory) -> VoiceprintStore:
    return VoiceprintStore(paths.voiceprints_dir, factory)


@pytest.fixture
def participant(factory) -> int:
    conn = factory()
    try:
        cursor = conn.execute(
            "INSERT INTO participants (display_name, uuid) VALUES ('Budi', ?)",
            (str(uuid_module.uuid4()),),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def _save(store, cipher, participant_id, *, development_only=False, **kwargs):
    vp = str(uuid_module.uuid4())
    result = store.save(
        cipher=cipher,
        payload=kwargs.pop("payload", _payload()),
        voiceprint_uuid=vp,
        participant_id=participant_id,
        model=kwargs.pop("model", MODEL),
        enrollment_session_id=None,
        consent_event_id=None,
        development_only=development_only,
        device_fingerprint="f" * 32,
        device_transport="USB",
        sample_rate_hz=48_000,
        channels=1,
        quality_verdict="PASS",
        min_pair_cosine=0.91,
        **kwargs,
    )
    return vp, result


def _row(factory, vp: str) -> sqlite3.Row:
    conn = factory()
    try:
        return conn.execute(
            "SELECT * FROM voiceprints WHERE voiceprint_uuid = ?", (vp,)
        ).fetchone()
    finally:
        conn.close()


# ==================================================================== save


def test_save_activates_and_leaves_no_temporary_file(
    store, cipher, participant, paths
) -> None:
    vp, result = _save(store, cipher, participant)
    assert result["status"] == VoiceprintStatus.ACTIVE.value
    assert result["production_eligible"] is True
    final = paths.voiceprints_dir / f"{vp}.vpx"
    assert final.is_file()
    assert list(paths.voiceprints_dir.glob("*.tmp")) == []
    assert sealed_sha256(final.read_bytes()) == result["envelope_sha256"]


def test_the_envelope_on_disk_holds_no_plaintext(store, cipher, participant, paths) -> None:
    vp, _ = _save(store, cipher, participant)
    text = (paths.voiceprints_dir / f"{vp}.vpx").read_text(encoding="utf-8")
    assert "centroid" not in text
    assert "dispersion" not in text
    for value in ("0.1", "0.2", "0.3", "0.4"):
        assert f'"{value}"' not in text


def test_the_database_row_holds_no_biometric_data(store, cipher, participant, factory) -> None:
    vp, _ = _save(store, cipher, participant)
    row = _row(factory, vp)
    blob = " ".join(str(v) for v in dict(row).values()).lower()
    for forbidden in ("centroid", "dispersion", "ciphertext", "nonce"):
        assert forbidden not in blob
    # Shape is allowed: it is not content.
    assert row["embedding_dim"] == 4
    assert row["sample_count"] == 5


def test_a_development_only_save_is_not_production_eligible(
    store, cipher, participant, factory
) -> None:
    vp, result = _save(store, cipher, participant, development_only=True)
    assert result["status"] == VoiceprintStatus.DEVELOPMENT_ONLY.value
    assert result["production_eligible"] is False
    assert int(_row(factory, vp)["production_eligible"]) == 0


def test_saving_over_an_existing_envelope_is_refused(store, cipher, participant, paths) -> None:
    vp = str(uuid_module.uuid4())
    (paths.voiceprints_dir).mkdir(parents=True, exist_ok=True)
    (paths.voiceprints_dir / f"{vp}.vpx").write_bytes(b"existing")
    with pytest.raises(VoiceprintStoreError, match="already exists"):
        store.save(
            cipher=cipher,
            payload=_payload(),
            voiceprint_uuid=vp,
            participant_id=participant,
            model=MODEL,
            enrollment_session_id=None,
            consent_event_id=None,
            development_only=False,
            device_fingerprint=None,
            device_transport=None,
            sample_rate_hz=None,
            channels=None,
            quality_verdict=None,
            min_pair_cosine=None,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        {"payload_schema": 99},
        {"centroid": []},
        {"embedding_dim": 99},
        {"sample_count": 0},
    ],
)
def test_a_malformed_payload_never_reaches_disk(
    store, cipher, participant, paths, mutation
) -> None:
    payload = _payload()
    payload.update(mutation)
    with pytest.raises(PayloadSchemaError):
        _save(store, cipher, participant, payload=payload)
    assert not paths.voiceprints_dir.exists() or list(paths.voiceprints_dir.glob("*.vpx")) == []


def test_re_enrollment_supersedes_and_deletes_the_previous_envelope(
    store, cipher, participant, paths, factory
) -> None:
    first, _ = _save(store, cipher, participant)
    second, _ = _save(store, cipher, participant)

    assert _row(factory, first)["status"] == VoiceprintStatus.SUPERSEDED.value
    assert _row(factory, second)["status"] == VoiceprintStatus.ACTIVE.value
    # The old ciphertext is gone; keeping it would widen the blast radius.
    assert not (paths.voiceprints_dir / f"{first}.vpx").exists()
    assert (paths.voiceprints_dir / f"{second}.vpx").is_file()
    assert _row(factory, first)["envelope_relative_path"] is None


def test_only_one_voiceprint_stays_live_per_participant(
    store, cipher, participant, factory
) -> None:
    for _ in range(3):
        _save(store, cipher, participant)
    conn = factory()
    try:
        live = conn.execute(
            "SELECT count(*) AS n FROM voiceprints WHERE participant_id = ? "
            "AND status IN ('ACTIVE','DEVELOPMENT_ONLY')",
            (participant,),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert int(live) == 1


# ============================================================ load / verify


def test_load_returns_the_payload_for_a_live_voiceprint(store, cipher, participant) -> None:
    vp, _ = _save(store, cipher, participant)
    assert store.load_payload(vp, cipher=cipher) == _payload()


def test_verify_passes_for_a_healthy_voiceprint(store, cipher, participant) -> None:
    vp, _ = _save(store, cipher, participant)
    outcome = store.verify(vp, cipher=cipher)
    assert outcome.ok is True, outcome.problems
    for check in (
        "row_present",
        "file_present",
        "size_matches",
        "envelope_sha256_matches",
        "schema_known",
        "participant_binding",
        "uuid_binding",
        "model_binding",
        "authenticated",
        "payload_schema_valid",
        "embedding_dim_matches",
    ):
        assert outcome.checks.get(check) is True, f"{check} was not verified"


def test_verify_without_a_key_still_checks_what_it_can(store, cipher, participant) -> None:
    """`doctor` verifies integrity without unwrapping the master key."""
    vp, _ = _save(store, cipher, participant)
    outcome = store.verify(vp)
    assert outcome.ok is True
    assert outcome.checks["envelope_sha256_matches"] is True
    assert "authenticated" not in outcome.checks


def test_a_tampered_envelope_is_detected_and_quarantines_the_row(
    store, cipher, participant, paths, factory
) -> None:
    vp, _ = _save(store, cipher, participant)
    path = paths.voiceprints_dir / f"{vp}.vpx"
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = bytearray(bytes.fromhex(payload["ciphertext"]))
    raw[0] ^= 0x01
    payload["ciphertext"] = raw.hex()
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    outcome = store.verify(vp, cipher=cipher)
    assert outcome.ok is False
    assert outcome.status == VoiceprintStatus.INTEGRITY_FAILED.value
    assert _row(factory, vp)["status"] == VoiceprintStatus.INTEGRITY_FAILED.value
    # And it is no longer loadable.
    with pytest.raises(VoiceprintStoreError, match="INTEGRITY_FAILED"):
        store.load_payload(vp, cipher=cipher)


def test_a_missing_envelope_marks_the_row_integrity_failed(
    store, cipher, participant, paths, factory
) -> None:
    vp, _ = _save(store, cipher, participant)
    (paths.voiceprints_dir / f"{vp}.vpx").unlink()
    outcome = store.verify(vp, cipher=cipher)
    assert outcome.ok is False
    assert _row(factory, vp)["status"] == VoiceprintStatus.INTEGRITY_FAILED.value


def test_a_wrong_key_cannot_load_a_voiceprint(store, cipher, participant, tmp_path) -> None:
    vp, _ = _save(store, cipher, participant)
    wrong = VoiceprintCipher(
        FakeKeyProtector(tmp_path / "other", material=bytes(range(60, 92))).create_if_missing(
            created_utc="x"
        )
    )
    from mom_igd.enrollment.cipher import CipherError

    with pytest.raises(CipherError):
        store.load_payload(vp, cipher=wrong)


def test_a_swapped_envelope_between_participants_is_detected(
    store, cipher, participant, paths, factory
) -> None:
    """Overwriting one person's envelope with another's must not authenticate."""
    conn = factory()
    try:
        other = int(
            conn.execute(
                "INSERT INTO participants (display_name, uuid) VALUES ('Siti', ?)",
                (str(uuid_module.uuid4()),),
            ).lastrowid
        )
        conn.commit()
    finally:
        conn.close()

    first, _ = _save(store, cipher, participant)
    second, _ = _save(store, cipher, other)
    # Copy Budi's envelope over Siti's file.
    target = paths.voiceprints_dir / f"{second}.vpx"
    target.write_bytes((paths.voiceprints_dir / f"{first}.vpx").read_bytes())

    outcome = store.verify(second, cipher=cipher)
    assert outcome.ok is False
    assert outcome.checks["uuid_binding"] is False


# ================================================================ recovery


def test_recovery_is_idempotent_on_a_healthy_store(store, cipher, participant) -> None:
    _save(store, cipher, participant)
    first = store.recover_incomplete_operations()
    second = store.recover_incomplete_operations()
    assert first.changed is False
    assert second.changed is False


def test_crash_before_the_rename_is_recovered_as_failed(
    store, cipher, participant, paths, factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pending row, no final file -> honest failure, temp cleaned, never usable."""
    import os as os_module

    def _boom(src, dst):  # noqa: ANN001
        raise OSError(5, "simulated crash before rename")

    monkeypatch.setattr(os_module, "replace", _boom)
    with pytest.raises(OSError):
        _save(store, cipher, participant)
    monkeypatch.undo()

    row = factory().execute(
        "SELECT voiceprint_uuid, status FROM voiceprints"
    ).fetchone()
    assert row is not None, "the pending row must survive so recovery can see it"
    assert row["status"] == VoiceprintStatus.INTEGRITY_FAILED.value
    assert not VoiceprintStatus(row["status"]).usable

    # The save path already removed its own temp file, so recovery finds nothing
    # to quarantine -- and, crucially, changes nothing.
    report = store.recover_incomplete_operations()
    assert list(paths.voiceprints_dir.glob("*.tmp")) == []
    assert report.quarantined_temp == 0
    assert not (paths.voiceprints_dir / f"{row['voiceprint_uuid']}.vpx").exists()


def test_a_pending_row_with_a_valid_file_is_finalised_but_not_activated(
    store, cipher, participant, paths, factory
) -> None:
    """Crash after rename, before activation.

    The bytes are provably correct, so they are kept -- but the enrollment that
    produced them never finished, so eligibility cannot be re-established and the
    row becomes RE_ENROLL_REQUIRED rather than silently ACTIVE.
    """
    vp, _ = _save(store, cipher, participant)
    # Rewind the row to the pending state the crash would have left.
    conn = factory()
    try:
        conn.execute(
            "UPDATE voiceprints SET status = ?, activated_at = NULL "
            "WHERE voiceprint_uuid = ?",
            (VoiceprintStatus.PENDING_WRITE.value, vp),
        )
        conn.commit()
    finally:
        conn.close()

    report = store.recover_incomplete_operations()
    assert report.finalised == 1
    status = _row(factory, vp)["status"]
    assert status == VoiceprintStatus.RE_ENROLL_REQUIRED.value
    assert not VoiceprintStatus(status).usable
    assert (paths.voiceprints_dir / f"{vp}.vpx").is_file(), "valid bytes are kept"


def test_a_pending_row_whose_file_vanished_is_marked_failed(
    store, cipher, participant, paths, factory
) -> None:
    vp, _ = _save(store, cipher, participant)
    conn = factory()
    try:
        conn.execute(
            "UPDATE voiceprints SET status = ? WHERE voiceprint_uuid = ?",
            (VoiceprintStatus.PENDING_WRITE.value, vp),
        )
        conn.commit()
    finally:
        conn.close()
    (paths.voiceprints_dir / f"{vp}.vpx").unlink()

    report = store.recover_incomplete_operations()
    assert report.marked_failed == 1
    assert _row(factory, vp)["status"] == VoiceprintStatus.INTEGRITY_FAILED.value


def test_a_pending_row_with_a_corrupt_file_is_marked_integrity_failed(
    store, cipher, participant, paths, factory
) -> None:
    vp, _ = _save(store, cipher, participant)
    conn = factory()
    try:
        conn.execute(
            "UPDATE voiceprints SET status = ? WHERE voiceprint_uuid = ?",
            (VoiceprintStatus.PENDING_WRITE.value, vp),
        )
        conn.commit()
    finally:
        conn.close()
    (paths.voiceprints_dir / f"{vp}.vpx").write_bytes(b"replaced content")

    report = store.recover_incomplete_operations()
    assert report.marked_integrity_failed == 1
    assert _row(factory, vp)["status"] == VoiceprintStatus.INTEGRITY_FAILED.value


def test_an_active_row_with_an_altered_envelope_is_caught_by_recovery(
    store, cipher, participant, paths, factory
) -> None:
    vp, _ = _save(store, cipher, participant)
    (paths.voiceprints_dir / f"{vp}.vpx").write_bytes(b"tampered")
    report = store.recover_incomplete_operations()
    assert report.marked_integrity_failed == 1
    assert _row(factory, vp)["status"] == VoiceprintStatus.INTEGRITY_FAILED.value


def test_an_orphan_envelope_is_quarantined_not_deleted(store, paths) -> None:
    """Evidence is kept. A file that cannot be attributed is not trusted either."""
    paths.voiceprints_dir.mkdir(parents=True, exist_ok=True)
    orphan = paths.voiceprints_dir / f"{uuid_module.uuid4()}.vpx"
    orphan.write_bytes(b"unattributable")

    report = store.recover_incomplete_operations()
    assert report.quarantined_orphan == 1
    assert not orphan.exists()
    quarantined = list((paths.voiceprints_dir / "quarantine").glob("*.vpx"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"unattributable"
    assert (
        quarantined[0].with_suffix(quarantined[0].suffix + ".reason.txt").is_file()
    ), "a quarantined file must record why"


def test_a_stray_temporary_file_is_quarantined(store, paths) -> None:
    paths.voiceprints_dir.mkdir(parents=True, exist_ok=True)
    stray = paths.voiceprints_dir / f"{uuid_module.uuid4()}.vpx.tmp"
    stray.write_bytes(b"abandoned save")
    report = store.recover_incomplete_operations()
    assert report.quarantined_temp == 1
    assert not stray.exists()


# ============================================================== revocation


def test_revocation_deletes_the_ciphertext_and_clears_the_pointer(
    store, cipher, participant, paths, factory
) -> None:
    vp, _ = _save(store, cipher, participant)
    result = store.delete_for_revocation(participant)

    assert result["fully_deleted"] is True
    assert vp in result["deleted"]
    assert not (paths.voiceprints_dir / f"{vp}.vpx").exists()
    row = _row(factory, vp)
    assert row["status"] == VoiceprintStatus.REVOKED.value
    assert row["envelope_relative_path"] is None
    assert row["envelope_sha256"] is None
    assert int(row["production_eligible"]) == 0


def test_a_revoked_voiceprint_is_immediately_unusable(store, cipher, participant) -> None:
    vp, _ = _save(store, cipher, participant)
    store.delete_for_revocation(participant)
    assert store.status_for_participant(participant)["has_usable_voiceprint"] is False
    with pytest.raises(VoiceprintStoreError):
        store.load_payload(vp, cipher=cipher)


def test_a_failed_deletion_becomes_delete_pending_and_stays_unusable(
    store, cipher, participant, factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem error must never leave a usable template behind."""
    vp, _ = _save(store, cipher, participant)
    monkeypatch.setattr(
        VoiceprintStore,
        "_unlink_envelope",
        lambda self, relative: (False, "OSError(errno=13)"),
    )
    result = store.delete_for_revocation(participant)
    assert result["fully_deleted"] is False
    assert vp in result["delete_pending"]

    row = _row(factory, vp)
    assert row["status"] == VoiceprintStatus.DELETE_PENDING.value
    assert not VoiceprintStatus(row["status"]).usable
    assert int(row["production_eligible"]) == 0
    # The pointer survives, so cleanup can be retried.
    assert row["envelope_relative_path"] is not None
    assert row["delete_error"] == "OSError(errno=13)"
    assert store.status_for_participant(participant)["has_usable_voiceprint"] is False


def test_retry_pending_cleanup_finishes_the_job(
    store, cipher, participant, paths, factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    vp, _ = _save(store, cipher, participant)
    monkeypatch.setattr(
        VoiceprintStore, "_unlink_envelope", lambda self, relative: (False, "OSError(errno=13)")
    )
    store.delete_for_revocation(participant)
    monkeypatch.undo()

    report = store.retry_pending_cleanup()
    assert report.cleanup_retried == 1
    assert report.cleanup_still_pending == 0
    assert not (paths.voiceprints_dir / f"{vp}.vpx").exists()
    row = _row(factory, vp)
    assert row["status"] == VoiceprintStatus.REVOKED.value
    assert row["delete_error"] is None


def test_retry_pending_cleanup_is_idempotent(store, cipher, participant) -> None:
    _save(store, cipher, participant)
    store.delete_for_revocation(participant)
    first = store.retry_pending_cleanup()
    second = store.retry_pending_cleanup()
    assert first.cleanup_retried == 0
    assert second.cleanup_retried == 0
    assert second.cleanup_still_pending == 0


def test_retry_keeps_pending_when_deletion_still_fails(
    store, cipher, participant, factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(store, cipher, participant)
    monkeypatch.setattr(
        VoiceprintStore, "_unlink_envelope", lambda self, relative: (False, "OSError(errno=13)")
    )
    store.delete_for_revocation(participant)
    report = store.retry_pending_cleanup()
    assert report.cleanup_retried == 0
    assert report.cleanup_still_pending == 1


def test_revocation_audit_carries_no_biometric_payload(
    store, cipher, participant, factory
) -> None:
    _save(store, cipher, participant)
    store.delete_for_revocation(participant)
    conn = factory()
    try:
        rows = list(
            conn.execute(
                "SELECT action, detail_json FROM audit_events "
                "WHERE action LIKE 'VOICEPRINT%' ORDER BY id"
            )
        )
    finally:
        conn.close()
    actions = [r["action"] for r in rows]
    assert "VOICEPRINT_CREATED" in actions
    assert "VOICEPRINT_DELETED" in actions
    blob = " ".join(str(r["detail_json"] or "") for r in rows).lower()
    for forbidden in ("centroid", "dispersion", "ciphertext", "nonce", "key_material"):
        assert forbidden not in blob


def test_revocation_is_safe_to_repeat(store, cipher, participant) -> None:
    _save(store, cipher, participant)
    first = store.delete_for_revocation(participant)
    second = store.delete_for_revocation(participant)
    assert first["fully_deleted"] is True
    assert second["deleted"] == [] and second["delete_pending"] == []


def test_the_audit_chain_survives_store_operations(store, cipher, participant, factory) -> None:
    from mom_igd.audit import verify_chain

    _save(store, cipher, participant)
    _save(store, cipher, participant)
    store.delete_for_revocation(participant)
    store.retry_pending_cleanup()
    conn = factory()
    try:
        verify_chain(conn)
    finally:
        conn.close()
