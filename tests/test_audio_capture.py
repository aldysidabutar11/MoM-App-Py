"""Phase 2 capture engine: queue, writer, manifest, session, recovery.

Every test here drives :class:`FakeAudioBackend`. No test opens PortAudio, needs a
microphone, or touches the real runtime data directory. Audio fixtures are
deterministic generated PCM -- never a recording of a human voice.

The strongest assertion in this file is byte-exactness: because
:class:`CounterSource` makes frame *i* a pure function of *i*, a test can
regenerate the entire expected stream and compare it to what was written. A
dropped, duplicated or reordered frame changes the bytes and is caught exactly
rather than statistically.
"""

from __future__ import annotations

import threading
import wave
from array import array
from pathlib import Path

import pytest

from mom_igd.audio.backend import (
    CallbackStatus,
    CaptureProfile,
    DeviceNotFoundError,
    DeviceTransport,
    SampleFormat,
    StreamError,
    UnsupportedProfileError,
)
from mom_igd.audio.devices import (
    DeviceDiscoveryService,
    DeviceSelection,
    device_fingerprint,
    match_windows_endpoints,
    resolve_transport,
    split_device_name,
    WindowsAudioEndpoint,
)
from mom_igd.audio.fake_backend import (
    ClippingSource,
    CounterSource,
    FakeAudioBackend,
    SilenceSource,
    SineSource,
    StereoActivitySource,
)
from mom_igd.audio.frame_queue import BoundedFrameQueue
from mom_igd.audio.manifest import (
    ChunkStatus,
    ManifestWriter,
    RecoveryStatus,
    chunk_filename,
    compute_chain_hash,
    read_manifest,
    verify_manifest,
    write_manifest_summary,
)
from mom_igd.audio.quality import LevelVerdict, QualityMeter, analyse_block, to_dbfs
from mom_igd.audio.recovery import find_partials, recover_recording, scan_recoverable
from mom_igd.audio.session import WRITER_THREAD_NAME, CaptureSession, SessionState
from mom_igd.audio.writer import ChunkWriter, WriterError, partial_path, write_partial_meta

# 8 kHz keeps chunk_seconds >= 10 while staying fast: 80_000 frames per chunk.
FAST_RATE = 8_000
CHUNK_SECONDS = 10


@pytest.fixture
def profile() -> CaptureProfile:
    return CaptureProfile(
        sample_rate=FAST_RATE, channels=2, chunk_seconds=CHUNK_SECONDS, blocksize=0
    )


@pytest.fixture
def mono_profile() -> CaptureProfile:
    return CaptureProfile(sample_rate=FAST_RATE, channels=1, chunk_seconds=CHUNK_SECONDS)


@pytest.fixture
def rec_dir(tmp_path: Path) -> Path:
    target = tmp_path / "recordings" / "meeting-uuid" / "recording-uuid"
    target.mkdir(parents=True)
    return target


@pytest.fixture
def backend() -> FakeAudioBackend:
    return FakeAudioBackend(blocksize=1000, source=CounterSource())


def _no_endpoints() -> list[WindowsAudioEndpoint]:
    return []


def _live_writer_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name == WRITER_THREAD_NAME]


def _pump_frames(
    session: CaptureSession,
    stream,
    target_frames: int,
    *,
    batch_blocks: int = 8,
    timeout: float = 30.0,
) -> None:
    """Deliver ``target_frames`` at a pace the writer can keep up with.

    ``FakeStream.pump()`` is synchronous, so pumping a whole chunk in one burst
    pushes audio in far faster than real time and legitimately overflows the
    5-second bounded queue -- the queue doing exactly its job. A real microphone
    delivers one block per block-duration, so tests that need a full chunk on disk
    pump in batches and wait for the writer between them.

    Fails if any audio is dropped: at this pace there is no reason for it to be.
    """
    import time

    deadline = time.monotonic() + timeout
    while stream.frames_produced < target_frames:
        stream.pump(batch_blocks)
        while session.frames_written < stream.frames_produced:
            assert time.monotonic() < deadline, (
                f"writer consumed {session.frames_written} of "
                f"{stream.frames_produced} frames within {timeout} s"
            )
            time.sleep(0.001)
    assert session.status()["queue"]["dropped_frames"] == 0, (
        "paced delivery must not drop audio"
    )


