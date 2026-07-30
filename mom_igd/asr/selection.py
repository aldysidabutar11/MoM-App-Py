"""Which regions deserve a second, slower transcription pass -- and why.

Pass 2 costs roughly twice pass 1 per second of audio, so running it over a whole
meeting would put total real-time factor past the target. It therefore runs over a
*budgeted subset*, and this module decides that subset.

**Every decision is explained by a reason code.** A region is never selected because a
score came out high; it is selected because a named rule fired, and the rule's identifier
is stored on the segment. That matters for three reasons: a reviewer can see why the
machine went back over one part of the meeting and not another, a test can assert which
rule fired rather than only that *something* did, and when the thresholds turn out to be
wrong the evidence for retuning them already exists.

**Deterministic by construction.** Same pass-1 output plus same policy gives the same
selection, in the same order, every time. Ties break on start time, never on dictionary
order or floating-point noise. A selection that varied between runs would make "pass 2
improved this region" unfalsifiable.

**Signals available in Phase 4.** Only what the decoder itself reports: average log
probability, no-speech probability, compression ratio, decoding temperature and word
probabilities. Two of the strongest signals -- a speaker change inside a region, and
overlapping speech -- do not exist until diarization lands in Phase 5. The rule table is
built so those become additional reason codes rather than a rewrite.

**A budget is spent, not exceeded.** Regions are ranked worst-first and accepted while
the budget lasts. When it runs out that fact is recorded (``budget_exhausted``) rather
than quietly dropping the tail, because "we re-ran everything that needed it" and "we ran
out of time" are different statements about a transcript.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Iterable, Mapping, Sequence

from mom_igd.logging_setup import get_logger

__all__ = [
    "REASON_CODES",
    "RegionSelection",
    "SelectionPolicy",
    "SelectionResult",
    "select_regions_for_pass2",
]

_LOG = get_logger("asr.selection")


#: Every reason a region can be re-transcribed, with the weight it contributes to the
#: ranking. Weights order the queue when the budget cannot cover everything; they are
#: not probabilities and are not claimed to be calibrated.
#:
#: ``DECODER_FELL_BACK`` is weighted highest because it is the only signal that is not a
#: threshold on a continuous score: Whisper raises the temperature when its own checks
#: reject a decode, so a non-zero temperature is the decoder itself reporting that it
#: struggled.
REASON_CODES: Final[Mapping[str, tuple[float, str]]] = {
    "DECODER_FELL_BACK": (
        3.0,
        "the decoder raised its temperature, which it only does after rejecting its "
        "own first attempt",
    ),
    "EMPTY_IN_SPEECH_REGION": (
        2.5,
        "voice activity detection found speech here and the decoder produced no text",
    ),
    "LOW_AVG_LOGPROB": (
        2.0,
        "average token log probability below the configured floor",
    ),
    "REPETITION_SUSPECTED": (
        1.75,
        "compression ratio above the repetition threshold, which usually means a "
        "looping or hallucinated passage",
    ),
    "LOW_WORD_CONFIDENCE": (
        1.5,
        "at least one word came back below the word-probability floor",
    ),
    "HIGH_NO_SPEECH_PROB": (
        1.25,
        "the decoder thinks this may not be speech, yet emitted text for it",
    ),
}


@dataclass(frozen=True, slots=True)
class SelectionPolicy:
    """Thresholds and budget. Built from ``AppConfig.asr``, never from defaults here.

    The defaults exist so a unit test can construct one, and they are the same numbers
    as the configuration -- but production must pass ``from_config``. Phase 3 learned
    that lesson the hard way: a service that silently fell back to its own built-in
    numbers meant two runtimes disagreed about the same policy.
    """

    enabled: bool = True
    budget_ratio: float = 0.25
    min_avg_logprob: float = -1.0
    max_no_speech_prob: float = 0.6
    min_word_probability: float = 0.45
    max_compression_ratio: float = 2.4

    @classmethod
    def from_config(cls, config: Any) -> SelectionPolicy:
        asr = config.asr
        return cls(
            enabled=bool(asr.pass2_enabled),
            budget_ratio=float(asr.pass2_budget_ratio),
            min_avg_logprob=float(asr.pass2_min_avg_logprob),
            max_no_speech_prob=float(asr.pass2_max_no_speech_prob),
            min_word_probability=float(asr.pass2_min_word_probability),
            max_compression_ratio=float(asr.pass2_max_compression_ratio),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "budget_ratio": self.budget_ratio,
            "min_avg_logprob": self.min_avg_logprob,
            "max_no_speech_prob": self.max_no_speech_prob,
            "min_word_probability": self.min_word_probability,
            "max_compression_ratio": self.max_compression_ratio,
        }


@dataclass(frozen=True, slots=True)
class RegionSelection:
    """One region's verdict, whether or not it was chosen."""

    region_seq: int
    start_ms: int
    end_ms: int
    reason_codes: tuple[str, ...]
    score: float
    selected: bool
    rank: int | None
    segment_seqs: tuple[int, ...]

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "region_seq": self.region_seq,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "duration_ms": self.duration_ms,
            "reason_codes": list(self.reason_codes),
            "score": round(self.score, 4),
            "selected": self.selected,
            "rank": self.rank,
            "segment_seqs": list(self.segment_seqs),
        }


