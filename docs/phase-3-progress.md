# Phase 3 progress — participants, biometric consent, secure voice enrollment

**This file is the recovery checkpoint.** A session can stop at any moment; whoever
picks the work up reads this file first, then `git status --short`, then re-runs the
suite. Update it after every completed section, not at the end.

---

## 1. Baseline (verified before any edit)

| | |
|---|---|
| Repository | `D:\Aldy\MoM-IGD` |
| Runtime data | `D:\MoM-IGD-Data` |
| Baseline commit | **`4a9518d`** — `Phase 2: offline audio capture` |
| Previous commit | `b2a089c` — `Phase 1: application foundation` |
| Working tree at start | **clean** (`git status --short` empty) |
| `git diff --check` | clean |
| Baseline suite | **840 passed**, 0 failed, exit 0 |
| Baseline `doctor` | **16 PASS / 6 WARN / 0 FAIL**, exit 0 |
| Python | 3.12.10, `.venv` |
| DB schema at start | 2 (`0001_initial`, `0002_audio_capture` applied) |

### Doctor count reconciliation

The Phase 2 handoff recorded `17 PASS / 5 WARN`. This machine reports
`16 PASS / 6 WARN`. The difference is **`ram`**, which flipped to WARN because
Docker/WSL held 1958 MB at audit time, leaving 1671 MB free against a 2048 MB
threshold. It is a transient machine condition, not a regression: `0 FAIL` either
way, and no other check changed.

### Migration immutability evidence

Recorded here so any later drift is provable. `0001` and `0002` must never change.

| Migration | Bytes | SHA-256 (raw == LF-normalised) | Git blob at `4a9518d` |
|---|--:|---|---|
| `0001_initial.sql` | 14 219 | `f1426fa94b8ae90e4c0b646c0f132ac4a483525165675c947649cad124e89796` | `8c702839206ed48f1d2468247c6cdd89909d3702` |
| `0002_audio_capture.sql` | 15 018 | `8d42086530a4560d28ca5cfd2707b5402c1b2872fea02a49ce3768106f570ded` | `fc9a056b737b16b1ed42d088bcdce1883e4a28cf` |

Re-verify with:

```powershell
git diff --exit-code 4a9518d -- mom_igd\db\migrations\0001_initial.sql
git diff --exit-code 4a9518d -- mom_igd\db\migrations\0002_audio_capture.sql
```

### Real runtime data — do not touch

`D:\MoM-IGD-Data` holds the operator's real Phase 2 evidence and must not be
migrated, deleted, moved or overwritten during development:

* `db\mom_igd.db` — 163 840 bytes, schema 2
* 2 meetings, 5 finalised `.wav` chunks, 0 partials

Every automated test and every verification command uses a **temporary data root**.
Migrating the real database happens only later, manually, by the operator, after a
backup — see the manual handoff section of
[`phase-3-participants-enrollment.md`](phase-3-participants-enrollment.md).

---

## 2. Audit findings that shape the design

Read from the existing schema and code before designing anything.

| Finding | Consequence |
|---|---|
| `participants` carries `UNIQUE INDEX ux_participants_display_name` | **Conflicts with the Phase 3 rule that duplicate names are allowed.** `0003` must `DROP INDEX` it (SQLite can drop an index without a table rebuild). |
| `participants` has **no `uuid` column** | Add one and backfill, reusing the `meetings.uuid` pattern from `0002` (`randomblob` v4 UUID, then a unique index). |
| `PARTICIPANT` is already an allowed `audit_events.category` | No category migration needed. `CONSENT_*`, `ENROLLMENT_*` and `VOICEPRINT_*` are *actions* inside existing categories. |
| `RUNTIME_SUBDIRS` has no `voiceprints` or `keys` entry | Extend `mom_igd/paths.py`. Never build these paths by hand elsewhere. |
| `cryptography` **not installed**; `pywin32` **not installed**; `ctypes` available | DPAPI through `ctypes` + `crypt32.dll` → **no new dependency for key protection**. AES-256-GCM needs `cryptography` (pre-approved in the Phase 3 brief, exact pin). |
| `0002` documents the FK-cascade hazard when rebuilding a parent table | Reuse that ordering if any table must be rebuilt: the *new* child must reference the *new* parent, or `DROP TABLE` cascades away freshly copied rows. |
| `meetings` deliberately has no state column; `jobs` owns workflow state | Do not add participant state to `meetings`. |
| Phase 2 `RecordingService` holds a `SingleRecordingLock` (`temp/recording.lock`) | Enrollment must take **the same** lock, so a meeting recording and an enrollment can never open the microphone together. |

---

## 3. Architecture decisions taken

Recorded as they are made, so a resumed session does not re-litigate them.

1. **DPAPI via `ctypes`, not `pywin32`.** `CryptProtectData` / `CryptUnprotectData`
   in `crypt32.dll` are reachable from the standard library. Adding `pywin32` for
   two calls would grow the dependency closure for nothing.
2. **Two-layer encryption.** A random 256-bit master key is protected by DPAPI
   (`CRYPTPROTECT_UI_FORBIDDEN`, current-user scope); voiceprint payloads are sealed
   with AES-256-GCM under that key. Per-blob DPAPI was rejected: it gives no
   versioned envelope, no explicit nonce and no caller-controlled AAD.
3. **`cryptography` is the only new runtime dependency**, pinned exactly. It ships
   prebuilt wheels, needs no compiler, and makes no network call.
4. **Participant identity is the UUID.** `display_name` is descriptive only: not a
   key, not a path component, not a voiceprint identifier, and **not unique**.
5. **Enrollment reuses the Phase 2 capture path** — same backend, same device
   fingerprint rule, same preflight and calibration evidence, same
   `SingleRecordingLock`. No second hardware path.
6. **Enrollment audio never reaches disk.** Bounded in-memory capture only; a
   hard cap on total bytes prevents unbounded growth.

---

## 4. Checklist

Status: `[ ]` todo · `[~]` in progress · `[x]` done