def _await_chunks(session: CaptureSession, count: int, timeout: float = 15.0) -> None:
    """Wait until ``count`` chunks are finalised on disk.

    Reaching a frame count is not the same as having the file: the writer
    increments its counter and only then builds the WAV, hashes it and renames it
    into place. A test that checks for the file the instant the counter hits the
    boundary is racing that finalisation.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.status()["chunks_finalised"] >= count:
            return
        time.sleep(0.002)
    raise AssertionError(
        f"only {session.status()['chunks_finalised']} of {count} chunk(s) were "
        f"finalised within {timeout} s"
    )


def _read_wav(path: Path) -> tuple[bytes, int, int, int]:
    with wave.open(str(path), "rb") as handle:
        return (
            handle.readframes(handle.getnframes()),
            handle.getnchannels(),
            handle.getframerate(),
            handle.getnframes(),
        )


# ===========================================================================
# Import safety: no hardware may be touched by importing anything
# ===========================================================================


def test_importing_the_audio_package_does_not_load_portaudio() -> None:
    import subprocess
    import sys

    code = (
        "import sys, mom_igd.audio, mom_igd.audio.sounddevice_backend, "
        "mom_igd.audio.session, mom_igd.audio.recovery;"
        "hw=[m for m in sys.modules if m.split('.')[0] in {'sounddevice','_sounddevice'}];"
        "print(','.join(sorted(hw)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "", f"importing loaded {result.stdout.strip()}"


def test_constructing_a_session_opens_no_stream(
    backend: FakeAudioBackend, profile: CaptureProfile, rec_dir: Path, make_session
) -> None:
    CaptureSession(backend, device_index=0, profile=profile, directory=rec_dir)
    assert backend.open_calls == 0, "the microphone must only open on start()"


# ===========================================================================
# Devices: identity, rejection, no silent fallback
# ===========================================================================


def test_enumeration_finds_the_usable_devices(backend: FakeAudioBackend) -> None:
    service = DeviceDiscoveryService(backend, endpoint_provider=_no_endpoints)
    usable = service.input_devices(refresh=True)
    assert [d.name for d in usable] == [
        "Fake Internal Microphone Array",
        "Fake USB Conference Mic",
        "Fake Narrowband Phone Mic",
    ]


def test_output_only_device_is_rejected(backend: FakeAudioBackend) -> None:
    service = DeviceDiscoveryService(backend, endpoint_provider=_no_endpoints)
    rejected = {d.name: d.rejection_reason for d in service.rejected_devices(refresh=True)}
    assert "Fake Speakers (output only)" in rejected
    assert "output-only" in rejected["Fake Speakers (output only)"]


def test_loopback_device_is_rejected() -> None:
    from mom_igd.audio.backend import RawDeviceInfo

    backend = FakeAudioBackend(
        devices=[
            RawDeviceInfo(
                index=0,
                name="Stereo Mix (Realtek)",
                host_api="Windows WDM-KS",
                max_input_channels=2,
                max_output_channels=0,
                default_sample_rate=48_000.0,
            )
        ]
    )
    service = DeviceDiscoveryService(backend, endpoint_provider=_no_endpoints)
    assert service.input_devices(refresh=True) == []
    assert "loopback" in service.rejected_devices()[0].rejection_reason


def test_virtual_aggregate_device_is_rejected() -> None:
    from mom_igd.audio.backend import RawDeviceInfo

    backend = FakeAudioBackend(
        devices=[
            RawDeviceInfo(
                index=0,
                name="Microsoft Sound Mapper - Input",
                host_api="MME",
                max_input_channels=2,
                max_output_channels=0,
                default_sample_rate=44_100.0,
            )
        ]
    )
    service = DeviceDiscoveryService(backend, endpoint_provider=_no_endpoints)
    assert service.input_devices(refresh=True) == []
    assert "virtual/aggregate" in service.rejected_devices()[0].rejection_reason


def test_fingerprint_excludes_the_portaudio_index() -> None:
    first = device_fingerprint("Windows WASAPI", "Mic", 2)
    assert first == device_fingerprint("windows wasapi", "  Mic  ", 2)
    assert first != device_fingerprint("MME", "Mic", 2)
    assert first != device_fingerprint("Windows WASAPI", "Mic", 1)


def test_fingerprint_survives_a_device_reindex(backend: FakeAudioBackend) -> None:
    service = DeviceDiscoveryService(backend, endpoint_provider=_no_endpoints)
    before = service.input_devices(refresh=True)
    selection = DeviceSelection.from_device(before[0])
    backend.reindex(offset=5)

    resolved = service.resolve_selection(selection)
    assert resolved.fingerprint == selection.fingerprint
    assert resolved.index != selection.last_known_index, "index moved, identity held"


def test_missing_device_raises_instead_of_falling_back(backend: FakeAudioBackend) -> None:
    service = DeviceDiscoveryService(backend, endpoint_provider=_no_endpoints)
    devices = service.input_devices(refresh=True)
    selection = DeviceSelection.from_device(devices[0])
    backend.remove_device(devices[0].index)

    with pytest.raises(DeviceNotFoundError) as excinfo:
        service.resolve_selection(selection)
    message = str(excinfo.value)
    assert selection.name in message
    assert "will not start on a different device" in message


def test_unsupported_profile_is_rejected(backend: FakeAudioBackend, profile: CaptureProfile) -> None:
    service = DeviceDiscoveryService(backend, endpoint_provider=_no_endpoints)
    usb = next(d for d in service.input_devices(refresh=True) if "USB" in d.name)
    assert usb.max_input_channels == 1
    with pytest.raises(UnsupportedProfileError):
        service.validate_for_capture(usb, profile)  # stereo on a mono device


def test_recommended_profile_preserves_native_shape(backend: FakeAudioBackend) -> None:
    service = DeviceDiscoveryService(backend, endpoint_provider=_no_endpoints)
    devices = {d.name: d for d in service.input_devices(refresh=True)}
    mono = devices["Fake USB Conference Mic"].recommended_profile()
    assert mono.channels == 1, "a mono microphone is never inflated to stereo"
    assert mono.sample_rate == 48_000
    narrow = devices["Fake Narrowband Phone Mic"].recommended_profile()
    assert narrow.sample_rate == 8_000, "native rate is preferred over resampling"


def test_selection_round_trips_without_storing_only_an_index(
    backend: FakeAudioBackend,
) -> None:
    service = DeviceDiscoveryService(backend, endpoint_provider=_no_endpoints)
    original = DeviceSelection.from_device(service.input_devices(refresh=True)[0])
    restored = DeviceSelection.from_dict(original.to_dict())
    assert restored == original
    assert restored.fingerprint and restored.name and restored.host_api


# ===========================================================================
# Windows transport resolution: verified or UNKNOWN, never guessed
# ===========================================================================


def test_transport_is_unknown_without_os_evidence() -> None:
    transport, source, _ = resolve_transport("Some USB Conference Mic", [])
    assert transport is DeviceTransport.UNKNOWN
    assert source == "unverified"


def test_transport_is_never_inferred_from_the_name() -> None:
    endpoints = [
        WindowsAudioEndpoint("id", "Headset", "", "BTHENUM", True),
    ]
    transport, _, _ = resolve_transport("Totally USB Microphone (Vendor)", endpoints)
    assert transport is DeviceTransport.UNKNOWN


def test_transport_is_read_from_the_windows_enumerator() -> None:
    endpoints = [WindowsAudioEndpoint("id", "Jabra Speak", "", "USB", True)]
    transport, source, evidence = resolve_transport("Jabra Speak (Jabra Corp)", endpoints)
    assert transport is DeviceTransport.USB
    assert source == "windows-mmdevices-registry"
    assert "USB" in evidence


def test_active_endpoints_win_over_stale_ones() -> None:
    endpoints = [
        WindowsAudioEndpoint("stale", "Microphone Array", "", "INTELAUDIO", False),
        WindowsAudioEndpoint("live", "Microphone Array", "", "INTELAUDIO", True),
    ]
    transport, source, _ = resolve_transport("Microphone Array (Intel Smart Sound)", endpoints)
    assert transport is DeviceTransport.INTERNAL
    assert source == "windows-mmdevices-registry"


def test_exact_matching_prevents_a_generic_description_from_matching() -> None:
    """A Bluetooth endpoint called simply "Microphone" must not match an array."""
    endpoints = [
        WindowsAudioEndpoint("bt", "Microphone", "", "BTHENUM", True),
        WindowsAudioEndpoint("intel", "Microphone Array", "", "INTELAUDIO", True),
    ]
    matched = match_windows_endpoints("Microphone Array (Intel Smart Sound)", endpoints)
    assert [m.endpoint_id for m in matched] == ["intel"]
    transport, _, _ = resolve_transport("Microphone Array (Intel Smart Sound)", endpoints)
    assert transport is DeviceTransport.INTERNAL


def test_ambiguous_bus_reports_unknown() -> None:
    endpoints = [
        WindowsAudioEndpoint("a", "Microphone", "", "USB", True),
        WindowsAudioEndpoint("b", "Microphone", "", "BTHENUM", True),
    ]
    transport, source, evidence = resolve_transport("Microphone (Some Adapter)", endpoints)
    assert transport is DeviceTransport.UNKNOWN
    assert source == "windows-mmdevices-registry"
    assert "ambiguous" in evidence


def test_device_name_split() -> None:
    assert split_device_name("Microphone Array (Intel Smart Sound)") == (
        "Microphone Array",
        "Intel Smart Sound",
    )
    assert split_device_name("Plain Name") == ("Plain Name", "")


def test_device_disabled_in_windows_is_rejected() -> None:
    from mom_igd.audio.backend import RawDeviceInfo

    endpoints = [WindowsAudioEndpoint("id", "External Microphone", "", "HDAUDIO", False)]
    backend = FakeAudioBackend(
        devices=[
            RawDeviceInfo(
                index=0,
                name="External Microphone (Realtek)",
                host_api="Windows WASAPI",
                max_input_channels=1,
                max_output_channels=0,
                default_sample_rate=48_000.0,
            )
        ]
    )
    service = DeviceDiscoveryService(backend, endpoint_provider=lambda: endpoints)
    assert service.input_devices(refresh=True) == []
    assert "disabled or unplugged" in service.rejected_devices()[0].rejection_reason


# ===========================================================================
# Bounded queue
# ===========================================================================


def test_queue_capacity_is_measured_in_seconds(profile: CaptureProfile) -> None:
    queue = BoundedFrameQueue(profile, capacity_seconds=5.0)
    assert queue.capacity_frames == FAST_RATE * 5


def test_queue_rejects_an_insane_capacity(profile: CaptureProfile) -> None:
    with pytest.raises(ValueError, match="sane range"):
        BoundedFrameQueue(profile, capacity_seconds=0.01)
    with pytest.raises(ValueError, match="sane range"):
        BoundedFrameQueue(profile, capacity_seconds=600.0)


def test_queue_never_grows_past_capacity_and_counts_the_loss(
    profile: CaptureProfile,
) -> None:
    queue = BoundedFrameQueue(profile, capacity_seconds=0.5)
    block = b"\x00" * (1000 * profile.bytes_per_frame)
    accepted = sum(1 for _ in range(20) if queue.put_nowait(block, 1000))

    stats = queue.stats()
    assert accepted == 4, "0.5 s at 8 kHz holds exactly four 1000-frame blocks"
    assert stats.queued_frames <= queue.capacity_frames
    assert stats.dropped_frames == 16_000
    assert stats.drop_events == 16
    assert stats.has_loss is True


def test_queue_put_does_not_block_when_full(profile: CaptureProfile) -> None:
    """The callback must return immediately even with nobody consuming."""
    import time

    queue = BoundedFrameQueue(profile, capacity_seconds=0.5)
    block = b"\x00" * (1000 * profile.bytes_per_frame)
    for _ in range(10):
        queue.put_nowait(block, 1000)

    started = time.perf_counter()
    for _ in range(500):
        queue.put_nowait(block, 1000)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5, f"500 rejected puts took {elapsed:.3f} s; must not block"


def test_queue_preserves_order_and_tracks_high_water(profile: CaptureProfile) -> None:
    queue = BoundedFrameQueue(profile, capacity_seconds=5.0)
    for index in range(5):
        queue.put_nowait(bytes([index]) * profile.bytes_per_frame, 1)
    received = [queue.get(timeout=0.01) for _ in range(5)]
    assert [item[0][0] for item in received if item] == [0, 1, 2, 3, 4]
    assert queue.stats().high_water_frames == 5
    assert queue.stats().queued_frames == 0


def test_queue_get_returns_none_on_timeout(profile: CaptureProfile) -> None:
    assert BoundedFrameQueue(profile).get(timeout=0.01) is None


def test_closed_queue_refuses_input(profile: CaptureProfile) -> None:
    queue = BoundedFrameQueue(profile)
    queue.close()
    assert queue.put_nowait(b"\x00\x00\x00\x00", 1) is False
    assert queue.closed


# ===========================================================================
# Quality meter
# ===========================================================================


def test_dbfs_uses_the_asymmetric_int16_reference() -> None:
    """A hard-clipped negative rail reaches 32768; dBFS must clamp at 0."""
    assert to_dbfs(32_768) == 0.0
    assert to_dbfs(40_000) == 0.0, "a magnitude above full scale is still 0 dBFS"
    assert to_dbfs(0) == pytest.approx(-120.0)
    assert to_dbfs(3_277) == pytest.approx(-20.0, abs=0.05)


def test_empty_block_is_handled(profile: CaptureProfile) -> None:
    levels = analyse_block(b"", profile.channels)
    assert levels.frames == 0
    assert levels.rms == 0.0
    assert levels.rms_dbfs == pytest.approx(-120.0)


def test_partial_trailing_frame_is_ignored(profile: CaptureProfile) -> None:
    pcm = CounterSource().read(0, 10, profile) + b"\x01\x02\x03"
    assert analyse_block(pcm, profile.channels).frames == 10


def test_silence_is_reported_as_no_signal(profile: CaptureProfile) -> None:
    meter = QualityMeter(profile)
    meter.add(SilenceSource().read(0, FAST_RATE, profile))
    snapshot = meter.cumulative_snapshot()
    assert snapshot.verdict is LevelVerdict.NO_SIGNAL
    assert snapshot.silence_percent == 100.0
    assert snapshot.clipped_samples == 0


def test_normal_level_is_reported_as_good(profile: CaptureProfile) -> None:
    meter = QualityMeter(profile)
    meter.add(SineSource(frequency_hz=440.0, level_dbfs=-15.0).read(0, FAST_RATE, profile))
    snapshot = meter.cumulative_snapshot()
    assert snapshot.verdict is LevelVerdict.GOOD
    assert snapshot.peak_dbfs == pytest.approx(-15.0, abs=0.3)
    assert snapshot.rms_dbfs == pytest.approx(-18.0, abs=0.3)
    assert snapshot.clipping_percent == 0.0


def test_clipping_is_detected(profile: CaptureProfile) -> None:
    meter = QualityMeter(profile)
    meter.add(ClippingSource(overdrive_db=6.0).read(0, FAST_RATE, profile))
    snapshot = meter.cumulative_snapshot()
    assert snapshot.verdict is LevelVerdict.CLIPPING
    assert snapshot.clipped_samples > 0
    assert snapshot.peak_dbfs == pytest.approx(0.0, abs=0.01)
    assert "Lower the input level" in snapshot.verdict.advice


def test_too_quiet_is_detected(profile: CaptureProfile) -> None:
    """A usable-but-faint signal is TOO_QUIET; below the silence floor it is NO_SIGNAL.

    -52 dBFS sits deliberately between the two thresholds: quiet enough to warn
    about, loud enough to be distinguishable from an unplugged microphone.
    """
    meter = QualityMeter(profile)
    meter.add(SineSource(level_dbfs=-52.0).read(0, FAST_RATE, profile))
    assert meter.cumulative_snapshot().verdict is LevelVerdict.TOO_QUIET


def test_signal_below_the_silence_floor_is_no_signal(profile: CaptureProfile) -> None:
    meter = QualityMeter(profile)
    meter.add(SineSource(level_dbfs=-70.0).read(0, FAST_RATE, profile))
    snapshot = meter.cumulative_snapshot()
    assert snapshot.verdict is LevelVerdict.NO_SIGNAL
    assert snapshot.silence_percent == 100.0


def test_channel_activity_exposes_a_dead_channel(profile: CaptureProfile) -> None:
    meter = QualityMeter(profile)
    meter.add(
        StereoActivitySource(left_dbfs=-15.0, right_dbfs=-90.0).read(0, FAST_RATE, profile)
    )
    snapshot = meter.cumulative_snapshot()
    assert len(snapshot.channels) == 2
    assert snapshot.channels[0].active is True
    assert snapshot.channels[1].active is False
    assert snapshot.inactive_channels == (1,)


def test_mono_meter_reports_one_channel(mono_profile: CaptureProfile) -> None:
    meter = QualityMeter(mono_profile)
    meter.add(SineSource(level_dbfs=-20.0).read(0, FAST_RATE, mono_profile))
    assert len(meter.cumulative_snapshot().channels) == 1


def test_rolling_window_forgets_old_audio(profile: CaptureProfile) -> None:
    meter = QualityMeter(profile, rolling_seconds=1.0)
    meter.add(ClippingSource().read(0, FAST_RATE * 2, profile))
    meter.add(SilenceSource().read(0, FAST_RATE, profile))
    assert meter.rolling_snapshot().verdict is LevelVerdict.NO_SIGNAL
    assert meter.cumulative_snapshot().clipped_samples > 0, "history keeps the clipping"


def test_snapshot_is_json_safe(profile: CaptureProfile) -> None:
    import json

    meter = QualityMeter(profile)
    meter.add(SineSource().read(0, 1000, profile))
    json.dumps(meter.cumulative_snapshot().to_dict())


# ===========================================================================
# Writer: rotation, exactness, checksums, no overwrite
# ===========================================================================


def test_writer_rotates_at_exact_chunk_boundaries(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    writer = ChunkWriter(rec_dir, profile)
    source = CounterSource()
    total = profile.frames_per_chunk * 2 + 1234
    written = bytearray()
    position = 0
    while position < total:
        take = min(997, total - position)
        pcm = source.read(position, take, profile)
        written += pcm
        writer.write(pcm)
        position += take
    writer.close()

    records = writer.finalised
    assert [r.frame_count for r in records] == [
        profile.frames_per_chunk,
        profile.frames_per_chunk,
        1234,
    ]
    assert [r.seq for r in records] == [0, 1, 2]
    assert all(r.end_frame == records[i + 1].start_frame for i, r in enumerate(records[:-1]))

    recovered = bytearray()
    for record in records:
        payload, channels, rate, frames = _read_wav(rec_dir / record.filename)
        assert channels == profile.channels
        assert rate == profile.sample_rate
        assert frames == record.frame_count
        recovered += payload
    assert bytes(recovered) == bytes(written), "audio must be byte-exact"


def test_writer_filenames_carry_no_personal_data(rec_dir: Path, profile: CaptureProfile) -> None:
    writer = ChunkWriter(rec_dir, profile, recording_uuid="uuid-1234")
    writer.write(CounterSource().read(0, 100, profile))
    writer.close()
    assert writer.finalised[0].filename == "chunk_000000.wav"
    assert chunk_filename(7) == "chunk_000007.wav"


def test_writer_removes_partials_only_after_the_final_file_exists(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    writer = ChunkWriter(rec_dir, profile)
    writer.write(CounterSource().read(0, 500, profile))
    assert partial_path(rec_dir, 0).is_file(), "partial exists while writing"
    writer.close()
    assert not partial_path(rec_dir, 0).exists()
    assert (rec_dir / "chunk_000000.wav").is_file()
    assert list(rec_dir.glob("*.tmp")) == []
    assert list(rec_dir.glob("*.meta.json")) == []


def test_writer_refuses_to_overwrite_a_final_chunk(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    first = ChunkWriter(rec_dir, profile)
    first.write(CounterSource().read(0, 200, profile))
    first.close()
    original = (rec_dir / "chunk_000000.wav").read_bytes()

    second = ChunkWriter(rec_dir, profile, start_seq=0)
    second.write(CounterSource().read(0, 300, profile))
    with pytest.raises(WriterError, match="Refusing to overwrite"):
        second.close()
    assert (rec_dir / "chunk_000000.wav").read_bytes() == original


def test_writer_discards_an_empty_chunk(rec_dir: Path, profile: CaptureProfile) -> None:
    writer = ChunkWriter(rec_dir, profile)
    assert writer.close() is None
    assert list(rec_dir.glob("*.wav")) == []


def test_writer_carries_a_partial_frame_between_writes(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    writer = ChunkWriter(rec_dir, profile)
    pcm = CounterSource().read(0, 10, profile)
    writer.write(pcm[:-1])  # one byte short of a whole frame
    writer.write(pcm[-1:])
    writer.close()
    payload, _, _, frames = _read_wav(rec_dir / "chunk_000000.wav")
    assert frames == 10
    assert payload == pcm


def test_writer_reports_a_disk_failure(rec_dir: Path, profile: CaptureProfile) -> None:
    writer = ChunkWriter(rec_dir, profile)
    writer.write(CounterSource().read(0, 100, profile))

    class _Failing:
        def write(self, _data):
            raise OSError(28, "No space left on device")

        def flush(self):
            pass

        def fileno(self):
            raise OSError("closed")

        def close(self):
            pass

    writer._current.handle = _Failing()  # noqa: SLF001 - simulating a full disk
    with pytest.raises(WriterError, match="No space left"):
        writer.write(CounterSource().read(100, 100, profile))


# ===========================================================================
# Manifest and integrity
# ===========================================================================


def test_manifest_records_everything_needed_to_verify_audio(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    manifest = ManifestWriter(rec_dir)
    writer = ChunkWriter(
        rec_dir, profile, on_finalised=lambda f: manifest.append_chunk(f.record)
    )
    writer.write(CounterSource().read(0, 5_000, profile))
    writer.close()

    records, _, torn = read_manifest(rec_dir)
    assert torn == 0
    record = records[0]
    for field_name in (
        "seq",
        "filename",
        "start_frame",
        "end_frame",
        "frame_count",
        "duration_ms",
        "utc_start",
        "utc_end",
        "monotonic_start_ns",
        "monotonic_end_ns",
        "sample_rate",
        "channels",
        "sample_format",
        "byte_count",
        "sha256",
        "xrun_callbacks",
        "dropped_frames",
        "status",
        "recovery_status",
        "finalized",
    ):
        assert getattr(record, field_name) is not None, field_name
    assert len(record.sha256) == 64
    assert record.sample_format == SampleFormat.INT16.value
    assert record.finalized is True


def test_manifest_verification_passes_for_a_clean_recording(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    manifest = ManifestWriter(rec_dir)
    writer = ChunkWriter(rec_dir, profile, on_finalised=lambda f: manifest.append_chunk(f.record))
    writer.write(CounterSource().read(0, profile.frames_per_chunk + 500, profile))
    writer.close()
    write_manifest_summary(
        rec_dir,
        recording_uuid="r",
        meeting_uuid="m",
        profile=profile,
        records=writer.finalised,
    )

    report = verify_manifest(rec_dir)
    assert report.ok, report.problems
    assert report.verified_chunks == 2
    assert report.chain_sha256 == report.summary_chain_sha256


def test_modified_audio_is_detected(rec_dir: Path, profile: CaptureProfile) -> None:
    manifest = ManifestWriter(rec_dir)
    writer = ChunkWriter(rec_dir, profile, on_finalised=lambda f: manifest.append_chunk(f.record))
    writer.write(CounterSource().read(0, 1_000, profile))
    writer.close()

    victim = rec_dir / "chunk_000000.wav"
    data = bytearray(victim.read_bytes())
    data[80] ^= 0xFF
    victim.write_bytes(bytes(data))

    report = verify_manifest(rec_dir)
    assert not report.ok
    assert report.checksum_mismatches == ["chunk_000000.wav"]
    assert any("checksum mismatch" in p for p in report.problems)


def test_edited_manifest_is_detected_by_the_chain(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    manifest = ManifestWriter(rec_dir)
    writer = ChunkWriter(rec_dir, profile, on_finalised=lambda f: manifest.append_chunk(f.record))
    writer.write(CounterSource().read(0, 1_000, profile))
    writer.close()
    write_manifest_summary(
        rec_dir, recording_uuid="r", meeting_uuid="m", profile=profile, records=writer.finalised
    )

    # Rewrite manifest.jsonl claiming a different frame count.
    lines = (rec_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    tampered = [line.replace('"frame_count":1000', '"frame_count":999') for line in lines]
    (rec_dir / "manifest.jsonl").write_text("\n".join(tampered) + "\n", encoding="utf-8")

    report = verify_manifest(rec_dir)
    assert not report.ok
    assert any("chain hash mismatch" in p for p in report.problems)


def test_missing_chunk_file_is_detected(rec_dir: Path, profile: CaptureProfile) -> None:
    manifest = ManifestWriter(rec_dir)
    writer = ChunkWriter(rec_dir, profile, on_finalised=lambda f: manifest.append_chunk(f.record))
    writer.write(CounterSource().read(0, 1_000, profile))
    writer.close()
    (rec_dir / "chunk_000000.wav").unlink()

    report = verify_manifest(rec_dir)
    assert report.missing_files == ["chunk_000000.wav"]
    assert not report.ok


def test_torn_final_manifest_line_is_tolerated(rec_dir: Path, profile: CaptureProfile) -> None:
    manifest = ManifestWriter(rec_dir)
    writer = ChunkWriter(rec_dir, profile, on_finalised=lambda f: manifest.append_chunk(f.record))
    writer.write(CounterSource().read(0, 1_000, profile))
    writer.close()

    with (rec_dir / "manifest.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"type": "chunk", "seq": 1, "filen')  # crash mid-append

    records, _, torn = read_manifest(rec_dir)
    assert torn == 1
    assert len(records) == 1, "records before the torn line stay valid"


def test_chain_hash_is_stable_and_content_sensitive(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    """The chain is order-independent as input but sensitive to every field.

    It sorts by sequence, so passing the records in a different order yields the
    same hash -- that is what makes it a stable identifier. What it must detect is
    a *changed* record, so each field that describes the audio is varied here.
    """
    import dataclasses

    manifest = ManifestWriter(rec_dir)
    writer = ChunkWriter(rec_dir, profile, on_finalised=lambda f: manifest.append_chunk(f.record))
    writer.write(CounterSource().read(0, profile.frames_per_chunk + 10, profile))
    writer.close()
    records = writer.finalised
    baseline = compute_chain_hash(records)

    assert compute_chain_hash(list(reversed(records))) == baseline

    for field_name, value in (
        ("sha256", "f" * 64),
        ("frame_count", records[0].frame_count + 1),
        ("byte_count", records[0].byte_count + 1),
        ("seq", 99),
    ):
        mutated = [dataclasses.replace(records[0], **{field_name: value}), records[1]]
        assert compute_chain_hash(mutated) != baseline, field_name


def test_chunk_filenames_cannot_traverse(rec_dir: Path, profile: CaptureProfile) -> None:
    manifest = ManifestWriter(rec_dir)
    writer = ChunkWriter(rec_dir, profile, on_finalised=lambda f: manifest.append_chunk(f.record))
    writer.write(CounterSource().read(0, 100, profile))
    writer.close()
    for record in writer.finalised:
        assert "/" not in record.filename
        assert "\\" not in record.filename
        assert ".." not in record.filename
        assert not Path(record.filename).is_absolute()


# ===========================================================================
# Session lifecycle
# ===========================================================================


@pytest.fixture
def make_session():
    """Create capture sessions and guarantee they are stopped.

    Without this, a mid-test assertion failure leaves the writer thread running,
    and every later test that asserts "no writer thread leaked" fails for a reason
    that has nothing to do with what it is testing. One leaked thread produced six
    misleading failures before this fixture existed.
    """
    created: list[CaptureSession] = []

    def _factory(
        backend: FakeAudioBackend, profile: CaptureProfile, rec_dir: Path, **kwargs
    ) -> CaptureSession:
        session = CaptureSession(
            backend,
            device_index=0,
            profile=profile,
            directory=rec_dir,
            recording_uuid="rec-uuid",
            manifest=ManifestWriter(rec_dir),
            **kwargs,
        )
        created.append(session)
        return session

    yield _factory

    for session in created:
        try:
            session.stop()
        except Exception:  # noqa: BLE001 - teardown must not mask the real failure
            pass
    assert _live_writer_threads() == [], "a capture session leaked its writer thread"


def test_session_captures_byte_exact_audio(
    backend: FakeAudioBackend, profile: CaptureProfile, rec_dir: Path, make_session
) -> None:
    session = make_session(backend, profile, rec_dir)
    session.start()
    stream = backend.streams[0]
    stream.pump(20)  # 20_000 frames
    result = session.stop()

    assert result.state is SessionState.STOPPED
    assert result.dropped_frames == 0
    assert result.frames_written == 20_000
    payload, _, _, frames = _read_wav(rec_dir / "chunk_000000.wav")
    assert frames == 20_000
    assert payload == CounterSource().read(0, 20_000, profile)
    assert _live_writer_threads() == []


def test_double_start_does_not_open_a_second_stream(
    backend: FakeAudioBackend, profile: CaptureProfile, rec_dir: Path, make_session
) -> None:
    session = make_session(backend, profile, rec_dir)
    session.start()
    session.start()
    assert backend.open_calls == 1
    session.stop()


def test_stop_is_idempotent(
    backend: FakeAudioBackend, profile: CaptureProfile, rec_dir: Path, make_session
) -> None:
    session = make_session(backend, profile, rec_dir)
    session.start()
    backend.streams[0].pump(3)
    first = session.stop()
    second = session.stop()
    third = session.stop()
    assert first.frames_written == second.frames_written == third.frames_written
    assert second.state is SessionState.STOPPED
    assert _live_writer_threads() == []


def test_stop_without_start_is_safe(
    backend: FakeAudioBackend, profile: CaptureProfile, rec_dir: Path, make_session
) -> None:
    result = make_session(backend, profile, rec_dir).stop()
    assert result.state is SessionState.STOPPED
    assert result.frames_written == 0


def test_pause_finalises_a_chunk_and_resume_starts_a_new_one(
    backend: FakeAudioBackend, profile: CaptureProfile, rec_dir: Path, make_session
) -> None:
    session = make_session(backend, profile, rec_dir)
    session.start()
    backend.streams[0].pump(5)
    session.pause()

    assert session.state is SessionState.PAUSED
    assert (rec_dir / "chunk_000000.wav").is_file(), "pause closes a chunk boundary"

    session.resume()
    backend.streams[1].pump(5)
    result = session.stop()

    assert [r.seq for r in result.chunks] == [0, 1]
    assert result.chunks[0].frame_count == 5_000
    assert result.chunks[1].frame_count == 5_000
    gaps = [g for g in result.gaps if g["reason"] == "paused"]
    assert len(gaps) == 1
    assert gaps[0]["intentional"] is True
    assert gaps[0]["at_frame"] == 5_000


def test_pause_is_idempotent_and_state_guarded(
    backend: FakeAudioBackend, profile: CaptureProfile, rec_dir: Path, make_session
) -> None:
    session = make_session(backend, profile, rec_dir)
    with pytest.raises(StreamError):
        session.pause()  # not running yet
    session.start()
    session.pause()
    session.pause()
    session.resume()
    session.resume()
    session.stop()


def test_audio_either_side_of_a_pause_is_intact_and_the_gap_is_recorded(
    backend: FakeAudioBackend, profile: CaptureProfile, rec_dir: Path, make_session
) -> None:
    """A pause splits the timeline; neither segment loses or duplicates a frame.

    Each ``FakeStream`` counts from zero, so the two segments carry the same
    deterministic pattern -- which is precisely what makes "no frame lost inside a
    segment" checkable byte for byte. Continuity *across* the pause is not
    claimed: the pause is real missing time, and it is recorded as a gap rather
    than stitched over.
    """
    session = make_session(backend, profile, rec_dir)
    session.start()
    backend.streams[0].pump(4)
    session.pause()  # drains the queue before finalising, so no wait is needed
    assert session.frames_written == 4_000
    session.resume()
    backend.streams[1].pump(4)
    result = session.stop()

    first, _, _, first_frames = _read_wav(rec_dir / "chunk_000000.wav")
    second, _, _, second_frames = _read_wav(rec_dir / "chunk_000001.wav")
    expected = CounterSource().read(0, 4_000, profile)

    assert first_frames == second_frames == 4_000
    assert first == expected, "no frame lost or reordered before the pause"
    assert second == expected, "no frame lost or reordered after the pause"
    gaps = [g for g in result.gaps if g["reason"] == "paused"]
    assert len(gaps) == 1 and gaps[0]["intentional"] is True
    assert gaps[0]["at_frame"] == 4_000, "the gap is anchored to the timeline"
    assert result.dropped_frames == 0


def test_queue_overflow_is_counted_and_marks_the_recording_degraded(
    profile: CaptureProfile, rec_dir: Path, make_session
) -> None:
    backend = FakeAudioBackend(blocksize=4_000, source=CounterSource())
    session = make_session(backend, profile, rec_dir, queue_seconds=0.5)
    session.start()
    stream = backend.streams[0]
    # The writer thread is polling, so drive far past capacity in one burst.
    for _ in range(40):
        stream.pump(1)
    result = session.stop()

    if result.dropped_frames:
        assert result.degraded is True
        overflow = [g for g in result.gaps if g["reason"] == "queue_overflow"]
        assert overflow, "a dropped block must be recorded as an unintentional gap"
        assert overflow[0]["intentional"] is False
        assert result.frames_written < result.frames_captured
    else:
        pytest.skip("writer kept up; overflow path exercised by the queue unit tests")


def test_xrun_status_is_recorded_not_hidden(
    backend: FakeAudioBackend, profile: CaptureProfile, rec_dir: Path, make_session
) -> None:
    session = make_session(backend, profile, rec_dir)
    session.start()
    stream = backend.streams[0]
    stream.pump(2)
    stream.pump(2, status=CallbackStatus(input_overflow=True))
    result = session.stop()
    assert result.xrun_callbacks >= 1
    assert sum(c.xrun_callbacks for c in result.chunks) >= 1


def test_device_disconnect_preserves_finalised_audio(
    backend: FakeAudioBackend, profile: CaptureProfile, rec_dir: Path, make_session
) -> None:
    session = make_session(backend, profile, rec_dir)
    session.start()
    stream = backend.streams[0]
    # One full chunk plus a little, so a chunk is definitely finalised and a
    # partial is definitely open when the device vanishes.
    _pump_frames(session, stream, profile.frames_per_chunk + 2_000)
    stream.raise_on_stop = OSError("device was unplugged")

    result = session.abandon()

    assert result.state is SessionState.FAILED
    assert (rec_dir / "chunk_000000.wav").is_file(), "the finalised chunk survives"
    assert verify_manifest(rec_dir).verified_chunks >= 1
    assert _live_writer_threads() == []
    assert backend.open_streams == [] or all(s.closed for s in backend.streams)


def test_no_silent_fallback_when_the_stream_cannot_open(
    backend: FakeAudioBackend, profile: CaptureProfile, rec_dir: Path, make_session
) -> None:
    backend.fail_on_open = StreamError("device busy")
    session = make_session(backend, profile, rec_dir)
    with pytest.raises(StreamError, match="device busy"):
        session.start()
    assert session.state is SessionState.FAILED
    assert _live_writer_threads() == []


def test_writer_exception_fails_the_session_without_losing_earlier_chunks(
    backend: FakeAudioBackend, profile: CaptureProfile, rec_dir: Path, make_session
) -> None:
    session = make_session(backend, profile, rec_dir)
    session.start()
    stream = backend.streams[0]
    _pump_frames(session, stream, profile.frames_per_chunk)  # one full chunk on disk
    _await_chunks(session, 1)
    assert (rec_dir / "chunk_000000.wav").is_file(), "first chunk finalised normally"

    # Make the next write fail the way a full disk would.
    original = session._writer.write  # noqa: SLF001 - fault injection

    def _boom(*_args, **_kwargs):
        raise WriterError("No space left on device")

    session._writer.write = _boom  # noqa: SLF001
    stream.pump(2)
    result = session.stop()

    assert result.error is not None
    assert "No space left" in result.error
    assert result.degraded is True
    assert (rec_dir / "chunk_000000.wav").is_file()
    assert _live_writer_threads() == []
    session._writer.write = original  # noqa: SLF001


def test_status_snapshot_is_cheap_and_leaks_no_path(
    backend: FakeAudioBackend, profile: CaptureProfile, rec_dir: Path, make_session
) -> None:
    import json

    session = make_session(backend, profile, rec_dir)
    session.start()
    backend.streams[0].pump(2)
    status = session.status()
    session.stop()

    text = json.dumps(status)
    assert str(rec_dir) not in text
    assert ":\\" not in text
    for key in ("state", "elapsed_seconds", "frames_written", "queue", "profile"):
        assert key in status


def test_session_drift_is_measured_against_a_monotonic_clock(
    backend: FakeAudioBackend, profile: CaptureProfile, rec_dir: Path, make_session
) -> None:
    session = make_session(backend, profile, rec_dir)
    session.start()
    backend.streams[0].pump(10)
    result = session.stop()
    assert result.audio_seconds == pytest.approx(10_000 / FAST_RATE, abs=0.001)
    assert result.wall_seconds >= 0.0


def test_no_thread_leaks_across_many_sessions(
    profile: CaptureProfile, tmp_path: Path
) -> None:
    before = threading.active_count()
    for index in range(5):
        directory = tmp_path / f"rec{index}"
        directory.mkdir()
        backend = FakeAudioBackend(blocksize=500, source=CounterSource())
        session = CaptureSession(
            backend, device_index=0, profile=profile, directory=directory
        )
        session.start()
        backend.streams[0].pump(2)
        session.stop()
    assert _live_writer_threads() == []
    assert threading.active_count() <= before + 1


# ===========================================================================
# Recovery
# ===========================================================================


def _make_partial(
    rec_dir: Path, profile: CaptureProfile, seq: int, frames: int, *, extra: bytes = b""
) -> Path:
    write_partial_meta(
        rec_dir,
        seq,
        profile,
        start_frame=0,
        utc_start="2026-01-01T00:00:00.000Z",
        monotonic_start_ns=1,
        recording_uuid="rec",
    )
    path = partial_path(rec_dir, seq)
    path.write_bytes(CounterSource().read(0, frames, profile) + extra)
    return path


def test_recovery_rebuilds_a_partial_into_valid_audio(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    _make_partial(rec_dir, profile, 0, 3_210)

    report = recover_recording(rec_dir, profile=profile)

    assert report.ok, report.problems
    assert report.chunks_recovered == 1
    assert report.frames_recovered == 3_210
    payload, _, _, frames = _read_wav(rec_dir / "chunk_000000.wav")
    assert frames == 3_210
    assert payload == CounterSource().read(0, 3_210, profile)
    records, _, _ = read_manifest(rec_dir)
    assert records[0].recovery_status == RecoveryStatus.RECOVERED.value
    assert not partial_path(rec_dir, 0).exists()


def test_recovery_discards_only_the_incomplete_trailing_frame(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    _make_partial(rec_dir, profile, 0, 100, extra=b"\x01\x02\x03")

    report = recover_recording(rec_dir, profile=profile)

    assert report.frames_recovered == 100
    assert report.bytes_discarded == 3
    records, _, _ = read_manifest(rec_dir)
    assert records[0].status == ChunkStatus.TRUNCATED.value
    assert "trailing" in (records[0].notes or "")


def test_recovery_quarantines_a_partial_without_metadata(rec_dir: Path) -> None:
    partial_path(rec_dir, 0).write_bytes(b"\x01\x02\x03\x04" * 10)

    report = recover_recording(rec_dir, profile=None)

    assert report.chunks_quarantined == 1
    assert report.chunks_recovered == 0
    quarantined = list((rec_dir / "quarantine").glob("*.pcm.part"))
    assert len(quarantined) == 1, "evidence is preserved, not deleted"
    assert list((rec_dir / "quarantine").glob("*.reason.txt"))


def test_recovery_quarantines_a_partial_shorter_than_one_frame(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    write_partial_meta(
        rec_dir, 0, profile, start_frame=0, utc_start="x", monotonic_start_ns=0
    )
    partial_path(rec_dir, 0).write_bytes(b"\x01\x02")  # 2 bytes, frame is 4

    report = recover_recording(rec_dir, profile=profile)

    assert report.chunks_quarantined == 1
    assert not (rec_dir / "chunk_000000.wav").exists()


def test_recovery_never_overwrites_a_valid_final_chunk(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    manifest = ManifestWriter(rec_dir)
    writer = ChunkWriter(rec_dir, profile, on_finalised=lambda f: manifest.append_chunk(f.record))
    writer.write(CounterSource().read(0, 500, profile))
    writer.close()
    original = (rec_dir / "chunk_000000.wav").read_bytes()

    # A stale partial for the same sequence turns up after a crash.
    _make_partial(rec_dir, profile, 0, 9_999)
    report = recover_recording(rec_dir, profile=profile)

    assert (rec_dir / "chunk_000000.wav").read_bytes() == original
    assert report.already_final == 1
    assert report.chunks_recovered == 0


def test_recovery_quarantines_a_partial_whose_final_is_unrecorded(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    (rec_dir / "chunk_000000.wav").write_bytes(b"RIFF-not-in-manifest")
    _make_partial(rec_dir, profile, 0, 100)

    report = recover_recording(rec_dir, profile=profile)

    assert report.chunks_quarantined == 1
    assert (rec_dir / "chunk_000000.wav").read_bytes() == b"RIFF-not-in-manifest"


def test_recovery_is_idempotent(rec_dir: Path, profile: CaptureProfile) -> None:
    _make_partial(rec_dir, profile, 0, 1_000)
    first = recover_recording(rec_dir, profile=profile)
    second = recover_recording(rec_dir, profile=profile)

    assert first.chunks_recovered == 1
    assert second.chunks_recovered == 0
    assert second.partials_found == 0
    assert second.changed is False
    assert verify_manifest(rec_dir).verified_chunks == 1


def test_recovery_removes_an_abandoned_temporary_wav(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    (rec_dir / "chunk_000000.wav.tmp").write_bytes(b"incomplete")
    report = recover_recording(rec_dir, profile=profile)
    assert report.chunks_quarantined == 1
    assert not (rec_dir / "chunk_000000.wav.tmp").exists()
    assert list((rec_dir / "quarantine").glob("*.tmp"))


def test_scan_finds_only_recordings_needing_recovery(
    tmp_path: Path, profile: CaptureProfile
) -> None:
    root = tmp_path / "recordings"
    clean = root / "m1" / "r1"
    clean.mkdir(parents=True)
    manifest = ManifestWriter(clean)
    writer = ChunkWriter(clean, profile, on_finalised=lambda f: manifest.append_chunk(f.record))
    writer.write(CounterSource().read(0, 100, profile))
    writer.close()
    write_manifest_summary(
        clean, recording_uuid="r1", meeting_uuid="m1", profile=profile, records=writer.finalised
    )

    dirty = root / "m2" / "r2"
    dirty.mkdir(parents=True)
    _make_partial(dirty, profile, 0, 100)

    found = scan_recoverable(root)
    assert dirty in found
    assert clean not in found


def test_find_partials_orders_by_sequence(rec_dir: Path, profile: CaptureProfile) -> None:
    for seq in (2, 0, 1):
        _make_partial(rec_dir, profile, seq, 10)
    assert [seq for seq, _ in find_partials(rec_dir)] == [0, 1, 2]


def test_recovered_audio_verifies_against_the_manifest(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    _make_partial(rec_dir, profile, 0, 2_048)
    recover_recording(rec_dir, profile=profile)
    write_manifest_summary(
        rec_dir,
        recording_uuid="r",
        meeting_uuid="m",
        profile=profile,
        records=read_manifest(rec_dir)[0],
    )
    report = verify_manifest(rec_dir)
    assert report.ok, report.problems
    assert report.verified_chunks == 1


def test_recovery_after_an_abandoned_session_salvages_the_partial(
    backend: FakeAudioBackend, profile: CaptureProfile, rec_dir: Path, make_session
) -> None:
    """End to end: a killed recording loses nothing that reached the writer."""
    session = make_session(backend, profile, rec_dir)
    session.start()
    backend.streams[0].pump(7)  # 7_000 frames, no chunk boundary reached
    session.abandon()

    assert list(rec_dir.glob("*.pcm.part")), "the partial is left for recovery"
    report = recover_recording(rec_dir, profile=profile)

    assert report.chunks_recovered == 1
    assert report.frames_recovered == 7_000
    payload, _, _, frames = _read_wav(rec_dir / "chunk_000000.wav")
    assert frames == 7_000
    assert payload == CounterSource().read(0, 7_000, profile)
    assert _live_writer_threads() == []


# ===========================================================================
# Repository and dependency boundaries
# ===========================================================================


def test_no_audio_artefact_is_committed_to_the_repository() -> None:
    from mom_igd.paths import repo_root

    root = repo_root()
    skip = {".git", ".venv", "__pycache__", ".pytest_cache"}
    offenders = [
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file()
        and not any(part in skip for part in p.parts)
        and p.suffix.lower() in {".wav", ".flac", ".mp3", ".pcm", ".part", ".raw"}
    ]
    assert offenders == [], f"audio artefacts in the repository: {offenders}"


def test_the_capture_path_does_not_use_numpy() -> None:
    """Capture reads bytes from `RawInputStream` and meters with `array.array`.

    NumPy became installed in Phase 4 -- CTranslate2 requires it -- so its *absence* is no
    longer the thing to assert. The property that still matters, and that Phase 4 must not
    erode, is that the capture path itself does not depend on it: an import there would put
    a large numerical library on the real-time audio path, and the writer callback must
    stay allocation-light.
    """
    import ast

    offenders: list[str] = []
    for path in sorted((Path(__file__).resolve().parent.parent / "mom_igd" / "audio").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in {"numpy", "soundfile", "librosa", "soxr", "av"}:
                    offenders.append(f"{path.name}:{node.lineno} imports {name}")
    assert offenders == [], (
        f"the capture path must stay free of heavy numerical/codec libraries: {offenders}"
    )


def test_no_out_of_phase_dependency_is_installed() -> None:
    """The Phase 4 ASR stack graduated; Phase 5+ dependencies must still be absent.

    `audit_installed_distributions` reads the *current* deferred set, so this keeps
    working as phases advance -- what it guards against is a Phase 5 or Phase 8 dependency
    arriving early.
    """
    from mom_igd.offline_policy import audit_installed_distributions

    audit = audit_installed_distributions()
    assert audit["cloud"] == []
    assert audit["deferred"] == [], f"out-of-phase dependencies: {audit['deferred']}"


def test_the_phase_5_and_later_stacks_are_still_absent() -> None:
    """Named explicitly, so graduating one by accident is visible."""
    import importlib.util

    for name in ("torch", "torchaudio", "pyannote", "speechbrain", "transformers",
                 "sentence_transformers", "llama_cpp", "openvino"):
        assert importlib.util.find_spec(name) is None, (
            f"{name} belongs to a later phase and must not be installed yet"
        )


def test_sounddevice_is_an_allowed_phase_2_dependency() -> None:
    from mom_igd.offline_policy import (
        CLOUD_SDK_DENYLIST,
        DEFERRED_HEAVY_DISTRIBUTIONS,
        audit_distribution_names,
    )

    assert "sounddevice" not in DEFERRED_HEAVY_DISTRIBUTIONS
    assert "sounddevice" not in CLOUD_SDK_DENYLIST
    assert audit_distribution_names(["sounddevice"])["deferred"] == []
    # Everything else audio-related stays out.
    # `numpy` and `av` graduated in Phase 4 (ADR-0014): `av` decodes and resamples the
    # 16 kHz working copy and numpy is a hard CTranslate2 requirement. They are checked
    # above by `test_the_capture_path_does_not_use_numpy` instead -- the capture path is
    # what must stay free of them, not the environment.
    findings = audit_distribution_names(
        ["soundfile", "pyaudio", "librosa", "webrtcvad", "silero-vad"]
    )
    assert sorted(findings["deferred"]) == [
        "librosa",
        "pyaudio",
        "silero-vad",
        "soundfile",
        "webrtcvad",
    ]


def test_sounddevice_is_pinned_in_the_runtime_lock() -> None:
    from mom_igd.paths import repo_root

    text = (repo_root() / "requirements.txt").read_text(encoding="utf-8")
    pins = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("sounddevice") and "==" in line
    ]
    assert pins == ["sounddevice==0.5.5"]


# ---------------------------------------------------------------------------
# Recovery closes an interrupted recording
#
# Found during the Phase 4 acceptance handoff: a capture killed *after* its last
# chunk but *before* finalisation left a directory with manifest lines and no
# summary. `scan_recoverable` reported it for ever, `doctor` told the operator to
# run `audio recover`, and recovery had no partial to salvage so it changed
# nothing. A warning whose named remedy provably does nothing is worse than no
# warning: it teaches the operator to ignore the check.
# ---------------------------------------------------------------------------


def _closed_chunks(directory: Path, profile: CaptureProfile, count: int = 2) -> None:
    """Write `count` complete chunks and their manifest lines, but no summary."""
    manifest = ManifestWriter(directory)
    writer = ChunkWriter(
        directory, profile, on_finalised=lambda f: manifest.append_chunk(f.record)
    )
    for index in range(count):
        writer.write(CounterSource().read(index * 500, 500, profile))
        writer.finalise_current()
    writer.close()


def test_recovery_closes_a_recording_whose_chunks_were_all_complete(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    _closed_chunks(rec_dir, profile)
    assert not (rec_dir / "manifest.json").is_file()
    assert rec_dir in scan_recoverable(rec_dir.parent.parent)

    report = recover_recording(rec_dir, profile=profile)

    assert report.summary_written is True
    assert report.changed is True, "a closed recording is a change, not a no-op"
    assert report.chunks_recovered == 0
    assert (rec_dir / "manifest.json").is_file()
    assert scan_recoverable(rec_dir.parent.parent) == [], (
        "the directory must stop being reported, or the warning has no remedy"
    )


def test_the_recovered_summary_says_recovery_produced_it(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    """Nothing later may mistake a salvaged recording for one that closed cleanly."""
    import json

    _closed_chunks(rec_dir, profile)
    recover_recording(rec_dir, profile=profile)
    summary = json.loads((rec_dir / "manifest.json").read_text(encoding="utf-8"))
    assert summary["counters"]["finalised_by"] == "recovery"
    assert summary["counters"]["interrupted"] is True


def test_the_recovered_summary_is_derived_from_the_verified_chunk_records(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    """The manifest lines are authoritative and already carry their own SHA-256."""
    _closed_chunks(rec_dir, profile)
    records = read_manifest(rec_dir)[0]
    recover_recording(rec_dir, profile=profile)
    report = verify_manifest(rec_dir)
    assert report.ok, report.problems
    assert report.verified_chunks == len(records) == 2


def test_closing_a_recording_is_idempotent(rec_dir: Path, profile: CaptureProfile) -> None:
    _closed_chunks(rec_dir, profile)
    first = recover_recording(rec_dir, profile=profile)
    second = recover_recording(rec_dir, profile=profile)
    assert first.summary_written is True
    assert second.summary_written is False
    assert second.changed is False


def test_a_recording_that_still_has_a_partial_is_not_closed_early(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    """Closing it now would understate the recording: the partial is still audio."""
    _closed_chunks(rec_dir, profile, count=1)
    (rec_dir / "chunk_000001.pcm.part").write_bytes(b"\x00" * 400)
    report = recover_recording(rec_dir, profile=profile)
    # The partial is salvaged in this same pass, and only then is the summary written.
    assert report.chunks_recovered + report.chunks_quarantined == 1
    assert (rec_dir / "manifest.json").is_file()
    assert report.summary_written is True


def test_a_directory_with_no_surviving_chunk_record_is_not_invented_into_a_recording(
    rec_dir: Path,
) -> None:
    """A torn first line means there is no recording to close."""
    (rec_dir / "manifest.jsonl").write_text('{"type": "chunk", "seq": 0', encoding="utf-8")
    report = recover_recording(rec_dir)
    assert report.summary_written is False
    assert not (rec_dir / "manifest.json").exists()
    assert any("no recording to close" in problem for problem in report.problems)


def test_the_format_is_taken_from_the_chunk_records_when_no_profile_is_given(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    """Recovery from the CLI may not know the profile the capture used."""
    import json

    _closed_chunks(rec_dir, profile)
    report = recover_recording(rec_dir)
    assert report.summary_written is True
    summary = json.loads((rec_dir / "manifest.json").read_text(encoding="utf-8"))
    assert summary["profile"]["sample_rate"] == profile.sample_rate
    assert summary["profile"]["channels"] == profile.channels


def test_a_clean_recording_is_left_completely_alone(
    rec_dir: Path, profile: CaptureProfile
) -> None:
    import hashlib

    _closed_chunks(rec_dir, profile)
    write_manifest_summary(
        rec_dir,
        recording_uuid="r",
        meeting_uuid="m",
        profile=profile,
        records=read_manifest(rec_dir)[0],
    )
    before = hashlib.sha256((rec_dir / "manifest.json").read_bytes()).hexdigest()
    report = recover_recording(rec_dir, profile=profile)
    after = hashlib.sha256((rec_dir / "manifest.json").read_bytes()).hexdigest()
    assert report.summary_written is False
    assert before == after, "an already-closed recording must not be rewritten"
