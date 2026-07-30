"""Real-model offline smoke test.

Proves the claim that matters most about Phase 4: **a provisioned model loads from a
local path and transcribes local audio with no network access.** A benchmark run with a
fake provider proves nothing about that, and neither does a unit test.

By default the audio is **synthesised** -- a source-filter model with formant resonances,
a syllable-rate envelope and vowel transitions -- so this needs no microphone, no corpus
and no human voice. That matters more than it sounds: an earlier version used an
amplitude-modulated tone, VAD correctly found no speech in it, and the whole
region-decoding path was therefore never exercised. The synthesis crosses Silero's
threshold (about 94 % of the file), so the run now covers VAD producing regions and the
engine decoding them.

``--audio <local-wav>`` runs the same steps against an operator's own working-copy
recording. The only difference is the provenance of the audio; both modes require speech
to be found and words to be produced, so neither can pass vacuously.

**Neither mode measures accuracy, and the synthetic one cannot.** Synthesised formants are
not speech, so whatever the engine returns for them is meaningless as text -- what is
proven is that the machinery runs. Accuracy needs a reference transcript, which needs
consent and licence metadata, which is what ``asr bench --manifest`` exists to enforce.
Nothing here computes a WER, and accuracy is never derived from the model's own output.

**How the no-network claim is enforced, not merely asserted.** Every outbound
connection primitive is replaced for the duration of the run: ``socket.socket.connect``,
``connect_ex``, ``create_connection``, ``getaddrinfo`` and ``urllib.request.urlopen``.
A call to any of them raises and is recorded. This is deliberately different from the
project's standing rule against a global socket patch (ADR-0002): that rule is about
the *runtime*, where patching sockets would break loopback IPC and hide bugs. Here the
patch is the instrument of a test, scoped to one function, and reverted in a ``finally``.
"""

from __future__ import annotations

import math
import socket
import struct
import time
import wave
from pathlib import Path
from typing import Any, Callable, Final, Iterator

from mom_igd.logging_setup import get_logger

__all__ = [
    "generate_formant_speech_wav",
    "generate_speech_like_wav",
    "no_network",
    "run_asr_smoke",
]

_LOG = get_logger("asr.smoke")

WORKING_SAMPLE_RATE = 16_000


