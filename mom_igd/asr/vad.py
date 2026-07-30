"""Voice activity detection as an explicit, checkpointable stage.

**Why VAD is its own stage and not a flag on the ASR call.** faster-whisper can gate
internally, but then the regions it transcribed exist only inside that call: they cannot
be stored, reviewed, resumed or compared against a later run. Phase 4 has to persist
speech regions, because the pass-2 selection and every stored timestamp are expressed
relative to them, and because a resumed job must not re-run VAD it already completed.
So VAD runs here, its result is written down, and the ASR provider is told
``vad_filter=False`` so the two can never disagree about what was transcribed.

**The model is local by construction.** Silero VAD ships as
``assets/silero_vad_v6.onnx`` *inside* the faster-whisper wheel. There is no download,
no cache lookup and nothing to provision -- the artefact is part of an installed,
version-pinned dependency, and its hash is recorded alongside the stage result so a
wheel upgrade that changes the VAD is visible in the provenance.

**A silent recording is a legitimate result, not a failure.** Zero speech regions is
reported as zero speech regions. What must never happen is a VAD *error* being
presented as an empty transcript, so :class:`VadResult` distinguishes "ran and found
nothing" from "did not run".
"""

from __future__ import annotations

import hashlib
import math
import os
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from mom_igd.asr.provider import SpeechRegion
from mom_igd.logging_setup import get_logger

__all__ = [
    "VAD_MODEL_NAME",
    "onnx_provider_evidence",
    "VadConfig",
    "VadError",
    "VadResult",
    "detect_speech_regions",
    "vad_asset_digest",
]

_LOG = get_logger("asr.vad")

VAD_MODEL_NAME: Final[str] = "silero-vad-v6-bundled"


class VadError(RuntimeError):
    """VAD could not run. Distinct from "ran and found no speech"."""


@dataclass(frozen=True, slots=True)
class VadConfig:
    """Tunables, all recorded with the result so a rerun is comparable.

    The defaults are the ones the Phase 4A benchmark used. They are chosen for a
    meeting recorded from one microphone at the centre of a table, where a speaker
    pausing mid-sentence must not split into two regions that each lose their context.
    """

    threshold: float = 0.5
    #: Anything shorter is not a turn: it is a cough, a chair, or a keyboard.
    min_speech_ms: int = 250
    #: A pause shorter than this stays inside one region. Half a second is roughly a
    #: breath; splitting there would cut a sentence in half and cost the ASR its
    #: context.
    min_silence_ms: int = 500
    #: Padding on each side, so a region never clips the first or last phoneme.
    speech_pad_ms: int = 200
    #: Regions separated by less than this are merged after padding. Two adjacent
    #: regions with a 60 ms gap are one utterance as far as the engine is concerned,
    #: and merging them avoids paying the model's fixed per-call cost twice.
    merge_gap_ms: int = 150
    #: A single region longer than this is split, so one long monologue cannot become
    #: a single enormous decode that blows the memory budget.
    max_region_seconds: float = 30.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "min_speech_ms": self.min_speech_ms,
            "min_silence_ms": self.min_silence_ms,
            "speech_pad_ms": self.speech_pad_ms,
            "merge_gap_ms": self.merge_gap_ms,
            "max_region_seconds": self.max_region_seconds,
        }

    @property
    def config_hash(self) -> str:
        """Stable digest of the configuration, for checkpoint invalidation."""
        payload = ";".join(f"{k}={v}" for k, v in sorted(self.to_dict().items()))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VadResult:
    """The speech regions of one working copy, plus how they were produced."""

    regions: tuple[SpeechRegion, ...]
    audio_seconds: float
    model_name: str
    model_sha256: str
    config: VadConfig
    ran: bool = True
    merged_count: int = 0
    split_count: int = 0
    dropped_short_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def total_speech_seconds(self) -> float:
        return sum(region.duration for region in self.regions)

    @property
    def speech_ratio(self) -> float:
        if self.audio_seconds <= 0:
            return 0.0
        return self.total_speech_seconds / self.audio_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "regions": [r.to_dict() for r in self.regions],
            "region_count": len(self.regions),
            "audio_seconds": round(self.audio_seconds, 3),
            "total_speech_seconds": round(self.total_speech_seconds, 3),
            "speech_ratio": round(self.speech_ratio, 4),
            "model_name": self.model_name,
            "model_sha256": self.model_sha256,
            "config": self.config.to_dict(),
            "config_hash": self.config.config_hash,
            "merged_count": self.merged_count,
            "split_count": self.split_count,
            "dropped_short_count": self.dropped_short_count,
        }


