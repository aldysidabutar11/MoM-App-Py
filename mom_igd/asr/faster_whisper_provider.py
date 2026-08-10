"""The production ASR provider: faster-whisper / CTranslate2, CPU INT8.

Chosen by the Phase 4A benchmark on this machine (ADR-0014). Everything about how it
is wired here follows from three constraints.

**Fail closed.** :func:`resolve_model` finds a promoted model directory, reads its
manifest, and verifies it before the engine is constructed. No verified directory means
:class:`ModelUnavailableError` -- never a download, never a fall back to whichever other
model happens to be on disk. Loading a different model than the one recorded against a
transcript would make its provenance a lie.

**Never reach the network.** ``local_files_only=True`` is passed to ``WhisperModel``,
and the model is addressed by absolute local path rather than by a hub id, so there is
nothing for the library to resolve remotely. :func:`assert_offline_environment` also
sets the offline flags the underlying libraries honour, so even a future code path that
tried would be refused.

**Cost nothing until used.** ``ctranslate2`` and ``faster_whisper`` are imported inside
:meth:`FasterWhisperProvider.load`. Importing this module is free, which is what lets
``doctor``, the CLI and the API health endpoint stay light.

A note on the ONNX runtime this pulls in: its build on this machine advertises an
``AzureExecutionProvider`` alongside ``CPUExecutionProvider``. Its mere presence in the
capability list is **not** evidence that anything reaches a network. What is checked is
the *session's* provider list: ``mom_igd.asr.vad`` verifies the live VAD session reports
``CPUExecutionProvider`` and refuses to run otherwise. Measured on this machine, it does.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Final, Sequence

from mom_igd.asr.manifest import ManifestError, ModelManifest, manifest_digest, verify_directory
from mom_igd.asr.provider import (
    AsrModelInfo,
    ModelUnavailableError,
    ProviderError,
    SpeechRegion,
    TranscriptSegment,
    TranscriptionRequest,
    TranscriptionResult,
    Word,
    validate_transcription,
)
from mom_igd.asr.installed import load_index
from mom_igd.asr.provision import MODEL_CATALOGUE, promoted_models
from mom_igd.logging_setup import get_logger

__all__ = [
    "DEFAULT_COMPUTE_TYPE",
    "FasterWhisperProvider",
    "ResolvedModel",
    "assert_offline_environment",
    "offline_environment_evidence",
    "resolve_model",
    "resolve_verified_directory",
]

_LOG = get_logger("asr.provider")

#: The compute type the Phase 4A benchmark selected. INT8 is the only profile that
#: keeps the pass-2 model inside the resident-memory budget on a 16 GB machine.
DEFAULT_COMPUTE_TYPE: Final[str] = "int8"

#: The working-copy format the normalisation stage produces, and the only one the
#: fast read path accepts. Whisper's feature extractor wants exactly this.
WORKING_SAMPLE_RATE: Final[int] = 16_000
WORKING_CHANNELS: Final[int] = 1

#: Environment flags the underlying libraries honour to refuse network access. Set
#: before any of them is imported, so a stray code path cannot reach out.
_OFFLINE_FLAGS: Final[dict[str, str]] = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
    "HF_HUB_DISABLE_XET": "1",
    # Disable the transfer accelerator too: it opens its own connections, and an
    # offline runtime has nothing for it to accelerate.
    "HF_HUB_ENABLE_HF_TRANSFER": "0",
}

#: Credential variables scrubbed from a runtime process.
#:
#: With ``HF_HUB_DISABLE_IMPLICIT_TOKEN=1`` an inherited token would not be used
#: implicitly, but leaving a credential in the environment of a process that has no
#: business authenticating is gratuitous: it survives into crash dumps, child
#: processes and diagnostics. The runtime never authenticates to anything, so the
#: honest state is "no credential present".
#:
#: Provisioning deliberately does **not** call this -- and does not need to, because
#: every catalogue artefact is public and ungated.
_CREDENTIAL_VARIABLES: Final[tuple[str, ...]] = (
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "HUGGINGFACEHUB_API_TOKEN",
)


def assert_offline_environment() -> dict[str, str]:
    """Put this process into offline mode and return the flags that were set.

    Called before the heavy libraries are imported. Uses **assignment, never
    ``setdefault``**: an operator environment carrying ``HF_HUB_OFFLINE=0`` must not be
    able to put a worker online, and ``setdefault`` would silently honour it. Idempotent,
    and called again inside each spawned worker because a child does not inherit an
    in-process import.
    """
    for key, value in _OFFLINE_FLAGS.items():
        os.environ[key] = value
    for name in _CREDENTIAL_VARIABLES:
        os.environ.pop(name, None)
    return dict(_OFFLINE_FLAGS)


def offline_environment_evidence() -> dict[str, Any]:
    """What the offline posture of *this* process actually is, for a report."""
    return {
        "flags": {key: os.environ.get(key) for key in sorted(_OFFLINE_FLAGS)},
        "flags_enforced": all(
            os.environ.get(key) == value for key, value in _OFFLINE_FLAGS.items()
        ),
        "credentials_present": sorted(
            name for name in _CREDENTIAL_VARIABLES if name in os.environ
        ),
    }


class ResolvedModel:
    """A model directory that has been found *and verified*.

    There is no constructor that takes a path without verifying it. That is
    deliberate: the only way to get one of these is through :func:`resolve_model`,
    which means a caller cannot accidentally hand the engine an unverified directory.
    """

    __slots__ = ("directory", "manifest", "digest", "role")

    def __init__(self, directory: Path, manifest: ModelManifest, digest: str) -> None:
        self.directory = directory
        self.manifest = manifest
        self.digest = digest
        self.role = str(manifest.extra.get("role") or "")

    @property
    def model_name(self) -> str:
        return self.manifest.model_name

    @property
    def revision(self) -> str:
        return self.manifest.revision

    def describe(self) -> dict[str, Any]:
        """Provenance without the path: safe for an API response or a log line."""
        return {
            "model_name": self.model_name,
            "revision": self.revision,
            "manifest_sha256": self.digest,
            "license": self.manifest.license_name,
            "hardware_profile": self.manifest.hardware_profile,
            "role": self.role,
            "source_repo": self.manifest.source_repo,
            "total_bytes": self.manifest.total_bytes,
        }


def resolve_verified_directory(directory: Path, *, deep: bool = False) -> ResolvedModel:
    """Verify one specific model directory and wrap it. **Provisioning probe only.**

    This deliberately bypasses the installed-model registry, because it is the function
    that *establishes* a registry entry: the provisioning probe has just promoted a
    directory and needs to prove the model loads before anything is recorded as ready.
    Going through :func:`resolve_model` there would be circular -- it would demand the
    readiness record the probe exists to create.

    It is not a back door. The manifest is still verified before the engine is
    constructed, and the only caller is provisioning, which derives the directory itself
    rather than accepting it from an operator or a request. The runtime path is
    :func:`resolve_model` and nothing else.
    """
    try:
        manifest = verify_directory(directory, deep=deep)
    except ManifestError as exc:
        raise ModelUnavailableError(f"MODEL_UNAVAILABLE: {exc}") from None
    return ResolvedModel(directory, manifest, manifest_digest(manifest))


def resolve_model(
    models_dir: Path,
    *,
    role: str = "pass1",
    deep: bool = False,
    expected_digest: str | None = None,
) -> ResolvedModel:
    """Find and verify the promoted model for ``role``. Fails closed.

    ``deep=False`` verifies presence and size, which is what a load should cost;
    ``deep=True`` re-hashes every byte and is what ``asr verify`` and provisioning do.
    Either way, an unverifiable model raises rather than loading.

    When several revisions of the same role are present the newest is used, and the
    choice is logged -- an operator who provisioned twice should be able to see which
    one ran.
    """
    catalogue_names = {
        spec.model_name: spec for spec in MODEL_CATALOGUE.values() if spec.role == role
    }
    if not catalogue_names:
        raise ModelUnavailableError(f"no catalogue model declares role={role!r}")

    # The installed-model registry, NOT a directory scan. A directory that verifies
    # byte-for-byte can still be unusable -- that is exactly what a missing
    # `preprocessor_config.json` produced -- so readiness is only ever the recorded
    # verdict of a load-and-decode probe.
    index = load_index(models_dir)
    if not index.readable:
        raise ModelUnavailableError(
            f"MODEL_UNAVAILABLE: the installed-model registry cannot be trusted "
            f"({index.problem}). No model is treated as ready. Re-run "
            "`python -m mom_igd asr provision` to rebuild it."
        )

    ready = [
        entry
        for entry in index.ready(models_dir, role=role)
        # Exact role match only, and the name must still be in the approved catalogue:
        # a registry edited to name some other model must not get it loaded.
        if entry.model_name in catalogue_names
    ]
    if not ready:
        wanted = ", ".join(sorted(catalogue_names))
        present = [
            f"{c['model_name']}@{c['revision'][:12]}"
            f"{'' if c.get('ok') else ' (fails verification)'}"
            for c in promoted_models(models_dir)
        ]
        raise ModelUnavailableError(
            f"MODEL_UNAVAILABLE: no model is installed AND probe-verified for "
            f"role={role!r} (expected one of: {wanted}). On disk: "
            f"{present or 'nothing'}. Provision with "
            "`python -m mom_igd asr provision`. Transcription never downloads a model "
            "and never falls back to a different role -- a broken pass-1 does not "
            "become pass-2."
        )
    if len(ready) > 1:
        # Newest probe wins, and the choice is logged: an operator who provisioned
        # twice must be able to see which revision actually ran.
        ready.sort(key=lambda entry: entry.probed_at, reverse=True)
        _LOG.warning(
            "asr.model.multiple_revisions",
            extra={
                "role": role,
                "count": len(ready),
                "chosen": ready[0].revision[:12],
            },
        )
    chosen = ready[0]
    directory = models_dir / chosen.relative_path
    try:
        # Re-verify at load time even though the index says it passed. The index records
        # a past verdict; this proves the bytes are still the ones that earned it.
        manifest = verify_directory(
            directory,
            expected_digest=expected_digest or chosen.manifest_sha256,
            deep=deep,
        )
    except ManifestError as exc:
        raise ModelUnavailableError(f"MODEL_UNAVAILABLE: {exc}") from None
    return ResolvedModel(directory, manifest, manifest_digest(manifest))


class FasterWhisperProvider:
    """Production ASR provider. One model, loaded on demand, released on close.

    Not thread-safe by design: exactly one heavy model may be resident, in one
    short-lived worker process, so there is no case where two threads should be
    driving the same engine.
    """

    provider_id: Final[str] = "faster-whisper/ctranslate2"

    def __init__(
        self,
        resolved: ResolvedModel,
        *,
        compute_type: str = DEFAULT_COMPUTE_TYPE,
        cpu_threads: int = 0,
        num_workers: int = 1,
    ) -> None:
        self._resolved = resolved
        self._compute_type = str(compute_type)
        # 0 lets CTranslate2 pick, which is what the benchmark measured as the
        # baseline. An explicit count is what the thread sweep varies.
        self._cpu_threads = max(0, int(cpu_threads))
        self._num_workers = max(1, int(num_workers))
        self._model: Any = None
        self._info: AsrModelInfo | None = None
        self._load_seconds: float = 0.0
        # One working copy, held across calls. The pipeline calls `transcribe` once per
        # 30-second window, and re-reading the file each time cost 18 minutes of pure
        # waste on a 90-minute meeting -- more than the decode itself. See `_audio_for`.
        self._audio: _LoadedAudio | None = None

    # -- lifecycle ----------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def info(self) -> AsrModelInfo:
        if self._info is None:
            raise ProviderError("provider.info is only available after load()")
        return self._info

    @property
    def load_seconds(self) -> float:
        return self._load_seconds

    def load(self) -> AsrModelInfo:
        """Construct the engine from the verified local directory."""
        if self._model is not None and self._info is not None:
            return self._info

        assert_offline_environment()
        started = time.perf_counter()
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # noqa: BLE001 - a missing wheel is a real outcome
            raise ModelUnavailableError(
                "MODEL_UNAVAILABLE: faster-whisper is not importable "
                f"({type(exc).__name__}). Install the runtime requirements; the "
                "application does not install dependencies for you."
            ) from None

        try:
            self._model = WhisperModel(
                str(self._resolved.directory),
                device="cpu",
                compute_type=self._compute_type,
                cpu_threads=self._cpu_threads,
                num_workers=self._num_workers,
                # The model is a local directory, so there is nothing to resolve
                # remotely -- but say so explicitly, so a future refactor that passed
                # a hub id would fail rather than fetch.
                local_files_only=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise ModelUnavailableError(
                f"MODEL_UNAVAILABLE: {self._resolved.model_name}@"
                f"{self._resolved.revision[:12]} could not be loaded "
                f"({type(exc).__name__}). The artefact verified, so this is an engine "
                "or compute-type problem rather than a corrupt download."
            ) from None
        self._load_seconds = time.perf_counter() - started

        self._info = AsrModelInfo(
            model_name=self._resolved.model_name,
            revision=self._resolved.revision,
            manifest_sha256=self._resolved.digest,
            compute_type=self._compute_type,
            cpu_threads=self._cpu_threads,
            provider_id=self.provider_id,
            is_test_double=False,
        )
        _LOG.info(
            "asr.model.loaded",
            extra={
                "model": self._info.model_name,
                "revision": self._info.revision[:12],
                "compute_type": self._compute_type,
                "cpu_threads": self._cpu_threads,
                "load_ms": round(self._load_seconds * 1000),
            },
        )
        return self._info

    def close(self) -> None:
        """Release the engine and the audio. Idempotent, and never raises.

        Dropping the reference is what returns the memory; the worker process exits
        immediately afterwards, which is the actual guarantee that the operating
        system reclaims it.
        """
        self._model = None
        self._audio = None
        self._load_seconds = 0.0

    def _audio_for(self, path: Path) -> _LoadedAudio:
        """The working copy, read from disk at most once per provider.

        **Why this cache exists.** `transcribe` is called once per 30-second window, and
        it used to read and convert the whole file on every call. Measured on a working
        copy of a 90-minute meeting: 144 windows x 7.6 s = **18 minutes of pure waste**,
        against a pass-1 decode of about 13 minutes -- the overhead was larger than the
        work. It did not show up on the 24-second end-to-end test, which produces exactly
        one window.

        Keyed on the file's identity rather than only its path, so a cache can never
        serve the wrong audio: that would produce a transcript which looks entirely
        plausible and belongs to a different meeting.
        """
        fingerprint = _audio_fingerprint(path)
        cached = self._audio
        if cached is not None and cached.fingerprint == fingerprint:
            return cached
        # Release the previous array before allocating the next one. On a 2.5 GB budget
        # holding two working copies at once is the difference between fitting and not.
        self._audio = None
        loaded = _load_working_copy(path, fingerprint=fingerprint)
        self._audio = loaded
        _LOG.info(
            "asr.audio.loaded",
            extra={"seconds": round(loaded.seconds, 1), "resident_mib": round(loaded.nbytes / (1 << 20), 1)},
        )
        return loaded

    def health(self) -> dict[str, Any]:
        """Readiness detail. No paths, no transcript, no key material."""
        return {
            "provider_id": self.provider_id,
            "loaded": self.loaded,
            "compute_type": self._compute_type,
            "cpu_threads": self._cpu_threads,
            "model": self._resolved.describe(),
            "offline_flags": sorted(_OFFLINE_FLAGS),
        }

    # -- transcription ------------------------------------------------------

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        """Transcribe the requested speech regions of a working copy.

        **The audio is read once per provider and then sliced.** Two versions of this
        were wrong. The first passed the file path plus ``clip_timestamps`` once per
        region, so the library re-read the whole file every time. The second read it once
        per ``transcribe`` call -- which the pipeline makes once per 30-second window, so
        a 90-minute meeting still read the file 144 times. Both are O(windows x duration)
        and neither shows up on a 24-second test, which produces one window. The working
        copy is now held on the provider (:meth:`_audio_for`) for as long as it is needed.

        Regions are still decoded one at a time, which is deliberate -- it bounds peak
        memory inside the engine and gives cancellation a boundary to land on.

        Timestamps come back relative to the **working copy**, not to the slice, because
        that is the frame of reference every downstream stage and every stored row uses.
        The region offset is added here, once, rather than being re-derived by each
        reader.
        """
        if self._model is None:
            self.load()
        assert self._model is not None  # noqa: S101 - load() raises otherwise

        audio_path = Path(request.audio_path)
        if not audio_path.is_file():
            raise ProviderError(
                "the working copy to transcribe does not exist. The normalisation "
                "stage must run first."
            )

        segments: list[TranscriptSegment] = []
        language = request.language
        language_probability: float | None = None
        started = time.perf_counter()
        audio_seconds = 0.0
        next_index = 0

        audio = self._audio_for(audio_path)
        total_seconds = audio.seconds
        windows: list[tuple[float, float, tuple[SpeechRegion, ...]]]
        if request.regions:
            windows = group_regions_into_windows(request.regions)
        else:
            # No VAD result: transcribe the whole file. Used by the smoke test and by
            # the benchmark, never by the pipeline, which always runs VAD first.
            windows = [(0.0, total_seconds, ())]

        for start_raw, end_raw, covered in windows:
            start = max(0.0, min(start_raw, total_seconds))
            end = max(start, min(end_raw, total_seconds))
            clip = audio.window(
                int(round(start * WORKING_SAMPLE_RATE)),
                int(round(end * WORKING_SAMPLE_RATE)),
            )
            if len(clip) == 0:
                continue
            kwargs: dict[str, Any] = {
                "language": request.language,
                "task": "transcribe",
                "beam_size": request.beam_size,
                "temperature": request.temperature,
                "word_timestamps": request.word_timestamps,
                # Off on purpose: conditioning on previous text is the main cause of
                # runaway repetition loops, and a meeting transcript must not invent
                # a paragraph because one segment was noisy.
                "condition_on_previous_text": request.condition_on_previous_text,
                # VAD has already run as its own checkpointed stage; letting the
                # engine re-gate would make the stored speech regions and the
                # transcribed audio disagree.
                "vad_filter": False,
            }
            if request.initial_prompt:
                kwargs["initial_prompt"] = request.initial_prompt

            try:
                raw_segments, transcription_info = self._model.transcribe(clip, **kwargs)
                emitted = list(raw_segments)
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(
                    f"transcription failed ({type(exc).__name__}). No transcript text "
                    "is included in this message by policy."
                ) from None

            language = getattr(transcription_info, "language", language) or language
            probability = getattr(transcription_info, "language_probability", None)
            if probability is not None:
                language_probability = float(probability)
            audio_seconds += end - start

            for item in emitted:
                words: list[Word] = []
                for word in getattr(item, "words", None) or ():
                    words.append(
                        Word(
                            text=str(getattr(word, "word", "")),
                            # Clamped to the window as well as offset: the engine can
                            # report a word a few milliseconds past the end of a clip,
                            # and a word outside its parent segment is refused by
                            # validation -- correctly, so it is kept inside here.
                            start=_shift(getattr(word, "start", 0.0), start, end),
                            end=_shift(getattr(word, "end", 0.0), start, end),
                            probability=_optional_float(
                                getattr(word, "probability", None)
                            ),
                        )
                    )
                segment_start = _shift(item.start, start, end)
                segment_end = _shift(item.end, start, end)
                segments.append(
                    TranscriptSegment(
                        index=next_index,
                        start=segment_start,
                        end=segment_end,
                        text=str(item.text).strip(),
                        words=tuple(words),
                        avg_logprob=_optional_float(getattr(item, "avg_logprob", None)),
                        no_speech_prob=_optional_float(
                            getattr(item, "no_speech_prob", None)
                        ),
                        compression_ratio=_optional_float(
                            getattr(item, "compression_ratio", None)
                        ),
                        temperature=_optional_float(getattr(item, "temperature", None)),
                        asr_pass=1,
                        region_index=attribute_to_region(
                            segment_start, segment_end, covered
                        ),
                    )
                )
                next_index += 1

        result = TranscriptionResult(
            segments=tuple(segments),
            model=self.info,
            language=language,
            language_probability=language_probability,
            audio_seconds=audio_seconds,
            processing_seconds=time.perf_counter() - started,
            extra={"load_seconds": round(self._load_seconds, 3)},
        )
        # Validate before it leaves the provider: nothing downstream re-checks.
        return validate_transcription(result)


#: Whisper's fixed analysis window. The encoder always consumes 30 seconds of mel
#: frames, padding with silence when the audio is shorter -- so decoding a 2-second
#: region costs almost exactly what decoding 30 seconds costs.
WHISPER_WINDOW_SECONDS: Final[float] = 30.0


def group_regions_into_windows(
    regions: Sequence[SpeechRegion],
) -> list[tuple[float, float, tuple[SpeechRegion, ...]]]:
    """Batch consecutive regions into contiguous spans of at most 30 seconds.

    **This is the difference between meeting the real-time target and missing it by an
    order of magnitude.** Whisper pads every window to 30 seconds, so one decode per
    region means a meeting of two-second utterances costs fifteen times its own duration.
    Measured on a 24-second recording split into ten regions: RTF 2.8 decoding
    region-by-region. Batching them into 30-second spans collapses that to a handful of
    decodes.

    The span is **contiguous in the working copy**: from the first region's start to the
    last one's end, silence between them included. Concatenating only the speech would be
    cheaper still and would corrupt every timestamp, because the returned times would no
    longer map linearly onto the recording. A gap wide enough to matter simply ends the
    window.

    A single region longer than the window gets a window of its own -- VAD bounds regions
    at 30 seconds, so this is the boundary case rather than the normal one.
    """
    ordered = sorted(regions, key=lambda region: (region.start, region.end))
    windows: list[tuple[float, float, tuple[SpeechRegion, ...]]] = []
    current: list[SpeechRegion] = []
    for region in ordered:
        if current and (region.end - current[0].start) > WHISPER_WINDOW_SECONDS:
            windows.append((current[0].start, current[-1].end, tuple(current)))
            current = []
        current.append(region)
    if current:
        windows.append((current[0].start, current[-1].end, tuple(current)))
    return windows


def attribute_to_region(
    start: float, end: float, covered: Sequence[SpeechRegion]
) -> int | None:
    """Which region a decoded segment belongs to: the one it overlaps most.

    Every segment must get an answer. Pass-2 selection groups by region and the merge
    supersedes by region, so an unattributed segment makes its region look empty and the
    budget is spent on the wrong thing. When a segment overlaps nothing -- the engine can
    emit text over the silence between two regions -- the nearest region by midpoint
    wins, which is the only defensible choice given the audio it was decoded from.
    """
    if not covered:
        return None
    best: tuple[float, int] | None = None
    for region in covered:
        overlap = min(end, region.end) - max(start, region.start)
        if overlap > 0 and (best is None or overlap > best[0]):
            best = (overlap, region.index)
    if best is not None:
        return best[1]
    midpoint = (start + end) / 2.0
    return min(
        covered,
        key=lambda region: abs(((region.start + region.end) / 2.0) - midpoint),
    ).index


def _shift(value: Any, start: float, end: float) -> float:
    """Move a slice-relative timestamp onto the working copy's timeline, and bound it."""
    try:
        moved = start + float(value)
    except (TypeError, ValueError):
        return start
    return max(start, min(moved, end))


