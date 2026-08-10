# Team handoff audit — MoM-IGD

**Date:** 2026-08-05 · **Commit audited:** `4674ea4` · **Branch:** `master` · **Working tree:** clean
**Mode:** read-only. No source file, test, migration, configuration file or dependency manifest was
modified. The only repository change is the creation of the five documents this audit was asked to
produce.

---

## 1. Verdict

# `SAFE_TO_SHARE_FOR_DEVELOPMENT_ONLY`

The two questions the brief asked to be kept apart:

| Question | Answer |
|---|---|
| **Is the repository fit to hand to a development team?** | **Yes.** It is clean, disciplined, documented to an unusually high standard, contains no secrets or personal data, and every claim in it is either backed by evidence or explicitly marked as unproven. The gaps that matter for a team are process gaps (no CI, no lint configuration, no contribution files), not code quality. |
| **Is the application fit to use in a real production meeting?** | **No.** One P1 defect breaks the GUI transcription workflow for any meeting longer than about three minutes; real-speech accuracy has never been measured; no speaker-embedding model has been selected, so nobody can be identified; there is no USB conference microphone; the consent wording is a draft; and there is no backup, restore, retention or packaging. |

`READY_FOR_CONTROLLED_INTERNAL_PILOT` was considered and rejected: a pilot requires the primary
workflow to complete for a real meeting length, and MOM-BUG-001 means it does not.

`PRODUCTION_READY` is not available: real-speech accuracy, privacy approval, backup/recovery,
packaging and a real-meeting pilot all lack evidence, exactly as the brief specified.

---

## 2. Executive summary

MoM-IGD is a fully offline Windows desktop application that turns a recording of an in-person
meeting into a structured Minutes of Meeting. Four of twelve planned phases are built: the
application foundation, offline audio capture, participants and voice enrollment, and offline
transcription.

**The engineering is of high quality.** 2 263 automated tests pass with no failures and no skips.
Every architectural invariant the project set for itself — no cloud, no CUDA, loopback only, one
heavy model at a time, master audio never modified, no speaker attribution before Phase 5 — was
checked during this audit and holds. Several of them are enforced structurally rather than by
convention: configuration validation refuses a non-loopback host, the database refuses a second
active recording, and `validate_transcription` rejects any result carrying a speaker. The codebase
contains **zero** `TODO`, `FIXME`, `HACK` or `XXX` markers, no dead scaffolding for future phases,
and no unreachable feature flags.

**The honesty of the documentation is its most unusual property, and it is load-bearing.** The
build deliberately still reports `phase: 3` although Phase 4 is implemented, because raising it
would assert a capability that has not been validated. `docs/benchmarks.md` marks every accuracy
row `N/A — PENDING` rather than substituting a plausible number, and records a *withdrawn* finding
where a single benchmark sweep did not reproduce. `docs/phase-4-progress.md` lists fifteen defects
the team found in its own work and what found each one. A reviewer can trust the parts of this
repository that claim to be proven, because the parts that are not proven say so.

**What this audit found that the documentation does not say.** Three things, in order of weight:

1. **The desktop shell aborts every transcription that takes longer than 60 seconds** (MOM-BUG-001,
   P1). The shell's Python bridge has a hard 60-second HTTP timeout; `POST /asr/transcribe` runs
   the whole pipeline synchronously inside that request. Reproduced during this audit against the
   real `ShellApi._send` code path. The pipeline still finishes and writes a correct transcript,
   but the UI reports *Gagal* and stops polling. The project's own manual acceptance script
   (Part D: record 3–5 minutes, then transcribe) will hit it.
2. **Phase 4 runs entirely outside the job state machine** (MOM-DEBT-001, P2). `jobs` and
   `job_stages` are created by the capture service and then never advanced; the ASR pipeline keeps
   its state in `transcripts.status`. Both stated invariants — CLAUDE.md's *"`jobs` is the single
   owner of workflow state"* and the architecture document's *"api → DB sole writer of
   `jobs`/`job_stages` state"* — are therefore not true of Phase 4. Verified in the production
   database: both jobs sit at `RECORDED` and all 20 stage rows are untouched. This is a decision to
   make deliberately before Phase 7, not a bug today.
3. **There is no CI, no lint configuration and no type checking** (MOM-DEBT-003, P2). Survivable
   with one author; not with a team, especially for a 10-minute test suite nothing triggers
   automatically.

**Zero P0 findings.** No data loss, no privacy breach, no security weakness and no silently-wrong
critical result was found. The production data root was byte-identical before and after this audit.

---

## 3. Repository state

### 3.1 Git