@dataclass(slots=True)
class SelectionResult:
    """What pass 2 should run over, and the accounting that justifies it."""

    regions: tuple[RegionSelection, ...] = ()
    budget_ms: int = 0
    selected_ms: int = 0
    speech_ms: int = 0
    budget_exhausted: bool = False
    skipped_reason: str | None = None
    policy: SelectionPolicy = field(default_factory=SelectionPolicy)

    @property
    def selected(self) -> tuple[RegionSelection, ...]:
        return tuple(region for region in self.regions if region.selected)

    @property
    def flagged(self) -> tuple[RegionSelection, ...]:
        """Regions a rule fired on, whether or not the budget covered them."""
        return tuple(region for region in self.regions if region.reason_codes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget_ms": self.budget_ms,
            "selected_ms": self.selected_ms,
            "speech_ms": self.speech_ms,
            "budget_exhausted": self.budget_exhausted,
            "skipped_reason": self.skipped_reason,
            "selected_region_count": len(self.selected),
            "flagged_region_count": len(self.flagged),
            "policy": self.policy.to_dict(),
            "regions": [region.to_dict() for region in self.selected],
        }


def _reasons_for_segment(segment: Mapping[str, Any], policy: SelectionPolicy) -> list[str]:
    """Which rules fire on one pass-1 segment. Order is the table's order, not chance."""
    reasons: list[str] = []
    text = str(segment.get("text") or "").strip()

    temperature = segment.get("temperature")
    if temperature is not None and float(temperature) > 0.0:
        reasons.append("DECODER_FELL_BACK")

    if not text:
        reasons.append("EMPTY_IN_SPEECH_REGION")

    avg_logprob = segment.get("avg_logprob")
    if avg_logprob is not None and float(avg_logprob) < policy.min_avg_logprob:
        reasons.append("LOW_AVG_LOGPROB")

    compression = segment.get("compression_ratio")
    if compression is not None and float(compression) > policy.max_compression_ratio:
        reasons.append("REPETITION_SUSPECTED")

    words = segment.get("words") or ()
    probabilities = [
        float(word["probability"])
        for word in words
        if isinstance(word, Mapping) and word.get("probability") is not None
    ]
    if probabilities and min(probabilities) < policy.min_word_probability:
        reasons.append("LOW_WORD_CONFIDENCE")

    no_speech = segment.get("no_speech_prob")
    if no_speech is not None and float(no_speech) > policy.max_no_speech_prob and text:
        reasons.append("HIGH_NO_SPEECH_PROB")

    return reasons


def _score(reasons: Iterable[str]) -> float:
    return sum(REASON_CODES[reason][0] for reason in reasons if reason in REASON_CODES)


