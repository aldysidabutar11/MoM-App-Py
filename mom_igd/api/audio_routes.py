"""Phase 2 audio endpoints. All token-protected, all loopback-only.

Rules this module holds to:

* **No absolute filesystem path in any response.** A recording is addressed by its
  UUID; the client never learns, and never supplies, a path.
* **No path from the client, ever.** The only identifiers accepted are a device
  fingerprint and a recording UUID, both validated by shape before use.
* **An illegal lifecycle request is a 409, not a 500.** Asking to pause a
  recording that is not running is a client mistake with a clear answer.
* **The microphone opens only on an explicit request.** ``/devices``,
  ``/preflight`` and ``/status`` never touch hardware; ``/calibrate``,
  ``/open-test`` and ``/start`` do, and are POSTs for that reason.
"""

from __future__ import annotations

import re
import threading
from typing import Annotated, Any, Final

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from mom_igd.api.deps import require_session_token
from mom_igd.audio.backend import AudioError, DeviceNotFoundError, StreamError
from mom_igd.audio.service import (
    InvalidLifecycleTransition,
    RecordingService,
    RecordingServiceError,
)

__all__ = ["audio_router", "get_recording_service"]

audio_router = APIRouter(
    prefix="/audio", tags=["audio"], dependencies=[Depends(require_session_token)]
)

_FINGERPRINT_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{32}$")
_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


_SERVICE_LOCK: Final[threading.Lock] = threading.Lock()


def get_recording_service(request: Request) -> RecordingService:
    """Return the process-wide recording service, creating it on first use.

    One instance per process, because the selected device, the lifecycle state and
    the live capture session all live in it. Created lazily so importing the app --
    or serving ``/health`` -- never constructs an audio backend.

    The lock matters: uvicorn runs synchronous endpoints in a threadpool, so two
    requests arriving together would otherwise each build a service and the second
    would replace the first in ``app.state``. The losing instance would keep a
    device selection -- or an active recording -- that no later request can reach.
    """
    existing = getattr(request.app.state, "recording_service", None)
    if existing is not None:
        return existing
    with _SERVICE_LOCK:
        # Re-check: another thread may have created it while we waited.
        existing = getattr(request.app.state, "recording_service", None)
        if existing is not None:
            return existing
        service = RecordingService(request.app.state.config, request.app.state.paths)
        request.app.state.recording_service = service
        return service


ServiceDep = Annotated[RecordingService, Depends(get_recording_service)]


def _guard(callable_, *args, **kwargs):
    """Translate service errors into the right HTTP status."""
    try:
        return callable_(*args, **kwargs)
    except InvalidLifecycleTransition as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except DeviceNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except RecordingServiceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except StreamError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from None
    except AudioError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None


# ---------------------------------------------------------------- devices


@audio_router.get("/devices", summary="List capture devices (opens no stream)")
def list_devices(service: ServiceDep, refresh: bool = Query(default=True)) -> dict[str, Any]:
    return _guard(service.list_devices, refresh=refresh)


@audio_router.post("/devices/select", summary="Choose a capture device explicitly")
def select_device(
    service: ServiceDep,
    fingerprint: Annotated[str, Body(embed=True, min_length=32, max_length=32)],
) -> dict[str, Any]:
    if not _FINGERPRINT_RE.match(fingerprint):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "fingerprint must be 32 lower-case hex characters. Obtain one from "
                "GET /audio/devices; a device index is not an identity."
            ),
        )
    device = _guard(service.select_device, fingerprint)
    return {"selected": device.to_dict()}


# -------------------------------------------------------- gate and levels


@audio_router.get("/preflight", summary="Pre-recording checks (opens no stream)")
def preflight(
    service: ServiceDep,
    planned_minutes: float = Query(default=120.0, gt=0, le=1440),
) -> dict[str, Any]:
    return _guard(service.preflight, planned_minutes=planned_minutes).to_dict()


@audio_router.post("/open-test", summary="Briefly open the microphone to prove it works")
def open_test(service: ServiceDep) -> dict[str, Any]:
    return _guard(service.open_test)


@audio_router.post("/calibrate", summary="Microphone level test (opens the microphone)")
def calibrate(
    service: ServiceDep,
    seconds: float | None = Query(default=None, gt=0, le=60),
) -> dict[str, Any]:
    # Audio is never persisted here: save_to is deliberately not exposed over the
    # API, so a calibration clip cannot be written by a remote call.
    return _guard(service.calibrate, seconds=seconds).to_dict()


# ------------------------------------------------------------- lifecycle


@audio_router.post("/recordings/start", summary="Start recording (opens the microphone)")
def start_recording(
    service: ServiceDep,
    meeting_id: Annotated[int | None, Body(embed=True, ge=1)] = None,
    meeting_title: Annotated[str | None, Body(embed=True, max_length=200)] = None,
    planned_minutes: Annotated[float | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    """Start a recording.

    ``meeting_id`` is optional on purpose: Meeting setup is a later phase, so a
    fresh install has no meeting row and the operator must not be asked to invent
    an internal database id. Omit it and a draft meeting is created from
    ``meeting_title`` (or a UTC timestamp when that is blank too).
    """
    return _guard(
        service.start,
        meeting_id,
        meeting_title=meeting_title,
        planned_minutes=planned_minutes,
    )


@audio_router.post("/recordings/pause", summary="Pause: closes a chunk boundary")
def pause_recording(service: ServiceDep) -> dict[str, Any]:
    return _guard(service.pause)


@audio_router.post("/recordings/resume", summary="Resume into a new chunk")
def resume_recording(service: ServiceDep) -> dict[str, Any]:
    return _guard(service.resume)


@audio_router.post("/recordings/stop", summary="Stop and finalise (idempotent)")
def stop_recording(service: ServiceDep) -> dict[str, Any]:
    return _guard(service.stop)


@audio_router.get("/recordings/status", summary="Current recording status (cheap)")
def recording_status(service: ServiceDep) -> dict[str, Any]:
    return _guard(service.status)


@audio_router.get("/quality", summary="Level meter snapshot")
def quality(service: ServiceDep) -> dict[str, Any]:
    return _guard(service.quality)


# ------------------------------------------------ integrity and recovery


@audio_router.get(
    "/recordings/{recording_uuid}/verify",
    summary="Verify chunks against the manifest and the database",
)
def verify_recording(service: ServiceDep, recording_uuid: str) -> dict[str, Any]:
    if not _UUID_RE.match(recording_uuid.lower()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="recording_uuid must be a lower-case UUID.",
        )
    return _guard(service.verify, recording_uuid.lower())


@audio_router.get("/recovery/pending", summary="Interrupted recordings awaiting recovery")
def recovery_pending(service: ServiceDep, request: Request) -> dict[str, Any]:
    from mom_igd.audio.recovery import scan_recoverable

    pending = scan_recoverable(request.app.state.paths.recordings_dir)
    # Directory *names* only: they are UUIDs, and the absolute path stays server-side.
    return {
        "pending_count": len(pending),
        "pending": [f"{p.parent.name}/{p.name}" for p in pending],
    }


@audio_router.post("/recovery/run", summary="Recover interrupted recordings (idempotent)")
def run_recovery(service: ServiceDep) -> dict[str, Any]:
    return _guard(service.recover_all)
