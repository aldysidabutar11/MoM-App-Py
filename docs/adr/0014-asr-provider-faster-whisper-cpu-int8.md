# ADR-0014 — ASR provider: faster-whisper / CTranslate2, CPU INT8

* **Status:** Accepted for Phase 4 implementation. **Accuracy acceptance is deferred**
  and explicitly not granted by this decision.
* **Phase:** 4A (benchmark) → 4 (implementation)
* **Supersedes:** the deferral in ADR-0005, for the ASR slot only. Diarization, speaker
  embedding and the LLM remain deferred.

## Context

ADR-0005 refused to pick an AI provider without measurements, and listed exactly what was
unknown: whether `whisper.cpp` could be built here at all, whether the Iris Xe could be
targeted, and what the real-time factor and resident memory actually are. Phase 4A
measured what could be measured on this machine. This ADR records the decision that
follows, and — just as importantly — what the measurements do **not** license.

## Decision

**Pass-1 and pass-2 both run faster-whisper on CTranslate2, CPU, INT8.**

| | Pass 1 | Pass 2 |
|---|---|---|
| Model | `faster-whisper-small` | `faster-whisper-large-v3-turbo` |
| Source | `Systran/faster-whisper-small` | `deepdml/faster-whisper-large-v3-turbo-ct2` |
| Revision | `536b0662742c…` | `4df90f753211…` |
| Licence | MIT | MIT |
| Compute | CPU INT8 | CPU INT8 |
| Threads | **12** | **12** |
| Beam size | **1** | **5** |
| Scope | every VAD speech region | flagged regions only, under a duration budget |

## Evidence

Measured on the target device: Windows 11 build 26200, i7-1260P (12 physical / 16 logical),
16 GB, Python 3.12.10, ctranslate2 4.8.1, faster-whisper 1.2.1. Every run in a spawned
worker so peak RSS is the ASR process's own.

**Five sweeps, run strictly sequentially on an otherwise idle machine**, on identical
deterministic 60 s synthetic audio — 30 runs, no errors. Three covered 4/8/12 threads and
two covered 12/14/16. Per-run detail:
[`benchmarks/phase-4a-asr-summary.json`](benchmarks/phase-4a-asr-summary.json). Aggregate
with the spread:
[`benchmarks/phase-4a-asr-thread-sweep.json`](benchmarks/phase-4a-asr-thread-sweep.json).

| model | thr | beam | n | RTF min | **RTF median** | RTF max | spread | peak RSS max | WER |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| small | 4 | 1 | 3 | 0.169 | 0.175 | 0.179 | 6.1 % | 638 MiB | N/A |
| small | 8 | 1 | 3 | 0.160 | 0.160 | 0.160 | 0.3 % | 670 MiB | N/A |
| small | **12** | 1 | 5 | 0.138 | **0.142** | 0.152 | 10.2 % | 693 MiB | N/A |
| small | 14 | 1 | 2 | 0.151 | 0.153 | 0.155 | 2.6 % | 584 MiB | N/A |
| small | 16 | 1 | 2 | 0.149 | 0.154 | 0.158 | 5.8 % | 623 MiB | N/A |
| turbo | 4 | 5 | 3 | 0.367 | 0.372 | 0.395 | 7.5 % | 1 851 MiB | N/A |
| turbo | 8 | 5 | 3 | 0.316 | 0.318 | 0.322 | 1.8 % | 1 639 MiB | N/A |
| turbo | **12** | 5 | 5 | 0.275 | **0.284** | 0.297 | 7.9 % | 1 910 MiB | N/A |
| turbo | 14 | 5 | 2 | 0.287 | 0.291 | 0.296 | 3.0 % | 1 685 MiB | N/A |
| turbo | 16 | 5 | 2 | 0.300 | 0.302 | 0.305 | 1.6 % | 1 631 MiB | N/A |

### Why these thread counts

RTF improves monotonically to **12 threads for both models** and reverses beyond it: 14 and
16 were measured and are consistently worse. Twelve is this machine's physical core count,
and pushing past it costs more in scheduling than it returns.

> **Correction.** An earlier single sweep put the small model's optimum at 8 threads with 12
> *worse* than 8, and this ADR said so. That did not reproduce. Repeating the sweep five
> times showed the reversal was run-to-run variance, and the same-configuration spread
> (0.3 %–10 %) is of the same order as the difference that finding rested on. One sweep was
> not enough evidence for a per-model thread policy, and the earlier conclusion is
> withdrawn rather than quietly amended.

Two of those earlier sweeps also ran **concurrently**, which contaminated their timings and
exposed a real defect: the benchmark wrote its synthetic audio to a fixed filename, so
whichever run finished first deleted the audio the other was still decoding. Four runs
failed with `the working copy to transcribe does not exist`. The filename is now
per-process, and the contaminated sweeps were discarded rather than averaged in.

### Why different beam sizes

Beam size dominates throughput, by more than any thread effect. On identical audio with the
same model: `beam_size=5` gave RTF **0.711**, `beam_size=1` with `temperature=0` gave
**0.285** — a 2.5× swing. Pass-1 runs over every region and needs the throughput; pass-2
runs over a small budgeted subset where quality is the entire point, so it pays for the
wider beam.

**This split is provisional.** It trades accuracy for speed on pass-1, and no accuracy
measurement exists to say whether that trade is acceptable. It must be revisited the first
time a reference transcript is available. It is recorded here as a decision made on
throughput evidence alone, not as a validated configuration.

### Why not OpenVINO or the GPU: **NOT BENCHMARKED — NOT SELECTED**

Probed, and the honest conclusion is narrower than "impossible":

