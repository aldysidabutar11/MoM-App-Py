"""Phase 3 CLI and diagnostics.

Two properties dominate:

* **Read-only means read-only.** `list`, `consent` (status), `enrollment`,
  `voiceprint`, `cleanup` and `doctor` must open no microphone, create no DPAPI
  master key, load no model and write nothing. A diagnostic that mutates state is
  worse than no diagnostic, because it is run precisely when things are already
  wrong.
* **Consent is a decision, not a flag.** Grant and revoke both refuse without an
  exact typed phrase, so no script can obtain biometric permission on someone's
  behalf by passing `--yes`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mom_igd.cli import main
from mom_igd.config import AppConfig
from mom_igd.db import initialize_database
from mom_igd.db.connection import connect
from mom_igd.diagnostics.enrollment_checks import (
    enrollment_checks,
)
from mom_igd.diagnostics.model import Status


@pytest.fixture
def runtime(paths, config: AppConfig) -> list[str]:
    """CLI arguments pointing at the temporary data root."""
    return ["--data-dir", str(paths.root)]


@pytest.fixture
def migrated(config: AppConfig, paths) -> Path:
    initialize_database(
        paths.database_path(config.database.filename),
        busy_timeout_ms=config.database.busy_timeout_ms,
        app_version=config.app_version,
    )
    return paths.database_path(config.database.filename)


@pytest.fixture
def factory(migrated: Path, config: AppConfig):
    def _connect() -> sqlite3.Connection:
        return connect(migrated, busy_timeout_ms=config.database.busy_timeout_ms)

    return _connect


def _by_key(results) -> dict[str, object]:
    return {r.key: r for r in results}


def _run(args: list[str], capsys) -> tuple[int, str]:
    code = main(args)
    return code, capsys.readouterr().out


def _create(runtime: list[str], capsys, name: str = "Budi") -> str:
    code, out = _run(["participant", "create", name, *runtime, "--json"], capsys)
    assert code == 0, out
    return json.loads(out)["uuid"]


# ================================================================== CLI: CRUD


def test_create_list_and_update(migrated, runtime, capsys) -> None:
    uuid_value = _create(runtime, capsys)
    code, out = _run(["participant", "list", *runtime], capsys)
    assert code == 0
    assert uuid_value in out
    assert "Budi" in out

    code, out = _run(
        ["participant", "update", uuid_value, "--role", "Ketua", *runtime], capsys
    )
    assert code == 0
    assert "Updated participant" in out


def test_duplicate_names_are_accepted_by_the_cli(migrated, runtime, capsys) -> None:
    first = _create(runtime, capsys, "Budi")
    second = _create(runtime, capsys, "Budi")
    assert first != second
    _code, out = _run(["participant", "list", *runtime], capsys)
    assert out.count("Budi") >= 2


def test_deactivate_and_reactivate(migrated, runtime, capsys) -> None:
    uuid_value = _create(runtime, capsys)
    code, out = _run(["participant", "deactivate", uuid_value, *runtime], capsys)
    assert code == 0
    assert "Deactivated" in out
    assert "never deleted" in out

    code, out = _run(
        ["participant", "deactivate", uuid_value, "--reactivate", *runtime], capsys
    )
    assert code == 0
    assert "Reactivated" in out


def test_an_unknown_uuid_exits_with_a_clear_message(migrated, runtime, capsys) -> None:
    with pytest.raises(SystemExit):
        main(["participant", "consent", "0" * 8 + "-0000-4000-8000-" + "0" * 12, *runtime])


# =============================================================== CLI: consent


def test_consent_status_is_read_only_and_shows_the_append_only_history(
    migrated, runtime, capsys
) -> None:
    uuid_value = _create(runtime, capsys)
    code, out = _run(["participant", "consent", uuid_value, *runtime], capsys)
    assert code == 0
    assert "active          : False" in out
    assert "append-only" in out


def test_granting_consent_without_the_phrase_refuses_and_changes_nothing(
    migrated, runtime, factory, capsys
) -> None:
    """No `--yes`: a script must not be able to grant biometric permission."""
    uuid_value = _create(runtime, capsys)
    code = main(["participant", "consent", uuid_value, "--action", "grant", *runtime])
    assert code == 1
    conn = factory()
    try:
        assert conn.execute("SELECT count(*) FROM consent_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_granting_consent_prints_the_full_text_and_hash(migrated, runtime, capsys) -> None:
    """The operator cannot record consent to wording they were never shown."""
    from mom_igd.enrollment.consent import CONSENT_TEXT_SHA256

    uuid_value = _create(runtime, capsys)
    main(["participant", "consent", uuid_value, "--action", "grant", *runtime])
    captured = capsys.readouterr()
    assert "PERSETUJUAN PEMROSESAN DATA BIOMETRIK SUARA" in captured.out
    assert CONSENT_TEXT_SHA256 in captured.out
    # The required phrase is on stderr, so a piped stdout still shows the operator
    # what to do next.
    assert "SAYA SETUJU" in captured.err
    assert "Nothing has been recorded" in captured.err


@pytest.mark.parametrize("wrong", ["yes", "SAYA  SETUJU", "saya setuju", ""])
def test_a_near_miss_confirmation_phrase_is_refused(
    migrated, runtime, factory, capsys, wrong: str
) -> None:
    uuid_value = _create(runtime, capsys)
    code = main(
        ["participant", "consent", uuid_value, "--action", "grant",
         "--confirm", wrong, *runtime]
    )
    assert code == 1
    conn = factory()
    try:
        assert conn.execute("SELECT count(*) FROM consent_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_granting_with_the_exact_phrase_records_one_event(
    migrated, runtime, factory, capsys
) -> None:
    uuid_value = _create(runtime, capsys)
    code, out = _run(
        ["participant", "consent", uuid_value, "--action", "grant",
         "--confirm", "SAYA SETUJU", *runtime],
        capsys,
    )
    assert code == 0
    assert "Consent recorded" in out
    conn = factory()
    try:
        rows = conn.execute("SELECT action FROM consent_events").fetchall()
    finally:
        conn.close()
    assert [r["action"] for r in rows] == ["GRANTED"]


def test_revoking_without_the_phrase_refuses_and_explains(
    migrated, runtime, capsys
) -> None:
    uuid_value = _create(runtime, capsys)
    main(["participant", "consent", uuid_value, "--action", "grant",
          "--confirm", "SAYA SETUJU", *runtime])
    capsys.readouterr()
    code = main(["participant", "consent", uuid_value, "--action", "revoke", *runtime])
    assert code == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "delete this participant" in combined
    assert "UNKNOWN" in combined
    assert "NOT delete existing minutes" in combined


# ======================================================= CLI: no side effects


def test_read_only_commands_create_no_key_and_no_voiceprint(
    migrated, runtime, paths, capsys
) -> None:
    uuid_value = _create(runtime, capsys)
    for args in (
        ["participant", "list"],
        ["participant", "consent", uuid_value],
        ["participant", "enrollment", uuid_value],
        ["participant", "voiceprint", uuid_value],
        ["participant", "cleanup"],
    ):
        assert main([*args, *runtime]) == 0, args
        capsys.readouterr()

    assert not (paths.keys_dir / "voiceprint_master.dpapi").exists()
    if paths.voiceprints_dir.exists():
        assert list(paths.voiceprints_dir.glob("*.vpx")) == []


def test_enrollment_readiness_reports_model_unavailable(migrated, runtime, capsys) -> None:
    uuid_value = _create(runtime, capsys)
    code, out = _run(["participant", "enrollment", uuid_value, *runtime], capsys)
    assert code == 0
    assert "MODEL_UNAVAILABLE" in out
    assert "microphone will not be opened" in out


def test_voiceprint_status_prints_no_biometric_payload(migrated, runtime, capsys) -> None:
    uuid_value = _create(runtime, capsys)
    _code, out = _run(["participant", "voiceprint", uuid_value, *runtime], capsys)
    lowered = out.lower()
    for forbidden in ("centroid", "dispersion", "ciphertext", "nonce"):
        assert forbidden not in lowered
    assert "No biometric payload is ever printed" in out


def test_cleanup_retry_is_idempotent(migrated, runtime, capsys) -> None:
    for _ in range(3):
        code, out = _run(["participant", "cleanup", "--retry", *runtime], capsys)
        assert code == 0
        assert "0 deleted" in out


def test_participant_without_a_subcommand_explains_itself(runtime, capsys) -> None:
    assert main(["participant", *runtime]) == 1
    assert "requires a subcommand" in capsys.readouterr().err


def test_the_help_text_states_what_opens_the_microphone(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["participant", "--help"])
    out = capsys.readouterr().out
    assert "opens the microphone" in out.lower() or "None of them opens the microphone" in out
    assert "consent is a decision, not a flag" in out.lower()


# ============================================================== diagnostics


def test_checks_report_without_a_database(config: AppConfig, paths) -> None:
    keys = _by_key(enrollment_checks(config, paths))
    assert keys["enrollment_database"].status is Status.WARN
    assert "db init" in keys["enrollment_database"].detail


def test_cryptography_and_dpapi_are_reported(config: AppConfig, paths) -> None:
    keys = _by_key(enrollment_checks(config, paths))
    assert keys["cryptography_backend"].status is Status.PASS
    import platform

    if platform.system() == "Windows":
        assert keys["dpapi_available"].status is Status.PASS


def test_the_key_store_check_creates_no_key(config: AppConfig, paths) -> None:
    """The single most important side-effect guarantee in this module."""
    keys = _by_key(enrollment_checks(config, paths))
    result = keys["voiceprint_key_store"]
    assert result.status is Status.WARN
    assert "never generated implicitly" in result.detail or "created only by an explicit" in result.detail
    assert not (paths.keys_dir / "voiceprint_master.dpapi").exists()


def test_the_consent_text_is_reported_as_draft(config: AppConfig, paths) -> None:
    keys = _by_key(enrollment_checks(config, paths))
    assert keys["consent_text"].status is Status.WARN
    assert "DRAFT" in keys["consent_text"].detail
    assert "does not claim legal compliance" in keys["consent_text"].detail


def test_the_consent_text_is_a_production_failure(config: AppConfig, paths) -> None:
    keys = _by_key(enrollment_checks(config, paths, production=True))
    assert keys["consent_text"].status is Status.FAIL


def test_a_missing_model_is_warn_in_development_and_fail_in_production(
    config: AppConfig, paths
) -> None:
    development = _by_key(enrollment_checks(config, paths))["speaker_embedding_model"]
    production = _by_key(
        enrollment_checks(config, paths, production=True)
    )["speaker_embedding_model"]
    assert development.status is Status.WARN
    assert production.status is Status.FAIL
    assert "MODEL_UNAVAILABLE" in development.detail
    assert "phase-3-speaker-model-selection" in development.detail


def test_the_phase_3_tables_are_detected(config: AppConfig, paths, migrated) -> None:
    keys = _by_key(enrollment_checks(config, paths))
    assert keys["enrollment_database"].status is Status.PASS
    for table in ("meeting_participants", "consent_events", "voiceprints"):
        assert table in keys["enrollment_database"].detail


def test_an_empty_database_implies_no_voiceprint_requirement(
    config: AppConfig, paths, migrated
) -> None:
    """This replaces an assertion that demanded "0 of 9".

    That number was fabricated: with no meeting and no roster there is nobody whose
    voice needs enrolling, and naming nine invented a requirement out of the old
    hard-coded cap. Coverage is per roster and identity-aware now, so an empty
    database reports that there is nothing to enrol -- and says why.

    The production gate is not weakened by this: a fresh install still fails
    `--production` on the microphone, calibration, consent text and model checks.
    """
    keys = _by_key(enrollment_checks(config, paths, production=True))
    result = keys["production_voiceprints"]
    assert result.status is Status.WARN, result.detail
    assert result.data["coverage_available"] is True
    assert result.data["meetings"] == 0
    assert result.data["populated_rosters"] == 0
    assert "nothing to enrol" in result.detail
    assert "seats, not a number of people" in result.detail
    # No fabricated count of any kind.
    assert "of 9" not in result.detail
    assert "required_production" not in result.data


def test_integrity_and_cleanup_pass_on_an_empty_store(
    config: AppConfig, paths, migrated
) -> None:
    keys = _by_key(enrollment_checks(config, paths))
    assert keys["voiceprint_integrity"].status is Status.PASS
    assert keys["voiceprint_cleanup"].status is Status.PASS
    assert keys["active_enrollment"].status is Status.PASS
    assert keys["voiceprint_storage"].status is Status.PASS


def test_a_tampered_envelope_is_reported_without_a_key(
    config: AppConfig, paths, migrated, factory
) -> None:
    """Integrity is verifiable from the file and the row alone."""
    paths.voiceprints_dir.mkdir(parents=True, exist_ok=True)
    voiceprint_uuid = "11111111-1111-4111-8111-111111111111"
    (paths.voiceprints_dir / f"{voiceprint_uuid}.vpx").write_bytes(b"tampered")
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO participants (display_name, uuid) VALUES ('Budi', ?)",
            ("22222222-2222-4222-8222-222222222222",),
        )
        conn.execute(
            "INSERT INTO voiceprints (voiceprint_uuid, participant_id, status,"
            " envelope_relative_path, envelope_sha256, model_name, model_version,"
            " embedding_dim, sample_count) VALUES (?,1,'ACTIVE',?,?,'m','1',64,5)",
            (voiceprint_uuid, f"{voiceprint_uuid}.vpx", "a" * 64),
        )
        conn.commit()
    finally:
        conn.close()

    keys = _by_key(enrollment_checks(config, paths))
    result = keys["voiceprint_integrity"]
    assert result.status is Status.FAIL
    assert "hash mismatch" in result.detail
    # No key was needed, and none was created.
    assert not (paths.keys_dir / "voiceprint_master.dpapi").exists()


def test_an_orphan_envelope_is_reported(config: AppConfig, paths, migrated) -> None:
    paths.voiceprints_dir.mkdir(parents=True, exist_ok=True)
    (paths.voiceprints_dir / "33333333-3333-4333-8333-333333333333.vpx").write_bytes(b"x")
    result = _by_key(enrollment_checks(config, paths))["voiceprint_storage"]
    assert result.status is Status.WARN
    assert "no database row" in result.detail


def test_delete_pending_is_warn_in_development_and_fail_in_production(
    config: AppConfig, paths, migrated, factory
) -> None:
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO participants (display_name, uuid) VALUES ('Budi', ?)",
            ("44444444-4444-4444-8444-444444444444",),
        )
        conn.execute(
            "INSERT INTO voiceprints (voiceprint_uuid, participant_id, status,"
            " model_name, model_version, embedding_dim, sample_count, delete_error)"
            " VALUES ('55555555-5555-4555-8555-555555555555',1,'DELETE_PENDING',"
            "'m','1',64,5,'OSError(errno=13)')"
        )
        conn.commit()
    finally:
        conn.close()

    development = _by_key(enrollment_checks(config, paths))["voiceprint_cleanup"]
    production = _by_key(
        enrollment_checks(config, paths, production=True)
    )["voiceprint_cleanup"]
    assert development.status is Status.WARN
    assert production.status is Status.FAIL
    assert "already unusable" in development.detail


def test_the_checks_use_a_read_only_connection(config: AppConfig, paths, migrated) -> None:
    """A diagnostic must not be able to modify state, by construction."""
    import inspect

    from mom_igd.diagnostics import enrollment_checks as module

    source = inspect.getsource(module)
    assert "mode=ro" in source, "the diagnostic connection is not read-only"
    for forbidden in ("INSERT ", "UPDATE ", "DELETE FROM", "DROP "):
        assert forbidden not in source, f"the diagnostics module contains {forbidden}"


def test_doctor_includes_the_phase_3_checks(config: AppConfig, paths, migrated) -> None:
    from mom_igd.diagnostics.doctor import run_doctor

    report = run_doctor(config, data_root=str(paths.root))
    keys = {result.key for result in report.results}
    for expected in (
        "cryptography_backend",
        "dpapi_available",
        "voiceprint_key_store",
        "consent_text",
        "speaker_embedding_model",
        "enrollment_database",
        "voiceprint_integrity",
        "voiceprint_cleanup",
    ):
        assert expected in keys, f"doctor is missing the {expected} check"


def test_doctor_creates_no_key_or_voiceprint(config: AppConfig, paths, migrated) -> None:
    from mom_igd.diagnostics.doctor import run_doctor

    run_doctor(config, data_root=str(paths.root))
    run_doctor(config, data_root=str(paths.root), production=True)
    assert not (paths.keys_dir / "voiceprint_master.dpapi").exists()
    if paths.voiceprints_dir.exists():
        assert list(paths.voiceprints_dir.glob("*.vpx")) == []
