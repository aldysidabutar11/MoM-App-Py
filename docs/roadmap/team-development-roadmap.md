# Team development roadmap — MoM-IGD

**Derived from the audit of 2026-08-05, commit `4674ea4`.**
Sequenced by real dependency, not by phase number. Where two phases can genuinely run in parallel,
that is stated; where they cannot, the reason is given.

**No calendar estimates.** Team size and velocity are unknown, so every item carries a relative
size (`S` ≤ 2 days · `M` ≤ 1 week · `L` ≤ 3 weeks · `XL` > 3 weeks, for one competent developer)
and nothing else. Converting those into dates is the team lead's job once the team exists.

---

## 0. The critical path, in one picture

```
                    ┌── buy USB conference microphone ──────────────┐
                    │            (procurement, no engineering)      │
                    │                                               ▼
  MOM-BUG-001 ──► manual acceptance ──► Phase 2 production ──► real-speech
   (fix first)      (functional)         acceptance             accuracy (WER)
                          │                                        │
                          │              ┌── source consented ─────┘
                          │              │   Indonesian corpus
                          ▼              ▼
                    ┌──────────────────────────────┐
                    │  PHASE 4 CLOSURE  → phase 4  │
                    └───────────┬──────────────────┘
                                │
        ┌───────────────────────┼───────────────────────────┐
        ▼                       ▼                           ▼
   Phase 5 diarization    select speaker-           CI / lint / packaging
        │                 embedding model            (parallel throughout)
        ▼                       │
   Phase 6 voice-ID ◄───────────┘
        ▼
   Phase 7 reconciliation
        ▼
   Phase 8 MoM generation ──► Phase 9 review ──► Phase 10 export
        │
        └──────────────► Phase 11 security/packaging/backup ──► Phase 12 pilot
```

Two items are **long-lead and non-engineering**, and both sit on the critical path. Start them on
day one:

* **A USB conference microphone.** Until one exists, no accuracy number is valid as production
  evidence, and `doctor --production` cannot pass.
* **A consented or licensed Indonesian evaluation corpus** with reference transcripts, ≥ 10 minutes
  of it far-field. Producing a reference transcript costs four to six times the audio duration.

---

# Phase 4 closure

**Objective:** turn *"implemented and tested"* into *"validated"*, so `CURRENT_PHASE` can move to
`4` on evidence rather than on completion of coding.

**Entry criteria:** the current state — 2 263 tests green, models provisioned and verified,
preflight `READY`.

**Exit criteria — every one of these, or the phase does not close:**

| # | Gate | Measured how | Currently |
|--:|---|---|---|
| 1 | GUI transcription completes for a ≥ 30-minute recording | Manual acceptance | **Fails — MOM-BUG-001** |
| 2 | Manual functional acceptance returned, all 30 checks | `docs/phase-4-manual-acceptance.md` Part E | Not run |
| 3 | Clean Indonesian WER ≤ 25% | `asr bench --manifest` | `N/A — PENDING` |
| 4 | Far-field WER ≤ 35% | `asr bench --manifest`, USB mic | `N/A — PENDING` |
| 5 | Technical-term recall reported | `asr bench --manifest` | `N/A` |
| 6 | Median and P95 word-timestamp error reported | `asr bench --manifest` | `N/A` |
| 7 | Pass 2 measurably improves the flagged subset | WER on flagged regions, pass 1 vs pass 2 | `N/A` |
| 8 | CPU / RAM / wall-clock on a real ≥ 30-minute meeting | Long-run acceptance test | Longest run on record: **24 s** |
| 9 | Capture quality unaffected by a concurrent transcription | Contention test | Never measured |
| 10 | USB conference microphone calibrated; `doctor --production` passes | `doctor --production` | Hardware absent |
| 11 | Consent text finalised and reviewed | Legal/compliance sign-off | Version `1.0-draft` |
| 12 | Production models provisioned into `D:\MoM-IGD-Data\models` | `asr verify` on the production root | Acceptance root only |
| 13 | Production migration 3 → 5 executed after a rehearsal and a backup | Written plan | No plan, no backup mechanism |
| 14 | Rollback plan written and tested | Restore drill | Neither exists |
| 15 | `CURRENT_PHASE` → `4`, `APP_VERSION` → `0.4.0`, both in one commit | `test_cli.py` asserts they agree | Correctly still `3` / `0.3.0` |

