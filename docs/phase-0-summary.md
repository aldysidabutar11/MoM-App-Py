# Phase 0 summary — audit, feasibility, architecture

Condensed, evidence-based record of the Phase 0 audit. Every entry marked
**[verified]** came from a read-only command actually executed on the production
device. Items marked **[estimate]** are informed estimates that Phase 4A must
replace with measurements.

Phase 0 verdict: **CONDITIONAL** — feasible, with named prerequisites and
decisions to settle before implementation.

---

## 1. Device

| | Value | |
|---|---|---|
| OS | Windows 11 Home 10.0.26200 build 26200, 64-bit | [verified] |
| CPU | Intel Core i7-1260P — 12 cores / 16 logical, 2.1 GHz base, 18 MB L3 | [verified] |
| RAM | 8 × 2 GB LPDDR5-6400 = 16 GB (15.73 GB visible) | [verified] |
| **RAM free during a normal session** | **~4.1 GB** | [verified] |
| GPU | Intel Iris Xe, driver 32.0.101.7088 | [verified] |
| Storage | Samsung MZVL21T0HCLR NVMe SSD, 954 GB | [verified] |
| Volumes | C: NTFS 111.8 GB free · D: NTFS 197.3 GB free, both on disk 0 | [verified] |
| Long paths | `LongPathsEnabled = 1` | [verified] |
| Power plan | High performance | [verified] |
| Microphone | Intel Smart Sound **digital microphone array** (internal) | [verified] |
| Audio services | `Audiosrv`, `AudioEndpointBuilder` both running | [verified] |
| BitLocker | **Not verified** — `Get-BitLockerVolume` returned `Access denied` | — |

### Three findings that changed the design

**1. The "≈2 GB GPU memory" figure is a reporting artefact, not a limit.**
WMI `AdapterRAM` is a 32-bit field and reported `2147479552`; the registry
`HardwareInformation.qwMemorySize` was empty. Iris Xe allocates system RAM
dynamically. The real constraint is therefore the **shared 16 GB total**, and every
byte the GPU takes is a byte the CPU loses. This strengthens the memory argument
rather than relaxing it.

**2. No NPU.** The i7-1260P is 12th-gen Alder Lake; Intel AI Boost arrived with
Meteor Lake. Nothing may be designed against an `NPU` device target. (High
confidence; `openvino.Core().available_devices` will confirm once OpenVINO is
installed in Phase 4A.)

**3. No CUDA, ever.** `torch.cuda.is_available() = False`,
`torch.version.cuda = None` [verified]. Any dependency, model artefact or example
that assumes an NVIDIA GPU must be rejected at review.

---

## 2. Toolchain at audit time

**Present** [verified]: Git 2.53.0 · Node 24.14.1 / npm 11.11.0 ·
.NET runtimes 8/9/10 (**no SDK**) · Docker 29.6.2 + Compose v5.3.1 ·
WSL2 with Ubuntu-22.04 · Ollama 0.32.5 (qwen3:8b, llama3.1:8b, nomic-embed-text,
9.7 GB) · Python 3.14.2 (official) · Python 3.11.9 (**Microsoft Store shim**) ·
Anaconda3 (`conda` not on PATH) · ONNX Runtime 1.27.0 (**CPU + Azure providers
only**) · torch 2.5.1+**cpu** · `OpenCL.dll`, `ze_loader.dll`, `DirectML.dll`

**Absent** [verified]: **Python 3.12** · MSVC / `cl.exe` · gcc / clang ·
Visual Studio or Build Tools (`vswhere` absent) · Windows SDK · **CMake** ·
Ninja · make · Rust/Cargo · **FFmpeg** · SQLite CLI · OpenVINO toolkit ·
every audio Python library (`sounddevice`, `soundfile`, `librosa`, `av`, `soxr`,
`webrtcvad`, `silero_vad`, `pyaudio`) · any Whisper / diarization / speaker model

Intel GPU compute libraries (`igdrcl64.dll`, `ze_intel_gpu64.dll`, `igc64.dll`)
**are** present in the DriverStore, but the Khronos OpenCL/Level-Zero registry
keys are **absent** [verified]. The OpenVINO-GPU path is therefore *plausible but
unproven* and must be demonstrated empirically in Phase 4A, not assumed.

### Consequences

