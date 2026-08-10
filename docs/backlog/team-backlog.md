# Team backlog — MoM-IGD

Derived from the audit of 2026-08-05 (commit `4674ea4`). Every item is ready to paste into GitHub
Issues or Jira. Sizes are relative for one competent developer: `S` ≤ 2 days · `M` ≤ 1 week ·
`L` ≤ 3 weeks · `XL` > 3 weeks. **No calendar dates** — team size and velocity are unknown.

Cross-references: [`../audit/bug-risk-register.md`](../audit/bug-risk-register.md) ·
[`../audit/feature-completion-matrix.md`](../audit/feature-completion-matrix.md) ·
[`../roadmap/team-development-roadmap.md`](../roadmap/team-development-roadmap.md).

---

# The first ten issues the team should create

In this order. The first is a defect blocking the primary workflow; the next four are long-lead
items that will otherwise sit idle behind engineering; the rest are the process floor a team needs
before a second person commits.

| # | Issue | Priority | Owner | Size |
|--:|---|---|---|---|
| 1 | **Fix: the shell aborts every transcription longer than 60 seconds** (`NOW-01`) | P1 | Desktop UI/API | M |
| 2 | **Procure a USB conference microphone** (`NOW-02`) | P1 | Product / Ops | S (procurement) |
| 3 | **Source a consented or licensed Indonesian evaluation corpus** (`NOW-03`) | P1 | QA/Evaluation | L (elapsed) |
| 4 | **Legal and compliance review of the biometric consent text; start the DPIA** (`NOW-04`) | P1 | Security/Privacy | M (elapsed) |
| 5 | **Select and benchmark a speaker-embedding model** (`NOW-05`) | P1 | Audio/ML + Security | L |
| 6 | **Stand up CI on a Windows runner** (`NOW-06`) | P2 | QA/Evaluation | S |
| 7 | **Commit a lint and type-check configuration** (`NOW-07`) | P2 | Platform/Data | S |
| 8 | **Run the Phase 4 manual functional acceptance** (`NOW-08`) | P1 | QA/Evaluation | M |
| 9 | **Fix: the acceptance preflight's production-root guard is bypassable** (`NOW-09`) | P2 | Doc/Release | S |
| 10 | **Design decision: does Phase 4 join the job state machine?** (`NOW-10`) | P2 | Platform/Data | S (decision) |

Items 2, 3, 4 and 5 are **not engineering work** and are on the critical path. Create them on day
one so they run in the background.

---

# NOW — before anything else, or in parallel with it

## NOW-01 · Fix: the shell aborts every transcription longer than 60 seconds

* **Priority:** P1 · **Type:** Bug · **Finding:** MOM-BUG-001
* **Rationale:** `ShellApi._send` uses a hard 60-second `urlopen` timeout while
  `POST /asr/transcribe` runs the whole pipeline synchronously. Reproduced during the audit: at
  60.0 s the bridge returns `{'ok': False, 'status': 0, 'error': 'TimeoutError: timed out'}`, the
  page prints *Gagal* and stops polling, while the server completes the run and writes a correct
  transcript. Measured RTF 0.31 means the ceiling is reached at roughly 190 seconds of audio; the
  project's own acceptance procedure (Part D, 3–5 minutes) will hit it. The primary Phase 4
  workflow does not work for a real meeting.
* **Scope:** make `POST /asr/transcribe` return `202 Accepted` immediately, running the pipeline on
  a background thread; let `app.js` drive the UI to its terminal state from the existing
  `/asr/status` poll (the service already exposes `TranscriptionHandle`, `last_result` and
  `last_error`, and the page already polls every 1200 ms); keep `AsrService.transcribe()`
  synchronous for the CLI; report a run that ended while the panel was closed on the next poll.
* **Out of scope:** changing `_PROXY_TIMEOUT_S` as the fix (no value is both long enough for a
  three-hour meeting and short enough to detect a dead backend); a progress bar; any pipeline change.
* **Acceptance criteria:** a 10-minute recording transcribes to completion through the GUI, with
  stages appearing as they finish and the elapsed timer running throughout · the panel reaches
  *Selesai* and renders the transcript without a manual refresh · a second press during a run still
  returns 409 · cancel still lands at the next boundary · the CLI path is unchanged.
