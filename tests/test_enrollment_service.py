"""The enrollment state machine, capture mutual exclusion, and the fake end-to-end flow.

Everything here uses a temporary data root, the Phase 2 `FakeAudioBackend`, and an
**injected** fake embedding provider. No physical microphone is opened, no real
DPAPI key is created, and the real data root is never touched.

The properties that matter most:

* **`meeting recording XOR enrollment`**, enforced by the same cross-process lock
  file Phase 2 uses -- not an in-process flag.
* **Consent is re-checked immediately before encryption**, so withdrawing it during
  a one-minute enrollment prevents the template from ever being stored.
* **No partial voiceprint is ever active**, on any failure path.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid as uuid_module
from pathlib import Path
from typing import Any

import pytest

from mom_igd.audio.backend import CaptureProfile, SampleFormat
from mom_igd.audio.devices import DeviceDiscoveryService
from mom_igd.audio.fake_backend import FakeAudioBackend, SilenceSource, SineSource
from mom_igd.audio.service import RecordingService
from mom_igd.config import AppConfig
from mom_igd.db import initialize_database
from mom_igd.db.connection import connect
from mom_igd.enrollment.consent import ConfirmationMethod, ConsentService
from mom_igd.enrollment.fake_provider import (
    BrokenFakeProvider,
    DriftingFakeProvider,
    FakeSpeakerEmbeddingProvider,
    fake_model_spec,
)
from mom_igd.enrollment.keys import FakeKeyProtector
from mom_igd.enrollment.participants import ParticipantService
from mom_igd.enrollment.provider import ModelUnavailableError
from mom_igd.enrollment.service import (
    ALLOWED_TRANSITIONS,
    MAX_TOTAL_CAPTURE_BYTES,
    EnrollmentError,
    EnrollmentService,
    EnrollmentState,
    InvalidEnrollmentTransition,
    ReasonCode,
)
from mom_igd.enrollment.store import VoiceprintStatus, VoiceprintStore

SR = 48_000


@pytest.fixture
def audio_config(config: AppConfig) -> AppConfig:
    payload = config.model_dump()
    payload["audio"] = {
        **config.audio.model_dump(),
        "min_free_disk_gb": 0.0,
        "low_disk_abort_gb": 0.0,
    }
    return AppConfig.model_validate(payload)


@pytest.fixture
def db_path(audio_config: AppConfig, paths) -> Path:
    initialize_database(
        paths.database_path(audio_config.database.filename),
        busy_timeout_ms=audio_config.database.busy_timeout_ms,
        app_version=audio_config.app_version,
    )
    return paths.database_path(audio_config.database.filename)


@pytest.fixture
def factory(db_path: Path, audio_config: AppConfig):
    def _connect() -> sqlite3.Connection:
        return connect(db_path, busy_timeout_ms=audio_config.database.busy_timeout_ms)

    return _connect


@pytest.fixture
def backend() -> FakeAudioBackend:
    return FakeAudioBackend(blocksize=1_200, source=SineSource(level_dbfs=-20.0))


@pytest.fixture
def recording(audio_config: AppConfig, paths, backend: FakeAudioBackend, db_path):
    service = RecordingService(
        audio_config,
        paths,
        backend=backend,
        discovery=DeviceDiscoveryService(backend, endpoint_provider=lambda: []),
    )
    yield service
    try:
        service.abandon("test teardown")
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def provider() -> FakeSpeakerEmbeddingProvider:
    return FakeSpeakerEmbeddingProvider()


@pytest.fixture
def enrollment(audio_config, paths, recording, provider, tmp_path):
    service = EnrollmentService(
        audio_config,
        paths,
        recording_service=recording,
        provider=provider,
        key_protector=FakeKeyProtector(paths.keys_dir),
    )
    yield service
    service.shutdown()


@pytest.fixture
def people(factory) -> ParticipantService:
    return ParticipantService(factory)


@pytest.fixture
def consent(factory) -> ConsentService:
    return ConsentService(factory)


def _pid(factory, participant_uuid: str) -> int:
    conn = factory()
    try:
        return int(
            conn.execute(
                "SELECT id FROM participants WHERE uuid = ?", (participant_uuid,)
            ).fetchone()["id"]
        )
    finally:
        conn.close()


def _calibrate(recording: RecordingService, backend: FakeAudioBackend) -> None:
    """Record real Phase 2 calibration evidence with the fake backend."""
    devices = recording.list_devices()["devices"]
    mono = next(d for d in devices if d["max_input_channels"] == 1)
    recording.select_device(mono["fingerprint"])
    backend.source = SineSource(frequency_hz=440.0, level_dbfs=-18.0)
    backend.realtime = True
    backend.speed = 80.0
    result = recording.calibrate(seconds=0.4)
    assert result.verdict.value == "GOOD", result.verdict
    backend.realtime = False
    backend.source = SineSource(level_dbfs=-20.0)


def _enrolled_participant(people, consent, factory, name: str = "Budi") -> str:
    person = people.create(display_name=name)
    consent.grant(
        _pid(factory, person.uuid),
        confirmation_method=ConfirmationMethod.PARTICIPANT_CONFIRMED_ON_DEVICE,
    )
    return person.uuid


def _sample_pcm(seconds: float = 10.0, source=None, channels: int = 1) -> bytes:
    profile = CaptureProfile(
        sample_rate=SR,
        channels=channels,
        sample_format=SampleFormat.INT16,
        chunk_seconds=30,
    )
    return (source or SineSource(level_dbfs=-20.0)).read(0, int(SR * seconds), profile)


def _capture_all(service: EnrollmentService, count: int = 5) -> None:
    for _ in range(count):
        result = service.add_sample(_sample_pcm())
        assert result["sample_accepted"] is True, result["last_sample"]


# ============================================================ state machine


def test_the_state_names_match_the_database_constraint(factory) -> None:
    """The schema is the source of truth; drift would break every UPDATE."""
    conn = factory()
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'enrollment_sessions'"
        ).fetchone()["sql"]
    finally:
        conn.close()
    for state in EnrollmentState:
        assert f"'{state.value}'" in sql, f"{state.value} is not in the CHECK constraint"


def test_terminal_states_have_no_successor() -> None:
    for state in EnrollmentState:
        if state.terminal:
            assert ALLOWED_TRANSITIONS[state] == frozenset(), state


def test_every_state_appears_in_the_transition_table() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(EnrollmentState)


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (EnrollmentState.CREATED, EnrollmentState.COMPLETED),
        (EnrollmentState.CREATED, EnrollmentState.CAPTURING),
        (EnrollmentState.READY, EnrollmentState.COMPLETED),
        (EnrollmentState.READY, EnrollmentState.EMBEDDING),
        (EnrollmentState.CAPTURING, EnrollmentState.COMPLETED),
        (EnrollmentState.CAPTURING, EnrollmentState.ENCRYPTING),
        (EnrollmentState.VALIDATING, EnrollmentState.COMPLETED),
        (EnrollmentState.EMBEDDING, EnrollmentState.COMPLETED),
        (EnrollmentState.ENCRYPTING, EnrollmentState.CAPTURING),
        (EnrollmentState.ENCRYPTING, EnrollmentState.EMBEDDING),
        (EnrollmentState.COMPLETED, EnrollmentState.CAPTURING),
        (EnrollmentState.CANCELLED, EnrollmentState.READY),
        (EnrollmentState.REJECTED, EnrollmentState.CAPTURING),
        (EnrollmentState.FAILED, EnrollmentState.READY),
    ],
)
def test_illegal_transitions_are_refused(current, requested) -> None:
    assert requested not in ALLOWED_TRANSITIONS[current]


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (EnrollmentState.CREATED, EnrollmentState.READY),
        (EnrollmentState.CREATED, EnrollmentState.CONSENT_REQUIRED),
        (EnrollmentState.CONSENT_REQUIRED, EnrollmentState.READY),
        (EnrollmentState.READY, EnrollmentState.CAPTURING),
        (EnrollmentState.CAPTURING, EnrollmentState.VALIDATING),
        (EnrollmentState.VALIDATING, EnrollmentState.CAPTURING),
        # finalise() runs from CAPTURING once enough samples are accepted.
        (EnrollmentState.CAPTURING, EnrollmentState.EMBEDDING),
        (EnrollmentState.EMBEDDING, EnrollmentState.ENCRYPTING),
        (EnrollmentState.ENCRYPTING, EnrollmentState.COMPLETED),
    ],
)
def test_the_happy_path_transitions_are_permitted(current, requested) -> None:
    assert requested in ALLOWED_TRANSITIONS[current]


@pytest.mark.parametrize("state", list(EnrollmentState))
def test_every_non_terminal_state_can_reach_a_terminal_one(state) -> None:
    """A session must always be abandonable; otherwise it wedges the capture lock."""
    if state.terminal:
        return
    successors = ALLOWED_TRANSITIONS[state]
    assert any(s.terminal for s in successors), f"{state.value} cannot terminate"


def test_encrypting_cannot_return_to_capturing() -> None:
    """Once sealing starts the audio buffer is gone; there is nothing to re-validate."""
    assert EnrollmentState.CAPTURING not in ALLOWED_TRANSITIONS[EnrollmentState.ENCRYPTING]


# ================================================================ readiness


def test_readiness_reports_model_unavailable_without_a_provider(
    audio_config, paths, recording, backend, people, consent, factory
) -> None:
    """Production has no model, so readiness must say so -- and block Start."""
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    service = EnrollmentService(
        audio_config, paths, recording_service=recording
    )  # no injected provider
    ready = service.readiness(participant)
    assert ready["model"]["ready"] is False
    assert ready["can_start"] is False
    assert ReasonCode.MODEL_UNAVAILABLE.value in ready["blockers"]
    assert "registry" in ready["model"]["detail"].lower()


def test_readiness_opens_no_stream_and_creates_no_key(
    enrollment, recording, backend, people, consent, factory, paths
) -> None:
    _calibrate(recording, backend)
    opens_before = backend.open_calls
    participant = _enrolled_participant(people, consent, factory)
    enrollment.readiness(participant)
    assert backend.open_calls == opens_before, "readiness must not open the microphone"
    assert not (paths.keys_dir / "voiceprint_master.dpapi").exists()


def test_readiness_blocks_without_consent(
    enrollment, recording, backend, people
) -> None:
    _calibrate(recording, backend)
    person = people.create(display_name="Tanpa Consent")
    ready = enrollment.readiness(person.uuid)
    assert ready["can_start"] is False
    assert ReasonCode.CONSENT_MISSING.value in ready["blockers"]


def test_readiness_blocks_a_deactivated_participant(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    people.set_active(participant, active=False)
    ready = enrollment.readiness(participant)
    assert ReasonCode.PARTICIPANT_INACTIVE.value in ready["blockers"]


def test_readiness_blocks_without_calibration(
    enrollment, recording, backend, people, consent, factory
) -> None:
    devices = recording.list_devices()["devices"]
    recording.select_device(devices[0]["fingerprint"])
    participant = _enrolled_participant(people, consent, factory)
    ready = enrollment.readiness(participant)
    assert ReasonCode.CALIBRATION_INVALID.value in ready["blockers"]


# ==================================================================== start


def test_start_refuses_without_consent(
    enrollment, recording, backend, people
) -> None:
    _calibrate(recording, backend)
    person = people.create(display_name="Tanpa Consent")
    with pytest.raises(EnrollmentError) as excinfo:
        enrollment.start(person.uuid)
    assert excinfo.value.reason is ReasonCode.CONSENT_MISSING
    assert backend.open_calls == 1, "only calibration opened a stream"


def test_start_refuses_a_deactivated_participant(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    people.set_active(participant, active=False)
    with pytest.raises(EnrollmentError) as excinfo:
        enrollment.start(participant)
    assert excinfo.value.reason is ReasonCode.PARTICIPANT_INACTIVE


def test_start_refuses_before_opening_the_microphone_when_no_model_exists(
    audio_config, paths, recording, backend, people, consent, factory
) -> None:
    """Nobody should be asked to speak for a template that cannot be built."""
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    service = EnrollmentService(audio_config, paths, recording_service=recording)
    opens_before = backend.open_calls
    with pytest.raises(EnrollmentError) as excinfo:
        service.start(participant)
    assert excinfo.value.reason is ReasonCode.MODEL_UNAVAILABLE
    assert backend.open_calls == opens_before


def test_start_refuses_without_valid_calibration(
    enrollment, recording, backend, people, consent, factory
) -> None:
    devices = recording.list_devices()["devices"]
    recording.select_device(devices[0]["fingerprint"])
    participant = _enrolled_participant(people, consent, factory)
    with pytest.raises(EnrollmentError) as excinfo:
        enrollment.start(participant)
    assert excinfo.value.reason is ReasonCode.CALIBRATION_INVALID


def test_a_double_clicked_start_creates_one_session(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    first = enrollment.start(participant)
    second = enrollment.start(participant)
    assert first["session_uuid"] == second["session_uuid"]
    conn = factory()
    try:
        assert conn.execute(
            "SELECT count(*) AS n FROM enrollment_sessions"
        ).fetchone()["n"] == 1
    finally:
        conn.close()


def test_concurrent_starts_produce_one_session(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    results: list[Any] = []
    barrier = threading.Barrier(2)

    def _go() -> None:
        barrier.wait()
        try:
            results.append(enrollment.start(participant)["session_uuid"])
        except Exception as exc:  # noqa: BLE001
            results.append(type(exc).__name__)

    threads = [threading.Thread(target=_go) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    conn = factory()
    try:
        count = conn.execute("SELECT count(*) AS n FROM enrollment_sessions").fetchone()["n"]
    finally:
        conn.close()
    assert int(count) == 1, results


# ============================================== mutual exclusion with Phase 2


def test_enrollment_refuses_while_a_meeting_recording_is_active(
    enrollment, recording, backend, people, consent, factory
) -> None:
    """`meeting recording XOR enrollment`, enforced by the shared lock file."""
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    recording.start(meeting_title="Rapat berjalan")
    try:
        with pytest.raises(EnrollmentError) as excinfo:
            enrollment.start(participant)
        assert excinfo.value.reason is ReasonCode.CAPTURE_LOCK_HELD
    finally:
        recording.stop()


def test_a_meeting_recording_refuses_while_enrollment_holds_the_lock(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)
    from mom_igd.audio.service import RecordingServiceError

    # Phase 2 refuses through its own `single_recording` preflight check, which
    # names the enrollment holding the lock. Either refusal path is acceptable; what
    # matters is that the recording does not start.
    with pytest.raises(RecordingServiceError, match="still active|already in progress"):
        recording.start(meeting_title="Tidak boleh")
    assert recording.status()["recording_active"] is False


def test_a_stale_lock_from_a_dead_process_does_not_block_enrollment(
    enrollment, recording, backend, people, consent, factory
) -> None:
    """A lock left by a killed process must not wedge enrollment forever.

    That would be a worse failure than the one the lock prevents. The staleness
    check is delegated to the Phase 2 logic so both paths agree.
    """
    import json

    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    lock_path = enrollment._capture_lock.path  # noqa: SLF001
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # PID 1 does not correspond to a live Windows process this test could own.
    lock_path.write_text(
        json.dumps(
            {"pid": 999_999_999, "recording_uuid": "dead-process", "acquired_utc": "x"}
        ),
        encoding="utf-8",
    )
    started = enrollment.start(participant)
    assert started["state"] == EnrollmentState.READY.value
    holder = enrollment._capture_lock.read_holder()  # noqa: SLF001
    assert holder["recording_uuid"].startswith("enrollment:")


def test_a_live_lock_holder_is_respected_even_in_the_same_process(
    enrollment, recording, backend, people, consent, factory
) -> None:
    """`held` must not be used to decide this.

    The lock object is shared with RecordingService, so `held` is True whenever this
    process owns it -- including when it owns it for a meeting recording, which is
    exactly the case to refuse. An earlier version of this check used `held` and let
    enrollment start on top of a live recording.
    """
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    recording.start(meeting_title="Rapat berjalan")
    try:
        assert enrollment._capture_lock.held is True  # noqa: SLF001 - same object
        with pytest.raises(EnrollmentError) as excinfo:
            enrollment.start(participant)
        assert excinfo.value.reason is ReasonCode.CAPTURE_LOCK_HELD
    finally:
        recording.stop()


def test_enrollment_and_recording_share_one_lock_file(enrollment, recording) -> None:
    """Two different lock files would let both run at once."""
    assert enrollment._capture_lock is recording._lock  # noqa: SLF001
    assert enrollment._capture_lock.path.name == "recording.lock"  # noqa: SLF001


def test_the_lock_is_released_after_a_completed_enrollment(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)
    _capture_all(enrollment)
    enrollment.finalize()
    assert enrollment._capture_lock.held is False  # noqa: SLF001
    assert not enrollment._capture_lock.path.exists()  # noqa: SLF001
    # And a meeting recording can now start.
    recording.start(meeting_title="Setelah enrollment")
    recording.stop()


def test_the_lock_is_released_after_a_cancelled_enrollment(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)
    enrollment.cancel(reason="operator changed their mind")
    assert not enrollment._capture_lock.path.exists()  # noqa: SLF001


def test_the_lock_is_released_after_a_rejected_enrollment(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)
    _capture_all(enrollment, 5)
    # Force a quality rejection by swapping in drifting embeddings.
    enrollment._active.provider = DriftingFakeProvider()  # noqa: SLF001
    enrollment.finalize()
    assert not enrollment._capture_lock.path.exists()  # noqa: SLF001


# ================================================================= capture


def test_a_rejected_sample_is_discarded_and_recapturable(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)

    bad = enrollment.add_sample(_sample_pcm(seconds=10, source=SilenceSource()))
    assert bad["sample_accepted"] is False
    assert bad["samples_accepted"] == 0
    assert bad["buffered_bytes"] == 0, "rejected audio must be released at once"

    good = enrollment.add_sample(_sample_pcm())
    assert good["sample_accepted"] is True
    assert good["samples_accepted"] == 1


def test_the_buffer_ceiling_is_enforced_and_the_session_fails_safely(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)
    huge = b"\x00\x01" * (MAX_TOTAL_CAPTURE_BYTES // 2 + 1_000)
    with pytest.raises(EnrollmentError) as excinfo:
        enrollment.add_sample(huge)
    assert excinfo.value.reason is ReasonCode.BUFFER_LIMIT_EXCEEDED
    assert not enrollment._capture_lock.path.exists()  # noqa: SLF001


def test_a_device_change_mid_capture_abandons_the_session(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)
    enrollment.add_sample(_sample_pcm())
    with pytest.raises(EnrollmentError) as excinfo:
        enrollment.add_sample(_sample_pcm(), device_fingerprint="a" * 32)
    assert excinfo.value.reason is ReasonCode.DEVICE_CHANGED
    assert enrollment.status()["active"] is False


def test_adding_a_sample_without_a_session_is_refused(enrollment) -> None:
    with pytest.raises(EnrollmentError, match="No enrollment is in progress"):
        enrollment.add_sample(_sample_pcm())


def test_finalising_early_is_refused(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)
    _capture_all(enrollment, 3)
    with pytest.raises(EnrollmentError) as excinfo:
        enrollment.finalize()
    assert excinfo.value.reason is ReasonCode.QUALITY_REJECTED
    assert "3 of 5" in str(excinfo.value)


def test_no_raw_audio_file_is_ever_written(
    enrollment, recording, backend, people, consent, factory, paths
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)
    _capture_all(enrollment)
    enrollment.finalize()
    for pattern in ("*.wav", "*.pcm", "*.raw", "*.part", "*.tmp"):
        stray = [
            p
            for p in paths.root.rglob(pattern)
            # Phase 2 recordings are legitimate; enrollment must add none.
            if "recordings" not in p.parts
        ]
        assert stray == [], f"enrollment left {pattern} behind: {stray}"


# ================================================== cancel / retry semantics


def test_cancel_is_idempotent(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)
    first = enrollment.cancel()
    second = enrollment.cancel()
    third = enrollment.cancel()
    assert first["state"] == EnrollmentState.CANCELLED.value
    assert second["active"] is False and third["active"] is False


def test_cancel_without_a_session_is_safe(enrollment) -> None:
    assert enrollment.cancel()["active"] is False


def test_a_new_enrollment_can_start_after_a_cancel(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    first = enrollment.start(participant)
    enrollment.cancel()
    second = enrollment.start(participant)
    assert second["session_uuid"] != first["session_uuid"]


def test_shutdown_abandons_a_live_session_and_frees_the_lock(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)
    enrollment.shutdown()
    assert enrollment.status()["active"] is False
    assert not enrollment._capture_lock.path.exists()  # noqa: SLF001


# ================================ consent / participant change mid-enrollment


def test_consent_revoked_mid_enrollment_stores_nothing(
    enrollment, recording, backend, people, consent, factory, paths
) -> None:
    """The reason this re-check exists: a minute is long enough to change your mind."""
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)
    _capture_all(enrollment)

    consent.revoke(_pid(factory, participant), reason="changed mind during enrollment")

    with pytest.raises(EnrollmentError) as excinfo:
        enrollment.finalize()
    assert excinfo.value.reason is ReasonCode.CONSENT_REVOKED_DURING_ENROLLMENT

    conn = factory()
    try:
        assert conn.execute("SELECT count(*) AS n FROM voiceprints").fetchone()["n"] == 0
        state = conn.execute(
            "SELECT state, reason_code FROM enrollment_sessions"
        ).fetchone()
    finally:
        conn.close()
    assert state["state"] == EnrollmentState.CANCELLED.value
    assert state["reason_code"] == ReasonCode.CONSENT_REVOKED_DURING_ENROLLMENT.value
    assert list(paths.voiceprints_dir.glob("*.vpx")) == []


def test_participant_deactivated_mid_enrollment_stores_nothing(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)
    _capture_all(enrollment)
    people.set_active(participant, active=False)

    with pytest.raises(EnrollmentError) as excinfo:
        enrollment.finalize()
    assert excinfo.value.reason is ReasonCode.PARTICIPANT_DEACTIVATED_DURING_ENROLLMENT
    conn = factory()
    try:
        assert conn.execute("SELECT count(*) AS n FROM voiceprints").fetchone()["n"] == 0
    finally:
        conn.close()


def test_a_re_granted_consent_mid_enrollment_forces_a_new_session(
    enrollment, recording, backend, people, consent, factory
) -> None:
    """A different consent event means a different agreement to bind the template to."""
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)
    _capture_all(enrollment)
    participant_id = _pid(factory, participant)
    consent.revoke(participant_id)
    consent.grant(
        participant_id, confirmation_method=ConfirmationMethod.OPERATOR_CONFIRMED_IN_PERSON
    )
    with pytest.raises(EnrollmentError) as excinfo:
        enrollment.finalize()
    assert excinfo.value.reason is ReasonCode.CONSENT_REVOKED_DURING_ENROLLMENT


# ================================================== embedding / quality gates


def test_an_invalid_embedding_rejects_the_enrollment_and_stores_nothing(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)
    _capture_all(enrollment)
    enrollment._active.provider = BrokenFakeProvider(mode="nan")  # noqa: SLF001

    with pytest.raises(EnrollmentError) as excinfo:
        enrollment.finalize()
    assert excinfo.value.reason is ReasonCode.EMBEDDING_INVALID
    conn = factory()
    try:
        assert conn.execute("SELECT count(*) AS n FROM voiceprints").fetchone()["n"] == 0
    finally:
        conn.close()


def test_inconsistent_samples_reject_without_storing(
    enrollment, recording, backend, people, consent, factory, paths
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)
    _capture_all(enrollment)
    enrollment._active.provider = DriftingFakeProvider()  # noqa: SLF001

    result = enrollment.finalize()
    assert result["voiceprint"] is None
    assert result["quality"]["accepted"] is False
    conn = factory()
    try:
        assert conn.execute("SELECT count(*) AS n FROM voiceprints").fetchone()["n"] == 0
        assert conn.execute(
            "SELECT state FROM enrollment_sessions"
        ).fetchone()["state"] == EnrollmentState.REJECTED.value
    finally:
        conn.close()
    assert list(paths.voiceprints_dir.glob("*.vpx")) == []


# ======================================================== fake end-to-end flow


def test_the_whole_fake_enrollment_flow(
    enrollment, recording, backend, people, consent, factory, paths
) -> None:
    """create participant -> consent -> enrol -> verify -> revoke -> deleted.

    This is **test evidence only**. It uses a fake audio backend and an injected
    fake embedding provider, so it proves the pipeline is wired correctly -- not
    that the product can identify anybody.
    """
    from mom_igd.audit import verify_chain
    from mom_igd.enrollment.cipher import VoiceprintCipher

    _calibrate(recording, backend)

    # 1. Register and obtain consent.
    person = people.create(display_name="Budi Santoso", role="Ketua")
    participant_id = _pid(factory, person.uuid)
    consent.grant(
        participant_id,
        confirmation_method=ConfirmationMethod.PARTICIPANT_CONFIRMED_ON_DEVICE,
    )

    # 2. Readiness: everything green except the device is not USB.
    ready = enrollment.readiness(person.uuid)
    assert ready["can_start"] is True, ready["blockers"]
    assert ready["device"]["production_eligible_device"] is False

    # 3. Enrol five samples.
    started = enrollment.start(person.uuid)
    assert started["state"] == EnrollmentState.READY.value
    assert started["will_be_development_only"] is True
    _capture_all(enrollment, 5)
    assert enrollment.status()["samples_accepted"] == 5

    # 4. Finalise: embed, validate, seal, store, verify.
    result = enrollment.finalize()
    assert result["state"] == EnrollmentState.COMPLETED.value
    voiceprint = result["voiceprint"]
    assert voiceprint["verified"] is True
    assert voiceprint["status"] == VoiceprintStatus.DEVELOPMENT_ONLY.value
    assert voiceprint["production_eligible"] is False, (
        "a non-USB device must never yield a production-eligible template"
    )
    assert result["quality"]["accepted"] is True
    assert result["quality"]["min_pair_cosine"] >= 0.80

    # 5. The envelope exists, is named by UUID, and holds no plaintext.
    envelope = paths.voiceprints_dir / f"{voiceprint['voiceprint_uuid']}.vpx"
    assert envelope.is_file()
    assert "Budi" not in str(envelope), "a name must never reach the filesystem"
    text = envelope.read_text(encoding="utf-8")
    assert "centroid" not in text and "dispersion" not in text

    # 6. Independent verification through the store.
    store = VoiceprintStore(paths.voiceprints_dir, factory)
    cipher = VoiceprintCipher(
        FakeKeyProtector(paths.keys_dir).create_if_missing(created_utc="x")
    )
    outcome = store.verify(voiceprint["voiceprint_uuid"], cipher=cipher)
    assert outcome.ok is True, outcome.problems
    assert outcome.checks["authenticated"] is True

    # 7. Eligibility policy agrees.
    eligibility = enrollment.eligibility(person.uuid)
    assert eligibility["eligible_for_identification"] is True
    assert eligibility["production_eligible"] is False

    # 8. Revoke consent: the ciphertext must go.
    revocation = enrollment.revoke_consent_and_delete(person.uuid, reason="withdrawn")
    assert revocation["eligible"] is False
    assert revocation["deletion"]["fully_deleted"] is True
    assert not envelope.exists(), "the ciphertext must actually be deleted"

    # 9. Unusable from every angle afterwards.
    after = enrollment.eligibility(person.uuid)
    assert after["eligible_for_identification"] is False
    assert "CONSENT_NOT_ACTIVE" in after["reasons"]
    assert "NO_USABLE_VOICEPRINT" in after["reasons"]

    # 10. The audit chain is intact and carries no biometric payload.
    conn = factory()
    try:
        verify_chain(conn)
        rows = list(
            conn.execute("SELECT action, detail_json FROM audit_events ORDER BY id")
        )
    finally:
        conn.close()
    actions = [r["action"] for r in rows]
    for expected in (
        "PARTICIPANT_CREATED",
        "CONSENT_GRANTED",
        "ENROLLMENT_STARTED",
        "ENROLLMENT_COMPLETED",
        "VOICEPRINT_CREATED",
        "CONSENT_REVOKED",
        "VOICEPRINT_DELETED",
    ):
        assert expected in actions, f"missing audit event {expected}"
    blob = " ".join(str(r["detail_json"] or "") for r in rows).lower()
    for forbidden in ("centroid", "dispersion", "ciphertext", "nonce", "master"):
        assert forbidden not in blob, f"audit trail leaked {forbidden}"


def test_a_second_enrollment_supersedes_the_first(
    enrollment, recording, backend, people, consent, factory, paths
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)

    enrollment.start(participant)
    _capture_all(enrollment)
    first = enrollment.finalize()["voiceprint"]

    enrollment.start(participant)
    _capture_all(enrollment)
    second = enrollment.finalize()["voiceprint"]

    conn = factory()
    try:
        rows = {
            r["voiceprint_uuid"]: r["status"]
            for r in conn.execute("SELECT voiceprint_uuid, status FROM voiceprints")
        }
    finally:
        conn.close()
    assert rows[first["voiceprint_uuid"]] == VoiceprintStatus.SUPERSEDED.value
    assert rows[second["voiceprint_uuid"]] == VoiceprintStatus.DEVELOPMENT_ONLY.value
    # The superseded ciphertext is gone.
    assert not (paths.voiceprints_dir / f"{first['voiceprint_uuid']}.vpx").exists()


def test_a_re_grant_after_revocation_requires_a_fresh_enrollment(
    enrollment, recording, backend, people, consent, factory
) -> None:
    """Re-granting consent is permission to enrol again, not a revival."""
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)
    _capture_all(enrollment)
    old = enrollment.finalize()["voiceprint"]["voiceprint_uuid"]

    enrollment.revoke_consent_and_delete(participant)
    consent.grant(
        _pid(factory, participant),
        confirmation_method=ConfirmationMethod.OPERATOR_CONFIRMED_IN_PERSON,
    )

    # Consent is active again, but the old template stays dead.
    assert enrollment.eligibility(participant)["eligible_for_identification"] is False
    conn = factory()
    try:
        status = conn.execute(
            "SELECT status FROM voiceprints WHERE voiceprint_uuid = ?", (old,)
        ).fetchone()["status"]
    finally:
        conn.close()
    assert status == VoiceprintStatus.REVOKED.value

    # A new enrollment restores eligibility.
    enrollment.start(participant)
    _capture_all(enrollment)
    enrollment.finalize()
    assert enrollment.eligibility(participant)["eligible_for_identification"] is True


def test_revoking_during_a_live_enrollment_stops_it(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)
    _capture_all(enrollment, 2)
    enrollment.revoke_consent_and_delete(participant, reason="withdrawn mid-session")
    assert enrollment.status()["active"] is False
    assert not enrollment._capture_lock.path.exists()  # noqa: SLF001


# ============================================================ status hygiene


def test_status_leaks_no_audio_vector_or_key(
    enrollment, recording, backend, people, consent, factory
) -> None:
    _calibrate(recording, backend)
    participant = _enrolled_participant(people, consent, factory)
    enrollment.start(participant)
    _capture_all(enrollment, 2)
    blob = repr(enrollment.status()).lower()
    for forbidden in ("centroid", "pcm", "ciphertext", "nonce", "material", "key_id"):
        assert forbidden not in blob


def test_reason_codes_are_enumerated_not_free_text() -> None:
    """A raw exception string could carry a path or part of a payload."""
    for reason in ReasonCode:
        assert reason.value.isupper()
        assert " " not in reason.value


def test_an_injected_test_double_is_not_reachable_from_production_config(
    audio_config, paths, recording
) -> None:
    """No config key, env var or parameter may select the fake provider."""
    service = EnrollmentService(audio_config, paths, recording_service=recording)
    with pytest.raises(ModelUnavailableError):
        service._resolve_provider()  # noqa: SLF001
    assert "fake" not in repr(audio_config.model_dump()).lower()