* **The `whisper.cpp + OpenVINO` plan was rejected as the foundation.** Building
  it from source needs MSVC + CMake + the OpenVINO toolkit — none installed — and
  the official prebuilt releases ship no OpenVINO variant. The primary ASR
  candidate became `faster-whisper` (CTranslate2 INT8: prebuilt wheels, no
  compiler, and per-segment confidence signals that make selective
  re-transcription measurable). whisper.cpp/OpenVINO remains a Phase 4A benchmark
  candidate.
* **Docker was rejected for the production runtime, quantitatively.**
  `docker info` reported `MemTotal = 8,182,722,560` bytes (≈7.6 GiB) for the WSL2
  VM on a 16 GB machine [verified]. Containers also cannot reach WASAPI. Docker
  Desktop is the single largest RAM competitor to the AI pipeline.
* **FFmpeg turned out not to be a blocker.** `libsndfile` (via `soundfile`) writes
  16-bit FLAC natively, and `soxr`/`scipy` can resample 48 → 16 kHz. FFmpeg stays
  a convenience, never a critical-path dependency.
* **The SQLite CLI is unnecessary** — Python's `sqlite3` module (SQLite 3.45+) is
  sufficient.

---

## 3. Legacy project assessment

`D:\Aldy\Project APP VTT` was inspected read-only (its `.env` was **not** opened).
Findings: FastAPI + uvicorn + pywebview + PyInstaller; ASR via **`google-genai`**
— a cloud API; not a Git repository; `models/`, `recordings/` and `exports/` all
empty; `venv/` contained only `fastapi` and `uvicorn`; `voicescribe.db` 20 KB.

* **Rejected for reuse:** the entire ASR path. Cloud-based, fundamentally at odds
  with the offline requirement.
* **Adoptable as a pattern only:** the `FastAPI loopback + pywebview + SQLite +
  static web UI` desktop shape, and PyInstaller packaging.
* **Nothing inherited:** no model, no audio, no voiceprint existed.

The project is **not a code source** and must remain untouched.

---

## 4. Feasibility

**Feasible**, with one boundary that has to be communicated rather than
discovered: the MoM is **not** available immediately after a meeting.

### Processing budget for a 2-hour meeting — [estimate]

| Stage | xRT | Duration | Confidence |
|---|---|---|---|
| Validate + normalise + resample | ~20× | 5–8 min | high |
| VAD | ~50× | 2–4 min | high |
| ASR pass 1 (small INT8, VAD-gated) | 2–4× | 30–60 min | medium |
| ASR pass 2 (≤25 % of audio) | 0.8–1.5× | 20–40 min | low |
| **Diarization (CPU)** | **0.5–1.5×** | **80–240 min** | **low — dominant** |
| Voice-ID | selected segments | 3–8 min | medium |
| Reconciliation | deterministic | < 1 min | high |
| MoM (4B Q4, hierarchical) | — | 25–45 min | low |
| Evidence verification | deterministic | < 2 min | high |
| **Total** | | **≈2.5–6.5 h** | |

Realistic usage model: **record today, review tomorrow morning.**

### Peak memory per stage — [estimate]

Recorder 150–300 MB · ASR small INT8 0.8–1.2 GB · ASR large-turbo INT8
1.8–2.5 GB · diarization (torch) 2.0–3.0 GB · Voice-ID ONNX 0.4–0.8 GB ·
LLM 4B Q4 3.0–4.0 GB.

Against ~4.1 GB observed free, **process isolation per stage is mandatory, not an
optimisation**, and Docker Desktop must be closed during processing.

### Storage — [verified arithmetic]

48 kHz/16-bit mono WAV = 345.6 MB/h; FLAC ≈ 190 MB/h; 16 kHz working copy
115 MB/h. A 2-hour meeting ≈ **1.3 GB** including intermediates. With 197.3 GB
free on D:, that is roughly **150 meetings** before retention matters.

---

## 5. Highest-impact finding: the microphone

The active input is an **Intel Smart Sound digital microphone array** [verified].
Laptop arrays apply adaptive beamforming, acoustic echo cancellation and noise
suppression tuned for one person facing the screen. With nine people spread around
a room this will:

* suppress speakers outside the beam, so their segments vanish from the recording;
* change spectral characteristics dynamically, which **destroys voiceprint
  consistency** and therefore Voice-ID;
* suppress overlap — removing exactly the signal that must be detected.