* **Dependency:** none. **Owner:** Desktop UI/API. **Size:** M.
* **Required tests:** (1) an integration test that starts the real backend, points a real
  `ShellApi` at it, and asserts a run outliving `_PROXY_TIMEOUT_S` still reaches a terminal state;
  (2) a unit test that `POST /asr/transcribe` returns without waiting for the pipeline; (3) a static
  assertion that no allowlisted POST is designed to block longer than the proxy timeout.

## NOW-02 · Procure a USB conference microphone

* **Priority:** P1 · **Type:** Task (procurement) · **Finding:** MOM-GAP-004
* **Rationale:** the internal Intel Smart Sound array applies beamforming that suppresses speakers
  not facing the laptop, and it gets worse with room size. Every accuracy number produced on it is
  invalid as production evidence. `doctor --production` cannot pass without a USB device Windows
  reports as such, and Phase 2 production acceptance is gated on it.
* **Scope:** one omnidirectional USB conference microphone suitable for a table centre; verify
  Windows reports transport `USB` via `mom_igd audio devices`; record the model in
  `docs/benchmarks.md`.
* **Out of scope:** an array, a mixer, or any driver installation.
* **Acceptance criteria:** `mom_igd audio devices` lists it with transport `USB` and
  `transport_verified: true` · `doctor --production` reports `usb_conference_microphone` PASS.
* **Dependency:** none. **Owner:** Product/Ops. **Size:** S.
* **Required tests:** none (hardware); record the `audio devices` output as evidence.

## NOW-03 · Source a consented or licensed Indonesian evaluation corpus

* **Priority:** P1 · **Type:** Task · **Finding:** MOM-GAP-001
* **Rationale:** accuracy has never been measured. WER needs a reference transcript, and a
  reference transcript needs consent and licence metadata. This is the gate that keeps
  `CURRENT_PHASE` at `3`, and it has the longest lead time of anything in the project — producing a
  reference transcript costs four to six times the audio duration.
* **Scope:** ≥ 30 minutes total, of which ≥ 10 minutes far-field, recorded on the USB microphone in
  a real room; human-written reference transcripts; a manifest following
  `docs/examples/asr-evaluation-manifest.example.json` with per-sample `sha256`, `consent_status`,
  `license_name`, `condition` and `technical_terms`; ideally one sample with
  `word_timestamp_reference_path`. Priority order: a locally licensed corpus with transcripts · a
  small clearly-licensed public Indonesian subset · in-house meeting audio with recorded consent.
* **Out of scope:** storing any audio or transcript inside this repository — ever.
* **Acceptance criteria:** `asr bench --manifest <path> --validate-only` passes with every checksum
  verified and every sample declaring consent.
* **Dependency:** NOW-02 for the far-field portion. **Owner:** QA/Evaluation. **Size:** L (elapsed).
* **Required tests:** the existing manifest loader already enforces checksums and the consent gate.

## NOW-04 · Legal and compliance review of the consent text; start the DPIA

* **Priority:** P1 · **Type:** Task (compliance) · **Finding:** MOM-GAP-005
* **Rationale:** `doctor` reports the consent text as version `1.0-draft`, unreviewed. Voiceprints
  are *data pribadi bersifat spesifik* under UU PDP No. 27/2022, requiring recorded explicit
  consent, a purpose limit, a retention period and a right to erasure. No real person may be
  enrolled until this is signed off.
* **Scope:** review and finalise the wording in `mom_igd/enrollment/consent.py`; decide and document
  the retention period; document the erasure path (revoke-and-delete exists and works); begin the
  DPIA.
* **Out of scope:** implementing retention enforcement (Phase 11) — but the *policy* must be decided
  here so Phase 11 has something to implement.
* **Acceptance criteria:** consent version is no longer `-draft` · `doctor`'s `consent_text` check
  passes · the retention period is written down · the DPIA is started with an owner and a date.
* **Dependency:** none. **Owner:** Security/Privacy/Compliance. **Size:** M (elapsed).
* **Required tests:** update the consent-version assertions in `tests/test_participants_consent.py`.