```
Branch  : master (no remotes, no tags, no other branches)
HEAD    : 4674ea4  Phase 4: acceptance preflight and operator handoff
Status  : nothing to commit, working tree clean
git diff / git diff --cached : empty
```

| Commit | Subject | Verified |
|---|---|---|
| `4674ea4` | Phase 4: acceptance preflight and operator handoff | ✅ 24 files, +3 003 / −56 |
| `7fcf8a6` | Phase 4: offline ASR — provisioning, pipeline, selective second pass | ✅ |
| `e5cc427` | Phase 3: participants, consent, enrollment, and meeting rosters | ✅ |
| `4a9518d` | Phase 2: offline audio capture | ✅ |
| `b2a089c` | Phase 1: application foundation | ✅ |

All three reported checkpoints confirmed. 188 tracked files. Untracked/ignored entries are build
artefacts only (`.venv`, `.coverage`, `.pytest_cache`, `__pycache__`).

### 3.2 Structure

```
mom_igd/          15 000 lines of Python + 2 400 lines of vanilla JS
  api/            7 modules   FastAPI, loopback only, four routers
  asr/           19 modules   Phase 4 transcription
  audio/         15 modules   Phase 2 capture engine
  db/             3 modules + 5 migrations
  diagnostics/    5 modules   doctor, bootstrap, audio and enrollment checks
  enrollment/    11 modules   Phase 3 participants, consent, voiceprints
  jobs/           2 modules   workflow state machine
  shell/          launcher + static web page (no npm, no CDN, no framework)
tests/           60 files, 2 263 tests
docs/            16 ADRs, architecture, per-phase guides, benchmarks
scripts/         1 read-only PowerShell preflight
config/          default.toml + glossary.id-en.toml
```

Surface counts: **6 top-level CLI command groups** (`doctor`, `db`, `config`, `registry`, `audio`,
`asr`, `participant`, `serve`, `smoke`, `shell`) · **API routes**: 2 public + `/doctor` +
`/internal/ready` + `/audio/*` + `/enrollment/*` + `/asr/*` (7) · **GUI**: 3 live panels
(recording, participants/enrollment, transcription) and 3 honestly-disabled cards.

### 3.3 Migration immutability — verified

The migrator hashes **canonically** (BOM stripped, line endings unified, trailing whitespace
removed) because the development machine has `core.autocrlf=true`. The canonical checksum is
therefore the correct baseline; a raw `Get-FileHash` is not, and will differ for any file with
trailing whitespace.

| Ver | Name | Canonical SHA-256 (what the migrator records) | Raw file SHA-256 | File bytes |
|--:|---|---|---|--:|
| 1 | `initial` | `f1426fa94b8ae90e4c0b646c0f132ac4a483525165675c947649cad124e89796` | same | 14 219 |
| 2 | `audio_capture` | `8d42086530a4560d28ca5cfd2707b5402c1b2872fea02a49ce3768106f570ded` | same | 15 018 |
| 3 | `participants_consent_voiceprints` | `fb3220d96d9b9a711189ca2a3d275e0bb74e6d0043e86db17d122d3cf9079fc1` | `067062c8…0444` | 19 568 (19 215 canonical) |
| 4 | `meeting_participant_capacity` | `54a7908e80d8c8cae2397deb1c644e0242f13e8370711010defa8a404d808172` | same | 3 632 |
| 5 | `offline_asr` | `c8d4f6b4b64e62e56c1b534691e6d5871417e0973a7a517e1e2bf505d462bb61` | same | 21 550 |

Compared against what each database actually recorded, read from **copies** taken outside the
repository:

| Database | 0001 | 0002 | 0003 | 0004 | 0005 |
|---|---|---|---|---|---|
| Production `D:\MoM-IGD-Data` | MATCH | MATCH | MATCH | not applied | not applied |
| Acceptance `D:\MoM-IGD-Models-Phase4` | MATCH | MATCH | MATCH | MATCH | MATCH |

**Conclusion: migrations 0001–0005 are immutable and unmodified.** Nothing has been edited after
being applied. Record the canonical column above as the baseline; note in the team's onboarding
that raw file hashes are not comparable.

### 3.4 Runtime data roots — untouched

| | Production | Acceptance |
|---|---|---|
| Path | `D:\MoM-IGD-Data` | `D:\MoM-IGD-Models-Phase4` |
| Schema | **3** (migrations 1–3) | **5 of 5** |
| `PRAGMA integrity_check` | `ok` | `ok` (via `db verify`) |
| `PRAGMA foreign_key_check` | clean | clean |
| Audit chain | 26 events | 14 events, **intact** |
| Content | 2 meetings, 2 recordings (both `RECORDED`), 5 chunks, 0 participants, 0 voiceprints, 0 consent events | 1 meeting, the 24 s synthesised verification recording, 6 transcript revisions, both models provisioned |
| Touched by this audit | **No** | Read-only commands only |

