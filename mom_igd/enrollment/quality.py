"""Enrollment quality gates.

**Built on the Phase 2 meter, not beside it.** Every level figure here comes from
:mod:`mom_igd.audio.quality` -- the same RMS, peak, clipping, silence and
noise-floor code that judges a meeting recording. A second, subtly different
measurement path would eventually disagree with the first, and then nobody could
say which number was true.

**No model-based VAD.** "How much of this sample is speech?" is estimated from
energy against the measured noise floor, which is arithmetic on figures the meter
already produces. A Silero/WebRTC VAD would be a Phase 4 dependency, and Phase 3
does not need one to answer "did this person actually talk for 30 seconds?".

**Honesty about thresholds.** Exactly one number here comes from the architecture
with a rationale behind it: the intra-speaker cosine floor of **0.80**. Everything
else is a *provisional* engineering default chosen to catch obvious problems, and it
is labelled as such rather than dressed up as a validated figure. The distinction
matters because a threshold presented as calibrated, but actually invented, is worse
than an admitted guess -- it stops anyone from re-examining it.

`NOT_MEASURED` is a first-class verdict for the same reason it is in the Phase 2
benchmark: a check that could not run must say so, never quietly pass.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Sequence

from mom_igd.audio.quality import LevelVerdict, QualitySnapshot, analyse_block
from mom_igd.enrollment.provider import cosine_similarity

__all__ = [
    "MIN_INTRA_SPEAKER_COSINE",
    "EnrollmentQualityThresholds",
    "GateStatus",
    "QualityGate",
    "SampleQuality",
    "EnrollmentQualityReport",
    "evaluate_sample",
    "evaluate_enrollment",
]

MIN_INTRA_SPEAKER_COSINE: Final[float] = 0.80
"""From the architecture: five samples of one voice must agree at least this well.

The only threshold in this module with a documented origin. It is a *consistency*
check, not an accuracy claim: it detects a sample that captured the wrong person,
a burst of noise, or a microphone that moved -- it says nothing about how well the
system will later distinguish two different speakers, which is a Phase 6 question
that needs its own measurement on real hardware.
"""


class GateStatus(StrEnum):
    """Outcome of one quality gate."""

    PASS = "PASS"
    WARN = "WARN"
    REJECT = "REJECT"
    NOT_MEASURED = "NOT_MEASURED"


@dataclass(frozen=True, slots=True)
class EnrollmentQualityThresholds:
    """Tunable gates. All provisional except the cosine floor.

    Exposed as a dataclass so a future calibration exercise can replace these with
    measured values without touching the logic, and so a test can drive an edge
    case without monkeypatching module constants.
    """

    # Duration. 8-12 s per sample and >= 30 s total come from the phase brief.
    min_sample_seconds: float = 6.0
    target_sample_seconds: float = 8.0
    max_sample_seconds: float = 15.0
    min_total_speech_seconds: float = 30.0

    # Levels. Provisional: chosen to catch obvious faults, not calibrated.
    min_rms_dbfs: float = -45.0
    warn_rms_dbfs: float = -35.0
    max_peak_dbfs: float = -1.0
    max_clipping_percent: float = 0.05
    max_silence_percent: float = 60.0
    min_speech_active_ratio: float = 0.35
    min_snr_db: float = 10.0
    warn_snr_db: float = 15.0

    # Capture integrity. Zero tolerance: a dropped frame in a 10 s sample is a
    # hole in biometric evidence, and re-recording costs seconds.
    max_dropped_frames: int = 0
    max_xrun_callbacks: int = 0

    # Consistency.
    min_intra_speaker_cosine: float = MIN_INTRA_SPEAKER_COSINE

    # Calibration freshness, mirroring the Phase 2 production gate.
    max_calibration_age_days: float = 30.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_sample_seconds": self.min_sample_seconds,
            "target_sample_seconds": self.target_sample_seconds,
            "max_sample_seconds": self.max_sample_seconds,
            "min_total_speech_seconds": self.min_total_speech_seconds,
            "min_rms_dbfs": self.min_rms_dbfs,
            "max_peak_dbfs": self.max_peak_dbfs,
            "max_clipping_percent": self.max_clipping_percent,
            "max_silence_percent": self.max_silence_percent,
            "min_speech_active_ratio": self.min_speech_active_ratio,
            "min_snr_db": self.min_snr_db,
            "max_dropped_frames": self.max_dropped_frames,
            "max_xrun_callbacks": self.max_xrun_callbacks,
            "min_intra_speaker_cosine": self.min_intra_speaker_cosine,
            "max_calibration_age_days": self.max_calibration_age_days,
            "provisional": [
                "min_rms_dbfs",
                "warn_rms_dbfs",
                "max_peak_dbfs",
                "max_clipping_percent",
                "max_silence_percent",
                "min_speech_active_ratio",
                "min_snr_db",
                "warn_snr_db",
            ],
            "provisional_note": (
                "These defaults catch obvious faults but have NOT been calibrated "
                "against a verified USB conference microphone. Re-examine them once "
                "real hardware measurements exist."
            ),
        }


@dataclass(frozen=True, slots=True)
class QualityGate:
    """One named check and why it landed where it did."""

    key: str
    status: GateStatus
    detail: str
    measured: Any = None
    threshold: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status.value,
            "detail": self.detail,
            "measured": self.measured,
            "threshold": self.threshold,
        }


def _gate(
    key: str,
    ok: bool,
    detail: str,
    *,
    measured: Any = None,
    threshold: Any = None,
    warn_only: bool = False,
) -> QualityGate:
    if ok:
        status = GateStatus.PASS
    else:
        status = GateStatus.WARN if warn_only else GateStatus.REJECT
    return QualityGate(
        key=key, status=status, detail=detail, measured=measured, threshold=threshold
    )


SPEECH_DYNAMIC_RANGE_DB: Final[float] = 20.0
"""How far below the sample's own overall level a window may sit and still count.

