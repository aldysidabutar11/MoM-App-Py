"""Phase 3 diagnostics: participants, consent, encryption, voiceprints.

**Nothing here has a side effect.** No check opens the microphone, creates the DPAPI
master key, loads a model, decrypts a voiceprint, writes to the database or
downloads anything. That constraint is what makes ``doctor`` safe to run on a
machine mid-incident, which is exactly when it is most useful.

Two of those deserve spelling out, because they are easy to violate by accident:

* **Key-store state is read from the file, never by unwrapping.**
  :meth:`KeyProtector.describe` reports presence, envelope version and key id
  without calling DPAPI, so a diagnostic can never pull plaintext key material into
  the process.
* **Voiceprint integrity is checked without a key.** The envelope hash, schema and
  participant/model bindings are all verifiable from the file and the row. A check
  that had to decrypt would mean any caller could obtain a biometric template.

Development and production readiness are deliberately different, as in Phase 2. A
missing model, a laptop microphone and an unreviewed consent text are all ``WARN``
in the default run and ``FAIL`` under ``--production``: they block a real meeting in a
real room, and none of them blocks development.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mom_igd.diagnostics.model import CheckResult, Status

if TYPE_CHECKING:  # pragma: no cover - typing only
    import sqlite3

    from mom_igd.config import AppConfig
    from mom_igd.paths import RuntimePaths

__all__ = ["enrollment_checks"]

# There is deliberately NO module-level "minimum voiceprints" constant.
#
# Two earlier versions had one. The first hard-coded nine. The second used the
# largest configured roster *capacity*. Both answered the wrong question: the number
# of voiceprints a deployment needs is the number of **actual active roster members**,
# not a global constant and not a seat count. A meeting with capacity 15 and ten
# people on its roster needs ten templates; demanding fifteen invents five people who
# do not exist.
#
# Coverage is computed per roster in `_roster_coverage()`, by joining each roster
# member to that same participant's own live voiceprint. A fallback number would only
# be reachable when there is no roster at all -- and in that case the honest answer is
# that nothing is required yet, which needs no constant.


def enrollment_checks(
    config: AppConfig, paths: RuntimePaths, *, production: bool = False
) -> list[CheckResult]:
    """Run every Phase 3 check. Opens no device, creates no key, writes nothing."""
    results: list[CheckResult] = [_check_cryptography(), _check_dpapi()]
    results.append(_check_key_store(paths))
    results.append(_check_consent_text(production=production))
    results.append(_check_speaker_model(config, paths, production=production))

    database = _open_readonly(config, paths)
    if database is None:
        results.append(
            CheckResult(
                key="enrollment_database",
                title="Phase 3 tables",
                status=Status.WARN,
                detail=(
                    "The database does not exist yet, so participant, consent and "
                    "voiceprint state cannot be reported. Run "
                    "`python -m mom_igd db init`."
                ),
                required_in_phase="3",
            )
        )
        return results

    try:
        results.append(_check_schema(database))
        results.extend(_check_registry_counts(database, production=production))
        results.append(_check_voiceprint_integrity(database, paths))
        results.append(_check_pending_cleanup(database, production=production))
        results.append(_check_active_enrollment(database))
        results.append(_check_stray_envelopes(database, paths))
    finally:
        database.close()
    return results


# ---------------------------------------------------------------------------
# Crypto and key material
# ---------------------------------------------------------------------------


def _check_cryptography() -> CheckResult:
    try:
        import cryptography
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            key="cryptography_backend",
            title="Cryptography backend (AES-256-GCM)",
            status=Status.FAIL,
            detail=(
                f"`cryptography` is not importable ({type(exc).__name__}). Voiceprints "
                "cannot be encrypted or read. Install it with: "
                r".venv\Scripts\python.exe -m pip install -r requirements.txt"
            ),
            required_in_phase="3",
        )
    return CheckResult(
        key="cryptography_backend",
        title="Cryptography backend (AES-256-GCM)",
        status=Status.PASS,
        detail=f"cryptography {cryptography.__version__}, AES-GCM available.",
        required_in_phase="3",
        data={"version": cryptography.__version__},
    )


def _check_dpapi() -> CheckResult:
    """Report DPAPI availability without protecting or unprotecting anything."""
    from mom_igd.enrollment.keys import dpapi_available

    available, detail = dpapi_available()
    return CheckResult(
        key="dpapi_available",
        title="Windows DPAPI (protects the master key)",
        status=Status.PASS if available else Status.FAIL,
        detail=(
            detail
            if available
            else (
                f"{detail} The voiceprint master key cannot be protected, so "
                "enrollment is unavailable."
            )
        ),
        required_in_phase="3",
        data={"available": available},
    )


def _check_key_store(paths: RuntimePaths) -> CheckResult:
    """Report key-store state. **Never creates a key and never unwraps one.**"""
    from mom_igd.enrollment.keys import KeyProtector

    payload = KeyProtector(paths.keys_dir).describe()
    present = bool(payload.get("key_present"))
    if not present:
        return CheckResult(
            key="voiceprint_key_store",
            title="Voiceprint master key",
            status=Status.WARN,
            detail=(
                "No master key exists yet. That is the correct state before the "
                "first enrollment: the key is created only by an explicit "
                "enrollment, never by `doctor`, an import or application startup."
            ),
            required_in_phase="3",
            data=payload,
        )
    if payload.get("readable") is False:
        return CheckResult(
            key="voiceprint_key_store",
            title="Voiceprint master key",
            status=Status.FAIL,
            detail=(
                "A master key file exists but cannot be read "
                f"({payload.get('error', 'unknown error')}). Existing voiceprints "
                "cannot be decrypted. Do not delete it: a replacement key cannot "
                "recover them, and the participants would have to be re-enrolled."
            ),
            required_in_phase="3",
            data=payload,
        )
    return CheckResult(
        key="voiceprint_key_store",
        title="Voiceprint master key",
        status=Status.PASS,
        detail=(
            f"Protected key present (id {payload.get('key_id')}, envelope v"
            f"{payload.get('envelope_version')}, created {payload.get('created_utc')}). "
            "Not unwrapped by this check."
        ),
        required_in_phase="3",
        data=payload,
    )


# ---------------------------------------------------------------------------
# Consent and model provenance
# ---------------------------------------------------------------------------


def _check_consent_text(*, production: bool) -> CheckResult:
    from mom_igd.enrollment.consent import (
        CONSENT_PURPOSE,
        CONSENT_TEXT_SHA256,
        CONSENT_VERSION,
    )

    draft = CONSENT_VERSION.endswith("-draft")
    data = {
        "version": CONSENT_VERSION,
        "sha256": CONSENT_TEXT_SHA256,
        "purpose": CONSENT_PURPOSE,
        "review_pending": draft,
    }
    if not draft:
        return CheckResult(
            key="consent_text",
            title="Biometric consent text",
            status=Status.PASS,
            detail=(
                f"Consent text {CONSENT_VERSION} (sha256 {CONSENT_TEXT_SHA256[:12]}…) "
                "is marked reviewed."
            ),
            required_in_phase="3",
            data=data,
        )
    return CheckResult(
        key="consent_text",
        title="Biometric consent text",
        status=Status.FAIL if production else Status.WARN,
        detail=(
            f"Consent text is version {CONSENT_VERSION}: still a DRAFT, not yet "
            "reviewed by the organisation's legal/compliance function. Voiceprints "
            "are biometric data, so recording consent against unreviewed wording is "
            "not acceptable for production use. The application does not claim legal "
            "compliance automatically."
        ),
        required_in_phase="3",
        data=data,
    )


def _check_speaker_model(
    config: AppConfig, paths: RuntimePaths, *, production: bool
) -> CheckResult:
    """Report model provisioning. Loads nothing and downloads nothing."""
    from mom_igd.registry import RegistryError, load_registry

    try:
        registry = load_registry(config.model_registry_path)
    except RegistryError as exc:
        return CheckResult(
            key="speaker_embedding_model",
            title="Speaker embedding model",
            status=Status.FAIL,
            detail=f"The model registry is unreadable: {exc}",
            required_in_phase="3",
        )

    entries = [
        entry
        for entry in getattr(registry, "models", [])
        if str(getattr(entry, "slot", "")).lower() in {"speaker", "speaker_embedding"}
    ]
    if not entries:
        return CheckResult(
            key="speaker_embedding_model",
            title="Speaker embedding model",
            status=Status.FAIL if production else Status.WARN,
            detail=(
                "No speaker-embedding model is declared in models/registry.json, so "
                "no voiceprint can be produced. This is the expected state: the model "
                "has not been selected or approved yet (see "
                "docs/phase-3-speaker-model-selection.md). Enrollment refuses with "
                "MODEL_UNAVAILABLE rather than falling back to a stand-in."
            ),
            required_in_phase="3",
            data={"declared": 0},
        )

    missing: list[str] = []
    for entry in entries:
        relative = getattr(entry, "path", None)
        if not relative:
            missing.append(f"{getattr(entry, 'name', '?')}: no path declared")
            continue
        candidate = paths.models_dir / str(relative)
        if not candidate.is_file():
            missing.append(f"{getattr(entry, 'name', '?')}: artefact not present")
    if missing:
        return CheckResult(
            key="speaker_embedding_model",
            title="Speaker embedding model",
            status=Status.FAIL if production else Status.WARN,
            detail=(
                f"{len(entries)} model(s) declared but not provisioned: "
                f"{'; '.join(missing)}. The runtime never downloads a model; "
                "provisioning is a separate, deliberate step."
            ),
            required_in_phase="3",
            data={"declared": len(entries), "missing": missing},
        )
    return CheckResult(
        key="speaker_embedding_model",
        title="Speaker embedding model",
        status=Status.PASS,
        detail=(
            f"{len(entries)} speaker model artefact(s) present. The SHA-256 is "
            "verified at load time, not here -- hashing a large artefact on every "
            "diagnostic run would make `doctor` slow for no benefit."
        ),
        required_in_phase="3",
        data={"declared": len(entries)},
    )


# ---------------------------------------------------------------------------
# Database-backed checks (all read-only)
# ---------------------------------------------------------------------------


def _open_readonly(config: AppConfig, paths: RuntimePaths) -> sqlite3.Connection | None:
    """Open the database read-only, or return ``None`` if it is not there.

    Read-only on purpose: a diagnostic must not be able to modify state, and
    ``mode=ro`` makes that a property of the connection rather than of discipline.
    """
    import sqlite3

    path = paths.database_path(config.database.filename)
    if not path.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.DatabaseError:
        return None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Needed because `doctor` runs against whatever schema is on disk.

    A schema-3 database has no ``meetings.participant_capacity``, and `doctor` must
    report that honestly rather than raising -- it is the tool an operator runs
    *before* migrating.
    """
    if not _table_exists(conn, table):
        return False
    return any(
        row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})")
    )


