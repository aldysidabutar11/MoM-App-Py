"""A deterministic ASR stand-in **for tests only**.

**This must never be reachable from production.** There is deliberately no
configuration key, no environment variable, no request field and no factory function
that can select it: the only way to get one is to construct the class, which only a
test does. ``tests/test_asr_provider.py`` asserts that, and the reason is blunt --
a transcript produced by a stand-in and stored as though a real model produced it would
be a fabricated record.

Two safeguards make an accident visible rather than silent:

* :attr:`AsrModelInfo.is_test_double` is ``True`` and is carried into every result, so
  a stored transcript would say so;
* the model name is prefixed ``FAKE-``, so it is obvious in any log or row.

What it is useful for: exercising the pipeline, checkpointing, cancellation, selection,
merge and persistence without a 500 MB model and without minutes of CPU time. It
produces plausible, deterministic segments derived from the requested regions, so the
same input always yields the same output and a test can assert exact values.
"""

from __future__ import annotations

import hashlib
from typing import Any, Final

from mom_igd.asr.provider import (
    AsrModelInfo,
    ProviderError,
    TranscriptSegment,
    TranscriptionRequest,
    TranscriptionResult,
    Word,
    validate_transcription,
)

__all__ = [
    "FAKE_MODEL_NAME",
    "BrokenAsrProvider",
    "DeterministicAsrProvider",
    "SlowAsrProvider",
]

FAKE_MODEL_NAME: Final[str] = "FAKE-deterministic-asr"

#: A tiny vocabulary that mixes Indonesian with the English technical terms this
#: product actually has to cope with. Fixed order, so output is reproducible.
_VOCAB: Final[tuple[str, ...]] = (
    "kita",
    "perlu",
    "deploy",
    "server",
    "produksi",
    "minggu",
    "depan",
    "sprint",
    "review",
    "database",
    "migrasi",
    "selesai",
)


class DeterministicAsrProvider:
    """Produces reproducible segments from the requested regions. Loads nothing."""

    provider_id: Final[str] = "fake/deterministic"

    def __init__(
        self,
        *,
        words_per_second: float = 2.0,
        avg_logprob: float = -0.25,
        no_speech_prob: float = 0.05,
        compression_ratio: float = 1.6,
        seed: str = "phase4",
    ) -> None:
        self._words_per_second = max(0.1, float(words_per_second))
        self._avg_logprob = float(avg_logprob)
        self._no_speech_prob = float(no_speech_prob)
        self._compression_ratio = float(compression_ratio)
        self._seed = str(seed)
        self._loaded = False
        self._calls = 0

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def calls(self) -> int:
        """How many times :meth:`transcribe` ran. Used to assert budget behaviour."""
        return self._calls

    @property
    def info(self) -> AsrModelInfo:
        return AsrModelInfo(
            model_name=FAKE_MODEL_NAME,
            revision="0" * 40,
            manifest_sha256=hashlib.sha256(self._seed.encode()).hexdigest(),
            compute_type="none",
            cpu_threads=0,
            provider_id=self.provider_id,
            is_test_double=True,
        )

    def load(self) -> AsrModelInfo:
        self._loaded = True
        return self.info

    def close(self) -> None:
        self._loaded = False

    def health(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "loaded": self._loaded,
            "is_test_double": True,
        }

    def _token(self, region_index: int, position: int) -> str:
        digest = hashlib.sha256(
            f"{self._seed}:{region_index}:{position}".encode()
        ).digest()
        return _VOCAB[digest[0] % len(_VOCAB)]

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        if not self._loaded:
            self.load()
        self._calls += 1

        regions = request.regions
        if not regions:
            raise ProviderError(
                "the deterministic provider requires explicit regions, so a test "
                "cannot accidentally assert against a whole-file decode it did not set up"
            )

        segments: list[TranscriptSegment] = []
        for region in regions:
            span = max(0.0, region.end - region.start)
            count = max(1, int(span * self._words_per_second))
            step = span / count if count else span
            words: list[Word] = []
            for position in range(count):
                start = region.start + position * step
                end = min(region.end, start + step)
                digest = hashlib.sha256(
                    f"{self._seed}:p:{region.index}:{position}".encode()
                ).digest()
                words.append(
                    Word(
                        text=self._token(region.index, position),
                        start=start,
                        end=end,
                        # Deterministic but varied, so confidence-based selection has
                        # something real to rank.
                        probability=0.55 + (digest[1] % 45) / 100.0,
                    )
                )
            segments.append(
                TranscriptSegment(
                    index=len(segments),
                    start=region.start,
                    end=region.end,
                    text=" ".join(word.text for word in words),
                    words=tuple(words),
                    avg_logprob=self._avg_logprob,
                    no_speech_prob=self._no_speech_prob,
                    compression_ratio=self._compression_ratio,
                    temperature=0.0,
                    asr_pass=1,
                )
            )

        audio_seconds = sum(region.end - region.start for region in regions)
        return validate_transcription(
            TranscriptionResult(
                segments=tuple(segments),
                model=self.info,
                language=request.language,
                language_probability=0.99,
                audio_seconds=audio_seconds,
                # A plausible but fixed cost, so a test can assert an RTF without
                # depending on how fast the machine running it happens to be.
                processing_seconds=audio_seconds * 0.2,
            )
        )


