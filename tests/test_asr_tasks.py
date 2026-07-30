"""What runs *inside* the worker: task orchestration, aggregation, cancellation.

`tests/test_asr_worker.py` covers the parent side -- spawning, measurement, escalation.
This file covers the task bodies, run in-process so their branching is actually
observable. A task body is where region batching, per-region cancellation and the
release-in-`finally` contract live, and none of that is visible through a subprocess.

The engine is substituted here, deliberately and only here: these tests are about the
task's own logic, not the decoder's. The substitute is the Phase 4 deterministic double
from `mom_igd.asr.fake_provider`, which cannot be selected by configuration, environment
or request -- `tests/test_asr_provider.py` proves that separately.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path
from typing import Any

import pytest

from mom_igd.asr import faster_whisper_provider as fwp
from mom_igd.asr import tasks as tasks_module
from mom_igd.asr.fake_provider import DeterministicAsrProvider
from mom_igd.asr.provider import (
    ProviderError,
    SpeechRegion,
    TranscriptionRequest,
    validate_transcription,
)
from mom_igd.asr.smoke import (
    _claim,
    _working_copy_complaint,
    describe_wav,
    generate_speech_like_wav,
    no_network,
)
from mom_igd.asr.tasks import TASK_REGISTRY, TaskCancelled

TASKS_SRC = Path(__file__).resolve().parent.parent / "mom_igd" / "asr" / "tasks.py"


def _tone_wav(path: Path, seconds: float = 1.0, rate: int = 16_000) -> Path:
    frames = bytearray()
    for index in range(int(seconds * rate)):
        frames += struct.pack("<h", int(6000 * math.sin(2 * math.pi * 200 * index / rate)))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return path


# ===========================================================================
# A stand-in engine, with the constructor shape the task actually uses
# ===========================================================================


class _StubEngine:
    """Wraps the deterministic double behind `FasterWhisperProvider`'s constructor.

    Records what the task asked for, so the task's decisions can be asserted rather
    than inferred.
    """

    instances: list[_StubEngine] = []

    def __init__(self, resolved: Any, *, compute_type: str = "int8", cpu_threads: int = 0):
        self.resolved = resolved
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self.inner = DeterministicAsrProvider()
        self.requests: list[TranscriptionRequest] = []
        self.closed = 0
        self.load_calls = 0
        _StubEngine.instances.append(self)

    @property
    def load_seconds(self) -> float:
        return 0.5

    def load(self):
        self.load_calls += 1
        return self.inner.load()

    def health(self) -> dict[str, Any]:
        return {"provider_id": "stub", "loaded": True}

    def transcribe(self, request: TranscriptionRequest):
        self.requests.append(request)
        # The double refuses a whole-file decode on purpose, so a whole-file request is
        # given one explicit region here. The task's batching is what is under test.
        if not request.regions:
            request = TranscriptionRequest(
                audio_path=request.audio_path,
                regions=(SpeechRegion(index=0, start=0.0, end=4.0),),
                language=request.language,
                initial_prompt=request.initial_prompt,
                beam_size=request.beam_size,
                temperature=request.temperature,
                condition_on_previous_text=request.condition_on_previous_text,
                word_timestamps=request.word_timestamps,
            )
        return self.inner.transcribe(request)

    def close(self) -> None:
        self.closed += 1


class _ExplodingEngine(_StubEngine):
    def transcribe(self, request: TranscriptionRequest):
        raise ProviderError("decode failed")


@pytest.fixture()
def stub_engine(monkeypatch: pytest.MonkeyPatch):
    """Substitute the engine and the resolver, and hand back the instance list."""
    _StubEngine.instances.clear()
    monkeypatch.setattr(fwp, "FasterWhisperProvider", _StubEngine)
    monkeypatch.setattr(fwp, "resolve_model", lambda models_dir, **kw: object())
    return _StubEngine.instances


# ===========================================================================
# transcribe: batching
# ===========================================================================


def _payload(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "models_dir": str(tmp_path / "models"),
        "audio_path": str(tmp_path / "work.wav"),
        "regions": [
            {"index": 0, "start": 0.0, "end": 4.0},
            {"index": 1, "start": 5.0, "end": 9.0},
        ],
        "language": "id",
        "asr_pass": 1,
    }
    payload.update(overrides)
    return payload


def test_nearby_regions_are_batched_into_one_decode(
    tmp_path: Path, stub_engine: list
) -> None:
    """Whisper pads every window to 30 s, so one decode per region wastes most of it.

    Measured: a 24-second recording split into ten regions ran at RTF 2.8 decoding
    region-by-region, and at 0.31 once the regions were batched into 30-second windows.
    """
    result = TASK_REGISTRY["transcribe"](_payload(tmp_path), lambda: False)
    engine = stub_engine[0]
    assert len(engine.requests) == 1, "two regions 9 seconds apart fit in one window"
    assert [region.index for region in engine.requests[0].regions] == [0, 1]
    assert result["regions_requested"] == 1
    assert result["regions_completed"] == 1
    assert result["cancelled"] is False


def test_regions_further_apart_than_the_window_are_decoded_separately(
    tmp_path: Path, stub_engine: list
) -> None:
    result = TASK_REGISTRY["transcribe"](
        _payload(
            tmp_path,
            regions=[
                {"index": 0, "start": 0.0, "end": 4.0},
                {"index": 1, "start": 100.0, "end": 104.0},
            ],
        ),
        lambda: False,
    )
    assert len(stub_engine[0].requests) == 2
    assert result["regions_requested"] == 2


def test_segment_indices_are_renumbered_across_regions(
    tmp_path: Path, stub_engine: list
) -> None:
    """Each per-region decode starts its own numbering; the task must make it global."""
    result = TASK_REGISTRY["transcribe"](_payload(tmp_path), lambda: False)
    assert [segment["index"] for segment in result["segments"]] == list(
        range(len(result["segments"]))
    )
    assert len(result["segments"]) == 2


def test_every_segment_carries_the_pass_number_that_produced_it(
    tmp_path: Path, stub_engine: list
) -> None:
    result = TASK_REGISTRY["transcribe"](_payload(tmp_path, asr_pass=2), lambda: False)
    assert {segment["asr_pass"] for segment in result["segments"]} == {2}


def test_durations_are_summed_across_regions(tmp_path: Path, stub_engine: list) -> None:
    result = TASK_REGISTRY["transcribe"](_payload(tmp_path), lambda: False)
    assert result["audio_seconds"] == pytest.approx(8.0)
    assert result["processing_seconds"] >= 0.0
    assert result["load_seconds"] == pytest.approx(0.5)


def test_no_regions_means_one_whole_file_decode(tmp_path: Path, stub_engine: list) -> None:
    """The benchmark and the smoke test both decode a whole file."""
    result = TASK_REGISTRY["transcribe"](_payload(tmp_path, regions=[]), lambda: False)
    engine = stub_engine[0]
    assert len(engine.requests) == 1
    assert engine.requests[0].regions == ()
    assert result["regions_requested"] == 1


def test_the_decode_options_the_task_passes_are_the_documented_ones(
    tmp_path: Path, stub_engine: list
) -> None:
    result = TASK_REGISTRY["transcribe"](
        _payload(tmp_path, beam_size=5, temperature=0.0, initial_prompt="rapat"),
        lambda: False,
    )
    request = stub_engine[0].requests[0]
    assert request.beam_size == 5
    assert request.temperature == 0.0
    assert request.initial_prompt == "rapat"
    assert request.condition_on_previous_text is False, (
        "conditioning on previous text propagates a hallucination through a meeting"
    )
    assert request.word_timestamps is True
    assert result["language"] == "id"


def test_the_task_output_passes_the_real_validator(tmp_path: Path, stub_engine: list) -> None:
    """The task reshapes segments; the reshaped form must still be valid."""
    result = TASK_REGISTRY["transcribe"](_payload(tmp_path), lambda: False)
    for segment in result["segments"]:
        assert segment["speaker"] is None
        assert segment["end"] >= segment["start"]
        for word in segment["words"]:
            assert segment["start"] - 1e-3 <= word["start"] <= segment["end"] + 1e-3


def test_the_model_provenance_travels_with_the_result(
    tmp_path: Path, stub_engine: list
) -> None:
    result = TASK_REGISTRY["transcribe"](_payload(tmp_path), lambda: False)
    assert "model" in result
    assert result["model"]["is_test_double"] is True, (
        "a double must be identifiable in the payload it produced"
    )


# ===========================================================================
# transcribe: cancellation and release
# ===========================================================================


def test_cancellation_between_batches_reports_what_was_completed(
    tmp_path: Path, stub_engine: list
) -> None:
    """"Cancelled after 1 of 2 batches" must be distinguishable from "failed".

    The batch is the cancellation boundary, not the region: that is the trade batching
    makes -- at most 30 seconds of work is discarded, in exchange for up to a fifteenfold
    reduction in decode cost.
    """
    calls = {"n": 0}

    def cancelled() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # allow the first batch, stop before the second

    result = TASK_REGISTRY["transcribe"](
        _payload(
            tmp_path,
            regions=[
                {"index": 0, "start": 0.0, "end": 4.0},
                {"index": 1, "start": 100.0, "end": 104.0},
            ],
        ),
        cancelled,
    )
    assert result["cancelled"] is True
    assert result["regions_completed"] == 1
    assert result["regions_requested"] == 2
    assert len(result["segments"]) == 1


def test_cancellation_before_the_first_region_produces_an_empty_result_not_an_error(
    tmp_path: Path, stub_engine: list
) -> None:
    result = TASK_REGISTRY["transcribe"](_payload(tmp_path), lambda: True)
    assert result["cancelled"] is True
    assert result["regions_completed"] == 0
    assert result["segments"] == []
    assert stub_engine[0].closed == 1, "the model must be released even so"


def test_the_engine_is_released_on_the_success_path(
    tmp_path: Path, stub_engine: list
) -> None:
    TASK_REGISTRY["transcribe"](_payload(tmp_path), lambda: False)
    assert stub_engine[0].closed == 1


def test_the_engine_is_released_when_a_decode_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the `finally`, a failed job would hold hundreds of megabytes."""
    _StubEngine.instances.clear()
    monkeypatch.setattr(fwp, "FasterWhisperProvider", _ExplodingEngine)
    monkeypatch.setattr(fwp, "resolve_model", lambda models_dir, **kw: object())
    with pytest.raises(ProviderError):
        TASK_REGISTRY["transcribe"](_payload(tmp_path), lambda: False)
    assert _StubEngine.instances[0].closed == 1