A SHA-256 inventory of every file under the production root was taken before and after the audit.
They are **byte-identical**: same files, same sizes, same modification times, same hashes. The
production database was inspected only through a copy in a scratchpad directory outside the
repository; no command was ever pointed at the production root.

### 3.5 Code hygiene sweep

Searched across the whole repository, not just the documentation:

| Looked for | Found |
|---|---|
| `TODO`, `FIXME`, `HACK`, `XXX` | **0 in source.** Every hit is the ordinary English word in a comment or docstring. |
| `NotImplementedError` | 1, in `pyproject.toml`'s coverage `exclude_lines`. None raised in code. |
| Empty placeholder modules / future-phase scaffolding | **None.** Every package has real content. |
| Dead code, deprecated paths | 2 stale capability blocks (MOM-RISK-008); nothing else |
| Feature flags never enabled | **None.** No environment variable or configuration key can select a fake provider, a test double or an alternative engine — verified by grep over `getenv`/`environ.get` across `mom_igd/`. |
| GUI buttons without a handler | **None.** `tests/test_asr_ui_contract.py` resolves every `getElementById` against the markup — a discipline that came from a real past defect (a dead revoke dialog). |
| Unused API endpoints | None orphaned; every route is reached by the CLI, the shell allowlist or a test |
| Tests that only test a mock | Present and material — see MOM-DEBT-004. The GUI contract tests are static string assertions and cannot fail on a runtime defect. That is why MOM-BUG-001 survived. |

---

## 4. Verified test evidence

Every command below was run during this audit. ASR commands targeted the acceptance root.

| Check | Command | Result |
|---|---|---|
| Byte-compile | `python -m compileall -q mom_igd tests` | **exit 0** |
| Dependency consistency | `python -m pip check` | **No broken requirements found** |
| Interpreter | `python -V` | **3.12.10**, in `.venv`, official distribution (not the Store shim) |
| Full suite + coverage | `python -m pytest -q --cov=mom_igd --cov-report=term-missing` | **2 263 passed · 0 failed · 0 skipped · 14 warnings · 602 s** |
| Coverage | same run | **84%** (12 417 statements, 1 666 missed; branch coverage on, 2 930 branches, 417 partial) |
| Backend smoke | `python -m mom_igd smoke` | **PASS 11/11** |
| Capture smoke | `python -m mom_igd audio smoke` | **PASS 9/9** |
| Diagnostics | `python -m mom_igd doctor --json` | **24 PASS / 11 WARN / 0 FAIL**, exit 0 |
| Model inventory | `python -m mom_igd asr models` | 2 provisioned, both `OK` |
| Model integrity | `python -m mom_igd asr verify` | **Every byte re-hashed from disk** — both models match |
| Real-model offline smoke | `python -m mom_igd asr smoke` | **PASS 11/11**, zero outbound attempts recorded |
| Database integrity | `python -m mom_igd db verify` | schema 5 of 5, WAL, FK on, **audit chain intact** |
| Operator preflight | `scripts\phase4_acceptance_preflight.ps1` | **9 PASS / 4 WARN / 0 FAIL**, exit 0, `READY FOR MANUAL FUNCTIONAL TESTING` |

### Reconciliation with the reported figures

| Reported | Observed | Verdict |
|---|---|---|
| 2 263 passed, no failures, no skips | 2 263 passed, 0 failed, 0 skipped | ✅ exact |
| Coverage ~84% | 84% | ✅ exact |
| Phase 4 service coverage ~96% | `asr/service.py` 96% | ✅ exact |
| ASR smoke 11/11 | 11/11 | ✅ |
| Audio smoke 9/9 | 9/9 | ✅ |
| Doctor 25 PASS / 10 WARN / 0 FAIL | **24/11/0** direct, **25/10/0** minutes later inside the preflight | ⚠️ both correct — see below |
| Preflight 10 PASS / 3 WARN / 0 FAIL | **9/4/0** | ⚠️ same cause |
| `CURRENT_PHASE=3`, `APP_VERSION=0.3.0` | confirmed in `mom_igd/version.py` and `pyproject.toml` | ✅ |
| Pass 1 `faster-whisper-small`, pass 2 `faster-whisper-large-v3-turbo` | confirmed, both deep-verified | ✅ |
| Production database at schema 3 | `user_version = 3` | ✅ |