| Priority | Option | Effect |
|---|---|---|
| **Best** | USB omnidirectional conference microphone at the centre of the table | 360° pickup, uniform gain, shorter distance to every participant |
| Good | Generic USB omnidirectional microphone, centre of table | Large improvement over the array |
| Minimum | Internal array with **all** Windows audio enhancements disabled, laptop centred | Still far from ideal |
| **Unacceptable** | Internal array with enhancements on, laptop at one end | Diarization will fail |

`device_probe.py` (Phase 2) must refuse to start a production recording on the
internal array with enhancements active, and say how to fix it.

---

## 6. Decisions taken (now recorded as ADRs)

| # | Decision | ADR |
|---|---|---|
| 1 | Production runtime is native Windows; Docker/WSL2 are not dependencies | [0001](adr/0001-native-windows-runtime.md) |
| 2 | Offline at runtime; one-time controlled provisioning allowed; no socket monkey-patch; firewall → Phase 11 | [0002](adr/0002-offline-runtime-definition.md) |
| 3 | SQLite + WAL; runtime data root outside the repository, default `D:\MoM-IGD-Data` | [0003](adr/0003-sqlite-and-external-runtime-data.md) |
| 4 | At most one heavy model, in its own short-lived worker process | [0004](adr/0004-single-heavy-worker-resource-policy.md) |
| 5 | No AI provider or model selected; deferred to the Phase 4A benchmark | [0005](adr/0005-ai-provider-selection-deferred-to-phase-4a.md) |
| 6 | Official Python 3.12 (per-user), not 3.14 and not the Store shim | 0001 / README |
| 7 | Internal microphone is development-only; USB conference mic required before Phase 2 acceptance | 0001 / README |

Additional accepted decisions: do not create or modify `.wslconfig`; never stop
Docker, WSL, a browser or any other user process automatically.

---

## 7. Assumptions Phase 4A must measure

1. xRT of ASR pass 1 (small INT8)
2. xRT of ASR pass 2 (large-v3-turbo INT8)
3. **xRT of diarization — widest uncertainty, largest impact**
4. LLM 4B Q4 prompt-processing and generation throughput
5. Peak RSS per stage
6. Whether OpenVINO GPU is genuinely available and faster on Iris Xe
   (the missing Khronos registry keys make this an open question)
7. Thermal throttling behaviour over a 2–4 hour CPU-saturated run
8. Indonesian-language quality of a 4B model versus the existing 8B
9. DER with nine speakers on the production microphone in the real room
10. Voice-ID EER and the resulting thresholds
11. CPU cost of FLAC encoding during capture
12. Timestamp drift over a 2-hour recording
13. Whether a `num_speakers` hint actually improves DER here

---

## 8. Risk register (carried forward)

| Risk | Impact | Mitigation |
|---|---|---|
| Internal microphone destroys diarization | Critical | USB conference mic; automatic device validation; measure DER early |
| Diarization too slow on CPU (possibly > 3 h) | High | Measure in 4A **before** writing production code; keep a lighter ONNX candidate |
| RAM exhaustion (~4.1 GB free measured) | High | Process isolation per stage; preflight check; close Docker and browsers |
| DER poor with 9 speakers on one microphone | High | `num_speakers` hint from the participant list; strict `UNKNOWN` policy; reviewer correction |
| Thermal throttling over long runs | Medium | Chunked processing with pauses; monitor clocks |
| LLM hallucinating PIC or deadline | High | Deterministic verifier; grammar-constrained JSON; human approval gate |
| pyannote needs HF access + licence acceptance | Medium | Settle during provisioning; keep a gate-free ONNX alternative |
| Store-shim Python breaks packaging | Medium | **Resolved in Phase 1** — official 3.12 installed per-user |
| Biometric-data compliance (UU PDP No. 27/2022) | High (legal) | Consent flow, DPIA, retention, right to erasure — from Phase 3 |
| `core.autocrlf=true` corrupting binary fixtures | Low, if ignored: high | **Resolved in Phase 1** — `.gitattributes` committed |
| Users expecting an instant MoM | Medium | Communicate "record today, review tomorrow" up front |

---

## 9. What Phase 0 changed, and did not change

**Changed:** nothing. Phase 0 created, modified and deleted zero files, installed
zero dependencies, downloaded zero models, and made no commit. The target
directory was empty and was not a Git repository.

**Established:** the environment matrix, the hardware implications above, the
rejection of the `whisper.cpp + OpenVINO` foundation and of Docker as a
production runtime, the microphone requirement, and the Phase 4A benchmark gate.
