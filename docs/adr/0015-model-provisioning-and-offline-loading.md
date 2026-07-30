# ADR-0015 — Model provisioning, the readiness index, and offline loading

* **Status:** Accepted
* **Phase:** 4

## Context

The runtime is offline (ADR-0002). Model weights are hundreds of megabytes and cannot be
committed (they are not source, and the repository would become unusable). So something has
to fetch them, and that something is the one place in this application permitted to touch
the network. Every design question here is about keeping that hole exactly one command wide
and making everything downstream of it verifiable.

Two incidents during Phase 4A shaped the result, and both are worth recording because each
looked impossible in advance.

**A byte-perfect model that could not decode.** The catalogue listed the four largest files
of `large-v3-turbo` as required. `preprocessor_config.json` is a few hundred bytes and
declares the mel-bin count; `large-v3` needs 128 where the extractor defaults to 80. Every
file hashed correctly, the manifest digest matched, and the first decode failed with
`expected (1, 128, 3000), got (1, 80, 3000)`. **Byte verification is necessary and not
sufficient.**

**"Offline" quietly meaning "from cache".** A test asked `snapshot_download` for the pass-1
repository with `HF_HUB_OFFLINE=1` and it *succeeded* — from this machine's shared Hugging
Face cache, which provisioning had populated. Nothing was downloaded, but nothing was
verified either. An offline guarantee that resolves through a cache is not a guarantee about
what got loaded.

## Decision

### 1. Three layers, never conflated

| Layer | What it answers | Where |
|---|---|---|
| **Approved catalogue** | What this build is *willing* to provision | `provision.MODEL_CATALOGUE`, a closed set in source |
| **Installed registry** | Which model/revision/manifest-digest triples are on disk **and passed a load-and-decode probe** | `<models>/installed.json` |
| **Runtime resolver** | Which model may be loaded right now | `faster_whisper_provider.resolve_model` |

The resolver consults the **registry**, never a directory scan. A directory scan was the
original design and it is exactly what let the mel-bin model look ready.

Provisioning takes a **catalogue key**, not a repository id. An operator cannot point it at
an arbitrary remote repository, which is how an unreviewed or gated artefact would arrive.

### 2. Order of operations, and why

1. **Resolve the revision to a commit sha first.** A branch name is not an identity; pinning
   means a later re-run either reproduces the same bytes or tells you the upstream moved.
2. **Download into `.staging/`**, never onto a promoted model. An interrupted download
   cannot leave a half-replaced model that loads.
3. **Verify in staging** — every expected file present, every size right, every SHA-256
   recomputed from disk.
4. **Strip downloader bookkeeping** (`.cache/huggingface/...`), so a promoted directory
   contains exactly the verified model files plus the manifest. That is what makes the
   "undeclared file present" check meaningful.
5. **Write the manifest**, then compute its digest.
6. **Promote atomically.** `os.replace` on a directory fails on Windows when the destination
   exists, so an existing model is moved aside to `.superseded.<stamp>` first and deleted
   only once the new one is in place — and restored if promotion fails, because a failed
   re-provision must not leave the operator with no model at all.
7. **Re-verify from the promoted path.** Staging and the load path are different
   directories, and only the second one matters at runtime.
8. **Probe that it loads and decodes**, in an isolated worker, on two seconds of generated
   audio.
9. **Only then record it ready.** A probe failure **quarantines** the directory to
   `.quarantine/` with the reason written beside it — kept, not deleted, because an operator
   needs to inspect it and silently discarding 1.5 GB they just downloaded would be hostile.

### 3. A two-link hash chain

The manifest lists every file with its size and SHA-256. The registry records the SHA-256 of
*the manifest*. That gives:

1. the recorded digest proves the manifest is the one that passed the probe;
2. each manifest entry proves a file is the one that was downloaded.

Breaking either link is detected before a model is loaded, and there is deliberately no
repair path — a mismatch means refusal.

This specifically defeats **rewriting the manifest to match tampered weights**: the
directory becomes internally consistent, so byte verification passes on its own terms, and
only the recorded digest catches it. `InstalledIndex.ready()` therefore re-derives the
manifest digest on every call rather than trusting the stored verdict.

