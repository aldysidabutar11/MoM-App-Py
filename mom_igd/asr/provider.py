"""The ASR provider contract, and hard validation of whatever a provider returns.

Nothing downstream trusts a provider's output shape. A transcription engine is a large
third-party numerical program; it can and does emit ``NaN`` probabilities, word
timestamps outside the segment that contains them, zero-length or reversed intervals,
and occasionally a wall of repeated tokens. Every one of those is a data-integrity
problem for a system whose whole purpose is evidence: a timestamp that is off breaks
the link back to the master recording, and a ``NaN`` confidence silently poisons the
pass-2 selection that depends on it.

So :func:`validate_transcription` is deliberately strict and runs on **every** result,
production or test. It rejects rather than repairs, with two carefully chosen
exceptions that are corrections of representation rather than of content:

* a word whose bounds sit a hair outside its segment (floating-point drift from the
  engine's own frame arithmetic) is clamped to the segment, because the alternative is
  discarding a correct word over a microsecond;
* a word list that is empty is accepted, because a segment of music or noise
  legitimately produces text with no aligned words.

Everything else -- a reversed interval, a non-finite number, a probability outside
``[0, 1]``, a segment that starts before the previous one ends, text beyond the size
cap -- is a :class:`ProviderOutputError`.

**What must never leave this boundary:** a filesystem path, a raw model object, an
internal row id, ``NaN``, or an infinity. The dataclasses here are the only shape the
rest of Phase 4 sees.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Final, Iterable, Protocol, runtime_checkable

__all__ = [
    "MAX_SEGMENT_CHARS",
    "MAX_WORDS_PER_SEGMENT",
    "ModelUnavailableError",
    "ProviderError",
    "ProviderOutputError",
    "SpeakerStatus",
    "SpeechRegion",
    "TranscriptSegment",
    "TranscriptionRequest",
    "TranscriptionResult",
    "Word",
    "AsrProvider",
    "AsrModelInfo",
    "validate_transcription",
]

#: A single ASR segment is a phrase, not a chapter. Whisper's own segments are seconds
#: long; anything past this is a runaway repetition loop, which is a known failure mode
#: and must be caught rather than persisted.
MAX_SEGMENT_CHARS: Final[int] = 4_000
MAX_WORDS_PER_SEGMENT: Final[int] = 800

#: Timestamps are compared with a tolerance rather than exactly. The engine computes
#: them from frame indices in float32, so exact arithmetic does not hold and a strict
#: `<=` would reject correct output.
_EPSILON: Final[float] = 1e-3

#: How far outside its segment a word may sit before it is an error rather than drift.
#: 50 ms is larger than any float32 rounding and smaller than any real misalignment.
_WORD_DRIFT_TOLERANCE: Final[float] = 0.05


class ProviderError(RuntimeError):
    """A provider could not do what was asked. The message says what to do next."""


class ModelUnavailableError(ProviderError):
    """No usable model is provisioned. **This is the fail-closed path.**

    Raised when a model directory is absent, or its manifest is missing, or its hash
    does not verify. Never followed by a download and never followed by a fallback to
    a different model: silently transcribing with something other than the model the
    operator provisioned would make the provenance recorded against the transcript a
    lie.
    """


class ProviderOutputError(ProviderError):
    """A provider returned output that fails validation. Never repaired silently."""


class SpeakerStatus:
    """Phase 4 assigns no speakers, and says so explicitly.

    A ``None`` speaker with no explanation reads like a bug or a gap. ``UNASSIGNED``
    states that the pipeline has not attempted attribution yet, which is the truth
    until Phase 5 diarization exists.
    """

    UNASSIGNED: Final[str] = "UNASSIGNED"


@dataclass(frozen=True, slots=True)
class AsrModelInfo:
    """What a loaded provider is, for provenance. Carries no path and no handle."""

    model_name: str
    revision: str
    manifest_sha256: str
    compute_type: str
    cpu_threads: int
    provider_id: str
    is_test_double: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "revision": self.revision,
            "manifest_sha256": self.manifest_sha256,
            "compute_type": self.compute_type,
            "cpu_threads": self.cpu_threads,
            "provider_id": self.provider_id,
            "is_test_double": self.is_test_double,
        }


@dataclass(frozen=True, slots=True)
class SpeechRegion:
    """One region of speech, in seconds relative to the working copy."""

    index: int
    start: float
    end: float
    confidence: float | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "confidence": None if self.confidence is None else round(self.confidence, 4),
        }


@dataclass(frozen=True, slots=True)
class Word:
    """One word with its own timing and probability."""

    text: str
    start: float
    end: float
    probability: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "probability": None if self.probability is None else round(self.probability, 4),
        }


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """One transcribed segment. **No speaker**: that is Phase 5's job."""

    index: int
    start: float
    end: float
    text: str
    words: tuple[Word, ...] = ()
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None
    temperature: float | None = None
    asr_pass: int = 1
    region_index: int | None = None
    """Which speech region this came from. ``None`` only for a whole-file decode.

    Load bearing, not bookkeeping: pass-2 selection groups by region, and the merge
    supersedes by region. A segment with no region attribution makes its region look
    empty to selection -- which flags every region as ``EMPTY_IN_SPEECH_REGION`` and
    spends the whole pass-2 budget on the wrong thing. That happened.
    """
    speaker: None = None
    speaker_status: str = SpeakerStatus.UNASSIGNED

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "text": self.text,
            "words": [w.to_dict() for w in self.words],
            "avg_logprob": None if self.avg_logprob is None else round(self.avg_logprob, 4),
            "no_speech_prob": (
                None if self.no_speech_prob is None else round(self.no_speech_prob, 4)
            ),
            "compression_ratio": (
                None if self.compression_ratio is None else round(self.compression_ratio, 4)
            ),
            "temperature": None if self.temperature is None else round(self.temperature, 3),
            "asr_pass": self.asr_pass,
            "region_index": self.region_index,
            "speaker": None,
            "speaker_status": self.speaker_status,
        }


