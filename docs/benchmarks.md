# Benchmarks

Measured on the target device. Nothing in this file is estimated, and anything that could
not be measured says so rather than carrying a plausible number.

**Machine:** Windows 11 build 26200 · Intel Core i7-1260P (12 physical / 16 logical) · 16
GB · Intel Iris Xe (no CUDA) · NVMe SSD · Python 3.12.10.

**Repeat before concluding.** A single sweep on a laptop is not a measurement of the
machine, it is a measurement of the machine *and whatever else it was doing*. The
same-configuration spread here reaches 10 %, which is larger than several differences an
earlier single sweep was read as evidence for. Every conclusion below rests on repeated
sequential runs, and one earlier conclusion was withdrawn when it failed to reproduce.

**Reading rule.** Every table states what the audio was. A number measured on synthetic
audio is a measurement of *engine throughput*, and never of accuracy. Accuracy figures are
absent, not low — see [Accuracy is not measured yet](#accuracy-is-not-measured-yet).

---

## Phase 4A — offline ASR provider selection

Machine-readable records, neither containing transcript text, participant data or private
paths:

* [`benchmarks/phase-4a-asr-summary.json`](benchmarks/phase-4a-asr-summary.json) — one
  clean sweep, full per-run detail.
* [`benchmarks/phase-4a-asr-thread-sweep.json`](benchmarks/phase-4a-asr-thread-sweep.json)
  — the aggregate over all five sweeps, with the spread.

Reproduce with:

```powershell
.\.venv\Scripts\python.exe -m mom_igd asr bench `
  --data-dir <model-store> --threads 4,8,12 --seconds 60 `
  --out docs\benchmarks\phase-4a-asr-summary.json
```

Every run executes in a **spawned worker process**, so peak RSS is the ASR process's own
(plus its children) rather than the test runner's. Peak resident memory cannot be recovered
after a process exits, so the parent samples it while the child runs.

### Results

Five sweeps, **strictly sequential on an otherwise idle machine**, identical deterministic
60 s synthetic audio: 30 runs, no errors. Three covered 4/8/12 and two covered 12/14/16,
which is why 12 has five samples.

`RTF = wall-clock ÷ audio duration`, **including model load**, because that is what an
operator waits for. Decode-only time is recorded separately in the JSON.

| model | threads | beam | n | RTF min | **median** | RTF max | spread | peak RSS max | median CPU s |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| `faster-whisper-small` | 4 | 1 | 3 | 0.169 | 0.175 | 0.179 | 6.1 % | 638 MiB | 36.9 |
| `faster-whisper-small` | 8 | 1 | 3 | 0.160 | 0.160 | 0.160 | 0.3 % | 670 MiB | 69.2 |
| `faster-whisper-small` | **12** | 1 | 5 | 0.138 | **0.142** | 0.152 | 10.2 % | 693 MiB | 88.9 |
| `faster-whisper-small` | 14 | 1 | 2 | 0.151 | 0.153 | 0.155 | 2.6 % | 584 MiB | 105.5 |
| `faster-whisper-small` | 16 | 1 | 2 | 0.149 | 0.154 | 0.158 | 5.8 % | 623 MiB | 113.2 |
| `faster-whisper-large-v3-turbo` | 4 | 5 | 3 | 0.367 | 0.372 | 0.395 | 7.5 % | 1 851 MiB | 81.5 |
| `faster-whisper-large-v3-turbo` | 8 | 5 | 3 | 0.316 | 0.318 | 0.322 | 1.8 % | 1 639 MiB | 135.9 |
| `faster-whisper-large-v3-turbo` | **12** | 5 | 5 | 0.275 | **0.284** | 0.297 | 7.9 % | 1 910 MiB | 176.6 |
| `faster-whisper-large-v3-turbo` | 14 | 5 | 2 | 0.287 | 0.291 | 0.296 | 3.0 % | 1 685 MiB | 201.0 |
| `faster-whisper-large-v3-turbo` | 16 | 5 | 2 | 0.300 | 0.302 | 0.305 | 1.6 % | 1 631 MiB | 217.9 |

Both models: CPU, INT8, `condition_on_previous_text=False`, `word_timestamps=True`,
language `id`, 2 segments per run (112 words for small, 6 for turbo — a tone is not
speech). Manifest SHA-256 for each run is in the JSON.

### Findings

**Both models want 12 threads.** RTF improves monotonically to 12 and reverses beyond it;
14 and 16 were measured and are consistently worse. Twelve is this machine's physical core
count.

> **A withdrawn finding.** An earlier single sweep put the small model's optimum at 8
> threads and called 12 "measurably worse" (0.192 vs 0.174). It did not reproduce. Five
> repeats put 12 clearly ahead for both models, and the same-configuration spread (up to
> 10 %) is of the same order as the gap that finding rested on. It is withdrawn, not
> amended: the mistake was concluding from one sweep, and recording that is more useful
> than quietly replacing the numbers.

Two of the discarded sweeps also ran **concurrently with each other**, which contended for
CPU and exposed a real defect: the benchmark wrote its synthetic audio to a fixed filename,
so whichever run finished first deleted the file the other was still decoding — four runs
failed with `the working copy to transcribe does not exist`. The filename is now
per-process, and those sweeps were discarded rather than averaged in.

**Beam size dominates throughput.** On identical audio, `beam_size=5` gave RTF **0.711**
and `beam_size=1` with `temperature=0` gave **0.285** — a 2.5× swing, larger than any thread
effect. Pass-1 therefore uses `beam_size=1` and pass-2 uses `beam_size=5`.

> **This is a speed-for-accuracy trade made without an accuracy measurement.** It was chosen
> on throughput evidence alone and must be revisited the first time a reference transcript
> exists. It is recorded as provisional, not validated.

**The two models cannot be resident together.** Worst pass-1 worker 693 MiB + worst pass-2
worker 1 910 MiB = **2 603 MiB (2.54 GiB), over the 2.5 GB budget**. Co-residency is
measured to breach it, which is direct evidence for the one-heavy-worker policy (ADR-0004)
and for unloading pass-1 before loading pass-2.

**Load time is not negligible for the large model** — 3.3 s median, and up to 12 s on a cold
page cache — which argues against reloading it per region.

### Gate

| Target | Verdict |
|---|---|
| Peak worker RSS `< 2.5 GB` | **PASS** — worst single worker 1 910 MiB (1.87 GiB) across 30 runs |
| Phase 4 total RTF `<= 1.0` | **PASS by projection** — 0.142 + 0.25 × 0.284 ≈ **0.213**. Arithmetic over two measured medians, not one end-to-end run; labelled as a projection |
| Zero network egress | **PASS, measured** — every outbound primitive intercepted during each decode; `network_attempts == []` on all 30 runs. The recorder was itself defective in the first attempt (below) and was fixed before these runs |
| No OOM, crash or silent fallback | **PASS** — 30 runs, no errors |
| Clean Indonesian WER `<= 25 %` | **N/A — PENDING** |
| Far-field WER `<= 35 %` | **N/A — PENDING** |
| Median / P95 word-timestamp error | **N/A — PENDING** |
| Pass-2 improves the flagged subset | **N/A — PENDING** |
| Thermal / clock evidence | **N/A — sensor unavailable on Windows.** `psutil.sensors_temperatures` is not implemented on this platform and the WMI thermal-zone class is absent or stale on consumer laptops. No number is invented |

### The egress recorder was itself broken

Worth recording because it is the failure mode evidence is most vulnerable to. The first
benchmark reported `network_attempts == []`, and that was true for a reason unrelated to the
network: the transcription task built its result dictionary **inside** the `try` block, while
the `finally` that drains the interception recorder ran afterwards. The list was frozen empty
before anything could be appended, so the field could not have reported an attempt however
hard the decoder tried to make one.

The interception was real — every outbound primitive was patched for the duration of each
decode, so a call would have raised — but the *recorded* evidence was vacuous. **A field that
cannot fail is worse than no field**, because it reads as a measurement. The result is now
built after the `finally`, a unit test performs a real `getaddrinfo` from inside a decode and
asserts it appears in the payload, and the 30 runs above were measured with the fix in place.

### Accuracy is not measured yet

No evaluation corpus with reference transcripts exists on this machine, so timing was
measured on **deterministic synthetic audio** (an amplitude-modulated tone with silence
gaps, generated in-process — no human voice, nothing committed).

The decoder does real work on it: segments and words are produced, and configuration changes
throughput measurably. So the timings above are a valid measurement of engine throughput on
this hardware.

They are **not** a substitute for real speech. Segment density differs and the
temperature-fallback path triggers differently — note the turbo rows produce 6 words where
small produces 112, which is itself a sign that a tone is not speech.

And they say nothing about accuracy. **WER is `N/A` because there is no reference
transcript, and it is never derived from the model's own output.** Comparing a model against
its own transcription measures self-consistency, not correctness.

**Accuracy acceptance for Phase 4 is PENDING.** To close it, supply an evaluation manifest:

```powershell
.\.venv\Scripts\python.exe -m mom_igd asr bench `
  --data-dir <model-store> --manifest <path-to-corpus.json> `
  --out docs\benchmarks\phase-4a-asr-accuracy.json
```

The manifest references audio **outside** the repository and must declare, per sample:
`sample_uuid`, `audio_path`, `sha256`, `duration_seconds`, `language`,
`reference_transcript_path`, `consent_status` (`granted`, `public-licensed` or `synthetic`),
`license_name`, optional `technical_terms`, optional `word_timestamp_reference_path`, and
`condition` (e.g. `clean`, `far-field`). Checksums are verified on load and a missing sample
is an error — a benchmark must not silently shrink its corpus and flatter the result.

Priority for sourcing: a locally licensed corpus with transcripts · a small, clearly licensed
public Indonesian subset · in-house meeting audio with recorded consent and a manual
reference. At least 10 minutes of consented far-field audio is needed for the far-field
target.

---

## Phase 2 — audio capture

See [`phase-2-audio-capture.md`](phase-2-audio-capture.md) for the capture benchmark
(`audio bench`), which measures the fake-backend writer path: frames written vs produced vs
requested, capture drift, queue high-water mark and chunk integrity. It loads no model and
opens no microphone.