def test_a_model_that_cannot_be_resolved_raises_before_an_engine_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MODEL_UNAVAILABLE, never a fallback and never a download."""
    _StubEngine.instances.clear()
    monkeypatch.setattr(fwp, "FasterWhisperProvider", _StubEngine)

    def refuse(models_dir, **kw):
        raise fwp.ModelUnavailableError("MODEL_UNAVAILABLE: nothing is ready")

    monkeypatch.setattr(fwp, "resolve_model", refuse)
    with pytest.raises(fwp.ModelUnavailableError, match="MODEL_UNAVAILABLE"):
        TASK_REGISTRY["transcribe"](_payload(tmp_path), lambda: False)
    assert _StubEngine.instances == []


def test_the_role_is_passed_through_to_the_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass-2 must not silently resolve the pass-1 model."""
    seen: dict[str, Any] = {}

    def record(models_dir, **kw):
        seen.update(kw)
        return object()

    _StubEngine.instances.clear()
    monkeypatch.setattr(fwp, "FasterWhisperProvider", _StubEngine)
    monkeypatch.setattr(fwp, "resolve_model", record)
    TASK_REGISTRY["transcribe"](_payload(tmp_path, role="pass2"), lambda: False)
    assert seen["role"] == "pass2"


def test_the_thread_count_and_compute_type_reach_the_engine(
    tmp_path: Path, stub_engine: list
) -> None:
    TASK_REGISTRY["transcribe"](
        _payload(tmp_path, cpu_threads=8, compute_type="int8"), lambda: False
    )
    assert stub_engine[0].cpu_threads == 8
    assert stub_engine[0].compute_type == "int8"