def _check_schema(conn: sqlite3.Connection) -> CheckResult:
    expected = ("meeting_participants", "consent_events", "enrollment_sessions", "voiceprints")
    missing = [name for name in expected if not _table_exists(conn, name)]
    if missing:
        return CheckResult(
            key="enrollment_database",
            title="Phase 3 tables",
            status=Status.FAIL,
            detail=(
                f"Missing table(s): {', '.join(missing)}. Migration 0003 has not been "
                "applied. Run `python -m mom_igd db init`."
            ),
            required_in_phase="3",
            data={"missing": missing},
        )
    return CheckResult(
        key="enrollment_database",
        title="Phase 3 tables",
        status=Status.PASS,
        detail="meeting_participants, consent_events, enrollment_sessions, voiceprints present.",
        required_in_phase="3",
    )


def _count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def _check_registry_counts(
    conn: sqlite3.Connection, *, production: bool
) -> list[CheckResult]:
    if not _table_exists(conn, "voiceprints"):
        return []
    participants = _count(conn, "SELECT count(*) FROM participants")
    active = _count(conn, "SELECT count(*) FROM participants WHERE is_active = 1")
    consented = _count(
        conn,
        "SELECT count(*) FROM (SELECT participant_id, action FROM consent_events e "
        "WHERE id = (SELECT max(id) FROM consent_events WHERE participant_id = "
        "e.participant_id)) WHERE action = 'GRANTED'",
    )
    development = _count(
        conn, "SELECT count(*) FROM voiceprints WHERE status = 'DEVELOPMENT_ONLY'"
    )
    production_ready = _count(
        conn,
        "SELECT count(*) FROM voiceprints WHERE status = 'ACTIVE' "
        "AND production_eligible = 1",
    )
    coverage = _roster_coverage(conn)
    data = {
        "participants": participants,
        "active_participants": active,
        "active_consent": consented,
        "development_only_voiceprints": development,
        "production_voiceprints": production_ready,
        **coverage,
    }

    registry = CheckResult(
        key="participant_registry",
        title="Participant registry",
        status=Status.PASS if participants else Status.WARN,
        detail=(
            f"{participants} participant(s) registered, {active} active, "
            f"{consented} with active biometric consent."
            if participants
            else "No participant is registered yet."
        ),
        required_in_phase="3",
        data=data,
    )
    return [registry, _coverage_result(coverage, development, production=production)]


