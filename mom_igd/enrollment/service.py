"""The enrollment state machine, and the only place that orchestrates Phase 3.

**What this coordinates.** Consent, participant lifecycle, the Phase 2 capture
path, quality gates, the embedding provider, encryption and storage -- each of
which is implemented and tested elsewhere. This module owns the *order* those
happen in, and the checks that make the order safe.

**Mutual exclusion with meeting recording.** Enrollment takes the **same**
``SingleRecordingLock`` at the **same** path that :class:`RecordingService` uses
(``temp/recording.lock``). That is why the guarantee is
``meeting recording XOR enrollment`` rather than a hope: the lock is created with
``O_EXCL``, so it holds across processes, which an in-process boolean cannot do.
The Phase 2 lock is reused unchanged -- it already takes an arbitrary identifier
and already clears a lock whose owning process is gone.

**Consent is re-checked immediately before encryption.** A five-sample enrollment
takes a minute, and a person can withdraw consent during it. Checking only at the
start would mean building and storing a template for someone who had already said
no. So consent, participant status, device identity, calibration and model
identity are all verified a second time after capture and before anything is
sealed -- and a change means the buffer is dropped and nothing is stored.

**No fallback to a test double, ever.** In production the provider comes only from
:func:`load_provider_from_registry`, which currently raises
:class:`ModelUnavailableError` because no model has been approved. A provider whose
``is_test_double`` is true is refused unless it was explicitly injected by a test.
The microphone is not opened until the provider is known to be usable, so an
operator is never asked to speak into a device for a template that cannot be built.

**Raw audio never reaches disk.** Samples live in a bounded in-memory buffer with a
hard byte ceiling. The buffer is released on success, rejection, cancellation,
exception and shutdown alike.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid as uuid_module
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Mapping

from mom_igd.audit import record_event
from mom_igd.db.connection import maybe_transaction
from mom_igd.enrollment.consent import ConsentService
from mom_igd.enrollment.cipher import VoiceprintCipher
from mom_igd.enrollment.keys import KeyProtector
from mom_igd.enrollment.provider import (
    ModelUnavailableError,
    SpeakerEmbeddingProvider,
    validate_embedding,
)
from mom_igd.enrollment.quality import (
    EnrollmentQualityThresholds,
    GateStatus,
    evaluate_enrollment,
    evaluate_sample,
)
from mom_igd.enrollment.store import PAYLOAD_SCHEMA, VoiceprintStore
from mom_igd.logging_setup import get_logger

__all__ = [
    "ALLOWED_TRANSITIONS",
    "DEFAULT_SAMPLE_TARGET",
    "MAX_TOTAL_CAPTURE_BYTES",
    "EnrollmentError",
    "EnrollmentService",
    "EnrollmentState",
    "InvalidEnrollmentTransition",
    "ReasonCode",
]

_LOG = get_logger("enrollment")

DEFAULT_SAMPLE_TARGET: Final[int] = 5
"""Five samples, 8-12 s each, from the phase requirements."""

MAX_SAMPLE_SECONDS: Final[float] = 15.0
MAX_TOTAL_CAPTURE_SECONDS: Final[float] = 120.0
"""Hard ceiling. Five 12-second samples plus retries fit comfortably inside this."""

MAX_TOTAL_CAPTURE_BYTES: Final[int] = 48_000 * 2 * 2 * int(MAX_TOTAL_CAPTURE_SECONDS)
"""Absolute memory ceiling: 48 kHz, stereo, 16-bit, for the maximum duration.

