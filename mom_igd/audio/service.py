"""Recording service: the integration layer for Phase 2.

Ties device discovery, preflight, calibration, the capture session, the database,
the manifest, recovery, the job state machine and the audit trail into one object
with a deterministic lifecycle.

Invariants it enforces:

* **One recording per data root.** Guarded twice -- a lock file (so a second
  *process* is refused before it opens the microphone) and a partial unique index
  in the database (so a second *row* cannot exist). Either alone is insufficient:
  the index cannot stop a second process from grabbing the device, and the lock
  file cannot survive a stale process id.
* **No silent fallback.** A missing device is an error naming the device that was
  expected, never a switch to whatever else is plugged in.
* **The job reaches ``RECORDING`` only after the stream is actually open**, and
  ``RECORDED`` only after every chunk, checksum, manifest entry and database row
  is final. A workflow state that runs ahead of the audio would make the audit
  trail lie.
* **A recoverable error never destroys valid audio.** Finalised chunks stay
  finalised; the open partial is left on disk for the recovery service.
* **The manifest is authoritative.** It is written next to the audio by the thread
  that wrote the audio. The database mirrors it, and a mismatch is surfaced rather
  than reconciled silently.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Mapping

from mom_igd.audio.backend import (
    BYTES_PER_GB,
    AudioBackend,
    AudioError,
    CaptureProfile,
    DeviceNotFoundError,
)
from mom_igd.audio.calibration import CalibrationResult, run_calibration
from mom_igd.audio.devices import DeviceDiscoveryService, DeviceInfo, DeviceSelection
from mom_igd.audio.manifest import (
    MANIFEST_FILENAME,
    MANIFEST_SUMMARY_FILENAME,
    ChunkRecord,
    ManifestWriter,
    summarise_records,
    utc_now_iso,
    verify_manifest,
    write_manifest_summary,
)
from mom_igd.audio.preflight import PreflightReport, microphone_open_test, run_preflight
from mom_igd.audio.recovery import recover_recording, scan_recoverable
from mom_igd.audio.session import CaptureSession, SessionState
from mom_igd.audio.writer import FinalisedChunk
from mom_igd.audit import record_event
from mom_igd.config import AppConfig
from mom_igd.db.connection import connect, maybe_transaction
from mom_igd.jobs.state_machine import JobState, transition_job, transition_path
from mom_igd.logging_setup import get_logger
from mom_igd.paths import RuntimePaths

__all__ = [
    "ACTIVE_LIFECYCLE_STATES",
    "RecordingLifecycle",
    "RecordingServiceError",
    "RecordingService",
    "SingleRecordingLock",
]

_LOG = get_logger("audio.service")
_LOCK_FILENAME: Final[str] = "recording.lock"
_SELECTED_DEVICE_KEY: Final[str] = "selected_audio_device"
_CALIBRATION_KEY: Final[str] = "last_calibration"


class RecordingLifecycle(StrEnum):
    """Capture lifecycle. Must match the CHECK constraint in migration 0002."""

    IDLE = "IDLE"
    PREFLIGHT = "PREFLIGHT"
    ARMED = "ARMED"
    RECORDING = "RECORDING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    FINALIZING = "FINALIZING"
    RECORDED = "RECORDED"
    RECOVERABLE = "RECOVERABLE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ACTIVE_LIFECYCLE_STATES: Final[frozenset[str]] = frozenset(
    {
        RecordingLifecycle.PREFLIGHT.value,
        RecordingLifecycle.ARMED.value,
        RecordingLifecycle.RECORDING.value,
        RecordingLifecycle.PAUSED.value,
        RecordingLifecycle.STOPPING.value,
        RecordingLifecycle.FINALIZING.value,
    }
)
"""States the single-active-recording index treats as in flight."""

ALLOWED_LIFECYCLE_TRANSITIONS: Final[Mapping[RecordingLifecycle, frozenset[RecordingLifecycle]]] = {
    RecordingLifecycle.IDLE: frozenset(
        {RecordingLifecycle.PREFLIGHT, RecordingLifecycle.CANCELLED}
    ),
    RecordingLifecycle.PREFLIGHT: frozenset(
        {RecordingLifecycle.ARMED, RecordingLifecycle.FAILED, RecordingLifecycle.CANCELLED}
    ),
    RecordingLifecycle.ARMED: frozenset(
        {RecordingLifecycle.RECORDING, RecordingLifecycle.FAILED, RecordingLifecycle.CANCELLED}
    ),
    RecordingLifecycle.RECORDING: frozenset(
        {
            RecordingLifecycle.PAUSED,
            RecordingLifecycle.STOPPING,
            RecordingLifecycle.FAILED,
            RecordingLifecycle.RECOVERABLE,
        }
    ),
    RecordingLifecycle.PAUSED: frozenset(
        {
            RecordingLifecycle.RECORDING,
            RecordingLifecycle.STOPPING,
            RecordingLifecycle.FAILED,
            RecordingLifecycle.RECOVERABLE,
        }
    ),
    RecordingLifecycle.STOPPING: frozenset(
        {RecordingLifecycle.FINALIZING, RecordingLifecycle.FAILED}
    ),
    RecordingLifecycle.FINALIZING: frozenset(
        {RecordingLifecycle.RECORDED, RecordingLifecycle.FAILED, RecordingLifecycle.RECOVERABLE}
    ),
    RecordingLifecycle.RECORDED: frozenset(),
    RecordingLifecycle.RECOVERABLE: frozenset(
        {RecordingLifecycle.RECORDED, RecordingLifecycle.FAILED, RecordingLifecycle.CANCELLED}
    ),
    RecordingLifecycle.FAILED: frozenset({RecordingLifecycle.RECOVERABLE}),
    RecordingLifecycle.CANCELLED: frozenset(),
}


class RecordingServiceError(RuntimeError):
    """Raised for an invalid request against the recording lifecycle."""


class InvalidLifecycleTransition(RecordingServiceError):
    def __init__(self, current: RecordingLifecycle, requested: RecordingLifecycle) -> None:
        allowed = sorted(s.value for s in ALLOWED_LIFECYCLE_TRANSITIONS[current])
        super().__init__(
            f"Illegal recording transition {current.value} -> {requested.value}. "
            f"Allowed from {current.value}: {', '.join(allowed) or '<terminal>'}."
        )
        self.current = current
        self.requested = requested


# ---------------------------------------------------------------------------
# Single-active-recording lock
# ---------------------------------------------------------------------------


class SingleRecordingLock:
    """A cross-process lock so two processes cannot record at once.

    Created with ``O_EXCL``, so acquisition is atomic. A stale lock left by a
    process that was killed is detected by checking whether that process id is
    still alive -- otherwise a crash would make the application permanently
    unable to record, which is a worse failure than the one being prevented.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._held = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._held

    def read_holder(self) -> dict[str, Any] | None:
        if not self._path.is_file():
            return None
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"pid": None, "recording_uuid": None, "malformed": True}

    def read_live_holder(self) -> dict[str, Any] | None:
        """Return the holder only if its owning process is still running.

        The public way to ask "is the microphone actually in use?". Callers must not
        use :attr:`held` for that: this object is shared between the recording
        service and Phase 3 enrollment, so ``held`` is true whenever *this process*
        owns the lock -- including when it owns it for the other activity, which is
        exactly the case to refuse.

        A lock left behind by a killed process reports ``None``, because
        :meth:`acquire` would clear it and a caller that treated it as live would
        block forever on a holder that no longer exists.
        """
        holder = self.read_holder()
        if holder is None or not self._owner_alive(holder):
            return None
        return holder

    def acquire(self, recording_uuid: str) -> None:
        """Take the lock, clearing it first if its owner is gone.

        Raises:
            RecordingServiceError: If another live process holds it.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        holder = self.read_holder()
        if holder is not None and not self._owner_alive(holder):
            _LOG.warning(
                "Clearing a stale recording lock left by pid %s.", holder.get("pid")
            )
            self._path.unlink(missing_ok=True)
        try:
            handle = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = self.read_holder() or {}
            raise RecordingServiceError(
                "Another recording is already in progress in this data directory "
                f"(process {holder.get('pid')}, recording "
                f"{holder.get('recording_uuid')}). Only one recording may run at a "
                "time; stop it before starting another."
            ) from None
        try:
            payload = json.dumps(
                {
                    "pid": os.getpid(),
                    "recording_uuid": recording_uuid,
                    "acquired_utc": utc_now_iso(),
                }
            ).encode("utf-8")
            os.write(handle, payload)
        finally:
            os.close(handle)
        self._held = True

    def release(self) -> None:
        if self._held or self._path.is_file():
            self._path.unlink(missing_ok=True)
        self._held = False

    @staticmethod
    def _owner_alive(holder: Mapping[str, Any]) -> bool:
        pid = holder.get("pid")
        if not isinstance(pid, int):
            return False
        if pid == os.getpid():
            return True
        try:
            import psutil

            return psutil.pid_exists(pid)
        except Exception:  # noqa: BLE001 - assume gone rather than wedge forever
            return False


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Active:
    """Bookkeeping for the recording currently in flight."""

    recording_id: int
    recording_uuid: str
    meeting_id: int
    meeting_uuid: str
    job_id: int
    directory: Path
    relative_dir: str
    profile: CaptureProfile
    device: DeviceInfo
    session: CaptureSession
    manifest: ManifestWriter
    lifecycle: RecordingLifecycle
    started_monotonic_ns: int
    planned_minutes: float
    chunks: list[ChunkRecord]


class RecordingService:
    """Orchestrates one capture at a time, with the database and audit trail."""

    def __init__(
        self,
        config: AppConfig,
        paths: RuntimePaths,
        *,
        backend: AudioBackend | None = None,
        discovery: DeviceDiscoveryService | None = None,
    ) -> None:
        self._config = config
        self._paths = paths
        self._backend = backend if backend is not None else self._default_backend()
        self._discovery = discovery or DeviceDiscoveryService(self._backend)
        self._lock = SingleRecordingLock(paths.temp_dir / _LOCK_FILENAME)
        self._db_lock = threading.RLock()
        self._active: _Active | None = None
        #: Live preview transcriber for the running capture, if any. Optional by
        #: construction: absent here means the recording is unaffected.
        self._live: Any = None
        #: Most recent rolling level, published while the microphone is open so the
        #: interface can move a bar in real time. Plain dict, replaced wholesale, so a
        #: reader never sees a half-written value and no lock is needed.
        self._live_level: dict[str, Any] = {"active": False}
        self._state_lock = threading.RLock()

    @staticmethod
    def _default_backend() -> AudioBackend:
        from mom_igd.audio.sounddevice_backend import SoundDeviceBackend

        return SoundDeviceBackend()

    # -- database -----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        return connect(
            self._paths.database_path(self._config.database.filename),
            busy_timeout_ms=self._config.database.busy_timeout_ms,
        )

    def _database_ready(self) -> bool:
        path = self._paths.database_path(self._config.database.filename)
        if not path.exists():
            return False
        try:
            from mom_igd.db.migrator import current_schema_version, head_version

            with self._db_lock:
                conn = self._connect()
                try:
                    return current_schema_version(conn) == head_version()
                finally:
                    conn.close()
        except Exception:  # noqa: BLE001
            return False

    def _setting(self, key: str) -> str | None:
        if not self._database_ready():
            return None
        with self._db_lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT value FROM app_settings WHERE key = ?", (key,)
                ).fetchone()
            finally:
                conn.close()
        return str(row["value"]) if row else None

    def _store_setting(self, key: str, value: str) -> None:
        with self._db_lock:
            conn = self._connect()
            try:
                with maybe_transaction(conn):
                    conn.execute(
                        "INSERT INTO app_settings (key, value, updated_at) "
                        "VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                        "value = excluded.value, updated_at = excluded.updated_at",
                        (key, value, utc_now_iso()),
                    )
            finally:
                conn.close()

    def _audit(
        self,
        conn: sqlite3.Connection,
        action: str,
        *,
        entity_id: int | None = None,
        category: str = "RECORDING",
        entity_type: str = "recording",
        **detail: Any,
    ) -> None:
        record_event(
            conn,
            category=category,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail or None,
        )

    # -- devices ------------------------------------------------------------

    def list_devices(self, *, refresh: bool = True) -> dict[str, Any]:
        """Enumerate capture devices. Opens no stream."""
        usable = self._discovery.input_devices(refresh=refresh)
        rejected = self._discovery.rejected_devices()
        selected = self.selected_selection()
        return {
            "devices": [d.to_dict() for d in usable],
            "rejected": [
                {"name": d.name, "host_api": d.host_api, "reason": d.rejection_reason}
                for d in rejected
            ],
            "selected_fingerprint": selected.fingerprint if selected else None,
            "verified_usb_available": any(d.is_usb_conference_candidate for d in usable),
            "backend": self._backend.describe(),
        }

    def selected_selection(self) -> DeviceSelection | None:
        """The stored device preference: database first, then configuration."""
        raw = self._setting(_SELECTED_DEVICE_KEY)
        if raw:
            try:
                return DeviceSelection.from_dict(json.loads(raw))
            except (json.JSONDecodeError, KeyError, TypeError):
                _LOG.warning("Stored device selection is unreadable; ignoring it.")
        fingerprint = self._config.audio.preferred_device_fingerprint
        if fingerprint:
            return DeviceSelection(
                fingerprint=fingerprint, name="(from configuration)", host_api="", max_input_channels=0
            )
        return None

    def select_device(self, fingerprint: str) -> DeviceInfo:
        """Choose a device explicitly and remember it by fingerprint."""
        device = self._discovery.find_by_fingerprint(fingerprint, refresh=True)
        if device is None:
            available = [d.fingerprint for d in self._discovery.input_devices()]
            raise DeviceNotFoundError(
                f"No capture device with fingerprint {fingerprint!r}. Available: "
                f"{available or 'none'}."
            )
        if not device.is_usable:
            raise AudioError(f"Device {device.name!r} cannot be used: {device.rejection_reason}")
        selection = DeviceSelection.from_device(device)
        if self._database_ready():
            self._store_setting(_SELECTED_DEVICE_KEY, json.dumps(selection.to_dict()))
            with self._db_lock:
                conn = self._connect()
                try:
                    with maybe_transaction(conn):
                        record_event(
                            conn,
                            category="RECORDING",
                            action="device.selected",
                            detail={
                                "fingerprint": device.fingerprint,
                                "name": device.name,
                                "host_api": device.host_api,
                                "transport": device.transport.value,
                                "transport_verified": device.transport_source
                                == "windows-mmdevices-registry",
                            },
                        )
                finally:
                    conn.close()
        return device

    def resolve_device(self) -> tuple[DeviceInfo | None, str | None]:
        """Resolve the stored selection, or fall back to nothing (never to another
        microphone). Returns ``(device, error)``."""
        selection = self.selected_selection()
        if selection is None:
            default = self._discovery.default_input_device(refresh=True)
            if default is None:
                return None, "No capture device is available."
            return default, None
        try:
            return self._discovery.resolve_selection(selection), None
        except (DeviceNotFoundError, AudioError) as exc:
            return None, str(exc)

    def profile_for(self, device: DeviceInfo) -> CaptureProfile:
        return self._config.audio.capture_profile(
            sample_rate=device.default_sample_rate, channels=device.max_input_channels
        )

    # -- gate ---------------------------------------------------------------

    def preflight(self, *, planned_minutes: float | None = None) -> PreflightReport:
        """Run every pre-recording check. Opens no stream."""
        device, error = self.resolve_device()
        profile = self.profile_for(device) if device else None
        # A lock whose owner is gone must not count. `acquire()` clears such a lock,
        # but preflight runs *before* acquire -- so reading the raw holder here made a
        # lock left by a killed process fail preflight forever and permanently prevent
        # recording. That is precisely the failure `_owner_alive` exists to avoid,
        # defeated by the ordering.
        holder = self._lock.read_live_holder()
        active_db = self._active_recording_uuid()
        active = active_db or (holder.get("recording_uuid") if holder else None)
        return run_preflight(
            device=device,
            selection=self.selected_selection(),
            profile=profile,
            recordings_dir=self._paths.recordings_dir,
            database_ready=self._database_ready(),
            active_recording=active,
            min_free_disk_gb=self._config.audio.min_free_disk_gb,
            planned_minutes=planned_minutes if planned_minutes is not None else 120.0,
            device_error=error,
            pending_recovery=len(scan_recoverable(self._paths.recordings_dir)),
            production_requires_usb=self._config.audio.production_requires_usb,
        )

    def open_test(self) -> dict[str, Any]:
        """Briefly open the microphone to prove it delivers audio."""
        device, error = self.resolve_device()
        if device is None:
            raise RecordingServiceError(error or "No capture device available.")
        return microphone_open_test(self._backend, device, self.profile_for(device))

    def calibrate(
        self, *, seconds: float | None = None, save_to: Path | None = None
    ) -> CalibrationResult:
        """Run a microphone level test. **Opens the microphone.**

        The duration comes from ``audio.calibration_seconds`` (validated to 10-15 s
        by configuration). An explicit shorter value is accepted so automated tests
        can exercise this path without stalling, but it is not a useful production
        calibration -- a couple of seconds is not a representative sample of a room.
        """
        device, error = self.resolve_device()
        if device is None:
            raise RecordingServiceError(error or "No capture device available.")
        duration = seconds if seconds is not None else self._config.audio.calibration_seconds
        if duration <= 0:
            raise RecordingServiceError(f"calibration seconds={duration} must be positive.")
        self._live_level = {"active": True, "source": "calibration"}
        try:
            result = run_calibration(
                self._backend,
                device,
                self.profile_for(device),
                seconds=duration,
                save_to=save_to,
                meter_stride=self._config.audio.meter_stride,
                on_level=self._publish_level,
            )
        finally:
            self._live_level = {"active": False}
        if self._database_ready():
            self._store_setting(_CALIBRATION_KEY, json.dumps(result.evidence()))
            with self._db_lock:
                conn = self._connect()
                try:
                    with maybe_transaction(conn):
                        record_event(
                            conn,
                            category="RECORDING",
                            action="calibration.completed",
                            detail=result.evidence(),
                        )
                finally:
                    conn.close()
        return result

    # -- lifecycle ----------------------------------------------------------

    def _active_recording_uuid(self) -> str | None:
        if not self._database_ready():
            return None
        with self._db_lock:
            conn = self._connect()
            try:
                placeholders = ",".join("?" for _ in ACTIVE_LIFECYCLE_STATES)
                row = conn.execute(
                    f"SELECT recording_uuid FROM recordings WHERE status IN ({placeholders}) LIMIT 1",
                    tuple(sorted(ACTIVE_LIFECYCLE_STATES)),
                ).fetchone()
            finally:
                conn.close()
        return str(row["recording_uuid"]) if row else None

    def _assert_transition(
        self, current: RecordingLifecycle, requested: RecordingLifecycle
    ) -> RecordingLifecycle:
        if requested not in ALLOWED_LIFECYCLE_TRANSITIONS[current]:
            raise InvalidLifecycleTransition(current, requested)
        return requested

    def _meeting_uuid(self, conn: sqlite3.Connection, meeting_id: int) -> str:
        row = conn.execute("SELECT uuid FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if row is None:
            raise RecordingServiceError(
                f"No meeting with id={meeting_id}. Start a recording without a "
                "meeting_id to have a draft meeting created for it."
            )
        existing = row["uuid"]
        if existing:
            return str(existing)
        generated = str(uuid.uuid4())
        conn.execute("UPDATE meetings SET uuid = ? WHERE id = ?", (generated, meeting_id))
        return generated

    def _create_draft_meeting(
        self, conn: sqlite3.Connection, title: str | None
    ) -> tuple[int, str]:
        """Create the minimal meeting row a recording needs, and return it.

        **Why this exists.** A recording is a child of a meeting, but Meeting setup
        is a Phase 9 screen. Without this, the operator would have to invent an
        internal database id -- and on a fresh data root there is no row to name, so
        recording would be impossible in a fresh install.

        The title is free text supplied by the operator, or a timestamp when they
        leave it blank. It is **never** used to build a path: the on-disk layout is
        addressed by ``uuid`` alone, so a title can hold a participant's name
        without that name reaching the filesystem.
        """
        clean = (title or "").strip()
        if not clean:
            clean = f"Rapat {utc_now_iso()[:16].replace('T', ' ')} UTC"
        clean = clean[:200]
        meeting_uuid = str(uuid.uuid4())
        # Roster capacity is written explicitly from configuration rather than left
        # to the column DEFAULT. The DEFAULT is 9 because migration 0004 has to
        # backfill meetings that predate the setting; it is *not* the policy for a
        # new meeting. Relying on it would mean an operator who configured 15 got 9
        # on every meeting the recording panel creates.
        #
        # This reads one validated integer out of the AppConfig the service already
        # holds. It deliberately does NOT import the participant or enrollment
        # package: that dependency direction is how a roster starts influencing
        # capture, which must never happen.
        capacity = int(self._config.participants.default_meeting_participant_capacity)
        cursor = conn.execute(
            "INSERT INTO meetings (title, uuid, participant_capacity) VALUES (?, ?, ?)",
            (clean, meeting_uuid, capacity),
        )
        meeting_id = int(cursor.lastrowid or 0)
        self._audit(
            conn,
            "meeting.draft_created",
            category="MEETING",
            entity_type="meeting",
            entity_id=meeting_id,
            meeting_uuid=meeting_uuid,
            participant_capacity=capacity,
        )
        return meeting_id, meeting_uuid

    def _ensure_job(self, conn: sqlite3.Connection, meeting_id: int) -> int:
        row = conn.execute(
            "SELECT id, state FROM jobs WHERE meeting_id = ? "
            "AND state NOT IN ('APPROVED','CANCELLED') ORDER BY id DESC LIMIT 1",
            (meeting_id,),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        from mom_igd.jobs.state_machine import create_job

        return create_job(conn, meeting_id)

    def start(
        self,
        meeting_id: int | None = None,
        *,
        meeting_title: str | None = None,
        planned_minutes: float | None = None,
    ) -> dict[str, Any]:
        """Arm and start a recording. Idempotent: a second call is a no-op.

        The microphone is opened here, and only here.

        ``meeting_id`` attaches the recording to an existing meeting. Omitting it
        creates a draft meeting from ``meeting_title`` -- which is what the shell
        does, because Meeting setup is a later phase and a fresh install has no
        meeting to attach to. Both paths run in the transaction below, and the
        whole method holds ``_state_lock``, so a double-clicked Start cannot
        produce two meetings or two recordings.
        """
        with self._state_lock:
            if self._active is not None:
                if self._active.lifecycle in {
                    RecordingLifecycle.RECORDING,
                    RecordingLifecycle.PAUSED,
                }:
                    _LOG.info("Start ignored: recording %s is already running.",
                              self._active.recording_uuid)
                    return self.status()
                raise RecordingServiceError(
                    f"A recording is already in state {self._active.lifecycle.value}."
                )

            report = self.preflight(planned_minutes=planned_minutes)
            if not report.can_start:
                reasons = "; ".join(f"{i.key}: {i.detail}" for i in report.failures)
                if self._database_ready():
                    with self._db_lock:
                        conn = self._connect()
                        try:
                            with maybe_transaction(conn):
                                self._audit(conn, "preflight.failed", reasons=reasons[:500])
                        finally:
                            conn.close()
                raise RecordingServiceError(f"Preflight failed. {reasons}")

            device, error = self.resolve_device()
            if device is None:
                raise RecordingServiceError(error or "No capture device available.")
            profile = self.profile_for(device)
            recording_uuid = str(uuid.uuid4())

            # Lock before touching the device: a second process must be refused
            # before it can open the microphone.
            self._lock.acquire(recording_uuid)
            try:
                with self._db_lock:
                    conn = self._connect()
                    try:
                        with maybe_transaction(conn):
                            if meeting_id is None:
                                meeting_id, meeting_uuid = self._create_draft_meeting(
                                    conn, meeting_title
                                )
                            else:
                                meeting_uuid = self._meeting_uuid(conn, meeting_id)
                            job_id = self._ensure_job(conn, meeting_id)
                            relative_dir = f"{meeting_uuid}/{recording_uuid}"
                            cursor = conn.execute(
                                "INSERT INTO recordings ("
                                " meeting_id, recording_uuid, relative_dir, status, container,"
                                " sample_rate_hz, channels, sample_format, bit_depth,"
                                " chunk_seconds, device_fingerprint, device_name,"
                                " device_host_api, device_transport,"
                                " device_transport_verified, device_index_hint,"
                                " device_snapshot_json, manifest_relative_path,"
                                " manifest_status"
                                ") VALUES (?,?,?,?,'wav',?,?,?,?,?,?,?,?,?,?,?,?,?, 'PENDING')",
                                (
                                    meeting_id,
                                    recording_uuid,
                                    relative_dir,
                                    RecordingLifecycle.ARMED.value,
                                    profile.sample_rate,
                                    profile.channels,
                                    profile.sample_format.value,
                                    profile.sample_format.bytes_per_sample * 8,
                                    profile.chunk_seconds,
                                    device.fingerprint,
                                    device.name,
                                    device.host_api,
                                    device.transport.value,
                                    1
                                    if device.transport_source == "windows-mmdevices-registry"
                                    else 0,
                                    device.index,
                                    json.dumps(device.to_dict()),
                                    f"{relative_dir}/{MANIFEST_FILENAME}",
                                ),
                            )
                            recording_id = int(cursor.lastrowid or 0)
                            self._audit(
                                conn,
                                "preflight.passed",
                                entity_id=recording_id,
                                warnings=[i.key for i in report.warnings],
                            )
                            self._audit(
                                conn,
                                "recording.armed",
                                entity_id=recording_id,
                                recording_uuid=recording_uuid,
                                device_fingerprint=device.fingerprint,
                                sample_rate=profile.sample_rate,
                                channels=profile.channels,
                            )
                    finally:
                        conn.close()

                directory = self._paths.recordings_dir / meeting_uuid / recording_uuid
                directory.mkdir(parents=True, exist_ok=True)
                manifest = ManifestWriter(directory)
                live = self._start_live_preview(profile)
                session = CaptureSession(
                    self._backend,
                    device_index=device.index,
                    profile=profile,
                    directory=directory,
                    recording_uuid=recording_uuid,
                    queue_seconds=self._config.audio.queue_seconds,
                    manifest=manifest,
                    on_chunk=self._on_chunk_finalised,
                    meter_stride=self._config.audio.meter_stride,
                    live_tap=live.feed if live is not None else None,
                )
                self._active = _Active(
                    recording_id=recording_id,
                    recording_uuid=recording_uuid,
                    meeting_id=meeting_id,
                    meeting_uuid=meeting_uuid,
                    job_id=job_id,
                    directory=directory,
                    relative_dir=relative_dir,
                    profile=profile,
                    device=device,
                    session=session,
                    manifest=manifest,
                    lifecycle=RecordingLifecycle.ARMED,
                    started_monotonic_ns=time.monotonic_ns(),
                    planned_minutes=planned_minutes or 120.0,
                    chunks=[],
                )

                # Open the microphone. The job only advances once this succeeds.
                session.start()
            except Exception as exc:
                self._lock.release()
                active = self._active
                self._active = None
                if active is not None:
                    self._set_lifecycle_db(
                        active.recording_id, RecordingLifecycle.FAILED, error=str(exc)
                    )
                raise

            self._active.lifecycle = RecordingLifecycle.RECORDING
            self._set_lifecycle_db(
                recording_id,
                RecordingLifecycle.RECORDING,
                started_at=utc_now_iso(),
                monotonic_start_ns=self._active.started_monotonic_ns,
            )
            self._advance_job(JobState.RECORDING, "recording started")
            with self._db_lock:
                conn = self._connect()
                try:
                    with maybe_transaction(conn):
                        self._audit(
                            conn,
                            "recording.started",
                            entity_id=recording_id,
                            recording_uuid=recording_uuid,
                        )
                finally:
                    conn.close()
            _LOG.info("Recording %s started.", recording_uuid)
            return self.status()

    def pause(self) -> dict[str, Any]:
        with self._state_lock:
            active = self._require_active()
            if active.lifecycle is RecordingLifecycle.PAUSED:
                return self.status()
            self._assert_transition(active.lifecycle, RecordingLifecycle.PAUSED)
            active.session.pause()
            active.lifecycle = RecordingLifecycle.PAUSED
            self._set_lifecycle_db(active.recording_id, RecordingLifecycle.PAUSED)
            with self._db_lock:
                conn = self._connect()
                try:
                    with maybe_transaction(conn):
                        conn.execute(
                            "UPDATE recordings SET pause_count = pause_count + 1 WHERE id = ?",
                            (active.recording_id,),
                        )
                        self._audit(conn, "recording.paused", entity_id=active.recording_id)
                finally:
                    conn.close()
            return self.status()

    def resume(self) -> dict[str, Any]:
        with self._state_lock:
            active = self._require_active()
            if active.lifecycle is RecordingLifecycle.RECORDING:
                return self.status()
            self._assert_transition(active.lifecycle, RecordingLifecycle.RECORDING)
            active.session.resume()
            active.lifecycle = RecordingLifecycle.RECORDING
            self._set_lifecycle_db(active.recording_id, RecordingLifecycle.RECORDING)
            with self._db_lock:
                conn = self._connect()
                try:
                    with maybe_transaction(conn):
                        self._audit(conn, "recording.resumed", entity_id=active.recording_id)
                finally:
                    conn.close()
            return self.status()

    def stop(self) -> dict[str, Any]:
        """Finalise the recording. Idempotent."""
        # Released before finalisation, not after: the preview holds 693 MiB and the
        # finalisation path hashes and renames files. Handing that memory back first
        # costs nothing and keeps the two from overlapping.
        self._stop_live_preview()
        with self._state_lock:
            active = self._active
            if active is None:
                return self.status()
            if active.lifecycle in {
                RecordingLifecycle.RECORDED,
                RecordingLifecycle.FAILED,
                RecordingLifecycle.CANCELLED,
            }:
                return self.status()

            self._assert_transition(active.lifecycle, RecordingLifecycle.STOPPING)
            active.lifecycle = RecordingLifecycle.STOPPING
            self._set_lifecycle_db(active.recording_id, RecordingLifecycle.STOPPING)

            result = active.session.stop()

            active.lifecycle = RecordingLifecycle.FINALIZING
            self._set_lifecycle_db(active.recording_id, RecordingLifecycle.FINALIZING)

            summary = write_manifest_summary(
                active.directory,
                recording_uuid=active.recording_uuid,
                meeting_uuid=active.meeting_uuid,
                profile=active.profile,
                records=active.chunks,
                device=active.device.to_dict(),
                quality=active.session.cumulative_quality().to_dict(),
                counters={
                    "queue": active.session.queue_stats,
                    "writer": active.session.writer_stats,
                    "stream": result.to_dict(),
                },
                gaps=result.gaps,
            )
            verification = verify_manifest(active.directory)

            failed = result.error is not None or not verification.ok
            final = RecordingLifecycle.RECOVERABLE if failed else RecordingLifecycle.RECORDED
            self._assert_transition(RecordingLifecycle.FINALIZING, final)

            with self._db_lock:
                conn = self._connect()
                try:
                    with maybe_transaction(conn):
                        conn.execute(
                            "UPDATE recordings SET status = ?, ended_at = ?,"
                            " monotonic_end_ns = ?, duration_ms = ?, paused_ms = ?,"
                            " written_frames = ?, dropped_frames = ?, xrun_callbacks = ?,"
                            " queue_high_water_frames = ?, chunk_count = ?,"
                            " total_bytes = ?, manifest_sha256 = ?, manifest_status = ?,"
                            " peak_dbfs = ?, rms_dbfs = ?, clipped_samples = ?,"
                            " quality_verdict = ?, degraded = ?, last_error = ?,"
                            " updated_at = ? WHERE id = ?",
                            (
                                final.value,
                                utc_now_iso(),
                                time.monotonic_ns(),
                                int(result.audio_seconds * 1000),
                                int(active.session.status()["paused_seconds"] * 1000),
                                result.frames_written,
                                result.dropped_frames,
                                result.xrun_callbacks,
                                active.session.queue_stats["high_water_frames"],
                                len(active.chunks),
                                int(summary["total_bytes"]),
                                str(summary["chain_sha256"]),
                                "VERIFIED" if verification.ok else "MISMATCH",
                                active.session.cumulative_quality().peak_dbfs,
                                active.session.cumulative_quality().rms_dbfs,
                                active.session.cumulative_quality().clipped_samples,
                                active.session.cumulative_quality().verdict.value,
                                1 if result.degraded else 0,
                                result.error
                                or ("; ".join(verification.problems)[:500] or None),
                                utc_now_iso(),
                                active.recording_id,
                            ),
                        )
                        self._audit(
                            conn,
                            "recording.stopped",
                            entity_id=active.recording_id,
                            recording_uuid=active.recording_uuid,
                            chunks=len(active.chunks),
                            frames=result.frames_written,
                            dropped_frames=result.dropped_frames,
                            degraded=result.degraded,
                            manifest_verified=verification.ok,
                        )
                finally:
                    conn.close()

            active.lifecycle = final
            if final is RecordingLifecycle.RECORDED:
                self._advance_job(JobState.RECORDED, "recording finalised and verified")
            else:
                self._advance_job(
                    JobState.FAILED,
                    result.error or "manifest verification failed",
                    error=result.error,
                )
            self._lock.release()
            return self._final_status()

    def abandon(self, reason: str) -> dict[str, Any]:
        """Give up without finalising, leaving the partial for recovery."""
        with self._state_lock:
            active = self._active
            if active is None:
                return self.status()
            result = active.session.abandon()
            active.lifecycle = RecordingLifecycle.RECOVERABLE
            self._set_lifecycle_db(
                active.recording_id,
                RecordingLifecycle.RECOVERABLE,
                error=reason,
                ended_at=utc_now_iso(),
            )
            with self._db_lock:
                conn = self._connect()
                try:
                    with maybe_transaction(conn):
                        self._audit(
                            conn,
                            "recording.abandoned",
                            entity_id=active.recording_id,
                            reason=reason[:300],
                            frames=result.frames_written,
                        )
                finally:
                    conn.close()
            self._advance_job(JobState.FAILED, reason, error=reason)
            self._lock.release()
            return self._final_status()

    # -- reporting ----------------------------------------------------------

    def _final_status(self) -> dict[str, Any]:
        """Clear the finished session and return its closing summary.

        The summary (chunk count, uuid, relative_dir, quality) is only reachable
        while ``_active`` is still set, so it is built first -- but the caller must
        not be told a recording is still running, because the UI enables Stop and
        disables Start on exactly that flag. So the snapshot is taken with the
        session in place and then corrected to the post-stop reality.
        """
        payload = self.status()
        self._active = None
        payload["recording_active"] = False
        payload["pending_recovery"] = len(scan_recoverable(self._paths.recordings_dir))
        return payload

    def status(self) -> dict[str, Any]:
        """Cheap snapshot, safe to poll at 2-4 Hz. Exposes no absolute path."""
        active = self._active
        free_bytes = self._free_bytes()
        payload: dict[str, Any] = {
            "recording_active": active is not None,
            "lifecycle": active.lifecycle.value if active else RecordingLifecycle.IDLE.value,
            "disk_free_gb": round(free_bytes / BYTES_PER_GB, 2),
            "disk_low": free_bytes < int(self._config.audio.low_disk_abort_gb * BYTES_PER_GB),
            "status_poll_hz": self._config.audio.status_poll_hz,
            "capabilities": {
                "audio_capture": True,
                "transcript": False,
                "speaker_identification": False,
                "mom_generation": False,
                "export": False,
            },
        }
        if active is None:
            payload["pending_recovery"] = len(scan_recoverable(self._paths.recordings_dir))
            return payload
        session = active.session.status()
        payload.update(
            {
                "recording_uuid": active.recording_uuid,
                "meeting_id": active.meeting_id,
                "job_id": active.job_id,
                "device": {
                    "name": active.device.name,
                    "fingerprint": active.device.fingerprint,
                    "transport": active.device.transport.value,
                    "transport_verified": active.device.transport_source
                    == "windows-mmdevices-registry",
                },
                "session": session,
                "chunks": len(active.chunks),
                "degraded": active.session.degraded,
                "planned_minutes": active.planned_minutes,
            }
        )
        return payload

    def quality(self) -> dict[str, Any]:
        active = self._active
        if active is None:
            return {"available": False, "reason": "no recording in progress"}
        rolling = active.session.quality()
        cumulative = active.session.cumulative_quality()
        return {
            "available": True,
            "rolling": rolling.to_dict(),
            "cumulative": cumulative.to_dict(),
        }

    def verify(self, recording_uuid: str) -> dict[str, Any]:
        """Verify one recording's chunks against its manifest, and the database."""
        with self._db_lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id, relative_dir, status, chunk_count, manifest_sha256 "
                    "FROM recordings WHERE recording_uuid = ?",
                    (recording_uuid,),
                ).fetchone()
                if row is None:
                    raise RecordingServiceError(f"No recording {recording_uuid!r}.")
                db_chunks = conn.execute(
                    "SELECT seq, filename, sha256, frames FROM recording_chunks "
                    "WHERE recording_id = ? ORDER BY seq",
                    (int(row["id"]),),
                ).fetchall()
            finally:
                conn.close()

        directory = self._paths.recordings_dir / str(row["relative_dir"])
        report = verify_manifest(directory)
        payload = report.to_dict()

        # Compare the database mirror against the authoritative manifest.
        from mom_igd.audio.manifest import read_manifest

        records, _, _ = read_manifest(directory)
        manifest_by_seq = {r.seq: r for r in records}
        mismatches: list[str] = []
        for chunk in db_chunks:
            record = manifest_by_seq.get(int(chunk["seq"]))
            if record is None:
                mismatches.append(f"chunk {chunk['seq']} is in the database but not the manifest")
                continue
            if chunk["sha256"] and record.sha256 != chunk["sha256"]:
                mismatches.append(f"chunk {chunk['seq']} checksum differs between database and manifest")
            if chunk["frames"] is not None and int(chunk["frames"]) != record.frame_count:
                mismatches.append(f"chunk {chunk['seq']} frame count differs")
        for seq in set(manifest_by_seq) - {int(c["seq"]) for c in db_chunks}:
            mismatches.append(f"chunk {seq} is in the manifest but not the database")

        payload["database_mismatches"] = mismatches
        payload["database_chunk_count"] = len(db_chunks)
        payload["recording_status"] = str(row["status"])
        payload["ok"] = report.ok and not mismatches
        return payload

    def recover_all(self) -> dict[str, Any]:
        """Recover every interrupted recording. Idempotent."""
        candidates = scan_recoverable(self._paths.recordings_dir)
        reports: list[dict[str, Any]] = []
        for directory in candidates:
            report = recover_recording(directory, profile=None)
            reports.append(report.to_dict())
            if self._database_ready():
                self._sync_recovery(directory, report)
        return {
            "scanned": len(candidates),
            "recovered_chunks": sum(r["chunks_recovered"] for r in reports),
            "quarantined_chunks": sum(r["chunks_quarantined"] for r in reports),
            "reports": reports,
        }

    # -- internals ----------------------------------------------------------

    def _require_active(self) -> _Active:
        if self._active is None:
            raise RecordingServiceError("No recording is in progress.")
        return self._active

    def _free_bytes(self) -> int:
        import shutil

        probe = self._paths.recordings_dir
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            return 0

    def voice_check(self, *, seconds: float | None = None) -> dict[str, Any]:
        """Open the microphone, show the level moving **and** the words appearing.

        The verification tool an operator actually needs before a meeting. A level meter
        proves the microphone is delivering *sound*; it says nothing about whether that
        sound becomes the right words. Somebody responsible for a minute needs to see a
        sentence they just spoke appear correctly before they trust ninety minutes of it.

        **Nothing is written.** No chunk, no manifest, no database row, no transcript.
        The audio is measured, transcribed for display, and discarded. This is not a
        recording and does not appear in the recordings list.

        Refused while a capture is running: the microphone is already in use, and one
        recording at a time is the rule the whole capture path is built on.
        """
        if self._active is not None:
            raise RecordingServiceError(
                "A recording is in progress. Stop it before running a voice check -- "
                "the microphone is already in use."
            )
        device, error = self.resolve_device()
        if device is None:
            raise RecordingServiceError(error or "No capture device available.")

        profile = self.profile_for(device)
        # Long enough for at least two accurate windows plus their tail.
        duration = float(seconds or 30.0)
        if not 3.0 <= duration <= 60.0:
            raise RecordingServiceError(
                f"voice check seconds={duration} must be between 3 and 60. A shorter "
                "check proves nothing and a longer one is a recording."
            )

        live = None
        try:
            from mom_igd.asr.live import LiveTranscriber

            # The *fast* profile while the operator is speaking. Its job here is only
            # reassurance -- proof that words are arriving at all -- and for that,
            # something every six seconds beats something correct at twenty.
            #
            # Accuracy comes from the single pass at the end instead. See
            # `_final_transcription`.
            live = LiveTranscriber(
                self._paths.models_dir,
                source_rate=profile.sample_rate,
                source_channels=profile.channels,
                cpu_threads=int(getattr(self._config.audio, "live_preview_threads", 4)),
                language=getattr(self._config.asr, "language", "id"),
                initial_prompt=self._live_prompt(),
            )
            live.start()
            self._live = live
        except Exception as exc:  # noqa: BLE001 - the level check still has value alone
            _LOG.warning(
                "Voice check could not start transcription (%s); the level meter still "
                "runs, so the microphone can be verified even without a model.",
                type(exc).__name__,
            )
            live = None

        # Held in memory for the final pass, and only that. Thirty seconds of 44.1 kHz
        # stereo is about five megabytes; it is dropped when this method returns and
        # never touches the disk.
        captured: list[bytes] = []

        def tap(pcm: bytes) -> None:
            captured.append(pcm)
            if live is not None:
                live.feed(pcm)

        self._live_level = {"active": True, "source": "voice_check"}
        try:
            result = run_calibration(
                self._backend,
                device,
                profile,
                seconds=duration,
                save_to=None,
                meter_stride=self._config.audio.meter_stride,
                on_level=self._publish_level,
                on_audio=tap,
            )
        finally:
            self._live_level = {"active": False}

        transcript = {"running": False, "segments": [], "text": "", "is_preview": True}
        if live is not None:
            transcript = live.stop().to_dict()
            self._live = None

        final_text, final_error = self._final_transcription(captured, profile)

        return {
            "ok": result.error is None and result.frames > 0,
            "seconds": round(result.seconds, 2),
            "verdict": result.verdict.value,
            "advice": result.advice,
            "levels": result.snapshot.to_dict(),
            "transcript": transcript,
            # What the operator should actually read and judge.
            "final_text": final_text,
            "final_error": final_error,
            "model_available": live is not None,
            "error": result.error,
            # Stated in the payload, not only in the interface: nothing here was kept.
            "stored": False,
        }

    def _final_transcription(
        self, captured: list[bytes], profile: Any
    ) -> tuple[str, str | None]:
        """The answer the operator reads: one accurate pass over the whole check.

        The decoding itself lives in `mom_igd.asr.live.decode_once`, not here. Phase 2
        captures audio and must not try to understand it -- a boundary a test enforces by
        reading this package, and one this method crossed on its first version by calling
        the Whisper provider directly. What stays here is what this layer actually owns:
        the captured bytes, the device profile they were captured with, and the decision
        that a failed pass must leave the streaming preview standing.

        Imported inside the method, like every other model import in this file, so a
        machine with no ASR dependency can still run a level check.
        """
        try:
            from mom_igd.asr.live import decode_once
        except Exception:  # noqa: BLE001 - a level check does not need a decoder
            return ("", "NO_MODEL")
        return decode_once(
            self._paths.models_dir,
            b"".join(captured),
            source_rate=profile.sample_rate,
            source_channels=profile.channels,
            language=getattr(self._config.asr, "language", "id"),
            initial_prompt=self._live_prompt(),
        )

    def _publish_level(self, snapshot: Any) -> None:
        """Store the rolling level for the interface. Called from the measuring thread."""
        self._live_level = {
            "active": True,
            "source": "calibration",
            "rms_dbfs": round(snapshot.rms_dbfs, 2),
            "peak_dbfs": round(snapshot.peak_dbfs, 2),
            "silence_percent": round(snapshot.silence_percent, 1),
            "channels": [
                {"channel": c.channel, "rms_dbfs": round(c.rms_dbfs, 2), "active": c.active}
                for c in snapshot.channels
            ],
        }

    def live_level(self) -> dict[str, Any]:
        """The level right now, while the microphone is open. Cheap to poll.

        Reports the *recording* meter when a capture is running and the calibration
        meter when a microphone test is. Idle returns ``active: false`` rather than a
        stale reading: a bar frozen at somebody's last words looks exactly like a bar
        that is working, and this project has now been caught by that once.
        """
        active = self._active
        if active is not None:
            rolling = active.session.quality()
            return {
                "active": True,
                "source": "recording",
                "rms_dbfs": round(rolling.rms_dbfs, 2),
                "peak_dbfs": round(rolling.peak_dbfs, 2),
                "silence_percent": round(rolling.silence_percent, 1),
                "channels": [
                    {"channel": c.channel, "rms_dbfs": round(c.rms_dbfs, 2), "active": c.active}
                    for c in rolling.channels
                ],
            }
        return dict(self._live_level)

    def _live_prompt(self) -> str | None:
        """The terminology prompt the batch pipeline uses, for the preview too.

        Whisper spells unfamiliar words by ear -- "deploy" comes back as "deploi" -- and
        a meeting is full of them. The batch path has primed with this since Phase 4;
        the preview was reading the same rooms without it. Any failure is ignored: a
        missing glossary must not stop a microphone test.
        """
        try:
            from mom_igd.asr.glossary import load_glossary
            from mom_igd.paths import repo_root

            glossary = load_glossary(
                repo_root() / "config" / self._config.asr.glossary_filename
            )
            return glossary.initial_prompt(
                max_chars=int(self._config.asr.initial_prompt_max_chars)
            )
        except Exception:  # noqa: BLE001 - a prompt is an optimisation, never a gate
            return None

    def _start_live_preview(self, profile: Any) -> Any:
        """Start live preview transcription, or return ``None``. Never raises.

        Off unless ``[audio].live_preview`` is on and a pass-1 model is installed.
        Everything about this is optional: a machine with no model records exactly as
        before, and so does a machine where the preview fails to start. The recording is
        the product; the preview is reassurance that it is working.
        """
        if not getattr(self._config.audio, "live_preview", False):
            return None
        try:
            from mom_igd.asr.live import LiveTranscriber

            live = LiveTranscriber(
                self._paths.models_dir,
                source_rate=profile.sample_rate,
                source_channels=profile.channels,
                cpu_threads=int(getattr(self._config.audio, "live_preview_threads", 4)),
                language=getattr(self._config.asr, "language", "id"),
                initial_prompt=self._live_prompt(),
            )
            live.start()
        except Exception as exc:  # noqa: BLE001 - a preview must not stop a recording
            _LOG.warning(
                "Live preview could not start (%s); the recording continues without it.",
                type(exc).__name__,
            )
            return None
        self._live = live
        return live

    def _stop_live_preview(self) -> None:
        """Release the preview model. Called on every exit path from a recording."""
        live, self._live = self._live, None
        if live is None:
            return
        try:
            live.stop()
        except Exception:  # noqa: BLE001 - teardown must not mask a recording result
            pass

    def live_transcript(self) -> dict[str, Any]:
        """What the preview has heard so far. Empty when the preview is not running."""
        live = self._live
        if live is None:
            return {"running": False, "segments": [], "text": "", "is_preview": True}
        return live.snapshot().to_dict()

    def _on_chunk_finalised(self, finalised: FinalisedChunk) -> None:
        """Mirror a finalised chunk into the database. Runs on the writer thread."""
        active = self._active
        if active is None:  # pragma: no cover - chunk after teardown
            return
        active.chunks.append(finalised.record)
        record = finalised.record
        try:
            with self._db_lock:
                conn = self._connect()
                try:
                    with maybe_transaction(conn):
                        conn.execute(
                            "INSERT INTO recording_chunks ("
                            " recording_id, seq, filename, start_frame, end_frame, frames,"
                            " duration_ms, utc_start, utc_end, monotonic_start_ns,"
                            " monotonic_end_ns, sample_rate_hz, channels, sample_format,"
                            " size_bytes, sha256, dropped_frames, xrun_callbacks,"
                            " peak_dbfs, rms_dbfs, clipped_samples, status,"
                            " recovery_status, finalized"
                            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                            (
                                active.recording_id,
                                record.seq,
                                record.filename,
                                record.start_frame,
                                record.end_frame,
                                record.frame_count,
                                record.duration_ms,
                                record.utc_start,
                                record.utc_end,
                                record.monotonic_start_ns,
                                record.monotonic_end_ns,
                                record.sample_rate,
                                record.channels,
                                record.sample_format,
                                record.byte_count,
                                record.sha256,
                                record.dropped_frames,
                                record.xrun_callbacks,
                                record.peak_dbfs,
                                record.rms_dbfs,
                                record.clipped_samples,
                                record.status,
                                record.recovery_status,
                            ),
                        )
                        conn.execute(
                            "UPDATE recordings SET chunk_count = chunk_count + 1,"
                            " total_bytes = total_bytes + ?, written_frames = ?,"
                            " updated_at = ? WHERE id = ?",
                            (
                                record.byte_count,
                                record.end_frame,
                                utc_now_iso(),
                                active.recording_id,
                            ),
                        )
                        self._audit(
                            conn,
                            "recording.chunk_finalized",
                            entity_id=active.recording_id,
                            seq=record.seq,
                            frames=record.frame_count,
                            sha256=record.sha256[:16],
                            dropped_frames=record.dropped_frames,
                        )
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001 - never kill the writer thread
            _LOG.error(
                "Chunk %d is on disk and in the manifest but could not be recorded in "
                "the database: %s. The manifest remains authoritative; run "
                "`audio verify` to see the mismatch.",
                record.seq,
                exc,
            )

    def _set_lifecycle_db(
        self, recording_id: int, lifecycle: RecordingLifecycle, **fields: Any
    ) -> None:
        if not self._database_ready():
            return
        assignments = ["status = ?", "updated_at = ?"]
        params: list[Any] = [lifecycle.value, utc_now_iso()]
        for column, value in fields.items():
            column_name = "last_error" if column == "error" else column
            assignments.append(f"{column_name} = ?")
            params.append(value)
        params.append(recording_id)
        with self._db_lock:
            conn = self._connect()
            try:
                with maybe_transaction(conn):
                    conn.execute(
                        f"UPDATE recordings SET {', '.join(assignments)} WHERE id = ?",
                        tuple(params),
                    )
            finally:
                conn.close()

    def _advance_job(self, target: JobState, reason: str, *, error: str | None = None) -> None:
        """Walk the job to ``target`` along the declared transition graph.

        The route is computed rather than hardcoded: Phase 1 sends a job from
        ``DRAFT`` to ``RECORDING`` via ``READY``, and this layer should not have to
        know that. If no legal route exists the job is left alone and the problem is
        logged -- writing the state directly would put the audit trail out of step
        with the state machine that is supposed to own it.
        """
        active = self._active
        if active is None or not self._database_ready():
            return
        with self._db_lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT state FROM jobs WHERE id = ?", (active.job_id,)
                ).fetchone()
                if row is None:
                    return
                current = JobState(str(row["state"]))
                route = transition_path(current, target)
                if route is None:
                    _LOG.error(
                        "Job %d cannot reach %s from %s; leaving it unchanged.",
                        active.job_id,
                        target.value,
                        current.value,
                    )
                    return
                for index, step in enumerate(route):
                    last = index == len(route) - 1
                    transition_job(
                        conn,
                        active.job_id,
                        step,
                        reason=reason if last else f"{reason} (via {step.value})",
                        error=error if last else None,
                    )
            except Exception as exc:  # noqa: BLE001 - a refused transition is reported
                _LOG.error("Job %d could not move to %s: %s", active.job_id, target, exc)
            finally:
                conn.close()

    def _sync_recovery(self, directory: Path, report: Any) -> None:
        """Record recovery results against the matching recording row, if any."""
        relative = f"{directory.parent.name}/{directory.name}"
        with self._db_lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id, status FROM recordings WHERE relative_dir = ?", (relative,)
                ).fetchone()
                if row is None:
                    return
                recovered = report.chunks_recovered
                quarantined = report.chunks_quarantined
                with maybe_transaction(conn):
                    conn.execute(
                        "UPDATE recordings SET recovered_chunks = recovered_chunks + ?,"
                        " quarantined_chunks = quarantined_chunks + ?,"
                        " recovery_notes = ?, updated_at = ? WHERE id = ?",
                        (
                            recovered,
                            quarantined,
                            json.dumps(report.to_dict())[:2000],
                            utc_now_iso(),
                            int(row["id"]),
                        ),
                    )
                    record_event(
                        conn,
                        category="RECORDING",
                        action="recovery.completed" if report.ok else "recovery.failed",
                        entity_type="recording",
                        entity_id=int(row["id"]),
                        detail={
                            "chunks_recovered": recovered,
                            "chunks_quarantined": quarantined,
                            "frames_recovered": report.frames_recovered,
                            "bytes_discarded": report.bytes_discarded,
                        },
                    )
            finally:
                conn.close()