# ---------------------------------------------------------------------------
# Roster coverage
# ---------------------------------------------------------------------------
#
# WHAT THIS MEASURES, AND WHY IT IS NOT A COUNT
#
# The question "is this deployment ready?" is per roster and per person, not a
# total. An earlier version compared the global number of production-eligible
# voiceprints against the largest configured *capacity*, and that was wrong twice:
#
#   * Capacity is the number of seats, not the number of attendees. A meeting with
#     capacity 15 and ten people on its roster needs ten voiceprints, not fifteen.
#     Counting empty seats as missing templates invents work that does not exist.
#   * A total says nothing about *whose* voice is enrolled. Fifteen voiceprints
#     belonging to people who are not on this meeting's roster would have satisfied
#     a count while leaving every actual attendee unrecognised.
#
# Coverage is therefore computed by joining roster membership to that same
# participant's live voiceprint. A roster member counts as covered only when all of
# these hold: the participant is active, the membership is active, their latest
# consent event is a grant, and they own a voiceprint that is ACTIVE and
# production_eligible.
#
# WHAT THIS DELIBERATELY DOES NOT DO
#
# The schema has no signal that distinguishes an upcoming meeting from a historical
# one -- `meetings` has no state column by design (see migration 0001), and adding
# one to make a diagnostic prettier would be the wrong reason to change the schema.
# So this does not guess which meeting matters. It reports every roster, and it
# reports the worst one, and it says which limitation applies. Inventing a
# "current meeting" would produce a confident answer with nothing behind it.
#
# No display name appears in any result. A diagnostic report gets pasted into
# tickets and chat; participant UUIDs are enough to act on, and a name is personal
# data that does not need to travel.


