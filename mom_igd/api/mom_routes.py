"""Minutes endpoints. All token-protected, all loopback-only.

The same rules as :mod:`mom_igd.api.asr_routes`, for the same reasons:

* **No filesystem path is accepted from a request.** A minute is addressed by its
  recording's UUID and a revision number. The export directory is not nameable, and a
  format is chosen from a closed set -- otherwise an export path becomes a way to write a
  file anywhere the process can reach.
* **Nothing here downloads anything.** ``GET /mom/status`` reports whether the model is
  ready; provisioning is a deliberate command-line action. A missing model is
  ``409 MODEL_UNAVAILABLE``, never a fetch.
* **A second concurrent run is a 409, not a queue.** One heavy model resident at a time
  (ADR-0004). Refusing visibly beats queueing invisibly.
* **Generating is a POST, and a long one.** It runs synchronously in uvicorn's threadpool
  and the GUI polls ``GET /mom/status``, exactly as it does for recording and
  transcription.

One addition the ASR routes do not need: **the download endpoint streams the file's bytes
rather than returning its path**, because the shell has no filesystem access and a path in
a response would be the first step towards one.
"""

from __future__ import annotations

import re
import threading
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response

from mom_igd.api.deps import require_session_token
from mom_igd.mom.pipeline import MinutesExportError
from mom_igd.mom.service import (
    MinutesBusyError,
    MinutesService,
    MinutesServiceError,
    RecordingInProgressError,
)

__all__ = ["get_minutes_service", "mom_router"]

mom_router = APIRouter(
    prefix="/mom", tags=["mom"], dependencies=[Depends(require_session_token)]
)

_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

_SERVICE_LOCK: Final[threading.Lock] = threading.Lock()

#: What a browser should do with each rendering. DOCX downloads; the rest are readable in
#: place, which is what makes "check it before you send it" a realistic instruction.
_MEDIA_TYPES: Final[dict[str, tuple[str, bool]]] = {
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        True,
    ),
    "markdown": ("text/markdown; charset=utf-8", False),
    "html": ("text/html; charset=utf-8", False),
    "txt": ("text/plain; charset=utf-8", False),
}


def get_minutes_service(request: Request) -> MinutesService:
    """Return the process-wide minutes service, creating it on first use.

    One instance per process, because the single-run guard lives in it: a second instance
    would hold its own lock and both would think they were the only one running.
    """
    existing = getattr(request.app.state, "minutes_service", None)
    if existing is not None:
        return existing
    with _SERVICE_LOCK:
        existing = getattr(request.app.state, "minutes_service", None)
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

        service = MinutesService(_connect, config=config, paths=paths)
        request.app.state.minutes_service = service
        return service


ServiceDep = Annotated[MinutesService, Depends(get_minutes_service)]


def _require_uuid(value: str) -> str:
    if not _UUID_RE.match(value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"recording_uuid must be a lower-case UUID, got {value!r}. Obtain one "
                "from the transcripts list; a path or an index is not an identity."
            ),
        )
    return value


def _guard(callable_, *args, **kwargs):
    try:
        return callable_(*args, **kwargs)
    except (MinutesBusyError, RecordingInProgressError, MinutesExportError) as exc:
        # 409, not 503 and not 500: the server is fine and the precondition is not met.
        # An export that could not be written is the operator's file lock, not a fault
        # here, and a 500 would send them looking for a crash.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except MinutesServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None


def _require_model(service: MinutesService) -> None:
    if not service.status()["model_ready"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "MODEL_UNAVAILABLE: no minutes model is provisioned and probe-passed. "
                "Provision one with `python -m mom_igd asr provision mom-llm`. Minutes "
                "generation never downloads a model by itself, and never falls back to "
                "a different one."
            ),
        )


# ---------------------------------------------------------------- status


@mom_router.get("/status", summary="Minutes state (loads no model)")
def mom_status(service: ServiceDep) -> dict[str, Any]:
    return service.status()


