# Architecture

Fully offline Minutes of Meeting application for in-person meetings, running
natively on Windows 11 on a single laptop.

* **Current phase: 2 — offline audio capture.**
* Decisions already taken: [`adr/`](adr/)
* Evidence behind those decisions: [`phase-0-summary.md`](phase-0-summary.md)
* Capture engine detail: [`phase-2-audio-capture.md`](phase-2-audio-capture.md)

---

## 1. Product shape

One physical room, up to nine registered participants, Indonesian speech with
English technical terms. The application must:

record locally · transcribe · determine who spoke and map anonymous speaker
segments to registered names · detect overlapping speech · produce a structured
MoM with decisions, action items, PIC, deadlines, open questions and next steps ·
attach a source timestamp to every decision and action item · require human
approval · export to PDF, Word, JSON and Markdown · operate with no cloud API and
no internet at runtime.

### The split that makes it feasible

During a meeting the device does **only lightweight work**: microphone capture,
level and clipping monitoring, lossless chunk writing, manifest and checksum
creation, crash recovery, and optionally a very small VAD.

All heavy work runs **after** the meeting, sequentially, one model at a time.
This is the central constraint the whole design serves, and it comes from the
hardware: 16 GB of RAM with roughly 4 GB actually free during a normal desktop
session (measured in Phase 0).

---

## 2. Runtime topology

Native Windows, multi-process. Docker and WSL2 are not production dependencies
([ADR-0001](adr/0001-native-windows-runtime.md)).

```
┌──────────────────────────────────────────────────────────────┐
│ mom-shell     pywebview + WebView2, static HTML/CSS/JS       │
│               UI host, level meter, review, print-to-PDF     │
└───────────────┬──────────────────────────────────────────────┘
                │ loopback HTTP + in-process Python bridge
┌───────────────▼──────────────────────────────────────────────┐
│ mom-api       FastAPI/uvicorn on 127.0.0.1                   │
│               orchestrator; sole writer of job/state tables   │
│               preflight (RAM, disk) before each heavy stage   │
└───┬───────────────────────────────┬──────────────────────────┘
    │ in-process, threads (§3.2)    │ spawn per stage, then kill
┌───▼─────────────────────────┐ ┌───▼──────────────────────────┐
│ capture engine  (Phase 2)   │ │ mom-worker  (Phase 4+)       │
│ • WASAPI shared-mode capture│ │ • exactly ONE model loaded    │
│ • callback → bounded queue  │ │ • killed after each stage so  │
│ • single writer thread      │ │   the OS reclaims the RAM     │
│ • chunk write + fsync       │ │ • checkpoints to SQLite       │
│ • SHA-256 + manifest        │ └───┬──────────────────────────┘
│ • NO heavy model, ever      │     │ spawn/kill for the MoM stage
└─────────────────────────────┘ ┌───▼──────────────────────────┐
                                │ mom-llm  local llama-server   │
                                │ alive only during that stage  │
                                └──────────────────────────────┘
```

**Why process isolation rather than unloading models in-process:** releasing a
model inside a Python process is unreliable — the allocator commonly retains its
arenas. Ending the process is the only mechanism that reliably returns memory to
the OS. It also means a crashed stage cannot take the application down, and
resuming from a checkpoint becomes trivial
([ADR-0004](adr/0004-single-heavy-worker-resource-policy.md)).

### Process boundaries and ownership

| Boundary | Mechanism | Why |
|---|---|---|
| shell → api | loopback HTTP + session token | Same origin as the served UI, so no CORS |
| shell → protected api | in-process Python bridge (`ShellApi.api_get`) | The token never enters JavaScript |
| api → capture engine | direct call into `RecordingService` (same process) | The capture path loads no model, so process isolation buys nothing (§3.2) |
| capture callback → writer | bounded in-memory queue, single consumer thread | The device callback must never do I/O or block |
| capture engine → api | polled snapshot (`status`, `quality`) | One-way, loss-tolerant telemetry; a missed poll costs nothing |
| api → worker | subprocess with `--job-id --stage` | Absolute memory isolation |
| worker → api | JSON events on stdout | Simple, loggable, crash-tolerant |
| worker → DB | direct writes to artefact tables only | Bulk inserts over HTTP would be wasteful |
| api → DB | **sole writer** of `jobs` / `job_stages` state | Prevents state-machine races |

