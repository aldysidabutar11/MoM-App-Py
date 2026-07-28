# ADR-0006 — Capture format: PCM16 at the device's native rate

* **Status:** Accepted
* **Phase:** 2

## Context

Phase 2 must capture up to a three-hour meeting on a laptop while staying
lightweight, and hand Phase 4 audio good enough to transcribe Indonesian speech
mixed with English technical terms. The candidate axes were sample format (int16,
int24, float32), sample rate (native vs forced 48 kHz vs 16 kHz for ASR), channel
handling (native, forced mono, forced stereo) and compression (none vs FLAC).

Phase 0 established the constraints: 16 GB RAM with roughly 4 GB free, no C/C++
toolchain, and a strong preference for prebuilt wheels.

## Decision

**Signed 16-bit little-endian PCM, at the device's native sample rate, at the
device's native channel count capped at two, uncompressed, in a WAV container
written by the standard-library `wave` module.**

1. **int16.** It is what every conference microphone delivers natively, what `wave`
   writes without a codec, and what keeps the audio callback free of conversion
   work. Higher bit depths buy dynamic range that speech in a meeting room does not
   have, and would inflate storage for no accuracy gain.
2. **Native sample rate, never resampled during capture.** Resampling in the audio
   callback is exactly the kind of work that makes a driver miss its deadline.
   Resampling in the writer would spend CPU during the meeting to produce something
   Phase 4 can derive afterwards at no cost. If the native rate is not one the
   application accepts, it falls back to 48 kHz rather than resampling.
3. **Native channel count, capped at 2.** A mono microphone is never inflated to
   fake stereo (it would double storage for duplicated data) and stereo is never
   downmixed during capture (spatial information cannot be recovered, and it is
   genuinely useful to Phase 5 diarization). Devices reporting more than two
   channels are clamped, because Phase 2 handles mono and stereo only.
4. **No compression.** FLAC would roughly halve storage, but it needs `libsndfile`
   or an encoder, costs CPU during the meeting, and turns a bit-flip into a
   decode failure rather than one damaged sample. Disk is not the scarce resource
   here: 197 GB free is about 140 two-hour stereo meetings.
5. **The 16 kHz mono working copy for ASR is a processing step, not a capture
   step.** It is derived from the master in Phase 4, where CPU is available and
   nothing is time-critical.
6. **`NumPy` is not a dependency.** `sounddevice.RawInputStream` delivers bytes, and
   the quality meter uses `array.array` from the standard library.

### Consequences for the peak measurement

int16 is asymmetric: the negative rail reaches −32768 while the positive rail stops
at +32767, so a hard-clipped signal has a peak *magnitude* of 32768. Magnitude is
normalised against 32768 and dBFS is clamped at 0.0. Normalising against 32767 would
report a positive dBFS, which is nonsense — this was caught by a test asserting the
wrong thing about `ClippingSource`.

### `audioop` was deliberately avoided

It would compute RMS faster in C, but it was removed from the standard library in
Python 3.13, so depending on it would put a hard blocker in front of the next
interpreter upgrade. The meter instead keeps the multiply-accumulate loop in C with
`sum(map(operator.mul, lane, lane))`, and `array.count()` counts hard-clipped
samples at C speed.

## Relationship to the capture / AI separation

Phase 2 deliberately keeps *no* AI in the capture path. That rule is not a new
decision — it is the intersection of this ADR and
[ADR-0004](0004-single-heavy-worker-resource-policy.md), so it is recorded here
rather than duplicated into a fourth document:

| Rule | Where it is decided |
|---|---|
| Capture runs no model, and the callback never loads one | ADR-0004 §5 ("the recorder never loads a heavy model"), plus the zero-conversion callback above |
| Capture never resamples and never runs ASR | This ADR, §2 and §5 |
| A recording and a heavy worker never run concurrently | ADR-0004 §5, enforced by the job state machine rather than by convention |
| Normalisation and ASR happen *after* the meeting | This ADR §5; the `normalize_audio` and `asr_pass1` stages in `docs/architecture.md` §4 |
| Only lightweight work happens during a meeting | `docs/architecture.md` §1, "the split that makes it feasible" |

The practical consequence for anyone editing `mom_igd/audio/`: if a change would
make the capture path import a model runtime, resample, or hold a heavy allocation
across the meeting, it contradicts one of the rows above and belongs in Phase 4.

## Consequences

**Good.** Zero conversion in the callback. No codec, resampler or DSP dependency —
one new distribution (`sounddevice`) for the whole phase. A bit-flip damages one
sample and is caught by the chunk checksum rather than breaking a decoder. Every
byte written is byte-for-byte comparable against a deterministic test source, which
is what makes "no frame lost, reordered or duplicated" provable rather than
plausible.

**Bad / accepted.** Roughly twice the storage of FLAC: 691 MB/h for 48 kHz stereo.
A device that reports an unusual native rate is recorded at 48 kHz instead, which is
a resample by the *driver* rather than by us. Devices with more than two channels
lose the extra channels; no conference microphone in scope has more.

**Rejected alternatives.** Forcing 16 kHz mono at capture (irreversibly discards
information that Phase 5 diarization benefits from, to save disk that is not
scarce). float32 (double the size, no benefit for speech). FLAC at capture (encoder
dependency, CPU during the meeting, worse corruption behaviour) — it remains a
reasonable option for a later archival step.