def _audio_fingerprint(path: Path) -> tuple[str, int, int]:
    """Identity of the bytes on disk, so a cached array can never go stale.

    Path alone would be enough given that a provider handles one file for its lifetime,
    but a stale audio cache is the worst kind of bug available here -- it would transcribe
    the wrong audio and produce a transcript that looks entirely plausible. Size and
    modification time make that impossible for the cost of one ``stat``.
    """
    stat = path.stat()
    return (str(path), stat.st_size, stat.st_mtime_ns)


class _LoadedAudio:
    """A working copy held in memory, sliced per window.

    **Kept as int16 where the source allows it.** A three-hour working copy is 172 MB as
    int16 and 345 MB as float32, and the pass-2 model already occupies 1.9 GB of the
    2.5 GB budget -- so the smaller representation is what keeps a long meeting inside it.
    The conversion the engine actually wants happens per window, on roughly 2 MB at a time,
    which costs nothing measurable.
    """

    __slots__ = ("_samples", "_needs_scaling", "fingerprint")

    def __init__(
        self, samples: Any, *, needs_scaling: bool, fingerprint: tuple[str, int, int]
    ) -> None:
        self._samples = samples
        self._needs_scaling = needs_scaling
        self.fingerprint = fingerprint

    def __len__(self) -> int:
        return int(len(self._samples))

    @property
    def seconds(self) -> float:
        return len(self._samples) / WORKING_SAMPLE_RATE

    @property
    def nbytes(self) -> int:
        return int(getattr(self._samples, "nbytes", 0))

    def window(self, start_frame: int, end_frame: int) -> Any:
        """The float32 samples for one window. A view where possible, a copy where not."""
        clip = self._samples[start_frame:end_frame]
        if not self._needs_scaling:
            return clip
        import numpy as np

        return clip.astype(np.float32) / 32768.0


