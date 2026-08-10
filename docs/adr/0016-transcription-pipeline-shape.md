# ADR-0016 — The transcription pipeline: working copy, windows, revisions

* **Status:** Accepted
* **Phase:** 4
* **Relates to:** ADR-0004 (one heavy worker), ADR-0007 (durability order), ADR-0014
  (provider selection), ADR-0015 (model provisioning)

## Context

ADR-0014 chose the engine and ADR-0015 established how a model gets onto the machine
verifiably. What remained was the shape of the pipeline around them: what the model reads,
how the work is divided, what is stored, and what happens on a second run.

Four decisions here were **changed by measurement**, not reasoned to. Each one looked
correct in review and was wrong in a way only a real recording exposed, so each is recorded
with what it cost.

## Decision

### 1. The models read a working copy, not the master

Phase 2 captures at the device's native format — 48 kHz stereo on the target hardware,
split across chunk files. Whisper wants 16 kHz mono. Handing the engine the master works,
because the library resamples internally, and that is exactly the problem: the conversion
becomes invisible, unversioned and unmeasured, and the audio the model actually saw is not
something anybody can inspect.

So normalisation is its own stage with an input hash, an output hash, a duration and a row
in `audio_working_copies`. The master is opened read-only and never modified; a test hashes
every chunk before and after a full run.

Resampling is linear interpolation over integer sample positions, in pure Python against
`array`. Not the best resampler available — but auditable, deterministic, dependency-free,
and adequate for speech at these rates. The alternative was handing the file to the FFmpeg
build inside `av`, which would make the working copy's content depend on a binary this
repository does not control. A test asserts a 440 Hz tone survives with the right number of
zero crossings, and that a blocked conversion is sample-identical to a single-shot one —
without carrying the boundary state between blocks, every block seam becomes a click.

### 2. A gap is filled in the copy and recorded on the row

Phase 2's rule is that a gap in the master is recorded and never filled, and CLAUDE.md gives
the reason: *an invisible gap shifts every downstream timestamp*.

A working copy is not evidence — it is the model's input, and its entire job is to carry the
master's timeline. Each chunk is therefore placed at the frame offset the manifest recorded
for it, and a hole becomes explicit silence, so a transcript timestamp equals an offset into
the meeting. The gap does not become invisible: `gap_count`, `gap_total_ms` and `gaps_json`
record every one, and a speech region overlapping one is flagged so a reviewer can see that
part of the audio under it is synthetic.

Filled **and** recorded is the only combination that keeps both the timeline and the truth.
A missing chunk is treated the same way — a gap of its known length, named in
`skipped_chunks` — because one lost chunk must not cost the operator the rest of the meeting.

### 3. Regions are decoded in batched 30-second windows

**This is the change that decided whether Phase 4 meets its real-time target.**

The obvious design is one decode per speech region: it bounds memory, and it gives
cancellation a clean boundary. Measured on a 24-second recording that VAD split into ten
regions, it gave **RTF 2.8** — nearly three times slower than real time, against a target of
1.0.

The cause is architectural: Whisper's encoder always consumes a 30-second window and pads
it with silence. Decoding a two-second region costs almost exactly what decoding thirty
seconds costs. Ten short regions therefore cost ten full windows.

Consecutive regions are now grouped into **contiguous spans of at most 30 seconds** — from
the first region's start to the last one's end, silence between them included. The same
recording then ran at **RTF 0.31**, a ninefold improvement, with peak worker memory falling
from 1 577 MiB to 592 MiB because pass 2 was no longer dragged in by false selections.

Two things were deliberately *not* done:

* **Concatenating only the speech**, skipping the silence between regions, would be cheaper
  still and would corrupt every timestamp, because the returned times would no longer map
  linearly onto the recording.
* **Passing the whole file with a multi-clip timestamp list** would leave the engine to
  decide the batching, and would give cancellation nothing to land on.

The batch, not the region, is now the cancellation boundary. That is the trade: at most 30
seconds of work is discarded on a cancel, in exchange for up to a fifteenfold reduction in
decode cost.

