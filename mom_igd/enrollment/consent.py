"""Biometric consent: the text, its hash, and the append-only event log.

**Status of the text below: DRAFT, pending organisational and legal review.**
This module implements a consent *mechanism*. It does not make the application
legally compliant, and nothing here should be read as legal advice. Indonesia's
UU PDP No. 27/2022 treats voice biometrics as *data pribadi bersifat spesifik*,
which carries obligations -- recorded explicit consent, a stated purpose, a
retention limit and a right to erasure -- that a code module cannot discharge on
its own. Until the organisation records its approval in configuration,
``doctor --production`` reports the review as outstanding rather than passing.

**Why consent is an append-only event log, not a boolean.** A ``granted`` flag
would let one ``UPDATE`` erase the fact that consent was ever given, or ever
withdrawn. For biometric data that history *is* the record: an operator needs to
be able to answer "when did she agree, to what wording, and when did she change
her mind?" months later. Current state is therefore derived -- the latest event
for a participant wins -- and no row is ever modified or deleted.

**Why the text is hashed.** Recording "consent v1" proves nothing if the wording
of v1 can drift. The SHA-256 of the exact text is stored with every event, so a
later reader can tell whether a stored consent refers to the wording in front of
them or to something that has since been edited.

**Why a re-grant does not revive a voiceprint.** Revocation deletes the encrypted
template. A later re-grant is permission to enrol *again*; it cannot resurrect
what was deleted, and pretending otherwise would mean holding biometric data the
person had withdrawn permission for. Enrollment after a re-grant therefore starts
from scratch (ADR-0009).
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from mom_igd.audit import record_event
from mom_igd.db.connection import maybe_transaction

__all__ = [
    "CONSENT_PURPOSE",
    "CONSENT_TEXT_ID",
    "CONSENT_TEXT_SHA256",
    "CONSENT_TEXT_V1",
    "CONSENT_VERSION",
    "ConfirmationMethod",
    "ConsentAction",
    "ConsentError",
    "ConsentService",
    "ConsentState",
    "consent_text_sha256",
]

CONSENT_VERSION: Final[str] = "1.0-draft"
"""Version label stored with every consent event.

The ``-draft`` suffix is deliberate and load-bearing: it is what
``doctor --production`` looks at to decide that organisational review is still
outstanding. Removing it is an assertion that the text has been approved, and must
only be done by whoever is entitled to make that assertion.
"""

CONSENT_TEXT_ID: Final[str] = "id-ID"
"""Language of the canonical text. Consent must be given in a language understood."""

CONSENT_PURPOSE: Final[str] = (
    "Identifikasi pembicara dalam meeting offline pada perangkat MoM-IGD."
)
"""The single permitted purpose. Any other use requires new consent, not reuse."""

CONSENT_TEXT_V1: Final[str] = """\
PERSETUJUAN PEMROSESAN DATA BIOMETRIK SUARA
Versi 1.0 (DRAF — menunggu peninjauan organisasi/legal)

Dokumen ini menjelaskan apa yang akan terjadi pada suara Anda apabila Anda
setuju. Bacalah sampai selesai sebelum memutuskan. Anda tidak wajib setuju.

1. APA YANG DIREKAM
   Anda akan diminta berbicara beberapa kali, seluruhnya sekitar 30–60 detik.
   Rekaman itu dipakai untuk membuat "templat biometrik suara" (voiceprint):
   sederet angka yang mewakili ciri suara Anda. Templat ini adalah data
   biometrik dan diperlakukan sebagai data pribadi yang bersifat spesifik.

2. TUJUAN PENGGUNAAN — HANYA SATU
   Templat suara Anda dipakai semata-mata untuk:
       Identifikasi pembicara dalam meeting offline pada perangkat MoM-IGD.
   Artinya: membantu aplikasi menandai bagian notulen mana yang Anda ucapkan.
   Templat tidak dipakai untuk tujuan lain, termasuk verifikasi identitas,
   penilaian kinerja, pemantauan kehadiran, atau analisis emosi.

3. SEMUA PEMROSESAN BERLANGSUNG LOKAL DAN OFFLINE
   Suara dan templat Anda tidak dikirim ke internet, tidak diunggah ke layanan
   awan, dan tidak dibagikan ke pihak ketiga. Seluruh proses berjalan di satu
   komputer milik organisasi ini.

4. REKAMAN MENTAH TIDAK DISIMPAN
   Audio pendaftaran hanya berada di memori selama proses berlangsung. Setelah
   templat selesai dibuat — atau apabila proses dibatalkan atau gagal — audio
   tersebut dilepas dan tidak ditulis ke penyimpanan.