**Deliverables:** the MOM-BUG-001 fix and its regression test · a returned acceptance form · an
accuracy report at `docs/benchmarks/phase-4-accuracy.json` · a long-meeting resource record · a
production migration runbook with rollback · a release commit.

**Risks:** WER may miss target on far-field audio, which is a *hardware* ceiling more than a
software one — the mitigation is microphone placement and the pass-2 budget, not a model change.
The corpus may be slow to source; do not let that block gates 1, 2, 8, 9.

**Effort:** `M` engineering (the bug, the runbook, the tests) + `L` elapsed (procurement, corpus,
legal review, the manual runs themselves).

**Roles:** Desktop UI/API (the bug) · QA/Evaluation (acceptance, WER) · Platform/Data (migration
runbook) · Security/Privacy (consent review) · Audio/ML (contention and long-run measurement).

**Parallelisable:** gates 1–2, 10, 11 and 13–14 are independent of each other. Gates 3–7 all depend
on the corpus and gate 10.

**Phase 4 must not be declared closed while:** any accuracy figure is `N/A`; the only accuracy
evidence came from the internal microphone array; any number was derived from the model's own
output; or the GUI still cannot complete a real-length run.

---

## The manual acceptance tests still required

Procedures for a human to follow. **Do not run any of these against `D:\MoM-IGD-Data`.** All use
the acceptance root `D:\MoM-IGD-Models-Phase4`.

Tests **A1–A11** extend `docs/phase-4-manual-acceptance.md`, which already covers Parts C and D
well; run that document first and use these to fill its gaps. Tests **A12–A21** are the accuracy
set and require the corpus and the USB microphone.

**Common precondition for every test:** `powershell -ExecutionPolicy Bypass -File
.\scripts\phase4_acceptance_preflight.ps1` exits 0 with `READY FOR MANUAL FUNCTIONAL TESTING`.
Record its full output before you start.

**Common cleanup, and it is deliberately minimal:** delete nothing. Every recording, transcript
and revision produced by these tests is evidence. If the acceptance root grows inconveniently
large, archive the whole directory elsewhere and re-run `db init` on a fresh one — never
selectively delete rows or files, and never point cleanup at the production root.

### A1 — Human speech, 60–90 seconds

* **Precondition:** microphone selected, calibration verdict recorded.
* **Steps:** `phase-4-manual-acceptance.md` §C.1 steps 1–7 (speak Indonesian naturally for 60–90 s,
  including the nine listed technical terms, an action item, a date, a time and an amount).
* **Expected:** level meter moves; no dropped frames; recording finalises.
* **Evidence:** recording UUID · `audio verify` output · the calibration verdict.
* **Pass/fail:** PASS if `chunk_count > 0`, `dropped_frames == 0` and the manifest verifies.

### A2 — Pause and resume

* **Steps:** during A1, pause once, wait 5 s, resume, continue 15 s.
* **Expected:** `pause_count = 1`; a `gap` entry with `reason: "paused"` and `intentional: true` in
  `manifest.jsonl`; after transcription, `gap_count ≥ 1` on the working copy row and any region
  overlapping it flagged `overlaps_gap`.
* **Evidence:** the manifest gap entry · the working-copy gap fields.
* **Pass/fail:** PASS if the gap is *recorded in the master* and *filled in the working copy* — both.
  A missing gap record is a FAIL even if the audio sounds fine.

### A3 — Stop and finalisation

* **Expected:** status `RECORDED`, `manifest_status VERIFIED`, `chain_sha256` present, lock file
  released (`temp/recording.lock` absent).
* **Pass/fail:** PASS only if **Verifikasi integritas** reports every chunk verified **and** zero
  database mismatches.

### A4 — Transcribe through the UI

* **Precondition:** **MOM-BUG-001 must be fixed first.** Until then this test can only pass for
  recordings under ~3 minutes and will produce a misleading result for anything longer.