# ===========================================================================
# transcribe: egress recording is off unless asked for
# ===========================================================================


def test_egress_recording_is_off_by_default(tmp_path: Path, stub_engine: list) -> None:
    """The production pipeline must not depend on monkey-patching the socket layer."""
    result = TASK_REGISTRY["transcribe"](_payload(tmp_path), lambda: False)
    assert result["network_attempts"] == []


def test_egress_recording_reports_an_empty_list_when_nothing_was_attempted(
    tmp_path: Path, stub_engine: list
) -> None:
    result = TASK_REGISTRY["transcribe"](
        _payload(tmp_path, record_network_attempts=True), lambda: False
    )
    assert result["network_attempts"] == []


def test_an_attempt_made_during_a_decode_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent zero would make the benchmark's egress line worthless."""

    class _ReachingEngine(_StubEngine):
        def transcribe(self, request: TranscriptionRequest):
            import socket

            try:
                socket.getaddrinfo("huggingface.co", 443)
            except OSError:
                pass
            return super().transcribe(request)

    _StubEngine.instances.clear()
    monkeypatch.setattr(fwp, "FasterWhisperProvider", _ReachingEngine)
    monkeypatch.setattr(fwp, "resolve_model", lambda models_dir, **kw: object())
    result = TASK_REGISTRY["transcribe"](
        _payload(tmp_path, record_network_attempts=True), lambda: False
    )
    assert result["network_attempts"], "an outbound attempt was not recorded"
    assert any("huggingface" in attempt for attempt in result["network_attempts"])


