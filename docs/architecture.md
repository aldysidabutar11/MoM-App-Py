# Architecture

Fully offline Minutes of Meeting application for in-person meetings, running
natively on Windows 11 on a single laptop.

* **Current phase: 1 — application foundation.**
* Decisions already taken: [`adr/`](adr/)
* Evidence behind those decisions: [`phase-0-summary.md`](phase-0-summary.md)

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
    │ named pipe (control + VU)     │ spawn per stage, then kill
┌───▼─────────────────────────┐ ┌───▼──────────────────────────┐
│ mom-recorder    (Phase 2)   │ │ mom-worker  (Phase 4+)       │
│ • WASAPI capture            │ │ • exactly ONE model loaded    │
│ • high-priority audio thread│ │ • killed after each stage so  │
│ • lock-free ring buffer     │ │   the OS reclaims the RAM     │
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
| api → recorder | named pipe (start/stop/marker) | Low latency, never blocks the audio thread |
| recorder → api | named pipe (VU, clipping, frames, drops) | One-way, loss-tolerant telemetry |
| api → worker | subprocess with `--job-id --stage` | Absolute memory isolation |
| worker → api | JSON events on stdout | Simple, loggable, crash-tolerant |
| worker → DB | direct writes to artefact tables only | Bulk inserts over HTTP would be wasteful |
| api → DB | **sole writer** of `jobs` / `job_stages` state | Prevents state-machine races |

Rules that must not be broken: the recorder never loads a heavy model; the worker
never touches an audio device; the recorder and a worker never run concurrently
(enforced by the state machine); the UI never opens SQLite directly.

---

## 3. Phase 1 implementation

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

### Diagnostics — `mom_igd/diagnostics/`

Split into three modules so that `doctor` works on a machine that is not set up
yet — which is the situation it is most useful in:

* `model.py` — result types (`Status`, `CheckResult`, `DoctorReport`) and the text
  renderer. **Standard library only.**
* `doctor.py` — the full run: configuration, database and migration state,
  loopback configuration, offline policy, model registry, future optional
  dependencies, audio devices (via `ctypes`/`winmm`, so no audio library is
  needed), Docker/WSL presence and memory.
* `bootstrap.py` — a reduced run with **no third-party import at all**, used
  automatically when a Phase 1 runtime dependency is missing from the running
  interpreter. It still reports the interpreter, Store-shim status, OS, CPU, disk
  and data path, and reports the missing dependencies as a `FAIL` with the exact
  install command. A traceback is a poor answer to "why doesn't this work?".

Neither run creates a directory or changes anything.

### Offline policy — `mom_igd/offline_policy.py`

Three application-level rules: a **dependency denylist** (cloud SDKs, plus a
separate list of AI/audio dependencies deferred to later phases), an **endpoint
rule** (local filesystem path or loopback URL only), and a **bind rule**
(loopback only). Plus the environment flags future model libraries need for
offline mode — defined now so the worker-spawn path is correct the day a model
library appears.

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

---

## 4. Planned design for later phases

Recorded here so the foundation is built to fit, and so nothing is scaffolded
prematurely.

**Audio capture (Phase 2).** 48 kHz / mono / 16-bit FLAC master via WASAPI shared
mode; 16 kHz working copy for AI. 30-second chunks written as `.part` then
`fsync`ed and atomically renamed, so any visible chunk file is by definition
complete and checksummed. A monotonic sample counter detects dropped frames;
every gap is recorded explicitly, never hidden — an unrecorded gap would shift
every downstream timestamp and break the evidence chain. Recovery re-verifies
checksums, rebuilds missing manifest entries, salvages a truncated tail, and
never auto-resumes into an old session.

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
| **1 ◀** | **Application foundation** | doctor 0 FAIL · WAL + FK verified · migrations transactional & idempotent · loopback + token proven · smoke green · suite green |
| 2 | Offline audio capture | 3 × 60 min with no unrecorded frame loss; recovery survives 5 random kills; CPU < 5 %, RSS < 300 MB |
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