* **Steps:** `§C.2` steps 11–20.
* **Expected:** all six stages appear in order; the pill reaches *Selesai*; the transcript renders
  with timestamps; every line reads `UNASSIGNED`; **no invented speaker name anywhere**.
* **Evidence:** a screenshot of the stage list and the cost card · the reported RTF and peak RSS.
* **Pass/fail:** an invented speaker name is an immediate FAIL and a P0 report.

### A5 — Progress and elapsed time

* **Expected:** the elapsed timer increments once per second throughout; stages appear as they
  complete, not all at the end; the window stays responsive (scroll and click during the run).
* **Pass/fail:** FAIL if the timer freezes or the panel stops updating before the run ends — that
  is the MOM-BUG-001 signature.

### A6 — Transcript is read-only

* **Expected:** no editable field, no save button, no delete control anywhere in the panel.
* **Pass/fail:** FAIL if any transcript text can be modified from the UI. Editing arrives in
  Phase 9 with an audit trail; an unaudited edit now would corrupt the evidence chain.

### A7 — Persistence across a restart

* **Steps:** close the window completely, reopen, select the same recording, press **Muat
  transkrip tersimpan**.
* **Expected:** identical segment count, word count and timestamps.
* **Pass/fail:** PASS only on an exact match, not "looks the same".

### A8 — Cancel

* **Steps:** start a transcription of a 3–5 minute recording; press **Batalkan** mid-stage.
* **Expected:** pill shows *pembatalan diminta*, then a terminal state naming `CANCELLED`; the
  revision is `CANCELLED` and **not** active; no partial transcript is displayed.
* **Evidence:** `asr revisions <uuid>` output.
* **Pass/fail:** FAIL if a cancelled revision is ever active, or if a partial transcript is shown.

### A9 — Retry and checkpoint reuse

* **Steps:** after A8, close and reopen the application, re-run the same recording.
* **Expected:** the stage list says *reused the existing working copy … its SHA-256 still matches*
  and *reused the existing run … same configuration hash*; a new revision is created; exactly one
  revision is active at the end.
* **Pass/fail:** FAIL if either stage re-derives, or if two revisions are active.

### A10 — Resume after a hard kill

* **Steps:** start a transcription; kill `python.exe` from Task Manager mid-run; reopen; re-run.
* **Expected:** the application starts cleanly; the re-run succeeds and produces an active revision.
* **Known issue to record, not to fail on:** the killed run leaves a `BUILDING` transcript row
  for ever (MOM-RISK-003). Note the row count; do not delete it.
* **Pass/fail:** PASS if the application recovers and the re-run succeeds.

### A11 — Recording during transcription *(new — closes MOM-RISK-002)*

* **Precondition:** a ≥ 10-minute recording already captured, so the transcription runs long enough
  to overlap.
* **Steps:** start the transcription; while it is running, start a **new** recording and speak for
  two minutes; stop; verify.
* **Expected:** the recording **is not refused** (this is the deliberate asymmetry). Then measure:
  `dropped_frames`, `queue_high_water_frames`, `xrun_callbacks`, `degraded`, and the calibration
  verdict of the new recording.
* **Evidence:** both recordings' status rows · Task Manager CPU and memory during the overlap.
* **Pass/fail:** PASS if `dropped_frames == 0` and `degraded` is false. **Any** dropped frame is a
  finding that must be filed, even though the queue counted it correctly — it is exactly the risk
  this test exists to measure.

### A12 — Real far-field speech

* **Precondition:** USB conference microphone at the centre of a table, `doctor` reports transport
  `USB`.
* **Steps:** 10 minutes, speakers 1.5–3 m from the microphone, normal room acoustics.
* **Evidence:** microphone model, room dimensions, speaker distances, the calibration verdict.
* **Pass/fail:** this is a *measurement*, not a pass/fail. Record the conditions precisely — an
  accuracy number without them is not evidence.

### A13 — Several speakers

* **Steps:** 4–6 people, ordinary turn-taking, 10 minutes.
* **Expected in Phase 4:** every segment still `UNASSIGNED`. **No speaker names anywhere.**
* **Purpose:** the baseline against which Phase 5 diarization will be measured.
* **Evidence:** a human-written turn log (who spoke when) — this becomes the Phase 5 DER reference,
  so write it now while you remember.