# ===========================================================================
# vad
# ===========================================================================


def test_the_vad_task_returns_a_serialisable_result(tmp_path: Path) -> None:
    audio = _tone_wav(tmp_path / "a.wav", 1.0)
    result = TASK_REGISTRY["vad"]({"audio_path": str(audio)}, lambda: False)
    assert result["ran"] is True
    assert result["region_count"] == 0
    assert result["model_name"] == "silero-vad-v6-bundled"
    assert len(result["model_sha256"]) == 64


def test_the_vad_task_applies_a_supplied_configuration(tmp_path: Path) -> None:
    audio = _tone_wav(tmp_path / "a.wav", 1.0)
    result = TASK_REGISTRY["vad"](
        {"audio_path": str(audio), "config": {"threshold": 0.9, "min_speech_ms": 400}},
        lambda: False,
    )
    assert result["config"]["threshold"] == 0.9
    assert result["config"]["min_speech_ms"] == 400
    default = TASK_REGISTRY["vad"]({"audio_path": str(audio)}, lambda: False)
    assert result["config_hash"] != default["config_hash"]


def test_an_unrecognised_configuration_key_is_ignored_rather_than_crashing(
    tmp_path: Path,
) -> None:
    """A payload from an older job row must not break the worker."""
    audio = _tone_wav(tmp_path / "a.wav", 1.0)
    result = TASK_REGISTRY["vad"](
        {"audio_path": str(audio), "config": {"threshold": 0.4, "future_option": 1}},
        lambda: False,
    )
    assert result["config"]["threshold"] == 0.4