def _roster_coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    """Per-roster, identity-aware voiceprint coverage. Reads only."""
    if not _column_exists(conn, "meetings", "participant_capacity"):
        return {
            "coverage_available": False,
            "coverage_reason": "migration_0004_pending",
            "rosters": [],
        }
    if not _table_exists(conn, "meeting_participants"):
        return {
            "coverage_available": False,
            "coverage_reason": "migration_0003_pending",
            "rosters": [],
        }

    rows = conn.execute(
        """
        SELECT m.uuid                AS meeting_uuid,
               m.participant_capacity AS capacity,
               -- count(p.id), NOT count(mp.id). A participant who has been
               -- deactivated is not an active roster member even if a stale
               -- membership row still says is_active = 1. Counting the membership
               -- row would report them as somebody needing a voiceprint.
               count(p.id)           AS roster_size,
               coalesce(sum(
                   CASE WHEN v.id IS NOT NULL THEN 1 ELSE 0 END
               ), 0)                 AS covered
          FROM meetings m
          LEFT JOIN meeting_participants mp
                 ON mp.meeting_id = m.id
                AND mp.is_active = 1
          LEFT JOIN participants p
                 ON p.id = mp.participant_id
                AND p.is_active = 1
          -- The voiceprint must belong to this same participant, be live, and be
          -- production eligible. A DEVELOPMENT_ONLY template does not count.
          LEFT JOIN voiceprints v
                 ON v.participant_id = p.id
                AND v.status = 'ACTIVE'
                AND v.production_eligible = 1
          -- ...and that participant's most recent consent event must be a grant.
                AND (
                    SELECT ce.action FROM consent_events ce
                     WHERE ce.participant_id = p.id
                     ORDER BY ce.id DESC LIMIT 1
                ) = 'GRANTED'
         GROUP BY m.id
         ORDER BY m.id
        """
    ).fetchall()

    rosters = [
        {
            "meeting_uuid": str(r["meeting_uuid"] or ""),
            "capacity": int(r["capacity"]),
            "roster_size": int(r["roster_size"]),
            "covered": int(r["covered"]),
            "missing": max(0, int(r["roster_size"]) - int(r["covered"])),
        }
        for r in rows
    ]
    # Only rosters with somebody on them can be incomplete. An empty roster is not
    # a shortfall -- nobody has been put in the room yet.
    populated = [r for r in rosters if r["roster_size"] > 0]
    incomplete = [r for r in populated if r["missing"] > 0]
    worst = max(incomplete, key=lambda r: r["missing"], default=None)
    return {
        "coverage_available": True,
        "coverage_reason": None,
        "meetings": len(rosters),
        "populated_rosters": len(populated),
        "incomplete_rosters": len(incomplete),
        "worst_roster": worst,
        "rosters": rosters,
    }