### A14 — Overlapping speech baseline

* **Steps:** deliberately overlap two speakers 5–10 times, marking the timestamps.
* **Expected in Phase 4:** transcription degrades in overlaps; that is expected and is not a defect.
* **Evidence:** the marked overlap timestamps and what the transcript produced there. This is the
  Phase 5 overlap-detection reference.

### A15 — Indonesian technical terminology

* **Steps:** use each of the 41 glossary terms at least once across the corpus.
* **Expected:** correct spellings in `text`; the model's original wording preserved in `text_raw`;
  the glossary correction count reported.
* **Pass/fail:** any term that is *translated, paraphrased or expanded* rather than re-spelled is a
  FAIL — the normaliser is a spelling corrector and nothing more.

### A16 — Mixed Indonesian and English technical terms

* **Steps:** natural code-switching, e.g. *"deployment-nya sudah di-approve"*.
* **Expected:** English technical terms survive; Indonesian morphology around them is not mangled.
* **Evidence:** a list of every code-switched phrase and what came back.

### A17 — WER measurement

* **Precondition:** a manifest validated with
  `asr bench --manifest <path> --validate-only`. Every sample must declare `sha256`,
  `consent_status` (`granted` / `public-licensed` / `synthetic`), `license_name` and `condition`.
* **Steps:** `asr bench --data-dir "D:\MoM-IGD-Models-Phase4" --manifest <path> --out
  docs\benchmarks\phase-4-accuracy.json`.
* **Pass/fail:** clean ≤ 25%, far-field ≤ 35%.
* **Never:** derive WER from the model's own output. Keep audio and reference transcripts **outside
  this repository**.

### A18 — Technical-term recall

* **Steps:** declare `technical_terms` per sample in the manifest.
* **Pass/fail:** report the number; do not set a target before the first measurement exists.
* **Watch for:** substring credit (`"api"` matching inside `"apik"`) — a defect the team already
  found and fixed once. Re-check it on the first real corpus.

### A19 — Word-timestamp accuracy

* **Steps:** supply `word_timestamp_reference_path` for at least one sample.
* **Evidence:** median and P95 absolute error in milliseconds.
* **Why it matters:** Phase 8 proves a quotation by its timespan. An unmeasured timestamp makes
  every later evidence claim unverifiable.

### A20 — Pass 1 versus pass 2

* **Steps:** run the corpus with `pass2_enabled = true`, then with `false` (in `config/local.toml`,
  never in `default.toml`). Compare WER **restricted to the flagged regions**.
* **Pass/fail:** pass 2 must measurably improve the flagged subset. If it does not, its budget is
  being spent for nothing and the beam split must be revisited — `docs/benchmarks.md` already
  records that the split was chosen on throughput evidence alone.

### A21 — Long-meeting resource test

* **Steps:** a real or replayed meeting of ≥ 60 minutes (90+ preferred). Transcribe end to end.
* **Evidence:** wall-clock, RTF, peak worker RSS, peak system RAM, working-copy size, chunk count,
  segment and word row counts, database file growth before and after.
* **Pass/fail:** peak worker RSS < 2.5 GB, total RTF ≤ 1.0, no OOM, no timeout, exactly one active
  revision.
* **Note:** `worker_timeout_seconds` is 10 800 (3 h). A 90-minute meeting at RTF 1.0 would consume
  half of it; record how close you get.

---

# Phase 5 — Anonymous diarization

**Objective:** segment the working copy into anonymous speaker turns and detect overlapping speech.
**No names.** Mapping a cluster to a person is Phase 6, and doing it here would produce a confident
attribution with no voiceprint behind it.

**Dependencies:** Phase 4 closed (a reliable timeline and word timings). Independent of the
speaker-embedding model decision — that blocks Phase 6, not this.

**Entry criteria:** Phase 4 closure gates 1–9 met · a diarization library selected on measured
evidence, the way ADR-0014 selected faster-whisper (`pyannote.audio` and `torch` are still on the
deferred denylist and must be moved off it deliberately) · a benchmark showing CPU-only DER and RTF
on the target device · A13's human turn log available as reference.

