"""One minutes run at a time, refused while a capture is live.

The same shape and the same guarantees as :class:`mom_igd.asr.service.AsrService`, and for
the same reasons:

* **One heavy worker at a time** (ADR-0004). ``resources.max_heavy_workers`` above 1 is
  rejected by configuration validation, and the busy check here is what enforces it
  between a transcription and a minutes run that would otherwise overlap.
* **A capture always wins.** Generation is refused while a recording is live; a recording
  is never refused because generation is running. The operator must always be able to
  record the next meeting, and a minute can wait.
* **Cancellation is cooperative and the flag lives here**, so the API can cancel a run the
  CLI started in the same process.

The capture state is read with SQL rather than by importing the audio service: this needs
one fact, not a service. The list of live states is imported from the transcription
service rather than restated here, because a second copy of it drifted immediately -- see
the note on ACTIVE_CAPTURE_STATES below. A test reads migration 0002's partial unique
index to keep the single copy honest.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any, Callable

from mom_igd.asr.service import ACTIVE_CAPTURE_STATES
from mom_igd.logging_setup import get_logger

__all__ = [
    "ACTIVE_CAPTURE_STATES",
    "MinutesBusyError",
    "MinutesService",
    "MinutesServiceError",
    "RecordingInProgressError",
]

_LOG = get_logger("mom.service")

#: Re-exported from :mod:`mom_igd.asr.service`, **not** redefined here.
#:
#: A local copy said ``("RECORDING", "PAUSED", "STARTING")``: it invented a state the
#: schema does not have and omitted ``PREFLIGHT``, ``ARMED``, ``STOPPING`` and
#: ``FINALIZING``, so a capture that was arming or finalising would not have blocked a
#: minutes run -- two heavy processes against a one-heavy-worker policy, and a microphone
#: open next to a 5 GB model. Two lists of "what counts as recording" drift, and this one
#: drifted before it was ever used. The list lives in one place and a test reads
#: migration 0002's partial unique index to keep that place honest.


class MinutesServiceError(RuntimeError):
    """A minutes request was refused. The message says why and what to do."""


class MinutesBusyError(MinutesServiceError):
    """Another heavy run already holds the single worker slot."""


class RecordingInProgressError(MinutesServiceError):
    """A capture is live. Generation waits; the recording does not."""


class MinutesService:
    """Serialises minutes generation for one process."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        *,
        config: Any,
        paths: Any,
    ) -> None:
        self._connect = connect
        self._config = config
        self._paths = paths
        self._lock = threading.Lock()
        self._running = False
        self._cancel = threading.Event()
        self._current: str | None = None
        # The outcome of the most recent run, so a client that is not holding the
        # request open can still learn how it ended. `AsrService` has carried this
        # since Phase 4; the omission here is what forced the minutes panel to await
        # a POST that runs for minutes behind a bridge that gives up after sixty
        # seconds. Reset when a new run starts, so a stale answer is never mistaken
        # for the current one.
        self._last_result: dict | None = None
        self._last_error: str | None = None

    # -- state ---------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    def active_capture(self) -> str | None:
        """The UUID of a recording that is currently capturing, if any."""
        placeholders = ", ".join("?" for _ in ACTIVE_CAPTURE_STATES)
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT recording_uuid FROM recordings WHERE status IN ({placeholders}) "
                "LIMIT 1",
                ACTIVE_CAPTURE_STATES,
            ).fetchone()
        finally:
            conn.close()
        return str(row["recording_uuid"]) if row is not None else None

    def status(self) -> dict[str, Any]:
        """Everything the UI needs to decide whether the button is enabled."""
        from mom_igd.asr.installed import load_index

        index = load_index(self._paths.models_dir)
        ready = index.ready(self._paths.models_dir, role="mom") if index.readable else []
        return {
            "enabled": bool(getattr(self._config.mom, "enabled", True)),
            "running": self._running,
            "current_recording": self._current,
            "last_result": self._last_result,
            "last_error": self._last_error,
            "active_capture": self.active_capture(),
            "model_ready": bool(ready),
            "model_name": ready[0].model_name if ready else None,
            "registry_readable": index.readable,
            "registry_problem": index.problem,
        }

    def list_minuteable(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Completed transcripts, newest first, each with its minute state.

        ``eligible`` and ``reason`` are computed here rather than in JavaScript, so the
        button's enabled state and the explanation the operator reads cannot disagree.
        """
        capture = self.active_capture()
        model_ready = self.status()["model_ready"]
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT r.recording_uuid, r.started_at, r.duration_ms, m.title AS meeting_title,
                       m.uuid AS meeting_uuid, t.id AS transcript_id, t.revision,
                       t.segment_count, t.word_count, t.status AS transcript_status,
                       mi.id AS minute_id, mi.revision AS minute_revision,
                       mi.status AS minute_status, mi.item_count, mi.verified_count,
                       mi.unverified_count, mi.created_at AS minute_created_at
                  FROM transcripts t
                  JOIN recordings r ON r.id = t.recording_id
                  JOIN meetings m ON m.id = r.meeting_id
                  LEFT JOIN minutes mi ON mi.transcript_id = t.id AND mi.is_active = 1
                 WHERE t.is_active = 1
                 ORDER BY r.started_at DESC
                 LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        finally:
            conn.close()

        out: list[dict[str, Any]] = []
        for row in rows:
            entry = dict(row)
            reason: str | None = None
            if str(row["transcript_status"]) != "COMPLETE":
                reason = f"transkrip berstatus {row['transcript_status']}"
            elif not int(row["segment_count"] or 0):
                reason = "transkrip tidak memuat segmen"
            elif capture is not None:
                reason = f"rekaman {capture} sedang berjalan"
            elif self._running:
                reason = "pembuatan notulen lain sedang berjalan"
            elif not model_ready:
                reason = "model notulen belum tersedia"
            entry["eligible"] = reason is None
            entry["reason"] = reason
            out.append(entry)
        return out

    # -- running -------------------------------------------------------------

    def request_cancel(self) -> bool:
        """Ask the current run to stop at its next boundary. Never kills a process."""
        if not self._running:
            return False
        self._cancel.set()
        _LOG.info("mom.service.cancel_requested")
        return True

    def generate(
        self,
        recording_uuid: str,
        *,
        progress: Callable[[str], None] | None = None,
        job_id: int | None = None,
        export_formats: tuple[str, ...] | None = None,
        include_unverified: bool | None = None,
    ) -> Any:
        """Generate the minute for a recording's active transcript."""
        from mom_igd.mom.pipeline import MinutesPipeline

        if not getattr(self._config.mom, "enabled", True):
            raise MinutesServiceError(
                "pembuatan notulen dimatikan di konfigurasi ([mom].enabled = false). "
                "Transkripsi tetap berjalan."
            )

        capture = self.active_capture()
        if capture is not None:
            raise RecordingInProgressError(
                f"rekaman {capture} sedang berjalan. Pembuatan notulen ditunda sampai "
                "rekaman dihentikan -- perekaman tidak pernah dibatalkan demi notulen."
            )

        with self._lock:
            if self._running:
                raise MinutesBusyError(
                    "satu proses berat sudah berjalan. Hanya satu model boleh dimuat "
                    "pada satu waktu (ADR-0004); tunggu sampai selesai."
                )
            self._running = True
            self._current = recording_uuid
            self._last_result = None
            self._last_error = None
            self._cancel.clear()

        if export_formats is None:
            export_formats = tuple(self._config.mom.default_export_formats)
        if include_unverified is None:
            include_unverified = bool(self._config.mom.include_unverified_in_exports)

        pipeline = MinutesPipeline(
            config=self._config,
            paths=self._paths,
            connect=self._connect,
            progress=progress,
            should_cancel=self._cancel.is_set,
        )
        try:
            outcome = pipeline.run(
                recording_uuid,
                job_id=job_id,
                export_formats=export_formats,
                include_unverified=include_unverified,
            )
        except Exception as exc:
            # Recorded, then re-raised unchanged: a caller that *is* waiting must still
            # see the exception, and one that gave up waiting must still be able to find
            # out. Both readers get the same message.
            with self._lock:
                self._last_error = str(exc)
            raise
        else:
            with self._lock:
                self._last_result = (
                    outcome.to_dict() if hasattr(outcome, "to_dict") else None
                )
            return outcome
        finally:
            with self._lock:
                self._running = False
                self._current = None
                self._cancel.clear()

    # -- reading -------------------------------------------------------------

    def get_minute(
        self, recording_uuid: str, *, revision: int | None = None
    ) -> dict[str, Any]:
        """Read a stored minute. Loads no model and never generates one."""
        import json as _json

        from mom_igd.mom import store

        conn = self._connect()
        try:
            transcript = conn.execute(
                """
                SELECT t.id FROM transcripts t
                  JOIN recordings r ON r.id = t.recording_id
                 WHERE r.recording_uuid = ? AND t.is_active = 1
                """,
                (recording_uuid,),
            ).fetchone()
            if transcript is None:
                raise MinutesServiceError(
                    f"rekaman {recording_uuid} belum punya transkrip aktif."
                )
            transcript_id = int(transcript["id"])
            if revision is None:
                row = store.get_active_minute(conn, transcript_id=transcript_id)
            else:
                row = conn.execute(
                    "SELECT * FROM minutes WHERE transcript_id = ? AND revision = ?",
                    (transcript_id, int(revision)),
                ).fetchone()
            if row is None:
                raise MinutesServiceError(
                    f"rekaman {recording_uuid} belum punya notulen"
                    + (f" revisi {revision}" if revision else "")
                    + ". Jalankan `mom generate` lebih dulu."
                )
            minute = dict(row)
            minute["summary"] = _json.loads(row["summary_json"] or "[]")
            minute["warnings"] = _json.loads(row["warnings_json"] or "[]")
            minute["summary_unsupported_numbers"] = _json.loads(
                row["summary_unsupported_numbers"] or "[]"
            )
            minute.pop("summary_json", None)
            minute.pop("warnings_json", None)
            minute["items"] = store.load_items(conn, minute_id=int(row["id"]))
            minute["exports"] = [
                dict(entry) for entry in store.list_exports(conn, minute_id=int(row["id"]))
            ]
            return minute
        finally:
            conn.close()

    def list_revisions(self, recording_uuid: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT mi.id, mi.revision, mi.status, mi.is_active, mi.title,
                           mi.item_count, mi.verified_count, mi.unverified_count,
                           mi.covered_ms, mi.transcript_ms, mi.created_at, mi.last_error
                      FROM minutes mi
                      JOIN transcripts t ON t.id = mi.transcript_id
                      JOIN recordings r ON r.id = t.recording_id
                     WHERE r.recording_uuid = ?
                     ORDER BY mi.revision DESC
                    """,
                    (recording_uuid,),
                )
            ]
        finally:
            conn.close()

    def export(
        self,
        recording_uuid: str,
        *,
        export_format: str,
        revision: int | None = None,
        include_unverified: bool | None = None,
    ) -> dict[str, Any]:
        """Write an existing minute to a file. Loads no model."""
        from mom_igd.mom.pipeline import export_minute, resolve_branding

        if include_unverified is None:
            include_unverified = bool(self._config.mom.include_unverified_in_exports)
        minute = self.get_minute(recording_uuid, revision=revision)
        conn = self._connect()
        try:
            record = export_minute(
                conn,
                paths=self._paths,
                minute_id=int(minute["id"]),
                export_format=export_format,
                include_unverified=include_unverified,
                branding=resolve_branding(self._config, self._paths),
            )
            conn.commit()
            return record
        finally:
            conn.close()