* [x] Recovery audit (HEAD, clean tree, 840 tests, doctor 0 FAIL, migration hashes)
* [x] `docs/phase-3-progress.md` created
* [x] `cryptography==49.0.0` pinned, installed into `.venv`, lock files + offline policy updated
* [x] `mom_igd/paths.py`: `voiceprints` and `keys` subdirectories + `voiceprint_path()`
* [x] Migration `0003_participants_consent_voiceprints.sql` (schema head → 3)
* [x] Phase 1/2 boundary tests converted to Phase 3 reality (suite 843 green)
* [x] `KeyProtector` (DPAPI via `ctypes`) + `FakeKeyProtector` for tests
* [x] `VoiceprintCipher` (AES-256-GCM envelope, canonical AAD)
* [x] Participant service (lifecycle, 9-per-meeting transactional cap)
* [x] Consent service (append-only events, grant / revoke / re-grant)
* [x] Consent text v1 (Bahasa Indonesia, `1.0-draft`, SHA-256 recorded)
* [x] Tests: crypto (35), participants + consent (36)
* [x] `VoiceprintStore` — crash-consistent save, verify, revocation deletion, recovery (35 tests)
* [x] `SpeakerEmbeddingProvider` contract + output validation + `load_provider_from_registry` (fails closed)
* [x] Fake providers (deterministic / drifting / broken) — test-only, three barriers against production use
* [x] `quality.py` — enrollment quality gates (PASS/WARN/REJECT/NOT_MEASURED)
* [x] Tests: store (35), provider + quality (54)
* [x] `service.py` — 11-state enrollment machine, capture mutual exclusion, fake e2e
* [x] Phase 2 latent stale-lock defect found and fixed (+2 regression tests)
* [x] Tests: enrollment service (79) incl. full fake end-to-end flow
* [x] API routes (`mom_igd/api/enrollment_routes.py`) — 21 routes, registered, 80 tests
* [x] `EnrollmentCaptureController` — Python-side capture, no browser audio (29 tests)
* [x] Shell allowlist extended (exact + anchored UUID templates, PATCH/DELETE added)
* [x] `SingleRecordingLock.read_live_holder()` — one public home for the staleness rule
* [x] Desktop UI (participant panel, consent dialog, revoke dialog, enrollment wizard)
* [x] Diagnostics (`enrollment_checks.py`) + CLI `participant` group
* [x] Revocation + voiceprint deletion (incl. the pending-cleanup failure path)
* [x] `docs/phase-3-participants-enrollment.md`, `docs/phase-3-speaker-model-selection.md`
* [x] ADR-0009 … ADR-0012
* [x] `README.md`, `docs/architecture.md`, `CLAUDE.md`, `AGENTS.md` updated
* [x] Version bump to `0.3.0` / phase `3`
* [x] Session 5 gate: hygiene defect found and fixed, all scans green
* [x] **Session 6 corrective:** revoke-dialog cascade defect fixed (§7b), mutation-proven tests
* [x] **Session 6 corrective:** per-meeting roster capacity, migration `0004`, ADR-0013
* [ ] **Manual click test of the revoke dialog and the roster capacity control**
      ← the only outstanding item, and it needs the operator (§9a/§9b of the Phase 3 doc)
* [ ] Single local Phase 3 commit — awaiting explicit permission

---

## 5. Files changed so far