def _bundled_asset() -> Path:
    """Locate the Silero ONNX asset inside the installed faster-whisper wheel."""
    try:
        import faster_whisper
    except Exception as exc:  # noqa: BLE001
        raise VadError(
            f"faster-whisper is not importable ({type(exc).__name__}), so the bundled "
            "Silero VAD asset cannot be located. Install the runtime requirements."
        ) from None
    root = Path(faster_whisper.__file__).parent
    candidates = sorted(root.glob("assets/*.onnx"))
    if not candidates:
        raise VadError(
            f"no ONNX asset found under {root / 'assets'}. The installed "
            "faster-whisper does not bundle a VAD model, so VAD cannot run offline."
        )
    # Prefer the newest versioned asset when several ship.
    return candidates[-1]


def vad_asset_digest() -> tuple[str, str]:
    """Return ``(filename, sha256)`` of the bundled VAD asset.

    Recorded with every VAD result: upgrading the wheel can change the VAD, and a
    stored region set must say which model produced it.
    """
    asset = _bundled_asset()
    digest = hashlib.sha256()
    with open(asset, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return asset.name, digest.hexdigest()


#: Execution providers that must never run a model in this application. They are not
#: proven to make a network call by their mere presence -- the point is that a local,
#: offline product has no reason to dispatch to a remote or vendor-managed backend, so
#: anything other than plain CPU is refused rather than argued about.
_FORBIDDEN_PROVIDERS: Final[frozenset[str]] = frozenset(
    {
        "AzureExecutionProvider",
        "CUDAExecutionProvider",
        "TensorrtExecutionProvider",
        "OpenVINOExecutionProvider",
        "DmlExecutionProvider",
    }
)


def onnx_provider_evidence() -> dict[str, Any]:
    """What execution providers exist, and what the VAD session actually uses.

    Reported as evidence rather than asserted as belief: the interesting number is the
    *session's* provider list, not the runtime's advertised capability list.
    """
    evidence: dict[str, Any] = {"available": [], "session": [], "ok": False}
    try:
        import onnxruntime

        evidence["available"] = list(onnxruntime.get_available_providers())
    except Exception as exc:  # noqa: BLE001
        evidence["error"] = f"{type(exc).__name__}"
        return evidence
    try:
        from faster_whisper.vad import get_vad_model

        model = get_vad_model()
        for attribute in dir(model):
            if "session" not in attribute.lower():
                continue
            session = getattr(model, attribute, None)
            getter = getattr(session, "get_providers", None)
            if callable(getter):
                evidence["session"] = list(getter())
                break
    except Exception as exc:  # noqa: BLE001
        evidence["error"] = f"{type(exc).__name__}"
        return evidence
    used = set(evidence["session"])
    evidence["ok"] = bool(used) and not (used & _FORBIDDEN_PROVIDERS)
    return evidence


def _assert_cpu_execution_provider() -> None:
    """Refuse to run VAD on anything but CPU."""
    evidence = onnx_provider_evidence()
    session_providers = set(evidence.get("session") or ())
    forbidden = sorted(session_providers & _FORBIDDEN_PROVIDERS)
    if forbidden:
        raise VadError(
            f"the VAD session selected {forbidden}, which this application does not "
            "permit. Only CPUExecutionProvider is allowed: the product runs fully "
            "offline on CPU and must not dispatch a model to a vendor-managed or GPU "
            "backend that has not been benchmarked or approved."
        )
    if not session_providers:
        # Not fatal on its own -- the attribute name could change upstream -- but it
        # means the guarantee is unverified, and that must be visible.
        _LOG.warning(
            "asr.vad.provider_unverified",
            extra={"available": evidence.get("available")},
        )


def _read_wav_mono16(path: Path) -> tuple[list[float], int, float]:
    """Read a 16 kHz mono PCM16 WAV into normalised floats, in bounded blocks.

    The working copy is by construction 16 kHz mono PCM16, so this deliberately does
    not accept anything else: a surprise format here means the normalisation stage did
    not run, and silently coping would hide that.
    """
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.getnframes()
        if channels != 1 or width != 2:
            raise VadError(
                f"{path.name} is {channels}ch/{width * 8}-bit; VAD requires the "
                "16 kHz mono PCM16 working copy. Run the normalisation stage first."
            )
        raw = handle.readframes(frames)
    import array

    samples = array.array("h")
    samples.frombytes(raw)
    scale = 1.0 / 32768.0
    return [s * scale for s in samples], rate, frames / rate if rate else 0.0


def _merge_and_bound(
    intervals: list[tuple[float, float]], config: VadConfig, audio_seconds: float
) -> tuple[list[tuple[float, float]], int, int, int]:
    """Pad, clamp, merge and split raw intervals into final regions.

    Order matters and is deliberate: pad first (so a merge decision is made on the
    padded geometry the engine will actually see), clamp to the audio bounds (so
    padding can never produce a negative start or an end past the file), then merge
    near-adjacent regions, then split anything too long.
    """
    pad = config.speech_pad_ms / 1000.0
    gap = config.merge_gap_ms / 1000.0
    min_speech = config.min_speech_ms / 1000.0

    dropped = 0
    padded: list[tuple[float, float]] = []
    for start, end in intervals:
        if end - start < min_speech:
            dropped += 1
            continue
        # Clamp on both sides: padding must never create a negative timestamp or run
        # past the end of the audio, because every stored timestamp maps back to the
        # master recording.
        padded.append((max(0.0, start - pad), min(audio_seconds, end + pad)))

    merged: list[tuple[float, float]] = []
    merge_count = 0
    for start, end in sorted(padded):
        if merged and start - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            merge_count += 1
            continue
        merged.append((start, end))

    bounded: list[tuple[float, float]] = []
    split_count = 0
    limit = max(1.0, config.max_region_seconds)
    for start, end in merged:
        span = end - start
        if span <= limit:
            bounded.append((start, end))
            continue
        pieces = int(math.ceil(span / limit))
        step = span / pieces
        for piece in range(pieces):
            piece_start = start + piece * step
            bounded.append((piece_start, min(end, piece_start + step)))
        split_count += pieces - 1
    return bounded, merge_count, split_count, dropped


def detect_speech_regions(
    audio_path: str | os.PathLike[str], config: VadConfig | None = None
) -> VadResult:
    """Run Silero VAD over a 16 kHz mono working copy.

    Returns a :class:`VadResult` with zero regions for genuinely silent audio; raises
    :class:`VadError` when VAD could not run at all. Those two outcomes must stay
    distinguishable: a silent meeting is a valid transcript, a broken VAD is not.
    """
    settings = config or VadConfig()
    path = Path(audio_path)
    if not path.is_file():
        raise VadError(f"{path.name} does not exist; nothing to segment")

    samples, rate, audio_seconds = _read_wav_mono16(path)
    if rate != 16_000:
        raise VadError(
            f"{path.name} is {rate} Hz; VAD requires 16 kHz. Run normalisation first."
        )
    asset_name, asset_digest = vad_asset_digest()

    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
    except Exception as exc:  # noqa: BLE001
        raise VadError(
            f"the bundled VAD is not importable ({type(exc).__name__})"
        ) from None

    # Assignment, not setdefault: an operator environment carrying
    # ORT_DISABLE_ALL_TELEMETRY=0 must not be able to turn telemetry back on. A
    # setdefault here would silently honour whatever the environment already said,
    # which is the opposite of a guarantee.
    os.environ["ORT_DISABLE_ALL_TELEMETRY"] = "1"

    # Verify, rather than trust, that the session is on CPU. This ONNX build also
    # advertises an `AzureExecutionProvider`; faster-whisper currently pins
    # `CPUExecutionProvider` itself (vad.py), and this check is what would catch a
    # wheel upgrade that stopped doing so.
    _assert_cpu_execution_provider()

    import numpy as np

    audio = np.asarray(samples, dtype=np.float32)
    try:
        options = VadOptions(
            threshold=settings.threshold,
            min_speech_duration_ms=settings.min_speech_ms,
            min_silence_duration_ms=settings.min_silence_ms,
            max_speech_duration_s=max(1.0, settings.max_region_seconds),
            # Padding is applied and clamped in `_merge_and_bound`, so the library
            # must not also pad: doing it twice would push a region past the end of
            # the audio and break the mapping back to the master recording.
            speech_pad_ms=0,
        )
    except TypeError as exc:
        # A configuration that cannot be applied is an error, never a quiet fallback
        # to library defaults. Silently ignoring the operator's thresholds is how a
        # tuned VAD becomes an untuned one without anybody noticing.
        raise VadError(
            "the installed faster-whisper exposes a different VadOptions signature "
            f"({exc}). Refusing to run VAD with default settings that were not "
            "requested; the pinned dependency version and VadConfig must agree."
        ) from None

    try:
        raw = get_speech_timestamps(audio, options, sampling_rate=rate)
    except Exception as exc:  # noqa: BLE001
        raise VadError(f"VAD failed to run ({type(exc).__name__}: {exc})") from None

    intervals = [
        (float(chunk["start"]) / rate, float(chunk["end"]) / rate) for chunk in raw
    ]
    bounded, merged, split, dropped = _merge_and_bound(intervals, settings, audio_seconds)
    regions = tuple(
        SpeechRegion(index=index, start=start, end=end)
        for index, (start, end) in enumerate(bounded)
    )
    result = VadResult(
        regions=regions,
        audio_seconds=audio_seconds,
        model_name=VAD_MODEL_NAME,
        model_sha256=asset_digest,
        config=settings,
        ran=True,
        merged_count=merged,
        split_count=split,
        dropped_short_count=dropped,
        extra={"asset": asset_name},
    )
    _LOG.info(
        "asr.vad",
        extra={
            "regions": len(regions),
            "speech_seconds": round(result.total_speech_seconds, 2),
            "audio_seconds": round(audio_seconds, 2),
        },
    )
    return result