**Deliverables:** migration `0006` adding `diarization_runs` and `diarization_turns` (with an
overlap flag) · `mom_igd/diarize/` · a `diarize` worker task · wiring of the existing
`StageSpec("diarize")` · speaker-change and overlap **reason codes** added to
`mom_igd/asr/selection.py` (the rule table was designed for this) · a GUI display of anonymous turns.

**Tests:** deterministic turn boundaries for identical input · overlap regions flagged, never
silently merged · turns aligned to ASR word timings without drift · no name, no participant
reference, no import of `mom_igd.enrollment` anywhere under the new package · worker peak RSS
recorded.

**Acceptance criteria:** DER and JER measured on A13's multi-speaker recording with the production
microphone, at a stated speaker count · overlap detection recall and precision reported · peak
worker RSS < 2.5 GB · total pipeline RTF still ≤ 1.0 with diarization added · a speaker-count hint
accepted but never trusted as ground truth.

**Risks:** *the highest-uncertainty phase in the project.* `docs/architecture.md` already names
CPU diarization as the dominant cost. Far-field single-microphone audio is the real ceiling, and it
is a hardware problem. Mitigation: benchmark before writing production code, exactly as Phase 4A
did — that discipline is why Phase 4 landed inside budget.

**Effort:** `XL`. **Roles:** Audio/ML lead + QA/Evaluation.
**Parallel:** the migration and the schema can be written while the library is benchmarked.

**Must not be declared complete while:** DER is unmeasured; any turn carries a name; overlap
regions are merged away rather than flagged; or the RTF budget is exceeded.

---

# Phase 6 — Voice identification

**Objective:** map anonymous clusters to registered participants, with `UNKNOWN` as a first-class,
safe outcome.

**Dependencies:** Phase 5 (clusters to map) **and** MOM-GAP-002 (a selected, benchmarked,
licence-reviewed speaker-embedding model) **and** real voiceprints, which cannot exist until that
model does. **This is the longest dependency chain in the project — start the model selection now.**

**Entry criteria:** an embedding model in `models/registry.json` with a verified artefact SHA-256
and a reviewed licence · at least one real (non-`DEVELOPMENT_ONLY`) voiceprint enrolled on the USB
microphone · thresholds calibrated on held-out data, never on the enrollment data itself.

**Deliverables:** migration `0007` (`speaker_assignments`) · `mom_igd/voice_id/` · cluster-level
matching with **injective (Hungarian) assignment**, never greedy · a calibrated threshold with
`UNKNOWN` below it · overlap regions **excluded** from matching entirely · a GUI showing name or
`UNKNOWN` with a confidence.

**Tests:** an unregistered speaker is always `UNKNOWN` · one person cannot occupy two clusters ·
overlap segments never yield a confident identity · a below-threshold match yields `UNKNOWN`, never
a best guess · a model change invalidates existing voiceprints (the AAD already enforces this).

**Acceptance criteria:** **zero false-confident assignments** on the evaluation set — this is the
binary gate · identification accuracy and `UNKNOWN` rate reported at a stated speaker count, in the
real room, on the production microphone · thresholds documented with the data they were calibrated
on.

**Risks:** a false-confident identification puts words in someone's mouth in a document that may be
used in a dispute. It is the single most damaging failure mode in the product. The design already
anticipates it (cluster-level matching, injective assignment, overlap exclusion); the discipline
must be kept when the numbers are disappointing.

**Effort:** `L` after the model exists; `XL` including selection and calibration.
**Roles:** Audio/ML + Security/Privacy (biometric handling) + QA/Evaluation.
**Parallel:** model selection can and should start **now**, during Phase 4 closure.

**Must not be declared complete while:** any false-confident assignment exists on the evaluation
set; thresholds are uncalibrated; or the system was measured only on enrollment data.

---

# Phase 7 — Deterministic reconciliation

**Objective:** merge ASR word timings, diarization turns and speaker identity into canonical
utterances, preserving provenance. **No LLM.**

**Dependencies:** Phases 5 and 6.

**Entry criteria:** stable turn and assignment schemas.

**Deliverables:** migration `0008` (`utterances`) · `mom_igd/reconcile/` · wiring of
`StageSpec("reconcile_transcript")` · a provenance link from every utterance back to its segments,
words, turn and assignment.

