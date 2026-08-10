# Phase 4 — Offline ASR

What this phase builds, what it refuses to build, and exactly what has and has not been
proven about it.

**Status: PHASE 4 READY FOR MANUAL FUNCTIONAL TESTING — ACCURACY ACCEPTANCE PENDING.** Every
stage runs end to end on this machine with the real models, offline, and is covered by
automated tests. No accuracy figure exists, because no reference transcript exists. See
[What is not proven](#6-what-is-not-proven).

**Operator handoff:** run `scripts\phase4_acceptance_preflight.ps1`, then follow
[`phase-4-manual-acceptance.md`](phase-4-manual-acceptance.md).

---

## 1. What it does

One command turns a closed recording into a reviewed-ready transcript revision:

```powershell
.\.venv\Scripts\python.exe -m mom_igd asr transcribe <recording-uuid>
```

Six stages, each persisted before the next begins:

| Stage | What it does | Heavy? |
|---|---|---|
| `validate_audio` | the master's manifest verifies and its chunks are readable | no |
| `normalize_audio` | builds the 16 kHz mono working copy on the master's timeline | no |
| `vad` | finds speech regions with the Silero model bundled in the wheel | worker |
| `asr_pass1` | transcribes every region, fast configuration | worker |
| `asr_pass2_selective` | re-transcribes the least confident regions under a budget, then merges | worker |
| `normalize_terminology` | corrects technical spellings, keeping the original wording | no |

Each heavy stage is a **separate spawned process that exits before the next starts**. That
is not an optimisation: the measured worst-case working sets are 693 MiB and 1 910 MiB, and
2 603 MiB together exceeds the 2.5 GB budget, so co-residency is measured to breach it.

The evidence chain, one table per link:

```
recordings                      (Phase 2, never modified)
  -> audio_working_copies       the 16 kHz mono derivative the models read
       -> vad_runs              one voice-activity pass over that derivative
            -> speech_regions
       -> transcripts           one revision
            -> transcript_segments
                 -> transcript_words
```

Every link records the provenance of the thing above it — a SHA-256, a model revision, a
configuration hash — so a transcript can always be traced back to the exact bytes and the
exact model that produced it.

---

## 2. Commands

```powershell
# Once, with network access. The ONLY command in this application that downloads anything.
.\.venv\Scripts\python.exe -m mom_igd asr provision all

# Offline from here on.
.\.venv\Scripts\python.exe -m mom_igd asr models          # catalogue + what is ready
.\.venv\Scripts\python.exe -m mom_igd asr verify          # re-hash every byte from disk
.\.venv\Scripts\python.exe -m mom_igd asr smoke           # real model, generated audio
.\.venv\Scripts\python.exe -m mom_igd asr smoke --audio <local-16k-mono.wav>
.\.venv\Scripts\python.exe -m mom_igd asr bench --threads 4,8,12 --seconds 60
.\.venv\Scripts\python.exe -m mom_igd asr transcribe <recording-uuid>
.\.venv\Scripts\python.exe -m mom_igd asr transcribe <recording-uuid> --no-pass2
.\.venv\Scripts\python.exe -m mom_igd asr transcript <recording-uuid>
.\.venv\Scripts\python.exe -m mom_igd asr transcript <recording-uuid> --flagged
.\.venv\Scripts\python.exe -m mom_igd asr revisions <recording-uuid>
```

The GUI has the same thing behind **Buka panel transkripsi**. The Transcribe button stays
disabled until a pass-1 model is ready, and the panel tells the operator the provisioning
command rather than offering a download button.

---

## 2b. How long a real meeting takes

A **projection from measured components**, not one measured end-to-end run — the longest
real recording put through this pipeline so far is 24 seconds. Assumes 80 % speech, which
is high for a meeting.

| Stage | Rate | 60 min | 90 min |
|---|---|--:|--:|
| Normalise to the working copy | 0.022 × realtime, measured | 1.3 min | 2.0 min |
| Voice activity detection | 0.003 × realtime, measured | 0.2 min | 0.3 min |
| Read the working copy (once per pass) | 7.6 s per read, measured | 0.2 min | 0.3 min |
| Pass 1 | RTF 0.142, measured | 8.5 min | 12.8 min |
| Pass 2 (25 % budget) | RTF 0.284, measured | 3.4 min | 5.1 min |
| **Total** | | **~14 min** | **~20 min** |

So a 90-minute meeting is a coffee break, not an overnight job — the roadmap's original
"record today, review tomorrow" expectation is more pessimistic than the measurements.

Two caveats. Every rate above was measured on **synthetic** audio; real speech has a
different segment density and may fall back differently, so treat these as the right order
of magnitude rather than a promise. And before the audio-read fix the same 90-minute meeting
would have spent an **extra 18 minutes** re-reading its own working copy 144 times — the
kind of cost that only appears at length, which is why the numbers above are worth
re-measuring against the first real long recording.

---

## 3. Configuration

`[asr]` in `config/default.toml`. Every default was **measured** on the target device
([`benchmarks.md`](benchmarks.md)), not guessed:

| Key | Default | Why |
|---|--:|---|
| `pass1_cpu_threads` / `pass2_cpu_threads` | 12 / 12 | RTF improves monotonically to 12 on the i7-1260P and reverses beyond it; 14 and 16 were measured and are worse |
| `pass1_beam_size` / `pass2_beam_size` | 1 / 5 | beam size dominates throughput — a 2.5× swing. **Provisional: an accuracy-for-speed trade with no accuracy measurement behind it** |
| `compute_type` | `int8` | the only profile that keeps pass 2 inside the memory budget |
| `pass2_budget_ratio` | 0.25 | pass 2 costs about twice pass 1 per second; unbounded, it would put total RTF past the target on a long meeting |
| `initial_prompt_max_chars` | 400 | a long prompt evicts the audio context it is meant to help, and can be echoed into the transcript |
| `worker_timeout_seconds` | 10800 | generous on purpose: measured RTF is ~0.15, so this only fires on something genuinely stuck |

The thread counts are machine-specific. On different hardware, re-run `asr bench` rather
than reasoning from core counts.

---

## 4. Pass 2: what gets re-transcribed, and why

Selection is deterministic and every choice carries a named reason code. A region is never
selected because a score came out high; it is selected because a rule fired, and the rule is
stored on the segment so `asr transcript --flagged` can show it.

| Reason code | Weight | Fires when |
|---|--:|---|
| `DECODER_FELL_BACK` | 3.0 | the decoder raised its temperature, which it only does after rejecting its own first attempt |
| `EMPTY_IN_SPEECH_REGION` | 2.5 | VAD found speech and no segment covers the span |
| `LOW_AVG_LOGPROB` | 2.0 | average token log probability below the floor |
| `REPETITION_SUSPECTED` | 1.75 | compression ratio above 2.4 — Whisper's own repetition heuristic |
| `LOW_WORD_CONFIDENCE` | 1.5 | at least one word below the word-probability floor |
| `HIGH_NO_SPEECH_PROB` | 1.25 | the decoder thinks this may not be speech, yet emitted text |

Regions are ranked worst first, ties break on position in the meeting, and the budget is
spent down the queue. A region larger than the whole budget is **skipped, not treated as a
wall** — an earlier version stopped at the first region that did not fit and blocked
everything behind it. When the budget runs out that fact is recorded rather than the tail
being quietly dropped.

Two of the strongest selection signals — a speaker change inside a region, and overlapping
speech — do not exist until diarization arrives in Phase 5. The rule table is built so they
become additional codes rather than a rewrite.

Reason codes when pass 2 does **not** run:

| Code | Meaning |
|---|---|
| `PASS2_DISABLED` | turned off in configuration |
| `PASS2_NOTHING_FLAGGED` | no rule fired; the pass-1 transcript stands |
| `PASS2_BUDGET_TOO_SMALL` | regions were flagged and none fits the budget. Different news from the line above |
| `PASS2_MODEL_UNAVAILABLE` | the pass-2 model is not provisioned; pass 1 stands |

---

## 5. Terminology

`config/glossary.id-en.toml` maps reviewed misspellings to canonical forms:
"deploi" → "deploy", "data base" → "database", "bpjs" → "BPJS".

It is a **spelling corrector and nothing else** — no translation, paraphrase, expansion or
summarisation. The model's original wording is kept in `transcript_segments.text_raw` and
every replacement is counted, so nothing is silently rewritten.

Adding a term: whole words only (matching is on word boundaries, so "api" never fires inside
"apik"), never a person's or client's name (the file is committed), and never a word that is
ordinary Indonesian — "bug" is safe, "kode" is not. The loader refuses a variant shorter than
three characters, a variant mapped to two terms, and a variant that is another term's
canonical spelling.

---

## 6. What is not proven

**Accuracy is not measured, and this phase must not be cited as evidence that Indonesian
transcription is accurate enough for production.**

No evaluation corpus with reference transcripts exists on this machine. Every measured
number is throughput or memory:

| Target | Status |
|---|---|
| Peak worker RSS `< 2.5 GB` | **PASS** — worst 1 910 MiB over 30 benchmark runs |
| Total RTF `<= 1.0` | **PASS** — 0.31 measured end to end on generated audio; 0.213 projected from the benchmark medians |
| Zero network egress | **PASS, measured** — every outbound primitive intercepted, no attempt recorded |
| One heavy model at a time | **PASS** — separate worker process per stage, and co-residency is measured to breach the budget |
| Master audio unmodified | **PASS** — hashed before and after a full run |
| Clean Indonesian WER `<= 25 %` | **N/A — PENDING** |
| Far-field WER `<= 35 %` | **N/A — PENDING** |
| Word-timestamp error | **N/A — PENDING** |
| Pass 2 improves the flagged subset | **N/A — PENDING** (the mechanism to measure it exists: `text_changed_regions`) |

The end-to-end run used **synthesised formant audio** — a source-filter model, not a
recording of anyone. It crosses the VAD threshold, so it exercises the region path, and it is
not speech, so its transcript is meaningless as text. Its RTF is not representative either:
the decoder hallucinates on non-speech input, and hallucination is slow.

To close the accuracy gap, supply an evaluation manifest to `asr bench --manifest`. It
demands per sample: `sample_uuid`, `audio_path`, `sha256`, `duration_seconds`, `language`,
`reference_transcript_path`, `consent_status` (`granted`, `public-licensed` or `synthetic`),
`license_name`, and optionally `technical_terms`, `word_timestamp_reference_path` and
`condition`. Checksums are verified, a missing sample is an error, and audio without recorded
consent is refused — benchmarking somebody's voice is processing biometric data.

---

## 7. What Phase 4 deliberately does not contain

* **No speaker anywhere.** Diarization is Phase 5 and voice identification is Phase 6. There
  is no `speaker` column in migration 0005, and `validate_transcription` rejects any result
  that carries one. A column sitting NULL for two phases invites something to write a guess
  into it.
* **No text search index.** Phase 7 owns review and search; an FTS table built before its
  query patterns are known is an FTS table rebuilt later.
* **No MoM extraction, no exporter, no LLM.** Phases 8 and 10.
* **No download path outside `asr provision`.** Nothing in the runtime, the API, the shell or
  the pipeline can fetch a model. A missing one is `MODEL_UNAVAILABLE`.
* **No roster gate.** Nothing under `mom_igd/asr/` reads the participant roster. Transcription
  takes the whole room signal, exactly as capture does.

---

## 8. Where to look

| Concern | File |
|---|---|
| Provider choice, thread counts, beam sizes | [`adr/0014-asr-provider-faster-whisper-cpu-int8.md`](adr/0014-asr-provider-faster-whisper-cpu-int8.md) |
| Provisioning, the hash chain, offline loading | [`adr/0015-model-provisioning-and-offline-loading.md`](adr/0015-model-provisioning-and-offline-loading.md) |
| Pipeline shape, and the four defects measurement found | [`adr/0016-transcription-pipeline-shape.md`](adr/0016-transcription-pipeline-shape.md) |
| Measured numbers | [`benchmarks.md`](benchmarks.md) |
| Recovery checkpoint and the verification log | [`phase-4-progress.md`](phase-4-progress.md) |
| Operator's manual test script | [`phase-4-manual-acceptance.md`](phase-4-manual-acceptance.md) |
| Evaluation-corpus templates | [`examples/`](examples/) |
| Schema, with the reasoning inline | `mom_igd/db/migrations/0005_offline_asr.sql` |
