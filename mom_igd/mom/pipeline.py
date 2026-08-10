"""Run minutes generation for one transcript: load, generate, verify, persist, export.

Shaped like :class:`mom_igd.asr.pipeline.TranscriptionPipeline` on purpose -- same worker
discipline, same peak-RSS accounting, same stage reporting, same rule that a state change
and its audit event share a transaction. Two heavy pipelines that behave differently under
cancellation is a maintenance trap, and an operator who has learned to read one progress
log should be able to read the other.

**The model is never loaded in this process.** Every prompt goes through
:func:`mom_igd.asr.worker.run_in_worker`, which spawns, loads, answers and exits. Two
worker calls per run: one for all extraction windows, one for the summary that can only be
built after the parent has verified them.

**A capture always wins.** Generation is refused while a recording is live, and a
recording is never refused because generation is running -- the same asymmetry Phase 4
established, for the same reason: the operator must always be able to record the next
meeting.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Sequence

from mom_igd.logging_setup import get_logger
from mom_igd.mom import store
from mom_igd.mom.generator import PromptSpec, generate_minutes

__all__ = [
    "EXPORT_FORMATS",
    "MinutesExportError",
    "resolve_branding",
    "MinutesPipeline",
    "MinutesPipelineError",
    "MinutesResult",
    "REASON_CANCELLED",
    "REASON_MODEL_UNAVAILABLE",
    "REASON_NO_TRANSCRIPT",
    "export_minute",
]

_LOG = get_logger("mom.pipeline")


def _utc_date() -> str:
    """Today's date in UTC, as ``YYYY-MM-DD``. The only clock read in this module."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

#: Reason codes. Stable identifiers -- an operator matches them against the manual and a
#: test asserts them, so they never become prose.
REASON_NO_TRANSCRIPT: Final[str] = "NO_TRANSCRIPT"
REASON_MODEL_UNAVAILABLE: Final[str] = "MODEL_UNAVAILABLE"
REASON_CANCELLED: Final[str] = "CANCELLED"
REASON_RECORDING_IN_PROGRESS: Final[str] = "RECORDING_IN_PROGRESS"
REASON_EXPORT_FAILED: Final[str] = "EXPORT_FAILED"

#: Formats an export may be asked for. ``docx`` first: it is what the team opens.
EXPORT_FORMATS: Final[tuple[str, ...]] = ("docx", "markdown", "html", "txt")

_EXTENSIONS: Final[Mapping[str, str]] = {
    "docx": ".docx",
    "markdown": ".md",
    "html": ".html",
    "txt": ".txt",
}


class MinutesPipelineError(RuntimeError):
    """A stage failed. The message names a reason code and what to do about it."""


class MinutesExportError(MinutesPipelineError):
    """A document could not be written. **The minute itself is stored and intact.**

    Its own type because the caller has to treat it differently from every other failure
    here: the expensive part already succeeded, and telling the operator the run failed
    would send them to repeat twenty minutes of work that does not need repeating.
    """


@dataclass(slots=True)
class MinutesResult:
    minute_id: int | None
    transcript_id: int
    revision: int
    status: str
    title: str = ""
    item_count: int = 0
    verified_count: int = 0
    unverified_count: int = 0
    covered_ms: int = 0
    transcript_ms: int = 0
    peak_rss_bytes: int = 0
    total_seconds: float = 0.0
    stages: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    exports: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "minute_id": self.minute_id,
            "transcript_id": self.transcript_id,
            "revision": self.revision,
            "status": self.status,
            "title": self.title,
            "item_count": self.item_count,
            "verified_count": self.verified_count,
            "unverified_count": self.unverified_count,
            "covered_ms": self.covered_ms,
            "transcript_ms": self.transcript_ms,
            "coverage_ratio": (
                round(self.covered_ms / self.transcript_ms, 4)
                if self.transcript_ms
                else None
            ),
            "peak_rss_bytes": self.peak_rss_bytes,
            "peak_rss_mib": round(self.peak_rss_bytes / (1024 * 1024), 1),
            "total_seconds": round(self.total_seconds, 2),
            "stages": self.stages,
            "warnings": self.warnings,
            "exports": self.exports,
        }