### 4. `provisioned_at` means the digest is not reproducible, and that is intentional

The manifest carries the time the artefacts were fetched, so **re-provisioning the same
revision produces a different manifest digest**. The *file list* is deterministic; the
manifest as a whole is not. That is deliberate — re-provisioning is a new event worth
recording — and it means whoever re-provisions must refresh the recorded digest too.
`asr verify` reports the mismatch if they do not. Earlier documentation claimed two runs
produced byte-identical manifests; that was wrong and has been corrected.

### 5. Offline is enforced by assignment, and credentials are removed

`assert_offline_environment()` uses `os.environ[key] = value`, **never `setdefault`**. An
operator shell carrying `HF_HUB_OFFLINE=0` must not be able to put a worker online, and
`setdefault` would silently honour it. Inherited tokens (`HF_TOKEN`,
`HUGGING_FACE_HUB_TOKEN`, `HUGGINGFACEHUB_API_TOKEN`) are **deleted**: the runtime never
authenticates to anything, so the honest state is "no credential present", and leaving one
in the environment of such a process only exposes it to crash dumps and child processes.

The engine is given an **absolute local directory** and `local_files_only=True`, so there is
nothing for the library to resolve remotely and no cache lookup to fall through to. That is
the answer to the second incident: the product does not consult the Hugging Face cache at
all, and a test proves an empty model store fails closed even while the cache holds a copy
of the model.

A spawned worker calls `assert_offline_environment()` **before** importing anything that
could reach the network, because these libraries read the flags at import time.

### 6. Execution providers are verified, not trusted

The installed ONNX Runtime advertises `AzureExecutionProvider`. Its presence in the
capability list is **not** evidence of a network call, and nothing here claims it is. What is
checked is the *session*: `mom_igd.asr.vad` reads the live VAD session's provider list,
requires `CPUExecutionProvider`, and refuses to run on anything else. Measured on this
machine the session reports exactly `['CPUExecutionProvider']`, and the check exists so a
wheel upgrade that changed it would stop the pipeline rather than quietly redirect a model.

### 7. A missing model is `MODEL_UNAVAILABLE`, full stop

Never a download, never a fallback to whichever other model happens to be present. A broken
pass-1 does not become pass-2, and the resolver matches the requested role exactly and
intersects with the approved catalogue — two independent layers, so removing either does not
open the hole.

## Consequences

* Provisioning needs network access; nothing else does. `huggingface_hub` is imported
  *inside* the download function, and a test asserts no other module in `mom_igd` imports a
  network client.
* Model artefacts live under `<data_root>/models`, outside the repository, and are
  git-ignored.
* Provisioning is idempotent: an already-verified, already-probed model is left alone. A
  model that is present and verified but *not* recorded as ready is probed and recorded,
  which is how an interrupted provisioning run heals.
* A corrupt or unparseable `installed.json` means **nothing is ready** — it never falls back
  to a directory scan, which would reintroduce the original defect.
* Registry `relative_path` values are validated under both POSIX and Windows path flavours.
  `Path("/abs/model").is_absolute()` is `False` on Windows, so a POSIX-absolute path written
  into the registry by hand would otherwise have escaped the model store. A test caught that.
* The shared Hugging Face cache ends up holding a duplicate copy of the pass-1 weights
  (~464 MiB) as a side effect of downloading. It is unused by the product and safe to delete;
  it is noted here so the disk cost is not a surprise.

## Alternatives rejected

**One SHA-256 over the weights file.** A CTranslate2 model is a directory, and swapping the
tokenizer changes the output as surely as swapping the weights. One digest would leave the
rest unverified.

**Trusting a directory scan for readiness.** This was the original design, and the mel-bin
incident is what it costs: a manifest-valid, unusable model that looked ready.

**Deleting a model that fails its probe.** Loses the evidence and the download. Quarantine
keeps both while removing it from the load path.

**Letting the runtime resolve a hub id with `HF_HUB_OFFLINE=1`.** Convenient, and it silently
resolves through a cache whose contents nothing has verified. The product addresses an
absolute path with a hash chain instead.