Rules that must not be broken: the capture engine never loads a heavy model; the
worker never touches an audio device; a recording and a worker never run
concurrently (enforced by the state machine); the UI never opens SQLite directly.

---

## 3.1 Phase 1 implementation

### Configuration — `mom_igd/config.py`

Layered, lowest precedence first: `config/default.toml` → `config/local.toml`
(git-ignored) → `MOM_IGD_*` environment variables → explicit CLI/caller
overrides. Validation is a gate, not advice. It rejects:

a non-loopback API host · `max_heavy_workers > 1` · a relative or invalid data
path · the repository as the data path · an unsupported runtime mode · a
non-loopback or cloud provider endpoint · an unknown log level · an unknown
config schema version · `offline = false` · an unknown key.

### Runtime paths — `mom_igd/paths.py`

Every runtime location derives from one validated data root. Default
`D:\MoM-IGD-Data`, overridable through `MOM_IGD_DATA_DIR` or configuration —
never hardcoded as the only valid location. Subdirectories: `db`, `recordings`,
`exports`, `logs`, `models`, `temp`, `backups`.

Rejected: relative paths, a bare filesystem anchor (`D:\`), the repository
itself, anything inside the repository, and any parent that would contain the
repository. Directories are created **only** by `RuntimePaths.ensure()`, called
from an explicit initialisation path — never as an import side effect, and never
by `doctor`.

### Database — `mom_igd/db/`

SQLite, WAL, foreign keys, busy timeout, all **verified after connecting**; a
database that does not confirm them is an error, not a warning
([ADR-0003](adr/0003-sqlite-and-external-runtime-data.md)).

Migrations are `NNNN_name.sql`, versions contiguous from 1. Each runs inside
`BEGIN IMMEDIATE` together with the row that records it, so a failure can never
leave the schema version advanced past a migration that did not fully apply.
Checksums are recorded (line endings normalised first, because
`core.autocrlf=true` is set on the development machine), so editing an applied
migration is detected rather than silently diverging.

`sqlite3.executescript` is deliberately avoided — it issues an implicit `COMMIT`
that would defeat the transaction. Statements are split by
`split_sql_statements`, which understands comments, string literals, quoted
identifiers and `CREATE TRIGGER ... BEGIN ... END` bodies.

There is **no production downgrade path**: a `down` migration here would mean
dropping tables holding recordings metadata, transcripts and approvals. Recovery
is restore-from-backup. The only rollback is the transactional rollback of a
failing migration.

**Phase 1 tables (exactly nine):** `schema_migrations` (created by the runner),
`app_settings`, `participants`, `meetings`, `recordings`, `recording_chunks`,
`jobs`, `job_stages`, `audit_events`.

Phase 2 adds no table. `0002_audio_capture.sql` widens the two existing recording
tables to what a real capture produces (see §3.2) — it does **not** edit
`0001_initial.sql`, which is immutable once applied.

Deferred, with the phase that adds them: `meeting_participants` (3),
`voiceprints` (3), `consents` (3), `asr_words` (4), `diarization_turns` (5),
`speaker_assignments` (6), `utterances` (7), `mom_items` (8), `evidence_links`
(8), `action_tracking` (10).

### Workflow state machine — `mom_igd/jobs/state_machine.py`

A *job* is the workflow instance for exactly one meeting and spans the whole
lifecycle. It is the single owner of workflow state; `meetings` has no state
column, so the two can never disagree.

```
DRAFT ──► READY ──► RECORDING ──► RECORDED ──► QUEUED ──► PROCESSING
  │         │  ▲         │             │           │           │
  │         └──┘         │             │           │           ▼
  │       (re-edit)      │             │           │    REVIEW_REQUIRED
  │                      │             │           │      │        │
  │                      ▼             ▼           │      │        ▼
  │                    FAILED ◄────────┴───────────┴──────┘   APPROVED ■
  │                      │                                (immutable snapshot)
  └──────────────────────┴──────────────► CANCELLED ■
                    FAILED ──► QUEUED (retry)
```

`APPROVED` and `CANCELLED` are terminal. `APPROVED` is terminal because approval
freezes an immutable snapshot — a later change is a new revision, not a mutation.
`FAILED` is **not** terminal: a failed run can be re-queued once the operator has
addressed the cause.

Illegal transitions raise with the allowed set in the message and write nothing.
Every accepted transition writes an `audit_events` row **in the same
transaction** as the state change, so state and audit trail cannot diverge.

`PIPELINE_STAGES` declares the post-meeting pipeline as data — names, whether a
stage is heavy, and the phase that implements it. Nothing executes in Phase 1.

| # | Stage | Heavy | Phase |
|---|---|---|---|
| 1 | `validate_audio` | | 4 |
| 2 | `normalize_audio` | | 4 |
| 3 | `vad` | | 4 |
| 4 | `asr_pass1` | ● | 4 |
| 5 | `diarize` | ● | 5 |
| 6 | `asr_pass2_selective` | ● | 5 |
| 7 | `voice_id` | ● | 6 |
| 8 | `reconcile_transcript` | | 7 |
| 9 | `mom_extract` | ● | 8 |
| 10 | `verify_evidence` | | 8 |

**Diarization runs before selective re-transcription.** Two of the strongest
signals for choosing pass-2 segments — speaker-change boundaries and overlap
regions — only exist once diarization has run. This is a deliberate change from
the ordering originally proposed; keeping the original order would cost pass-2
its two best selection signals at no saving.

### Audit trail — `mom_igd/audit.py`

Append-only and hash-chained: each row stores the hash of its predecessor,
computed over a canonical JSON serialisation. Modifying a row or deleting one
from the middle of history is therefore detectable.

Known limit, asserted in the tests so it cannot later be mistaken for a bug:
truncating the **tail** keeps the remaining chain internally consistent. Detecting
that needs an external anchor (a signed high-water mark), which is Phase 11 work.

This is integrity, not confidentiality. Encryption at rest is Phase 11.

### API — `mom_igd/api/`

`GET /health` and `GET /version` are public; `GET /doctor` and
`GET /internal/ready` require the session token.

**Authentication policy, stated explicitly.** The two public endpoints must
answer before the shell has a token, so it can distinguish "backend down" from
"unauthorised", and they disclose only name, version, phase and coarse booleans —
no filesystem path, no hardware inventory, no user data. Everything else needs
the token because it discloses absolute paths, hardware details and the
running-process inventory.

Loopback is enforced twice: the bind address (validated configuration) and a
`Host`-header allowlist that blocks DNS rebinding. The second layer is what makes
"Swagger is not exposed outside loopback" enforceable rather than incidental.

A credential in a query string is refused with HTTP **400 even when correct** —
accepting one would undermine the rule that the token is never written anywhere,
because query strings reach logs, history and referrers.

### Session token — `mom_igd/security.py`

Generated per process, 256 bits, memory only. `SessionToken.__str__`,
`__repr__` and `__format__` all return `<redacted>`, so an accidental log line or
f-string cannot leak it; the real value is reachable only through the explicit
`.value` attribute. Pickling raises. `RedactingFilter` scrubs the live token —
and anything that looks like `token=` in a URL — from every log handler,
including uvicorn's.

### Desktop shell — `mom_igd/shell/`

pywebview over WebView2, static HTML/CSS/JS, system fonts, a strict
`Content-Security-Policy` limited to `'self'`. No Electron, no React/Vue/Svelte,
no npm, no CDN, no remote font, no external script or stylesheet.

The window loads the UI from the loopback backend, so page and API share an
origin. The page fetches `/health` and `/version` directly and asks Python for
anything authenticated through `window.pywebview.api.api_get()`, which has a
closed path allowlist. The token therefore never reaches JavaScript, and the page
uses no `localStorage`, `sessionStorage` or cookie.

Phase 1 shows: app name and version, `Offline Mode`, backend / database /
data-directory / hardware status, and six disabled cards (Meeting setup,
Recording, Participants, Processing, Review, Export) each stating plainly that it
is not implemented yet. No fake functionality.

Phase 2 enables exactly one of those cards — Recording — and adds the recording
panel described in §3.2. The other five remain disabled and honest.

### Diagnostics — `mom_igd/diagnostics/`

Split into three modules so that `doctor` works on a machine that is not set up
yet — which is the situation it is most useful in:

* `model.py` — result types (`Status`, `CheckResult`, `DoctorReport`) and the text
  renderer. **Standard library only.**
* `doctor.py` — the full run: configuration, database and migration state,
  loopback configuration, offline policy, model registry, future optional
  dependencies, the audio checks described in §3.2, Docker/WSL presence and
  memory.
* `bootstrap.py` — a reduced run with **no third-party import at all**, used
  automatically when a core runtime dependency is missing from the running
  interpreter. It still reports the interpreter, Store-shim status, OS, CPU, disk
  and data path, and reports the missing dependencies as a `FAIL` with the exact
  install command. A traceback is a poor answer to "why doesn't this work?".

Neither run creates a directory or changes anything.

### Offline policy — `mom_igd/offline_policy.py`

Three application-level rules: a **dependency denylist** (cloud SDKs, plus a
separate list of AI dependencies deferred to later phases), an **endpoint rule**
(local filesystem path or loopback URL only), and a **bind rule** (loopback
only). Plus the environment flags future model libraries need for offline mode —
defined now so the worker-spawn path is correct the day a model library appears.

The deferred list is phase-relative, and Phase 2 moved one entry across it:
`sounddevice` left the list because it is now a required runtime dependency,
while `numpy` was added to it — the capture path deliberately does not use
NumPy ([ADR-0006](adr/0006-capture-format-pcm16-device-native.md)), so its
appearance would mean a later phase's dependency arrived early. `soundfile`,
`librosa` and every AI runtime remain deferred.

Explicitly rejected: a global `socket.socket` monkey-patch. It breaks loopback
IPC, hides real bugs behind import order, and gives false confidence. Also
rejected: deliberately dialling out to prove a connection is blocked.
OS firewall hardening is deferred to Phase 11
([ADR-0002](adr/0002-offline-runtime-definition.md)).

### Model registry — `models/registry.json`

A versioned, Git-tracked **declaration**: provider slot, name, version, path,
SHA-256, size, licence metadata, provisioned and offline-ready flags, hardware
profile. Binaries live under `<data_root>/models` and are never committed. There
is no CUDA hardware profile, because such an artefact could never run here.

Empty in Phase 1 — the correct state — and an empty registry produces a doctor
**warning**, never a failure
([ADR-0005](adr/0005-ai-provider-selection-deferred-to-phase-4a.md)).

Still empty in Phase 2: capture needs no model.

---

## 3.2 Phase 2 implementation — the capture engine

Full detail, including the manual acceptance protocol and the Windows
troubleshooting table, is in
[`phase-2-audio-capture.md`](phase-2-audio-capture.md). What follows is the shape
and the reasoning.

### Layering — `mom_igd/audio/`

```
backend.py         AudioBackend protocol · CaptureProfile · StreamStats
  ├ sounddevice_backend.py   the real device (lazy import, no NumPy)
  └ fake_backend.py          deterministic PCM sources, no hardware
devices.py         fingerprint identity · Windows endpoint evidence · discovery
quality.py         RMS / peak / clipping meter, verdict + operator advice
frame_queue.py     bounded queue, capacity in SECONDS of audio
writer.py          exact chunk rotation · PCM .part → WAV → checksum
manifest.py        JSON Lines record + summary with a hash chain
recovery.py        salvage partials, quarantine the ambiguous
preflight.py       disk / device / permission checks before recording
calibration.py     level check with a verdict the operator can act on
session.py         CaptureSession: callback → queue → single writer thread
service.py         RecordingLifecycle, the single-recording lock, DB + jobs
bench.py           capture smoke and accelerated soak
```

The `AudioBackend` protocol is the seam that makes the whole phase testable. The
fake backend is not a mock of convenience: `CounterSource` encodes the frame index
into every sample, so a test can assert that the bytes on disk are *exactly* the
bytes produced, in order, with none lost or duplicated. That is what turns "no
frame loss" from a claim into an assertion. **No test opens a microphone**, and no
test fixture contains a human voice.

### Concurrency — three participants, one rule each

| | Rule |
|---|---|
| Device callback | Copy the bytes, enqueue, return. No I/O, no lock it can wait on, no allocation beyond the copy, and no exception may ever reach PortAudio. |
| Bounded queue | Never blocks the producer. When full it **drops and counts** — back-pressure becomes a number, not a stalled driver. |
| Writer thread | Sole owner of `ChunkWriter` and `QualityMeter`. Everything else touching them takes `_writer_lock`. |

Capacity is expressed in **seconds of audio** (default 5), not in blocks: blocks
vary in size, and "how much audio can we absorb during a disk hiccup?" is the
question that actually matters.

`pause()` was the subtle one. It must finalise the open chunk, which means
touching the writer's state from the caller's thread — so it waits for the writer
to go idle (`_writer_idle`, not merely "queue empty": the writer pops *then*
writes) before doing anything. Two threads interleaving PCM into one chunk would
corrupt it silently.

### Durability, identity, loss accounting

Three decisions carry the phase; each has its own ADR:

* **Format** — int16 PCM at the device's native rate, no resampling, no
  compression ([ADR-0006](adr/0006-capture-format-pcm16-device-native.md)).
* **Durability** — 30 s chunks, metadata sidecar before audio, `fsync`, hash from
  disk, atomic rename, manifest authoritative, gaps recorded and never filled with
  fabricated silence
  ([ADR-0007](adr/0007-chunking-checksums-and-crash-recovery.md)).
* **Device identity** — fingerprint not index, transport verified against the
  Windows registry or reported `UNKNOWN`, and **no silent fallback** to a
  different microphone
  ([ADR-0008](adr/0008-device-identity-and-no-silent-fallback.md)).

### Recording lifecycle

Eleven states, distinct from the job state machine:

```
IDLE → SELECTED → PREFLIGHT_OK → CALIBRATED → STARTING → RECORDING
                                                  │  ▲        │
                                                  │  └────────┤ PAUSED
                                                  ▼           ▼
                                              FAILED ◄──── STOPPING → COMPLETED
                                                              │
                                        RECOVERABLE ──────────┘   CANCELLED
```

The job is advanced with `transition_path()` — a breadth-first search over the
declared legal transitions — rather than a hardcoded route. `DRAFT → RECORDING` is
not a legal single step, and hardcoding `DRAFT → READY → RECORDING` would silently
rot the day the state machine changes. Computing the path keeps the coupling
honest.

**One recording at a time**, enforced twice: an `O_EXCL` lock file (whose stale
PID is cleared via `psutil`) and a partial unique index in SQLite. Neither alone
is sufficient — the lock file survives a database restore, and the index survives
a lock file deleted by hand.

### Surfaces

* **API** — `mom_igd/api/audio_routes.py`, mounted lazily so `doctor` stays
  import-light. Errors map to 409 (wrong lifecycle state), 404 (unknown
  recording), 503 (no backend) and 400 (malformed fingerprint or UUID).
* **UI** — the recording panel polls at ~3 Hz through the Python bridge, so the
  session token still never reaches JavaScript. It shows the level meter, the
  preflight checklist, transport controls, integrity and recovery — and states
  plainly that audio is **not encrypted at rest** yet.
* **CLI** — `audio devices | probe | calibrate | verify | recover | smoke | bench`,
  plus `doctor --production`, which is where the USB-microphone and
  calibration-evidence gates live.

### What Phase 2 deliberately does not do

No VAD, no ASR, no diarization, no speaker identity, no voiceprint, no
transcription, no LLM, no export, no encryption at rest, no consent workflow, no
retention enforcement, no resampling, no 16 kHz working copy, no FLAC. The
capture engine imports nothing from a future phase, and a test asserts it.

---

## 4. Planned design for later phases

Recorded here so the foundation is built to fit, and so nothing is scaffolded
prematurely.

**Audio normalisation (Phase 4).** The 16 kHz mono working copy for ASR is derived
from the Phase 2 master, in the `normalize_audio` stage, where CPU is plentiful and
nothing is time-critical. Capture never resamples
([ADR-0006](adr/0006-capture-format-pcm16-device-native.md)).

**ASR (Phase 4, provider chosen in 4A).** Pass 1 with a small multilingual model,
VAD-gated, word timestamps, an `initial_prompt` carrying the participant names
and an English technical glossary. Segments are flagged for pass 2 on
`avg_logprob`, `no_speech_prob`, compression ratio, low word probability,
high-value content (numbers, dates, names), proximity to a speaker boundary, and
overlap. Pass 2 re-transcribes only flagged segments with ±1.5 s context padding
and replaces pass-1 text only on a materially better score. Pass 2 is capped
(default 25 % of audio) and any truncation of coverage is logged explicitly.

**Diarization and Voice-ID (Phases 5–6).** Matching happens at **cluster** level,
not segment level, so each decision has far more audio behind it, with injective
assignment (Hungarian, not greedy) so one person cannot occupy two clusters.
`UNKNOWN` is a valid, safe outcome: the system never guesses. Overlap regions are
excluded from voiceprint matching entirely, because an embedding of mixed speech
is meaningless.

**MoM and evidence (Phase 8).** Every item carries `evidence`: utterance ids, a
timespan, a speaker and a verbatim quote. Extraction is map/reduce over
speaker-aligned windows with grammar-constrained JSON output. A **deterministic,
non-LLM verifier** then checks that each quote really exists in the cited
utterances, that the timespan is inside their range, that the PIC is a registered
participant or named in the quote, and that the deadline resolves to an absolute
date. Items that fail are flagged for review; a fabricated utterance reference is
discarded outright. A MoM cannot reach `APPROVED` while unreviewed unverified
items remain — enforced in the API, not only the UI.

**Security (Phase 11).** Voiceprints are biometric data: under Indonesia's UU PDP
No. 27/2022 they are *data pribadi bersifat spesifik*, requiring recorded
explicit consent, a purpose limit, a retention policy and a right to erasure.
Encryption at rest (AES-256-GCM with the key protected by Windows DPAPI),
firewall hardening, backup/restore and retention enforcement all land here.
**None of this exists in Phase 1, and retention is not enforced yet.**

---

## 5. Phase roadmap

| Phase | Objective | Key exit evidence |
|---|---|---|
| 0 ✅ | Audit, feasibility, architecture | Environment matrix; hardware implications; risk list |
| 1 ✅ | Application foundation | doctor 0 FAIL · WAL + FK verified · migrations transactional & idempotent · loopback + token proven · smoke green · suite green |
| **2 ◀** | **Offline audio capture** | Byte-exact chunks against a deterministic source · no unrecorded frame loss · recovery survives kill/truncation/corruption and is idempotent · manifest tamper-evident · no silent device fallback. **Production acceptance additionally requires a real USB conference microphone**: 3 × 60 min on hardware, CPU < 5 %, RSS < 300 MB |
| 3 | Participants & voice enrollment | 9 voiceprints encrypted at rest; consent recorded; intra/inter-speaker separation measured |
| 4A | **Benchmark gate** | Real xRT, peak RSS, WER/DER for every candidate on the real device |
| 4 | Offline ASR | WER within target; pass 2 measurably improves flagged segments; budget cap honoured |
| 5 | Diarization | DER/JER on 9 speakers with the production microphone |
| 6 | Voice identification | Calibrated thresholds; **zero false-confident assignments** |
| 7 | Transcript reconciliation | Byte-identical output for identical input (fully deterministic) |
| 8 | MoM intelligence | Every item has valid evidence; zero fabricated references |
| 9 | Review UI | A reviewer completes a 60-minute meeting in reasonable time; approval gate unbypassable |
| 10 | Exports & action tracking | All four formats offline; approved snapshots reproducible |
| 11 | Security, packaging, backup | Clean install on an air-gapped machine; zero egress observed; restore verified; DPIA |
| 12 | Evaluation & pilot readiness | 5 real meetings end-to-end; resilience tests; runbook |

Phase 4A is inserted deliberately: four technology decisions still rest on
numbers that have not been measured on the real device. Writing production code
before those numbers exist would mean building on a guess.

### Expected bottlenecks

Diarization on CPU is the dominant cost and carries the widest uncertainty, which
is why it is the first thing Phase 4A measures. Then selective pass-2 ASR
(bounded by its cap), then RAM headroom, then far-field single-microphone audio
quality — which is the real ceiling on achievable accuracy, and a hardware
problem rather than a software one.

Post-meeting processing for a two-hour meeting is expected to take roughly
1.5–3× the meeting duration. The realistic usage model is *record today, review
tomorrow morning*; this must be communicated rather than discovered.
