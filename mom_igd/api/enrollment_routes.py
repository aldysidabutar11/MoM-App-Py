"""Phase 3 endpoints: participants, biometric consent, enrollment, voiceprints.

Every route here is token-protected. There is no public Phase 3 endpoint, and there
is no reason for one: everything in this router concerns identifiable people and
their biometric data.

**Rules this module holds to.**

* **No biometric payload leaves the process.** No response carries PCM, an
  embedding, a centroid, a dispersion vector, ciphertext, a nonce, a DPAPI blob or
  key material. The store's own API makes that easy to honour -- ``status_for_*``
  and ``verify`` return metadata and verdicts, never plaintext.
* **No filesystem path in any response.** A voiceprint is addressed by UUID; the
  client never learns and never supplies a path.
* **No client-chosen provider.** There is deliberately no request field, query
  parameter or header that can select an embedding provider. The test double is
  reachable only by constructor injection inside the test suite.
* **Read-only routes touch no hardware and no key.** Listing participants, reading
  consent, checking readiness and reading voiceprint status open no stream, create
  no DPAPI key, decrypt nothing and load no model.
* **A quality rejection is not a server error.** ``finalize`` returning
  ``{"voiceprint": null}`` is an expected outcome and answers HTTP 200 with the
  quality report, because the request was processed correctly -- the audio simply
  was not good enough.

**Raw audio never crosses this boundary.** The browser has no microphone access:
sample capture is driven by :class:`EnrollmentCaptureController` inside the Python
process, which hands PCM to the service in memory. The UI sends "start a sample" and
"stop the sample", and receives levels, duration and a quality verdict -- never
audio. See ADR-0012.
"""

from __future__ import annotations

import re
import threading
from typing import Annotated, Any, Final

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from pydantic import StrictInt

from mom_igd.api.deps import require_session_token
from mom_igd.enrollment.consent import ConfirmationMethod, ConsentError, ConsentService
from mom_igd.enrollment.participants import (
    MINIMUM_MEETING_CAPACITY,
    ParticipantError,
    ParticipantService,
)
from mom_igd.enrollment.provider import ModelUnavailableError, ProviderError
from mom_igd.enrollment.service import (
    EnrollmentError,
    EnrollmentService,
    InvalidEnrollmentTransition,
    ReasonCode,
)
from mom_igd.enrollment.store import VoiceprintStoreError
from mom_igd.logging_setup import get_logger

__all__ = ["enrollment_router", "get_enrollment_context"]

_LOG = get_logger("api.enrollment")

enrollment_router = APIRouter(
    prefix="/enrollment",
    tags=["enrollment"],
    dependencies=[Depends(require_session_token)],
)

_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

_CONTEXT_LOCK: Final[threading.Lock] = threading.Lock()

# Reason codes that describe a conflict with the current state of the world rather
# than a malformed request. Mapped exhaustively, and a test asserts every member of
# ReasonCode appears in exactly one bucket -- so a new code cannot silently default
# to 500.
_CONFLICT_REASONS: Final[frozenset[ReasonCode]] = frozenset(
    {
        ReasonCode.CONSENT_MISSING,
        ReasonCode.CONSENT_REVOKED_DURING_ENROLLMENT,
        ReasonCode.PARTICIPANT_INACTIVE,
        ReasonCode.PARTICIPANT_DEACTIVATED_DURING_ENROLLMENT,
        ReasonCode.CAPTURE_LOCK_HELD,
        ReasonCode.NO_DEVICE,
        ReasonCode.DEVICE_CHANGED,
        ReasonCode.DEVICE_DISCONNECTED,
        ReasonCode.PREFLIGHT_FAILED,
        ReasonCode.CALIBRATION_INVALID,
        ReasonCode.QUALITY_REJECTED,
        ReasonCode.BUFFER_LIMIT_EXCEEDED,
        ReasonCode.OPERATOR_CANCELLED,
    }
)
_UNAVAILABLE_REASONS: Final[frozenset[ReasonCode]] = frozenset(
    {ReasonCode.MODEL_UNAVAILABLE, ReasonCode.TEST_DOUBLE_REFUSED}
)
_SERVER_REASONS: Final[frozenset[ReasonCode]] = frozenset(
    {
        ReasonCode.EMBEDDING_INVALID,
        ReasonCode.STORAGE_FAILED,
        ReasonCode.INTERNAL_ERROR,
    }
)