**Reading the audio was wrong twice, in the same way.** The first version called
`model.transcribe(path, clip_timestamps=…)` once per region, and faster-whisper decodes the
**entire file** on every call. The fix — read it once into an array and slice — was applied
inside `transcribe`, which looked complete and was not: the pipeline calls `transcribe`
*once per window*, so a 90-minute meeting still read the file 144 times. Measured: 7.6 s per
read, **18.2 minutes of waste against a pass-1 decode of about 13 minutes.** The overhead
was larger than the work.

Both versions are O(windows × duration), and neither is visible on the 24-second end-to-end
test, which produces exactly one window. The working copy is now held on the provider for as
long as it is needed, keyed on the file's path, size and modification time — a stale audio
cache would transcribe a different meeting and produce a transcript that looks entirely
plausible.

It is held as **int16** and converted to the float32 the engine wants one window at a time.
A three-hour working copy is 172 MB as int16 against 345 MB as float32, and the pass-2 model
already occupies 1.9 GB of the 2.5 GB budget; the per-window conversion touches about 2 MB
and costs nothing measurable. Reading the WAV directly with `wave` plus NumPy is
byte-identical to `decode_audio` — asserted by a test, not assumed — and about 20× faster,
because the working copy's format is fixed by stage 1 and there is nothing to negotiate.

### 4. Pass 2 supersedes, and its selection is explained by reason codes

Pass 2 re-transcribes the least confident regions with a slower configuration, under a
budget expressed as a fraction of detected speech (25 % by default). Its output does not
overwrite pass 1: the pass-2 segments are inserted with `asr_pass = 2` and the pass-1
segments covering the same region are marked inactive and pointed at their replacement.
Both rows survive.

That costs rows and buys three things nothing else does. A reviewer can see what the second
pass changed. The evidence chain Phase 8 verifies stays intact. And "pass 2 improved the
flagged subset", one of the Phase 4 acceptance targets, becomes checkable rather than
asserted — `text_changed_regions` counts the regions that actually came back different.

Selection is deterministic and every choice carries a named reason code
(`LOW_AVG_LOGPROB`, `REPETITION_SUSPECTED`, `DECODER_FELL_BACK`, `LOW_WORD_CONFIDENCE`,
`HIGH_NO_SPEECH_PROB`, `EMPTY_IN_SPEECH_REGION`). A region is never selected because a score
came out high; it is selected because a rule fired, and the rule is stored on the segment.
Two of the strongest signals — a speaker change inside a region, and overlapping speech — do
not exist until diarization lands in Phase 5, and the rule table is built so those become
additional codes rather than a rewrite.

**Three defects here were found by running the pipeline against a real recording, and every
one of them had passed a plausible unit test:**

* **Segments arrived with no region attribution.** `validate_transcription` rebuilds every
  segment rather than mutating it, so `region_index` — added to the dataclass — was silently
  dropped on the way out. Every region then looked empty, every region was flagged
  `EMPTY_IN_SPEECH_REGION`, and the whole pass-2 budget went to the wrong places.
* **A covered region was still called empty.** With batched windows one long segment can
  span several regions and is attributed to the one it overlaps most; the others had nothing
  attributed although there was text over them. Nine regions out of ten were flagged
  falsely. Emptiness is now decided by *overlap*, and a region with no attributed segment
  reads the overlapping ones for its signals — so attribution is an optimisation rather than
  a correctness dependency.
* **One over-budget region blocked the entire pass.** Selection stopped at the first region
  that did not fit. A 6.0-second region against a 5.3-second budget meant nothing at all was
  re-transcribed while nine smaller flagged regions waited behind it. It now skips and
  continues: priority sets the order, and it cannot promise that a region larger than the
  whole budget will run.

`PASS2_BUDGET_TOO_SMALL` is also now distinct from `PASS2_NOTHING_FLAGGED`. "Nothing needed
re-transcribing" and "the budget could not cover anything that did" are different facts
about a transcript, and only one of them is good news.

### 5. Terminology normalisation keeps the original

Whisper spells English technical vocabulary by ear: "deploy" comes back as "deploi",
"database" as "data base". Those are reasonable phonetic transcriptions and useless terms,
because a reader searching for "deploy" will not find "deploi".

