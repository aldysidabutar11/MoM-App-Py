# Phase 3 — speaker embedding model: selection status

> **No model has been selected, approved, downloaded or verified.**
> `models/registry.json` declares zero models. Real voice enrollment is therefore
> **not possible** in this build, and the application says so rather than pretending
> otherwise: enrollment refuses with `MODEL_UNAVAILABLE` before the microphone is
> opened.

This document records what is actually known, what is not, and what has to happen
before a model can be used. It deliberately contains **no invented figures**.

---

## 1. Current state, stated exactly

| | |
|---|---|
| Models declared in `models/registry.json` | **0** |
| Model artefacts present under `<data_root>/models` | **0** |
| Candidate evaluated on the target device | **none** |
| Benchmarks run | **none** |
| Artefact SHA-256 values known | **none** |
| Licences reviewed | **none** |
| Real voiceprint ever produced by this build | **no** |

Everything Phase 3 verified was verified with a **deterministic fake provider** whose
model name is prefixed `FAKE-` and whose declared hash is 64 `f` characters. That
provider is reachable only by constructor injection inside the test suite, and three
independent barriers keep it out of production
([ADR-0012](adr/0012-enrollment-capture-in-python-no-raw-audio-retention.md),
`mom_igd/enrollment/fake_provider.py`).

**Fake-provider evidence is test evidence. It is not evidence that the product can
identify anybody.**

---

## Roster size is not an accuracy lever

A meeting's roster capacity is a configuration limit on how many people may be listed,
and the safety ceiling (default 50) is a guard rail against a typo. Neither is a claim
about recognition:

* **No head count has been validated in a real room** -- not fifty, not nine.
* More participants make the problem *harder*, not easier: more overlapping speech,
  greater speaker-to-microphone distance, more reverberation, and more opportunities
  for a false match between two similar voices.
* Any acceptance criterion for a model must state the number of speakers it was
  measured at, in the real room, on the production microphone. Until that measurement
  exists there is no supported speaker count at all.
* The built-in laptop array remains **development only**; any voiceprint captured on it
  is stored `DEVELOPMENT_ONLY` and is never production eligible.

Nothing in the application describes speaker recognition as unlimited, and nothing
claims accuracy at any roster size.


## 2. Requirements a candidate must satisfy

These come from the hardware and the architecture, not from any particular model.

| Requirement | Source |
|---|---|
| Runs on **CPU only** — no CUDA | Target GPU is Intel Iris Xe; `torch.cuda.is_available()` is `False` |
| Windows 11 x64, Python 3.12 | Phase 0 environment audit |
| **ONNX preferred** over PyTorch | Avoids pulling a multi-gigabyte framework onto a 16 GB machine |
| Loaded in a short-lived worker, released afterwards | [ADR-0004](adr/0004-single-heavy-worker-resource-policy.md) |
| Peak RSS during enrollment ≤ 1.5 GB | Phase 3 brief; roughly 4 GB is free on the target device |
| Fixed, documented embedding dimension | The voiceprint envelope records it and Phase 6 checks it |
| Deterministic for identical input | Otherwise intra-enrollment consistency measures noise |
| Artefact SHA-256 verifiable before load | A model is executable input; `verify_artifact_sha256` refuses a mismatch |
| No network access at runtime | [ADR-0002](adr/0002-offline-runtime-definition.md) |
| Licence permits internal commercial use | Must be read, not assumed |

---

## 3. Candidate directions — **unverified**

The families below are the plausible places to look. **Nothing here is a
recommendation, and every field is marked unknown because it has not been checked.**
Filling this table in requires visiting the official source, reading the licence and
downloading the artefact — none of which has been done.

### Candidate A — ECAPA-TDNN family (e.g. SpeechBrain-lineage exports)