def _load_working_copy(path: Path, *, fingerprint: tuple[str, int, int]) -> _LoadedAudio:
    """Read a 16 kHz mono PCM16 WAV into memory.

    Read directly rather than through ``faster_whisper.decode_audio``: the working copy's
    format is fixed by the normalisation stage and enforced by a database CHECK, so there
    is nothing to negotiate, and this avoids handing the file to an FFmpeg build for a
    conversion that amounts to a divide. Measured byte-identical to ``decode_audio`` on
    the working copy, and about 20x faster.

    Anything that is *not* a working copy falls back to the library, so the benchmark can
    still be pointed at a corpus file in another format. That path already yields float32
    and is left alone.
    """
    import wave

    import numpy as np

    try:
        with wave.open(str(path), "rb") as handle:
            if (
                handle.getnchannels() == WORKING_CHANNELS
                and handle.getsampwidth() == 2
                and handle.getframerate() == WORKING_SAMPLE_RATE
            ):
                raw = handle.readframes(handle.getnframes())
                return _LoadedAudio(
                    np.frombuffer(raw, dtype="<i2"),
                    needs_scaling=True,
                    fingerprint=fingerprint,
                )
    except (wave.Error, EOFError, OSError):
        pass

    from faster_whisper import decode_audio

    return _LoadedAudio(
        decode_audio(str(path), sampling_rate=WORKING_SAMPLE_RATE),
        needs_scaling=False,
        fingerprint=fingerprint,
    )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