class _Context:
    """The Phase 3 services for one application instance."""

    __slots__ = ("people", "consent", "enrollment", "capture", "connect")

    def __init__(self, config: Any, paths: Any, recording_service: Any) -> None:
        from mom_igd.enrollment.capture import EnrollmentCaptureController

        def _connect():
            from mom_igd.db.connection import connect

            return connect(
                paths.database_path(config.database.filename),
                busy_timeout_ms=config.database.busy_timeout_ms,
            )

        self.connect = _connect
        self.people = ParticipantService(_connect, config=config)
        self.consent = ConsentService(_connect)
        self.enrollment = EnrollmentService(
            config, paths, recording_service=recording_service
        )
        self.capture = EnrollmentCaptureController(
            recording_service=recording_service, enrollment_service=self.enrollment
        )

    @property
    def voiceprints(self):
        """The voiceprint store the enrollment service already owns.

        Exposed as a named property so the routes do not reach into a private
        attribute; there must be exactly one store per data root, and creating a
        second here would give two objects an opinion about the same files.
        """
        return self.enrollment.store

    def participant_id(self, participant_uuid: str) -> int:
        """Resolve a UUID to the internal row id, or 404.

        The row id never leaves the process -- clients address participants by UUID
        only -- but the consent and voiceprint services key on it. Doing the lookup
        in one place keeps the routes from repeating a six-line query, and keeps the
        404 wording consistent.
        """
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT id FROM participants WHERE uuid = ?", (participant_uuid,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No participant with uuid={participant_uuid!r}.",
            )
        return int(row["id"])


def get_enrollment_context(request: Request) -> _Context:
    """Return the process-wide Phase 3 context, creating it on first use.

    Lazy for the same reason the recording service is: importing the app or serving
    ``/health`` must not construct an audio backend. Behind a lock with a re-check,
    because uvicorn runs synchronous endpoints in a threadpool -- two first requests
    arriving together would otherwise each build a context, and the loser's live
    enrollment would become unreachable.
    """
    existing = getattr(request.app.state, "enrollment_context", None)
    if existing is not None:
        return existing
    with _CONTEXT_LOCK:
        existing = getattr(request.app.state, "enrollment_context", None)
        if existing is not None:
            return existing
        from mom_igd.api.audio_routes import get_recording_service

        context = _Context(
            request.app.state.config,
            request.app.state.paths,
            get_recording_service(request),
        )
        request.app.state.enrollment_context = context
        return context


ContextDep = Annotated[_Context, Depends(get_enrollment_context)]


def _guard(callable_, *args, **kwargs):
    """Translate service errors into the right HTTP status.

    An unexpected exception becomes a generic 500 with a correlation-free message:
    the detail goes to the local log, because a traceback or a path in an HTTP body
    is an information leak even on loopback.
    """
    try:
        return callable_(*args, **kwargs)
    except InvalidEnrollmentTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from None
    except EnrollmentError as exc:
        raise HTTPException(
            status_code=_status_for(exc.reason),
            detail={"reason": exc.reason.value, "message": str(exc)},
        ) from None
    except ModelUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason": ReasonCode.MODEL_UNAVAILABLE.value, "message": str(exc)},
        ) from None
    except (ConsentError, ParticipantError) as exc:
        message = str(exc)
        not_found = message.startswith("No participant") or message.startswith(
            "No meeting"
        )
        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND if not_found else status.HTTP_409_CONFLICT
            ),
            detail=message,
        ) from None
    except VoiceprintStoreError as exc:
        message = str(exc)
        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND
                if message.startswith("No voiceprint")
                else status.HTTP_409_CONFLICT
            ),
            detail=message,
        ) from None
    except ProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from None
    except Exception:
        # Never surface the exception. It can carry a path, a device name, or -- in
        # the worst case -- part of a payload.
        _LOG.exception("Unhandled error in a Phase 3 endpoint.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "An internal error occurred and has been logged locally. No "
                "voiceprint was created or modified by the failed request."
            ),
        ) from None


def _status_for(reason: ReasonCode) -> int:
    if reason in _UNAVAILABLE_REASONS:
        return status.HTTP_503_SERVICE_UNAVAILABLE
    if reason in _CONFLICT_REASONS:
        return status.HTTP_409_CONFLICT
    if reason in _SERVER_REASONS:
        return status.HTTP_500_INTERNAL_SERVER_ERROR
    # Unreachable while the exhaustiveness test passes; conservative if it ever is.
    return status.HTTP_409_CONFLICT