def select_regions_for_pass2(
    *,
    segments: Sequence[Mapping[str, Any]],
    regions: Sequence[Mapping[str, Any]],
    policy: SelectionPolicy,
) -> SelectionResult:
    """Rank regions worst-first and take as many as the budget allows.

    ``segments`` are pass-1 outputs carrying ``region_seq``; ``regions`` are the VAD
    spans, each with ``seq``, ``start_ms`` and ``end_ms``. A region with no segment at
    all still gets a verdict -- ``EMPTY_IN_SPEECH_REGION`` -- because "VAD heard
    something and the decoder returned nothing" is exactly the case worth re-running.
    """
    speech_ms = sum(
        max(0, int(region["end_ms"]) - int(region["start_ms"])) for region in regions
    )
    if not policy.enabled:
        return SelectionResult(
            speech_ms=speech_ms,
            skipped_reason="pass 2 is disabled in configuration",
            policy=policy,
        )
    if not regions:
        return SelectionResult(
            speech_ms=0,
            skipped_reason="no speech regions were detected, so there is nothing to "
            "re-transcribe",
            policy=policy,
        )

    by_region: dict[int, list[Mapping[str, Any]]] = {}
    for segment in segments:
        key = segment.get("region_seq")
        if key is None:
            continue
        by_region.setdefault(int(key), []).append(segment)

    verdicts: list[dict[str, Any]] = []
    for region in regions:
        seq = int(region["seq"])
        start_ms = int(region["start_ms"])
        end_ms = int(region["end_ms"])
        owned = by_region.get(seq, [])
        if not owned:
            # Fall back to whatever *overlaps* this region. Regions are decoded in
            # batched 30-second windows, so one long segment can span several regions
            # and is attributed to the one it overlaps most -- leaving its neighbours
            # with nothing attributed although there is text over them. Reading the
            # overlapping segments instead does two things: a region that is covered is
            # not falsely called empty (nine were, and the whole budget went on audio
            # that already had text), and the quality signals of a segment that could
            # not be attributed are still used rather than silently lost.
            owned = [
                segment
                for segment in segments
                if int(segment["start_ms"]) < end_ms and int(segment["end_ms"]) > start_ms
            ]

        reasons: list[str] = []
        if not owned:
            reasons.append("EMPTY_IN_SPEECH_REGION")
        else:
            for segment in owned:
                for reason in _reasons_for_segment(segment, policy):
                    if reason not in reasons:
                        reasons.append(reason)
        # Report in the table's order regardless of which segment fired first, so two
        # runs over the same audio produce byte-identical reason lists.
        ordered = tuple(code for code in REASON_CODES if code in reasons)
        verdicts.append(
            {
                "region_seq": seq,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "reason_codes": ordered,
                "score": _score(ordered),
                "segment_seqs": tuple(
                    int(segment["seq"]) for segment in owned if "seq" in segment
                ),
            }
        )

    # Canonical order: the meeting's own. The caller's argument order must not show up
    # in the result, or two runs over the same audio would serialise differently.
    verdicts.sort(key=lambda verdict: (verdict["start_ms"], verdict["region_seq"]))

    budget_ms = int(round(speech_ms * policy.budget_ratio))
    # Worst first; ties by position in the meeting. Both keys are needed: score alone
    # leaves ties resolved by input order, which is stable only by accident.
    ranked = sorted(
        (verdict for verdict in verdicts if verdict["reason_codes"]),
        key=lambda verdict: (-verdict["score"], verdict["start_ms"], verdict["region_seq"]),
    )

    selected_seqs: dict[int, int] = {}
    spent = 0
    exhausted = False
    for rank, verdict in enumerate(ranked):
        duration = max(0, verdict["end_ms"] - verdict["start_ms"])
        if spent + duration > budget_ms:
            # Skip it and keep going down the queue. An earlier version stopped at the
            # first region that did not fit, which let one long region at the top of the
            # ranking block the entire pass: a 6-second region against a 5.3-second
            # budget meant nothing at all was re-transcribed, with nine other flagged
            # regions waiting behind it. The ranking still sets priority; it cannot
            # promise a region larger than the whole budget will run.
            exhausted = True
            continue
        selected_seqs[verdict["region_seq"]] = rank
        spent += duration

    result = SelectionResult(
        regions=tuple(
            RegionSelection(
                region_seq=verdict["region_seq"],
                start_ms=verdict["start_ms"],
                end_ms=verdict["end_ms"],
                reason_codes=verdict["reason_codes"],
                score=verdict["score"],
                selected=verdict["region_seq"] in selected_seqs,
                rank=selected_seqs.get(verdict["region_seq"]),
                segment_seqs=verdict["segment_seqs"],
            )
            for verdict in verdicts
        ),
        budget_ms=budget_ms,
        selected_ms=spent,
        speech_ms=speech_ms,
        budget_exhausted=exhausted,
        skipped_reason=None,
        policy=policy,
    )
    _LOG.info(
        "asr.selection",
        extra={
            "regions": len(regions),
            "flagged": len(result.flagged),
            "selected": len(result.selected),
            "budget_ms": budget_ms,
            "selected_ms": spent,
            "exhausted": exhausted,
        },
    )
    return result