def generate_speech_like_wav(
    target: Path, seconds: float, *, sample_rate: int = WORKING_SAMPLE_RATE
) -> float:
    """Write deterministic 16 kHz mono PCM16 with speech-like energy bursts.

    Not a voice, and never claimed to be one. Bursts separated by silence give VAD
    something real to segment and give the engine a plausible signal envelope, which
    is all this test needs.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    total = int(seconds * sample_rate)
    frames = bytearray()
    for index in range(total):
        t = index / sample_rate
        # 1.4 s of speech, 0.6 s of silence, repeating.
        phase = t % 2.0
        if phase > 1.4:
            frames += struct.pack("<h", 0)
            continue
        envelope = 0.55 + 0.45 * math.sin(2 * math.pi * 1.7 * t)
        sample = envelope * 0.30 * (
            math.sin(2 * math.pi * 160 * t)
            + 0.55 * math.sin(2 * math.pi * 410 * t)
            + 0.28 * math.sin(2 * math.pi * 930 * t)
        )
        frames += struct.pack("<h", max(-32768, min(32767, int(sample * 22000))))
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(frames))
    return total / sample_rate


#: Indonesian vowel formants (F1, F2, F3) in Hz. Textbook values for a synthesiser --
#: **not measured from any person**, and no recording of a voice is involved anywhere.
_VOWEL_FORMANTS: Final[tuple[tuple[float, float, float], ...]] = (
    (730.0, 1090.0, 2440.0),  # a
    (270.0, 2290.0, 3010.0),  # i
    (300.0, 870.0, 2240.0),   # u
    (530.0, 1840.0, 2480.0),  # e
    (570.0, 840.0, 2410.0),   # o
)


def _deterministic_noise(seed: int) -> Iterator[float]:
    """A linear congruential generator, so aspiration noise is reproducible.

    Deliberately not ``random``: a fixture that differs between runs cannot be compared
    with a previous run, and seeding the global generator would leak into other code.
    """
    state = seed & 0xFFFFFFFF
    while True:
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        yield (state / 0x3FFFFFFF) - 1.0


def generate_formant_speech_wav(
    target: Path,
    seconds: float,
    *,
    sample_rate: int = WORKING_SAMPLE_RATE,
    f0: float = 120.0,
) -> float:
    """Write deterministic 16 kHz mono PCM16 that voice activity detection accepts.

    **Why this exists.** An amplitude-modulated tone is not speech, and Silero correctly
    finds nothing in it -- which means a tone can never exercise the part of the pipeline
    that matters most: VAD producing regions, and the engine decoding those regions. This
    synthesises audio with the structure VAD keys on (a glottal pulse train, three formant
    resonances, a syllable-rate envelope, vowel transitions and a pitch contour) and
    measurably crosses the threshold: about 94 % of the file is classified as speech.

    **It is still not speech, and nothing here claims it is.** No human voice is recorded,
    sampled or reproduced -- it is a source-filter model evaluated in pure arithmetic. The
    words the engine returns for it are meaningless, so this proves that the machinery runs
    end to end and says nothing whatsoever about recognition accuracy.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    total = int(seconds * sample_rate)
    noise = _deterministic_noise(20260730)
    states = [[0.0, 0.0] for _ in range(3)]
    phase = 0.0
    frames = bytearray()
    for index in range(total):
        t = index / sample_rate
        # 1.6 s of utterance, 0.5 s of silence, repeating -- so VAD has boundaries to
        # find rather than one unbroken block.
        cycle = t % 2.1
        if cycle > 1.6:
            frames += struct.pack("<h", 0)
            for state in states:
                state[0] = state[1] = 0.0
            continue

        formants = _VOWEL_FORMANTS[int(cycle / 0.18) % len(_VOWEL_FORMANTS)]
        pitch = f0 * (1.0 + 0.12 * math.sin(2 * math.pi * 0.9 * t))

        phase += pitch / sample_rate
        if phase >= 1.0:
            phase -= 1.0
            excitation = 1.0
        else:
            excitation = -0.06 * phase
        excitation += 0.03 * next(noise)

        sample = 0.0
        for slot, frequency in enumerate(formants):
            bandwidth = 70.0 + 30.0 * slot
            decay = math.exp(-math.pi * bandwidth / sample_rate)
            theta = 2 * math.pi * frequency / sample_rate
            state = states[slot]
            value = (
                excitation
                + 2 * decay * math.cos(theta) * state[0]
                - decay * decay * state[1]
            )
            state[1] = state[0]
            state[0] = value
            sample += value / (slot + 1.5)

        envelope = 0.5 + 0.5 * math.sin(2 * math.pi * 4.0 * t - math.pi / 2)
        sample *= envelope * 0.28
        frames += struct.pack("<h", max(-32768, min(32767, int(sample * 9000))))

    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(frames))
    return total / sample_rate