class MinutesPipeline:
    """Generates the minute for one transcript. Construct per run, not per application."""

    def __init__(
        self,
        *,
        config: Any,
        paths: Any,
        connect: Callable[[], sqlite3.Connection],
        progress: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self._config = config
        self._paths = paths
        self._connect = connect
        self._progress = progress
        self._should_cancel = should_cancel
        self._peak_rss = 0
        self._model: dict[str, Any] = {}

    # -- helpers ------------------------------------------------------------

    def _say(self, message: str) -> None:
        if self._progress:
            self._progress(message)

    def _cancelled(self) -> bool:
        return bool(self._should_cancel and self._should_cancel())

    def _stage(
        self, stages: list[dict[str, Any]], name: str, ok: bool, detail: str, ms: int = 0
    ) -> None:
        stages.append({"name": name, "ok": bool(ok), "detail": detail, "ms": ms})
        self._say(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")

    @property
    def _mom_settings(self) -> Any:
        """The ``[mom]`` configuration block, or the module defaults.

        Falling back rather than raising because the pipeline must still run against a
        configuration written before this phase existed; the defaults are the measured
        ones, and a deployment that wants different values sets them.
        """
        return getattr(self._config, "mom", None)

    def _setting(self, name: str, fallback: Any) -> Any:
        settings = self._mom_settings
        value = getattr(settings, name, None) if settings is not None else None
        return fallback if value is None else value

    def _document_setting(self, name: str, fallback: Any) -> Any:
        settings = getattr(self._mom_settings, "document", None)
        value = getattr(settings, name, None) if settings is not None else None
        return fallback if value is None else value

    def _run_prompts(self, specs: Sequence[PromptSpec]) -> list[Mapping[str, Any]]:
        """Send a batch of prompts to one short-lived worker and return its answers.

        Batched deliberately: the weights take seconds to load and 2.3 GB of memory, so a
        worker per window would spend more time loading than generating. This is the same
        mistake, in the same shape, as re-reading the working copy once per decode window
        (ADR-0016 §3), and it is avoided the same way.
        """
        from mom_igd.asr.worker import WorkerTimeout, run_in_worker

        if not specs:
            return []
        payload = {
            "models_dir": str(self._paths.models_dir),
            "context_tokens": int(self._setting("context_tokens", 8192)),
            "threads": int(self._setting("threads", 12)),
            "batch_tokens": int(self._setting("batch_tokens", 256)),
            "deep_verify": False,
            "prompts": [spec.to_payload() for spec in specs],
        }
        try:
            outcome = run_in_worker(
                "mom_generate",
                payload,
                timeout_seconds=float(self._setting("worker_timeout_seconds", 7200)),
                should_cancel=self._should_cancel,
                progress=self._progress,
            )
        except WorkerTimeout as exc:
            raise MinutesPipelineError(f"{REASON_CANCELLED}: {exc}") from None
        self._peak_rss = max(self._peak_rss, outcome.peak_rss_bytes)
        if not outcome.ok:
            error = outcome.error or "the minutes worker failed"
            if "MODEL_UNAVAILABLE" in error:
                raise MinutesPipelineError(error)
            raise MinutesPipelineError(error)
        result = outcome.payload or {}
        if not self._model:
            self._model = dict(result.get("model") or {})
        if result.get("cancelled"):
            raise MinutesPipelineError(
                f"{REASON_CANCELLED}: pembuatan notulen dibatalkan sebelum selesai."
            )
        return list(result.get("outputs") or [])

    # -- inputs -------------------------------------------------------------

    def _load_transcript(
        self, conn: sqlite3.Connection, recording_uuid: str
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT t.*, r.recording_uuid, r.meeting_id, r.started_at, r.duration_ms
              FROM transcripts t
              JOIN recordings r ON r.id = t.recording_id
             WHERE r.recording_uuid = ? AND t.is_active = 1
            """,
            (recording_uuid,),
        ).fetchone()
        if row is None:
            raise MinutesPipelineError(
                f"{REASON_NO_TRANSCRIPT}: rekaman {recording_uuid} belum punya "
                "transkrip aktif. Jalankan `asr transcribe` lebih dulu; notulen dibuat "
                "dari transkrip, bukan langsung dari audio."
            )
        if str(row["status"]) != "COMPLETE":
            raise MinutesPipelineError(
                f"{REASON_NO_TRANSCRIPT}: transkrip rekaman {recording_uuid} berstatus "
                f"{row['status']}, bukan COMPLETE. Selesaikan transkripsi lebih dulu."
            )
        return row

    def _roster(self, conn: sqlite3.Connection, meeting_id: int) -> dict[str, str]:
        """Active roster names, keyed by their normalised form.

        Used **only** to correct the spelling of a name the transcript already contains --
        see :func:`mom_igd.mom.verify.verify_items`. It is never shown to the model, and it
        can never introduce a name.
        """
        from mom_igd.mom.verify import normalise

        rows = conn.execute(
            """
            SELECT p.display_name
              FROM meeting_participants mp
              JOIN participants p ON p.id = mp.participant_id
             WHERE mp.meeting_id = ? AND mp.is_active = 1 AND p.is_active = 1
            """,
            (meeting_id,),
        )
        return {
            normalise(row["display_name"]): str(row["display_name"])
            for row in rows
            if row["display_name"]
        }

    # -- the run ------------------------------------------------------------

    def run(
        self,
        recording_uuid: str,
        *,
        job_id: int | None = None,
        export_formats: Sequence[str] = (),
        include_unverified: bool = True,
    ) -> MinutesResult:
        """Generate the minute for a recording's active transcript."""
        started = time.perf_counter()
        stages: list[dict[str, Any]] = []
        minute_id: int | None = None

        conn = self._connect()
        try:
            transcript = self._load_transcript(conn, recording_uuid)
            transcript_id = int(transcript["id"])
            meeting_id = int(transcript["meeting_id"])
            self._stage(
                stages,
                "transcript",
                True,
                f"revisi {transcript['revision']}, {transcript['segment_count']} segmen, "
                f"{transcript['word_count']} kata",
            )

            from mom_igd.asr.store import load_segments

            segments = [
                {
                    "seq": int(row["seq"]),
                    "start_ms": int(row["start_ms"]),
                    "end_ms": int(row["end_ms"]),
                    "text": str(row["text"] or ""),
                }
                for row in load_segments(
                    conn, transcript_id=transcript_id, active_only=True
                )
            ]
            if not segments:
                raise MinutesPipelineError(
                    f"{REASON_NO_TRANSCRIPT}: transkrip aktif rekaman {recording_uuid} "
                    "tidak memuat segmen apa pun. Tidak ada yang bisa dinotulenkan."
                )

            meeting = conn.execute(
                "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
            ).fetchone()
            roster = self._roster(conn, meeting_id)
            minute_id = store.create_minute(
                conn,
                transcript_id=transcript_id,
                meeting_id=meeting_id,
                job_id=job_id,
                language=str(transcript["language"] or "id"),
            )
            revision = int(
                conn.execute(
                    "SELECT revision FROM minutes WHERE id = ?", (minute_id,)
                ).fetchone()["revision"]
            )

            if self._cancelled():
                raise MinutesPipelineError(f"{REASON_CANCELLED}: dibatalkan sebelum mulai.")

            self._say(
                f"membuat notulen revisi {revision} dari {len(segments)} segmen "
                "transkrip (model dijalankan di proses terpisah)"
            )
            generation_started = time.perf_counter()
            result = generate_minutes(
                segments,
                run_prompts=self._run_prompts,
                meeting_title=str(meeting["title"]) if meeting else None,
                roster=roster,
                context_tokens=int(self._setting("context_tokens", 8192)),
                model=self._model,
            )
            result.model = dict(self._model)
            self._stage(
                stages,
                "generate",
                True,
                f"{result.stats.chunk_count} bagian, {result.stats.raw_item_count} poin "
                f"mentah, {result.stats.merged_item_count} setelah digabung",
                int((time.perf_counter() - generation_started) * 1000),
            )
            self._stage(
                stages,
                "verify",
                result.stats.unverified_count == 0,
                f"{result.stats.verified_count} terverifikasi, "
                f"{result.stats.rebound_count} dicari ulang, "
                f"{result.stats.unverified_count} gagal; "
                f"{result.stats.owners_dropped} PIC dihapus",
            )

            draft = result.draft.to_dict()
            item_count, verified = store.save_result(
                conn,
                minute_id=minute_id,
                draft=draft,
                stats=result.stats.to_dict(),
                model=self._model,
            )
            store.update_minute(
                conn, minute_id, peak_rss_bytes=self._peak_rss or None
            )
            # The filing reference is minted here, once, before the minute becomes
            # current -- and inherited unchanged by every later revision.
            reference = store.assign_document_number(
                conn,
                minute_id=minute_id,
                transcript_id=transcript_id,
                number_format=str(self._document_setting("document_number_format", "")),
                stamp=_utc_date(),
            )
            store.activate_minute(conn, minute_id=minute_id)
            self._stage(
                stages,
                "persist",
                True,
                f"{item_count} poin disimpan, revisi {revision} aktif"
                + (f", nomor {reference}" if reference else ""),
            )

            outcome = MinutesResult(
                minute_id=minute_id,
                transcript_id=transcript_id,
                revision=revision,
                status="DRAFT",
                title=result.draft.title,
                item_count=item_count,
                verified_count=verified,
                unverified_count=result.stats.unverified_count,
                covered_ms=result.stats.covered_ms,
                transcript_ms=result.stats.transcript_ms,
                peak_rss_bytes=self._peak_rss,
                stages=stages,
                warnings=list(result.draft.warnings),
            )

            # The minute is stored and active from here on. An export is a separate,
            # cheap, repeatable step, so a failure is reported and the run still
            # succeeds -- the alternative tells the operator to redo twenty minutes of
            # work because a file was open in Word.
            for export_format in export_formats:
                try:
                    record = export_minute(
                        conn,
                        paths=self._paths,
                        minute_id=minute_id,
                        export_format=export_format,
                        include_unverified=include_unverified,
                        branding=resolve_branding(self._config, self._paths),
                    )
                except MinutesExportError as exc:
                    outcome.warnings.append(str(exc))
                    self._stage(stages, f"export:{export_format}", False, str(exc)[:200])
                    continue
                outcome.exports.append(record)
                self._stage(
                    stages,
                    f"export:{export_format}",
                    True,
                    f"{record['relative_path']} ({record['size_bytes']} bytes)",
                )

            outcome.total_seconds = time.perf_counter() - started
            store.update_minute(
                conn, minute_id, total_ms=int(outcome.total_seconds * 1000)
            )
            _LOG.info(
                "mom.pipeline.completed",
                extra={
                    "minute_id": minute_id,
                    "revision": revision,
                    "items": item_count,
                    "verified": verified,
                    "seconds": round(outcome.total_seconds, 2),
                    "peak_rss_mib": round(self._peak_rss / (1024 * 1024), 1),
                },
            )
            return outcome

        except MinutesPipelineError as exc:
            cancelled = str(exc).startswith(REASON_CANCELLED)
            if minute_id is not None:
                store.fail_minute(
                    conn, minute_id=minute_id, error=str(exc), cancelled=cancelled
                )
            self._stage(stages, "run", False, str(exc)[:200])
            raise
        finally:
            conn.close()


# ===========================================================================
# Export
# ===========================================================================


#: Logo formats the DOCX writer can size and Word can display. A file that is not one
#: of these is ignored rather than embedded blindly: Word shows a broken-image box for an
#: unrecognised part, which looks like a corrupt document.
_LOGO_MEDIA_TYPES: Final[Mapping[bytes, str]] = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8": "image/jpeg",
}

#: A letterhead logo larger than this is refused. Two megabytes is a generous scan of a
#: letterhead; beyond that it is a photograph somebody dropped in by mistake, and it would
#: be embedded into every exported document.
_MAX_LOGO_BYTES: Final[int] = 2 * 1024 * 1024


def resolve_branding(config: Any, paths: Any) -> dict[str, Any]:
    """Turn ``[mom.document]`` into what :func:`build_document` needs, logo bytes included.

    **The only place a branding file is read.** Renderers receive bytes, so none of them
    touches the filesystem and none can be pointed at a path by a document.

    Every failure here is non-fatal and deliberate: a missing logo, an unreadable one, an
    unrecognised format, one that is too large. An export must not fail because somebody
    moved a PNG, and a minute without a letterhead is still a correct minute.
    """
    settings = getattr(getattr(config, "mom", None), "document", None)
    if settings is None:
        return {}

    brand: dict[str, Any] = {
        "organisation": settings.organisation,
        "subtitle": settings.organisation_subtitle,
        "show_signature_block": settings.show_signature_block,
        "signature_roles": tuple(settings.signature_roles),
        "footer_note": settings.footer_note,
        "place": settings.place,
    }

    filename = (settings.logo_filename or "").strip()
    if not filename:
        return brand
    # The log key below is `logo`, not `filename`: `filename` is a reserved LogRecord
    # attribute and logging raises KeyError on the collision -- which would have made
    # every "this is non-fatal, just warn" path here fatal, and an absent logo would
    # have taken the export down with it. Found by the test that exists to prevent it.
    try:
        target = paths.branding_asset(filename)
        blob = target.read_bytes()
    except Exception as exc:  # noqa: BLE001 - any read failure is the same non-event
        _LOG.warning(
            "mom.branding.logo_unreadable",
            extra={"logo": filename, "reason": type(exc).__name__},
        )
        return brand
    if len(blob) > _MAX_LOGO_BYTES:
        _LOG.warning(
            "mom.branding.logo_too_large",
            extra={"logo": filename, "size_bytes": len(blob)},
        )
        return brand
    for signature, media_type in _LOGO_MEDIA_TYPES.items():
        if blob.startswith(signature):
            brand["logo"] = blob
            brand["logo_media_type"] = media_type
            return brand
    _LOG.warning("mom.branding.logo_not_an_image", extra={"logo": filename})
    return brand


def _render(document: Any, export_format: str) -> bytes:
    from mom_igd.mom.document import render_html, render_markdown, render_text
    from mom_igd.mom.docx import render_docx

    if export_format == "docx":
        return render_docx(document)
    if export_format == "markdown":
        return render_markdown(document).encode("utf-8")
    if export_format == "html":
        return render_html(document).encode("utf-8")
    if export_format == "txt":
        return render_text(document).encode("utf-8")
    raise MinutesPipelineError(
        f"format ekspor {export_format!r} tidak dikenal. Pilih salah satu dari: "
        f"{', '.join(EXPORT_FORMATS)}."
    )


def export_minute(
    conn: sqlite3.Connection,
    *,
    paths: Any,
    minute_id: int,
    export_format: str,
    include_unverified: bool = True,
    include_evidence: bool = True,
    branding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one minute to a file under the exports directory and record it.

    The filename carries the meeting's UUID, not its title: a display name is never a path
    component (ADR-0009), and two meetings called "Rapat Mingguan" must not collide. The
    title survives as the document's own heading, where it belongs.

    Written to a temporary file and renamed, so an export interrupted halfway leaves no
    half-written .docx that Word will open and the operator will forward.
    """
    from mom_igd.mom.document import build_document

    if export_format not in EXPORT_FORMATS:
        raise MinutesPipelineError(
            f"format ekspor {export_format!r} tidak dikenal. Pilih salah satu dari: "
            f"{', '.join(EXPORT_FORMATS)}."
        )

    minute = store.get_minute(conn, minute_id=minute_id)
    if minute is None:
        raise MinutesPipelineError(f"tidak ada notulen dengan id {minute_id}.")
    if str(minute["status"]) not in ("DRAFT",):
        raise MinutesPipelineError(
            f"notulen {minute_id} berstatus {minute['status']}, bukan DRAFT. Hanya "
            "notulen yang selesai dibuat yang dapat diekspor."
        )

    import json as _json

    header = dict(minute)
    header["summary"] = _json.loads(minute["summary_json"] or "[]")
    header["warnings"] = _json.loads(minute["warnings_json"] or "[]")
    header["summary_unsupported_numbers"] = _json.loads(
        minute["summary_unsupported_numbers"] or "[]"
    )
    header["document_number"] = minute["document_number"]

    items = store.load_items(conn, minute_id=minute_id)
    meeting = conn.execute(
        "SELECT * FROM meetings WHERE id = ?", (int(minute["meeting_id"]),)
    ).fetchone()
    recording = conn.execute(
        """
        SELECT r.* FROM recordings r
          JOIN transcripts t ON t.recording_id = r.id
         WHERE t.id = ?
        """,
        (int(minute["transcript_id"]),),
    ).fetchone()
    participants = [
        str(row["display_name"])
        for row in conn.execute(
            """
            SELECT p.display_name
              FROM meeting_participants mp
              JOIN participants p ON p.id = mp.participant_id
             WHERE mp.meeting_id = ? AND mp.is_active = 1 AND p.is_active = 1
             ORDER BY p.display_name
            """,
            (int(minute["meeting_id"]),),
        )
    ]

    document = build_document(
        minute=header,
        items=items,
        meeting=dict(meeting) if meeting else None,
        recording=dict(recording) if recording else None,
        participants=participants,
        include_unverified=include_unverified,
        include_evidence=include_evidence,
        branding=branding,
    )
    blob = _render(document, export_format)

    # `meetings.uuid` is nullable -- it arrived by ALTER in migration 0002, which cannot
    # add a NOT NULL column, and SQLite permits any number of NULLs in a unique index. A
    # row without one produced the filename "None-notulen-rev1.docx", and two such
    # meetings would collide on it: `record_export` deletes by path, so one meeting's
    # export row would silently come to describe another meeting's file.
    stem_id = str(meeting["uuid"]) if meeting and meeting["uuid"] else f"minute-{minute_id}"
    stem = f"{stem_id}-notulen-rev{int(minute['revision'])}"
    filename = f"{stem}{_EXTENSIONS[export_format]}"
    directory = Path(paths.exports_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename

    temporary = target.with_name(target.name + ".partial")
    try:
        temporary.write_bytes(blob)
        temporary.replace(target)
    except OSError as exc:
        # The likeliest cause by far is the operator having the previous export open in
        # Word, which locks the filename on Windows. That is an ordinary Tuesday, not a
        # crash, and it must not read like one -- so it is named, and the remedy is in
        # the message.
        temporary.unlink(missing_ok=True)
        raise MinutesExportError(
            f"{REASON_EXPORT_FAILED}: dokumen {filename} tidak dapat ditulis "
            f"({exc.strerror or exc}). Jika berkas itu sedang terbuka di Word, tutup "
            "dulu lalu jalankan `mom export` -- notulennya sendiri sudah tersimpan dan "
            "tidak perlu dibuat ulang."
        ) from None

    digest = hashlib.sha256(blob).hexdigest()
    store.record_export(
        conn,
        minute_id=minute_id,
        export_format=export_format,
        relative_path=filename,
        sha256=digest,
        size_bytes=len(blob),
        included_unverified=document.has_unverified,
        include_evidence=include_evidence,
    )
    return {
        "format": export_format,
        "relative_path": filename,
        "path": str(target),
        "sha256": digest,
        "size_bytes": len(blob),
        "included_unverified": document.has_unverified,
    }