def _uuid(value: str, *, label: str) -> str:
    lowered = value.lower()
    if not _UUID_RE.match(lowered):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{label} must be a canonical lower-case UUID.",
        )
    return lowered


# ============================================================== participants


@enrollment_router.get("/participants", summary="List participants (opens no device)")
def list_participants(
    context: ContextDep,
    search: str | None = Query(default=None, max_length=120),
    include_inactive: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    listing = _guard(
        context.people.list,
        search=search,
        include_inactive=include_inactive,
        limit=limit,
        offset=offset,
    )
    _decorate_with_state(context, listing["participants"])
    return listing


def _decorate_with_state(context: _Context, entries: list[dict[str, Any]]) -> None:
    """Attach consent and voiceprint state to participant rows, in place.

    **One HTTP request per screen, not one per person.** The participant table and
    the meeting roster both need a consent badge and a voiceprint badge for every
    row; without this the page would have to fan out a request per participant,
    which is exactly what a 50-person roster must not do.

    Shared by the directory listing and the roster so the two cannot drift into
    showing different state for the same person. Row ids are resolved in a single
    query rather than one per entry.
    """
    if not entries:
        return
    uuids = [str(e["uuid"]) for e in entries if e.get("uuid")]
    if not uuids:
        return
    conn = context.connect()
    try:
        placeholders = ",".join("?" * len(uuids))
        ids = {
            str(row["uuid"]): int(row["id"])
            for row in conn.execute(
                f"SELECT id, uuid FROM participants WHERE uuid IN ({placeholders})",
                uuids,
            )
        }
        for entry in entries:
            participant_id = ids.get(str(entry.get("uuid")))
            if participant_id is None:  # pragma: no cover - rows exist by construction
                continue
            entry["consent"] = context.consent.state(conn, participant_id).to_dict()
            entry["voiceprint"] = context.voiceprints.status_for_participant(
                participant_id
            )["current"]
    finally:
        conn.close()


@enrollment_router.get(
    "/participants/{participant_uuid}", summary="One participant with consent state"
)
def participant_detail(context: ContextDep, participant_uuid: str) -> dict[str, Any]:
    resolved = _uuid(participant_uuid, label="participant_uuid")
    person = _guard(context.people.get, resolved)
    participant_id = context.participant_id(resolved)
    conn = context.connect()
    try:
        consent_state = context.consent.state(conn, participant_id).to_dict()
        history = context.consent.history(conn, participant_id, limit=50)
    finally:
        conn.close()
    return {
        "participant": person.to_dict(),
        "consent": consent_state,
        "consent_history": history,
        "voiceprint": context.voiceprints.status_for_participant(participant_id),
        "eligibility": context.enrollment.eligibility(resolved),
    }


@enrollment_router.post(
    "/participants", status_code=status.HTTP_201_CREATED, summary="Register a person"
)
def create_participant(
    context: ContextDep,
    display_name: Annotated[str, Body(embed=True, min_length=1, max_length=120)],
    role: Annotated[str | None, Body(embed=True, max_length=80)] = None,
    email: Annotated[str | None, Body(embed=True, max_length=254)] = None,
    external_ref: Annotated[str | None, Body(embed=True, max_length=120)] = None,
    notes: Annotated[str | None, Body(embed=True, max_length=1000)] = None,
) -> dict[str, Any]:
    """Duplicate display names are accepted on purpose: identity is the UUID."""
    person = _guard(
        context.people.create,
        display_name=display_name,
        role=role,
        email=email,
        external_ref=external_ref,
        notes=notes,
    )
    return {"participant": person.to_dict()}


@enrollment_router.patch(
    "/participants/{participant_uuid}", summary="Edit descriptive fields"
)
def update_participant(
    context: ContextDep,
    participant_uuid: str,
    display_name: Annotated[str | None, Body(embed=True, max_length=120)] = None,
    role: Annotated[str | None, Body(embed=True, max_length=80)] = None,
    email: Annotated[str | None, Body(embed=True, max_length=254)] = None,
    external_ref: Annotated[str | None, Body(embed=True, max_length=120)] = None,
    notes: Annotated[str | None, Body(embed=True, max_length=1000)] = None,
) -> dict[str, Any]:
    person = _guard(
        context.people.update,
        _uuid(participant_uuid, label="participant_uuid"),
        display_name=display_name,
        role=role,
        email=email,
        external_ref=external_ref,
        notes=notes,
    )
    return {"participant": person.to_dict()}


@enrollment_router.post(
    "/participants/{participant_uuid}/deactivate",
    summary="Deactivate (never deletes)",
)
def deactivate_participant(
    context: ContextDep,
    participant_uuid: str,
    reason: Annotated[str | None, Body(embed=True, max_length=300)] = None,
) -> dict[str, Any]:
    """Deletion is not offered: history references the row (ADR-0009)."""
    person = _guard(
        context.people.set_active,
        _uuid(participant_uuid, label="participant_uuid"),
        active=False,
        reason=reason,
    )
    return {"participant": person.to_dict()}


@enrollment_router.post(
    "/participants/{participant_uuid}/reactivate", summary="Reactivate a participant"
)
def reactivate_participant(
    context: ContextDep, participant_uuid: str
) -> dict[str, Any]:
    person = _guard(
        context.people.set_active,
        _uuid(participant_uuid, label="participant_uuid"),
        active=True,
    )
    return {"participant": person.to_dict()}


# ======================================================= meeting membership


@enrollment_router.get("/meetings", summary="Meetings a roster can be attached to")
def list_meetings(
    context: ContextDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Bounded listing. Returns meeting UUIDs, never internal row ids."""
    return _guard(context.people.meetings, limit=limit, offset=offset)


@enrollment_router.get(
    "/meetings/{meeting_uuid}/roster",
    summary="Roster count, this meeting's capacity, and the configured ceiling",
)
def meeting_roster(context: ContextDep, meeting_uuid: str) -> dict[str, Any]:
    """The roster, its capacity, its settable bounds, and each member's state.

    Decorated with consent and voiceprint state for the same reason the directory
    listing is: the roster table shows a badge per member, and fetching those one
    participant at a time is what makes a large roster unusable.
    """
    roster = _guard(
        context.people.meeting_participants, _uuid(meeting_uuid, label="meeting_uuid")
    )
    _decorate_with_state(context, roster.get("participants", []))
    return roster


@enrollment_router.patch(
    "/meetings/{meeting_uuid}/capacity",
    summary="Change this meeting's roster capacity",
)
def set_meeting_capacity(
    context: ContextDep,
    meeting_uuid: str,
    capacity: Annotated[StrictInt, Body(embed=True)],
) -> dict[str, Any]:
    """Set one meeting's roster capacity.

    Status codes are split by *kind* of problem, consistently:

    * ``422`` -- the value itself is not acceptable: not an integer, below one, or
      above the highest value this meeting may be set to.
    * ``409`` -- the value is acceptable by itself but conflicts with this meeting's
      current state, i.e. it is below the number of people already on the roster.
      Capacity changes never remove a participant to make room.
    * ``404`` -- no such meeting.

    The upper bound comes from :meth:`settable_capacity_bounds`, not from the raw
    ceiling. A meeting stored above a since-lowered ceiling is grandfathered: it may
    still be reduced, so validating against the ceiling alone would refuse a change
    that moves the deployment *toward* compliance. That one lookup also means the
    ``422`` message can state the actual permitted range rather than a range the
    meeting does not have.

    The annotation is ``StrictInt``, not ``int``: FastAPI's default coercion accepts
    ``true`` as 1 and ``"12"`` as 12, so a plain ``int`` would silently take a
    boolean or a quoted number as a roster size. ``StrictInt`` refuses all three,
    along with ``12.0`` and ``12.5``. The service re-checks everything inside its
    transaction, so a non-HTTP caller gets the same rules and a concurrent change
    cannot slip past this pre-check.
    """
    people = context.people
    resolved = _uuid(meeting_uuid, label="meeting_uuid")
    bounds = _guard(people.settable_capacity_bounds, resolved)
    lowest = MINIMUM_MEETING_CAPACITY
    highest = int(bounds["capacity_max_settable"])
    if capacity < lowest or capacity > highest:
        detail = (
            f"capacity must be between {lowest} and {highest} for this meeting. "
            "A larger ceiling does not improve speaker-recognition accuracy."
        )
        if bounds["capacity_above_ceiling"]:
            detail = f"{detail} {bounds['capacity_notice']}"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail
        )
    return _guard(people.set_meeting_capacity, resolved, capacity)


@enrollment_router.get(
    "/meetings/{meeting_uuid}/participants", summary="Who is expected in a meeting"
)
def meeting_participants(context: ContextDep, meeting_uuid: str) -> dict[str, Any]:
    return _guard(
        context.people.meeting_participants, _uuid(meeting_uuid, label="meeting_uuid")
    )


@enrollment_router.post(
    "/meetings/{meeting_uuid}/participants",
    summary="Add to the roster, up to this meeting's capacity",
)
def add_meeting_participant(
    context: ContextDep,
    meeting_uuid: str,
    participant_uuid: Annotated[str, Body(embed=True, min_length=36, max_length=36)],
    seat_label: Annotated[str | None, Body(embed=True, max_length=40)] = None,
) -> dict[str, Any]:
    return _guard(
        context.people.add_to_meeting,
        _uuid(meeting_uuid, label="meeting_uuid"),
        _uuid(participant_uuid, label="participant_uuid"),
        seat_label=seat_label,
    )


@enrollment_router.delete(
    "/meetings/{meeting_uuid}/participants/{participant_uuid}",
    summary="Remove from a meeting (row survives as history)",
)
def remove_meeting_participant(
    context: ContextDep, meeting_uuid: str, participant_uuid: str
) -> dict[str, Any]:
    return _guard(
        context.people.remove_from_meeting,
        _uuid(meeting_uuid, label="meeting_uuid"),
        _uuid(participant_uuid, label="participant_uuid"),
    )


# =================================================================== consent


@enrollment_router.get("/consent/text", summary="The consent text and its hash")
def consent_text(context: ContextDep) -> dict[str, Any]:
    """The wording a dialog must display, with the hash it has to echo back."""
    return context.consent.text_bundle()


@enrollment_router.get(
    "/participants/{participant_uuid}/consent", summary="Current consent state"
)
def consent_status(context: ContextDep, participant_uuid: str) -> dict[str, Any]:
    resolved = _uuid(participant_uuid, label="participant_uuid")
    participant_id = context.participant_id(resolved)
    conn = context.connect()
    try:
        return {
            "consent": context.consent.state(conn, participant_id).to_dict(),
            "history": context.consent.history(conn, participant_id, limit=100),
        }
    finally:
        conn.close()


@enrollment_router.post(
    "/participants/{participant_uuid}/consent/grant", summary="Record explicit consent"
)
def grant_consent(
    context: ContextDep,
    participant_uuid: str,
    acknowledged_text_sha256: Annotated[
        str, Body(embed=True, min_length=64, max_length=64)
    ],
    confirmed_by_participant: Annotated[bool, Body(embed=True)] = True,
) -> dict[str, Any]:
    """The caller must echo the hash of the text it actually displayed.

    That is what stops a UI granting consent to wording nobody saw. A mismatch is
    refused rather than reconciled -- there is no safe way to guess which version
    was on screen.
    """
    resolved = _uuid(participant_uuid, label="participant_uuid")
    participant_id = context.participant_id(resolved)

    method = (
        ConfirmationMethod.PARTICIPANT_CONFIRMED_ON_DEVICE
        if confirmed_by_participant
        else ConfirmationMethod.OPERATOR_CONFIRMED_IN_PERSON
    )
    result = _guard(
        context.consent.grant,
        participant_id,
        confirmation_method=method,
        acknowledged_text_sha256=acknowledged_text_sha256.lower(),
    )
    return {"granted": result}


@enrollment_router.post(
    "/participants/{participant_uuid}/consent/revoke",
    summary="Withdraw consent and destroy every voiceprint",
)
def revoke_consent(
    context: ContextDep,
    participant_uuid: str,
    reason: Annotated[str | None, Body(embed=True, max_length=300)] = None,
) -> dict[str, Any]:
    """Appends the REVOKED event first, then deletes the ciphertext.

    If deletion fails the template becomes ``DELETE_PENDING`` -- still unusable, and
    retryable -- so the response can honestly report an incomplete cleanup while the
    participant is already ineligible.
    """
    return _guard(
        context.enrollment.revoke_consent_and_delete,
        _uuid(participant_uuid, label="participant_uuid"),
        reason=reason,
    )


# ================================================================ enrollment


@enrollment_router.get(
    "/participants/{participant_uuid}/readiness",
    summary="Can enrollment start? (opens no device, creates no key)",
)
def enrollment_readiness(context: ContextDep, participant_uuid: str) -> dict[str, Any]:
    return _guard(
        context.enrollment.readiness, _uuid(participant_uuid, label="participant_uuid")
    )


@enrollment_router.post("/sessions", summary="Start an enrollment (idempotent)")
def start_enrollment(
    context: ContextDep,
    participant_uuid: Annotated[str, Body(embed=True, min_length=36, max_length=36)],
    samples_target: Annotated[int, Body(embed=True, ge=1, le=10)] = 5,
) -> dict[str, Any]:
    """No provider may be named by the caller: there is deliberately no such field."""
    return _guard(
        context.enrollment.start,
        _uuid(participant_uuid, label="participant_uuid"),
        samples_target=samples_target,
    )


@enrollment_router.get("/sessions/current", summary="Enrollment status (cheap to poll)")
def enrollment_status(context: ContextDep) -> dict[str, Any]:
    return _guard(context.enrollment.status)


@enrollment_router.post(
    "/sessions/current/samples", summary="Capture one sample (OPENS the microphone)"
)
def capture_sample(
    context: ContextDep,
    seconds: Annotated[float, Body(embed=True, ge=1.0, le=15.0)] = 10.0,
) -> dict[str, Any]:
    """Record one sample entirely inside the Python process.

    The browser never touches the microphone and never sees audio: this call opens
    the Phase 2 device, fills a bounded in-memory buffer, hands it to the enrollment
    service, and returns only levels, duration and a quality verdict.
    """
    return _guard(context.capture.capture_sample, seconds=seconds)


@enrollment_router.post(
    "/sessions/current/finalize", summary="Embed, encrypt and store the voiceprint"
)
def finalize_enrollment(context: ContextDep) -> dict[str, Any]:
    """A quality rejection answers 200 with ``voiceprint: null``.

    The request was processed correctly; the audio simply was not good enough. A 500
    would say the server malfunctioned, which would be false and would hide a
    result the operator needs to read.
    """
    return _guard(context.enrollment.finalize)


@enrollment_router.post("/sessions/current/cancel", summary="Abandon (idempotent)")
def cancel_enrollment(
    context: ContextDep,
    reason: Annotated[str | None, Body(embed=True, max_length=300)] = None,
) -> dict[str, Any]:
    _guard(context.capture.abort)
    return _guard(context.enrollment.cancel, reason=reason)


# ================================================================ voiceprints


@enrollment_router.get(
    "/participants/{participant_uuid}/voiceprint", summary="Voiceprint status"
)
def voiceprint_status(context: ContextDep, participant_uuid: str) -> dict[str, Any]:
    resolved = _uuid(participant_uuid, label="participant_uuid")
    return _guard(
        context.voiceprints.status_for_participant, context.participant_id(resolved)
    )


@enrollment_router.get(
    "/participants/{participant_uuid}/eligibility",
    summary="Fail-closed identification eligibility (for Phase 6)",
)
def eligibility(context: ContextDep, participant_uuid: str) -> dict[str, Any]:
    return _guard(
        context.enrollment.eligibility, _uuid(participant_uuid, label="participant_uuid")
    )


@enrollment_router.post(
    "/voiceprints/{voiceprint_uuid}/verify", summary="Verify integrity"
)
def verify_voiceprint(context: ContextDep, voiceprint_uuid: str) -> dict[str, Any]:
    """Verifies without a key: hashes and bindings only.

    Deliberately does not unwrap the master key. An integrity check that had to
    decrypt would mean any caller could pull plaintext into the process, and the
    checks that matter here -- file present, size, envelope hash, schema,
    participant/UUID/model binding -- need no key at all.
    """
    resolved = _uuid(voiceprint_uuid, label="voiceprint_uuid")
    outcome = _guard(context.voiceprints.verify, resolved)
    return outcome.to_dict()


@enrollment_router.get("/cleanup/pending", summary="Voiceprints awaiting deletion")
def pending_cleanup(context: ContextDep) -> dict[str, Any]:
    conn = context.connect()
    try:
        rows = conn.execute(
            "SELECT voiceprint_uuid, status, delete_error, revoked_at FROM voiceprints"
            " WHERE status IN ('DELETE_PENDING','INTEGRITY_FAILED','PENDING_WRITE')"
            " ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()
    return {
        "pending": [
            {
                "voiceprint_uuid": str(r["voiceprint_uuid"]),
                "status": str(r["status"]),
                # Already sanitised at the source: an errno, never a path.
                "delete_error": r["delete_error"],
                "revoked_at": r["revoked_at"],
            }
            for r in rows
        ],
        "count": len(rows),
    }


@enrollment_router.post("/cleanup/retry", summary="Retry deletion (idempotent)")
def retry_cleanup(context: ContextDep) -> dict[str, Any]:
    report = _guard(context.voiceprints.retry_pending_cleanup)
    return report.to_dict()