| File | Change |
|---|---|
| `docs/phase-3-progress.md` | new — this checkpoint |
| `requirements.txt` | + `cryptography==49.0.0` (direct count 5 → 6), rationale documented |
| `mom_igd/offline_policy.py` | `cryptography` explicitly allowed; `onnxruntime` stays deferred; NumPy note extended to embeddings |
| `mom_igd/paths.py` | + `voiceprints`, `keys` subdirs; `voiceprints_dir`, `keys_dir`, `voiceprint_path()`, `_UUID_RE` |
| `mom_igd/version.py` | `SCHEMA_VERSION_HEAD` 2 → 3 |
| `mom_igd/db/migrations/0003_participants_consent_voiceprints.sql` | new — 4 tables, participant UUID, unique-name index dropped |
| `mom_igd/enrollment/__init__.py` | new — package contract and invariants |
| `mom_igd/enrollment/keys.py` | new — DPAPI `KeyProtector`, `FakeKeyProtector`, `MasterKey` |
| `mom_igd/enrollment/cipher.py` | new — `VoiceprintCipher`, `ModelIdentity`, canonical AAD, atomic write |
| `tests/test_paths.py` | subdirectory set advanced to include `voiceprints`/`keys` |
| `tests/test_db.py` | table set → Phase 3; **name-uniqueness test inverted** per ADR-0009; index-reversal guard added |
| `tests/test_migration_0002.py` | three migrations; 0002 immutability test added; `0003_broken` fixture renumbered to `0004_broken` |
| `mom_igd/enrollment/consent.py` | new — consent text v1.0-draft, hash, append-only `ConsentService` |
| `mom_igd/enrollment/participants.py` | new — `ParticipantService`, transactional 9-per-meeting cap |
| `tests/test_voiceprint_crypto.py` | new — 35 tests (key lifecycle, envelope, rejection matrix) |
| `tests/test_participants_consent.py` | new — 36 tests (registry, membership race, consent lifecycle) |
| `tests/test_cli.py` | module boundary advanced to Phase 4; enrollment AI/identification guard added |
| `mom_igd/db/migrations/0003_…sql` | `PENDING` → `PENDING_WRITE`; that status now *requires* path+hash so recovery has something to check |
| `mom_igd/enrollment/store.py` | new — crash-consistent `VoiceprintStore` |
| `mom_igd/enrollment/provider.py` | new — `SpeakerModelSpec`, output validation, artefact hash check, `load_provider_from_registry` |
| `mom_igd/enrollment/fake_provider.py` | new — deterministic / drifting / broken test doubles |
| `tests/test_voiceprint_store.py` | new — 35 tests incl. every crash scenario |
| `mom_igd/enrollment/quality.py` | new — gates; energy-relative speech ratio, optional SNR |
| `mom_igd/enrollment/service.py` | new — 11-state machine, shared capture lock, fake e2e |
| `mom_igd/audio/service.py` | **Phase 2 fix** — preflight now ignores a stale lock holder |
| `tests/test_audio_service.py` | +2 Phase 2 regression tests for the stale-lock fix |
| `tests/test_enrollment_provider_quality.py` | new — 54 tests |
| `tests/test_enrollment_service.py` | new — 79 tests incl. the full fake end-to-end flow |
| `mom_igd/api/enrollment_routes.py` | new — 21 protected routes, exhaustive reason-code mapping |
| `mom_igd/api/app.py` | router registered; shutdown closes capture controller + session |
| `mom_igd/enrollment/capture.py` | new — Python-side capture, bounded buffer, no browser audio |
| `mom_igd/enrollment/fake_provider.py` | + `StableSpeakerFakeProvider` (models same-voice similarity) |
| `mom_igd/shell/launcher.py` | Phase 3 allowlist; `_permitted()`; `api_patch`/`api_delete`; templated UUID patterns |
| `tests/test_enrollment_api.py` | new — 80 tests (auth, mapping, leakage, app isolation) |
| `tests/test_enrollment_capture.py` | new — 29 tests (bounded buffer, cleanup, lock interaction) |
| `tests/test_static_ui.py` | allowlist advanced to Phase 3; wildcard + query-smuggling guards added |
| `tests/test_audio_api_ui.py` | audio allowlist assertion relaxed to subset, with an origin check |
| `mom_igd/shell/web/index.html`, `app.css`, `app.js` | participant panel, consent and revoke dialogs, `.state-badge`; no `innerHTML`, no browser audio API |
| `mom_igd/cli.py` | `participant` group; typed-phrase confirmation for grant (`SAYA SETUJU`) and revoke (`CABUT`) |
| `mom_igd/diagnostics/enrollment_checks.py` | new — 9 checks, read-only `mode=ro` connection |
| `mom_igd/diagnostics/doctor.py` | enrollment checks registered |
| `tests/test_participants_ui.py` | new — 38 tests (no `innerHTML`, no browser audio, no token in JS) |
| `tests/test_enrollment_cli_doctor.py` | new — 34 tests |
| `mom_igd/version.py` | `APP_VERSION` → `0.3.0`, `CURRENT_PHASE` → `3` |
| `pyproject.toml` | version `0.3.0`; `cryptography>=45,<50` |
| `README.md`, `docs/architecture.md`, `CLAUDE.md`, `AGENTS.md` | phase statements and hard boundaries advanced to Phase 3 |
| `docs/adr/0009`–`0012` | new — identity/consent, encryption, storage lifecycle, capture boundary |
| `docs/phase-3-participants-enrollment.md`, `docs/phase-3-speaker-model-selection.md` | new |
| `.gitignore` | **Session 5 fix** — `/keys/`, `*.vpx`, `*.vpx.tmp`, `*.dpapi` (see section 7a) |
| `mom_igd/shell/web/app.css` | **Session 6 fix** — `[hidden] { display: none !important }`; `.capacity-row` |
| `mom_igd/shell/web/app.js` | **Session 6** — revoke dialog owns its state; roster capacity control; dialog buttons removed from `setActionsDisabled` |
| `mom_igd/shell/web/index.html` | **Session 6** — name/role in the revoke dialog, conditional deletion wording, roster card, stale "sembilan" copy corrected |
| `mom_igd/db/migrations/0004_meeting_participant_capacity.sql` | new — per-meeting capacity, `DEFAULT 9`, `CHECK >= 1` |
| `config/default.toml`, `mom_igd/config.py` | new `[participants]` section; `config_schema_version` 2 → 3 |
| `mom_igd/enrollment/participants.py` | capacity read from the meeting row; `meetings()`, `set_meeting_capacity()`, `capacity_policy()`; the fixed-nine constant removed |
| `mom_igd/api/enrollment_routes.py` | `GET /meetings`, `GET /meetings/{uuid}/roster`, `PATCH /meetings/{uuid}/capacity` (`StrictInt`) |
| `mom_igd/shell/launcher.py` | allowlist: `/enrollment/meetings`, `.../roster`, `.../capacity` |
| `mom_igd/diagnostics/enrollment_checks.py` | production target scales to the largest configured roster |
| `mom_igd/version.py` | `SCHEMA_VERSION_HEAD` 3 → 4, `CONFIG_SCHEMA_VERSION` 2 → 3 |
| `tests/test_revoke_modal.py` | new — 42 tests, mutation-proven |
| `tests/test_participants_capacity.py` | new — 59 tests |
| `tests/test_migration_0004.py` | new — 18 tests |
| `docs/adr/0013-per-meeting-roster-capacity.md` | new |
| `.gitattributes` | **Session 5 fix** — `*.vpx`, `*.dpapi` marked `binary` |
| `tests/test_repo_hygiene.py` | **Session 5 fix** — +7 tests covering the real Phase 3 artefact names |

### Session-2 verification (before any edit)

| | |
|---|---|
| HEAD | `4a9518d` unchanged, no checkpoint commit |
| Working tree | matched the documented 13 entries exactly, no foreign changes |
| Suite | 915 passed |
| `pip check` | clean |
| Migration hashes | `0001`/`0002` byte-identical to the recorded values |
| Runtime DB | untouched, still schema 2, 5 `.wav`, 0 partials, no `voiceprints`/`keys` dirs |

**RAM after the operator closed Docker/WSL: 4176 MB available, up from 1671 MB
(+2505 MB).** The `ram` check flipped WARN → PASS; Docker/WSL residency fell from
21 processes / 1958 MB to 10 / 406 MB. Nothing was started or stopped by this
session.

**`doctor` now reports 1 FAIL — and it is correct.** `SCHEMA_VERSION_HEAD` is 3
while the real database is still at 2, so the `database` check reports a pending
migration. That is the honest consequence of the instruction *not* to auto-migrate
`D:\MoM-IGD-Data`. Proof that the code is healthy: against a **migrated temporary**
data root the same build reports **17 PASS / 5 WARN / 0 FAIL, exit 0**. The FAIL
clears when the operator runs the manual migration in the handoff.

### Bug found in `_supersede_live` during review

The failure branch nulled `envelope_relative_path` even when the unlink had
**failed**, which would have stranded the leftover ciphertext: `DELETE_PENDING`
would have had no pointer left, so `retry_pending_cleanup()` could never remove the
file and nothing would record that it still existed. Now the pointer is cleared only
on a successful delete, and a test asserts the pointer survives a failed one.

### Design flaw found and fixed in `quality.py`

A healthy 10-second tone was **rejected** with a 0 % speech ratio and 0 dB SNR. The
cause was mine: both estimators were relative to the Phase 2 noise floor, which is a
low percentile of per-block RMS — so for a *constant-level* signal the floor equals
the signal and both measures degenerate.

Two honest fixes rather than a threshold nudge:

* `estimate_speech_active_ratio` is now **energy-relative**: a window counts as
  active if it sits within `SPEECH_DYNAMIC_RANGE_DB` (20 dB) of the sample's own
  overall RMS. Speech pauses fall well below that; a steady tone does not.
* `estimate_snr_db` returns **`None`** when the floor-to-RMS gap is under 1 dB, and
  the gate reports `NOT_MEASURED`. Reporting 0 dB would flunk a healthy sample;
  inventing a figure would be worse.

Both behaviours are pinned by tests
(`test_a_steady_signal_reports_snr_as_unmeasurable_not_zero`,
`test_the_speech_ratio_is_energy_relative_not_floor_relative`).