Energy-relative rather than noise-floor-relative, and that choice was forced by a
real failure: the Phase 2 noise floor is a low percentile of per-block RMS, so for a
*constant-level* signal it equals the signal itself. A floor-relative threshold then
classified a perfectly good steady sample as 0 % speech. Comparing each window
against the sample's overall RMS behaves correctly in both cases -- speech pauses
fall well below it, a steady tone does not.
"""

SNR_UNMEASURABLE_DB: Final[float] = 1.0
"""Below this floor-to-RMS gap, the SNR estimate is meaningless rather than bad."""


def estimate_speech_active_ratio(
    pcm: bytes,
    *,
    channels: int,
    sample_rate_hz: int,
    window_ms: int = 30,
    dynamic_range_db: float = SPEECH_DYNAMIC_RANGE_DB,
) -> float:
    """Fraction of ``pcm`` carrying energy comparable to the sample's own level.

    Deliberately crude, and that is the point: it answers "did this person keep
    talking?" using the Phase 2 block analyser over short windows. **It is not a
    voice activity detector** and must not be described as one -- it cannot tell
    speech from a slammed door. What it reliably catches is the failure that
    actually happens during enrollment: a sample that is mostly silence because the
    person stopped talking or the microphone was muted.
    """
    frame_bytes = 2 * max(1, channels)
    window_frames = max(1, int(sample_rate_hz * window_ms / 1000))
    window_bytes = window_frames * frame_bytes
    if window_bytes <= 0 or len(pcm) < window_bytes:
        return 0.0

    overall = analyse_block(pcm, channels)
    if overall.frames == 0:
        return 0.0
    threshold_dbfs = overall.rms_dbfs - dynamic_range_db

    active = 0
    total = 0
    for offset in range(0, len(pcm) - window_bytes + 1, window_bytes):
        levels = analyse_block(pcm[offset : offset + window_bytes], channels)
        total += 1
        if levels.rms_dbfs >= threshold_dbfs:
            active += 1
    return (active / total) if total else 0.0


def estimate_snr_db(rms_dbfs: float, noise_floor_dbfs: float) -> float | None:
    """Signal level minus the measured noise floor, or ``None`` if unmeasurable.

    An estimate, not a measurement: it assumes the noise floor represents noise and
    the RMS represents signal-plus-noise. That holds for a sample containing natural
    pauses and fails completely when it does not -- the Phase 2 floor is a low
    percentile of block RMS, so a steady signal makes floor and RMS coincide.

    Returning ``None`` in that case is the honest answer. Reporting 0 dB would flunk
    a healthy sample; reporting a made-up figure would be worse.
    """
    if not math.isfinite(rms_dbfs) or not math.isfinite(noise_floor_dbfs):
        return None
    gap = rms_dbfs - noise_floor_dbfs
    if gap < SNR_UNMEASURABLE_DB:
        return None
    return gap


@dataclass(slots=True)
class SampleQuality:
    """Quality of one enrollment sample."""

    index: int
    seconds: float
    frames: int
    snapshot: QualitySnapshot
    speech_active_ratio: float
    estimated_snr_db: float | None
    dropped_frames: int
    xrun_callbacks: int
    gates: list[QualityGate] = field(default_factory=list)

    @property
    def status(self) -> GateStatus:
        if any(g.status is GateStatus.REJECT for g in self.gates):
            return GateStatus.REJECT
        if any(g.status is GateStatus.WARN for g in self.gates):
            return GateStatus.WARN
        return GateStatus.PASS

    @property
    def accepted(self) -> bool:
        return self.status is not GateStatus.REJECT

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "seconds": round(self.seconds, 2),
            "frames": self.frames,
            "status": self.status.value,
            "accepted": self.accepted,
            "levels": self.snapshot.to_dict(),
            "speech_active_ratio": round(self.speech_active_ratio, 4),
            "estimated_snr_db": (
                None if self.estimated_snr_db is None
                else round(self.estimated_snr_db, 2)
            ),
            "dropped_frames": self.dropped_frames,
            "xrun_callbacks": self.xrun_callbacks,
            "gates": [g.to_dict() for g in self.gates],
        }


def evaluate_sample(
    *,
    index: int,
    pcm: bytes,
    channels: int,
    sample_rate_hz: int,
    dropped_frames: int,
    xrun_callbacks: int,
    thresholds: EnrollmentQualityThresholds | None = None,
) -> SampleQuality:
    """Judge one captured sample. Holds the audio only long enough to measure it."""
    limits = thresholds or EnrollmentQualityThresholds()
    from mom_igd.audio.backend import CaptureProfile, SampleFormat
    from mom_igd.audio.quality import QualityMeter

    # Reuse the Phase 2 meter verbatim rather than recomputing levels here, so an
    # enrollment sample and a meeting recording are judged by identical arithmetic.
    profile = CaptureProfile(
        sample_rate=sample_rate_hz,
        channels=channels,
        sample_format=SampleFormat.INT16,
        chunk_seconds=30,
    )
    meter = QualityMeter(profile)
    meter.add(pcm)
    snapshot = meter.cumulative_snapshot()

    frame_bytes = 2 * max(1, channels)
    frames = len(pcm) // frame_bytes
    seconds = frames / sample_rate_hz if sample_rate_hz else 0.0
    ratio = estimate_speech_active_ratio(
        pcm, channels=channels, sample_rate_hz=sample_rate_hz
    )
    snr = estimate_snr_db(snapshot.rms_dbfs, snapshot.noise_floor_dbfs)

    gates = [
        _gate(
            "duration",
            seconds >= limits.min_sample_seconds,
            f"{seconds:.1f} s captured; at least {limits.min_sample_seconds:.0f} s "
            f"is needed and about {limits.target_sample_seconds:.0f} s is ideal.",
            measured=round(seconds, 2),
            threshold=limits.min_sample_seconds,
        ),
        _gate(
            "not_silent",
            snapshot.verdict is not LevelVerdict.NO_SIGNAL,
            f"level verdict {snapshot.verdict.value}: {snapshot.verdict.advice}",
            measured=snapshot.verdict.value,
        ),
        _gate(
            "no_clipping",
            snapshot.clipping_percent <= limits.max_clipping_percent,
            f"{snapshot.clipping_percent:.3f}% of samples clipped; the limit is "
            f"{limits.max_clipping_percent}%. Clipped audio loses the detail a "
            "voiceprint depends on.",
            measured=round(snapshot.clipping_percent, 4),
            threshold=limits.max_clipping_percent,
        ),
        _gate(
            "level_not_too_low",
            snapshot.rms_dbfs >= limits.min_rms_dbfs,
            f"RMS {snapshot.rms_dbfs:.1f} dBFS against a {limits.min_rms_dbfs:.0f} "
            "dBFS floor. Move closer to the microphone or raise the Windows input "
            "level.",
            measured=round(snapshot.rms_dbfs, 2),
            threshold=limits.min_rms_dbfs,
        ),
        _gate(
            "peak_headroom",
            snapshot.peak_dbfs <= limits.max_peak_dbfs,
            f"peak {snapshot.peak_dbfs:.1f} dBFS; keep it below "
            f"{limits.max_peak_dbfs:.0f} dBFS.",
            measured=round(snapshot.peak_dbfs, 2),
            threshold=limits.max_peak_dbfs,
            warn_only=True,
        ),
        _gate(
            "mostly_speech",
            ratio >= limits.min_speech_active_ratio,
            f"{ratio * 100:.0f}% of the sample is above the noise floor; at least "
            f"{limits.min_speech_active_ratio * 100:.0f}% is needed. Keep talking "
            "for the whole sample.",
            measured=round(ratio, 4),
            threshold=limits.min_speech_active_ratio,
        ),
        _gate(
            "silence_share",
            snapshot.silence_percent <= limits.max_silence_percent,
            f"{snapshot.silence_percent:.0f}% of the sample is silent; the limit is "
            f"{limits.max_silence_percent:.0f}%.",
            measured=round(snapshot.silence_percent, 2),
            threshold=limits.max_silence_percent,
        ),
        (
            QualityGate(
                key="estimated_snr",
                status=GateStatus.NOT_MEASURED,
                detail=(
                    "the sample contains no quiet passage to estimate noise from, so "
                    "SNR is unmeasurable here. This is not a fault; it is what a "
                    "steady signal looks like."
                ),
                threshold=limits.min_snr_db,
            )
            if snr is None
            else _gate(
                "estimated_snr",
                snr >= limits.min_snr_db,
                f"estimated SNR {snr:.1f} dB against a {limits.min_snr_db:.0f} dB "
                "floor. Reduce room noise or move the microphone closer.",
                measured=round(snr, 2),
                threshold=limits.min_snr_db,
            )
        ),
        _gate(
            "no_dropped_frames",
            dropped_frames <= limits.max_dropped_frames,
            f"{dropped_frames} frame(s) dropped. A gap in an enrollment sample is a "
            "hole in biometric evidence; re-record it.",
            measured=dropped_frames,
            threshold=limits.max_dropped_frames,
        ),
        _gate(
            "no_xruns",
            xrun_callbacks <= limits.max_xrun_callbacks,
            f"{xrun_callbacks} driver overflow(s) reported.",
            measured=xrun_callbacks,
            threshold=limits.max_xrun_callbacks,
        ),
    ]
    return SampleQuality(
        index=index,
        seconds=seconds,
        frames=frames,
        snapshot=snapshot,
        speech_active_ratio=ratio,
        estimated_snr_db=snr,
        dropped_frames=dropped_frames,
        xrun_callbacks=xrun_callbacks,
        gates=gates,
    )


@dataclass(slots=True)
class EnrollmentQualityReport:
    """Whole-enrollment verdict across every sample and the embeddings."""

    samples: list[SampleQuality]
    gates: list[QualityGate]
    total_speech_seconds: float
    min_pair_cosine: float | None
    mean_pair_cosine: float | None

    @property
    def status(self) -> GateStatus:
        everything = list(self.gates) + [g for s in self.samples for g in s.gates]
        if any(g.status is GateStatus.REJECT for g in everything):
            return GateStatus.REJECT
        if any(g.status is GateStatus.WARN for g in everything):
            return GateStatus.WARN
        return GateStatus.PASS

    @property
    def accepted(self) -> bool:
        return self.status is not GateStatus.REJECT

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "accepted": self.accepted,
            "total_speech_seconds": round(self.total_speech_seconds, 2),
            "min_pair_cosine": (
                None if self.min_pair_cosine is None else round(self.min_pair_cosine, 4)
            ),
            "mean_pair_cosine": (
                None if self.mean_pair_cosine is None else round(self.mean_pair_cosine, 4)
            ),
            "gates": [g.to_dict() for g in self.gates],
            "samples": [s.to_dict() for s in self.samples],
        }


def pairwise_cosines(embeddings: Sequence[Sequence[float]]) -> list[float]:
    """Cosine similarity of every unordered pair.

    Intra-enrollment only: all vectors come from the same consenting person in one
    session. Comparing across people is speaker identification (Phase 6).
    """
    values: list[float] = []
    for i in range(len(embeddings)):
        for j in range(i + 1, len(embeddings)):
            values.append(cosine_similarity(embeddings[i], embeddings[j]))
    return values


def evaluate_enrollment(
    *,
    samples: Sequence[SampleQuality],
    embeddings: Sequence[Sequence[float]],
    device_fingerprint: str | None,
    selected_fingerprint: str | None,
    device_transport: str | None,
    calibration_age_days: float | None,
    calibration_verdict: str | None,
    thresholds: EnrollmentQualityThresholds | None = None,
) -> EnrollmentQualityReport:
    """Judge the enrollment as a whole.

    Device and calibration provenance are gates here rather than warnings because a
    template built through an unverified path would be indistinguishable, later,
    from one built properly.
    """
    limits = thresholds or EnrollmentQualityThresholds()
    accepted = [s for s in samples if s.accepted]
    total_speech = sum(s.seconds * s.speech_active_ratio for s in accepted)

    gates: list[QualityGate] = [
        _gate(
            "total_speech_duration",
            total_speech >= limits.min_total_speech_seconds,
            f"{total_speech:.1f} s of speech across {len(accepted)} accepted "
            f"sample(s); at least {limits.min_total_speech_seconds:.0f} s is needed.",
            measured=round(total_speech, 2),
            threshold=limits.min_total_speech_seconds,
        ),
        _gate(
            "device_consistency",
            bool(device_fingerprint)
            and (
                selected_fingerprint is None
                or device_fingerprint == selected_fingerprint
            ),
            "every sample was captured on the selected device"
            if device_fingerprint
            else "no capture device fingerprint was recorded, so the template cannot "
            "be tied to a microphone",
            measured=device_fingerprint,
            threshold=selected_fingerprint,
        ),
    ]

    if calibration_age_days is None:
        gates.append(
            QualityGate(
                key="calibration_freshness",
                status=GateStatus.REJECT,
                detail=(
                    "no calibration evidence was found. Run `audio calibrate` with "
                    "the enrollment microphone before enrolling."
                ),
                threshold=limits.max_calibration_age_days,
            )
        )
    else:
        gates.append(
            _gate(
                "calibration_freshness",
                calibration_age_days <= limits.max_calibration_age_days,
                f"calibration is {calibration_age_days:.1f} days old; the limit is "
                f"{limits.max_calibration_age_days:.0f} days.",
                measured=round(calibration_age_days, 2),
                threshold=limits.max_calibration_age_days,
            )
        )
        gates.append(
            _gate(
                "calibration_verdict",
                calibration_verdict == "GOOD",
                f"last calibration verdict was {calibration_verdict!r}; it must be "
                "GOOD before enrolling a voice.",
                measured=calibration_verdict,
                threshold="GOOD",
            )
        )

    # Production eligibility is reported, not enforced here: an INTERNAL microphone
    # yields a DEVELOPMENT_ONLY template rather than a rejected enrollment, because
    # development enrollment is a legitimate activity.
    gates.append(
        QualityGate(
            key="production_device",
            status=(
                GateStatus.PASS
                if device_transport == "USB"
                else GateStatus.WARN
            ),
            detail=(
                "captured on a Windows-verified USB device"
                if device_transport == "USB"
                else f"captured on a {device_transport or 'UNKNOWN'} device, so the "
                "voiceprint will be marked DEVELOPMENT_ONLY and is not production "
                "eligible"
            ),
            measured=device_transport,
            threshold="USB",
        )
    )

    cosines = pairwise_cosines(embeddings) if len(embeddings) >= 2 else []
    if not cosines:
        gates.append(
            QualityGate(
                key="intra_speaker_consistency",
                status=GateStatus.NOT_MEASURED,
                detail=(
                    "fewer than two embeddings, so sample-to-sample consistency "
                    "could not be measured"
                ),
                threshold=limits.min_intra_speaker_cosine,
            )
        )
        min_cos = mean_cos = None
    else:
        min_cos = min(cosines)
        mean_cos = sum(cosines) / len(cosines)
        gates.append(
            _gate(
                "intra_speaker_consistency",
                min_cos >= limits.min_intra_speaker_cosine,
                f"lowest sample-to-sample cosine {min_cos:.3f} (mean "
                f"{mean_cos:.3f}); at least "
                f"{limits.min_intra_speaker_cosine:.2f} is required. A low value "
                "means the samples do not sound like one voice -- check that the "
                "same person spoke each time and that the microphone did not move.",
                measured=round(min_cos, 4),
                threshold=limits.min_intra_speaker_cosine,
            )
        )

    return EnrollmentQualityReport(
        samples=list(samples),
        gates=gates,
        total_speech_seconds=total_speech,
        min_pair_cosine=min_cos,
        mean_pair_cosine=mean_cos,
    )