class no_network:  # noqa: N801 - used as a context manager, reads as a statement
    """Block every outbound connection primitive for the duration of the block.

    Records attempts rather than only refusing them, so a test can assert that the
    count is zero instead of assuming the absence of an exception means nothing tried.
    """

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self._saved: dict[str, Any] = {}

    @staticmethod
    def _target(args: tuple[Any, ...]) -> str:
        """Best-effort description of *what* was being reached.

        Recording only the primitive would say something was attempted without saying
        where to, which is the first question anyone asks when a run that should have
        made no outbound call did. The bound-method patches receive the socket as their
        first argument, so the first host-shaped argument wins rather than the first.
        """
        for value in args:
            if isinstance(value, str) and value:
                return value[:120]
            if isinstance(value, tuple) and value:
                return ":".join(str(part) for part in value[:2])[:120]
            url = getattr(value, "full_url", None)
            if isinstance(url, str) and url:
                return url[:120]
        return "unknown-target"

    def __enter__(self) -> no_network:
        import urllib.request

        def blocker(label: str) -> Callable[..., Any]:
            def _refuse(*args: Any, **_kwargs: Any) -> Any:
                target = self._target(args)
                self.attempts.append(f"{label} -> {target}")
                raise OSError(
                    f"outbound network call to {target} via {label} was blocked by the "
                    "offline smoke test"
                )

            return _refuse

        self._saved = {
            "socket.connect": socket.socket.connect,
            "socket.connect_ex": socket.socket.connect_ex,
            "socket.create_connection": socket.create_connection,
            "socket.getaddrinfo": socket.getaddrinfo,
            "urllib.urlopen": urllib.request.urlopen,
        }
        socket.socket.connect = blocker("socket.connect")  # type: ignore[method-assign]
        socket.socket.connect_ex = blocker("socket.connect_ex")  # type: ignore[method-assign]
        socket.create_connection = blocker("socket.create_connection")  # type: ignore[assignment]
        socket.getaddrinfo = blocker("socket.getaddrinfo")  # type: ignore[assignment]
        urllib.request.urlopen = blocker("urllib.urlopen")  # type: ignore[assignment]
        return self

    def __exit__(self, *_exc: Any) -> None:
        import urllib.request

        socket.socket.connect = self._saved["socket.connect"]  # type: ignore[method-assign]
        socket.socket.connect_ex = self._saved["socket.connect_ex"]  # type: ignore[method-assign]
        socket.create_connection = self._saved["socket.create_connection"]  # type: ignore[assignment]
        socket.getaddrinfo = self._saved["socket.getaddrinfo"]  # type: ignore[assignment]
        urllib.request.urlopen = self._saved["urllib.urlopen"]  # type: ignore[assignment]


WORKING_COPY_FORMAT = "16 kHz mono PCM16 WAV"


def describe_wav(path: Path) -> dict[str, Any]:
    """Format and provenance of a local WAV, without reading it into memory twice."""
    from mom_igd.asr.manifest import sha256_file

    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.getnframes()
    return {
        "channels": channels,
        "sample_width": width,
        "sample_rate": rate,
        "frames": frames,
        "seconds": (frames / rate) if rate else 0.0,
        "sha256": sha256_file(path),
    }


def _working_copy_complaint(described: dict[str, Any]) -> str | None:
    """Why this file is not a working copy, phrased so the reader can fix it."""
    problems: list[str] = []
    if described["channels"] != 1:
        problems.append(f"{described['channels']} channels (need 1)")
    if described["sample_width"] != 2:
        problems.append(f"{described['sample_width'] * 8}-bit samples (need 16-bit)")
    if described["sample_rate"] != WORKING_SAMPLE_RATE:
        problems.append(f"{described['sample_rate']} Hz (need {WORKING_SAMPLE_RATE})")
    if not problems:
        return None
    return (
        f"not a working copy: {', '.join(problems)}. Supply {WORKING_COPY_FORMAT} -- "
        "the smoke test deliberately does not convert the file, because resampling is "
        "a pipeline stage with its own provenance and not something a diagnostic should "
        "do silently to an operator's audio."
    )


def _claim(mode: str, ok: bool) -> str:
    """Exactly what a run of this mode does and does not establish.

    Derived from the outcome, never fixed text: a failing run that still printed "the
    speech path ran end to end" would be the most misleading line in the report.
    """
    if not ok:
        return (
            "nothing is established -- the run did not complete. See the failing "
            "step(s) above."
        )
    if mode == "synthetic":
        return (
            "the whole path ran on synthesised audio: model verified and loaded from a "
            "local directory, VAD found regions, the engine decoded them offline and "
            "produced validated word timings, and the model was released. Synthesised "
            "formants are not speech, so the transcript is meaningless as text and NO "
            "accuracy claim follows. Use --audio with a real recording for that path."
        )
    return (
        "the same path ran on a real local recording: VAD found regions and the engine "
        "produced words for them. Still no accuracy claim -- there is no reference "
        "transcript, and accuracy is never derived from the model's own output."
    )