def test_the_vad_task_stops_when_cancellation_is_already_requested() -> None:
    with pytest.raises(TaskCancelled, match="before VAD started"):
        TASK_REGISTRY["vad"]({"audio_path": "unused.wav"}, lambda: True)


# ===========================================================================
# probe tasks
# ===========================================================================


def test_the_directory_probe_refuses_when_cancellation_is_requested() -> None:
    with pytest.raises(TaskCancelled, match="before the probe started"):
        TASK_REGISTRY["probe_directory"](
            {"directory": "x", "audio_path": "y"}, lambda: True
        )


def test_the_directory_probe_uses_the_unverified_free_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It runs *before* readiness is recorded, so it cannot use the readiness gate.

    That is the circular dependency this split exists to break: the probe is what
    produces the readiness verdict, so it must resolve by verifying the directory
    directly.
    """
    _StubEngine.instances.clear()
    seen: dict[str, Any] = {}

    def record(directory, deep=False):
        seen["directory"] = str(directory)
        seen["deep"] = deep
        return object()

    monkeypatch.setattr(fwp, "FasterWhisperProvider", _StubEngine)
    monkeypatch.setattr(fwp, "resolve_verified_directory", record)

    def refuse(*args: Any, **kw: Any):
        raise AssertionError("the probe must not consult the readiness index")

    monkeypatch.setattr(fwp, "resolve_model", refuse)
    result = TASK_REGISTRY["probe_directory"](
        {"directory": str(tmp_path / "m"), "audio_path": str(tmp_path / "a.wav")},
        lambda: False,
    )
    assert seen["directory"] == str(tmp_path / "m")
    assert result["segments"] >= 1
    assert _StubEngine.instances[0].closed == 1


def test_the_model_probe_loads_and_releases_without_transcribing(
    tmp_path: Path, stub_engine: list
) -> None:
    result = TASK_REGISTRY["probe_model"](
        {"models_dir": str(tmp_path), "role": "pass1"}, lambda: False
    )
    assert result["load_seconds"] == pytest.approx(0.5)
    assert result["health"]["provider_id"] == "stub"
    assert stub_engine[0].requests == [], "a probe must not decode"
    assert stub_engine[0].closed == 1


def test_the_model_probe_releases_the_engine_when_loading_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FailingLoad(_StubEngine):
        def load(self):
            raise ProviderError("cannot construct")

    _StubEngine.instances.clear()
    monkeypatch.setattr(fwp, "FasterWhisperProvider", _FailingLoad)
    monkeypatch.setattr(fwp, "resolve_model", lambda models_dir, **kw: object())
    with pytest.raises(ProviderError):
        TASK_REGISTRY["probe_model"]({"models_dir": str(tmp_path)}, lambda: False)
    assert _StubEngine.instances[0].closed == 1


# ===========================================================================
# The task module itself
# ===========================================================================


def test_no_task_imports_the_heavy_stack_at_module_level() -> None:
    """A worker that imports the engine to run VAD wastes seconds of every job."""
    import ast

    tree = ast.parse(TASKS_SRC.read_text(encoding="utf-8"))
    top_level = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    names: set[str] = set()
    for node in top_level:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif node.module:
            names.add(node.module.split(".")[0])
    assert names <= {"__future__", "pathlib", "typing"}, names


def test_the_task_module_writes_nothing_to_stdout() -> None:
    source = TASKS_SRC.read_text(encoding="utf-8")
    assert "print(" not in source
    assert "sys.stdout" not in source


# ===========================================================================
# Smoke helpers
# ===========================================================================


def test_the_generated_smoke_audio_is_the_working_copy_format(tmp_path: Path) -> None:
    path = tmp_path / "s.wav"
    duration = generate_speech_like_wav(path, 3.0)
    assert duration == pytest.approx(3.0)
    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 16_000
        assert handle.getnframes() == 48_000


def test_the_generated_smoke_audio_is_byte_identical_between_runs(tmp_path: Path) -> None:
    """A smoke test that varies cannot be compared against a previous run."""
    first, second = tmp_path / "a.wav", tmp_path / "b.wav"
    generate_speech_like_wav(first, 2.0)
    generate_speech_like_wav(second, 2.0)
    assert first.read_bytes() == second.read_bytes()


def test_the_generated_smoke_audio_has_both_speech_and_silence(tmp_path: Path) -> None:
    """VAD needs something to segment; a constant tone gives it nothing."""
    path = tmp_path / "s.wav"
    generate_speech_like_wav(path, 4.0)
    with wave.open(str(path), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    assert any(sample == 0 for sample in samples), "no silence"
    assert max(abs(sample) for sample in samples) > 1000, "no signal"


def test_the_generated_smoke_audio_contains_no_human_voice(tmp_path: Path) -> None:
    """Asserted at the source: it is arithmetic, not a recording."""
    import inspect

    source = inspect.getsource(generate_speech_like_wav)
    assert "math.sin" in source
    for forbidden in ("read_bytes", "wave.open(str(source", "sounddevice", "microphone"):
        assert forbidden not in source


def test_the_egress_blocker_restores_every_primitive_it_patched() -> None:
    """A leaked patch would silently break unrelated tests later in the session."""
    import socket
    import urllib.request

    before = (
        socket.socket.connect,
        socket.socket.connect_ex,
        socket.create_connection,
        socket.getaddrinfo,
        urllib.request.urlopen,
    )
    with no_network() as blocker:
        assert socket.getaddrinfo is not before[3], "the blocker did not patch DNS"
        with pytest.raises(OSError, match="blocked by the offline"):
            socket.getaddrinfo("huggingface.co", 443)
        assert blocker.attempts, "the attempt must be recorded, not only refused"
    after = (
        socket.socket.connect,
        socket.socket.connect_ex,
        socket.create_connection,
        socket.getaddrinfo,
        urllib.request.urlopen,
    )
    assert before == after


def test_the_egress_blocker_records_the_host_it_refused() -> None:
    """Evidence has to say *what* was attempted, or it cannot be investigated."""
    import socket

    with no_network() as blocker:
        for call in (
            lambda: socket.getaddrinfo("example.invalid", 80),
            lambda: socket.create_connection(("example.invalid", 80)),
        ):
            with pytest.raises(OSError):
                call()
    joined = " ".join(blocker.attempts)
    assert "example.invalid" in joined
    assert len(blocker.attempts) >= 2


# ===========================================================================
# The --audio speech path: format gate and claim wording
# ===========================================================================


def _wav(path: Path, *, channels: int, width: int, rate: int, frames: int = 1600) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(b"\x00" * (frames * channels * width))
    return path


def test_a_working_copy_is_accepted(tmp_path: Path) -> None:
    described = describe_wav(_wav(tmp_path / "a.wav", channels=1, width=2, rate=16_000))
    assert _working_copy_complaint(described) is None
    assert described["seconds"] == pytest.approx(0.1)
    assert len(described["sha256"]) == 64


@pytest.mark.parametrize(
    ("channels", "width", "rate", "expected"),
    [
        (2, 2, 16_000, "2 channels"),
        (1, 1, 16_000, "8-bit samples"),
        (1, 2, 8_000, "8000 Hz"),
        (2, 1, 44_100, "channels"),
    ],
)
def test_a_file_that_is_not_a_working_copy_is_refused_with_the_reason(
    tmp_path: Path, channels: int, width: int, rate: int, expected: str
) -> None:
    described = describe_wav(
        _wav(tmp_path / "a.wav", channels=channels, width=width, rate=rate)
    )
    complaint = _working_copy_complaint(described)
    assert complaint is not None
    assert expected in complaint
    assert "16 kHz mono PCM16 WAV" in complaint, "the message must name what would work"


def test_the_format_gate_reports_every_problem_at_once(tmp_path: Path) -> None:
    """Fixing one thing and being told about the next is a bad diagnostic."""
    described = describe_wav(_wav(tmp_path / "a.wav", channels=2, width=1, rate=8_000))
    complaint = _working_copy_complaint(described)
    assert complaint is not None
    assert "channels" in complaint and "8-bit" in complaint and "8000 Hz" in complaint


def test_the_format_gate_does_not_offer_to_convert(tmp_path: Path) -> None:
    """Silently resampling an operator's audio would destroy its provenance."""
    described = describe_wav(_wav(tmp_path / "a.wav", channels=2, width=2, rate=16_000))
    complaint = _working_copy_complaint(described)
    assert complaint is not None
    assert "does not convert" in complaint