**The one difference is not a regression.** The `ram` check crossed its 2 048 MB warning threshold
between the two runs because Docker/WSL was holding 3 149 MB. `ram` is a warning threshold only and
can never become a `FAIL` (`mom_igd/diagnostics/doctor.py:254-267`), so advancing `CURRENT_PHASE`
would not change it. Both observations are recorded rather than the flattering one being reported.

### Coverage detail

**Below 80%:**

| Module | Cover | Explained by the team? |
|---|--:|---|
| `mom_igd/__main__.py` | 0% | Yes — `if __name__` guard, covered in a child process |
| `asr/provision.py` | 31% | Yes — needs a real network download |
| `asr/faster_whisper_provider.py` | 39% | Yes — needs a 464 MiB model load |
| `asr/smoke.py` | 44% | Yes — it *is* the real-model test |
| `diagnostics/__init__.py` | 50% | Re-export shim |
| **`cli.py`** | **62%** | **No — 347 of 1 002 statements missed, not mentioned anywhere.** MOM-DEBT-002 |
| `asr/benchmark.py` | 65% | Yes |
| `enrollment/keys.py` | 68% | Yes — DPAPI `ctypes` paths, verified live by hand |
| `asr/worker.py` | 72% | Yes — the child runs in a spawned interpreter coverage cannot instrument |
| `diagnostics/enrollment_checks.py` | 76% | Partly |
| `shell/launcher.py` | 76% | Yes — `run_shell()` opens a GUI window |

**Critical production modules below 90%:** `asr/pipeline.py` 87 · `api/asr_routes.py` 86 ·
`api/routes.py` 84 · `asr/installed.py` 84 · `enrollment/service.py` 87 · `enrollment/cipher.py` 88 ·
`enrollment/store.py` 89 · `audio/writer.py` 89 · `audio/manifest.py` 87 · `audio/devices.py` 89 ·
`audio/preflight.py` 89 · `diagnostics/doctor.py` 83 · `smoke.py` 82.

**Genuinely untestable without hardware or a model:** the four modules above that the team already
documents, plus `run_shell()` and the DPAPI call sites.

**Should be automatable but is not:** `cli.py`'s command handlers (a `CliRunner`-style harness over
`main(argv)` would reach most of the 347 missed statements), the `pipeline.py` error branches, and
— most importantly — an integration test that drives `ShellApi` against a running backend, which is
the class of test that would have caught MOM-BUG-001.

---

## 5. Current architecture

```
┌──────────────────────────────────────────────────────────────┐
│ shell         pywebview + WebView2, static HTML/CSS/JS       │
│               3 live panels; token never enters JavaScript   │
└───────┬───────────────────────────────┬──────────────────────┘
        │ loopback HTTP (page → public) │ ShellApi bridge (Python, allowlisted)
        │                               │   ⚠ 60 s hard timeout — MOM-BUG-001
┌───────▼───────────────────────────────▼──────────────────────┐
│ api           FastAPI/uvicorn on 127.0.0.1, ephemeral port   │
│               Host-header allowlist · session token          │
│               4 routers, all token-protected but /health,/version │
└───┬───────────────────────────────────┬──────────────────────┘
    │ in-process (no model loaded)      │ spawn per stage, exits after
┌───▼──────────────────────────┐  ┌─────▼────────────────────────┐
│ capture engine  (Phase 2)    │  │ asr worker  (Phase 4)        │
│ callback → bounded queue     │  │ vad | transcribe | probe     │
│ single writer thread         │  │ exactly ONE model resident   │
│ chunk + fsync + SHA-256      │  │ offline flags asserted first │
│ manifest is authoritative    │  │ peak RSS sampled by parent   │
└──────────────────────────────┘  └──────────────────────────────┘
                    │                              │
              ┌─────▼──────────────────────────────▼─────┐
              │ SQLite (WAL, FK, verified per connection)│
              │ evidence chain, hash-chained audit trail │
              └──────────────────────────────────────────┘
```

**Boundaries that hold.** Nothing under `mom_igd/audio/` imports `mom_igd.enrollment`; nothing under
`mom_igd/asr/` imports either — roster size can never gate recording or transcription. `doctor`
stays import-light and works on a bare interpreter. `mom_igd/paths.py` owns every runtime path and
no directory is created as an import side effect.

**Boundary that does not hold as documented.** `jobs`/`job_stages` — see MOM-DEBT-001.

---

## 6. Current phase status