def _coverage_result(
    coverage: dict[str, Any], development: int, *, production: bool
) -> CheckResult:
    key, title = "production_voiceprints", "Roster voiceprint coverage"
    limitation = (
        " Reported per roster: the schema carries no upcoming/historical meeting "
        "state, so no single meeting is assumed to be the relevant one."
    )

    if not coverage["coverage_available"]:
        pending = (
            "0004" if coverage["coverage_reason"] == "migration_0004_pending" else "0003"
        )
        return CheckResult(
            key=key,
            title=title,
            status=Status.WARN,
            detail=(
                f"Coverage cannot be computed: migration {pending} has not been "
                "applied, so roster capacity or membership is not in this database "
                "yet. Run `python -m mom_igd db init`."
            ),
            required_in_phase="3",
            data=coverage,
        )

    populated = coverage["populated_rosters"]
    if populated == 0:
        detail = (
            f"{coverage['meetings']} meeting(s), none with anybody on its roster, so "
            "there is nothing to enrol yet. No voiceprint requirement is implied by "
            "an empty roster -- capacity is a number of seats, not a number of people."
        )
        return CheckResult(
            key=key,
            title=title,
            status=Status.WARN,
            detail=detail,
            required_in_phase="3",
            data=coverage,
        )

    incomplete = coverage["incomplete_rosters"]
    if incomplete == 0:
        return CheckResult(
            key=key,
            title=title,
            status=Status.PASS,
            detail=(
                f"Every active member of {populated} populated roster(s) has a "
                "production-eligible voiceprint of their own." + limitation
            ),
            required_in_phase="3",
            data=coverage,
        )

    worst = coverage["worst_roster"] or {}
    return CheckResult(
        key=key,
        title=title,
        status=Status.FAIL if production else Status.WARN,
        detail=(
            f"{incomplete} of {populated} populated roster(s) have members without a "
            f"production-eligible voiceprint. Worst: meeting "
            f"{worst.get('meeting_uuid', '?')} covers {worst.get('covered')} of "
            f"{worst.get('roster_size')} active member(s) "
            f"({worst.get('missing')} missing; capacity {worst.get('capacity')} is "
            f"seats, not attendees). {development} template(s) are DEVELOPMENT_ONLY, "
            "captured on a microphone Windows does not report as USB -- usable for "
            "development only. An unenrolled voice is labelled UNKNOWN rather than "
            "dropped, so this blocks accuracy, never recording." + limitation
        ),
        required_in_phase="3",
        data=coverage,
    )


