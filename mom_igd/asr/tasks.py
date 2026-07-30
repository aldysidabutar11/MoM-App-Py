"""Tasks that run **inside** the spawned worker process.

Kept in their own module for one reason: the child re-imports it by name under
``spawn``, and it must be importable without dragging in the API, the shell, or
anything that would make a worker start slowly. Every heavy import happens inside a
task body.

A task is a plain callable ``(payload: dict, cancelled: () -> bool) -> dict``. Both
sides are JSON-serialisable, so nothing that cannot cross a process boundary is ever
put in a payload -- no model handles, no open files, no numpy arrays.

**Cancellation is cooperative.** Every task checks ``cancelled()`` at a natural
boundary, which for transcription is between speech regions. The parent escalates to
termination only if a task ignores it, and a task that returns early reports what it
completed so the caller can distinguish "cancelled after 4 of 10 regions" from
"failed".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Final

__all__ = ["TASK_REGISTRY", "TaskCancelled"]

CancelCheck = Callable[[], bool]


class TaskCancelled(RuntimeError):
    """A task stopped because cancellation was requested. Not an error condition."""


def _resolve(payload: dict[str, Any], role: str):
    """Resolve and verify a model inside the worker.

    Verification happens in the child, not the parent: the parent may have checked
    minutes ago, and the child is the process that will actually load the weights.
    """
    from mom_igd.asr.faster_whisper_provider import resolve_model

    models_dir = Path(str(payload["models_dir"]))
    return resolve_model(models_dir, role=role, deep=bool(payload.get("deep_verify", False)))


def _transcribe(payload: dict[str, Any], cancelled: CancelCheck) -> dict[str, Any]:
    """Load a model, transcribe the requested regions, release it.

    Regions are transcribed one at a time so cancellation has a boundary to land on and
    so a single enormous decode cannot hold the whole meeting's memory at once.
    """
    from mom_igd.asr.faster_whisper_provider import (
        DEFAULT_COMPUTE_TYPE,
        FasterWhisperProvider,
        group_regions_into_windows,
    )
    from mom_igd.asr.provider import SpeechRegion, TranscriptionRequest

    role = str(payload.get("role", "pass1"))
    resolved = _resolve(payload, role)
    provider = FasterWhisperProvider(
        resolved,
        compute_type=str(payload.get("compute_type", DEFAULT_COMPUTE_TYPE)),
        cpu_threads=int(payload.get("cpu_threads", 0)),
    )

    # Optional egress recording, requested by the benchmark. Every outbound primitive is
    # intercepted for the duration of the decode and any attempt is *recorded*, so the
    # "zero network egress" line in a benchmark report is a measurement rather than a
    # claim. Off by default: the production pipeline must not depend on monkey-patching
    # the socket layer (ADR-0002), and this is an instrument, not a control.
    network_attempts: list[str] = []
    blocker = None
    if payload.get("record_network_attempts"):
        from mom_igd.asr.smoke import no_network

        blocker = no_network()
        blocker.__enter__()

    try:
        info = provider.load()
        regions_in = payload.get("regions") or []
        regions = tuple(
            SpeechRegion(
                index=int(entry["index"]),
                start=float(entry["start"]),
                end=float(entry["end"]),
            )
            for entry in regions_in
        )

        segments: list[dict[str, Any]] = []
        completed_regions = 0
        audio_seconds = 0.0
        processing_seconds = 0.0
        language = str(payload.get("language", "id"))
        language_probability: float | None = None
        was_cancelled = False

        # Regions are grouped into contiguous windows of at most 30 seconds, because
        # Whisper's encoder always consumes a 30-second window and pads it: decoding a
        # two-second region costs what decoding thirty seconds costs. One region per
        # call was measured at RTF 2.8 on a 24-second recording split into ten regions.
        #
        # The batch, not the region, is therefore the cancellation boundary. That is the
        # trade: at most 30 seconds of work is discarded on a cancel, in exchange for up
        # to a fifteen-fold reduction in decode cost.
        #
        # No regions means "the whole file": used by the benchmark and the smoke test.
        batches: list[tuple[SpeechRegion, ...]] = (
            [covered for _start, _end, covered in group_regions_into_windows(regions)]
            if regions
            else [()]
        )
        for batch in batches:
            if cancelled():
                was_cancelled = True
                break
            result = provider.transcribe(
                TranscriptionRequest(
                    audio_path=str(payload["audio_path"]),
                    regions=batch,
                    language=language,
                    initial_prompt=payload.get("initial_prompt"),
                    beam_size=int(payload.get("beam_size", 5)),
                    temperature=float(payload.get("temperature", 0.0)),
                    condition_on_previous_text=False,
                    word_timestamps=bool(payload.get("word_timestamps", True)),
                )
            )
            for segment in result.segments:
                entry = segment.to_dict()
                entry["index"] = len(segments)
                entry["asr_pass"] = int(payload.get("asr_pass", 1))
                segments.append(entry)
            audio_seconds += result.audio_seconds
            processing_seconds += result.processing_seconds
            language = result.language
            if result.language_probability is not None:
                language_probability = result.language_probability
            completed_regions += 1

        load_seconds = provider.load_seconds
        batch_count = len(batches)
    finally:
        # Release before the process exits, so a leak would show up as a rising peak
        # across repeated runs rather than being hidden by the exit.
        provider.close()
        if blocker is not None:
            network_attempts.extend(blocker.attempts)
            blocker.__exit__(None, None, None)

    # Built *after* the `finally`, deliberately. An earlier version returned from inside
    # the `try`, which froze `network_attempts` as an empty list before the blocker had
    # been drained -- so the field read as "zero egress" whatever the decode did. An
    # evidence field that cannot fail is worse than no field at all.
    return {
        "segments": segments,
        "model": info.to_dict(),
        "language": language,
        "language_probability": language_probability,
        "audio_seconds": round(audio_seconds, 3),
        "processing_seconds": round(processing_seconds, 3),
        "load_seconds": round(load_seconds, 3),
        "regions_requested": batch_count,
        "regions_completed": completed_regions,
        "cancelled": was_cancelled,
        "network_attempts": sorted(set(network_attempts)),
    }


def _vad(payload: dict[str, Any], cancelled: CancelCheck) -> dict[str, Any]:
    """Run VAD in the worker. Light, but isolated for the same provenance reasons."""
    from mom_igd.asr.vad import VadConfig, detect_speech_regions

    if cancelled():
        raise TaskCancelled("cancelled before VAD started")
    config_in = dict(payload.get("config") or {})
    config = VadConfig(
        **{
            key: config_in[key]
            for key in (
                "threshold",
                "min_speech_ms",
                "min_silence_ms",
                "speech_pad_ms",
                "merge_gap_ms",
                "max_region_seconds",
            )
            if key in config_in
        }
    )
    return detect_speech_regions(str(payload["audio_path"]), config).to_dict()


def _probe_directory(payload: dict[str, Any], cancelled: CancelCheck) -> dict[str, Any]:
    """Load one explicit, already-verified directory and decode a little audio.

    Used only by the provisioning probe, which has just promoted the directory and must
    prove the model runs *before* anything records it as ready. It therefore cannot go
    through the readiness-gated resolver -- that would be circular.
    """
    from mom_igd.asr.faster_whisper_provider import (
        DEFAULT_COMPUTE_TYPE,
        FasterWhisperProvider,
        resolve_verified_directory,
    )
    from mom_igd.asr.provider import TranscriptionRequest

    if cancelled():
        raise TaskCancelled("cancelled before the probe started")
    resolved = resolve_verified_directory(Path(str(payload["directory"])), deep=False)
    provider = FasterWhisperProvider(
        resolved,
        compute_type=str(payload.get("compute_type", DEFAULT_COMPUTE_TYPE)),
        cpu_threads=int(payload.get("cpu_threads", 4)),
    )
    try:
        info = provider.load()
        result = provider.transcribe(
            TranscriptionRequest(
                audio_path=str(payload["audio_path"]),
                regions=(),
                language=str(payload.get("language", "id")),
                beam_size=1,
                word_timestamps=True,
            )
        )
        return {
            "model": info.to_dict(),
            "load_seconds": round(provider.load_seconds, 3),
            "audio_seconds": round(result.audio_seconds, 3),
            "segments": len(result.segments),
        }
    finally:
        provider.close()


def _probe_model(payload: dict[str, Any], cancelled: CancelCheck) -> dict[str, Any]:
    """Load a model, report load cost and provenance, release it. No transcription.

    Used by the benchmark to separate model-load cost from decode cost, and by the
    readiness probe to prove a model can actually be constructed rather than merely
    existing on disk.
    """
    from mom_igd.asr.faster_whisper_provider import DEFAULT_COMPUTE_TYPE, FasterWhisperProvider

    resolved = _resolve(payload, str(payload.get("role", "pass1")))
    provider = FasterWhisperProvider(
        resolved,
        compute_type=str(payload.get("compute_type", DEFAULT_COMPUTE_TYPE)),
        cpu_threads=int(payload.get("cpu_threads", 0)),
    )
    try:
        info = provider.load()
        return {
            "model": info.to_dict(),
            "load_seconds": round(provider.load_seconds, 3),
            "health": provider.health(),
        }
    finally:
        provider.close()


#: The closed set of tasks a worker may run. A worker cannot be asked to execute
#: arbitrary code: the parent sends a task *name*, and an unknown name is refused.
TASK_REGISTRY: Final[dict[str, Callable[[dict[str, Any], CancelCheck], dict[str, Any]]]] = {
    "transcribe": _transcribe,
    "vad": _vad,
    "probe_model": _probe_model,
    "probe_directory": _probe_directory,
}