@dataclass(frozen=True, slots=True)
class TranscriptionRequest:
    """What to transcribe. Times are seconds relative to the working copy."""

    audio_path: str
    regions: tuple[SpeechRegion, ...]
    language: str = "id"
    initial_prompt: str | None = None
    beam_size: int = 5
    temperature: float = 0.0
    condition_on_previous_text: bool = False
    word_timestamps: bool = True


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """The validated output of one transcription call."""

    segments: tuple[TranscriptSegment, ...]
    model: AsrModelInfo
    language: str
    language_probability: float | None = None
    audio_seconds: float = 0.0
    processing_seconds: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def real_time_factor(self) -> float | None:
        if self.audio_seconds <= 0:
            return None
        return self.processing_seconds / self.audio_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "segments": [s.to_dict() for s in self.segments],
            "model": self.model.to_dict(),
            "language": self.language,
            "language_probability": (
                None
                if self.language_probability is None
                else round(self.language_probability, 4)
            ),
            "audio_seconds": round(self.audio_seconds, 3),
            "processing_seconds": round(self.processing_seconds, 3),
            "real_time_factor": (
                None if self.real_time_factor is None else round(self.real_time_factor, 4)
            ),
            "speaker_status": SpeakerStatus.UNASSIGNED,
        }


@runtime_checkable
class AsrProvider(Protocol):
    """What Phase 4 requires of an ASR engine.

    Deliberately narrow. A provider loads a local model, transcribes speech regions,
    reports what it is, and unloads deterministically. It does not resolve paths, does
    not decide policy, does not fetch anything, and does not know about the database.
    """

    @property
    def info(self) -> AsrModelInfo:
        """Provenance of the loaded model. Available only after :meth:`load`."""

    @property
    def loaded(self) -> bool:
        """Whether a model is currently resident."""

    def load(self) -> AsrModelInfo:
        """Load the model from its verified local path. Raises
        :class:`ModelUnavailableError` when no verified model exists."""

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        """Transcribe the requested regions. Output is validated before it returns."""

    def health(self) -> dict[str, Any]:
        """Readiness detail, safe to log: no paths, no transcript, no key material."""

    def close(self) -> None:
        """Release the model. Must be idempotent and must not raise."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _finite(value: float | None, label: str) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ProviderOutputError(
            f"{label} is {numeric!r}, which is not a finite number. A non-finite "
            "confidence would silently corrupt pass-2 selection, so it is refused "
            "rather than coerced."
        )
    return numeric


def _probability(value: float | None, label: str) -> float | None:
    numeric = _finite(value, label)
    if numeric is None:
        return None
    if not (0.0 - _EPSILON) <= numeric <= (1.0 + _EPSILON):
        raise ProviderOutputError(
            f"{label}={numeric!r} is outside [0, 1]. Acceptable values are "
            "probabilities."
        )
    return min(1.0, max(0.0, numeric))


def _validate_words(
    words: Iterable[Word], *, segment_index: int, seg_start: float, seg_end: float
) -> tuple[Word, ...]:
    cleaned: list[Word] = []
    previous_end = -math.inf
    for position, word in enumerate(words):
        label = f"segment {segment_index} word {position}"
        start = _finite(word.start, f"{label} start")
        end = _finite(word.end, f"{label} end")
        if start is None or end is None:
            raise ProviderOutputError(f"{label} has no timestamps")
        if start < -_EPSILON or end < -_EPSILON:
            raise ProviderOutputError(f"{label} has a negative timestamp: {start}..{end}")
        if end < start - _EPSILON:
            raise ProviderOutputError(
                f"{label} ends before it starts: {start} > {end}"
            )
        # Drift correction, not content correction: the engine derives these from
        # frame indices, so a few microseconds outside the segment is arithmetic, not
        # a misaligned word.
        if start < seg_start - _WORD_DRIFT_TOLERANCE or end > seg_end + _WORD_DRIFT_TOLERANCE:
            raise ProviderOutputError(
                f"{label} spans {start:.3f}..{end:.3f}, outside its segment "
                f"{seg_start:.3f}..{seg_end:.3f} by more than "
                f"{_WORD_DRIFT_TOLERANCE * 1000:.0f} ms"
            )
        start = min(max(start, seg_start), seg_end)
        end = min(max(end, start), seg_end)
        if start < previous_end - _WORD_DRIFT_TOLERANCE:
            raise ProviderOutputError(
                f"{label} starts at {start:.3f}, before the previous word ended at "
                f"{previous_end:.3f}"
            )
        previous_end = end
        text = str(word.text)
        if len(text) > MAX_SEGMENT_CHARS:
            raise ProviderOutputError(f"{label} text exceeds {MAX_SEGMENT_CHARS} characters")
        cleaned.append(
            Word(
                text=text,
                start=start,
                end=end,
                probability=_probability(word.probability, f"{label} probability"),
            )
        )
    if len(cleaned) > MAX_WORDS_PER_SEGMENT:
        raise ProviderOutputError(
            f"segment {segment_index} has {len(cleaned)} words, above the "
            f"{MAX_WORDS_PER_SEGMENT} cap. That is a repetition loop, not speech."
        )
    return tuple(cleaned)


def validate_transcription(
    result: TranscriptionResult, *, audio_seconds: float | None = None
) -> TranscriptionResult:
    """Validate and normalise a transcription result. Raises on anything unsafe.

    Returns a new result whose numbers are guaranteed finite, whose intervals are
    ordered and non-overlapping, whose words lie inside their segments, and whose
    text is within the size cap. Callers may rely on all of that; nothing downstream
    re-checks it.
    """
    limit = None
    if audio_seconds is not None and audio_seconds > 0:
        limit = float(audio_seconds)

    validated: list[TranscriptSegment] = []
    previous_end = -math.inf
    for segment in result.segments:
        label = f"segment {segment.index}"
        start = _finite(segment.start, f"{label} start")
        end = _finite(segment.end, f"{label} end")
        if start is None or end is None:
            raise ProviderOutputError(f"{label} has no timestamps")
        if start < -_EPSILON or end < -_EPSILON:
            raise ProviderOutputError(f"{label} has a negative timestamp: {start}..{end}")
        if end < start - _EPSILON:
            raise ProviderOutputError(f"{label} ends before it starts: {start} > {end}")
        if limit is not None and start > limit + 1.0:
            raise ProviderOutputError(
                f"{label} starts at {start:.3f}s, past the end of {limit:.3f}s of audio"
            )
        if start < previous_end - _EPSILON:
            raise ProviderOutputError(
                f"{label} starts at {start:.3f}, before the previous segment ended at "
                f"{previous_end:.3f}. Segments must be ordered and must not overlap."
            )
        text = str(segment.text)
        if len(text) > MAX_SEGMENT_CHARS:
            raise ProviderOutputError(
                f"{label} text is {len(text)} characters, above the "
                f"{MAX_SEGMENT_CHARS} cap. A segment that long is a repetition loop."
            )
        if segment.asr_pass not in (1, 2):
            raise ProviderOutputError(f"{label} has asr_pass={segment.asr_pass!r}")
        if segment.speaker is not None:
            raise ProviderOutputError(
                f"{label} carries a speaker. Phase 4 assigns no speakers; that is "
                "Phase 5's job and guessing here would invent an attribution."
            )
        previous_end = end
        validated.append(
            TranscriptSegment(
                index=segment.index,
                start=start,
                end=end,
                text=text,
                words=_validate_words(
                    segment.words, segment_index=segment.index, seg_start=start, seg_end=end
                ),
                avg_logprob=_finite(segment.avg_logprob, f"{label} avg_logprob"),
                no_speech_prob=_probability(segment.no_speech_prob, f"{label} no_speech_prob"),
                compression_ratio=_finite(
                    segment.compression_ratio, f"{label} compression_ratio"
                ),
                temperature=_finite(segment.temperature, f"{label} temperature"),
                asr_pass=segment.asr_pass,
                # Carried through explicitly. This function rebuilds every segment
                # rather than mutating it, so a field that is not listed here is
                # silently dropped -- which is exactly how region attribution went
                # missing and made pass-2 selection flag every region as empty.
                region_index=segment.region_index,
                speaker_status=segment.speaker_status,
            )
        )

    processing = _finite(result.processing_seconds, "processing_seconds") or 0.0
    audio = _finite(result.audio_seconds, "audio_seconds") or 0.0
    if processing < 0 or audio < 0:
        raise ProviderOutputError("processing_seconds and audio_seconds must be >= 0")
    return TranscriptionResult(
        segments=tuple(validated),
        model=result.model,
        language=str(result.language),
        language_probability=_probability(
            result.language_probability, "language_probability"
        ),
        audio_seconds=audio,
        processing_seconds=processing,
        extra=dict(result.extra),
    )
