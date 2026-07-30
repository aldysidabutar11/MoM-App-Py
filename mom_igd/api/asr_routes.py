"""Phase 4 transcription endpoints. All token-protected, all loopback-only.

Rules this module holds to, and the reason for each:

* **No filesystem path in any response, and none accepted.** A recording is addressed by
  its UUID and nothing else. Neither the master audio, the working copy nor the model
  directory is nameable from a request.
* **Nothing here downloads anything.** ``GET /asr/models`` reports whether a model is
  ready; provisioning is a deliberate command-line action, never an HTTP call. A missing
  model is `409 MODEL_UNAVAILABLE`, never a fetch.
* **A second concurrent run is a 409, not a queue.** Exactly one heavy model may be
  resident (ADR-0004), and the measured working sets say two runs would breach the memory
  budget. Refusing visibly beats queueing invisibly.
* **Transcribing is a POST, and a long one.** It runs the whole pipeline synchronously in
  uvicorn's threadpool. The GUI polls ``GET /asr/status`` for progress rather than holding
  a request open, exactly as it does for recording.
* **No transcript text in a status response.** ``/asr/status`` carries counts and timings
  so the shell can poll it frequently without repeatedly shipping a meeting's words
  around.
"""

from __future__ import annotations

import re
import threading
from typing import Annotated, Any, Final

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from mom_igd.api.deps import require_session_token
from mom_igd.asr.service import (
    AsrBusyError,
    AsrService,
    AsrServiceError,
    RecordingInProgressError,
)

__all__ = ["asr_router", "get_asr_service"]

asr_router = APIRouter(
    prefix="/asr", tags=["asr"], dependencies=[Depends(require_session_token)]
)

_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

_SERVICE_LOCK: Final[threading.Lock] = threading.Lock()


def get_asr_service(request: Request) -> AsrService:
    """Return the process-wide transcription service, creating it on first use.

    One instance per process, because the single-run guard lives in it: a second instance
    would hold its own lock and both would think they were the only one running.
    """
    existing = getattr(request.app.state, "asr_service", None)
    if existing is not None:
        return existing
    with _SERVICE_LOCK:
        existing = getattr(request.app.state, "asr_service", None)
        if existing is not None:
            return existing
        config = request.app.state.config
        paths = request.app.state.paths

        def _connect():
            from mom_igd.db.connection import connect

            return connect(
                paths.database_path(config.database.filename),
                busy_timeout_ms=config.database.busy_timeout_ms,
            )

        service = AsrService(_connect, config=config, paths=paths)
        request.app.state.asr_service = service
        return service


ServiceDep = Annotated[AsrService, Depends(get_asr_service)]


def _require_uuid(value: str) -> str:
    if not _UUID_RE.match(value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"recording_uuid must be a lower-case UUID, got {value!r}. Obtain one "
                "from the recordings list; a path or an index is not an identity."
            ),
        )
    return value


def _guard(callable_, *args, **kwargs):
    try:
        return callable_(*args, **kwargs)
    except (AsrBusyError, RecordingInProgressError) as exc:
        # 409, not 503: the server is fine and the precondition is not met. A recording
        # in progress is a state the operator resolves, not an error to retry blindly.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except AsrServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None


# ---------------------------------------------------------------- status


@asr_router.get("/status", summary="Transcription state (loads no model)")
def asr_status(service: ServiceDep) -> dict[str, Any]:
    return service.status()


@asr_router.get("/recordings", summary="Closed recordings that can be transcribed")
def transcribable_recordings(
    service: ServiceDep, limit: int = Query(default=100, ge=1, le=500)
) -> dict[str, Any]:
    """The list the panel offers instead of asking the operator to type a UUID.

    Each entry carries `eligible` and `ineligible_reason`, computed server-side so the
    button's enabled state and the explanation shown next to it cannot disagree.
    """
    return {
        "recordings": service.list_transcribable(limit=limit),
        "active_capture": service.active_capture(),
    }


@asr_router.get("/preflight", summary="Everything that must hold before a run")
def asr_preflight(
    service: ServiceDep, recording_uuid: str | None = Query(default=None)
) -> dict[str, Any]:
    """Loads no model and opens no microphone.

    Separate from `transcribe` on purpose: an operator told *before* pressing the button
    that no model is provisioned has a problem they can fix; one told five seconds into a
    run has a failure to interpret.
    """
    if recording_uuid is not None:
        _require_uuid(recording_uuid)
    return service.preflight(recording_uuid)


@asr_router.get("/models", summary="Which models are ready (never downloads)")
def asr_models(service: ServiceDep) -> dict[str, Any]:
    payload = service.status()["models"]
    payload["provisioning_is_a_cli_action"] = True
    payload["provision_command"] = "python -m mom_igd asr provision all"
    return payload


# ------------------------------------------------------------- transcribe


@asr_router.post("/transcribe", summary="Run the offline pipeline for one recording")
def transcribe(
    service: ServiceDep,
    recording_uuid: Annotated[str, Body(embed=True, min_length=36, max_length=36)],
) -> dict[str, Any]:
    _require_uuid(recording_uuid)
    models = service.status()["models"]
    if not models["pass1_ready"]:
        # 409 rather than 503: the server is fine, the precondition is not met, and the
        # fix is an operator action. The reason code is stable so the UI can key on it.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "MODEL_UNAVAILABLE: no pass-1 model is provisioned and verified. "
                "Provision one with `python -m mom_igd asr provision asr-pass1`. "
                "Transcription never downloads a model by itself."
            ),
        )
    result = _guard(service.transcribe, recording_uuid)
    payload = result.to_dict()
    if not result.ok:
        # A pipeline failure is reported with the run's own detail, at 409 when it is a
        # precondition and 500 only for something genuinely unexpected.
        code = (
            status.HTTP_409_CONFLICT
            if result.reason_code
            else status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        raise HTTPException(status_code=code, detail=payload)
    return payload


@asr_router.post("/cancel", summary="Ask a running transcription to stop")
def cancel(service: ServiceDep) -> dict[str, Any]:
    stopped = service.request_cancel()
    if not stopped:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no transcription is running, so there is nothing to cancel.",
        )
    return {"cancel_requested": True}


# ----------------------------------------------------------------- reads


@asr_router.get("/transcript/{recording_uuid}", summary="One transcript revision")
def get_transcript(
    service: ServiceDep,
    recording_uuid: str,
    revision: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    _require_uuid(recording_uuid)
    return _guard(service.get_transcript, recording_uuid, revision=revision)


@asr_router.get("/revisions/{recording_uuid}", summary="Every revision, newest first")
def list_revisions(service: ServiceDep, recording_uuid: str) -> dict[str, Any]:
    _require_uuid(recording_uuid)
    return {"revisions": _guard(service.list_revisions, recording_uuid)}


@asr_router.get("/flagged/{recording_uuid}", summary="Regions a selection rule fired on")
def flagged(service: ServiceDep, recording_uuid: str) -> dict[str, Any]:
    _require_uuid(recording_uuid)
    return {"flagged": _guard(service.flagged_regions, recording_uuid)}