`config/glossary.id-en.toml` maps reviewed misspellings to canonical forms, and it is used
twice: as a bounded initial prompt so the model has the right spellings in context, and as a
post-decode normaliser. The normaliser is a **spelling** corrector and nothing else — no
translation, no paraphrase, no expansion, no summarisation. Anything cleverer would be
editing the record.

The model's original wording is kept in `transcript_segments.text_raw` beside the corrected
`text`, and every replacement is counted, because a transformation of evidence that cannot
be undone is not a transformation but a loss.

Matching is on word boundaries, so "api" never fires inside "apik", and the loader refuses:
a variant shorter than three characters (it would collide with ordinary Indonesian), a
variant mapped to two terms (the winner would depend on file order), and a variant that is
another term's canonical spelling (it would rewrite correct text). A case-only variant
(`bpjs` → `BPJS`) is allowed and does **not** re-fire on its own output, so the replacement
count stays truthful and normalisation is idempotent.

### 6. Re-running writes a new revision

A second run over the same recording writes transcript revision *n+1* and deactivates the
previous one. Nothing is edited in place. The partial unique index on
`transcripts (recording_id) WHERE is_active = 1` makes "at most one current" a database
guarantee, so two concurrent runs cannot both end up current.

The reasoning is the same as APPROVED being terminal in the job state machine: a reviewer
who has read a transcript must be able to see what changed underneath them, and Phase 7's
reconciliation needs the earlier revision to diff against.

### 7. Checkpointed at every stage boundary

A working copy whose recorded SHA-256 still matches the file on disk is reused. A VAD run
whose configuration hash matches the current configuration is reused. Restarting a
three-hour meeting from the beginning because the machine slept is not acceptable, and
re-deriving something provably identical is not evidence — it is waiting.

Both checks are positive: a deleted or tampered working copy is rebuilt, and a retuned
threshold produces a new VAD run with the old one kept inactive as the record of what an
earlier transcript was built from.

## Consequences

* Peak worker memory is one model at a time by construction, because each heavy stage is a
  separate spawned process that exits before the next starts. The measured worst-case
  working sets are 693 MiB and 1 910 MiB; **2 603 MiB together exceeds the 2.5 GB budget**,
  so co-residency is measured to breach it rather than merely being undesirable.
* A missing pass-1 model fails the run as `MODEL_UNAVAILABLE`. A missing *pass-2* model does
  not: the pass-1 transcript stands and the reason is recorded, because a complete first
  pass is worth more than no transcript at all.
* A recording with no detected speech completes as an empty revision with reason
  `NO_SPEECH_DETECTED`. An empty room is a legitimate outcome, not a failure for the
  operator to interpret from an empty transcript.
* Transcription never blocks recording. Capture owns the microphone and a lock file; the
  pipeline owns a worker slot. Neither package imports the other, and an operator can always
  record the next meeting while the last one is still being transcribed.
* `[asr]` in configuration carries the measured thread counts and beam sizes. They are
  machine-specific, and the honest way to retune is to re-run `asr bench` rather than to
  reason about core counts.

## What this does not license

**No accuracy claim.** Every number above is throughput or memory. The end-to-end run that
produced them used synthesised formant audio, which crosses the VAD threshold but is not
speech — so the transcript it produced is meaningless as text, and its RTF is not
representative either (the decoder hallucinates on non-speech input, and hallucination is
slow: the same 24 seconds produced 734 words before the batching fix). WER, technical-term
recall and word-timestamp error remain `N/A — PENDING`, and none of them is ever derived
from the model's own output.

## Alternatives rejected

**Resampling with `av`/FFmpeg.** Available, faster, and it makes the bytes the model sees
depend on a binary build rather than on reviewable code. Kept as the fallback for a corpus
file that is not already a working copy.

**One decode for the whole file.** Simplest of all, and it feeds the model minutes of
silence, gives cancellation nothing to land on, and makes region attribution guesswork.

**Overwriting pass-1 text with pass-2 text.** Fewer rows, no supersession bookkeeping, and
it destroys the only record of what the first pass said — which is the thing the pass-2
acceptance target is measured against.

**A `speaker` column now, filled later.** It would sit NULL for two phases and invite
something to write a guess into it. Phase 4 assigns no speaker at all, and
`validate_transcription` rejects any result that carries one.