**Tests:** **byte-identical output for identical input** — the phase's defining property · a word
never belongs to two utterances · an `UNKNOWN` speaker survives reconciliation unchanged · every
utterance resolves to at least one word row.

**Acceptance criteria:** full determinism proven by repeated runs · no utterance without provenance
· no timestamp drift against the master timeline.

**Risks:** low. This is the most tractable phase. **This is the natural point to resolve
MOM-DEBT-001** — if the job state machine is going to own the pipeline, wire it here.

**Effort:** `M`. **Roles:** Platform/Data. **Parallel:** can be designed during Phase 6.

**Must not be declared complete while:** output is not reproducible, or provenance is incomplete.

---

# Phase 8 — Local LLM MoM generation

**Objective:** decisions, action items, PIC, deadlines, issues and risks — every one carrying
evidence. All inference local.

**Dependencies:** Phase 7.

**Entry criteria:** a local LLM selected and benchmarked on the target device (a loopback
`llama-server` is the shape the architecture assumes, and `[providers.endpoints]` already validates
loopback-only URLs) · a constrained output schema · a decision on whether the model can be resident
alongside anything else (measured, not assumed — the ASR pair already breaches the budget together).

**Deliverables:** migration `0009` (`mom_items`, `evidence_links`) · `mom_igd/mom/` · map/reduce
extraction over speaker-aligned windows with grammar-constrained JSON · a **deterministic, non-LLM
verifier** that checks each quote exists verbatim in the cited utterances, that the timespan lies
inside their range, that the PIC is a registered participant or named in the quote, and that the
deadline resolves to an absolute date.

**Tests:** a fabricated utterance reference is discarded outright · a quote that does not appear
verbatim is rejected · an item failing verification is flagged, never silently dropped · the
verifier itself never calls the model.

**Acceptance criteria:** **zero fabricated evidence references** on the evaluation set · precision
and recall for decisions and action items, measured against human-written minutes · every item
traceable to a timespan and a quote · RTF within budget for a 60-minute meeting.

**Risks:** hallucination is the defining risk, and the deterministic verifier is the answer. Do not
let a "the model is usually right" argument weaken it. Resource contention with a 1.9 GB ASR model
is a real constraint — the LLM stage must be the only heavy process alive.

**Effort:** `XL`. **Roles:** Audio/ML (inference) + Platform/Data (verifier, schema) + QA.
**Parallel:** the verifier is independent of the model and can be built and tested first, against
hand-written fixtures. Do that.

**Must not be declared complete while:** any item can reach `APPROVED` with unverified evidence, or
the verifier depends on the model.

---

# Phase 9 — Human review and approval

**Objective:** a reviewer can hear the evidence, fix what is wrong, and approve.

**Dependencies:** Phase 8. **Also depends on resolving MOM-DEBT-001** — this phase needs
`REVIEW_REQUIRED → APPROVED` to actually be driven.

**Entry criteria:** a stable MoM item schema; audio playback over loopback decided (the page must
not reach the filesystem).

**Deliverables:** transcript-audio synchronisation · jump-to-evidence · relabel speaker · resolve
`UNKNOWN` · edit a decision or action · an approval state machine · an **immutable** audit trail of
every review action.

**Tests:** approval is unbypassable from the API, not only the UI · an edit after approval creates a
new revision rather than mutating the approved one · every review action writes an audit event in
the same transaction · a resolved `UNKNOWN` records who resolved it and on what basis.

**Acceptance criteria:** a reviewer completes a 60-minute meeting in a time they consider
reasonable (record it) · the approval gate cannot be bypassed · the audit chain verifies after a
full review session.

**Risks:** this is where usability decides adoption. Budget real user testing, not a demo.

**Effort:** `XL`. **Roles:** Desktop UI/API lead + Platform/Data.
**Parallel:** the approval state machine and the audit surface can be built before the UI.

**Must not be declared complete while:** approval can be bypassed, or a post-approval edit mutates
an approved snapshot.

---

# Phase 10 — Export and action tracking

**Objective:** PDF, DOCX, Markdown and JSON, all offline, plus action tracking across meetings.

