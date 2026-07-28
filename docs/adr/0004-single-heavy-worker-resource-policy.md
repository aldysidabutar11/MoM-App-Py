# ADR-0004 — At most one heavy model, in its own short-lived process

* **Status:** Accepted
* **Phase:** 0 (audited) / 1 (foundation implemented)

## Context

Measured on the production device in Phase 0:

* 16 GB RAM installed, 15.73 GB visible, **≈4.1 GB actually free** during a normal
  desktop session (Chrome, Docker Desktop, editor all running).
* Docker Desktop's WSL2 VM reserves up to **≈7.6 GiB**.
* CPU is a hybrid i7-1260P: 4 P-cores + 8 E-cores, 16 logical, no AVX-512, 28 W
  nominal TDP in a thin chassis.
* Iris Xe has no dedicated VRAM: GPU memory is taken from the same 16 GB.

Estimated peak resident memory per pipeline stage:

| Stage | Peak RSS (estimate) |
|---|---|
| Recorder | 150–300 MB |
| ASR small INT8 | 0.8–1.2 GB |
| ASR large-v3-turbo INT8 | 1.8–2.5 GB |
| Diarization (torch) | 2.0–3.0 GB |
| Voice-ID (ONNX) | 0.4–0.8 GB |
| LLM 4B Q4 + KV cache | 3.0–4.0 GB |

Any two of the larger stages together exceed the available headroom and push the
machine into paging — which on this workload means an already-slow batch job
becoming unusable.

## Decision

**Exactly one heavy model may be resident at any moment, and it lives in its own
short-lived worker process that is terminated when its stage completes.**

1. `resources.max_heavy_workers` is **hard-capped at 1**. A value above 1 is
   rejected by configuration validation with an explanation, not clamped silently.
2. **One stage = one process = one model.** The orchestrator spawns
   `mom-worker --job J --stage S`, streams progress events, waits for exit, then
   verifies the stage output.
3. **Process termination is the memory-release mechanism.** Releasing a model
   in-process is unreliable in Python — allocators commonly retain their arenas, so
   RSS does not return to the OS. Ending the process is the only mechanism that
   reliably does. An in-process `ModelSlot` guard (exclusive lock, RSS logged
   before and after) exists to *detect* leaks, not as the primary mechanism.
4. **A preflight check runs before every heavy stage**: free RAM against
   `min_free_ram_mb`, free disk against `min_free_disk_gb`. Below threshold the
   stage does not start and the operator is told what to close — concretely.
5. **The recorder never loads a heavy model**, and **the recorder and a worker
   never run concurrently** (enforced by the state machine, not by convention).
   Optional VAD during recording is limited to a tiny ONNX model (~2 MB) and must
   be switchable off. Phase 2 implements this rule and ships **no** VAD at all;
   the capture side of the separation — no resampling, no ASR, no model in the
   audio callback — is tabulated in
   [ADR-0006](0006-capture-format-pcm16-device-native.md), "Relationship to the
   capture / AI separation".
6. **Stage-level checkpoints and resume metadata** are persisted, so an interrupted
   run continues instead of restarting. A failed stage is the resume point, not the
   stage after it.
7. **Thread counts are tuned, not maximised.** On a hybrid CPU, scheduling
   inference threads onto E-cores creates stragglers: the batch finishes only when
   the slowest thread does. Starting point is 6–8 threads, with a 4/6/8/10/12 sweep
   in Phase 4A. Never a blind `num_threads = 16`.
8. **The application never stops a user's processes.** It reports Docker/WSL memory
   use as information and leaves the decision to the operator.

## Consequences

**Good.** The memory budget holds with a real margin on a 16 GB machine. A crashed
or hung stage cannot take the application down. Resume-after-interruption is a
natural consequence of the design rather than a bolted-on feature. Peak RSS per
stage is measurable, because each stage is its own process.

**Bad / accepted.** No pipeline parallelism: total wall-clock is the sum of stages,
estimated at 1.5–3× meeting duration for a 2-hour meeting. That is an accepted
product boundary — the usage model is *record today, review tomorrow morning* —
and it must be communicated up front rather than discovered by a user. Process
startup costs a few hundred milliseconds per stage, which is irrelevant against
stages measured in minutes. Passing data between stages goes through SQLite and the
filesystem rather than shared memory; for this data volume that is a good trade.

**Load-bearing in Phase 1.** The `job_stages.is_heavy` flag, the checkpoint and
resume-metadata columns, the `max_heavy_workers` validator, and the
`min_free_ram_mb` / `min_free_disk_gb` thresholds all exist now so that the
orchestrator has nothing to invent when the first real stage arrives in Phase 4.
