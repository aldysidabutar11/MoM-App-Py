"""Python-side enrollment capture: bounded buffers, cleanup, and no raw audio anywhere.

The architectural point being defended here is that **the browser never touches the
microphone**. Voice capture runs inside the Python process through the Phase 2
backend, so a biometric sample never becomes a base64 blob in a JSON body, never
enters browser memory, and never lands on disk. See ADR-0012.

These tests therefore assert two different kinds of thing:

* the controller's own contract -- bounded buffer, hard ceilings, cleanup on every
  terminal path, no stream or thread left behind;
* the *absence* of the browser-capture route -- no `getUserMedia`, no upload
  endpoint, no raw PCM crossing HTTP.

No physical microphone is opened: the Phase 2 `FakeAudioBackend` supplies audio.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from mom_igd.audio.devices import DeviceDiscoveryService
from mom_igd.audio.fake_backend import FakeAudioBackend, SilenceSource, SineSource
from mom_igd.audio.service import RecordingService, RecordingServiceError
from mom_igd.config import AppConfig
from mom_igd.db import initialize_database
from mom_igd.db.connection import connect
from mom_igd.enrollment.capture import MAX_SAMPLE_BYTES, EnrollmentCaptureController
from mom_igd.enrollment.consent import ConfirmationMethod, ConsentService
from mom_igd.enrollment.fake_provider import FakeSpeakerEmbeddingProvider
from mom_igd.enrollment.keys import FakeKeyProtector
from mom_igd.enrollment.participants import ParticipantService
from mom_igd.enrollment.service import (
    MAX_SAMPLE_SECONDS,
    EnrollmentError,
    EnrollmentService,
    ReasonCode,
)

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
    backend = FakeAudioBackend(blocksize=1_200, source=SineSource(level_dbfs=-20.0))
    # Realtime so the controller's own polling loop is exercised rather than being
    # handed a pre-filled buffer.
    backend.realtime = True
    backend.speed = 200.0
    return backend


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
def enrollment(audio_config, paths, recording, tmp_path):
    service = EnrollmentService(
        audio_config,
        paths,
        recording_service=recording,
        provider=FakeSpeakerEmbeddingProvider(),
        key_protector=FakeKeyProtector(paths.keys_dir),
    )
    yield service
    service.shutdown()


@pytest.fixture
def controller(recording, enrollment) -> EnrollmentCaptureController:
    made = EnrollmentCaptureController(
        recording_service=recording, enrollment_service=enrollment
    )
    yield made
    made.shutdown()


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
    devices = recording.list_devices()["devices"]
    mono = next(d for d in devices if d["max_input_channels"] == 1)
    recording.select_device(mono["fingerprint"])
    backend.source = SineSource(frequency_hz=440.0, level_dbfs=-18.0)
    result = recording.calibrate(seconds=0.4)
    assert result.verdict.value == "GOOD", result.verdict
    backend.source = SineSource(level_dbfs=-20.0)


def _ready_participant(factory, name: str = "Budi") -> str:
    people = ParticipantService(factory)
    consent = ConsentService(factory)
    person = people.create(display_name=name)
    consent.grant(
        _pid(factory, person.uuid),
        confirmation_method=ConfirmationMethod.PARTICIPANT_CONFIRMED_ON_DEVICE,
    )
    return person.uuid


def _live_threads() -> int:
    return threading.active_count()


def _flatten(value: Any) -> list[Any]:
    """Every leaf value inside a nested dict/list, for leak assertions."""
    if isinstance(value, dict):
        return [leaf for v in value.values() for leaf in _flatten(v)]
    if isinstance(value, (list, tuple)):
        return [leaf for v in value for leaf in _flatten(v)]
    return [value]


# ================================================= no browser audio, anywhere


def test_the_static_ui_never_captures_audio() -> None:
    """The whole reason this controller exists (ADR-0012).

    Comments are stripped first. The shipped page *documents* its own prohibition --
    "there is no getUserMedia, no AudioContext" -- so a bare substring search would
    flag the sentence forbidding the thing as an instance of the thing. The
    exhaustive version of this check lives in `tests/test_participants_ui.py`.
    """
    import re

    from mom_igd.api.app import WEB_DIR

    for name, comment in (("app.js", "block"), ("index.html", "html")):
        text = (Path(WEB_DIR) / name).read_text(encoding="utf-8")
        if comment == "block":
            text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
            text = re.sub(r"//.*$", "", text, flags=re.M)
        else:
            text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        for forbidden in (
            "getUserMedia",
            "MediaRecorder",
            "AudioContext",
            "webkitAudioContext",
            "createMediaStreamSource",
            "navigator.mediaDevices",
            "ScriptProcessor",
            "AudioWorklet",
        ):
            assert forbidden not in text, f"{name} references {forbidden}"


def test_no_enrollment_route_accepts_audio() -> None:
    """No multipart, no octet-stream, no base64 PCM field."""
    import inspect

    from mom_igd.api import enrollment_routes

    source = inspect.getsource(enrollment_routes)
    for forbidden in ("UploadFile", "multipart", "File(", "octet-stream", "b64decode"):
        assert forbidden not in source, f"enrollment_routes references {forbidden}"


def test_pcm_is_handed_over_in_process_only() -> None:
    """`add_sample` is called by the controller, never from a request body."""
    import inspect

    from mom_igd.api import enrollment_routes

    source = inspect.getsource(enrollment_routes)
    assert "add_sample" not in source, (
        "a route calls add_sample directly; PCM would then have to arrive over HTTP"
    )
    assert "capture.capture_sample" in source


# ======================================================= readiness gates first


def test_capturing_without_a_session_is_refused_and_opens_nothing(
    controller, backend
) -> None:
    """The session is where consent and model availability were verified."""
    before = backend.open_calls
    with pytest.raises(EnrollmentError) as excinfo:
        controller.capture_sample(seconds=2.0)
    assert excinfo.value.reason is ReasonCode.INTERNAL_ERROR
    assert "No enrollment is in progress" in str(excinfo.value)
    assert backend.open_calls == before, "the device was opened with no session"


def test_a_production_service_without_a_model_opens_no_device(
    audio_config, paths, recording, backend, factory
) -> None:
    """Model readiness is checked before the microphone, not after."""
    _calibrate(recording, backend)
    participant = _ready_participant(factory)
    production = EnrollmentService(audio_config, paths, recording_service=recording)
    made = EnrollmentCaptureController(
        recording_service=recording, enrollment_service=production
    )
    before = backend.open_calls
    with pytest.raises(EnrollmentError) as excinfo:
        production.start(participant)
    assert excinfo.value.reason is ReasonCode.MODEL_UNAVAILABLE
    # And the controller refuses too, because there is no session.
    with pytest.raises(EnrollmentError):
        made.capture_sample(seconds=2.0)
    assert backend.open_calls == before
    made.shutdown()


# ================================================================== capture


def test_a_sample_is_captured_and_accepted(
    controller, enrollment, recording, backend, factory
) -> None:
    _calibrate(recording, backend)
    participant = _ready_participant(factory)
    enrollment.start(participant)
    opens_before = backend.open_calls

    result = controller.capture_sample(seconds=7.0)

    assert result["sample_accepted"] is True, result["last_sample"]
    assert result["samples_accepted"] == 1
    assert backend.open_calls == opens_before + 1
    # The stream was closed and the buffer released.
    assert controller.capturing is False
    assert backend.open_streams == [], "the capture stream was left open"
    assert result["buffered_bytes"] > 0  # the service holds it until embedding


def test_the_returned_payload_carries_metadata_not_audio(
    controller, enrollment, recording, backend, factory
) -> None:
    _calibrate(recording, backend)
    enrollment.start(_ready_participant(factory))
    result = controller.capture_sample(seconds=7.0)
    blob = repr(result).lower()
    for forbidden in ("centroid", "dispersion", "ciphertext", "nonce", "material"):
        assert forbidden not in blob, f"the capture result leaked {forbidden!r}"
    # No bytes object anywhere in the payload -- that is what audio would look like.
    assert not any(isinstance(v, (bytes, bytearray)) for v in _flatten(result))
    sample = result["last_sample"]
    assert "levels" in sample and "gates" in sample
    assert sample["seconds"] > 0


def test_a_rejected_sample_releases_its_audio(
    controller, enrollment, recording, backend, factory
) -> None:
    _calibrate(recording, backend)
    enrollment.start(_ready_participant(factory))
    backend.source = SilenceSource()
    result = controller.capture_sample(seconds=7.0)
    assert result["sample_accepted"] is False
    assert result["buffered_bytes"] == 0, "rejected audio must be released at once"
    assert controller.capturing is False


def test_a_concurrent_second_capture_is_refused_not_queued(
    controller, enrollment, recording, backend, factory
) -> None:
    """Refused, not serialised.

    Queuing behind a seven-second capture would eventually record an extra sample the
    operator never asked for -- and the wizard would look frozen while it waited. An
    earlier version held a lock for the whole capture and did exactly that.
    """
    _calibrate(recording, backend)
    enrollment.start(_ready_participant(factory))

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def _go() -> None:
        barrier.wait()
        try:
            # 7 s, comfortably over the 6 s minimum, so the winner is *accepted*
            # rather than quality-rejected for being too short.
            result = controller.capture_sample(seconds=7.0)
            outcomes.append("accepted" if result["sample_accepted"] else "rejected")
        except EnrollmentError as exc:
            outcomes.append(exc.reason.value)

    threads = [threading.Thread(target=_go) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert outcomes.count("accepted") == 1, outcomes
    assert ReasonCode.CAPTURE_LOCK_HELD.value in outcomes, outcomes
    assert backend.open_streams == []
    # Exactly one sample landed, not two.
    assert enrollment.status()["samples_accepted"] == 1


@pytest.mark.parametrize("seconds", [0.5, 0.0, -1.0, MAX_SAMPLE_SECONDS + 1, 999.0])
def test_an_out_of_range_duration_is_refused_before_opening(
    controller, enrollment, recording, backend, factory, seconds: float
) -> None:
    _calibrate(recording, backend)
    enrollment.start(_ready_participant(factory))
    before = backend.open_calls
    with pytest.raises(EnrollmentError):
        controller.capture_sample(seconds=seconds)
    assert backend.open_calls == before


def test_the_byte_ceiling_is_derived_from_the_duration_ceiling() -> None:
    """Enforced in bytes so a misreported sample rate cannot exceed it."""
    assert MAX_SAMPLE_BYTES >= 48_000 * 2 * 2 * MAX_SAMPLE_SECONDS
    # And it is not absurdly generous: one sample, not a whole meeting.
    assert MAX_SAMPLE_BYTES < 48_000 * 2 * 2 * 60


def test_a_buffer_overflow_discards_the_sample_safely(
    controller, enrollment, recording, backend, factory, monkeypatch
) -> None:
    """An overflow must reject the sample, not truncate it silently."""
    _calibrate(recording, backend)
    enrollment.start(_ready_participant(factory))

    from mom_igd.enrollment import capture as capture_module

    # Squeeze the ceiling so the fake source overruns it quickly.
    monkeypatch.setattr(capture_module, "MAX_SAMPLE_BYTES", 4_096)
    with pytest.raises(EnrollmentError) as excinfo:
        controller.capture_sample(seconds=5.0)
    assert excinfo.value.reason is ReasonCode.BUFFER_LIMIT_EXCEEDED
    assert controller.capturing is False
    assert backend.open_streams == [], "overflow left the stream open"


def test_a_device_change_between_start_and_capture_is_refused(
    controller, enrollment, recording, backend, factory, monkeypatch
) -> None:
    _calibrate(recording, backend)
    enrollment.start(_ready_participant(factory))

    real = recording.resolve_device

    class _Other:
        fingerprint = "a" * 32
        index = 0
        name = "Different microphone"

    monkeypatch.setattr(recording, "resolve_device", lambda: (_Other(), None))
    with pytest.raises(EnrollmentError) as excinfo:
        controller.capture_sample(seconds=3.0)
    assert excinfo.value.reason is ReasonCode.DEVICE_CHANGED
    monkeypatch.setattr(recording, "resolve_device", real)


def test_a_disconnected_device_is_a_controlled_failure(
    controller, enrollment, recording, backend, factory, monkeypatch
) -> None:
    _calibrate(recording, backend)
    enrollment.start(_ready_participant(factory))
    monkeypatch.setattr(
        recording, "resolve_device", lambda: (None, "the microphone was unplugged")
    )
    with pytest.raises(EnrollmentError) as excinfo:
        controller.capture_sample(seconds=3.0)
    assert excinfo.value.reason is ReasonCode.DEVICE_DISCONNECTED
    assert controller.capturing is False


def test_a_stream_that_fails_to_open_leaves_nothing_behind(
    controller, enrollment, recording, backend, factory, monkeypatch
) -> None:
    _calibrate(recording, backend)
    enrollment.start(_ready_participant(factory))
    threads_before = _live_threads()

    def _boom(*args, **kwargs):
        raise OSError("PortAudio refused to open the device")

    monkeypatch.setattr(backend, "open_input_stream", _boom)
    with pytest.raises(EnrollmentError) as excinfo:
        controller.capture_sample(seconds=3.0)
    assert excinfo.value.reason is ReasonCode.DEVICE_DISCONNECTED
    assert controller.capturing is False
    assert backend.open_streams == []
    assert _live_threads() <= threads_before


def test_a_silent_device_times_out_rather_than_hanging(
    controller, enrollment, recording, backend, factory
) -> None:
    """A stream that never delivers audio must fail, not block forever."""
    _calibrate(recording, backend)
    enrollment.start(_ready_participant(factory))
    # Manual mode with nothing pumping it: the stream opens but produces nothing.
    backend.realtime = False
    with pytest.raises(EnrollmentError) as excinfo:
        controller.capture_sample(seconds=1.0)
    assert excinfo.value.reason is ReasonCode.DEVICE_DISCONNECTED
    assert controller.capturing is False
    assert backend.open_streams == []


# ================================================================= cleanup


def test_abort_is_idempotent_and_closes_everything(
    controller, enrollment, recording, backend, factory
) -> None:
    _calibrate(recording, backend)
    enrollment.start(_ready_participant(factory))
    controller.capture_sample(seconds=5.0)
    first = controller.abort()
    second = controller.abort()
    third = controller.abort()
    assert first["aborted"] is False  # nothing was in flight
    assert second["aborted"] is False and third["aborted"] is False
    assert controller.capturing is False
    assert backend.open_streams == []


def test_shutdown_is_safe_with_nothing_in_flight(controller) -> None:
    controller.shutdown()
    controller.shutdown()
    assert controller.capturing is False


def test_no_raw_audio_artifact_is_written(
    controller, enrollment, recording, backend, factory, paths
) -> None:
    _calibrate(recording, backend)
    enrollment.start(_ready_participant(factory))
    for _ in range(3):
        controller.capture_sample(seconds=7.0)
    controller.abort()

    for pattern in ("*.wav", "*.pcm", "*.raw", "*.part", "*.tmp", "*.pcm.part"):
        stray = [
            p
            for p in paths.root.rglob(pattern)
            if "recordings" not in p.parts  # Phase 2 recordings are legitimate
        ]
        assert stray == [], f"enrollment left {pattern}: {stray}"


def test_no_thread_is_leaked_across_many_captures(
    controller, enrollment, recording, backend, factory
) -> None:
    _calibrate(recording, backend)
    enrollment.start(_ready_participant(factory))
    before = _live_threads()
    for _ in range(5):
        controller.capture_sample(seconds=7.0)
    assert _live_threads() <= before + 1, "capture leaked a thread"
    assert backend.open_streams == []


# ============================================ lock interaction, one more time


def test_a_live_meeting_recording_blocks_enrollment_start(
    enrollment, recording, backend, factory
) -> None:
    _calibrate(recording, backend)
    participant = _ready_participant(factory)
    recording.start(meeting_title="Rapat berjalan")
    try:
        with pytest.raises(EnrollmentError) as excinfo:
            enrollment.start(participant)
        assert excinfo.value.reason is ReasonCode.CAPTURE_LOCK_HELD
    finally:
        recording.stop()


def test_a_live_enrollment_blocks_a_meeting_recording(
    enrollment, recording, backend, factory
) -> None:
    _calibrate(recording, backend)
    enrollment.start(_ready_participant(factory))
    with pytest.raises(RecordingServiceError):
        recording.start(meeting_title="Tidak boleh")
    assert recording.status()["recording_active"] is False


def test_the_controller_does_not_acquire_the_lock_a_second_time(
    controller, enrollment, recording, backend, factory
) -> None:
    """Only the service owns the shared lock; a second acquire would deadlock or fail.

    The controller opens the device but must not touch the lock: `EnrollmentService`
    already holds it for the whole session, and `SingleRecordingLock.acquire` uses
    `O_EXCL` -- a second acquire in the same process would raise, aborting a healthy
    enrollment.
    """
    import inspect

    from mom_igd.enrollment import capture as capture_module

    source = inspect.getsource(capture_module)
    assert ".acquire(" not in source, "the controller acquires the capture lock"
    assert "_capture_lock" not in source

    _calibrate(recording, backend)
    enrollment.start(_ready_participant(factory))
    holder_before = enrollment._capture_lock.read_live_holder()  # noqa: SLF001
    controller.capture_sample(seconds=5.0)
    holder_after = enrollment._capture_lock.read_live_holder()  # noqa: SLF001
    assert holder_before == holder_after, "the lock holder changed during capture"
    assert holder_after["recording_uuid"].startswith("enrollment:")


def test_capture_survives_a_cancel_from_another_thread(
    controller, enrollment, recording, backend, factory
) -> None:
    """Cancel takes the service lock; capture takes the controller lock.

    Both orders are controller-then-service in the code, so a concurrent cancel must
    not deadlock. This asserts the process does not hang, and that the enrollment
    ends up terminal either way.
    """
    _calibrate(recording, backend)
    enrollment.start(_ready_participant(factory))

    done = threading.Event()

    def _capture() -> None:
        try:
            controller.capture_sample(seconds=6.0)
        except EnrollmentError:
            pass
        finally:
            done.set()

    worker = threading.Thread(target=_capture)
    worker.start()
    enrollment.cancel(reason="operator cancelled mid-sample")
    assert done.wait(timeout=45), "capture deadlocked against cancel"
    worker.join(timeout=10)
    controller.abort()
    assert enrollment.status()["active"] is False
    assert backend.open_streams == []


# =============================================== fake end-to-end via controller


def test_the_whole_flow_through_the_controller(
    controller, enrollment, recording, backend, factory, paths
) -> None:
    """create -> consent -> start -> five Python-side samples -> finalise -> verify.

    **Test evidence only.** The audio comes from a deterministic fake backend and the
    embedding from an injected fake provider, so this proves the pipeline is wired
    correctly -- not that the product can identify anybody.
    """
    from mom_igd.audit import verify_chain
    from mom_igd.enrollment.fake_provider import StableSpeakerFakeProvider
    from mom_igd.enrollment.store import VoiceprintStatus

    # The default fake derives its vector purely from the audio bytes, so five
    # genuinely different recordings give unrelated vectors and the consistency gate
    # correctly rejects them. Feeding byte-identical audio to dodge that would test
    # nothing, so use the variant that models "same voice -> similar vector", which is
    # the property a real model has.
    enrollment._injected_provider = StableSpeakerFakeProvider()  # noqa: SLF001

    _calibrate(recording, backend)
    participant = _ready_participant(factory, "Budi Santoso")

    ready = enrollment.readiness(participant)
    assert ready["can_start"] is True, ready["blockers"]

    enrollment.start(participant)
    for index in range(5):
        result = controller.capture_sample(seconds=7.0)
        assert result["sample_accepted"] is True, (index, result["last_sample"])
    assert enrollment.status()["samples_accepted"] == 5

    outcome = enrollment.finalize()
    voiceprint = outcome["voiceprint"]
    assert voiceprint is not None
    assert voiceprint["verified"] is True
    assert voiceprint["status"] == VoiceprintStatus.DEVELOPMENT_ONLY.value
    assert voiceprint["production_eligible"] is False, (
        "a non-USB device must never produce a production-eligible template"
    )

    # No raw audio, and no participant name, anywhere on disk.
    envelope = paths.voiceprints_dir / f"{voiceprint['voiceprint_uuid']}.vpx"
    assert envelope.is_file()
    assert "Budi" not in str(envelope)
    assert "centroid" not in envelope.read_text(encoding="utf-8")
    for pattern in ("*.wav", "*.pcm", "*.raw"):
        assert [
            p for p in paths.root.rglob(pattern) if "recordings" not in p.parts
        ] == []

    # Everything is released.
    assert controller.capturing is False
    assert backend.open_streams == []
    assert not enrollment._capture_lock.path.exists()  # noqa: SLF001

    conn = factory()
    try:
        verify_chain(conn)
    finally:
        conn.close()