def run_asr_smoke(
    config: Any,
    paths: Any,
    *,
    seconds: float = 8.0,
    audio_path: str | Path | None = None,
) -> dict[str, Any]:
    """Load the real local model and transcribe local audio, offline.

    Two modes, and the distinction is the whole point:

    * **synthetic** (default) -- deterministic generated tone. Proves the *plumbing*:
      the model resolves, verifies, loads from a local path, decodes, validates and
      releases, with every outbound primitive blocked. It proves nothing about speech
      recognition, because a tone is not speech.
    * **local-audio** (``audio_path``) -- an operator's own recording, already in
      working-copy format. Adds the one thing synthetic audio cannot: evidence that VAD
      finds real speech and the engine produces words for it.

    Neither mode measures accuracy. That needs a reference transcript, and a reference
    transcript needs the consent and licence metadata that ``asr bench --manifest``
    enforces. This function never computes a WER and never claims one.

    The supplied file is **read only**: never converted, never moved, never deleted, and
    neither its path nor any decoded text is logged.

    Returns a structured report. Never raises for an expected outcome -- a missing model
    is a reported failure with a clear reason, not a traceback, because that is the
    state a fresh machine is in.
    """
    steps: list[dict[str, Any]] = []
    error: str | None = None

    def record(name: str, ok: bool, detail: str) -> None:
        steps.append({"name": name, "ok": bool(ok), "detail": detail})

    from mom_igd.asr.faster_whisper_provider import (
        DEFAULT_COMPUTE_TYPE,
        FasterWhisperProvider,
        assert_offline_environment,
        resolve_model,
    )
    from mom_igd.asr.provider import ModelUnavailableError, SpeakerStatus
    from mom_igd.asr.vad import detect_speech_regions

    flags = assert_offline_environment()
    record("offline_flags", True, f"{len(flags)} offline environment flags set")

    try:
        resolved = resolve_model(paths.models_dir, role="pass1", deep=True)
    except ModelUnavailableError as exc:
        record("model_resolved", False, str(exc))
        return {
            "ok": False,
            "mode": "synthetic" if audio_path is None else "local-audio",
            "claim": _claim("", False),
            "steps": steps,
            "error": "MODEL_UNAVAILABLE",
            "detail": str(exc),
        }
    record(
        "model_resolved",
        True,
        f"{resolved.model_name}@{resolved.revision[:12]} verified "
        f"({resolved.manifest.total_bytes / 2**20:.0f} MiB, "
        f"manifest {resolved.digest[:16]}...)",
    )

    work = Path(paths.temp_dir) / "asr-smoke"
    work.mkdir(parents=True, exist_ok=True)

    supplied = Path(audio_path) if audio_path is not None else None
    mode = "synthetic" if supplied is None else "local-audio"
    generated = supplied is None
    audio = work / "smoke-16k-mono.wav" if generated else supplied

    if supplied is not None:
        if not supplied.is_file():
            record("audio_source", False, f"{supplied.name} does not exist")
            return {
                "ok": False,
                "mode": mode,
                "claim": _claim(mode, False),
                "steps": steps,
                "error": "AUDIO_UNAVAILABLE",
                "detail": "the supplied audio file does not exist",
            }
        try:
            described = describe_wav(supplied)
        except (wave.Error, EOFError, OSError) as exc:
            record("audio_source", False, f"{supplied.name} is not a readable WAV: {exc}")
            return {
                "ok": False,
                "mode": mode,
                "claim": _claim(mode, False),
                "steps": steps,
                "error": "AUDIO_UNREADABLE",
                "detail": "the supplied file could not be read as a WAV",
            }
        complaint = _working_copy_complaint(described)
        if complaint is not None:
            record("audio_source", False, complaint)
            return {
                "ok": False,
                "mode": mode,
                "claim": _claim(mode, False),
                "steps": steps,
                "error": "AUDIO_FORMAT",
                "detail": complaint,
            }
        record(
            "audio_source",
            True,
            f"operator-supplied {WORKING_COPY_FORMAT}, {described['seconds']:.1f}s, "
            f"sha256 {described['sha256'][:16]}... (read-only; the file is never "
            "converted, moved or deleted)",
        )

    try:
        if generated:
            duration = generate_formant_speech_wav(audio, seconds)
            record(
                "audio_generated",
                True,
                f"{duration:.1f}s deterministic 16 kHz mono PCM16, synthesised from a "
                "source-filter model (no human voice is recorded or sampled). It "
                "crosses the VAD threshold, so the region path is exercised -- but it "
                "is not speech, so its transcript is meaningless as text.",
            )
        else:
            duration = float(described["seconds"])

        # Regions are required in both modes. Zero would mean either that the audio is
        # unusable or that VAD is misconfigured, and in the synthetic case it would mean
        # the generator stopped producing speech-like structure -- all three are
        # failures worth surfacing rather than passing over.
        vad_result = detect_speech_regions(audio)
        bounded = all(
            0.0 <= region.start <= region.end <= vad_result.audio_seconds + 1e-6
            for region in vad_result.regions
        )
        ordered = all(
            vad_result.regions[i].end <= vad_result.regions[i + 1].start + 1e-6
            for i in range(len(vad_result.regions) - 1)
        )
        record(
            "vad_ran",
            vad_result.ran and bounded and ordered and bool(vad_result.regions),
            f"{len(vad_result.regions)} region(s), {vad_result.total_speech_seconds:.2f}s "
            f"speech of {duration:.1f}s ({vad_result.speech_ratio * 100:.0f}%); all "
            "bounded, ordered and non-overlapping. At least one region is required: "
            "none would mean the audio is unusable or VAD is misconfigured.",
        )
        record(
            "vad_provenance",
            bool(vad_result.model_sha256) and len(vad_result.model_sha256) == 64,
            f"{vad_result.model_name}, asset sha256 {vad_result.model_sha256[:16]}... "
            "(bundled in the wheel, never downloaded)",
        )

        provider = FasterWhisperProvider(
            resolved, compute_type=DEFAULT_COMPUTE_TYPE, cpu_threads=4
        )
        blocker = no_network()
        started = time.perf_counter()
        with blocker:
            info = provider.load()
            record(
                "model_loaded_offline",
                True,
                f"{info.model_name} {info.compute_type} in "
                f"{provider.load_seconds:.2f}s with outbound sockets blocked",
            )

            from mom_igd.asr.provider import SpeechRegion, TranscriptionRequest

            # Both modes decode the VAD regions, which is what the production pipeline
            # does. The earlier whole-file path existed only because a tone produced no
            # regions to decode.
            decode_regions: tuple[SpeechRegion, ...] = tuple(
                SpeechRegion(index=index, start=region.start, end=region.end)
                for index, region in enumerate(vad_result.regions)
            )
            result = provider.transcribe(
                TranscriptionRequest(
                    audio_path=str(audio),
                    regions=decode_regions,
                    language="id",
                    beam_size=1,
                )
            )
        elapsed = time.perf_counter() - started
        record(
            "transcribed_offline",
            result.processing_seconds > 0.0 and bool(result.segments),
            f"engine decoded {result.audio_seconds:.1f}s of audio in "
            f"{result.processing_seconds:.2f}s, producing {len(result.segments)} "
            f"validated segment(s); total {elapsed:.2f}s. This proves the plumbing, "
            "not accuracy -- no reference transcript is supplied to this command, and "
            "accuracy is never derived from the model's own output.",
        )
        record(
            "no_network_attempts",
            not blocker.attempts,
            "no outbound connection attempted"
            if not blocker.attempts
            else f"BLOCKED attempts: {sorted(set(blocker.attempts))}",
        )
        record(
            "no_speaker_assigned",
            all(
                s.speaker is None and s.speaker_status == SpeakerStatus.UNASSIGNED
                for s in result.segments
            ),
            "every segment reports speaker=None / UNASSIGNED, as Phase 4 must",
        )
        words = sum(len(s.words) for s in result.segments)
        record(
            "word_timestamps",
            words > 0,
            f"{words} word timestamp(s) produced and validated",
        )
        provider.close()
        record("model_released", not provider.loaded, "engine released after use")
    except Exception as exc:  # noqa: BLE001 - reported, never re-raised as a crash
        error = f"{type(exc).__name__}: {exc}"
        record("smoke", False, error)
    finally:
        # Only ever remove what this function created. An operator's recording is
        # theirs; deleting the file they asked us to read would be unforgivable.
        if generated:
            try:
                audio.unlink(missing_ok=True)
            except OSError:
                pass

    ok = all(step["ok"] for step in steps) and error is None
    _LOG.info("asr.smoke", extra={"ok": ok, "steps": len(steps), "mode": mode})
    return {
        "ok": ok,
        "mode": mode,
        "claim": _claim(mode, ok),
        "steps": steps,
        "error": error,
    }
