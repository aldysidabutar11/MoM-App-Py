# ADR-0005 — AI provider and model selection is deferred to the Phase 4A benchmark

* **Status:** Accepted
* **Phase:** 0 (decided) / 1 (encoded as an empty registry and provider abstractions)

## Context

Phase 0 proposed a technology direction: `whisper.cpp` with OpenVINO for ASR,
pyannote Community-1 for diarization, an ECAPA-style local embedding model for
speaker identification, and a text-only 4B instruct model in Q4 for MoM
generation.

The audit then established facts that make committing to any of these now
unjustifiable:

**The proposed ASR foundation cannot be built on this machine.** `whisper.cpp` with
OpenVINO requires building from source: MSVC, CMake and the OpenVINO toolkit are
**all absent** (verified), and the official prebuilt `whisper.cpp` releases ship no
OpenVINO variant. Adopting it as the foundation would have meant installing a full
C++ toolchain before knowing whether it is even faster here.

**The GPU acceleration path is unproven.** Intel's compute libraries
(`igdrcl64.dll`, `ze_intel_gpu64.dll`, `igc64.dll`) are present in the DriverStore,
but the Khronos OpenCL and Level-Zero registry keys are **absent** (verified).
Whether OpenVINO can actually target this Iris Xe, and whether it would be faster
than CPU INT8, is an open empirical question.

**The numbers that decide the architecture have not been measured.** Estimated
diarization throughput spans 0.5–1.5× realtime — for a 2-hour meeting that is
somewhere between 80 and 240 minutes. A factor-of-three uncertainty in the single
largest cost is not a basis for choosing a provider; the difference is a product
decision about how long users wait, not a technical detail.

**No model exists on the device.** No Whisper, diarization or speaker-embedding
model was found (verified). Nothing has been downloaded.

## Decision

**No ASR, diarization, speaker-embedding or LLM provider is selected in Phase 1.
The choice is made in Phase 4A, on the real device, against measured numbers.**

### 1. Phase 4A is a gate, not a task

It is inserted before production ASR code and produces `docs/benchmarks.md`
containing, for every candidate: xRT, peak RSS, accuracy (WER / DER / JER / EER),
and thermal behaviour over a long run. A provider is chosen with a written
justification referencing those numbers.

Candidates to compare:

| Slot | Candidates |
|---|---|
| ASR | `faster-whisper` (CTranslate2 INT8) — current front-runner, no compiler needed, exposes per-segment confidence · OpenVINO GenAI / optimum-intel on CPU and GPU · `whisper.cpp` + OpenVINO, only if the toolchain cost is justified |
| Diarization | pyannote community-1 (torch) · `sherpa-onnx` segmentation + embedding (no torch, much smaller RSS) |
| Speaker embedding | WeSpeaker / 3D-Speaker ONNX (preferred: onnxruntime only) · ECAPA-TDNN via SpeechBrain (torch) |
| LLM | Qwen3-4B-Instruct Q4_K_M · Gemma-3-4B-it Q4, with the already-present 8B used only as a quality baseline |

`faster-whisper` is recorded as the *current primary candidate*, not the decision.
It leads because it needs no compiler and because it returns `avg_logprob`,
`no_speech_prob`, compression ratio and per-word probabilities — the signals that
make "selective high-accuracy re-transcription" measurable rather than a guess.

### 2. Every capability sits behind an interface

```
VADProvider           .detect(audio)                     -> speech_regions
ASRProvider           .transcribe(audio, lang, hints)    -> segments + confidence
DiarizationProvider   .diarize(audio, num_speakers_hint) -> turns + overlap
EmbeddingProvider     .embed(audio_slices)               -> vectors
LLMProvider           .complete(prompt, json_schema)     -> dict
```

This is not speculative generality — it is the direct consequence of four
unresolved decisions. Swapping a provider after the benchmark must not touch
pipeline code.

### 3. The model registry ships empty, and that is correct

`models/registry.json` declares `registry_schema_version: 1` and zero models. An
empty registry is **valid** and produces a doctor **`WARN`, never a `FAIL`** — in
Phase 1 there is nothing for it to declare. It contains **no placeholder or fake
entries**: a fabricated entry would be indistinguishable from a real one at a
glance and would undermine the checksum discipline the registry exists to enforce.

The schema supports provider slot, name, version, path, SHA-256, expected size,
licence metadata, provisioned and offline-ready flags, hardware profile and source
URL (recorded for audit, never fetched at runtime). `offline_ready` requires
`provisioned`. **There is no CUDA hardware profile** — such an artefact could never
run on this device, so allowing one would only invite an unusable download.

### 4. Nothing is downloaded before its phase

Provisioning is a separate, explicit, one-time online step belonging to the phase
that needs the model. Phase 1 contains no download code and no model file.

### 5. One ordering decision *was* taken

Diarization runs **before** selective re-transcription, changing the originally
proposed order. Two of the strongest pass-2 selection signals — speaker-change
boundaries and overlap regions — only exist once diarization has run. Diarization
runs once either way, so the reordering costs nothing and keeping the original
order would deprive pass 2 of its two best signals. This is encoded in
`PIPELINE_STAGES` and asserted by a test.

## Consequences

**Good.** No C++ toolchain, no OpenVINO installation and no multi-gigabyte download
is required to complete Phase 1. The decision will be made against measurements
from the actual device rather than benchmarks published for other hardware. Being
wrong later is cheap, because the provider boundary is a seam.

**Bad / accepted.** No transcription is possible until Phase 4, and Phase 4A must
run before Phase 4 can start — real calendar time spent measuring rather than
building. That is the correct trade when the largest cost in the system is
uncertain by a factor of three.

**Explicit non-decisions.** Which ASR engine. Which diarization engine. Which
embedding model. Which LLM. Whether OpenVINO or DirectML is used at all. All of
these are open, and Phase 1 code must not assume any of them.