5. TEMPLAT DISIMPAN DALAM KEADAAN TERENKRIPSI
   Templat suara Anda disimpan terenkripsi (AES-256-GCM) dengan kunci yang
   dilindungi oleh Windows pada akun pengguna komputer tersebut. Perlu Anda
   ketahui secara jujur: perlindungan ini tidak menghalangi seseorang yang sudah
   dapat menjalankan program sebagai pengguna yang sama di komputer itu.

6. HAK ANDA UNTUK MENCABUT PERSETUJUAN
   Anda dapat mencabut persetujuan ini kapan saja, tanpa perlu memberikan
   alasan. Sampaikan kepada operator aplikasi.

7. AKIBAT PENCABUTAN
   Apabila Anda mencabut persetujuan:
     - templat suara Anda dinonaktifkan dan berkas terenkripsinya dihapus;
     - aplikasi tidak lagi berupaya mengenali suara Anda;
     - pada pemrosesan meeting berikutnya, suara Anda akan ditandai sebagai
       UNKNOWN, bukan dengan nama Anda;
     - persetujuan lama tidak dapat dipakai untuk menghidupkan kembali templat
       yang telah dihapus. Jika kelak Anda setuju lagi, pendaftaran suara harus
       dilakukan dari awal.

8. YANG TIDAK IKUT TERHAPUS
   Pencabutan persetujuan tidak otomatis menghapus notulen, rekaman rapat, atau
   dokumen yang sudah dibuat sebelumnya. Dokumen historis tetap ada sebagaimana
   adanya. Apabila Anda ingin data rapat tertentu dihapus, itu permintaan
   terpisah yang ditangani menurut kebijakan retensi organisasi.

9. KETERBATASAN PENGHAPUSAN — DISAMPAIKAN TERBUKA
   Penghapusan berkas menghapus templat dari jangkauan aplikasi. Pada media SSD,
   penghapusan tingkat berkas tidak menjamin data hilang secara fisik dari cip
   penyimpanan sampai pengendali media menimpanya. Jaminan yang lebih kuat
   memerlukan enkripsi seluruh disk pada perangkat.

10. PERSETUJUAN
    Dengan menyatakan setuju, Anda menegaskan bahwa Anda telah membaca dan
    memahami keterangan di atas, dan Anda memberikan persetujuan untuk
    pembuatan serta penyimpanan templat biometrik suara Anda untuk tujuan pada
    butir 2 saja.

CATATAN UNTUK ORGANISASI: teks ini masih berstatus DRAF dan belum ditinjau oleh
fungsi hukum/kepatuhan. Aplikasi tidak mengklaim kepatuhan hukum otomatis.
"""
"""Canonical consent text, version 1.0-draft.

Any edit changes :data:`CONSENT_TEXT_SHA256`, which is exactly the point: an
existing consent event then visibly refers to superseded wording instead of being
silently reinterpreted under new terms.
"""


def consent_text_sha256(text: str = CONSENT_TEXT_V1) -> str:
    """SHA-256 of the consent text, over UTF-8 with normalised line endings.

    Line endings are normalised first for the same reason the migration checksums
    are: this repository is developed with ``core.autocrlf=true``, and a hash that
    changed on checkout would make every stored consent look superseded after a
    fresh clone.
    """
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


CONSENT_TEXT_SHA256: Final[str] = consent_text_sha256()
"""Hash of the shipped text. Recorded with every consent event."""


class ConsentAction(StrEnum):
    """The only two things that can happen. Both are additive."""

    GRANTED = "GRANTED"
    REVOKED = "REVOKED"


class ConfirmationMethod(StrEnum):
    """How the person's agreement was captured.

    Recorded because "consent exists" and "consent was confirmed by the person
    rather than asserted on their behalf" are different facts, and an auditor is
    entitled to tell them apart.
    """

    OPERATOR_CONFIRMED_IN_PERSON = "OPERATOR_CONFIRMED_IN_PERSON"
    PARTICIPANT_CONFIRMED_ON_DEVICE = "PARTICIPANT_CONFIRMED_ON_DEVICE"


class ConsentError(RuntimeError):
    """A consent operation was refused. The message names the reason."""


@dataclass(frozen=True, slots=True)
class ConsentState:
    """Derived current consent for one participant."""

    participant_id: int
    active: bool
    action: ConsentAction | None
    event_id: int | None
    event_uuid: str | None
    consent_version: str | None
    consent_text_sha256: str | None
    purpose: str | None
    occurred_at: str | None
    text_matches_current: bool

    @property
    def enrollment_allowed(self) -> bool:
        """Whether an enrollment may proceed.

        Active consent is required. Wording drift is deliberately *not* a blocker
        here -- it is surfaced through :attr:`text_matches_current` and reported by
        ``doctor`` -- because silently refusing enrollment after an editorial fix
        to the text would be a confusing failure with no actionable message.
        """
        return self.active

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form. The integer ``participant_id`` is deliberately omitted.

        Same rule as :meth:`Participant.to_dict`: clients address a participant by
        UUID, and the autoincrement id is an internal detail that also reveals how
        many people have ever been registered. It stayed in this payload by oversight
        and reached every ``/enrollment/participants`` response until a roster test
        caught it. ``event_id`` is omitted for the same reason -- ``event_uuid``
        identifies an event.

        The field remains on the dataclass: in-process callers legitimately need it
        to key the consent and voiceprint tables.
        """
        return {
            "active": self.active,
            "action": self.action.value if self.action else None,
            "event_uuid": self.event_uuid,
            "consent_version": self.consent_version,
            "consent_text_sha256": self.consent_text_sha256,
            "purpose": self.purpose,
            "occurred_at": self.occurred_at,
            "text_matches_current": self.text_matches_current,
            "current_version": CONSENT_VERSION,
            "current_text_sha256": CONSENT_TEXT_SHA256,
            "review_pending": CONSENT_VERSION.endswith("-draft"),
        }