| Phase | Claim | Audited status |
|---|---|---|
| 0 Audit and architecture | ✅ | Confirmed. 16 ADRs, contiguous, each with real reasoning. |
| 1 Foundation | ✅ | Confirmed and verified by command. |
| 2 Audio capture | ✅ code | **Code complete and verified on a fake backend. Production acceptance NOT granted** — no USB conference microphone, no 3 × 60 min run on hardware. |
| 3 Participants and enrollment | ✅ code | **Machinery complete; the capability is BLOCKED.** No speaker-embedding model has been selected, so **no real voiceprint has ever been produced by this build.** Everything Phase 3 verified used a deterministic fake provider that three barriers keep out of production. |
| 4A Benchmark gate | ✅ | Confirmed: 30 runs, RTF and peak RSS measured, zero egress measured. Accuracy explicitly `N/A`. |
| 4 Offline ASR | *"implemented and tested; accuracy acceptance PENDING"* | **Accurate, and one defect the claim does not cover.** The pipeline, persistence, selection, merge and glossary are genuinely done. The GUI path is broken past 60 s (MOM-BUG-001). Accuracy is unmeasured (MOM-GAP-001). |
| 5–12 | not started | **Confirmed not started and not scaffolded.** Only declarative `StageSpec` rows and reserved audit categories exist. Scope discipline has held. |

**`CURRENT_PHASE = 3` is the right value and should not be raised.** Raising it would change what
`doctor` calls a `FAIL` on the strength of a stage whose accuracy has never been measured. This
audit endorses the existing decision.

---

## 7. Completed capabilities

Verified during this audit, not taken from documentation. Full evidence in
[`feature-completion-matrix.md`](feature-completion-matrix.md).

* **Offline guarantee.** Dependency denylist, provider-endpoint rule, loopback bind rule, seven
  Hugging Face offline flags set by **assignment** (never `setdefault`, so a hostile
  `HF_HUB_OFFLINE=0` cannot put a worker online), inherited HF tokens deleted. Zero outbound
  attempts recorded across 30 benchmark runs and every smoke run, with a recorder a test proves can
  report one.
* **Loopback and token security.** Two independent loopback layers; router-level token dependency
  on every non-public route; a credential in a query string refused with 400 even when correct;
  the token redacted in `__str__`/`__repr__`/`__format__`, unpicklable, and scrubbed from every log
  handler including uvicorn's. `smoke` proves all of it, 11/11.
* **Audio capture integrity.** Byte-exact chunks against a deterministic source; one flipped byte
  detected; recovery salvages a partial, is idempotent, and the salvaged chunk verifies; a gap is
  recorded and never filled in the master; a device is identified by fingerprint and a missing one
  raises rather than falling back.
* **Model integrity.** Three distinct layers — approved catalogue, installed registry, runtime
  resolver — with readiness granted only after a load-and-decode probe, never a directory scan.
  `asr verify` re-hashed all 2 011 MiB during this audit and matched.
* **Evidence chain.** recording → working copy → VAD run → transcript revision → segments → words,
  each link recording the provenance of the one above it, with model name, revision and manifest
  SHA-256 stored on the transcript.
* **Dynamic participant capacity.** Swept for a hidden 9 or 15 across the database, API, GUI,
  configuration, tests and documentation: none exists. Capacity is per meeting, the DB invariant is
  only `>= 1`, the ceiling lives in configuration (default 50), and a grandfathered value is never
  clamped.
* **Voiceprint protection.** AES-256-GCM with AAD binding each envelope to its voiceprint UUID,
  participant, schema and model identity, so an envelope moved between people fails to authenticate
  rather than mis-identifying someone. Master key protected by DPAPI with a domain separator, never
  created implicitly, atomically written, and never unwrapped by `doctor`.
* **No speaker before Phase 5.** No column exists and `validate_transcription` rejects any result
  carrying one.

---

## 8. Incomplete capabilities

| Capability | State | Reference |
|---|---|---|
| GUI transcription of a real-length meeting | Broken past 60 s | MOM-BUG-001 |
| Real-speech accuracy (WER, term recall, timestamp error, pass-2 benefit) | Never measured | MOM-GAP-001, MOM-GAP-003 |
| Real voice enrollment | Blocked — no embedding model selected | MOM-GAP-002 |
| Phase 2 production acceptance | Not granted — no USB microphone | MOM-GAP-004 |
| Job lifecycle for transcription | Phase 4 bypasses the state machine | MOM-DEBT-001 |
| Stale-transcript recovery | Does not exist | MOM-RISK-003 |
| Backup, restore, retention enforcement | Do not exist (Phase 11) | MOM-GAP-007 |
| Packaging / installer / offline wheelhouse | Do not exist (Phase 11) | MOM-GAP-007, MOM-RISK-006 |
| Consent wording | Draft, no legal review, no DPIA | MOM-GAP-005 |
| CI, lint, type checking | Do not exist | MOM-DEBT-003 |
| Long-meeting resource behaviour | Unmeasured (longest run on record: 24 s) | MOM-RISK-010 |