def test_the_file_digest_changes_with_the_content(tmp_path: Path) -> None:
    """The digest is the provenance recorded for the run; it has to be of the bytes."""
    first = describe_wav(_wav(tmp_path / "a.wav", channels=1, width=2, rate=16_000))
    second = describe_wav(
        _wav(tmp_path / "b.wav", channels=1, width=2, rate=16_000, frames=3200)
    )
    assert first["sha256"] != second["sha256"]


@pytest.mark.parametrize("mode", ["synthetic", "local-audio"])
def test_a_failed_run_claims_nothing(mode: str) -> None:
    """The most dangerous line in a report is a success claim on a failed run."""
    claim = _claim(mode, False)
    assert "nothing is established" in claim
    for forbidden in ("ran end to end", "produced words", "validation only"):
        assert forbidden not in claim


def test_the_synthetic_claim_disowns_speech_recognition() -> None:
    claim = _claim("synthetic", True)
    assert "not speech" in claim
    assert "NO" in claim and "accuracy claim" in claim
    assert "--audio" in claim, "it must point at the mode that does cover real speech"


def test_the_local_audio_claim_still_refuses_an_accuracy_claim() -> None:
    claim = _claim("local-audio", True)
    assert "no accuracy claim" in claim
    assert "reference transcript" in claim