class BrokenAsrProvider:
    """Returns output that must be rejected. For testing the validation boundary."""

    provider_id: Final[str] = "fake/broken"

    def __init__(self, mode: str = "nan_probability") -> None:
        self.mode = mode
        self._loaded = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def info(self) -> AsrModelInfo:
        return AsrModelInfo(
            model_name="FAKE-broken",
            revision="0" * 40,
            manifest_sha256="0" * 64,
            compute_type="none",
            cpu_threads=0,
            provider_id=self.provider_id,
            is_test_double=True,
        )

    def load(self) -> AsrModelInfo:
        self._loaded = True
        return self.info

    def close(self) -> None:
        self._loaded = False

    def health(self) -> dict[str, Any]:
        return {"provider_id": self.provider_id, "is_test_double": True}

    def raw_result(self) -> TranscriptionResult:
        """The invalid result, **unvalidated**, so a test can pass it to the validator."""
        import math

        base = TranscriptSegment(
            index=0, start=1.0, end=2.0, text="satu dua", words=(), asr_pass=1
        )
        if self.mode == "nan_probability":
            segment = TranscriptSegment(
                index=0, start=1.0, end=2.0, text="x", no_speech_prob=math.nan
            )
        elif self.mode == "infinite_logprob":
            segment = TranscriptSegment(
                index=0, start=1.0, end=2.0, text="x", avg_logprob=-math.inf
            )
        elif self.mode == "reversed_interval":
            segment = TranscriptSegment(index=0, start=5.0, end=2.0, text="x")
        elif self.mode == "negative_timestamp":
            segment = TranscriptSegment(index=0, start=-1.0, end=2.0, text="x")
        elif self.mode == "probability_above_one":
            segment = TranscriptSegment(
                index=0, start=1.0, end=2.0, text="x", no_speech_prob=1.9
            )
        elif self.mode == "word_outside_segment":
            segment = TranscriptSegment(
                index=0,
                start=1.0,
                end=2.0,
                text="x",
                words=(Word(text="x", start=8.0, end=9.0),),
            )
        elif self.mode == "reversed_word":
            segment = TranscriptSegment(
                index=0,
                start=1.0,
                end=2.0,
                text="x",
                words=(Word(text="x", start=1.8, end=1.2),),
            )
        elif self.mode == "text_too_long":
            segment = TranscriptSegment(index=0, start=1.0, end=2.0, text="a" * 100_000)
        elif self.mode == "too_many_words":
            segment = TranscriptSegment(
                index=0,
                start=1.0,
                end=2.0,
                text="x",
                words=tuple(
                    Word(text="x", start=1.0, end=1.0) for _ in range(2_000)
                ),
            )
        elif self.mode == "speaker_assigned":
            segment = base
            object.__setattr__(segment, "speaker", "Budi")  # type: ignore[misc]
        elif self.mode == "bad_pass":
            segment = TranscriptSegment(index=0, start=1.0, end=2.0, text="x", asr_pass=7)
        else:  # pragma: no cover - guards a typo in a test parameter
            raise AssertionError(f"unknown broken mode {self.mode!r}")

        return TranscriptionResult(
            segments=(segment,),
            model=self.info,
            language="id",
            audio_seconds=10.0,
            processing_seconds=1.0,
        )

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        return validate_transcription(self.raw_result())


class SlowAsrProvider(DeterministicAsrProvider):
    """Deterministic, but sleeps per region so cancellation has something to interrupt."""

    provider_id: Final[str] = "fake/slow"

    def __init__(self, *, delay_seconds: float = 0.4, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._delay = max(0.0, float(delay_seconds))

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        import time

        time.sleep(self._delay)
        return super().transcribe(request)
