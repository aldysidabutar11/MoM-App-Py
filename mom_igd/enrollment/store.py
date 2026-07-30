"""Encrypted voiceprint persistence, and honest recovery when it is interrupted.

**The problem this module exists to solve.** A voiceprint lives in two places at
once: an AES-256-GCM envelope on the filesystem and a metadata row in SQLite. The
filesystem and the database **cannot** be enrolled in one atomic transaction, and
this module does not pretend otherwise. What it does instead is order the steps so
that every possible interruption leaves a state that recovery can *identify* --
never a state it has to guess about.

**The save protocol.** Each step exists because of the crash it survives:

1.  build the biometric payload in memory
2.  seal it with AES-256-GCM
3.  write to ``<uuid>.vpx.tmp``
4.  flush + ``fsync`` -- the bytes are durable before anything references them
5.  hash the envelope **from the buffer that was written**
6.  insert the row as ``PENDING_WRITE``, carrying the expected path and hash
7.  atomic ``os.replace`` into ``<uuid>.vpx``
8.  re-read the final file and verify size and hash **from disk**
9.  only now set the row ``ACTIVE`` / ``DEVELOPMENT_ONLY``
10. audit event

Step 6 before step 7 is the load-bearing choice. It means a crash can leave a
pending row whose file has not yet appeared -- recoverable, because the row says
what to look for -- but never a finished file that nothing knows about. And step 8
before step 9 means a row is never marked usable on the strength of what we
*intended* to write; only on what is actually readable.

**What recovery can conclude, and what it does about it.**

===========================  =============================================
Observed                     Conclusion
===========================  =============================================
temp file, no row            abandoned save -> quarantine the file
pending row, final valid     rename survived -> finalise the row
pending row, final missing   crash before rename -> mark FAILED, clean temp
pending row, hash mismatch   truncated or altered -> INTEGRITY_FAILED
active row, file missing     envelope lost -> INTEGRITY_FAILED
active row, hash mismatch    tampered -> INTEGRITY_FAILED
final file, no row at all    orphan -> quarantine
===========================  =============================================

Nothing is ever deleted outright by recovery: an ambiguous file is moved to
``voiceprints/quarantine/`` with a reason, exactly as Phase 2 handles an ambiguous
audio partial. Deleting evidence to make a directory tidy is not a trade this
project makes.

**Deletion on revocation is different, and deliberately destructive.** When
consent is withdrawn the envelope must actually go. If the unlink fails, the row
becomes ``DELETE_PENDING``: still unusable, and retryable. It is never left
``ACTIVE`` because a filesystem error occurred.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from mom_igd.audit import record_event
from mom_igd.db.connection import maybe_transaction
from mom_igd.enrollment.cipher import (
    CipherError,
    ModelIdentity,
    VoiceprintCipher,
    sealed_sha256,
)

__all__ = [
    "QUARANTINE_DIRNAME",
    "PayloadSchemaError",
    "RecoveryReport",
    "VerifyOutcome",
    "VoiceprintStatus",
    "VoiceprintStore",
    "VoiceprintStoreError",
]

QUARANTINE_DIRNAME: Final[str] = "quarantine"
TEMP_SUFFIX: Final[str] = ".vpx.tmp"
FINAL_SUFFIX: Final[str] = ".vpx"

PAYLOAD_SCHEMA: Final[int] = 1
"""Version of the *decrypted* payload shape, inside the envelope."""

_REQUIRED_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "payload_schema",
        "centroid",
        "dispersion",
        "sample_count",
        "embedding_dim",
        "dtype",
    }
)


class VoiceprintStatus(StrEnum):
    """Must stay in sync with the CHECK constraint in migration 0003."""

    PENDING_WRITE = "PENDING_WRITE"
    ACTIVE = "ACTIVE"
    DEVELOPMENT_ONLY = "DEVELOPMENT_ONLY"
    SUPERSEDED = "SUPERSEDED"
    RE_ENROLL_REQUIRED = "RE_ENROLL_REQUIRED"
    REVOKED = "REVOKED"
    DELETE_PENDING = "DELETE_PENDING"
    INTEGRITY_FAILED = "INTEGRITY_FAILED"

    @property
    def usable(self) -> bool:
        """Whether Phase 6 would be permitted to compare against this template.

        Everything not explicitly usable is unusable. Stated this way round on
        purpose: a new status added later defaults to *not* usable, which is the
        safe direction for biometric data.
        """
        return self in {VoiceprintStatus.ACTIVE, VoiceprintStatus.DEVELOPMENT_ONLY}

    @property
    def production_eligible(self) -> bool:
        return self is VoiceprintStatus.ACTIVE


class VoiceprintStoreError(RuntimeError):
    """A store operation was refused. Never contains key material or a vector."""


class PayloadSchemaError(VoiceprintStoreError):
    """A decrypted payload did not have the expected shape."""


@dataclass(frozen=True, slots=True)
class VerifyOutcome:
    """Result of verifying one voiceprint. Carries no biometric data."""

    voiceprint_uuid: str
    ok: bool
    status: str
    checks: dict[str, bool]
    problems: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "voiceprint_uuid": self.voiceprint_uuid,
            "ok": self.ok,
            "status": self.status,
            "checks": dict(self.checks),
            "problems": list(self.problems),
        }


@dataclass(slots=True)
class RecoveryReport:
    """What a recovery sweep found and did."""

    finalised: int = 0
    marked_failed: int = 0
    marked_integrity_failed: int = 0
    quarantined_temp: int = 0
    quarantined_orphan: int = 0
    cleanup_retried: int = 0
    cleanup_still_pending: int = 0
    notes: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.notes is None:
            self.notes = []

    @property
    def changed(self) -> bool:
        return any(
            (
                self.finalised,
                self.marked_failed,
                self.marked_integrity_failed,
                self.quarantined_temp,
                self.quarantined_orphan,
                self.cleanup_retried,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "finalised": self.finalised,
            "marked_failed": self.marked_failed,
            "marked_integrity_failed": self.marked_integrity_failed,
            "quarantined_temp": self.quarantined_temp,
            "quarantined_orphan": self.quarantined_orphan,
            "cleanup_retried": self.cleanup_retried,
            "cleanup_still_pending": self.cleanup_still_pending,
            "changed": self.changed,
            "notes": list(self.notes),
        }


def validate_payload(payload: dict[str, Any]) -> None:
    """Check the decrypted payload's *shape*. Never logs its contents."""
    missing = _REQUIRED_PAYLOAD_KEYS - set(payload)
    if missing:
        raise PayloadSchemaError(
            f"Voiceprint payload is missing required field(s): {sorted(missing)}."
        )
    if payload.get("payload_schema") != PAYLOAD_SCHEMA:
        raise PayloadSchemaError(
            f"Unsupported voiceprint payload schema {payload.get('payload_schema')!r}; "
            f"this build understands {PAYLOAD_SCHEMA}."
        )
    centroid = payload.get("centroid")
    if not isinstance(centroid, list) or not centroid:
        raise PayloadSchemaError("Voiceprint payload centroid must be a non-empty list.")
    declared = payload.get("embedding_dim")
    if not isinstance(declared, int) or declared != len(centroid):
        raise PayloadSchemaError(
            f"Voiceprint payload declares embedding_dim={declared!r} but the centroid "
            f"has {len(centroid)} values."
        )
    count = payload.get("sample_count")
    if not isinstance(count, int) or count < 1:
        raise PayloadSchemaError(
            f"Voiceprint payload sample_count must be a positive integer, got {count!r}."
        )