### Provider boundary: three barriers against a fake reaching production

1. `load_provider_from_registry` — the only production entry point — always raises
   `ModelUnavailableError`, naming the model-selection document.
2. `is_test_double = True`, checked by the service before accepting a provider.
3. Name prefixed `FAKE-` and SHA-256 of all `f`, so a template built with it is
   identifiable in the registry, an audit event and an envelope.

A test also greps `provider.py` for any reference to the fake module, so a future
"temporary fallback" cannot be slipped in.

**Consumer-side validation is authoritative.** `BrokenFakeProvider` returns
deliberately invalid vectors *without* self-validating, because a real third-party
provider cannot be trusted to police itself. All five modes — wrong dimension, NaN,
infinity, zero vector, unnormalised — are rejected by `validate_embedding`.

### Session 3: latent Phase 2 defect found while wiring enrollment

`RecordingService.preflight()` read the capture-lock holder **without** the
staleness check, and preflight runs *before* `acquire()`. So a lock left by a killed
process failed preflight forever and **permanently prevented recording** — exactly
the failure `SingleRecordingLock._owner_alive` was written to avoid, defeated by the
ordering. Two-line fix in `audio/service.py`, plus two regression tests
(`test_a_stale_lock_does_not_block_preflight`, and a companion asserting a *live*
holder still blocks, so the fix cannot be mistaken for a weakened guard).

This was found only because enrollment shares the same lock. It is a Phase 2 bug,
fixed in Phase 2 code, with Phase 2 tests.

### Session 4: recovery of the interrupted tail, and four more bugs

The two files written just before the previous limit (`enrollment_routes.py`,
`capture.py`) were **complete and compiled** — the tail was not truncated. But
verification found four real defects:

1. **The router was never registered.** `enrollment_router` existed and was dead
   code; `app.include_router` had not been called. 21 routes now register (verified
   through the OpenAPI schema, not by inspecting `app.routes` — this FastAPI version
   wraps included routers as `_IncludedRouter`, so the naive check reads zero).
2. **Shutdown did not close the capture controller.** A crash mid-sample would have
   left the shared capture lock held, blocking the next meeting recording. Lifespan
   now aborts capture and abandons the session. Enrollment is *abandoned* rather than
   finalised, unlike a recording: there is no partial voiceprint worth keeping, and
   completing one unattended would store biometric data nobody watched being created.
3. **A concurrent second `capture_sample` was queued, not refused.** The controller
   held one lock for the whole seven-second capture, so a second call waited and then
   recorded an extra sample the operator never asked for — and a Cancel from the UI
   thread would have blocked on it too. Restructured: a non-blocking claim guards the
   slot, the capture itself runs outside any lock, and `abort()` deliberately takes
   no lock so Cancel takes effect immediately.
4. **The fake provider could not exercise the consistency gate.** It derives its
   vector from the audio bytes, so five genuinely different recordings of one voice
   produce unrelated vectors (measured cosine −0.21) and the gate correctly rejected
   the end-to-end flow. Feeding byte-identical audio would have tested nothing, so
   `StableSpeakerFakeProvider` was added: a fixed per-speaker direction plus a small
   audio-dependent perturbation, which is the property a real model has. The 0.80
   floor is unchanged and `DriftingFakeProvider` still proves rejection works.

Also corrected: `capture.py` counted a driver overflow as a *dropped-frame count*.
The driver does not report how many frames it discarded, so that number was invented.
It now counts an xrun only — the gate rejects on any xrun, so the sample is refused
either way without a fabricated figure.

### Session 3: two bugs in my own enrollment code

1. **`CAPTURING → EMBEDDING` was missing from the transition table.** `finalize()`
   runs from `CAPTURING`, so every end-to-end path failed. The specified flow is
   capture → quality → embedding; the table was wrong, not the flow.
2. **The lock pre-check used `self._capture_lock.held`.** The lock object is *shared*
   with `RecordingService`, so `held` is `True` whenever this process owns it —
   including when it owns it for a meeting recording, which is exactly the case to
   refuse. Enrollment could therefore start on top of a live recording. Now the
   *holder identity* is checked, with staleness delegated to the Phase 2 logic.
   `test_a_live_lock_holder_is_respected_even_in_the_same_process` pins it.

### Design note recorded while writing the store

A `PENDING_WRITE` row recovered with a **provably correct** envelope becomes
`RE_ENROLL_REQUIRED`, not `ACTIVE`. The bytes are trustworthy, but the enrollment
that produced them never finished, so its quality verdict and production
eligibility were never established — and inventing them would be exactly the kind
of fabricated evidence this project refuses.

### Verified by probe (not yet converted into suite tests)

`scratchpad/probe_0003.py` and `scratchpad/probe_crypto.py` confirmed:

* fresh install → schema 3; upgrade 2 → 3 preserves every Phase 2 row; UUID
  backfilled; duplicate display names accepted; 0 FK violations; idempotent.
* key: `load()` never creates; `create_if_missing` idempotent; `repr` redacted; key
  file holds no plaintext material.
* envelope: no plaintext payload; fresh nonce per seal; round trip exact.
* rejected — wrong participant, wrong voiceprint UUID, wrong model, tampered
  ciphertext, truncated ciphertext, truncated envelope, unknown schema, wrong
  master key, and **A's envelope presented as B's record**.
* the AEAD tag rejects a wrong key even with the `key_id` header stripped, so
  security does not rest on a plaintext field.

**Ported into `tests/test_voiceprint_crypto.py` (35 tests) and
`tests/test_participants_consent.py` (36 tests).** The probe scripts are now
redundant; the suite carries the evidence.

### Bug found by probing the real code path

`KeyProtector.create_if_missing` reopened the key file `"rb"` and called
``os.fsync`` on a read-only descriptor, which fails on Windows with
``OSError: [Errno 9] Bad file descriptor``. **No test caught it** because
`FakeKeyProtector` overrides `create_if_missing` wholesale, so the real atomic-write
path had zero coverage. Fixed to write and fsync through one writable handle, and
three tests now exercise the real writer with DPAPI stubbed reversibly
(`test_the_real_protector_writes_and_reloads_its_key_file` and neighbours).

The real DPAPI path was then verified end to end on this machine in a temporary
directory: key created (787-byte file, no plaintext material), unwrap round trip
exact, seal+open under the real key, and a tampered protected blob fails closed
rather than minting a replacement.

### Additional Phase 1/2 boundary conversions