**Dependencies:** Phase 9.

**Entry criteria:** an approved-snapshot format that is stable and versioned.

**Deliverables:** four exporters (print-to-PDF via WebView2 keeps the dependency count at zero) ·
versioned snapshots · a visible draft/approved distinction on every export · migration `0010`
(`action_tracking`).

**Tests:** an export of an approved snapshot is byte-reproducible · a draft export is unmistakably
watermarked · no exporter reaches the network · no email, no upload, no share target.

**Acceptance criteria:** all four formats produced offline · an approved snapshot re-exports
identically · nothing leaves the machine automatically.

**Risks:** DOCX without a heavy dependency needs care; a minimal OOXML writer is preferable to
adding a large library to an offline closure.

**Effort:** `L`. **Roles:** Desktop UI/API + Documentation/Release.
**Parallel:** exporters are independent of each other — four people could take one each.

**Must not be declared complete while:** any export path can reach the network, or a draft can be
mistaken for an approved document.

---

# Phase 11 — Security, packaging, backup, recovery

**Objective:** make the application installable, recoverable and compliant.

**Dependencies:** none technically — **and that is the point. Several items here are already
overdue and should be pulled forward.** Backup and key escrow (MOM-RISK-001, MOM-GAP-007) become
urgent the moment the first real voiceprint or real meeting exists, which is Phase 4 closure.

**Entry criteria:** the feature set stable enough that an installer is not rebuilt weekly.

**Deliverables:** a Windows installer (PyInstaller) · a hash-pinned offline wheelhouse · an offline
model bundle · **backup and restore** · **voiceprint key escrow or a documented, accepted
re-enrolment recovery path** · encryption at rest for transcripts and recordings · retention
enforcement · BitLocker guidance · firewall verification · a recovery drill · an air-gapped install
procedure · a DPIA and a security review.

**Tests:** a clean install on an air-gapped machine works · zero egress observed over a full session
· a restore from backup produces a working system · retention deletes what it should and nothing
else · the packaged build excludes the test doubles (MOM-DEBT-008) · **`pyproject.toml` declares the
real runtime dependency set** (MOM-RISK-006 — fix this before the first packaging attempt or the
installer will ship an application that cannot transcribe).

**Acceptance criteria:** air-gapped install verified · restore verified by drill, not by assertion ·
DPIA completed · zero egress measured.

**Risks:** PyInstaller plus CTranslate2, ONNX Runtime and PyAV is not a trivial freeze. Start early
with a spike, not late with a deadline.

**Effort:** `XL`. **Roles:** Documentation/Release Engineering lead + Security/Privacy.
**Pull forward now:** backup/restore, key escrow decision, the `pyproject.toml` fix.

**Must not be declared complete while:** a restore has not been performed from a real backup, or
any egress is observed.

---

# Phase 12 — Evaluation, hardening, pilot

**Objective:** prove the product works in the real world, on real meetings, with consent.

**Dependencies:** Phases 5–11.

**Entry criteria:** written consent from every participant of every pilot meeting · a completed
DPIA · a runbook · a rollback plan.

**Deliverables:** five real meetings end to end · WER · DER/JER · identification accuracy ·
`UNKNOWN` accuracy · MoM precision and recall · a long-meeting test · a resilience set (crash
recovery, device disconnect, disk full, sleep/hibernate, power loss) · user acceptance · a pilot
release.

**Acceptance criteria:** every metric measured and recorded with its conditions · every resilience
scenario survived without data loss · users accept the output as usable minutes.

**Risks:** the pilot is where accumulated small inaccuracies become a usability verdict. Plan for
one full iteration of fixes after it.

**Effort:** `XL`. **Roles:** everyone; QA/Evaluation leads.

**Must not be declared complete while:** any metric is unmeasured, or any resilience scenario loses
data.

---

# Team workstreams