class ConsentService:
    """Reads and appends consent events. Never updates or deletes one."""

    def __init__(self, connection_factory: Any) -> None:
        """``connection_factory`` returns a configured sqlite3 connection."""
        self._connect = connection_factory

    # -- reading ------------------------------------------------------------

    def state(self, conn: sqlite3.Connection, participant_id: int) -> ConsentState:
        """Derive current consent from the latest event.

        ``ORDER BY id DESC`` rather than by timestamp: two events inside the same
        millisecond would tie on ``occurred_at``, and the autoincrement id is the
        only strictly monotonic ordering available.
        """
        row = conn.execute(
            "SELECT id, event_uuid, action, purpose, consent_version, "
            "consent_text_sha256, occurred_at FROM consent_events "
            "WHERE participant_id = ? ORDER BY id DESC LIMIT 1",
            (participant_id,),
        ).fetchone()
        if row is None:
            return ConsentState(
                participant_id=participant_id,
                active=False,
                action=None,
                event_id=None,
                event_uuid=None,
                consent_version=None,
                consent_text_sha256=None,
                purpose=None,
                occurred_at=None,
                text_matches_current=False,
            )
        action = ConsentAction(str(row["action"]))
        stored_hash = str(row["consent_text_sha256"])
        return ConsentState(
            participant_id=participant_id,
            active=action is ConsentAction.GRANTED,
            action=action,
            event_id=int(row["id"]),
            event_uuid=str(row["event_uuid"]),
            consent_version=str(row["consent_version"]),
            consent_text_sha256=stored_hash,
            purpose=str(row["purpose"]),
            occurred_at=str(row["occurred_at"]),
            text_matches_current=stored_hash == CONSENT_TEXT_SHA256,
        )

    def history(
        self, conn: sqlite3.Connection, participant_id: int, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Full consent history, newest first. Contains no biometric data."""
        limit = max(1, min(int(limit), 500))
        return [
            {
                "event_uuid": str(r["event_uuid"]),
                "action": str(r["action"]),
                "purpose": str(r["purpose"]),
                "consent_version": str(r["consent_version"]),
                "consent_text_sha256": str(r["consent_text_sha256"]),
                "confirmation_method": str(r["confirmation_method"]),
                "actor": str(r["actor"]),
                "reason": r["reason"],
                "occurred_at": str(r["occurred_at"]),
            }
            for r in conn.execute(
                "SELECT event_uuid, action, purpose, consent_version, "
                "consent_text_sha256, confirmation_method, actor, reason, occurred_at "
                "FROM consent_events WHERE participant_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (participant_id, limit),
            )
        ]

    # -- appending ----------------------------------------------------------

    def _append(
        self,
        conn: sqlite3.Connection,
        *,
        participant_id: int,
        action: ConsentAction,
        confirmation_method: ConfirmationMethod,
        actor: str,
        reason: str | None,
    ) -> dict[str, Any]:
        """Insert one event and its audit record in the caller's transaction."""
        event_uuid = str(uuid.uuid4())
        cursor = conn.execute(
            "INSERT INTO consent_events (event_uuid, participant_id, action, purpose,"
            " consent_version, consent_text_sha256, confirmation_method, actor, reason)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event_uuid,
                participant_id,
                action.value,
                CONSENT_PURPOSE,
                CONSENT_VERSION,
                CONSENT_TEXT_SHA256,
                confirmation_method.value,
                actor[:120],
                (reason or None) and str(reason)[:300],
            ),
        )
        event_id = int(cursor.lastrowid or 0)
        record_event(
            conn,
            category="PARTICIPANT",
            action=(
                "CONSENT_GRANTED"
                if action is ConsentAction.GRANTED
                else "CONSENT_REVOKED"
            ),
            entity_type="participant",
            entity_id=participant_id,
            detail={
                # Metadata only. No voice features, no template, no audio.
                "consent_event_uuid": event_uuid,
                "consent_version": CONSENT_VERSION,
                "consent_text_sha256": CONSENT_TEXT_SHA256,
                "purpose": CONSENT_PURPOSE,
                "confirmation_method": confirmation_method.value,
            },
        )
        return {"event_id": event_id, "event_uuid": event_uuid}

    def grant(
        self,
        participant_id: int,
        *,
        confirmation_method: ConfirmationMethod,
        actor: str = "local-operator",
        acknowledged_text_sha256: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Record explicit consent.

        ``acknowledged_text_sha256`` is the hash of the text the caller actually
        displayed. Requiring the caller to echo it back is what stops a UI from
        granting consent to wording nobody saw -- a mismatch means the dialog and
        this module disagree about what was on screen, which is not something to
        resolve by guessing.

        Idempotent: granting when consent is already active returns the existing
        event instead of appending a duplicate, so a double-clicked button cannot
        litter the history.
        """
        if acknowledged_text_sha256 is not None:
            if acknowledged_text_sha256 != CONSENT_TEXT_SHA256:
                raise ConsentError(
                    "The consent text shown to the participant does not match the "
                    f"text this build ships (shown {acknowledged_text_sha256[:12]}…, "
                    f"expected {CONSENT_TEXT_SHA256[:12]}…). Refusing to record "
                    "consent for wording that cannot be reproduced."
                )
        owns = conn is None
        connection = self._connect() if owns else conn
        try:
            with maybe_transaction(connection):
                self._assert_enrollable_participant(connection, participant_id)
                current = self.state(connection, participant_id)
                if current.active:
                    return {
                        "event_id": current.event_id,
                        "event_uuid": current.event_uuid,
                        "already_active": True,
                    }
                result = self._append(
                    connection,
                    participant_id=participant_id,
                    action=ConsentAction.GRANTED,
                    confirmation_method=confirmation_method,
                    actor=actor,
                    reason=None,
                )
                result["already_active"] = False
                return result
        finally:
            if owns:
                connection.close()

    def revoke(
        self,
        participant_id: int,
        *,
        actor: str = "local-operator",
        reason: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Record withdrawal of consent.

        This method only writes the event. Deleting the voiceprint is the caller's
        responsibility (:mod:`mom_igd.enrollment.store`), because the deletion can
        fail independently and must then leave a retryable pending state rather
        than a lost consent record. The ordering is deliberate: the *event* lands
        first, so a crash between the two leaves consent withdrawn and a template
        awaiting cleanup -- never the reverse.
        """
        owns = conn is None
        connection = self._connect() if owns else conn
        try:
            with maybe_transaction(connection):
                current = self.state(connection, participant_id)
                if current.event_id is None:
                    raise ConsentError(
                        f"Participant {participant_id} has no consent to revoke."
                    )
                if not current.active:
                    return {
                        "event_id": current.event_id,
                        "event_uuid": current.event_uuid,
                        "already_revoked": True,
                    }
                result = self._append(
                    connection,
                    participant_id=participant_id,
                    action=ConsentAction.REVOKED,
                    # Withdrawal is always the person's decision, relayed by the
                    # operator running the device.
                    confirmation_method=ConfirmationMethod.OPERATOR_CONFIRMED_IN_PERSON,
                    actor=actor,
                    reason=reason,
                )
                result["already_revoked"] = False
                return result
        finally:
            if owns:
                connection.close()

    # -- guards -------------------------------------------------------------

    @staticmethod
    def _assert_enrollable_participant(
        conn: sqlite3.Connection, participant_id: int
    ) -> None:
        row = conn.execute(
            "SELECT is_active FROM participants WHERE id = ?", (participant_id,)
        ).fetchone()
        if row is None:
            raise ConsentError(f"No participant with id={participant_id}.")
        if not int(row["is_active"]):
            raise ConsentError(
                f"Participant {participant_id} is deactivated. Reactivate them "
                "before recording consent: a deactivated participant cannot be "
                "enrolled, so consent would grant nothing."
            )

    def text_bundle(self) -> dict[str, Any]:
        """Everything a consent dialog needs. Safe to send over the API."""
        return {
            "version": CONSENT_VERSION,
            "language": CONSENT_TEXT_ID,
            "purpose": CONSENT_PURPOSE,
            "text": CONSENT_TEXT_V1,
            "text_sha256": CONSENT_TEXT_SHA256,
            "review_pending": CONSENT_VERSION.endswith("-draft"),
            "review_note": (
                "Teks ini masih DRAF dan belum ditinjau oleh fungsi hukum/kepatuhan "
                "organisasi. Aplikasi tidak mengklaim kepatuhan hukum otomatis."
            ),
        }