| Test | Change |
|---|---|
| `test_no_phase_3_or_later_module_was_created` | → `test_no_phase_4_or_later_module_was_created`; `enrollment` moved from forbidden to allowed; `voiceprint`/`consent` entries removed (now implemented inside `enrollment`); `speaker` stays forbidden (Phase 6 identification) |
| *(new)* `test_phase_3_enrollment_contains_no_identification_or_ai_runtime` | forbids `torch`/`onnxruntime`/`numpy`/… imports and `identify_speaker`/`match_speaker`/`cluster_speakers` inside `mom_igd/enrollment` |

---

## 6. Last test run

| | |
|---|---|
| Command | `.\.venv\Scripts\python.exe -m pytest --cov=mom_igd --cov-report=term-missing -q` |
| Result | **1498 passed**, 0 failed, 0 skipped, exit 0 |
| Coverage | **90 % branch** — baseline held (8 762 statements, 1 992 branches) |
| Compile | `compileall mom_igd tests` clean |
| Dependencies | `pip check` clean |
| When | Session 7, after the final corrective pass |

Progression: 840 at `4a9518d` → 915 (s1) → 1004 (s2) → 1085 (s3) → 1269 (s4) →
1276 (s5) → 1423 (s6) → **1498** (s7). Net **+658** tests, no regression at any point. Session 5
added seven from the repository-hygiene gap; Session 6 added 119 from the corrective
pass (revoke dialog 42, roster capacity 59, migration 0004 18) plus reconciliations.
`enrollment/service.py` 87 % branch, `audio/service.py` held at 90 % after the
stale-lock fix.

**420** of those tests live in the nine new Phase 3 files: crypto 35, participants
and consent 36, store 35, provider and quality 54, service 79, API 80, capture 29,
UI 38, CLI and doctor 34. The remaining +16 are Phase 1/2 files advanced to the
Phase 3 boundary, plus the 7 hygiene tests.

Per-module branch coverage of the new package: `consent` 98 %, `quality` 96 %,
`fake_provider` 96 %, `participants` 94 %, `provider` 91 %, `store` 89 %,
`cipher` 88 %, `keys` 68 %. `keys.py` is the low one **by design**: the real DPAPI
`_protect`/`_unprotect` bodies cannot run in CI on a non-Windows host, so they are
stubbed reversibly in three tests and were additionally verified live on this
machine (see below). Do not chase that number by weakening the stub.

---

## 7. Blockers

| Blocker | Effect | Resolution |
|---|---|---|
| No speaker embedding model provisioned; `models/registry.json` declares 0 models | Real embeddings impossible; the fake provider covers tests only | Evaluate candidates in `phase-3-speaker-model-selection.md`, then **ask the operator** before downloading any artefact |
| No verified USB conference microphone | Any enrollment on this machine is `DEVELOPMENT_ONLY` | Operator supplies the microphone |
| Consent text not reviewed by the organisation | `doctor --production` must FAIL honestly on this | Organisational/legal review, then an explicit config flag |

Because of the first blocker the best achievable status this phase is
**`PHASE 3 CORE PASS — REAL EMBEDDING MODEL PROVISIONING PENDING`**.

---

## 7a. Session 5: the final gate, and the last bug it found

Session 5 re-verified `capture.py` and the API routes (written just before a
session limit, therefore untrusted), then ran the whole Phase 3 gate.

### Repository hygiene: the two Phase 3 secrets were committable

The gate asked `git check-ignore` about the *actual* Phase 3 filenames rather than
trusting the section heading in `.gitignore`, and found:

| Path | Before | Cause |
|---|---|---|
| `voiceprints/x.vpx` | ignored | matched by the anchored `/voiceprints/` |
| `mom_igd/enrollment/leaked.vpx` | **committable** | no `*.vpx` rule existed |
| `some/deep/path/x.vpx` | **committable** | same |
| `keys/voiceprint_master.dpapi` | **committable** | `*.key` does not match `.dpapi`, and `/keys/` was absent |
| `mom_igd/leaked.dpapi` | **committable** | same |

The Phase 2 hazard block had established exactly this defence-in-depth pattern for
audio; the Phase 3 artefact names were simply never added to it. The `.emb`
placeholder in the hygiene test gave false confidence, because nothing the code
actually writes is named `.emb`.

Fixed in three layers:

1. `.gitignore` — added `/keys/`, `*.vpx`, `*.vpx.tmp`, `*.dpapi`. The extension
   rules stay **unanchored** on purpose: a sealed envelope copied elsewhere while
   debugging must also be ignored.
2. `.gitattributes` — `*.vpx` and `*.dpapi` marked `binary`. `* text=auto` would
   otherwise let Git guess, and CRLF translation on AES-GCM ciphertext or a DPAPI
   blob corrupts it silently. This is the layer that makes a mistaken `git add -f`
   produce a byte-exact file instead of an unopenable one.
3. `tests/test_repo_hygiene.py` — the real filenames added to the parametrised
   leak paths, `.vpx`/`.dpapi`/`.emb`/`.embedding`/`.voiceprint` added to the
   forbidden-suffix sweep, `keys` added to the anchored-directory check, and a new
   test for the two `.gitattributes` rules. **+7 tests.**

Verified after the fix: all six leak paths ignored, and `mom_igd/enrollment/*.py`,
`mom_igd/audio/service.py` and `mom_igd/paths.py` still tracked — the new
unanchored rules shadow no source file.

### Gates run in Session 5

| Gate | Result |
|---|---|
| Full suite + branch coverage | 1276 passed, 90 % |
| Upgrade `2 → 3` on a **copy** of the real database | PASS — 2 meetings, 5 chunk rows byte-identical, 0 FK violations, original left at schema 2 |
| Fresh migration to head | `0001` → `0002` → `0003`, schema 3 of 3 |
| Migration immutability vs `4a9518d` | `0001`, `0002` byte-identical |
| Import side-effect audit | 56 modules imported with `RawInputStream`, `socket.connect`, `getaddrinfo`, `urlopen`, `mkdir`, write-mode `open` and `crypt32` all poisoned → **0 violations**, and a non-existent data root was **not** created |
| AST network audit | 0 banned modules anywhere (no `torch`, `numpy`, `pickle`, `audioop`, `requests`, `httpx`, cloud SDK); the enrollment package imports **no** network module; only `smoke.py` and `shell/launcher.py` import `urllib.request`, both loopback |
| Shell allowlist rejection matrix | 15 templated patterns, all anchored, lower-case-UUID-only, wildcard-free; 10 hostile paths refused for GET and POST including `/openapi.json`, `/docs`, `..` traversal, a query string and an upper-case UUID |
| Phase 3 fake enrollment end-to-end | **31/31 steps** on a temporary data root |
| `doctor` on the **real** data root | 19 PASS / 11 WARN / 2 FAIL, exit 1 — the two FAILs are the un-applied migration, which is correct: the operator migrates, not the agent. A before/after hash snapshot of all 24 entries proved `doctor` changed **nothing** and created no key |
| `git diff --check` | clean |