## NOW-05 · Select and benchmark a speaker-embedding model

* **Priority:** P1 · **Type:** Task · **Finding:** MOM-GAP-002
* **Rationale:** `models/registry.json` declares zero models and **no real voiceprint has ever been
  produced by this build**. Phase 3 is functionally blocked and Phase 6 cannot start. This has the
  longest dependency chain in the project.
* **Scope:** shortlist candidates meeting the constraints in
  `docs/phase-3-speaker-model-selection.md` §2 (CPU-only, no CUDA, fits the memory budget, licence
  permits internal commercial use); review licences **before** benchmarking; benchmark
  intra/inter-speaker separation on the target device; record the artefact SHA-256; write an ADR;
  populate `models/registry.json`.
* **Out of scope:** implementing Phase 6 matching; enrolling real people before NOW-04 completes.
* **Acceptance criteria:** an ADR records the decision and its evidence · the registry declares the
  model with a verified SHA-256 and a reviewed licence · `doctor`'s `speaker_embedding_model` check
  passes · one real (non-`DEVELOPMENT_ONLY`) voiceprint is produced on the USB microphone.
* **Dependency:** NOW-02 (production-eligible enrollment needs a USB device); NOW-04 before any real
  person is enrolled. **Owner:** Audio/ML + Security/Privacy. **Size:** L.
* **Required tests:** an integration test for the real provider behind the existing
  `SpeakerEmbeddingProvider` contract; the fake-provider tests stay as they are.

## NOW-06 · Stand up CI on a Windows runner

* **Priority:** P2 · **Type:** Infrastructure · **Finding:** MOM-DEBT-003
* **Rationale:** a 10-minute suite that nothing triggers will drift within weeks of a second
  developer joining. No CI configuration exists anywhere in the repository.
* **Scope:** Windows runner, Python 3.12; `pip install -r requirements.txt -r requirements-dev.txt`;
  `python -m compileall -q mom_igd tests`; `python -m pip check`; `python -m pytest -q --cov=mom_igd`;
  `python -m mom_igd doctor` (expect 0 FAIL); `python -m mom_igd audio smoke`;
  `python -m mom_igd smoke`. Publish the coverage summary. **No network, no model download, no
  microphone** — the suite already forbids all three.
* **Out of scope:** running `asr smoke`, `asr verify` or `asr bench` in CI (they need provisioned
  models); any deployment step.
* **Acceptance criteria:** CI runs on every push and pull request · it fails on a test failure, a
  compile error, a `pip check` break or a `doctor` FAIL · the run completes in under 20 minutes.
* **Dependency:** a git host must be chosen (there is no remote today). **Owner:** QA/Evaluation.
  **Size:** S.
* **Required tests:** the pipeline is the test; add a deliberately failing branch once to prove it
  goes red.

## NOW-07 · Commit a lint and type-check configuration

* **Priority:** P2 · **Type:** Infrastructure · **Finding:** MOM-DEBT-003
* **Rationale:** the code carries `# noqa: BLE001`, `# noqa: S310` and `PLC0415` markers, so a
  linter was used — but its configuration is not committed and nothing runs it. Two developers will
  immediately disagree about style, and the existing suppressions will look arbitrary.
* **Scope:** add `[tool.ruff]` to `pyproject.toml` with the rule set that produces the existing
  `noqa` codes and a line length matching the current code; add `[tool.mypy]` (or pyright) in
  non-strict mode over `mom_igd/` only; wire both into NOW-06; add a `.pre-commit-config.yaml`.
* **Out of scope:** fixing every finding in one pass — record a baseline and ratchet.
* **Acceptance criteria:** `ruff check mom_igd tests` is clean or has an explicit, committed
  baseline · the type checker runs and its current error count is recorded · both run in CI.
* **Dependency:** NOW-06. **Owner:** Platform/Data. **Size:** S.
* **Required tests:** none beyond the CI gate.

## NOW-08 · Run the Phase 4 manual functional acceptance

* **Priority:** P1 · **Type:** Task (QA) · **Finding:** MOM-GAP-006
* **Rationale:** the machinery is verified; a human voice is not, and nothing in the repository can
  supply one. The procedure exists and is good.