@mom_router.get("/transcripts", summary="Transcripts that can be minuted")
def transcripts(
    service: ServiceDep, limit: int = Query(default=50, ge=1, le=500)
) -> dict[str, Any]:
    return {"transcripts": service.list_minuteable(limit=limit)}


# ------------------------------------------------------------- generating


@mom_router.post("/generate", summary="Generate the minute for one recording")
def generate(
    service: ServiceDep,
    recording_uuid: Annotated[str, Body(embed=True, min_length=36, max_length=36)],
    export_formats: Annotated[
        list[Literal["docx", "markdown", "html", "txt"]] | None, Body(embed=True)
    ] = None,
    include_unverified: Annotated[bool | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    _require_uuid(recording_uuid)
    _require_model(service)
    result = _guard(
        service.generate,
        recording_uuid,
        export_formats=tuple(export_formats) if export_formats is not None else None,
        include_unverified=include_unverified,
    )
    return result.to_dict()


@mom_router.post("/cancel", summary="Ask a running generation to stop")
def cancel(service: ServiceDep) -> dict[str, Any]:
    if not service.request_cancel():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no minutes run is in progress, so there is nothing to cancel.",
        )
    return {"cancel_requested": True}


# ----------------------------------------------------------------- reads


@mom_router.get("/minute/{recording_uuid}", summary="One stored minute revision")
def get_minute(
    service: ServiceDep,
    recording_uuid: str,
    revision: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    _require_uuid(recording_uuid)
    return _guard(service.get_minute, recording_uuid, revision=revision)


@mom_router.get("/revisions/{recording_uuid}", summary="Every revision, newest first")
def list_revisions(service: ServiceDep, recording_uuid: str) -> dict[str, Any]:
    _require_uuid(recording_uuid)
    return {"revisions": _guard(service.list_revisions, recording_uuid)}


# --------------------------------------------------------------- exports


@mom_router.post("/export", summary="Write a minute to a document on disk")
def export(
    service: ServiceDep,
    recording_uuid: Annotated[str, Body(embed=True, min_length=36, max_length=36)],
    export_format: Annotated[
        Literal["docx", "markdown", "html", "txt"], Body(embed=True)
    ] = "docx",
    revision: Annotated[int | None, Body(embed=True)] = None,
    include_unverified: Annotated[bool | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    _require_uuid(recording_uuid)
    record = _guard(
        service.export,
        recording_uuid,
        export_format=export_format,
        revision=revision,
        include_unverified=include_unverified,
    )
    # The absolute path is dropped on the way out. The operator sees where exports live in
    # `doctor` and in the CLI; an API response that carries a path is the beginning of an
    # API that accepts one.
    return {key: value for key, value in record.items() if key != "path"}


@mom_router.get("/download/{recording_uuid}", summary="Stream a rendered minute")
def download(
    service: ServiceDep,
    recording_uuid: str,
    export_format: Literal["docx", "markdown", "html", "txt"] = Query(
        default="docx", alias="format"
    ),
    revision: int | None = Query(default=None, ge=1),
    include_unverified: bool | None = Query(default=None),
) -> Response:
    """Render and return the bytes. Also writes the file, so the export is recorded.

    Rendering rather than reading back a stored file, deliberately: a file on disk can
    have been edited or replaced since it was written, and serving it would attach this
    application's name to somebody else's document. What comes back is what the minute
    says now, and the export row records that this rendering happened.
    """
    _require_uuid(recording_uuid)
    record = _guard(
        service.export,
        recording_uuid,
        export_format=export_format,
        revision=revision,
        include_unverified=include_unverified,
    )
    media_type, attachment = _MEDIA_TYPES[export_format]
    from pathlib import Path

    blob = Path(record["path"]).read_bytes()
    disposition = "attachment" if attachment else "inline"
    return Response(
        content=blob,
        media_type=media_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{record["relative_path"]}"',
            "X-MoM-Included-Unverified": "1" if record["included_unverified"] else "0",
            "X-MoM-Sha256": record["sha256"],
        },
    )