### What the end-to-end gate proved, and its one substitution

Consent refused before grant (sole blocker `CONSENT_MISSING`) → grant → five 10 s
deterministic samples → `COMPLETED` with 40.39 s of measured speech → one sealed
envelope named by UUID with no plaintext name in its bytes → verified from disk →
revocation deleted the envelope and left the row `REVOKED` → re-grant and
re-enroll produced exactly one live voiceprint again.

Two designed behaviours worth recording, because both look like failures at first:

* `finalize()` **rejected** three 6 s samples. `min_total_speech_seconds` is 30 and
  they supplied ~14 s. That is the gate working.
* The voiceprint was stored **`DEVELOPMENT_ONLY`**, never `ACTIVE`, with
  `production_eligible = false`, because the embedding came from a declared test
  double. This is the third barrier from section 3 firing in a real run, and it is
  the concrete reason the phase status is `CORE PASS` and not `DEVELOPMENT PASS`.

The gate cannot verify embedding *quality* — a stand-in that returns a stable
vector per speaker proves the plumbing, never that real voices separate. That is
the residual risk the pending-model blocker names.

---

## 7b. Session 6: the corrective pass

Two things drove this session: a GUI bug found by clicking, and a product-requirement
change. They are unrelated, and both are fixed here.

### The revoke dialog was dead — one CSS rule, five broken elements

**Symptom.** The *Cabut persetujuan?* modal appeared, but "Ya, cabut persetujuan" and
"Batal" did nothing, and the modal showed **no participant name**.

**Root cause, proved rather than guessed.** `app.css` declared no `[hidden]` rule.
The HTML `hidden` attribute works *only* through the UA stylesheet's
`[hidden] { display: none }`, and **any** author-level `display` beats the UA
stylesheet regardless of specificity. `.modal-backdrop { display: flex }` therefore
made `show(node, false)` a no-op. Computed from the shipped files, **five** elements
could never be hidden:

| Element | Class that broke it |
|---|---|
| `#consent-backdrop` | `.modal-backdrop` → `display: flex` |
| `#revoke-backdrop` | `.modal-backdrop` → `display: flex` |
| `#enrollment-card` | `.status-card` → `display: flex` |
| `#participant-form-card` | `.status-card` → `display: flex` |
| `#voiceprint-card` | `.status-card` → `display: flex` |

`#revoke-backdrop` is **last in the document** at the same `z-index: 50`, so it
painted on top of the whole application and swallowed every pointer event from page
load. That single fact explains every symptom exactly:

1. The dialog was on screen without ever having been *opened*, so
   `openRevokeDialog()` had never run → `#revoke-who` was empty and
   `pendingRevokeUuid` was `null`. **That is the missing participant name.**
2. Clicking confirm *did* fire the handler; `confirmRevoke()` then hit
   `if (!pendingRevokeUuid) return` and sent nothing. Diagnostic class **2**: the
   click enters, no request is sent.
3. Clicking Batal ran `closeRevokeDialog()`, which set `hidden` — which the cascade
   ignored. The modal stayed. Diagnostic class **3**: handler runs, UI does not
   update.

**The fix, in three parts.**

1. `[hidden] { display: none !important }` at the top of `app.css`. `!important` is
   deliberate: without it `[hidden]` (0,1,0) merely *ties* with `.modal-backdrop`
   (0,1,0) and source order decides, so correctness would depend on where the next
   `display` rule gets added.
2. The dialog now owns its own state (`revokeTarget`, `revokeSubmitting`) instead of
   sharing the global `busy` flag, and **the two dialog confirm buttons were removed
   from `setActionsDisabled()`**. That list is blanket-reset to `false` in `once()`'s
   `finally`, which is a second latent instance of the same class of bug: any
   unrelated action left "Saya setuju" clickable with the consent box unticked.
3. Identity, focus, Escape, double-submit and the honest missing-voiceprint message,
   per the requirement.

**The tests are mutation-proven.** Substring assertions would have passed throughout
the original bug — every handler *was* attached and every id *did* match. So
`tests/test_revoke_modal.py` asserts structure and semantics computed from the files,
and a harness reintroduced **12** defects one at a time (removing the `[hidden]` rule,
dropping the in-flight guard, putting `revokeConfirm` back in `setActionsDisabled`,
removing the focus restore, …). **12/12 were caught**, and the assets were restored
byte-identical, verified by SHA-256.

### Roster capacity: nine is now a default, not a limit

See **ADR-0013** for the reasoning. In short:

| | Before | After |
|---|---|---|
| Participant directory | described as capped at nine | **no size limit** (was already uncapped in code; now explicit and tested) |
| Roster capacity | one module constant, 9, for every meeting | stored **per meeting** (`meetings.participant_capacity`, migration 0004) |
| Default | 9 | 9 — backfilled onto every existing meeting |
| Ceiling | none (9 was the cap) | configuration, default 50; DB `CHECK` is only `>= 1` |
| Lowering below the roster | impossible | refused `409`, **removes nobody** |
| `doctor` production target | fixed 9 | the largest configured roster capacity |

The `CHECK` deliberately omits the 50: encoding a business ceiling in the schema
would force a full rebuild of `meetings` — foreign keys, indexes, cascades — the first
time somebody legitimately needs 60.

Two real defects were found while writing the capacity tests:

* **FastAPI's `int` coercion accepted `true` as 1 and `"12"` as 12.** The requirement
  is explicit that a boolean and an ambiguous string must be rejected, so the body is
  now `StrictInt`. Found by a parametrised test, not by review.
* The `bool`/`float` guard in the service needed to exist independently, because
  `bool` is a subclass of `int` in Python and `True` would otherwise be a capacity
  of 1.

### Gates re-run after the change