* **Scope:** `scripts\phase4_acceptance_preflight.ps1`, then `docs/phase-4-manual-acceptance.md`
  Parts C and D, plus the additional tests A11 (recording during transcription) and A21 (long
  meeting) from the roadmap. Return Part E's 30-row result form.
* **Out of scope:** any accuracy claim (that is NOW-03 + LATER); anything against
  `D:\MoM-IGD-Data`.
* **Acceptance criteria:** the form is returned with an honest answer in every row · every failure
  is filed as an issue · the wall-clock, RTF and peak RSS for each run are recorded.
* **Dependency:** **NOW-01 must land first** — otherwise Part D produces evidence about a known
  defect. **Owner:** QA/Evaluation (operator). **Size:** M.
* **Required tests:** file a regression test for every defect the run finds.

## NOW-09 · Fix: the acceptance preflight's production-root guard is bypassable

* **Priority:** P2 · **Type:** Bug · **Finding:** MOM-BUG-002
* **Rationale:** the guard is a string comparison. `D:/MoM-IGD-Data`, `D:\MoM-IGD-Data\.` and
  `D:\.\MoM-IGD-Data` all pass it and all resolve to the production root inside Python. Every
  command the script then runs is read-only, so nothing is destroyed — but the production database
  is opened and the operator is told production is `READY`. A guard advertised as absolute, and
  backed by 65 tests, must actually be absolute.
* **Scope:** normalise both sides with `[System.IO.Path]::GetFullPath()` before comparing; also
  refuse a target that contains, or is contained by, the production root.
* **Out of scope:** changing what the script checks or making it write anything.
* **Acceptance criteria:** all six spellings in MOM-BUG-002's reproduction table exit 2 with
  `REFUSED`.