Enforced in bytes rather than seconds because bytes are what actually consume RAM,
and a device reporting an unexpected rate must not be able to grow the buffer past
this regardless of what it claims about duration.
"""


class EnrollmentState(StrEnum):
    """Must match the CHECK constraint on ``enrollment_sessions.state`` exactly."""

    CREATED = "CREATED"
    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    READY = "READY"
    CAPTURING = "CAPTURING"
    VALIDATING = "VALIDATING"
    EMBEDDING = "EMBEDDING"
    ENCRYPTING = "ENCRYPTING"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

    @property
    def terminal(self) -> bool:
        return self in {
            EnrollmentState.COMPLETED,
            EnrollmentState.REJECTED,
            EnrollmentState.CANCELLED,
            EnrollmentState.FAILED,
        }

    @property
    def holds_capture_resources(self) -> bool:
        """States in which the lock, the stream or the buffer may still be held."""
        return self in {
            EnrollmentState.READY,
            EnrollmentState.CAPTURING,
            EnrollmentState.VALIDATING,
            EnrollmentState.EMBEDDING,
            EnrollmentState.ENCRYPTING,
        }


ALLOWED_TRANSITIONS: Final[Mapping[EnrollmentState, frozenset[EnrollmentState]]] = {
    EnrollmentState.CREATED: frozenset(
        {
            EnrollmentState.CONSENT_REQUIRED,
            EnrollmentState.READY,
            EnrollmentState.CANCELLED,
            EnrollmentState.FAILED,
        }
    ),
    # Consent can be granted while the session waits, so this is not terminal.
    EnrollmentState.CONSENT_REQUIRED: frozenset(
        {EnrollmentState.READY, EnrollmentState.CANCELLED, EnrollmentState.FAILED}
    ),
    EnrollmentState.READY: frozenset(
        {
            EnrollmentState.CAPTURING,
            EnrollmentState.CANCELLED,
            EnrollmentState.FAILED,
        }
    ),
    # A rejected sample is re-captured, so CAPTURING can follow VALIDATING; and
    # finalise() runs from CAPTURING once enough samples have been accepted, so
    # EMBEDDING must be reachable from here too.
    EnrollmentState.CAPTURING: frozenset(
        {
            EnrollmentState.CAPTURING,
            EnrollmentState.VALIDATING,
            EnrollmentState.EMBEDDING,
            EnrollmentState.REJECTED,
            EnrollmentState.CANCELLED,
            EnrollmentState.FAILED,
        }
    ),
    EnrollmentState.VALIDATING: frozenset(
        {
            EnrollmentState.CAPTURING,
            EnrollmentState.EMBEDDING,
            EnrollmentState.REJECTED,
            EnrollmentState.CANCELLED,
            EnrollmentState.FAILED,
        }
    ),
    EnrollmentState.EMBEDDING: frozenset(
        {
            EnrollmentState.ENCRYPTING,
            EnrollmentState.REJECTED,
            EnrollmentState.CANCELLED,
            EnrollmentState.FAILED,
        }
    ),
    # No route back from ENCRYPTING to CAPTURING: once sealing begins the audio
    # buffer is gone, so there is nothing left to re-validate.
    EnrollmentState.ENCRYPTING: frozenset(
        {EnrollmentState.COMPLETED, EnrollmentState.FAILED, EnrollmentState.CANCELLED}
    ),
    EnrollmentState.COMPLETED: frozenset(),
    EnrollmentState.REJECTED: frozenset(),
    EnrollmentState.CANCELLED: frozenset(),
    EnrollmentState.FAILED: frozenset(),
}
"""Explicit transition table. Terminal states have no successor, by construction."""


class ReasonCode(StrEnum):
    """Safe, enumerated failure reasons.

    Enumerated rather than free text because these reach the UI and the audit
    trail: an exception string could carry a filesystem path, a device name or --
    worst -- part of a payload. A code cannot.
    """

    CONSENT_MISSING = "CONSENT_MISSING"
    CONSENT_REVOKED_DURING_ENROLLMENT = "CONSENT_REVOKED_DURING_ENROLLMENT"
    PARTICIPANT_INACTIVE = "PARTICIPANT_INACTIVE"
    PARTICIPANT_DEACTIVATED_DURING_ENROLLMENT = (
        "PARTICIPANT_DEACTIVATED_DURING_ENROLLMENT"
    )
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    TEST_DOUBLE_REFUSED = "TEST_DOUBLE_REFUSED"
    CAPTURE_LOCK_HELD = "CAPTURE_LOCK_HELD"
    NO_DEVICE = "NO_DEVICE"
    DEVICE_CHANGED = "DEVICE_CHANGED"
    DEVICE_DISCONNECTED = "DEVICE_DISCONNECTED"
    PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
    CALIBRATION_INVALID = "CALIBRATION_INVALID"
    QUALITY_REJECTED = "QUALITY_REJECTED"
    EMBEDDING_INVALID = "EMBEDDING_INVALID"
    BUFFER_LIMIT_EXCEEDED = "BUFFER_LIMIT_EXCEEDED"
    STORAGE_FAILED = "STORAGE_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    OPERATOR_CANCELLED = "OPERATOR_CANCELLED"


class EnrollmentError(RuntimeError):
    """An enrollment operation was refused.

    Carries a :class:`ReasonCode` so a caller can react without parsing prose, and
    a message safe to show an operator.
    """

    def __init__(self, reason: ReasonCode, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class InvalidEnrollmentTransition(EnrollmentError):
    """A state change that the transition table does not permit."""

    def __init__(self, current: EnrollmentState, requested: EnrollmentState) -> None:
        allowed = sorted(s.value for s in ALLOWED_TRANSITIONS[current])
        super().__init__(
            ReasonCode.INTERNAL_ERROR,
            f"Cannot move an enrollment from {current.value} to {requested.value}. "
            f"Allowed from {current.value}: {allowed or ['(terminal)']}.",
        )
        self.current = current
        self.requested = requested


@dataclass(slots=True)
class _Sample:
    """One captured sample, held only until embedding."""

    index: int
    pcm: bytes
    dropped_frames: int
    xrun_callbacks: int
    device_fingerprint: str | None


@dataclass(slots=True)
class _Active:
    """The enrollment currently in flight."""

    session_id: int
    session_uuid: str
    participant_id: int
    participant_uuid: str
    consent_event_id: int | None
    state: EnrollmentState
    attempt: int
    samples_target: int
    device_fingerprint: str | None
    device_name: str | None
    device_transport: str | None
    transport_verified: bool
    sample_rate_hz: int
    channels: int
    calibration_utc: str | None
    calibration_verdict: str | None
    calibration_age_days: float | None
    provider: SpeakerEmbeddingProvider | None
    samples: list[_Sample] = field(default_factory=list)
    quality: list[Any] = field(default_factory=list)
    reason_code: str | None = None

    @property
    def buffered_bytes(self) -> int:
        return sum(len(s.pcm) for s in self.samples)

    def drop_audio(self) -> None:
        """Release every PCM buffer. Called on every terminal path."""
        for sample in self.samples:
            sample.pcm = b""
        self.samples.clear()


class EnrollmentService:
    """Drives one voice enrollment at a time."""

    def __init__(
        self,
        config: Any,
        paths: Any,
        *,
        recording_service: Any,
        provider: SpeakerEmbeddingProvider | None = None,
        key_protector: KeyProtector | None = None,
        thresholds: EnrollmentQualityThresholds | None = None,
    ) -> None:
        """``provider`` and ``key_protector`` are injection points for tests only.

        Production passes neither: the provider is resolved from the registry (and
        currently refuses), and the key protector is the real DPAPI one.
        """
        self._config = config
        self._paths = paths
        self._recording = recording_service
        self._injected_provider = provider
        self._thresholds = thresholds or EnrollmentQualityThresholds()
        self._key_protector = key_protector or KeyProtector(paths.keys_dir)
        self._state_lock = threading.RLock()
        self._active: _Active | None = None
        self._consent = ConsentService(self._connect)
        self._store = VoiceprintStore(paths.voiceprints_dir, self._connect)

    # There must be exactly one store and one consent service per data root: two
    # objects with an opinion about the same envelopes, or the same append-only log,
    # is a way to get divergent state. Callers that need them read them from here
    # rather than constructing their own.

    @property
    def store(self) -> VoiceprintStore:
        return self._store

    @property
    def consent(self) -> ConsentService:
        return self._consent

    # -- infrastructure -----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        from mom_igd.db.connection import connect

        return connect(
            self._paths.database_path(self._config.database.filename),
            busy_timeout_ms=self._config.database.busy_timeout_ms,
        )

    @property
    def _capture_lock(self):
        """The **Phase 2** lock, at the **Phase 2** path.

        Reusing the same file is the entire mechanism behind
        ``meeting recording XOR enrollment``. Do not give enrollment its own lock
        file: two different files would let both run at once.
        """
        return self._recording._lock  # noqa: SLF001 - deliberate, documented reuse

    # -- readiness ----------------------------------------------------------

    def _participant_row(
        self, conn: sqlite3.Connection, participant_uuid: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT id, uuid, display_name, is_active FROM participants WHERE uuid = ?",
            (participant_uuid,),
        ).fetchone()
        if row is None:
            raise EnrollmentError(
                ReasonCode.INTERNAL_ERROR,
                f"No participant with uuid={participant_uuid!r}.",
            )
        return row

    def _calibration_evidence(self) -> tuple[str | None, str | None, float | None]:
        """Return (utc, verdict, age_days) from the Phase 2 calibration record."""
        import json

        raw = self._recording._setting("last_calibration")  # noqa: SLF001
        if not raw:
            return None, None, None
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return None, "UNKNOWN", None
        utc = str(payload.get("utc") or "") or None
        verdict = str(payload.get("verdict") or "") or None
        age = None
        if utc:
            from mom_igd.diagnostics.audio_checks import _calibration_age_days

            age = _calibration_age_days(utc)
        return utc, verdict, age

    def _resolve_provider(self) -> SpeakerEmbeddingProvider:
        """Return a usable provider, or refuse before the microphone is touched.

        Order matters: this runs during readiness, so a missing model is reported
        while the operator is still looking at a button -- not after they have
        spoken five samples into a microphone for nothing.
        """
        if self._injected_provider is not None:
            return self._injected_provider
        from mom_igd.enrollment.provider import load_provider_from_registry

        provider = load_provider_from_registry(self._config, self._paths)
        if getattr(provider, "is_test_double", False):
            # Belt and braces: the loader is not supposed to be able to return one.
            raise EnrollmentError(
                ReasonCode.TEST_DOUBLE_REFUSED,
                "A test-double embedding provider was resolved for a production "
                "enrollment and has been refused. A stand-in template would be "
                "encrypted, stored and marked eligible while identifying nobody.",
            )
        return provider

    def readiness(self, participant_uuid: str) -> dict[str, Any]:
        """Everything the wizard needs to decide whether Start may be enabled.

        Opens no stream, creates no key, decrypts nothing and downloads nothing.
        """
        conn = self._connect()
        try:
            row = self._participant_row(conn, participant_uuid)
            participant_id = int(row["id"])
            consent_state = self._consent.state(conn, participant_id)
        finally:
            conn.close()

        device, device_error = self._recording.resolve_device()
        profile = self._recording.profile_for(device) if device is not None else None
        cal_utc, cal_verdict, cal_age = self._calibration_evidence()

        model_ready = False
        model_detail = ""
        model_spec: dict[str, Any] | None = None
        try:
            provider = self._resolve_provider()
            model_ready = True
            model_spec = provider.spec.to_dict()
            model_detail = f"{provider.spec.name} {provider.spec.version}"
            if getattr(provider, "is_test_double", False):
                model_detail += " (TEST DOUBLE -- injected, not production)"
        except ModelUnavailableError as exc:
            model_detail = str(exc)
        except EnrollmentError as exc:
            model_detail = str(exc)

        # Same rule as start(): a live holder blocks, a stale one does not, and
        # `held` is never consulted because the lock object is shared.
        holder = self._capture_lock.read_live_holder()
        blockers: list[str] = []
        if not bool(int(row["is_active"])):
            blockers.append(ReasonCode.PARTICIPANT_INACTIVE.value)
        if not consent_state.enrollment_allowed:
            blockers.append(ReasonCode.CONSENT_MISSING.value)
        if not model_ready:
            blockers.append(ReasonCode.MODEL_UNAVAILABLE.value)
        if device is None:
            blockers.append(ReasonCode.NO_DEVICE.value)
        if cal_verdict != "GOOD" or cal_age is None:
            blockers.append(ReasonCode.CALIBRATION_INVALID.value)
        elif cal_age > self._thresholds.max_calibration_age_days:
            blockers.append(ReasonCode.CALIBRATION_INVALID.value)
        if holder is not None:
            blockers.append(ReasonCode.CAPTURE_LOCK_HELD.value)

        return {
            "participant_uuid": participant_uuid,
            "participant_active": bool(int(row["is_active"])),
            "consent": consent_state.to_dict(),
            "model": {
                "ready": model_ready,
                "detail": model_detail,
                "spec": model_spec,
            },
            "device": {
                "available": device is not None,
                "detail": device_error if device is None else device.name,
                "fingerprint": device.fingerprint if device else None,
                "transport": device.transport.value if device else None,
                "transport_verified": (
                    device.transport_source == "windows-mmdevices-registry"
                    if device
                    else False
                ),
                "production_eligible_device": (
                    bool(device and device.is_usb_conference_candidate)
                ),
                "profile": profile.describe() if profile is not None else None,
            },
            "calibration": {
                "utc": cal_utc,
                "verdict": cal_verdict,
                "age_days": None if cal_age is None else round(cal_age, 2),
                "max_age_days": self._thresholds.max_calibration_age_days,
            },
            "capture_lock": {
                "held_by_other": holder is not None,
                "holder_pid": (holder or {}).get("pid"),
            },
            "samples_target": DEFAULT_SAMPLE_TARGET,
            "can_start": not blockers,
            "blockers": blockers,
            "active_session": self.status().get("session_uuid"),
        }

    # -- state machine ------------------------------------------------------

    def _assert_transition(
        self, current: EnrollmentState, requested: EnrollmentState
    ) -> EnrollmentState:
        if requested not in ALLOWED_TRANSITIONS[current]:
            raise InvalidEnrollmentTransition(current, requested)
        return requested

    def _set_state(
        self,
        active: _Active,
        requested: EnrollmentState,
        *,
        reason: ReasonCode | None = None,
        persist: bool = True,
    ) -> None:
        self._assert_transition(active.state, requested)
        active.state = requested
        if reason is not None:
            active.reason_code = reason.value
        if not persist:
            return
        conn = self._connect()
        try:
            with maybe_transaction(conn):
                conn.execute(
                    "UPDATE enrollment_sessions SET state = ?, reason_code = ?,"
                    " samples_accepted = ?, samples_rejected = ?,"
                    " finished_at = CASE WHEN ? THEN"
                    " strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE finished_at END,"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                    (
                        requested.value,
                        active.reason_code,
                        sum(1 for q in active.quality if q.accepted),
                        sum(1 for q in active.quality if not q.accepted),
                        1 if requested.terminal else 0,
                        active.session_id,
                    ),
                )
        finally:
            conn.close()

    # -- start --------------------------------------------------------------

    def start(
        self, participant_uuid: str, *, samples_target: int = DEFAULT_SAMPLE_TARGET
    ) -> dict[str, Any]:
        """Create a session and arm capture. **Idempotent.**

        A second call while a session is live returns that session rather than
        creating another, so a double-clicked button cannot produce two
        enrollments. The whole method holds ``_state_lock``, so the check and the
        insert cannot interleave.
        """
        with self._state_lock:
            if self._active is not None and not self._active.state.terminal:
                _LOG.info(
                    "Enrollment start ignored: session %s is already %s.",
                    self._active.session_uuid,
                    self._active.state.value,
                )
                return self.status()

            if not 1 <= samples_target <= 10:
                raise EnrollmentError(
                    ReasonCode.INTERNAL_ERROR,
                    f"samples_target must be between 1 and 10, got {samples_target}.",
                )

            conn = self._connect()
            try:
                row = self._participant_row(conn, participant_uuid)
                participant_id = int(row["id"])
                if not bool(int(row["is_active"])):
                    raise EnrollmentError(
                        ReasonCode.PARTICIPANT_INACTIVE,
                        "This participant is deactivated and cannot be enrolled. "
                        "Reactivate them first.",
                    )
                consent_state = self._consent.state(conn, participant_id)
            finally:
                conn.close()

            if not consent_state.enrollment_allowed:
                raise EnrollmentError(
                    ReasonCode.CONSENT_MISSING,
                    "This participant has not given active consent to biometric "
                    "voice processing. Record consent before enrolling.",
                )

            # Provider first: refuse before the microphone is touched, so nobody is
            # asked to speak for a template that cannot be built.
            try:
                provider = self._resolve_provider()
            except ModelUnavailableError as exc:
                raise EnrollmentError(ReasonCode.MODEL_UNAVAILABLE, str(exc)) from None

            device, device_error = self._recording.resolve_device()
            if device is None:
                raise EnrollmentError(
                    ReasonCode.NO_DEVICE,
                    device_error or "No capture device is available.",
                )
            profile = self._recording.profile_for(device)

            # Check the shared capture lock *before* preflight. Phase 2's preflight
            # also detects it (as `single_recording`), but it would surface as a
            # generic PREFLIGHT_FAILED -- and "the microphone is busy" deserves its
            # own code so the wizard can say so precisely.
            #
            # Note what is NOT used here: `self._capture_lock.held`. The lock object
            # is *shared* with RecordingService, so `held` is True whenever this
            # process holds it -- including when it holds it for a meeting
            # recording, which is exactly the case to refuse. Identity of the holder
            # is what matters, not whether this process happens to own it.
            #
            # `read_live_holder()` also ignores a lock left by a killed process, so
            # a crash cannot block enrollment forever.
            holder = self._capture_lock.read_live_holder()
            if holder is not None:
                raise EnrollmentError(
                    ReasonCode.CAPTURE_LOCK_HELD,
                    "The microphone is already in use in this data directory "
                    f"(process {holder.get('pid')}, holder "
                    f"{holder.get('recording_uuid')}). A meeting recording and a "
                    "voice enrollment cannot run at the same time; stop the other "
                    "one first.",
                )

            report = self._recording.preflight(planned_minutes=5)
            if not report.can_start:
                reasons = "; ".join(f"{i.key}: {i.detail}" for i in report.failures)
                raise EnrollmentError(
                    ReasonCode.PREFLIGHT_FAILED, f"Preflight failed. {reasons}"
                )

            cal_utc, cal_verdict, cal_age = self._calibration_evidence()
            if cal_verdict != "GOOD" or cal_age is None:
                raise EnrollmentError(
                    ReasonCode.CALIBRATION_INVALID,
                    "A GOOD microphone calibration is required before enrolling a "
                    f"voice (current verdict: {cal_verdict or 'none recorded'}). Run "
                    "`audio calibrate` with the enrollment microphone.",
                )
            if cal_age > self._thresholds.max_calibration_age_days:
                raise EnrollmentError(
                    ReasonCode.CALIBRATION_INVALID,
                    f"The last calibration is {cal_age:.1f} days old, older than the "
                    f"{self._thresholds.max_calibration_age_days:.0f}-day limit.",
                )

            # Same lock, same path as a meeting recording.
            session_uuid = str(uuid_module.uuid4())
            try:
                self._capture_lock.acquire(f"enrollment:{session_uuid}")
            except Exception as exc:  # noqa: BLE001 - re-raised with a code
                holder = self._capture_lock.read_holder() or {}
                raise EnrollmentError(
                    ReasonCode.CAPTURE_LOCK_HELD,
                    "The microphone is already in use in this data directory "
                    f"(process {holder.get('pid')}, holder "
                    f"{holder.get('recording_uuid')}). A meeting recording and a "
                    f"voice enrollment cannot run at the same time. [{exc}]",
                ) from None

            try:
                conn = self._connect()
                try:
                    with maybe_transaction(conn):
                        cursor = conn.execute(
                            "INSERT INTO enrollment_sessions ("
                            " session_uuid, participant_id, consent_event_id, state,"
                            " samples_target, device_fingerprint, device_name,"
                            " device_transport, device_transport_verified,"
                            " sample_rate_hz, channels, sample_format,"
                            " calibration_utc, calibration_verdict, started_at"
                            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                            " strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                            (
                                session_uuid,
                                participant_id,
                                consent_state.event_id,
                                EnrollmentState.READY.value,
                                samples_target,
                                device.fingerprint,
                                device.name,
                                device.transport.value,
                                1
                                if device.transport_source
                                == "windows-mmdevices-registry"
                                else 0,
                                profile.sample_rate,
                                profile.channels,
                                profile.sample_format.value,
                                cal_utc,
                                cal_verdict,
                            ),
                        )
                        session_id = int(cursor.lastrowid or 0)
                        record_event(
                            conn,
                            category="PARTICIPANT",
                            action="ENROLLMENT_STARTED",
                            entity_type="enrollment_session",
                            entity_id=session_id,
                            detail={
                                "session_uuid": session_uuid,
                                "participant_uuid": participant_uuid,
                                "samples_target": samples_target,
                                "device_fingerprint": device.fingerprint,
                                "device_transport": device.transport.value,
                                "model_name": provider.spec.name,
                                "model_version": provider.spec.version,
                                "consent_event_id": consent_state.event_id,
                            },
                        )
                finally:
                    conn.close()
            except Exception:
                self._capture_lock.release()
                raise

            self._active = _Active(
                session_id=session_id,
                session_uuid=session_uuid,
                participant_id=participant_id,
                participant_uuid=participant_uuid,
                consent_event_id=consent_state.event_id,
                state=EnrollmentState.READY,
                attempt=1,
                samples_target=samples_target,
                device_fingerprint=device.fingerprint,
                device_name=device.name,
                device_transport=device.transport.value,
                transport_verified=(
                    device.transport_source == "windows-mmdevices-registry"
                ),
                sample_rate_hz=profile.sample_rate,
                channels=profile.channels,
                calibration_utc=cal_utc,
                calibration_verdict=cal_verdict,
                calibration_age_days=cal_age,
                provider=provider,
            )
            return self.status()

    # -- capture ------------------------------------------------------------

    def add_sample(
        self,
        pcm: bytes,
        *,
        dropped_frames: int = 0,
        xrun_callbacks: int = 0,
        device_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Accept one captured sample and judge it.

        The PCM arrives already captured, so this method performs no I/O on the
        audio device: the caller (API route or wizard) owns the stream and hands
        over bytes. That keeps embedding, encryption and database work strictly
        outside the audio callback.
        """
        with self._state_lock:
            active = self._require_active()
            if active.state not in {EnrollmentState.READY, EnrollmentState.CAPTURING}:
                raise InvalidEnrollmentTransition(
                    active.state, EnrollmentState.CAPTURING
                )

            if device_fingerprint and device_fingerprint != active.device_fingerprint:
                self._fail(
                    active,
                    EnrollmentState.FAILED,
                    ReasonCode.DEVICE_CHANGED,
                    "The capture device changed during enrollment. A template must "
                    "be tied to the microphone it was recorded on.",
                )
                raise EnrollmentError(
                    ReasonCode.DEVICE_CHANGED,
                    "The capture device changed during enrollment; the session has "
                    "been abandoned. Start again with a single device.",
                )

            if active.buffered_bytes + len(pcm) > MAX_TOTAL_CAPTURE_BYTES:
                self._fail(
                    active,
                    EnrollmentState.FAILED,
                    ReasonCode.BUFFER_LIMIT_EXCEEDED,
                    "Enrollment audio exceeded the in-memory ceiling.",
                )
                raise EnrollmentError(
                    ReasonCode.BUFFER_LIMIT_EXCEEDED,
                    f"Enrollment audio exceeded the {MAX_TOTAL_CAPTURE_BYTES} byte "
                    "in-memory ceiling and has been discarded. Enrollment audio is "
                    "never written to disk, so it cannot grow without bound.",
                )

            if active.state is EnrollmentState.READY:
                self._set_state(active, EnrollmentState.CAPTURING)

            index = len(active.samples)
            active.samples.append(
                _Sample(
                    index=index,
                    pcm=pcm,
                    dropped_frames=dropped_frames,
                    xrun_callbacks=xrun_callbacks,
                    device_fingerprint=device_fingerprint or active.device_fingerprint,
                )
            )

            self._set_state(active, EnrollmentState.VALIDATING)
            quality = evaluate_sample(
                index=index,
                pcm=pcm,
                channels=active.channels,
                sample_rate_hz=active.sample_rate_hz,
                dropped_frames=dropped_frames,
                xrun_callbacks=xrun_callbacks,
                thresholds=self._thresholds,
            )
            active.quality.append(quality)
            if not quality.accepted:
                # Drop the rejected audio immediately; it will be re-captured.
                active.samples[index].pcm = b""
                active.samples.pop(index)
                active.quality.pop()
                self._set_state(active, EnrollmentState.CAPTURING)
                return {
                    **self.status(),
                    "last_sample": quality.to_dict(),
                    "sample_accepted": False,
                }

            self._set_state(active, EnrollmentState.CAPTURING)
            return {
                **self.status(),
                "last_sample": quality.to_dict(),
                "sample_accepted": True,
            }

    # -- finalise -----------------------------------------------------------

    def finalize(self) -> dict[str, Any]:
        """Embed, seal, store and complete. The only path to an active voiceprint."""
        with self._state_lock:
            active = self._require_active()
            if active.state is not EnrollmentState.CAPTURING:
                raise InvalidEnrollmentTransition(
                    active.state, EnrollmentState.EMBEDDING
                )
            if len(active.samples) < active.samples_target:
                raise EnrollmentError(
                    ReasonCode.QUALITY_REJECTED,
                    f"{len(active.samples)} of {active.samples_target} samples have "
                    "been accepted. Capture the rest before finalising.",
                )

            provider = active.provider
            if provider is None:  # pragma: no cover - start() guarantees one
                self._fail(
                    active, EnrollmentState.FAILED, ReasonCode.MODEL_UNAVAILABLE, ""
                )
                raise EnrollmentError(
                    ReasonCode.MODEL_UNAVAILABLE, "No embedding provider is loaded."
                )

            try:
                self._set_state(active, EnrollmentState.EMBEDDING)
                embeddings: list[list[float]] = []
                for sample in active.samples:
                    raw = provider.embed(
                        sample.pcm,
                        sample_rate_hz=active.sample_rate_hz,
                        channels=active.channels,
                    )
                    # Consumer-side validation: a provider is not trusted to
                    # police its own output.
                    embeddings.append(validate_embedding(raw, spec=provider.spec))
            except EnrollmentError:
                raise
            except Exception as exc:
                self._fail(
                    active,
                    EnrollmentState.REJECTED,
                    ReasonCode.EMBEDDING_INVALID,
                    f"{type(exc).__name__}",
                )
                raise EnrollmentError(
                    ReasonCode.EMBEDDING_INVALID,
                    "The embedding model returned a vector that cannot be used, so "
                    "no voiceprint was stored.",
                ) from None

            report = evaluate_enrollment(
                samples=active.quality,
                embeddings=embeddings,
                device_fingerprint=active.device_fingerprint,
                selected_fingerprint=active.device_fingerprint,
                device_transport=active.device_transport,
                calibration_age_days=active.calibration_age_days,
                calibration_verdict=active.calibration_verdict,
                thresholds=self._thresholds,
            )
            if not report.accepted:
                failed = [
                    g.key for g in report.gates if g.status is GateStatus.REJECT
                ]
                self._fail(
                    active,
                    EnrollmentState.REJECTED,
                    ReasonCode.QUALITY_REJECTED,
                    ",".join(failed)[:200],
                )
                return {
                    **self.status(),
                    "quality": report.to_dict(),
                    "voiceprint": None,
                }

            # ---- Re-verify everything that could have changed during capture ----
            # A five-sample enrollment takes about a minute. Consent can be
            # withdrawn in that time, and storing a template afterwards would mean
            # keeping biometric data the person had already refused.
            self._recheck_preconditions(active)

            try:
                self._set_state(active, EnrollmentState.ENCRYPTING)
                key = self._key_protector.create_if_missing(
                    created_utc=_utc_now()
                )
                cipher = VoiceprintCipher(key)
                centroid = _mean_unit_vector(embeddings)
                payload = {
                    "payload_schema": PAYLOAD_SCHEMA,
                    "centroid": centroid,
                    "dispersion": _dispersion(embeddings, centroid),
                    "sample_count": len(embeddings),
                    "embedding_dim": provider.spec.embedding_dim,
                    "dtype": "float64-json",
                    "samples": embeddings,
                    "preprocessing_id": provider.spec.preprocessing_id,
                }
                development_only = active.device_transport != "USB" or not (
                    active.transport_verified
                )
                voiceprint_uuid = str(uuid_module.uuid4())
                saved = self._store.save(
                    cipher=cipher,
                    payload=payload,
                    voiceprint_uuid=voiceprint_uuid,
                    participant_id=active.participant_id,
                    model=provider.spec.model_identity(),
                    enrollment_session_id=active.session_id,
                    consent_event_id=active.consent_event_id,
                    development_only=development_only,
                    device_fingerprint=active.device_fingerprint,
                    device_transport=active.device_transport,
                    sample_rate_hz=active.sample_rate_hz,
                    channels=active.channels,
                    quality_verdict=report.status.value,
                    min_pair_cosine=report.min_pair_cosine,
                    preprocessing_id=provider.spec.preprocessing_id,
                )
            except Exception as exc:
                self._fail(
                    active,
                    EnrollmentState.FAILED,
                    ReasonCode.STORAGE_FAILED,
                    type(exc).__name__,
                )
                raise EnrollmentError(
                    ReasonCode.STORAGE_FAILED,
                    "The voiceprint could not be stored, so no template was "
                    "activated. Nothing partial has been left behind.",
                ) from None
            finally:
                # The audio has served its purpose either way.
                active.drop_audio()

            verified = self._store.verify(voiceprint_uuid, cipher=cipher)
            self._persist_quality(active, report)
            self._set_state(active, EnrollmentState.COMPLETED)
            self._finish(active, action="ENROLLMENT_COMPLETED", detail={
                "session_uuid": active.session_uuid,
                "participant_uuid": active.participant_uuid,
                "voiceprint_uuid": voiceprint_uuid,
                "status": saved["status"],
                "quality_verdict": report.status.value,
                "min_pair_cosine": report.min_pair_cosine,
                "model_name": provider.spec.name,
                "model_version": provider.spec.version,
                "device_transport": active.device_transport,
                "verified": verified.ok,
            })
            status = self.status()
            self._active = None
            return {
                **status,
                "quality": report.to_dict(),
                "voiceprint": {
                    "voiceprint_uuid": voiceprint_uuid,
                    "status": saved["status"],
                    "production_eligible": saved["production_eligible"],
                    "verified": verified.ok,
                },
            }

    def _recheck_preconditions(self, active: _Active) -> None:
        """Verify nothing that matters changed while the operator was speaking."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT is_active FROM participants WHERE id = ?",
                (active.participant_id,),
            ).fetchone()
            consent_state = self._consent.state(conn, active.participant_id)
        finally:
            conn.close()

        if row is None or not bool(int(row["is_active"])):
            self._fail(
                active,
                EnrollmentState.CANCELLED,
                ReasonCode.PARTICIPANT_DEACTIVATED_DURING_ENROLLMENT,
                "",
            )
            raise EnrollmentError(
                ReasonCode.PARTICIPANT_DEACTIVATED_DURING_ENROLLMENT,
                "The participant was deactivated during enrollment, so no "
                "voiceprint was stored.",
            )
        if not consent_state.enrollment_allowed:
            self._fail(
                active,
                EnrollmentState.CANCELLED,
                ReasonCode.CONSENT_REVOKED_DURING_ENROLLMENT,
                "",
            )
            raise EnrollmentError(
                ReasonCode.CONSENT_REVOKED_DURING_ENROLLMENT,
                "Consent was withdrawn during enrollment. The captured audio has "
                "been discarded and no voiceprint was stored.",
            )
        if consent_state.event_id != active.consent_event_id:
            # Consent was revoked and re-granted mid-session: a new consent event
            # means a new enrollment, not a continuation of this one.
            self._fail(
                active,
                EnrollmentState.CANCELLED,
                ReasonCode.CONSENT_REVOKED_DURING_ENROLLMENT,
                "",
            )
            raise EnrollmentError(
                ReasonCode.CONSENT_REVOKED_DURING_ENROLLMENT,
                "The consent record changed during enrollment. Start a new "
                "enrollment so the template is bound to the current consent.",
            )

        device, error = self._recording.resolve_device()
        if device is None:
            self._fail(
                active, EnrollmentState.FAILED, ReasonCode.DEVICE_DISCONNECTED, ""
            )
            raise EnrollmentError(
                ReasonCode.DEVICE_DISCONNECTED,
                error or "The capture device is no longer available.",
            )
        if device.fingerprint != active.device_fingerprint:
            self._fail(active, EnrollmentState.FAILED, ReasonCode.DEVICE_CHANGED, "")
            raise EnrollmentError(
                ReasonCode.DEVICE_CHANGED,
                "The capture device changed during enrollment, so no voiceprint was "
                "stored.",
            )

        _, verdict, age = self._calibration_evidence()
        if verdict != "GOOD" or age is None or age > self._thresholds.max_calibration_age_days:
            self._fail(
                active, EnrollmentState.REJECTED, ReasonCode.CALIBRATION_INVALID, ""
            )
            raise EnrollmentError(
                ReasonCode.CALIBRATION_INVALID,
                "The microphone calibration is no longer valid, so no voiceprint "
                "was stored.",
            )

    def _persist_quality(self, active: _Active, report: Any) -> None:
        aggregate = report.samples[-1].snapshot if report.samples else None
        conn = self._connect()
        try:
            with maybe_transaction(conn):
                conn.execute(
                    "UPDATE enrollment_sessions SET speech_seconds = ?,"
                    " total_seconds = ?, peak_dbfs = ?, rms_dbfs = ?,"
                    " noise_floor_dbfs = ?, estimated_snr_db = ?,"
                    " clipping_percent = ?, silence_percent = ?,"
                    " speech_active_ratio = ?, dropped_frames = ?,"
                    " xrun_callbacks = ?, min_pair_cosine = ?, mean_pair_cosine = ?,"
                    " quality_verdict = ?,"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                    (
                        report.total_speech_seconds,
                        sum(q.seconds for q in report.samples),
                        aggregate.peak_dbfs if aggregate else None,
                        aggregate.rms_dbfs if aggregate else None,
                        aggregate.noise_floor_dbfs if aggregate else None,
                        next(
                            (
                                q.estimated_snr_db
                                for q in report.samples
                                if q.estimated_snr_db is not None
                            ),
                            None,
                        ),
                        aggregate.clipping_percent if aggregate else None,
                        aggregate.silence_percent if aggregate else None,
                        (
                            sum(q.speech_active_ratio for q in report.samples)
                            / len(report.samples)
                        )
                        if report.samples
                        else None,
                        sum(q.dropped_frames for q in report.samples),
                        sum(q.xrun_callbacks for q in report.samples),
                        report.min_pair_cosine,
                        report.mean_pair_cosine,
                        report.status.value,
                        active.session_id,
                    ),
                )
        finally:
            conn.close()

    # -- terminate ----------------------------------------------------------

    def cancel(self, *, reason: str | None = None) -> dict[str, Any]:
        """Abandon the active session. **Idempotent.**"""
        with self._state_lock:
            active = self._active
            if active is None or active.state.terminal:
                return self.status()
            self._fail(
                active,
                EnrollmentState.CANCELLED,
                ReasonCode.OPERATOR_CANCELLED,
                (reason or "cancelled by operator")[:200],
            )
            status = self.status()
            self._active = None
            return status

    def _fail(
        self,
        active: _Active,
        state: EnrollmentState,
        reason: ReasonCode,
        detail: str,
    ) -> None:
        """Move to a terminal state and release every resource.

        Called from every failure path, so the cleanup lives in one place: a buffer
        released here cannot be forgotten in a branch added later.
        """
        active.drop_audio()
        try:
            self._set_state(active, state, reason=reason)
        except InvalidEnrollmentTransition:
            # Already terminal, or an unusual route. Record the truth rather than
            # raising from inside a cleanup path.
            _LOG.warning(
                "Enrollment %s could not move from %s to %s during cleanup.",
                active.session_uuid,
                active.state.value,
                state.value,
            )
        action = {
            EnrollmentState.CANCELLED: "ENROLLMENT_CANCELLED",
            EnrollmentState.REJECTED: "ENROLLMENT_REJECTED",
            EnrollmentState.FAILED: "ENROLLMENT_FAILED",
        }.get(state, "ENROLLMENT_FAILED")
        self._finish(
            active,
            action=action,
            detail={
                "session_uuid": active.session_uuid,
                "participant_uuid": active.participant_uuid,
                "reason_code": reason.value,
                # Enumerated code plus a short safe note; never an exception string
                # that could carry a path or payload.
                "note": detail[:200] or None,
            },
        )

    def _finish(self, active: _Active, *, action: str, detail: dict[str, Any]) -> None:
        """Release the capture lock, close the provider, and audit."""
        active.drop_audio()
        provider = active.provider
        if provider is not None and self._injected_provider is None:
            # Only close a provider this service resolved. An injected one belongs
            # to the test that supplied it.
            try:
                provider.close()
            except Exception:  # noqa: BLE001 - never mask the real outcome
                _LOG.warning("Embedding provider did not close cleanly.")
        active.provider = None
        try:
            self._capture_lock.release()
        except Exception:  # noqa: BLE001
            _LOG.warning("Capture lock could not be released cleanly.")
        conn = self._connect()
        try:
            with maybe_transaction(conn):
                record_event(
                    conn,
                    category="PARTICIPANT",
                    action=action,
                    entity_type="enrollment_session",
                    entity_id=active.session_id,
                    detail=detail,
                )
        finally:
            conn.close()

    def shutdown(self) -> None:
        """Abandon any live session. Safe to call from application shutdown."""
        with self._state_lock:
            if self._active is not None and not self._active.state.terminal:
                self.cancel(reason="application shutdown")

    # -- reporting ----------------------------------------------------------

    def _require_active(self) -> _Active:
        active = self._active
        if active is None or active.state.terminal:
            raise EnrollmentError(
                ReasonCode.INTERNAL_ERROR,
                "No enrollment is in progress. Start one before capturing samples.",
            )
        return active

    def status(self) -> dict[str, Any]:
        """Cheap snapshot, safe to poll. Contains no audio, vector or key."""
        active = self._active
        if active is None:
            return {
                "active": False,
                "session_uuid": None,
                "state": EnrollmentState.CREATED.value,
                "samples_accepted": 0,
                "samples_target": DEFAULT_SAMPLE_TARGET,
                "buffered_bytes": 0,
                "buffer_limit_bytes": MAX_TOTAL_CAPTURE_BYTES,
            }
        return {
            "active": not active.state.terminal,
            "session_uuid": active.session_uuid,
            "participant_uuid": active.participant_uuid,
            "state": active.state.value,
            "reason_code": active.reason_code,
            "attempt": active.attempt,
            "samples_accepted": len(active.samples),
            "samples_target": active.samples_target,
            "buffered_bytes": active.buffered_bytes,
            "buffer_limit_bytes": MAX_TOTAL_CAPTURE_BYTES,
            "device": {
                "fingerprint": active.device_fingerprint,
                "name": active.device_name,
                "transport": active.device_transport,
                "transport_verified": active.transport_verified,
            },
            "profile": {
                "sample_rate_hz": active.sample_rate_hz,
                "channels": active.channels,
            },
            "samples": [q.to_dict() for q in active.quality],
            "will_be_development_only": (
                active.device_transport != "USB" or not active.transport_verified
            ),
        }

    # -- revocation ---------------------------------------------------------

    def revoke_consent_and_delete(
        self, participant_uuid: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        """Withdraw consent, then destroy every voiceprint for that participant.

        Order is deliberate and must not be swapped: the ``REVOKED`` event is
        committed **first**, so from that instant the participant is ineligible
        regardless of what the filesystem does next. If deletion then fails, the
        template becomes ``DELETE_PENDING`` -- unusable and retryable -- rather than
        remaining usable because an unlink returned an error.
        """
        conn = self._connect()
        try:
            row = self._participant_row(conn, participant_uuid)
            participant_id = int(row["id"])
        finally:
            conn.close()

        with self._state_lock:
            active = self._active
            if (
                active is not None
                and not active.state.terminal
                and active.participant_id == participant_id
            ):
                # Withdrawal during their own enrollment: stop it before deleting.
                self._fail(
                    active,
                    EnrollmentState.CANCELLED,
                    ReasonCode.CONSENT_REVOKED_DURING_ENROLLMENT,
                    "",
                )
                self._active = None

        consent_result = self._consent.revoke(participant_id, reason=reason)
        deletion = self._store.delete_for_revocation(
            participant_id, reason=reason or "consent revoked"
        )
        return {
            "participant_uuid": participant_uuid,
            "consent": consent_result,
            "deletion": deletion,
            "eligible": False,
        }

    def eligibility(self, participant_uuid: str) -> dict[str, Any]:
        """The single fail-closed eligibility policy Phase 6 must call.

        Deliberately conservative: anything it cannot positively confirm is
        ``False``. Phase 6 has no business re-deriving this from raw rows.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, is_active FROM participants WHERE uuid = ?",
                (participant_uuid,),
            ).fetchone()
            if row is None:
                return {
                    "participant_uuid": participant_uuid,
                    "eligible_for_identification": False,
                    "production_eligible": False,
                    "reasons": ["UNKNOWN_PARTICIPANT"],
                }
            participant_id = int(row["id"])
            consent_state = self._consent.state(conn, participant_id)
        finally:
            conn.close()

        voiceprints = self._store.status_for_participant(participant_id)
        reasons: list[str] = []
        if not bool(int(row["is_active"])):
            reasons.append("PARTICIPANT_INACTIVE")
        if not consent_state.active:
            reasons.append("CONSENT_NOT_ACTIVE")
        if not voiceprints["has_usable_voiceprint"]:
            reasons.append("NO_USABLE_VOICEPRINT")
        return {
            "participant_uuid": participant_uuid,
            "eligible_for_identification": not reasons,
            "production_eligible": not reasons and voiceprints["production_eligible"],
            "reasons": reasons,
            "voiceprint": voiceprints["current"],
        }