| Gate | Result |
|---|---|
| Full suite + branch coverage | **1423 passed**, 0 failed, 0 skipped |
| New test files | `test_revoke_modal` 42 · `test_participants_capacity` 59 · `test_migration_0004` 18 |
| Mutation check on the modal tests | **12/12 defects caught**, assets restored byte-identical |
| Fresh migration | `0001`→`0002`→`0003`→`0004`, head 4 |
| Upgrade `3 → 4` on a **copy** of the real database | PASS — 2 meetings, 5 chunk rows byte-identical, 0 FK violations, both meetings backfilled to capacity 9 |
| `0001`/`0002` immutability vs `4a9518d` | byte-identical |
| Shell allowlist hostile-path matrix | 17 templated patterns, all anchored and lower-case-UUID-only; **23** hostile paths probed, 0 reachable by any method |
| Import side-effect audit | 56 modules, 0 violations, no data root created |
| AST network audit | 0 banned modules; the enrollment package imports no network module |
| Repository hygiene | 99 tracked files, no artefact, no banned package |
| `doctor` (migrated temp root) | 24 PASS / 10 WARN / **0 FAIL**, exit 0 |
| `doctor --production` | 24 PASS / 6 WARN / 5 FAIL — the same five external blockers |
| `doctor` (real data root) | 23 PASS / 10 WARN / **1 FAIL** — the pending 0004 migration, which the operator applies |
| `smoke` / `audio smoke` | PASS 11/11 · PASS 9/9 |
| `git diff --check` / `compileall` / `pip check` | clean |

### What is *not* covered by automation

Pointer hit-testing and real focus movement need a live DOM, and no JS runtime is
available offline — adding one to test one dialog would be a disproportionate
dependency. Those properties are in the manual acceptance steps
(§9a/§9b of `phase-3-participants-enrollment.md`). **The click test is still
outstanding**, so this session does not by itself close Phase 3.

---

## 7c. Session 7: the final corrective pass

Session 6 fixed the revoke dialog and introduced per-meeting capacity. Session 7
audited whether that work was actually *wired*, and found four gaps. None of them
would have been visible to a test of the configuration object in isolation -- which is
exactly why every new test here goes through the application, the API or the recording
service.

### Four findings

| # | Finding | Evidence |
|---|---|---|
| 1 | **The CLI never passed the configuration.** `_participant_services` built `ParticipantService(_connect)`, so every `participant` command silently used the built-in 9/50 fallback while the GUI honoured the operator's file. Two runtimes disagreeing about one policy. | `mom_igd/cli.py:970` |
| 2 | **New meetings ignored the configured default.** `_create_draft_meeting` inserted `(title, uuid)` only, so a new meeting took the column `DEFAULT 9`. An operator who configured 15 got 9. | `mom_igd/audio/service.py:619` |
| 3 | **The roster UI could not manage membership.** It could select a meeting, show a counter and change the capacity -- but there was no add, no remove, and no member list. The endpoints had existed since Phase 3 and nothing called them. | zero references to `add_to_meeting` in `app.js` |
| 4 | **`doctor` counted seats, not attendees.** The requirement was the largest configured *capacity*, so a meeting with capacity 15 and ten members was reported as needing fifteen templates. It was also a *global* count, so voiceprints belonging to people outside the roster would have satisfied it. | `_check_registry_counts` |

Finding 3 is confirmed by the operator's own temporary root
`D:\MoM-IGD-Test-Phase3-20260729-195451`: **15 participants in the directory, 0 active
roster memberships**. The capacity had been set to 15 and nobody could be put in it.

A fifth defect surfaced while testing: `ConsentState.to_dict()` carried the internal
integer `participant_id`, so **every** `/enrollment/participants` response had been
leaking a row id since Phase 3. Found by a roster test, fixed at the source.

### What changed

* `cli.py` -- passes `config=`, with a comment saying why it is not optional.
* `audio/service.py` -- writes `participant_capacity` from the configured default,
  reading one validated integer from the `AppConfig` it already holds. It still does
  **not** import the enrollment or participant package; a test asserts that.
* `enrollment/participants.py` -- `settable_capacity_bounds()` as the single source of
  truth, plus the grandfather policy for a lowered ceiling.
* `api/enrollment_routes.py` -- validates against the meeting's own bounds rather than
  the raw ceiling; `_decorate_with_state()` shared by the directory listing and the
  roster so both carry consent and voiceprint badges in **one** request.
* `enrollment/consent.py` -- `to_dict()` no longer exposes the row id.
* `diagnostics/enrollment_checks.py` -- `_roster_coverage()`: per roster,
  identity-aware, joined to *that participant's own* live voiceprint. The
  `MIN_PRODUCTION_VOICEPRINTS` constant was **deleted**, because a fallback number is
  only reachable when there is no roster, and then nothing is required.
* `shell/web/*` -- member list, remove button, bounded directory search with an add
  button, a remaining-slots line, and a range rendered from the server's settable
  bounds.

### The lowered-ceiling policy

Stored capacity 40, configured ceiling later lowered to 20:

* the stored 40 is **kept** -- not clamped on read, not silently adjusted;
* **no participant is ever removed**;
* it may be **lowered** (to at least the active roster count) but **not raised**, so
  every permitted change moves toward compliance;
* the state is reported explicitly (`capacity_above_ceiling`, `capacity_notice`,
  `capacity_min_settable`, `capacity_max_settable`) so neither the API nor the UI shows
  a range the meeting does not have.

### The new doctor definition

A roster member counts as **covered** only when all of these hold: the participant is
active, the membership is active, their latest consent event is a grant, and they own a
voiceprint that is `ACTIVE` **and** `production_eligible`. Coverage is reported per
roster, with the worst one named.

Stated limitations rather than guesses: the schema has no upcoming/historical meeting
state, so no meeting is assumed to be the relevant one; an empty roster asks for
nothing; and no display name or meeting title appears in the output.

This changed `doctor --production` on a fresh install from **5 FAIL to 4 FAIL**. The
gate still exits 1 -- the microphone, calibration, consent-text and model checks all
still fail. The fifth failure was simply not real.

### Gates re-run