| # | Workstream | Owns | Phases |
|--:|---|---|---|
| 1 | **Platform/Data** | `db/`, `jobs/`, `paths.py`, `config.py`, `audit.py`, migrations, reconciliation, the verifier | 1, 7, 8 (verifier), 9 (state) |
| 2 | **Audio/ML** | `audio/`, `asr/`, diarization, voice-ID, LLM inference, all benchmarking | 2, 4, 5, 6, 8 |
| 3 | **Desktop UI/API** | `api/`, `shell/`, the page, the bridge | 1, 2, 4, 9, 10 |
| 4 | **QA/Evaluation** | the suite, CI, manual acceptance, WER/DER/accuracy, resilience | all |
| 5 | **Security/Privacy/Compliance** | crypto, DPAPI, consent, DPIA, retention, threat model | 3, 6, 11 |
| 6 | **Documentation/Release Engineering** | ADRs, runbooks, packaging, installer, backup, release gates | all, 10, 11 |

### Dependencies between workstreams

* **Audio/ML → everyone.** Diarization DER is the number the whole downstream depends on. Its
  benchmark must land before Phase 5 production code, and before anyone plans around Phase 6.
* **Security/Privacy → Audio/ML.** The embedding model cannot be chosen on accuracy alone; its
  licence and its biometric handling need review first. Sequence the review *before* the benchmark,
  not after.
* **Platform/Data → Desktop UI/API.** Every schema change ripples into the API and the page. Land
  the migration first, then the endpoint, then the panel — the Phase 4 sequence, which worked.
* **QA/Evaluation → everyone.** Nobody self-certifies a phase gate.
* **Documentation/Release → everyone.** An ADR before a decision is implemented, not after.

### Work that must not run concurrently

| Do not overlap | Why |
|---|---|
| Two migrations in flight | Version numbers are contiguous and checksummed. Two branches each adding `0006` conflict irreconcilably once either is applied. **Serialise migration numbers through one owner.** |
| Schema change + API change on the same table | The API mirrors the schema; landing them in parallel guarantees a broken intermediate state. |
| Two people editing `shell/web/app.js` | 2 415 lines, one file, no module system. Split it before parallelising the UI, or serialise the work. |
| Any two benchmarks on the same machine | Already caused four discarded runs and a withdrawn finding. **Benchmarks are strictly sequential on an idle machine.** |
| Phase 5 and Phase 6 pipeline wiring | Phase 6 consumes Phase 5's clusters. Design in parallel, integrate sequentially. |
| Anything at all against `D:\MoM-IGD-Data` | One production root, one owner, one deliberate migration. |
| Diarization + ASR benchmarking | Both saturate 12 threads. Results from a contended machine are not measurements. |

---

# Release strategy

**Versioning.** `APP_VERSION` minor tracks the roadmap phase while pre-1.0 (`0.4.x` = Phase 4).
`pyproject.toml` and `mom_igd/version.py` must agree — a test enforces it. `CURRENT_PHASE` moves
**only** when the phase's acceptance gate is green, never when its code lands. This is the
project's most valuable existing discipline and must survive the handoff.

**Gate for every phase release:**

1. Full suite green, no skips.
2. Coverage recorded, and any drop explained module by module rather than averaged away.
3. `doctor` 0 FAIL on the target root; `doctor --production` 0 FAIL from Phase 4 closure onwards.
4. Every smoke command passes.
5. The phase's measurable acceptance criteria met, with the conditions of measurement recorded.
6. An ADR for every architectural decision taken during the phase.
7. The phase progress document updated with what was actually observed, including defects found and
   what found them.
8. `CURRENT_PHASE` and `APP_VERSION` raised in one commit, last.

**Branching (recommendation).** `main` protected; short-lived `phase-N/<topic>` branches; one PR per
logical change; squash on merge. Tag `v0.N.0` at each phase gate. There is no remote today —
choose the host before the second developer starts.

**Definition of done, per change.** Tests written first where practical and never weakened to pass ·
no new `TODO` · no new phase-crossing import · a docstring that explains *why* where the reason is
not obvious · error messages that name the offending value **and** what would be acceptable ·
`doctor` and the suite run locally before pushing · an ADR if a decision was made.

**Rollback.** There is no database downgrade path, by design. Rollback is: restore the data root
from backup, check out the previous tag. **That mechanism does not exist yet** (MOM-GAP-007) — it
must exist before the production migration to schema 5, which means it must exist before Phase 4
closure gate 13.