---

## 9. Production blockers

The five that stand between today and a controlled internal pilot:

1. **MOM-BUG-001** — the shell aborts transcription at 60 s. The primary workflow does not
   complete. *Days, not weeks: the service already exposes everything an asynchronous POST needs.*
2. **MOM-GAP-001** — accuracy has never been measured against real speech. Needs a consented or
   licensed Indonesian corpus with reference transcripts, ≥ 10 minutes of it far-field. The harness
   (`asr bench --manifest`) exists, is tested, and enforces a consent gate.
3. **MOM-GAP-004** — no USB conference microphone. Every accuracy number produced on the internal
   Intel Smart Sound array is invalid as production evidence, because its beamforming suppresses
   speakers not facing the laptop. This is a purchase, and it is on the critical path.
4. **MOM-GAP-005** — the consent wording is a draft with no legal or compliance review, and there
   is no DPIA. Voiceprints are *data pribadi bersifat spesifik* under UU PDP No. 27/2022. Recording
   real people cannot start without this.
5. **MOM-GAP-007 + MOM-RISK-001** — no backup, no restore, no retention enforcement, and the
   DPAPI-protected voiceprint key has no escrow. The moment real biometric data exists, a lost
   Windows profile destroys it permanently.

---

## 10. Security and privacy observations

**No security defect was found.** The specific attack surfaces the brief asked about, and what the
code does:

| Surface | Finding |
|---|---|
| Endpoint authorisation | Every router carries `Depends(require_session_token)`. Only `/health` and `/version` are public and both disclose booleans only — no path, no hardware inventory, no user data. |
| Shell allowlist | Exact-match sets per HTTP method plus anchored UUID-templated patterns. A `?` or `#` in the path is refused outright rather than stripped. No `/asr/*` or `/enrollment/*` wildcard. `provision` is unreachable from the page, and no route can delete a participant or a voiceprint directly. |
| CORS | No CORS middleware — correct. The page and the API share an origin; adding one would only create a hole. |
| DNS rebinding / local attack surface | `LoopbackHostMiddleware` rejects a non-loopback `Host` with 403; proven by `smoke`. A local process still needs the token, which lives only in the backend's memory. |
| Path traversal | `paths.py` owns every runtime path; identifiers that become filenames are validated against a canonical UUID regex before use; `installed._escapes_store` checks POSIX **and** Windows path flavours (a POSIX-absolute path once slipped through a naive check). |
| Command injection | No `shell=True`, no `os.system`. The only subprocess use is `multiprocessing` with `spawn` and JSON-serialisable payloads. |
| Unsafe deserialisation | No `pickle` on any data path. `SessionToken.__reduce__` raises. Worker payloads are plain JSON dictionaries. |
| SQL injection | Every query is parameterised. The three f-string SQL constructions interpolate only column names validated against a frozen allowlist, or `?` placeholder counts. |
| Secrets in logs | `RedactingFilter` scrubs the live token and anything resembling `token=` in a URL from every handler. Worker errors are truncated to type + 300 characters specifically so an ASR exception cannot carry an audio path or decoded text into a log. |
| Sensitive data in responses | No filesystem path leaves the API. `_public_transcript` strips internal references. Transcript text is never in a status poll. |
| Malformed / oversized payloads | `POST /asr/transcribe` bounds the body to exactly 36 characters. Enrollment accepts no audio over HTTP at all — samples are captured in Python, which is what keeps raw biometric audio out of the browser. |
| Biometric data handling | Templates are AES-256-GCM sealed with an AAD that binds them to one participant and one model; the key is DPAPI-protected; raw enrollment audio is held in bounded memory and discarded; a display name never reaches the filesystem. |

**Privacy gaps, all Phase 11 and all acknowledged in the repository:** transcripts and recordings
are **not** encrypted at rest (the UI states this plainly), retention is not enforced, there is no
backup or restore, and the audit chain cannot detect tail truncation without an external anchor.
One risk the repository does *not* state: the DPAPI key has no escrow, so its loss is unrecoverable
by design (MOM-RISK-001).

**Repository privacy:** no participant name, recording, voiceprint, transcript or secret is
committed. `.gitignore` covers `*.wav`, `*.db*`, `/recordings/`, `/voiceprints/`, `/keys/`,
`models/**`, `.env*` and `config/local.toml`, with runtime-data patterns correctly anchored to the
repository root (a bare `audio/` once silently excluded the whole capture engine). No test fixture
contains a human voice — PCM is generated deterministically.