| Gate | Result |
|---|---|
| Full suite + branch coverage | **1498 passed**, 0 failed, 0 skipped, **90 %** (8 762 statements, 1 992 branches) |
| New test files | `test_capacity_runtime_wiring` 27 - `test_roster_membership` 30 - `test_roster_coverage` 18 |
| Mutation check, Session 7 fixes | **13/13** defects caught, sources restored byte-identical |
| Mutation check, Session 6 modal | **12/12** still caught -- the revoke fix is intact |
| Fresh migration | `0001` to `0004`; `db version` reports 4 of 4 |
| Upgrade `3 -> 4` on a **copy** of the real database | PASS -- 2 meetings, 5 chunk rows byte-identical, 0 FK violations |
| `0001` / `0002` immutability vs `4a9518d` | byte-identical |
| `db verify` | schema 4 of 4, WAL, foreign keys on, audit chain **intact** |
| Shell allowlist hostile-path matrix | 17 anchored UUID-only patterns; **26** hostile paths probed, 0 reachable by any method; 6 legitimate paths all reachable |
| Import side-effect audit | 56 modules, 0 violations, no data root created |
| AST offline / network audit | 0 banned modules; the enrollment package imports no network module |
| Repository hygiene | 99 tracked files; no artefact, scratch script, data root or cache |
| `doctor` (fresh migrated temp root) | 24 PASS / 10 WARN / **0 FAIL**, exit 0 |
| `doctor --production` | 24 PASS / 7 WARN / **4 FAIL**, exit 1 |
| `doctor` (operator's temp root) | 25 PASS / 9 WARN / **0 FAIL**, exit 0 |
| `doctor` (real data root, read-only) | 23 PASS / 10 WARN / **1 FAIL** -- the pending `0004`; a 26-entry before/after snapshot proves nothing changed |
| `smoke` / `audio smoke` | PASS 11/11 - PASS 9/9 |
| `git diff --check` / `compileall` / `pip check` | clean |

### Deprecation warnings: 14, all pre-existing, none introduced

* `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated;
  install httpx2 instead` -- test-client only, from the pinned Starlette.
* `StarletteDeprecationWarning: 'HTTP_422_UNPROCESSABLE_ENTITY' is deprecated. Use
  'HTTP_422_UNPROCESSABLE_CONTENT' instead` -- the renamed constant does not exist in
  the pinned Starlette, so switching would break the build.

Neither affects runtime behaviour, and neither is worth changing a dependency to hide.

---

## 8. Exact steps to resume

```powershell
cd D:\Aldy\MoM-IGD
git rev-parse HEAD            # expect 4a9518d (no Phase 3 commit without permission)
git status --short             # expect 33 modified + 28 untracked (see section 5)
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q      # expect 1498 passed
.\.venv\Scripts\python.exe -m mom_igd doctor # 1 FAIL until migration 0004 is applied
```

Do **not** run `git reset`, `restore`, `checkout --`, `clean` or `stash`; the Phase 3
work is uncommitted by design and discarding it loses everything.

### What is built and trustworthy

Schema (0003), runtime paths, the key/cipher layer, consent and the participant
registry are complete and covered by 71 new tests. Build on them; do not rewrite
them.

### Next task, in order

1. **`mom_igd/api/enrollment_routes.py`.** Follow `mom_igd/api/audio_routes.py`
   exactly: `APIRouter(prefix=..., dependencies=[Depends(require_session_token)])`,
   a lazily-created service behind `_SERVICE_LOCK` on `app.state`, and a `_guard`
   translating service errors to HTTP codes. Map `EnrollmentError.reason` →
   409 (lifecycle/consent/lock), 404 (unknown participant), 503
   (`MODEL_UNAVAILABLE`), 422 (malformed input). Responses must never carry an
   embedding, centroid, dispersion, ciphertext, nonce, key, DPAPI blob or path —
   `tests/test_fresh_recording_flow.py::test_no_api_response_leaks_a_filesystem_path`
   is the pattern to copy for the assertion.
2. `mom_igd/shell/launcher.py`: extend `ALLOWED_PROXY_PATHS` / `ALLOWED_POST_PATHS`,
   then the UI in `mom_igd/shell/web/`. Start must be **disabled** with a visible
   `MODEL_UNAVAILABLE` explanation, since `readiness()` reports
   `can_start = False` today.
3. `mom_igd/diagnostics/enrollment_checks.py` + CLI subcommands, mirroring
   `audio_checks.py` and the `audio` CLI group.
4. Docs: `phase-3-participants-enrollment.md`,
   `phase-3-speaker-model-selection.md`, **ADR-0009 … ADR-0012** (0001–0008 taken;
   verify with `ls docs/adr/`), then `README.md`, `architecture.md`, `CLAUDE.md`,
   `AGENTS.md`.
5. **Last:** bump `APP_VERSION` to `0.3.0` and `CURRENT_PHASE` to `"3"`, then run
   the final gate. `SCHEMA_VERSION_HEAD` is already 3. `CONFIG_SCHEMA_VERSION` stays
   **2** — no `[enrollment]` config block has been added, and the enrollment
   thresholds live in `EnrollmentQualityThresholds` rather than in TOML. If that
   changes, bump to 3 and update `config/default.toml` plus the two consistency
   tests in `tests/test_cli.py`.

### Traps already identified

* `FakeKeyProtector` overrides `create_if_missing`, so anything added to the real
  one needs its own test with DPAPI stubbed — that gap already hid one real bug.
* `ParticipantService.to_dict()` deliberately omits the integer row id. API layers
  must address participants by UUID.
* `ConsentService.revoke()` writes only the event. Envelope deletion is the
  store's job, and the event must land first so a crash cannot leave consent
  intact with the template gone.
* Migration 0003 drops `ux_participants_display_name`. Any future migration that
  re-adds a unique index on `display_name` reverses ADR-0009 and must not.
* `QualityMeter` takes a **`CaptureProfile`**, not keyword arguments, and the
  accessor is `cumulative_snapshot()` — not `cumulative()`. Getting this wrong is a
  `TypeError` at first call.
* `estimate_snr_db` returns `float | None`. Treat `None` as `NOT_MEASURED`, never as
  zero.
* `VoiceprintStore.save()` needs `development_only` passed in; it does not infer it.
  The enrollment service decides, from the device transport.
* `_supersede_live` runs inside the activation transaction and deletes the previous
  envelope. A re-enrollment therefore *destroys* the old template by design.
* `CaptureProfile` has **`describe()`**, not `to_dict()`.
* `EnrollmentService._capture_lock` returns `recording_service._lock` — the *same
  object*. Never decide anything from its `.held` flag; check the holder identity.
* `EnrollmentService.add_sample()` takes PCM the **caller** captured. The service
  performs no device I/O, which is what keeps embedding, encryption and database
  work out of the audio callback. The API route or wizard owns the stream.
* `enrollment.finalize()` returns `{"voiceprint": None}` on a quality rejection
  rather than raising — a rejected enrollment is an expected outcome, not an error.
  Only a *broken* embedding or a storage failure raises.
* `EnrollmentService.eligibility()` is the single fail-closed policy Phase 6 must
  call. Do not let Phase 6 re-derive eligibility from raw rows.