def _check_voiceprint_integrity(
    conn: sqlite3.Connection, paths: RuntimePaths
) -> CheckResult:
    """Verify every live envelope's hash. **No key is unwrapped.**"""
    if not _table_exists(conn, "voiceprints"):
        return CheckResult(
            key="voiceprint_integrity",
            title="Voiceprint integrity",
            status=Status.WARN,
            detail="Migration 0003 has not been applied.",
            required_in_phase="3",
        )
    from mom_igd.enrollment.cipher import sealed_sha256

    rows = conn.execute(
        "SELECT voiceprint_uuid, status, envelope_relative_path, envelope_sha256 "
        "FROM voiceprints WHERE status IN ('ACTIVE','DEVELOPMENT_ONLY','PENDING_WRITE')"
    ).fetchall()
    already_failed = _count(
        conn, "SELECT count(*) FROM voiceprints WHERE status = 'INTEGRITY_FAILED'"
    )

    problems: list[str] = []
    checked = 0
    for row in rows:
        relative = row["envelope_relative_path"]
        if not relative:
            problems.append(f"{row['voiceprint_uuid']}: no envelope recorded")
            continue
        path = paths.voiceprints_dir / str(relative)
        if not path.is_file():
            problems.append(f"{row['voiceprint_uuid']}: envelope missing from storage")
            continue
        checked += 1
        if sealed_sha256(path.read_bytes()) != str(row["envelope_sha256"]):
            problems.append(f"{row['voiceprint_uuid']}: envelope hash mismatch")

    data = {
        "checked": checked,
        "problems": problems,
        "already_integrity_failed": already_failed,
    }
    if problems or already_failed:
        return CheckResult(
            key="voiceprint_integrity",
            title="Voiceprint integrity",
            status=Status.FAIL,
            detail=(
                f"{len(problems)} live voiceprint(s) failed verification"
                + (f" and {already_failed} are already INTEGRITY_FAILED" if already_failed else "")
                + ". A template that cannot be authenticated must never be compared "
                "against a voice; re-enroll the affected participants. Details: "
                + ("; ".join(problems) if problems else "see the voiceprints table")
            ),
            required_in_phase="3",
            data=data,
        )
    return CheckResult(
        key="voiceprint_integrity",
        title="Voiceprint integrity",
        status=Status.PASS,
        detail=(
            f"{checked} live voiceprint envelope(s) match their recorded SHA-256. "
            "Verified from the files without unwrapping the master key."
        ),
        required_in_phase="3",
        data=data,
    )


def _check_pending_cleanup(
    conn: sqlite3.Connection, *, production: bool
) -> CheckResult:
    if not _table_exists(conn, "voiceprints"):
        return CheckResult(
            key="voiceprint_cleanup",
            title="Pending voiceprint cleanup",
            status=Status.WARN,
            detail="Migration 0003 has not been applied.",
            required_in_phase="3",
        )
    rows = conn.execute(
        "SELECT voiceprint_uuid, delete_error FROM voiceprints "
        "WHERE status = 'DELETE_PENDING'"
    ).fetchall()
    pending_write = _count(
        conn, "SELECT count(*) FROM voiceprints WHERE status = 'PENDING_WRITE'"
    )
    data = {
        "delete_pending": [str(r["voiceprint_uuid"]) for r in rows],
        "pending_write": pending_write,
    }
    if not rows and not pending_write:
        return CheckResult(
            key="voiceprint_cleanup",
            title="Pending voiceprint cleanup",
            status=Status.PASS,
            detail="No voiceprint is awaiting deletion or finalisation.",
            required_in_phase="3",
            data=data,
        )
    parts: list[str] = []
    if rows:
        parts.append(
            f"{len(rows)} voiceprint(s) are DELETE_PENDING: consent was withdrawn but "
            "the encrypted file could not be removed. They are already unusable. Run "
            "`python -m mom_igd participant cleanup-retry`."
        )
    if pending_write:
        parts.append(
            f"{pending_write} voiceprint(s) are PENDING_WRITE, meaning a save was "
            "interrupted. They are not usable and recovery will resolve them."
        )
    return CheckResult(
        key="voiceprint_cleanup",
        title="Pending voiceprint cleanup",
        status=Status.FAIL if production else Status.WARN,
        detail=" ".join(parts),
        required_in_phase="3",
        data=data,
    )