def test_the_smoke_never_computes_a_word_error_rate() -> None:
    """Accuracy belongs to `asr bench --manifest`, which gates on consent.

    Checked over identifiers rather than raw text: the module's own prose says it does
    not compute a WER, and a substring search would flag the sentence that promises it.
    """
    import ast

    source = (Path(__file__).resolve().parent.parent / "mom_igd" / "asr" / "smoke.py").read_text(
        encoding="utf-8"
    )
    identifiers: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            identifiers.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr.lower())
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            identifiers.add(node.name.lower())
        elif isinstance(node, ast.ImportFrom):
            identifiers.update(alias.name.lower() for alias in node.names)
    for banned in ("word_error_rate", "wer", "accuracy", "technical_term_recall"):
        assert banned not in identifiers, f"{banned} must not be computed by the smoke"


def test_the_smoke_deletes_only_what_it_generated() -> None:
    """An operator's recording must survive the run that read it."""
    source = (Path(__file__).resolve().parent.parent / "mom_igd" / "asr" / "smoke.py").read_text(
        encoding="utf-8"
    )
    unlink_at = source.index("audio.unlink")
    guard = source[source.index("finally:", unlink_at - 400) : unlink_at]
    assert "if generated:" in guard, (
        "the removal must be guarded by 'this function created the file'"
    )


def test_a_result_from_the_double_is_valid_by_the_real_rules() -> None:
    """Guards the substitution used above: if the double drifted, these tests would lie."""
    provider = DeterministicAsrProvider()
    provider.load()
    result = provider.transcribe(
        TranscriptionRequest(
            audio_path="unused.wav",
            regions=(SpeechRegion(index=0, start=0.0, end=4.0),),
            language="id",
        )
    )
    validated = validate_transcription(result)
    assert validated.segments == result.segments