| Field | Status |
|---|---|
| Exact artefact / filename | **unknown** |
| Official source URL | **unknown — must be an official first-party source** |
| Licence | **unknown** |
| File size | **unknown** |
| SHA-256 | **unknown (artefact not obtained)** |
| Expected sample rate | **unknown** (commonly 16 kHz in this family; unverified) |
| Preprocessing | **unknown** |
| Embedding dimension | **unknown** (often 192 in this family; unverified) |
| Runtime dependencies | **unknown** — an ONNX export would need `onnxruntime`, which is **not installed** |
| Measured RSS / latency | **not measured** |
| Integration risk | Unassessed. A PyTorch-only distribution would breach the ONNX preference. |

### Candidate B — WeSpeaker / ResNet-style speaker embedding exports

| Field | Status |
|---|---|
| Exact artefact / filename | **unknown** |
| Official source URL | **unknown** |
| Licence | **unknown** |
| File size | **unknown** |
| SHA-256 | **unknown (artefact not obtained)** |
| Expected sample rate | **unknown** |
| Preprocessing | **unknown** |
| Embedding dimension | **unknown** |
| Runtime dependencies | **unknown** |
| Measured RSS / latency | **not measured** |
| Integration risk | Unassessed. |

**No recommendation is made.** Recommending one of these now would mean asserting
things about licence, size, preprocessing and accuracy that have not been checked,
and a fabricated recommendation is worse than an open question because it stops
anyone from checking.

---

## 4. Why Phase 3 was built without a model

The model is one dependency of one step. Everything else — the schema, participant
lifecycle, append-only consent, AES-256-GCM encryption, DPAPI key protection,
crash-consistent storage, revocation and deletion, the state machine, the capture
path, the API, the UI, diagnostics and the CLI — is independent of *which* model is
chosen, and all of it is testable against a provider contract.

Building it first means the model decision can be made on evidence later, without a
deadline pushing it, and it means the provider boundary was designed against a real
consumer rather than guessed at.

What the boundary demands of any model is already fixed and enforced: identity,
version, artefact hash, sample rate, channel count, preprocessing identity, embedding
dimension, `embed()`, explicit release — plus consumer-side validation of every
vector returned (correct dimension, finite, non-zero, unit length). See
`mom_igd/enrollment/provider.py`.

---

## 5. What has to happen next, in order

1. **Choose candidates and read their licences** from official first-party sources.
2. **Fill in this document with verified facts** — artefact name, source, licence,
   size, SHA-256, sample rate, preprocessing, embedding dimension, dependencies.
3. **Ask the operator for explicit approval** of the exact artefact *and* the exact
   dependency it requires. `onnxruntime` is currently on the deferred-dependency
   denylist in `mom_igd/offline_policy.py`; approving a model means approving that
   change too.
4. **Provision deliberately**: download once, as a separate setup step, verify the
   SHA-256, place it under `<data_root>/models`, and declare it in
   `models/registry.json`. **The runtime has no download path and must not gain one.**
5. **Implement the provider adapter**, verifying the artefact hash before load and
   releasing the model when enrollment finishes.
6. **Measure on the target device** — load time, embedding time per sample, total
   enrollment time, peak RSS, RSS after the worker exits, CPU. Record the real
   numbers here, including any that miss the targets.
7. **Then** calibrate the provisional quality thresholds in
   `mom_igd/enrollment/quality.py` against a verified USB conference microphone. Only
   the intra-speaker cosine floor of **0.80** has a documented origin today; every
   other level threshold is an engineering default that catches obvious faults and is
   labelled provisional.

Until steps 1–5 are complete, `doctor --production` fails on
`speaker_embedding_model`, and it should.

---

## 6. Rules that do not change with the model

* Runtime never downloads a model.
* A model artefact is never committed to Git; it lives under `<data_root>/models`.
* SHA-256 is verified **before** load; a mismatch refuses rather than warns.
* The model identity (name, version, hash) is bound into every voiceprint's AAD, so a
  template cannot be reinterpreted under a different model
  ([ADR-0010](adr/0010-voiceprint-encryption-aes-gcm-under-dpapi.md)).
* The model is released when enrollment finishes; nothing stays resident
  ([ADR-0004](adr/0004-single-heavy-worker-resource-policy.md)).
* A provider whose `is_test_double` is true is refused in production, and no config
  key, environment variable, query parameter or request field can select one.