---

## 11. Performance observations

Target device: Intel Core i7-1260P (12 physical / 16 logical), 16 GB, Intel Iris Xe (no CUDA),
Windows 11 build 26200, CPU-only inference.

**Measured, and trustworthy** (30 runs, 5 sequential sweeps, on 60 s of synthetic audio):

| | RTF median | Peak worker RSS |
|---|--:|--:|
| `faster-whisper-small`, 12 threads, beam 1 | **0.142** | 693 MiB |
| `faster-whisper-large-v3-turbo`, 12 threads, beam 5 | **0.284** | 1 910 MiB |

* **Two models cannot be co-resident:** 693 + 1 910 = 2 603 MiB against a 2.5 GB budget. This is
  measured evidence for the one-heavy-worker policy, not a theoretical concern. The design
  addresses it correctly: each stage is a separate spawned process that exits before the next
  starts, which is the only reliable way to return arena memory to Windows.
* **Worker cleanup is sound:** cooperative cancel flag → 45 s grace → `terminate()` → 10 s →
  `kill()`. Peak RSS is sampled by the parent while the child runs, including its children,
  because it cannot be recovered afterwards.
* **No memory leak was found.** Nothing accumulates across runs in the parent; the heavy allocation
  lives and dies with the child process.

**Unmeasured, and material:**

* **Long meetings.** The longest end-to-end pipeline run recorded anywhere is **24 seconds**. For
  two hours: region lists cross the process boundary as one payload, `transcript_words` grows (the
  24 s run alone produced 2 530 word rows), the working copy reaches ~345 MB and is SHA-256'd on
  every re-run, and SQLite growth is unknown. The architecture document predicts 1.5–3× meeting
  duration for post-processing; that prediction has never been tested. (MOM-RISK-010)
* **Recording quality while ASR runs.** No priority management exists anywhere — `grep` for
  `nice(`/`SetPriorityClass`/`priority` finds nothing. A pass-2 decode takes 12 threads on 12
  physical cores and ~1.9 GB on a machine `doctor` currently reports as having 1.8–2.6 GB free. The
  capture path would *record* any loss (bounded queue drops and counts, `degraded` flag) rather than
  hiding it, but the loss would still happen. (MOM-RISK-002)
* **Cancellation latency.** Bounded by design at one region (≤ 30 s) plus a 45 s grace, but never
  timed on a real run.
* **Background polling.** 1200 ms in the transcription panel, ~3 Hz in the recording panel, each
  poll chained from the previous so they cannot overlap. Cheap and correct.

**Disk.** Master audio 691 MB/hour at 48 kHz stereo; working copy ~115 MB/hour. Preflight requires
2 GB free for a transcription and 5 GB to start a recording — but `transcribe()` does not re-check
disk, so the CLI and a direct API call can start a run that fills the volume (MOM-RISK-004).

---

## 12. Team-readiness assessment

### What is genuinely ready

| Area | Assessment |
|---|---|
| README setup from a new machine | **Excellent.** Python 3.12 acquisition (with the Store-shim trap called out), virtualenv, dependencies, every command, exit codes, troubleshooting. |
| Supported Python | Pinned `>=3.12,<3.13` in `pyproject.toml`, asserted by `doctor` and by the preflight. |
| Dependency pinning | `requirements.txt` and `requirements-dev.txt` pin every line with `==`; a test fails if one is not pinned. `pip check` clean. |
| Windows prerequisites | Documented, including WebView2 and the PortAudio/WASAPI situation. |
| Model provisioning | One documented command, network-gated, never implicit, with byte verification and a load-and-decode probe. |
| Data-root separation | Three roots clearly distinguished (development temp, acceptance, production) and enforced — tests fail if they touch the real one. |
| Sample configuration | `config/default.toml` is heavily commented and explains *why* each value is what it is; `local.toml` is git-ignored. |
| Secret handling | No secrets in the repository; `.env*` ignored; token policy documented and enforced. |
| Test commands | Documented, including the PowerShell pipe trap that makes pytest appear to hang. |
| Architecture documentation | 438 lines plus 16 ADRs. Among the best this auditor has read on a project this size. |
| Database schema documentation | The migrations themselves are the documentation, and they are extraordinary — each explains the reasoning, the rejected alternative and the failure it prevents. |
| API documentation | Docstrings on every route stating the rule and its reason; Swagger available over loopback. |
| Coding standard | Stated in `CLAUDE.md`/`AGENTS.md` and followed consistently. |
| Data privacy rules | Explicit, enforced by tests, and repeated where they matter. |
| Test fixture policy | Explicit: no human voice, deterministic PCM, temporary directories only, session-fixture guard on the real data root. |
| Prohibition on real participant data in Git | Explicit and enforced by `.gitignore` + `test_repo_hygiene.py`. |