| Probe | Result |
|---|---|
| `OpenCL.dll` | loads from `C:\WINDOWS\system32` |
| `HKLM\SOFTWARE\Khronos\OpenCL\Vendors` | absent |
| `igdrcl64.dll`, `ze_intel_gpu64.dll`, `igc64.dll` on PATH | absent |
| `ctranslate2.get_cuda_device_count()` | 0 |
| `ctranslate2.get_supported_compute_types("cuda")` | `RuntimeError` — no CUDA build |
| `onnxruntime.get_available_providers()` | `['AzureExecutionProvider', 'CPUExecutionProvider']` |

OpenVINO is **not installed**, so `openvino.Core().available_devices` has never been run
and **this ADR does not claim the Iris Xe cannot be reached** — a modern DCH driver may
expose it perfectly well. What is true is that nothing has measured it, installing the
toolkit is outside Phase 4's scope, and CPU INT8 already meets the resource and throughput
budgets with margin. If OpenVINO is evaluated later, the evidence required is
`available_devices` plus a like-for-like RTF and peak-RSS comparison — not the absence of
a registry key.

`AzureExecutionProvider` appearing in the capability list is **not** evidence of a network
call, and this ADR does not treat it as such. What matters is which provider the *session*
uses: `mom_igd.asr.vad` reads the live session's provider list, requires
`CPUExecutionProvider`, and refuses to run on anything else. Measured on this machine the
session reports exactly `['CPUExecutionProvider']`.

## Gate assessment

| Target | Verdict |
|---|---|
| Peak worker RSS `< 2.5 GB` | **PASS** — worst single worker 1 910 MiB (1.87 GiB) over 30 runs |
| Phase 4 total RTF `<= 1.0` | **PASS by projection** — pass-1 0.142 plus a 25 % pass-2 budget at 0.284 ≈ **0.213**. This is arithmetic over two measured medians, not one measured end-to-end run, and is labelled as such |
| Zero network egress | **PASS**, measured: every outbound primitive intercepted during each benchmark decode, `network_attempts == []` on all 30 runs. The recorder itself was defective in the first attempt (see below) and was fixed before these runs |
| No OOM, crash or silent fallback | **PASS** — 30 runs, no errors |
| Clean Indonesian WER `<= 25 %` | **N/A — PENDING** |
| Far-field WER `<= 35 %` | **N/A — PENDING** |
| Word-timestamp error | **N/A — PENDING** |
| Pass-2 improves the flagged subset | **N/A — PENDING** |

### What this decision does not license

No evaluation corpus with reference transcripts exists on this machine, so timing was
measured on deterministic synthetic audio. The decoder does real work on it — segments and
words are produced, and configuration changes throughput measurably — so the numbers are a
valid measurement of **engine throughput on this machine**. They are not a substitute for
real speech: segment density differs and the temperature-fallback path triggers
differently.

And they say nothing at all about accuracy. WER is `N/A` because there is no reference, and
it is **never** derived from the model's own output. **Accuracy acceptance is PENDING**, and
this ADR must not be cited as evidence that Indonesian transcription is accurate enough for
production.

### The egress recorder was itself broken

The first benchmark reported `network_attempts == []` for a reason that had nothing to do
with the network: the transcription task built its result dictionary **inside** the `try`
block, and the `finally` that drains the interception recorder ran afterwards. The list was
frozen empty before anything could be added to it, so the field could not have reported an
attempt however hard the decoder tried to make one.

The interception was real — every primitive was patched for the duration of each decode, so
an outbound call would have raised — but the *recorded* evidence was vacuous. An evidence
field that cannot fail is worse than no field, because it reads as a measurement. The
result is now built after the `finally`, a unit test makes a real `getaddrinfo` call from
inside a decode and asserts it appears in the payload, and the 30 runs above were measured
with the fixed recorder.

## Consequences

* Two models are provisioned, and **they must never be resident together**: the worst
  observed pass-1 worker (693 MiB) plus the worst pass-2 worker (1 910 MiB) is 2 603 MiB =
  **2.54 GiB, which exceeds the 2.5 GB budget**. Co-residency is therefore not merely
  undesirable, it is measured to breach the budget. That is direct evidence for ADR-0004's
  one-heavy-worker policy and for unloading pass-1 before loading pass-2.
* Load time is not negligible for the large model (3–12 s, varying with page cache), which
  argues against reloading it per region.
* `faster-whisper`, `ctranslate2`, `av`, `numpy`, `onnxruntime` and `tokenizers` graduate
  out of `DEFERRED_HEAVY_DISTRIBUTIONS`. `torch`, `pyannote.audio` and the LLM stack stay
  deferred to their own phases.
* The Silero VAD model needs no provisioning: it ships inside the faster-whisper wheel as
  `assets/silero_vad_v6.onnx`, so it is local by construction. Its hash is recorded with
  every VAD result, so a wheel upgrade that changes the VAD is visible in the provenance.

## Alternatives rejected

**`whisper.cpp` with OpenVINO** — ADR-0005 already established that it requires building
from source with MSVC, CMake and the OpenVINO toolkit, none of which is present.
Installing a C++ toolchain before knowing whether it is faster is the wrong order.

**`large-v3` (non-turbo) for pass-2** — 2 948 MiB of weights against turbo's 1 547 MiB.
At INT8 that would very likely breach the 2.5 GB budget, and turbo already meets the RTF
target. Available in the same format if the accuracy work later shows turbo is not good
enough.

**`medium` for pass-1** — the repository ships no `preprocessor_config.json`, and it is
3× the size of `small` for a throughput budget `small` already meets. Revisit alongside
the first real accuracy measurement, not before.

**One model for both passes** — defeats the point. Pass-1 needs throughput over the whole
meeting; pass-2 needs quality over a small subset. Using the large model for both puts RTF
at 0.33 for the whole meeting before pass-2 exists at all.