def _check_active_enrollment(conn: sqlite3.Connection) -> CheckResult:
    if not _table_exists(conn, "enrollment_sessions"):
        return CheckResult(
            key="active_enrollment",
            title="Enrollment sessions",
            status=Status.WARN,
            detail="Migration 0003 has not been applied.",
            required_in_phase="3",
        )
    live = conn.execute(
        "SELECT session_uuid, state FROM enrollment_sessions WHERE state IN "
        "('CREATED','CONSENT_REQUIRED','READY','CAPTURING','VALIDATING','EMBEDDING',"
        "'ENCRYPTING')"
    ).fetchall()
    if not live:
        return CheckResult(
            key="active_enrollment",
            title="Enrollment sessions",
            status=Status.PASS,
            detail="No enrollment is in progress.",
            required_in_phase="3",
        )
    return CheckResult(
        key="active_enrollment",
        title="Enrollment sessions",
        status=Status.WARN,
        detail=(
            f"{len(live)} enrollment session(s) are recorded as still in progress "
            f"({', '.join(str(r['state']) for r in live)}). If no wizard is open this "
            "is a session left behind by a crash; it holds no audio, and cancelling "
            "it frees the shared capture lock."
        ),
        required_in_phase="3",
        data={"sessions": [str(r["session_uuid"]) for r in live]},
    )


def _check_stray_envelopes(
    conn: sqlite3.Connection, paths: RuntimePaths
) -> CheckResult:
    """Look for envelope files with no row, and quarantined evidence."""
    directory = paths.voiceprints_dir
    if not directory.is_dir():
        return CheckResult(
            key="voiceprint_storage",
            title="Voiceprint storage",
            status=Status.PASS,
            detail="No voiceprint directory yet; nothing has been enrolled.",
            required_in_phase="3",
        )
    known = {
        str(row["voiceprint_uuid"])
        for row in conn.execute("SELECT voiceprint_uuid FROM voiceprints")
    } if _table_exists(conn, "voiceprints") else set()

    orphans: list[str] = []
    temporaries: list[str] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        if path.name.endswith(".vpx.tmp"):
            temporaries.append(path.name)
        elif path.name.endswith(".vpx"):
            if path.name[: -len(".vpx")] not in known:
                orphans.append(path.name)
    quarantine = directory / "quarantine"
    quarantined = (
        len([p for p in quarantine.iterdir() if p.suffix == ".vpx"])
        if quarantine.is_dir()
        else 0
    )
    data = {
        "orphans": orphans,
        "temporaries": temporaries,
        "quarantined": quarantined,
    }
    if not orphans and not temporaries and not quarantined:
        return CheckResult(
            key="voiceprint_storage",
            title="Voiceprint storage",
            status=Status.PASS,
            detail="No orphan, temporary or quarantined voiceprint file.",
            required_in_phase="3",
            data=data,
        )
    parts: list[str] = []
    if orphans:
        parts.append(
            f"{len(orphans)} envelope file(s) have no database row and cannot be "
            "attributed to a participant or a consent record."
        )
    if temporaries:
        parts.append(f"{len(temporaries)} temporary envelope(s) from an interrupted save.")
    if quarantined:
        parts.append(
            f"{quarantined} file(s) are in quarantine as evidence. They are never "
            "used and are kept deliberately rather than deleted."
        )
    return CheckResult(
        key="voiceprint_storage",
        title="Voiceprint storage",
        status=Status.WARN,
        detail=" ".join(parts) + " Recovery resolves these; nothing is deleted silently.",
        required_in_phase="3",
        data=data,
    )