# ---------------------------------------------------------------------------
# Small numeric helpers. Kept here because they operate on embeddings, which
# never leave this module in plaintext.
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    from mom_igd.audio.manifest import utc_now_iso

    return utc_now_iso()


def _mean_unit_vector(vectors: list[list[float]]) -> list[float]:
    """Mean of unit vectors, renormalised.

    The centroid of several embeddings of one voice is the template. Renormalising
    matters: the mean of unit vectors is shorter than one, and Phase 6 compares
    with cosine on normalised vectors.
    """
    import math

    dim = len(vectors[0])
    total = [0.0] * dim
    for vector in vectors:
        for i, value in enumerate(vector):
            total[i] += value
    mean = [v / len(vectors) for v in total]
    norm = math.sqrt(sum(v * v for v in mean))
    if norm == 0.0:  # pragma: no cover - would mean perfectly opposed vectors
        raise ValueError("Embeddings cancel out; centroid is undefined.")
    return [v / norm for v in mean]


def _dispersion(vectors: list[list[float]], centroid: list[float]) -> list[float]:
    """Per-dimension standard deviation around the centroid.

    Stored so Phase 6 can weight dimensions that are stable for this speaker more
    heavily than ones that vary. Inside the ciphertext, like every other biometric
    component.
    """
    import math

    dim = len(centroid)
    if len(vectors) < 2:
        return [0.0] * dim
    out: list[float] = []
    for i in range(dim):
        mean = centroid[i]
        variance = sum((v[i] - mean) ** 2 for v in vectors) / (len(vectors) - 1)
        out.append(math.sqrt(max(0.0, variance)))
    return out