class VoiceprintStore:
    """Persists and recovers encrypted voiceprints.

    Holds no key material of its own: a :class:`VoiceprintCipher` is supplied per
    operation, so the master key is only in memory while an explicit enrollment or
    verification needs it.
    """

    def __init__(self, voiceprints_dir: Path, connection_factory: Any) -> None:
        self._dir = Path(voiceprints_dir)
        self._connect = connection_factory

    # -- paths --------------------------------------------------------------

    def _final_path(self, voiceprint_uuid: str) -> Path:
        return self._dir / f"{voiceprint_uuid}{FINAL_SUFFIX}"

    def _temp_path(self, voiceprint_uuid: str) -> Path:
        return self._dir / f"{voiceprint_uuid}{TEMP_SUFFIX}"

    def _relative(self, voiceprint_uuid: str) -> str:
        """Path stored in the database: relative, so the data root can move."""
        return f"{voiceprint_uuid}{FINAL_SUFFIX}"

    def _quarantine(self, path: Path, reason: str) -> Path:
        """Move a file aside with its reason. Never deletes evidence."""
        target_dir = self._dir / QUARANTINE_DIRNAME
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        counter = 1
        while target.exists():
            target = target_dir / f"{path.name}.{counter}"
            counter += 1
        os.replace(path, target)
        target.with_suffix(target.suffix + ".reason.txt").write_text(
            f"{reason}\n", encoding="utf-8"
        )
        return target

    # -- save ---------------------------------------------------------------

    def save(
        self,
        *,
        cipher: VoiceprintCipher,
        payload: dict[str, Any],
        voiceprint_uuid: str,
        participant_id: int,
        model: ModelIdentity,
        enrollment_session_id: int | None,
        consent_event_id: int | None,
        development_only: bool,
        device_fingerprint: str | None,
        device_transport: str | None,
        sample_rate_hz: int | None,
        channels: int | None,
        quality_verdict: str | None,
        min_pair_cosine: float | None,
        preprocessing_id: str | None = None,
    ) -> dict[str, Any]:
        """Seal, persist and activate one voiceprint. See the module docstring.

        ``development_only`` decides the terminal status. It is passed in rather
        than inferred here because the decision belongs to the enrollment service,
        which knows whether the capture device was a verified USB microphone.
        """
        validate_payload(payload)
        if self._final_path(voiceprint_uuid).exists():
            raise VoiceprintStoreError(
                f"A voiceprint envelope for {voiceprint_uuid} already exists. "
                "Refusing to overwrite: a new enrollment gets a new identifier, and "
                "replacing an envelope in place would destroy a template that may "
                "still be referenced."
            )

        self._dir.mkdir(parents=True, exist_ok=True)
        envelope = cipher.seal(
            payload,
            voiceprint_uuid=voiceprint_uuid,
            participant_id=participant_id,
            model=model,
        )
        temp = self._temp_path(voiceprint_uuid)
        final = self._final_path(voiceprint_uuid)

        # Steps 3-5: durable bytes, then the hash of what was written.
        with temp.open("wb") as handle:
            handle.write(envelope)
            handle.flush()
            os.fsync(handle.fileno())
        digest = sealed_sha256(envelope)
        size = len(envelope)

        # Step 6: claim the row BEFORE the rename, so a crash is identifiable.
        conn = self._connect()
        try:
            with maybe_transaction(conn):
                cursor = conn.execute(
                    "INSERT INTO voiceprints ("
                    " voiceprint_uuid, participant_id, enrollment_session_id,"
                    " consent_event_id, status, envelope_relative_path,"
                    " envelope_schema, envelope_sha256, envelope_bytes, key_id,"
                    " model_name, model_version, model_sha256, preprocessing_id,"
                    " embedding_dim, sample_count, device_fingerprint,"
                    " device_transport, sample_rate_hz, channels,"
                    " production_eligible, quality_verdict, min_pair_cosine"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                    (
                        voiceprint_uuid,
                        participant_id,
                        enrollment_session_id,
                        consent_event_id,
                        VoiceprintStatus.PENDING_WRITE.value,
                        self._relative(voiceprint_uuid),
                        1,
                        digest,
                        size,
                        cipher.key_id,
                        model.name,
                        model.version,
                        model.sha256,
                        preprocessing_id,
                        int(payload["embedding_dim"]),
                        int(payload["sample_count"]),
                        device_fingerprint,
                        device_transport,
                        sample_rate_hz,
                        channels,
                        quality_verdict,
                        min_pair_cosine,
                    ),
                )
                voiceprint_id = int(cursor.lastrowid or 0)
        except Exception:
            # The row never landed, so the temp file references nothing. Remove it
            # rather than leaving a temp file for recovery to puzzle over.
            temp.unlink(missing_ok=True)
            raise
        finally:
            conn.close()

        try:
            # Step 7: atomic. After this, the envelope is visible under its final
            # name or not at all -- never half-renamed.
            os.replace(temp, final)

            # Step 8: verify from disk. Certifying the in-memory buffer would
            # certify what we meant to write, not what a reader will find.
            on_disk = final.read_bytes()
            if len(on_disk) != size or sealed_sha256(on_disk) != digest:
                raise VoiceprintStoreError(
                    "The voiceprint envelope on disk does not match what was "
                    "written. Refusing to activate it."
                )
        except Exception as exc:
            self._mark(
                voiceprint_id,
                VoiceprintStatus.INTEGRITY_FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
            temp.unlink(missing_ok=True)
            raise

        # Step 9: only now is it usable. Step 10 audits it.
        status = (
            VoiceprintStatus.DEVELOPMENT_ONLY
            if development_only
            else VoiceprintStatus.ACTIVE
        )
        conn = self._connect()
        try:
            with maybe_transaction(conn):
                # Any previously live template for this participant is superseded
                # first, so the one-live-per-participant index is never violated.
                superseded = self._supersede_live(conn, participant_id, voiceprint_id)
                conn.execute(
                    "UPDATE voiceprints SET status = ?, production_eligible = ?,"
                    " activated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                    " WHERE id = ?",
                    (
                        status.value,
                        1 if status.production_eligible else 0,
                        voiceprint_id,
                    ),
                )
                record_event(
                    conn,
                    category="PARTICIPANT",
                    action="VOICEPRINT_CREATED",
                    entity_type="voiceprint",
                    entity_id=voiceprint_id,
                    detail={
                        # Metadata only: identifiers, provenance, verdict.
                        "voiceprint_uuid": voiceprint_uuid,
                        "participant_id": participant_id,
                        "status": status.value,
                        "model_name": model.name,
                        "model_version": model.version,
                        "model_sha256": model.sha256,
                        "device_fingerprint": device_fingerprint,
                        "device_transport": device_transport,
                        "quality_verdict": quality_verdict,
                        "embedding_dim": int(payload["embedding_dim"]),
                        "sample_count": int(payload["sample_count"]),
                        "envelope_sha256": digest,
                        "superseded": superseded,
                    },
                )
        finally:
            conn.close()

        return {
            "voiceprint_uuid": voiceprint_uuid,
            "status": status.value,
            "production_eligible": status.production_eligible,
            "envelope_sha256": digest,
            "envelope_bytes": size,
            "superseded_count": len(superseded),
        }

    def _supersede_live(
        self, conn: sqlite3.Connection, participant_id: int, keep_id: int
    ) -> list[str]:
        """Retire any live template for this participant and delete its envelope.

        Runs inside the activation transaction. The envelope is removed because a
        superseded template is biometric data nobody has a reason to keep -- the
        replacement is what will be used, and retaining the old one widens the
        blast radius of a future disclosure for no benefit.
        """
        rows = conn.execute(
            "SELECT id, voiceprint_uuid, envelope_relative_path FROM voiceprints "
            "WHERE participant_id = ? AND id <> ? AND status IN (?, ?)",
            (
                participant_id,
                keep_id,
                VoiceprintStatus.ACTIVE.value,
                VoiceprintStatus.DEVELOPMENT_ONLY.value,
            ),
        ).fetchall()
        retired: list[str] = []
        for row in rows:
            relative = row["envelope_relative_path"]
            deleted, error = self._unlink_envelope(relative)
            # The pointer is cleared ONLY when the file really went. Nulling it on
            # failure would strand the leftover ciphertext: retry_pending_cleanup
            # would have nothing left to unlink, and the envelope would sit on disk
            # forever with no record that it exists.
            conn.execute(
                "UPDATE voiceprints SET status = ?,"
                " envelope_relative_path = CASE WHEN ? THEN NULL"
                " ELSE envelope_relative_path END,"
                " envelope_sha256 = CASE WHEN ? THEN NULL ELSE envelope_sha256 END,"
                " production_eligible = 0,"
                " superseded_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
                " deleted_at = CASE WHEN ? THEN strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                " ELSE NULL END,"
                " delete_error = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                " WHERE id = ?",
                (
                    VoiceprintStatus.SUPERSEDED.value
                    if deleted
                    else VoiceprintStatus.DELETE_PENDING.value,
                    1 if deleted else 0,
                    1 if deleted else 0,
                    1 if deleted else 0,
                    error,
                    int(row["id"]),
                ),
            )
            record_event(
                conn,
                category="PARTICIPANT",
                action="VOICEPRINT_SUPERSEDED",
                entity_type="voiceprint",
                entity_id=int(row["id"]),
                detail={
                    "voiceprint_uuid": str(row["voiceprint_uuid"]),
                    "replaced_by_id": keep_id,
                    "envelope_deleted": deleted,
                },
            )
            retired.append(str(row["voiceprint_uuid"]))
        return retired

    def _unlink_envelope(self, relative: str | None) -> tuple[bool, str | None]:
        """Delete an envelope. Returns (deleted, sanitised error).

        A missing file counts as deleted: the goal is "the ciphertext is gone", and
        it already is.
        """
        if not relative:
            return True, None
        try:
            (self._dir / str(relative)).unlink(missing_ok=True)
            return True, None
        except OSError as exc:
            # Sanitised: the type and errno, never the absolute path.
            return False, f"{type(exc).__name__}(errno={exc.errno})"

    def _mark(
        self, voiceprint_id: int, status: VoiceprintStatus, *, error: str | None = None
    ) -> None:
        conn = self._connect()
        try:
            with maybe_transaction(conn):
                conn.execute(
                    "UPDATE voiceprints SET status = ?, production_eligible = 0,"
                    " delete_error = COALESCE(?, delete_error),"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                    (status.value, error, voiceprint_id),
                )
                if status is VoiceprintStatus.INTEGRITY_FAILED:
                    record_event(
                        conn,
                        category="PARTICIPANT",
                        action="VOICEPRINT_INTEGRITY_FAILED",
                        entity_type="voiceprint",
                        entity_id=voiceprint_id,
                        detail={"reason": (error or "unspecified")[:300]},
                    )
        finally:
            conn.close()

    # -- reading ------------------------------------------------------------

    def _row(self, conn: sqlite3.Connection, voiceprint_uuid: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM voiceprints WHERE voiceprint_uuid = ?", (voiceprint_uuid,)
        ).fetchone()
        if row is None:
            raise VoiceprintStoreError(f"No voiceprint {voiceprint_uuid!r}.")
        return row

    def status_for_participant(self, participant_id: int) -> dict[str, Any]:
        """Non-biometric status summary. Safe for the API."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT voiceprint_uuid, status, production_eligible, model_name,"
                " model_version, device_fingerprint, device_transport,"
                " quality_verdict, min_pair_cosine, embedding_dim, sample_count,"
                " activated_at, revoked_at, created_at FROM voiceprints"
                " WHERE participant_id = ? ORDER BY id DESC",
                (participant_id,),
            ).fetchall()
        finally:
            conn.close()
        entries = [
            {
                "voiceprint_uuid": str(r["voiceprint_uuid"]),
                "status": str(r["status"]),
                "usable": VoiceprintStatus(str(r["status"])).usable,
                "production_eligible": bool(int(r["production_eligible"])),
                "model": {
                    "name": r["model_name"],
                    "version": r["model_version"],
                },
                "device_fingerprint": r["device_fingerprint"],
                "device_transport": r["device_transport"],
                "quality_verdict": r["quality_verdict"],
                "min_pair_cosine": r["min_pair_cosine"],
                "embedding_dim": r["embedding_dim"],
                "sample_count": r["sample_count"],
                "activated_at": r["activated_at"],
                "revoked_at": r["revoked_at"],
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]
        live = next((e for e in entries if e["usable"]), None)
        return {
            "participant_id": participant_id,
            "has_usable_voiceprint": live is not None,
            "production_eligible": bool(live and live["production_eligible"]),
            "current": live,
            "history": entries,
        }

    def load_payload(
        self,
        voiceprint_uuid: str,
        *,
        cipher: VoiceprintCipher,
        allow_unusable: bool = False,
    ) -> dict[str, Any]:
        """Decrypt one voiceprint.

        **Internal to the enrollment/identification layers.** The plaintext must not
        leave the caller: no API route returns it, and no audit event records it.
        Refuses an unusable template by default, so a revoked or integrity-failed
        record cannot be read back by accident.
        """
        conn = self._connect()
        try:
            row = self._row(conn, voiceprint_uuid)
        finally:
            conn.close()

        status = VoiceprintStatus(str(row["status"]))
        if not status.usable and not allow_unusable:
            raise VoiceprintStoreError(
                f"Voiceprint {voiceprint_uuid} is {status.value} and must not be "
                "used. Consent may have been withdrawn, the template may have been "
                "superseded, or its integrity check may have failed."
            )
        relative = row["envelope_relative_path"]
        if not relative:
            raise VoiceprintStoreError(
                f"Voiceprint {voiceprint_uuid} has no envelope; its ciphertext has "
                "been deleted."
            )
        path = self._dir / str(relative)
        if not path.is_file():
            raise VoiceprintStoreError(
                f"Voiceprint {voiceprint_uuid} envelope is missing from storage."
            )
        model = ModelIdentity(
            name=str(row["model_name"]),
            version=str(row["model_version"]),
            sha256=str(row["model_sha256"]),
        )
        payload = cipher.open(
            path.read_bytes(),
            voiceprint_uuid=voiceprint_uuid,
            participant_id=int(row["participant_id"]),
            model=model,
        )
        validate_payload(payload)
        return payload

    # -- verification -------------------------------------------------------

    def verify(
        self, voiceprint_uuid: str, *, cipher: VoiceprintCipher | None = None
    ) -> VerifyOutcome:
        """Check a stored voiceprint end to end.

        Without a ``cipher`` this performs the checks that need no key -- row
        present, file present, size, envelope hash, schema, key id. With one it also
        authenticates the ciphertext and validates the decrypted payload's shape.
        A confirmed authentication failure marks the row ``INTEGRITY_FAILED``,
        because a template that cannot be authenticated must never be compared
        against a voice again.
        """
        checks: dict[str, bool] = {}
        problems: list[str] = []
        conn = self._connect()
        try:
            row = self._row(conn, voiceprint_uuid)
        finally:
            conn.close()

        status = VoiceprintStatus(str(row["status"]))
        checks["row_present"] = True
        relative = row["envelope_relative_path"]

        if not status.usable:
            # A revoked or superseded row with no envelope is correct, not broken.
            checks["envelope_expected"] = bool(relative)
            if not relative:
                return VerifyOutcome(
                    voiceprint_uuid=voiceprint_uuid,
                    ok=True,
                    status=status.value,
                    checks=checks,
                    problems=[
                        f"status is {status.value}; the ciphertext is intentionally "
                        "absent and nothing is verifiable"
                    ],
                )

        path = self._dir / str(relative) if relative else None
        checks["file_present"] = bool(path and path.is_file())
        if not checks["file_present"]:
            problems.append("the envelope file is missing from storage")
            if status.usable:
                self._mark(
                    int(row["id"]),
                    VoiceprintStatus.INTEGRITY_FAILED,
                    error="envelope file missing",
                )
            return VerifyOutcome(
                voiceprint_uuid=voiceprint_uuid,
                ok=False,
                status=VoiceprintStatus.INTEGRITY_FAILED.value
                if status.usable
                else status.value,
                checks=checks,
                problems=problems,
            )

        assert path is not None
        raw = path.read_bytes()
        expected_bytes = row["envelope_bytes"]
        checks["size_matches"] = expected_bytes is None or len(raw) == int(expected_bytes)
        if not checks["size_matches"]:
            problems.append(
                f"envelope is {len(raw)} bytes, expected {int(expected_bytes)}"
            )
        digest = sealed_sha256(raw)
        checks["envelope_sha256_matches"] = digest == str(row["envelope_sha256"])
        if not checks["envelope_sha256_matches"]:
            problems.append("envelope SHA-256 does not match the recorded value")

        import json

        try:
            header = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            header = {}
            problems.append("envelope is not readable JSON")
        checks["schema_known"] = header.get("schema") == 1
        checks["participant_binding"] = int(
            header.get("participant_id", -1)
        ) == int(row["participant_id"])
        checks["uuid_binding"] = str(header.get("voiceprint_uuid")) == voiceprint_uuid
        model_header = header.get("model") or {}
        checks["model_binding"] = (
            str(model_header.get("name")) == str(row["model_name"])
            and str(model_header.get("version")) == str(row["model_version"])
            and str(model_header.get("sha256")) == str(row["model_sha256"])
        )
        if row["key_id"]:
            checks["key_id_matches"] = str(header.get("key_id")) == str(row["key_id"])
        for name in ("schema_known", "participant_binding", "uuid_binding", "model_binding"):
            if not checks.get(name, True):
                problems.append(f"{name} failed")

        if cipher is not None:
            try:
                payload = cipher.open(
                    raw,
                    voiceprint_uuid=voiceprint_uuid,
                    participant_id=int(row["participant_id"]),
                    model=ModelIdentity(
                        name=str(row["model_name"]),
                        version=str(row["model_version"]),
                        sha256=str(row["model_sha256"]),
                    ),
                )
                checks["authenticated"] = True
                try:
                    validate_payload(payload)
                    checks["payload_schema_valid"] = True
                    checks["embedding_dim_matches"] = int(
                        payload["embedding_dim"]
                    ) == int(row["embedding_dim"])
                    if not checks["embedding_dim_matches"]:
                        problems.append(
                            "payload embedding_dim disagrees with the recorded value"
                        )
                except PayloadSchemaError as exc:
                    checks["payload_schema_valid"] = False
                    problems.append(str(exc))
                finally:
                    del payload
            except CipherError as exc:
                checks["authenticated"] = False
                problems.append(f"authentication failed: {exc}")

        ok = not problems
        final_status = status.value
        if not ok and status.usable:
            self._mark(
                int(row["id"]),
                VoiceprintStatus.INTEGRITY_FAILED,
                error="; ".join(problems)[:300],
            )
            final_status = VoiceprintStatus.INTEGRITY_FAILED.value
        return VerifyOutcome(
            voiceprint_uuid=voiceprint_uuid,
            ok=ok,
            status=final_status,
            checks=checks,
            problems=problems,
        )

    # -- revocation ---------------------------------------------------------

    def delete_for_revocation(
        self, participant_id: int, *, reason: str = "consent revoked"
    ) -> dict[str, Any]:
        """Delete every voiceprint envelope for a participant.

        Called **after** the ``REVOKED`` consent event is already committed, so the
        participant is ineligible from that moment regardless of what happens here.
        A failed unlink therefore downgrades to ``DELETE_PENDING`` -- still
        unusable, and retryable -- instead of leaving a usable template behind.
        """
        conn = self._connect()
        deleted: list[str] = []
        pending: list[str] = []
        try:
            with maybe_transaction(conn):
                # secure_delete overwrites freed pages rather than merely unlinking
                # them. See the migration 0003 header for what this does and does
                # not achieve on an SSD.
                conn.execute("PRAGMA secure_delete = ON")
                rows = conn.execute(
                    "SELECT id, voiceprint_uuid, envelope_relative_path, status "
                    "FROM voiceprints WHERE participant_id = ? AND status NOT IN (?, ?)",
                    (
                        participant_id,
                        VoiceprintStatus.REVOKED.value,
                        VoiceprintStatus.SUPERSEDED.value,
                    ),
                ).fetchall()
                for row in rows:
                    removed, error = self._unlink_envelope(row["envelope_relative_path"])
                    new_status = (
                        VoiceprintStatus.REVOKED
                        if removed
                        else VoiceprintStatus.DELETE_PENDING
                    )
                    conn.execute(
                        "UPDATE voiceprints SET status = ?, production_eligible = 0,"
                        " envelope_relative_path = CASE WHEN ? THEN NULL ELSE"
                        " envelope_relative_path END,"
                        " envelope_sha256 = CASE WHEN ? THEN NULL ELSE envelope_sha256 END,"
                        " revoked_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
                        " deleted_at = CASE WHEN ? THEN"
                        " strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END,"
                        " delete_error = ?,"
                        " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                        " WHERE id = ?",
                        (
                            new_status.value,
                            1 if removed else 0,
                            1 if removed else 0,
                            1 if removed else 0,
                            error,
                            int(row["id"]),
                        ),
                    )
                    record_event(
                        conn,
                        category="PARTICIPANT",
                        action="VOICEPRINT_DELETED"
                        if removed
                        else "VOICEPRINT_DELETE_PENDING",
                        entity_type="voiceprint",
                        entity_id=int(row["id"]),
                        detail={
                            "voiceprint_uuid": str(row["voiceprint_uuid"]),
                            "participant_id": participant_id,
                            "previous_status": str(row["status"]),
                            "reason": reason[:200],
                            "envelope_deleted": removed,
                            "delete_error": error,
                        },
                    )
                    (deleted if removed else pending).append(str(row["voiceprint_uuid"]))
            self._checkpoint_wal(conn)
        finally:
            conn.close()
        return {
            "participant_id": participant_id,
            "deleted": deleted,
            "delete_pending": pending,
            "fully_deleted": not pending,
        }

    @staticmethod
    def _checkpoint_wal(conn: sqlite3.Connection) -> None:
        """Force freed pages out of the -wal file.

        ``secure_delete`` overwrites pages in the main database, but pages already
        copied into the write-ahead log are untouched until a checkpoint folds them
        back. Without this, deleted voiceprint metadata can persist in ``-wal``
        long after the row is gone.
        """
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError:
            # Non-fatal: the row is already gone and the envelope already unlinked.
            pass

    def retry_pending_cleanup(self) -> RecoveryReport:
        """Retry deletion for every ``DELETE_PENDING`` voiceprint.

        Idempotent and safe at startup. Opens no microphone, creates no key,
        touches no recording, and never revives consent.
        """
        report = RecoveryReport()
        conn = self._connect()
        try:
            with maybe_transaction(conn):
                conn.execute("PRAGMA secure_delete = ON")
                rows = conn.execute(
                    "SELECT id, voiceprint_uuid, envelope_relative_path FROM voiceprints"
                    " WHERE status = ?",
                    (VoiceprintStatus.DELETE_PENDING.value,),
                ).fetchall()
                for row in rows:
                    removed, error = self._unlink_envelope(row["envelope_relative_path"])
                    if not removed:
                        report.cleanup_still_pending += 1
                        conn.execute(
                            "UPDATE voiceprints SET delete_error = ?,"
                            " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                            " WHERE id = ?",
                            (error, int(row["id"])),
                        )
                        continue
                    conn.execute(
                        "UPDATE voiceprints SET status = ?, envelope_relative_path = NULL,"
                        " envelope_sha256 = NULL, delete_error = NULL,"
                        " deleted_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
                        " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                        " WHERE id = ?",
                        (VoiceprintStatus.REVOKED.value, int(row["id"])),
                    )
                    record_event(
                        conn,
                        category="PARTICIPANT",
                        action="VOICEPRINT_DELETED",
                        entity_type="voiceprint",
                        entity_id=int(row["id"]),
                        detail={
                            "voiceprint_uuid": str(row["voiceprint_uuid"]),
                            "retry": True,
                        },
                    )
                    report.cleanup_retried += 1
            self._checkpoint_wal(conn)
        finally:
            conn.close()
        return report

    # -- recovery -----------------------------------------------------------

    def recover_incomplete_operations(self) -> RecoveryReport:
        """Reconcile the filesystem and the database after an interruption.

        Idempotent: a second pass over a healthy store changes nothing. Opens no
        microphone and creates no key -- it needs neither, because every decision
        here is made from the row, the file's presence and its hash.
        """
        report = RecoveryReport()
        if not self._dir.is_dir():
            return report

        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, voiceprint_uuid, status, envelope_relative_path,"
                " envelope_sha256, envelope_bytes FROM voiceprints"
            ).fetchall()
        finally:
            conn.close()
        known = {str(r["voiceprint_uuid"]): r for r in rows}

        # 1. Pending rows: finalise, fail or flag.
        for row in rows:
            if str(row["status"]) != VoiceprintStatus.PENDING_WRITE.value:
                continue
            uuid_value = str(row["voiceprint_uuid"])
            final = self._final_path(uuid_value)
            temp = self._temp_path(uuid_value)
            if not final.is_file():
                # The rename never happened.
                temp.unlink(missing_ok=True)
                self._mark(
                    int(row["id"]),
                    VoiceprintStatus.INTEGRITY_FAILED,
                    error="interrupted before the envelope was renamed into place",
                )
                report.marked_failed += 1
                report.notes.append(
                    f"{uuid_value}: pending row had no final envelope; marked "
                    "INTEGRITY_FAILED and the temporary file was removed"
                )
                continue
            raw = final.read_bytes()
            if sealed_sha256(raw) != str(row["envelope_sha256"]):
                self._mark(
                    int(row["id"]),
                    VoiceprintStatus.INTEGRITY_FAILED,
                    error="envelope hash mismatch found during recovery",
                )
                report.marked_integrity_failed += 1
                report.notes.append(
                    f"{uuid_value}: pending envelope hash did not match; marked "
                    "INTEGRITY_FAILED"
                )
                continue
            # The bytes are exactly what was intended. Finalising here is safe --
            # but conservatively, since the enrollment that created it is gone and
            # its production eligibility cannot be re-established, it becomes
            # RE_ENROLL_REQUIRED rather than silently ACTIVE.
            self._mark_recovered(int(row["id"]), uuid_value)
            temp.unlink(missing_ok=True)
            report.finalised += 1
            report.notes.append(
                f"{uuid_value}: envelope verified after an interrupted save; marked "
                "RE_ENROLL_REQUIRED because the enrollment did not complete"
            )

        # 2. Live rows whose envelope is gone or altered.
        for row in rows:
            status = VoiceprintStatus(str(row["status"]))
            if not status.usable:
                continue
            relative = row["envelope_relative_path"]
            path = self._dir / str(relative) if relative else None
            if path is None or not path.is_file():
                self._mark(
                    int(row["id"]),
                    VoiceprintStatus.INTEGRITY_FAILED,
                    error="envelope missing during recovery",
                )
                report.marked_integrity_failed += 1
                report.notes.append(
                    f"{row['voiceprint_uuid']}: active row had no envelope; marked "
                    "INTEGRITY_FAILED"
                )
                continue
            if sealed_sha256(path.read_bytes()) != str(row["envelope_sha256"]):
                self._mark(
                    int(row["id"]),
                    VoiceprintStatus.INTEGRITY_FAILED,
                    error="envelope hash mismatch during recovery",
                )
                report.marked_integrity_failed += 1
                report.notes.append(
                    f"{row['voiceprint_uuid']}: active envelope hash mismatch; marked "
                    "INTEGRITY_FAILED"
                )

        # 3. Stray files on disk.
        for path in sorted(self._dir.iterdir()):
            if not path.is_file():
                continue
            name = path.name
            if name.endswith(TEMP_SUFFIX):
                stem = name[: -len(TEMP_SUFFIX)]
                row = known.get(stem)
                if row is None or str(row["status"]) != VoiceprintStatus.PENDING_WRITE.value:
                    self._quarantine(
                        path,
                        "Temporary voiceprint envelope with no matching pending row. "
                        "An interrupted save left this behind; it is kept as evidence "
                        "rather than deleted.",
                    )
                    report.quarantined_temp += 1
                    report.notes.append(f"{name}: quarantined (no pending row)")
                continue
            if name.endswith(FINAL_SUFFIX):
                stem = name[: -len(FINAL_SUFFIX)]
                if stem not in known:
                    self._quarantine(
                        path,
                        "Voiceprint envelope with no database row. It cannot be "
                        "attributed to a participant or a consent record, so it is "
                        "quarantined rather than trusted or deleted.",
                    )
                    report.quarantined_orphan += 1
                    report.notes.append(f"{name}: quarantined (orphan, no row)")

        return report

    def _mark_recovered(self, voiceprint_id: int, voiceprint_uuid: str) -> None:
        conn = self._connect()
        try:
            with maybe_transaction(conn):
                conn.execute(
                    "UPDATE voiceprints SET status = ?, production_eligible = 0,"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                    (VoiceprintStatus.RE_ENROLL_REQUIRED.value, voiceprint_id),
                )
                record_event(
                    conn,
                    category="PARTICIPANT",
                    action="VOICEPRINT_RECOVERED_REQUIRES_RE_ENROLLMENT",
                    entity_type="voiceprint",
                    entity_id=voiceprint_id,
                    detail={"voiceprint_uuid": voiceprint_uuid},
                )
        finally:
            conn.close()