* **Dependency:** none. **Owner:** Documentation/Release Engineering. **Size:** S.
* **Required tests:** extend `tests/test_acceptance_preflight_script.py` with a data-driven case
  list covering forward slashes, `\.`, `\.\`, `..\` round-trips and an extended-length path; assert
  the guard uses a path-normalising call rather than a bare string compare.

## NOW-10 · Decision: does Phase 4 join the job state machine?

* **Priority:** P2 · **Type:** Decision (ADR) · **Finding:** MOM-DEBT-001
* **Rationale:** `jobs`/`job_stages` are created by the capture service and then never advanced.
  The ASR pipeline keeps its state in `transcripts.status` and passes `job_id=None` everywhere.
  Verified in the production database: both jobs sit at `RECORDED`, all 20 stage rows untouched.
  Two written invariants say otherwise — CLAUDE.md's *"`jobs` is the single owner of workflow
  state"* and the architecture document's *"api → DB sole writer of `jobs`/`job_stages` state"*.
  Phase 9 (approval) and Phase 10 (approved snapshots) both assume that lifecycle exists. Decide
  now, cheaply, rather than discovering it in Phase 9.
* **Scope:** an ADR choosing one of: (a) wire Phase 4 into the machine — `QUEUED → PROCESSING →
  REVIEW_REQUIRED`, `set_stage_status` per stage, `save_checkpoint` at each boundary; or (b) narrow
  the invariant so `transcripts.status` is authoritative for transcription and the job machine
  begins at Phase 7. Update CLAUDE.md and `docs/architecture.md` to match whichever is chosen.
* **Out of scope:** the implementation, if (a) is chosen — that is a follow-up sized against Phase 7.
* **Acceptance criteria:** an ADR exists · the two documents agree with the code · a test asserts
  the chosen invariant so it cannot drift again.
* **Dependency:** none. **Owner:** Platform/Data. **Size:** S (decision), M–L (implementation if (a)).
* **Required tests:** whichever invariant is chosen, assert it.

---

# NEXT — after Phase 4 closure, before Phase 5 production code

## NEXT-01 · Add stale-transcript recovery and a `doctor` check

P2 · Bug/Feature · MOM-RISK-003 · Owner Platform/Data · Size S

A killed run leaves a `BUILDING` transcript row for ever. There is no sweeper, no `doctor` check
and no CLI command — in contrast with the audio side, which has both `audio_stale_recordings` and
`audio recover`. **Scope:** reconcile `BUILDING` rows older than a threshold at startup (mark them
`FAILED` with a reason, never delete); add a `doctor` check that names a remedy that actually
works — the project has been bitten once by a warning whose remedy did nothing. **Out of scope:**
resuming a killed run. **Acceptance:** after a hard kill, `doctor` reports the stale revision and
the named remedy resolves it. **Tests:** a killed-run fixture; a test that the remedy changes the
reported state.

## NEXT-02 · Re-check free disk inside `transcribe()`, not only in `preflight()`

P2 · Bug · MOM-RISK-004 · Owner Audio/ML · Size S

The GUI is safe (the button is disabled until preflight passes) but the CLI and a direct API call
are not. A working copy is ~115 MB/hour and is written first. **Scope:** move the 2 GB check into
`AsrService.transcribe()` and raise a typed error; keep it in `preflight()` too, as advice.
**Acceptance:** a run that cannot fit is refused with a message naming the required and available
space. **Tests:** a monkeypatched `shutil.disk_usage` asserting refusal from both the CLI and the
API.

## NEXT-03 · Do not drain the queue from the caller while the writer thread is alive

P2 · Bug · MOM-LIKELY-001 · Owner Audio/ML · Size S

`_await_queue_drain` falls through to `_drain_into_writer()` when its 10-second deadline expires,
even though that function's own docstring says it is *"only safe once the writer thread has
exited"*. Two threads then feed one `ChunkWriter`; block writes are serialised by `_writer_lock`
but their **order** is not, so PCM could be silently reordered inside a chunk that still verifies.
**Scope:** on the deadline path, escalate (signal stop, join the writer, then drain) or record a
gap and abandon the remainder — the latter matches the phase's never-fabricate rule better.
**Acceptance:** `stop()` never calls `_drain_into_writer()` while `writer_alive()` is true.
**Tests:** a `ChunkWriter` whose `write()` blocks on a controllable event.

## NEXT-04 · Harden the single-recording lock against PID reuse

P2 · Bug/Feature · MOM-RISK-005 · Owner Audio/ML · Size S

`_owner_alive` trusts `psutil.pid_exists(pid)`. A recycled PID makes a stale lock read as live and
the operator cannot record, with no documented remedy. **Scope:** record the holder's process start
time (and optionally a boot id) in the lock file and require both to match; add a documented way to
clear a lock the operator knows is stale. **Acceptance:** a lock whose PID has been reused by an
unrelated process is treated as stale and cleared, with a log line saying so. **Tests:** a
fabricated lock file naming a live PID with a mismatched start time.

## NEXT-05 · Declare the real runtime dependencies in `pyproject.toml`

P2 · Bug (packaging) · MOM-RISK-006 · Owner Doc/Release · Size S

`pyproject.toml` lists six dependencies and stops at `cryptography`; its comment still says
faster-whisper, ctranslate2, av and numpy *"must NOT appear yet"*. `requirements.txt` correctly
pins eight direct dependencies including faster-whisper and av, and `offline_policy.py` records
that they left the deferred list in Phase 4. Anything built from `pyproject.toml` — including the
Phase 11 PyInstaller work — produces an application that cannot transcribe. **Scope:** update the
dependency list and the comment; keep `requirements.txt` as the pinned truth. **Acceptance:**
`pip install .` into a clean venv yields an interpreter that can run `asr smoke`. **Tests:** extend
the existing pinning test to assert that every `pyproject` direct dependency appears in
`requirements.txt` and vice versa.

## NEXT-06 · Backup and restore for the runtime data root

P1 · Feature · MOM-GAP-007, MOM-RISK-007 · Owner Doc/Release + Platform/Data · Size M

`<data_root>/backups` is created and never written. The migrator states that recovery is
"restore from `<data_root>/backups`" — a mechanism that does not exist. This becomes urgent at the
production migration to schema 5, and again at the first real recording. **Scope:** a `backup`
command producing a consistent copy of the database (including WAL), the manifests and the key
store; a `restore` command; a documented drill. **Out of scope:** encryption at rest and retention
enforcement (Phase 11 proper). **Acceptance:** a restore into an empty root produces a system that
passes `db verify` and `doctor`. **Tests:** backup/restore round-trip on a temporary root,
including a backup taken while a WAL checkpoint is pending.

## NEXT-07 · Decide the voiceprint key recovery story

P1 · Decision (ADR) · MOM-RISK-001 · Owner Security/Privacy · Size S (decision)

DPAPI binds the master key to one Windows user on one machine, and `KeyProtector.load()` correctly
refuses to mint a replacement. There is no escrow and no export, so a profile rebuild destroys
every voiceprint permanently. Today the exposure is nil (0 voiceprints in production); it must be
resolved before the first real enrollment. **Scope:** an ADR choosing escrow, an accepted
re-enrolment recovery path, or both — and saying so in the consent text if re-enrolment is the
answer. **Acceptance:** an ADR exists and the consent wording matches it. **Tests:** whichever
mechanism is chosen.

## NEXT-08 · Write and rehearse the production migration 3 → 5

P2 · Task · MOM-GAP-008 · Owner Platform/Data · Size S

Production is at `user_version = 3` with `integrity_check ok` and a clean `foreign_key_check`; it
holds 2 meetings, 2 recordings, 5 chunks and 26 audit events. Migrations 0004 and 0005 are additive
and transactional, but the migration has never been rehearsed and there is nothing to roll back to.
**Scope:** a runbook — take a backup (NEXT-06), rehearse against a copy, verify `db verify` and
`doctor` afterwards, and state explicitly that migration 0004 backfills both existing meetings with
capacity **9** from the column DEFAULT. **Acceptance:** a rehearsal on a copy succeeds and the
runbook is reviewed. **Tests:** a test that migrates a schema-3 fixture to head and asserts the
backfilled capacity.

## NEXT-09 · Raise `cli.py` test coverage

P2 · Test · MOM-DEBT-002 · Owner QA/Evaluation · Size M

`cli.py` is 62% — 347 of 1 002 statements missed, the largest uncovered production surface, larger
than `provision.py` and `faster_whisper_provider.py` combined. The missing ranges include the
`asr transcribe`, `asr transcript` and `asr revisions` handlers, and `asr transcribe` is both the
documented primary verification command and the workaround for MOM-BUG-001. The Phase 4 coverage
reconciliation does not mention this module at all. **Scope:** a harness over `main(argv)` covering
argument parsing, exit codes and output formatting for every subcommand, with the service layer
stubbed at its boundary. **Out of scope:** anything needing a real model. **Acceptance:** `cli.py`
≥ 85%; the coverage document updated to explain whatever remains. **Tests:** the issue is the test.

## NEXT-10 · An integration test that drives `ShellApi` against a running backend

P2 · Test · MOM-DEBT-004 · Owner Desktop UI/API · Size S

Every GUI test is a static string assertion against `index.html`, `app.js` and `app.css`. They are
valuable — the `[hidden]` cascade test encodes a real past defect — but they cannot fail on a
runtime problem, and MOM-BUG-001 is the proof. **Scope:** a `slow`-marked test that starts the real
backend, constructs a real `ShellApi`, and exercises every allowlisted path for status codes,
envelope shape and the absence of the token and of any filesystem path in the response. **Acceptance:**
the test fails if an allowlisted path 404s, if a path leaks, or if the token appears anywhere.
**Tests:** the issue is the test.

## NEXT-11 · Correct the stale capability booleans

P3 · Bug · MOM-RISK-008 · Owner Desktop UI/API · Size S

`/internal/ready` returns `audio_capture: False, asr: False` with a comment saying *"Phase 1
implements none of these"*; `/audio/recordings/status` returns `capabilities.transcript: False`.
Both are wrong. **Scope:** derive the booleans from `CURRENT_PHASE` or from an explicit capability
table rather than hardcoding. **Acceptance:** the reported capabilities match what the build can do.
**Tests:** assert the capability block against the phase marker.

## NEXT-12 · Documentation corrections

P3 · Docs · MOM-DEBT-005 · Owner Doc/Release · Size S

`docs/architecture.md:218` puts `asr_pass2_selective` in Phase 5 (code says 4);
`docs/architecture.md:416-424`'s recording-lifecycle diagram lists five states that do not exist in
`RecordingLifecycle`; `AGENTS.md:87` says migrations `0001`–`0004` are applied (`0005` is);
`docs/phase-4-progress.md` §4 omits `cli.py` from the coverage reconciliation. **Acceptance:** each
corrected against the code. **Tests:** consider a docs test that reads `PIPELINE_STAGES` and
`RecordingLifecycle` and asserts the documented tables match — the project already does this kind
of thing for the ASR/capture state lists.

## NEXT-13 · Contribution and governance files

P3 · Docs · MOM-DEBT-009 · Owner Doc/Release · Size S

Absent: `CONTRIBUTING.md`, `CHANGELOG.md`, `CODEOWNERS`, issue and PR templates, `.editorconfig`,
and a licence decision (`LICENSE` is deliberately absent and recorded as deliberate — it still has
to be decided before the code is shared). **Scope:** a `CONTRIBUTING.md` addressed to humans
(`CLAUDE.md` and `AGENTS.md` are addressed to AI agents), a PR checklist matching the release gate,
a bug template that **requires the `doctor` output**, `CODEOWNERS` per workstream, and a licence
decision. **Acceptance:** a new developer can find the rules without reading `CLAUDE.md`.

## NEXT-14 · Replace the deprecated Starlette 422 constant

P3 · Cleanup · MOM-DEBT-006 · Owner Desktop UI/API · Size S

`mom_igd/api/enrollment_routes.py:344` and `:508` use `HTTP_422_UNPROCESSABLE_ENTITY`;
`asr_routes.py` already uses `HTTP_422_UNPROCESSABLE_CONTENT`. Three of the suite's 14 warnings come
from our own code. **Acceptance:** no `StarletteDeprecationWarning` originates from `mom_igd`.

## NEXT-15 · Rename the misleading variable in the pass-2 skip message

P3 · Cleanup · MOM-DEBT-007 · Owner Audio/ML · Size S

`mom_igd/asr/pipeline.py:860` — `longest = min(...)`. The output text is correct; the name is not.
Worth fixing precisely because the surrounding code is otherwise unusually careful.

---

# LATER — Phase 5 onwards, and everything gated on a closed Phase 4

## LATER-01 · Phase 5: anonymous diarization and overlap detection

P1 · Epic · Owner Audio/ML · Size XL · Depends: Phase 4 closure

Benchmark first, exactly as Phase 4A did — CPU-only DER and RTF on the target device before any
production code. Deliverables: migration `0006`, `mom_igd/diarize/`, the `diarize` worker task,
speaker-change and overlap reason codes added to the existing pass-2 selection table, and an
anonymous-turn display. **Acceptance:** DER/JER measured at a stated speaker count on the
production microphone; overlap recall and precision reported; peak worker RSS < 2.5 GB; total RTF
still ≤ 1.0. **Never:** a name anywhere in this phase.

## LATER-02 · Phase 6: voice identification with strict `UNKNOWN`

P1 · Epic · Owner Audio/ML + Security/Privacy · Size L–XL · Depends: LATER-01, NOW-05, real voiceprints

Cluster-level matching, injective (Hungarian) assignment, calibrated thresholds, overlap regions
excluded from matching entirely. **Acceptance: zero false-confident assignments** — a binary gate,
because a wrong attribution puts words in someone's mouth in a document that may be used in a
dispute.

## LATER-03 · Phase 7: deterministic transcript reconciliation

P1 · Epic · Owner Platform/Data · Size M · Depends: LATER-01, LATER-02

Canonical utterances from words + turns + identity, with full provenance and **no LLM**.
**Acceptance:** byte-identical output for identical input. Resolve NOW-10's decision here if the
answer was (a).

## LATER-04 · Phase 8: local LLM MoM generation with a deterministic verifier

P1 · Epic · Owner Audio/ML + Platform/Data · Size XL · Depends: LATER-03

Build the **verifier first**, against hand-written fixtures, before any model is chosen — it is
independent of the model and it is the thing that makes the phase safe. **Acceptance:** zero
fabricated evidence references; precision and recall for decisions and actions against human
minutes; the verifier never calls the model.

## LATER-05 · Phase 9: human review and approval

P1 · Epic · Owner Desktop UI/API · Size XL · Depends: LATER-04, NOW-10

Transcript-audio synchronisation, jump-to-evidence, relabel speaker, resolve `UNKNOWN`, edit
decisions and actions, an approval state machine and an immutable audit trail. **Acceptance:** the
approval gate is unbypassable from the API, not merely the UI.

## LATER-06 · Phase 10: exports and action tracking

P2 · Epic · Owner Desktop UI/API + Doc/Release · Size L · Depends: LATER-05

PDF, DOCX, Markdown, JSON — all offline, all reproducible for an approved snapshot, draft
unmistakably watermarked, nothing sent anywhere automatically. The four exporters are independent
and parallelisable.

## LATER-07 · Phase 11: packaging, encryption at rest, retention, air-gapped install

P1 · Epic · Owner Doc/Release + Security/Privacy · Size XL

Windows installer (PyInstaller), hash-pinned offline wheelhouse, offline model bundle, encryption
at rest for transcripts and recordings, retention enforcement, BitLocker guidance, firewall
verification, a recovery drill, an air-gapped install procedure, the DPIA and a security review.
**Pull NEXT-05, NEXT-06 and NEXT-07 out of this epic and do them earlier** — they are needed at
Phase 4 closure. Start the PyInstaller freeze as a spike early: CTranslate2 + ONNX Runtime + PyAV
is not a trivial one. Strip the test doubles from the packaged build (MOM-DEBT-008).

## LATER-08 · Phase 12: evaluation, hardening and pilot

P1 · Epic · Owner everyone, QA leads · Size XL · Depends: LATER-01…07

Five real consented meetings end to end; WER, DER/JER, identification accuracy, `UNKNOWN` accuracy,
MoM precision and recall; a long-meeting run; the resilience set (crash recovery, device
disconnect, disk full, sleep/hibernate, power loss); user acceptance; the pilot release. Plan one
full iteration of fixes after the pilot, not zero.

## LATER-09 · Split `shell/web/app.js`

P3 · Refactor · Owner Desktop UI/API · Size M

2 415 lines in one file with no module system. It is well organised into IIFE blocks today, but two
people cannot work on the UI concurrently, and Phase 9's review screen is the largest UI work in the
project. Split before parallelising, not during. Keep the no-npm, no-framework, no-CDN constraint.

## LATER-10 · Measure and, if necessary, manage transcription priority

P2 · Feature · MOM-RISK-002 · Owner Audio/ML · Size S–M · Depends: NOW-08 test A11

If A11 shows any dropped frame or a `degraded` recording while a transcription runs, add priority
management (lower the worker's Windows priority class, or reduce `cpu_threads` while a capture is
live). Do **not** implement it speculatively — measure first. The asymmetry (a recording is never
refused) must be preserved whatever the answer.

## LATER-11 · Long-meeting scalability work

P2 · Task · MOM-RISK-010 · Owner Audio/ML + Platform/Data · Size M · Depends: NOW-08 test A21

Once a ≥ 60-minute run has been measured, address whatever it exposes: region payload size across
the process boundary, `transcript_words` growth, SQLite growth, the cost of re-hashing a ~345 MB
working copy on every re-run, and how close the run gets to `worker_timeout_seconds`.

---

# Backlog hygiene notes for whoever owns this list

* **Do not close a `NOW` item because the code landed.** Six of the ten are gated on evidence — an
  acceptance form, a measurement, a signed-off document. Landing code is not the acceptance
  criterion for any of them.
* **Serialise migration numbers through one owner.** Two branches each adding `0006` cannot both be
  merged once either has been applied to a database.
* **Benchmarks are strictly sequential on an idle machine.** Concurrent runs have already produced
  four failed runs and one withdrawn finding in this project's history.
* **Never point anything at `D:\MoM-IGD-Data`** except the one deliberate, backed-up migration in
  NEXT-08.
* **A phase is not closed until `CURRENT_PHASE` moves, and `CURRENT_PHASE` moves only on evidence.**
  That discipline is the most valuable thing this repository has; protect it through the handoff.