### What is missing

| Gap | Impact on a team |
|---|---|
| **No CI** | A 10-minute suite that nothing triggers will drift within weeks. |
| **No lint or type-check configuration** | `# noqa` markers show a linter was used; its configuration is not committed, so two developers will disagree. |
| **No `CONTRIBUTING.md`, no PR checklist, no definition of done** | The rules exist in `CLAUDE.md`/`AGENTS.md` but are addressed to AI agents; a human contributor will not find "run `doctor` and the suite before you push". |
| **No branch strategy** | One branch, no remote, no tags. Nothing states what happens on the second parallel change. |
| **No `CODEOWNERS`, no ownership map** | Nobody knows who reviews the crypto, who reviews the audio callback. |
| **No issue templates** | Notably, no bug template asking for the `doctor` output — which is the single most useful artefact this application produces. |
| **No release checklist or `CHANGELOG.md`** | Phase completion is recorded in prose across several documents; a release gate is not written down as a list. |
| **No `LICENCE` decision** | Deliberate, and recorded as deliberate. It still has to be decided before the code is shared. |
| **Single-person knowledge risk — the largest one** | Every commit is by one author. The reasoning is unusually well externalised into `docs/adr/` and per-phase documents, which reduces the risk far more than most projects manage. But the *tacit* knowledge — why the writer thread waits on `_writer_idle` rather than queue depth, why batching windows changed RTF by 9×, why `region_index` must appear in `validate_transcription` — lives in prose that a new developer has to read and believe. Budget a real onboarding week, and pair the first Phase 5 work rather than assigning it. |

### Onboarding gaps to close first

1. A "your first day" path: clone → `py -3.12 -m venv .venv` → install → `doctor` → `pytest` →
   `audio smoke` → `smoke`. All of it exists; none of it is sequenced in one place.
2. A map of which subsystem to read in which order. `CLAUDE.md` is a rule list, not a tour.
3. An explicit statement of what a new developer may **not** do (touch production, raise
   `CURRENT_PHASE`, edit an applied migration, weaken a test) — currently addressed to AI agents.

---

## 13. Recommended immediate next step

**Fix MOM-BUG-001 before anything else, then run the manual acceptance.**

The reason is sequencing, not severity. `docs/phase-4-manual-acceptance.md` is a good procedure and
the operator is ready to run it — but Part D asks them to transcribe a 3–5 minute recording, which
will hit the 60-second timeout and report a failure that is not the failure they are testing for.
Running acceptance against a known defect produces evidence about the defect. Fixing it first is
roughly a day of work, and the service already exposes everything an asynchronous POST needs
(`TranscriptionHandle`, `/asr/status`, `last_result`, and a page that already polls).

In order:

1. **Fix MOM-BUG-001**, with the regression test that would have caught it (an integration test
   driving `ShellApi` against a running backend).
2. **Run the manual acceptance** (`phase4_acceptance_preflight.ps1`, then Parts C and D), extended
   with two measurements this audit says are missing: capture quality while a transcription runs
   (MOM-RISK-002) and one long meeting (MOM-RISK-010).
3. **In parallel, unblock the two long-lead items** — buy the USB conference microphone, and start
   sourcing a consented or licensed Indonesian evaluation corpus. Neither is engineering work and
   both are on the critical path to Phase 4 closure.
4. **In parallel, stand up CI** (Windows runner, Python 3.12, `pytest` + `compileall` + `pip check`
   + a committed ruff configuration). Do this before the second developer starts, not after.

Full sequencing in [`../roadmap/team-development-roadmap.md`](../roadmap/team-development-roadmap.md);
ready-to-file work items in [`../backlog/team-backlog.md`](../backlog/team-backlog.md).

---

## 14. Documents produced by this audit

| Document | Contents |
|---|---|
| `docs/audit/team-handoff-audit-2026-08-05.md` | This report |
| `docs/audit/feature-completion-matrix.md` | Every capability, Phase 0–12, with status, source, test, verification command and actual result |
| `docs/audit/bug-risk-register.md` | 31 findings with evidence, reproduction, impact and required regression tests |
| `docs/roadmap/team-development-roadmap.md` | Phase 4 closure and Phases 5–12 with entry/exit gates, dependencies and parallelisation |
| `docs/backlog/team-backlog.md` | NOW / NEXT / LATER backlog, ready to move to an issue tracker |

Nothing was committed. No source file, test, migration, configuration file or dependency manifest
was modified.
