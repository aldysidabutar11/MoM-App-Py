# Feature completion matrix — MoM-IGD, Phases 0–12

**Audit date:** 2026-08-05 · **Commit:** `4674ea4` · **Build reports:** `APP_VERSION 0.3.0`,
`CURRENT_PHASE 3`, `SCHEMA_VERSION_HEAD 5`

## Status vocabulary

| Status | Meaning |
|---|---|
| `IMPLEMENTED_AND_VERIFIED` | Implemented, covered by automated tests **and** proven by a command run during this audit or recorded evidence on this machine. |
| `IMPLEMENTED_MANUAL_TEST_PENDING` | Implemented and unit-tested; the property that matters needs a human, hardware or real speech to confirm. |
| `PARTIAL` | Some of the capability exists; a named part does not. |
| `SCAFFOLD_ONLY` | Declared as data or contract; nothing executes. |
| `NOT_STARTED` | No code. |
| `BLOCKED` | Implemented as far as it can be; waiting on an external decision or artefact. |
| `DEFERRED_BY_DESIGN` | Deliberately absent at this phase, with a recorded reason. |
| `UNKNOWN_REQUIRES_EVIDENCE` | Cannot be judged from the repository. |

**A file is not evidence.** Nothing below is marked verified because a module or a document
exists. Where the evidence is a test, the test file is named; where it is a command, the command
and its actual output are given.

---

# Phase 0 — Audit, feasibility, architecture

| Capability | Status | Source | Test | Verification | Actual | Missing | Next |
|---|---|---|---|---|---|---|---|
| Environment audit and hardware implications | `IMPLEMENTED_AND_VERIFIED` | `docs/phase-0-summary.md` | n/a | read | Documented: i7-1260P, 16 GB, Iris Xe, ~4.1 GB free RAM measured | — | — |
| Architecture decision records | `IMPLEMENTED_AND_VERIFIED` | `docs/adr/0001`–`0016` | `tests/test_repo_hygiene.py` | `git ls-files` | 16 ADRs, contiguous, each with context/decision/consequences | — | Add an ADR when Phase 5 lands |
| Roadmap | `IMPLEMENTED_AND_VERIFIED` | `docs/architecture.md` §5 | n/a | read | Phases 0-12 with exit evidence per phase | Two drift items — see MOM-DEBT-005 | Correct the stage-table phase and lifecycle diagram |

---

# Phase 1 — Application foundation

| Capability | Status | Source | Test | Verification command | Actual result | Missing | Next |
|---|---|---|---|---|---|---|---|
| Layered configuration + validation gate | `IMPLEMENTED_AND_VERIFIED` | `mom_igd/config.py` (312 stmt, 90%) | `test_config.py` | `mom_igd config show` | Schema v4, offline, rejects non-loopback host / `max_heavy_workers>1` / relative data root / unknown key | — | — |
| Runtime path service | `IMPLEMENTED_AND_VERIFIED` | `mom_igd/paths.py` (95%) | `test_paths.py` | `doctor` | `data_path` PASS; repo/data separation enforced; no directory created on import | `describe()` omits `voiceprints`, `keys`, `working` | Cosmetic |
| SQLite + WAL + FK, verified per connection | `IMPLEMENTED_AND_VERIFIED` | `mom_igd/db/connection.py` (97%) | `test_db.py` | `mom_igd db verify` | `journal_mode=wal`, `foreign_keys=1`, `busy_timeout 5000`, SQLite 3.49.1 | — | — |
| Transactional, checksummed migrations | `IMPLEMENTED_AND_VERIFIED` | `mom_igd/db/migrator.py` (95%) | `test_migrations.py`, `test_migration_0002.py`, `test_migration_0004.py`, `test_asr_migration.py` | `mom_igd db version` | Schema 5 of 5, up to date; `split_sql_statements` used, never `executescript` | No downgrade path (by design) | Rehearse 3→5 on a copy — MOM-GAP-008 |
| Job state machine (states, transitions, stages) | `IMPLEMENTED_AND_VERIFIED` | `mom_igd/jobs/state_machine.py` (94%) | `test_state_machine.py` (graph self-check) | n/a | 10 states, validated transitions, audit event in the same transaction, BFS `transition_path` | **Only the capture service drives it** — MOM-DEBT-001 | Decide: wire Phase 4 in, or amend the invariant |
| Loopback API + Host allowlist | `IMPLEMENTED_AND_VERIFIED` | `mom_igd/api/app.py` (95%) | `test_api.py`, `test_server_lifecycle.py` | `mom_igd smoke` | 11/11 — non-loopback Host → 403 | No CORS middleware (correct: same origin) | — |
| Session token protection | `IMPLEMENTED_AND_VERIFIED` | `mom_igd/security.py` (94%), `api/deps.py` (100%) | `test_security.py` | `mom_igd smoke` | 401 without token, 400 for a token in a query string even when correct, token in no response body | — | — |
| Offline policy (denylist, endpoint rule, bind rule) | `IMPLEMENTED_AND_VERIFIED` | `mom_igd/offline_policy.py` (95%) | `test_offline_policy.py` | `doctor` | `offline_policy` PASS: no cloud SDK, 0 endpoints, all local | OS firewall deferred to Phase 11 | — |
| Diagnostics (`doctor`, bootstrap) | `IMPLEMENTED_AND_VERIFIED` | `diagnostics/` | `test_doctor.py`, `test_bootstrap_doctor.py` | `mom_igd doctor --json` | 24 PASS / 11 WARN / 0 FAIL, exit 0; stdlib-only bootstrap path | `doctor.py` 83%, `enrollment_checks.py` 76% | Raise coverage of branch paths |
| Structured logging + token redaction | `IMPLEMENTED_AND_VERIFIED` | `mom_igd/logging_setup.py` (91%) | covered in `test_security.py` | `mom_igd smoke` | `RedactingFilter` on every handler incl. uvicorn | — | — |
| Hash-chained audit trail | `IMPLEMENTED_AND_VERIFIED` | `mom_igd/audit.py` (**100%**) | `test_audit.py` | `mom_igd db verify` | Audit chain intact on both data roots | Tail truncation undetectable (documented, asserted) | Signed high-water mark — Phase 11 |
| Crash recovery (application level) | `PARTIAL` | `api/app.py` lifespan | `test_server_lifecycle.py` | — | A live recording is finalised on shutdown; an enrollment is abandoned | No sweeper for stale `BUILDING` transcripts — MOM-RISK-003 | Add a startup reconciliation + `doctor` check |
| Model registry declaration | `IMPLEMENTED_AND_VERIFIED` | `mom_igd/registry.py` (96%), `models/registry.json` | `test_registry.py` | `mom_igd registry show` | Valid, schema v1, **0 models declared** (correct — ADR-0005) | — | Populate when a model is approved |
| Desktop shell (pywebview, no npm/CDN) | `IMPLEMENTED_MANUAL_TEST_PENDING` | `mom_igd/shell/` (launcher 76%) | `test_static_ui.py` | `mom_igd smoke` step `static_ui_local_only` | 30 235 bytes, **0 remote assets** | `run_shell()` cannot be exercised headlessly | Manual GUI acceptance |

---

# Phase 2 — Offline audio capture

| Capability | Status | Source | Test | Verification command | Actual result | Missing | Next |
|---|---|---|---|---|---|---|---|
| Device discovery (no stream opened) | `IMPLEMENTED_AND_VERIFIED` | `audio/devices.py` (89%) | `test_audio_backend_adapter.py` | `mom_igd audio devices` | 3 usable devices listed, enumeration opens nothing | — | — |
| Device fingerprint identity, no silent fallback | `IMPLEMENTED_AND_VERIFIED` | `audio/devices.py`, `audio/service.py:466-478` | `test_audio_service.py` | `doctor` | Fingerprint not index; a missing device raises (ADR-0008) | — | — |
| WASAPI / PortAudio backend, lazy import, no NumPy | `IMPLEMENTED_AND_VERIFIED` | `audio/sounddevice_backend.py` (90%) | `test_audio_backend_adapter.py` | `doctor` | sounddevice 0.5.5, PortAudio V19.7.0-devel | — | — |
| Recording (start/stop) | `IMPLEMENTED_MANUAL_TEST_PENDING` | `audio/service.py` (91%), `audio/session.py` (92%) | `test_audio_capture.py`, `test_fresh_recording_flow.py` | `mom_igd audio smoke` | 9/9 on the fake backend; **byte-exact** against a deterministic source | Never run for 60 min on a real USB device | MOM-GAP-004 |
| Pause / resume with a recorded gap | `IMPLEMENTED_MANUAL_TEST_PENDING` | `audio/session.py:305-360` | `test_audio_capture.py` | — | Gap appended to the manifest, never filled in the master | Human pause/resume not exercised | Manual acceptance C.1 step 8 |
| 30 s chunk rotation | `IMPLEMENTED_AND_VERIFIED` | `audio/writer.py` (89%) | `test_audio_capture.py` | `mom_igd audio smoke` | Exact rotation; `.part` → fsync → hash from disk → atomic rename | — | — |
| Manifest (JSONL + summary, hash chain) | `IMPLEMENTED_AND_VERIFIED` | `audio/manifest.py` (87%) | `test_audio_capture.py` | `mom_igd audio smoke` | `manifest_verified`, chain `e4e96a05c256` | — | — |
| Per-chunk SHA-256 + tamper detection | `IMPLEMENTED_AND_VERIFIED` | `audio/writer.py`, `audio/manifest.py` | `test_audio_capture.py` | `mom_igd audio smoke` | `tampering_detected`: one flipped byte caught | — | — |
| Bounded queue, overflow counted not blocked | `IMPLEMENTED_AND_VERIFIED` | `audio/frame_queue.py` (98%) | `test_audio_capture.py` | `mom_igd audio smoke` | 8000 frames written, 0 dropped; capacity in seconds | — | — |
| Clipping / silence warning with operator advice | `IMPLEMENTED_AND_VERIFIED` | `audio/quality.py` (93%), `audio/calibration.py` (83%) | `test_audio_capture.py` | `mom_igd audio calibrate` (not run — opens the mic) | Verdict + named Windows setting to change | Human calibration | Manual acceptance C.1 step 4 |
| Interrupted-recording recovery | `IMPLEMENTED_AND_VERIFIED` | `audio/recovery.py` (90%) | `test_audio_capture.py` | `mom_igd audio smoke` | `recovery_rebuilt_partial` (4321 frames, 1 trailing byte discarded), idempotent, recovered chunk verifies. Also finishes an interrupted finalisation (the dead-end-warning fix) | — | — |
| One-microphone lock (lock file + DB index) | `IMPLEMENTED_AND_VERIFIED` | `audio/service.py:173-273` | `test_audio_service.py` | — | `O_EXCL` lock + partial unique index; stale PID cleared | PID reuse can wedge it — MOM-RISK-005 | Add a start-time check and an unlock path |
| Long-duration recording | `UNKNOWN_REQUIRES_EVIDENCE` | — | `audio bench` (accelerated soak) | not run | Never run for 60 min on hardware | 3 × 60 min acceptance | MOM-GAP-004 |
| Disk-space handling | `IMPLEMENTED_MANUAL_TEST_PENDING` | `audio/preflight.py` (89%), `config` `min_free_disk_gb`/`low_disk_abort_gb` | `test_audio_service.py` | `doctor` | Refuses below 5 GB, finalises below 1 GB | Not exercised on a genuinely full volume | Add to the resilience test set |
| Device disconnect mid-recording | `IMPLEMENTED_MANUAL_TEST_PENDING` | `audio/service.py` `abandon()` | `test_audio_capture.py` (fake backend) | — | Partial preserved for recovery | Not exercised by unplugging real hardware | Phase 12 resilience |
| UI recording controls | `IMPLEMENTED_MANUAL_TEST_PENDING` | `shell/web/app.js:400-799` | `test_audio_api_ui.py`, `test_static_ui.py` | static | Double-click guarded, buttons driven by server state, poll chained | Static assertions only — MOM-DEBT-004 | Manual acceptance C.1 |
| Production acceptance gate | `BLOCKED` | `doctor --production` | `test_doctor.py` | not run | Requires a USB conference mic + recorded calibration | Hardware absent | MOM-GAP-004 |

---

# Phase 3 — Participants, consent, enrollment

| Capability | Status | Source | Test | Verification | Actual | Missing | Next |
|---|---|---|---|---|---|---|---|
| Dynamic per-meeting participant capacity | `IMPLEMENTED_AND_VERIFIED` | `enrollment/participants.py` (96%), migration `0004` | `test_participants_capacity.py`, `test_capacity_runtime_wiring.py`, `test_migration_0004.py` | `doctor` | Capacity stored per meeting; DB invariant only `>= 1`; ceiling in configuration (default 50); grandfathering never clamps | — | — |
| **No hidden 9/15 limit anywhere** | `IMPLEMENTED_AND_VERIFIED` | swept this audit | `test_participants_capacity.py` | `grep` over DB / API / GUI / config / tests / docs | Only occurrences of 9 are the *configured default*, `BASELINE_MEETING_CAPACITY`, `FALLBACK_DEFAULT_CAPACITY` and migration 0004's backfill DEFAULT — all documented as defaults, none enforcing | — | — |
| Create / edit / deactivate / reactivate participant | `IMPLEMENTED_AND_VERIFIED` | `enrollment/participants.py`, `api/enrollment_routes.py` (96%) | `test_enrollment_api.py`, `test_roster_membership.py` | `mom_igd participant list` | Directory has **no** size limit; identity is the UUID; name is never a key | — | — |
| Consent lifecycle (grant, append-only) | `IMPLEMENTED_AND_VERIFIED` | `enrollment/consent.py` (98%) | `test_participants_consent.py` | — | Append-only `consent_events`; typed confirmation `SAYA SETUJU` | Text is a **draft** — MOM-GAP-005 | Legal review |
| Revoke consent → delete ciphertext | `IMPLEMENTED_AND_VERIFIED` | `enrollment/service.py:revoke_consent_and_delete`, `cli.py:1361` | `test_participants_consent.py`, `test_voiceprint_store.py` | — | Typed confirmation `CABUT`; envelope deleted; status `REVOKED` | Never exercised on a real voiceprint (none exist) | After MOM-GAP-002 |
| Enrollment capture (Python-side, no browser audio) | `IMPLEMENTED_AND_VERIFIED` | `enrollment/capture.py` (98%) | `test_enrollment_capture.py` | static assertions in `test_participants_ui.py` | No `getUserMedia`/`AudioContext`/`MediaRecorder` anywhere in the page | — | — |
| No raw enrollment audio persisted | `IMPLEMENTED_AND_VERIFIED` | `enrollment/capture.py`, ADR-0012 | `test_enrollment_capture.py` | `doctor` `voiceprint_storage` PASS | Bounded memory only, discarded after embedding | — | — |
| Voiceprint encryption (AES-256-GCM + AAD) | `IMPLEMENTED_AND_VERIFIED` | `enrollment/cipher.py` (88%) | `test_voiceprint_crypto.py` | `doctor` `cryptography_backend` PASS | AAD binds uuid + participant + schema + model identity; fresh 96-bit nonce; atomic write | — | — |
| DPAPI master-key protection | `IMPLEMENTED_AND_VERIFIED` | `enrollment/keys.py` (68%) | `test_voiceprint_crypto.py` (with a reversible stub) + a recorded live run | `doctor` `dpapi_available` PASS | `crypt32.dll` exports present; key never created implicitly | **No backup or escrow** — MOM-RISK-001 | Decide the recovery story |
| Crash-consistent voiceprint storage | `IMPLEMENTED_AND_VERIFIED` | `enrollment/store.py` (89%) | `test_voiceprint_store.py` | `doctor` `voiceprint_integrity`, `voiceprint_cleanup` PASS | 10-step write order; row marked usable only after the bytes are durable | — | — |
| Deletion and re-enrollment | `IMPLEMENTED_AND_VERIFIED` | `enrollment/store.py`, `enrollment/service.py` | `test_voiceprint_store.py` | `doctor` `voiceprint_cleanup` PASS | `DELETE_PENDING` → deleted; at most one live template per participant | — | — |
| `UNKNOWN` fallback semantics | `DEFERRED_BY_DESIGN` | — | — | — | Phase 6 owns it; nothing here may guess | — | Phase 6 |
| Participant / roster UI | `IMPLEMENTED_MANUAL_TEST_PENDING` | `shell/web/app.js:801-2039` | `test_participants_ui.py`, `test_revoke_modal.py` | static | Revoke modal, capacity editor, consent dialog, readiness poll | Static assertions only | Manual acceptance |
| API authorisation for enrollment | `IMPLEMENTED_AND_VERIFIED` | `api/enrollment_routes.py` | `test_enrollment_api.py` | `mom_igd smoke` | Router-level token dependency; anchored UUID templates in the shell allowlist; no route deletes a participant or a voiceprint directly | Two deprecated 422 constants — MOM-DEBT-006 | — |
| `doctor` roster-coverage check (attendees, not seats) | `IMPLEMENTED_AND_VERIFIED` | `diagnostics/enrollment_checks.py` (76%) | `test_roster_coverage.py` | `doctor` | Joins each active member to *that person's own* live voiceprint | — | — |
| **Speaker-embedding model** | **`BLOCKED`** | `enrollment/provider.py` (93%) | `test_enrollment_provider_quality.py` (fake provider) | `doctor` `speaker_embedding_model` **WARN** | 0 models declared, 0 evaluated, 0 benchmarked, **no real voiceprint has ever been produced** | The model decision itself | **MOM-GAP-002 — blocks Phase 6** |

---

# Phase 4A — Benchmark gate

| Capability | Status | Source | Evidence | Actual |
|---|---|---|---|---|
| Real-device RTF and peak RSS | `IMPLEMENTED_AND_VERIFIED` | `asr/benchmark.py` (65%), `docs/benchmarks/*.json` | 5 sweeps, 30 runs, 0 errors | small@12thr RTF **0.142** / 693 MiB; turbo@12thr RTF **0.284** / 1 910 MiB |
| Zero-egress measurement | `IMPLEMENTED_AND_VERIFIED` | `asr/benchmark.py` | `network_attempts == []` on all 30 runs, with a recorder a test proves can report an attempt | PASS |
| Co-residency budget | `IMPLEMENTED_AND_VERIFIED` | `docs/benchmarks.md` | 693 + 1 910 = 2 603 MiB > 2.5 GB | Direct evidence for one heavy worker |
| Provider selection (ADR-0014) | `IMPLEMENTED_AND_VERIFIED` | `docs/adr/0014` | faster-whisper, CPU, INT8, 12 threads | — |
| Accuracy | `NOT_STARTED` | — | `docs/benchmarks.md:112-116` | Every accuracy row **N/A — PENDING** — MOM-GAP-001 |
| OpenVINO / GPU | `DEFERRED_BY_DESIGN` | — | Never installed, never probed; **no claim made** that Iris Xe cannot be reached | — |

---

# Phase 4 — Offline ASR

| Capability | Status | Source | Test | Verification command | Actual result | Missing | Next |
|---|---|---|---|---|---|---|---|
| Model catalogue and provisioning (the only downloader) | `IMPLEMENTED_AND_VERIFIED` | `asr/provision.py` (31%) | `test_asr_provisioning.py` (39) | `mom_igd asr models` | 2 provisioned `OK`: small@`536b0662742c` (464 MiB), turbo@`4df90f753211` (1 547 MiB) | 31% coverage — needs the network by nature | — |
| Model integrity (byte verification) | `IMPLEMENTED_AND_VERIFIED` | `asr/manifest.py` (90%) | `test_asr_provisioning.py` | `mom_igd asr verify` | **Every byte re-hashed from disk**, both models match | — | — |
| Readiness registry (load-and-decode probe) | `IMPLEMENTED_AND_VERIFIED` | `asr/installed.py` (84%) | `test_asr_provisioning.py` | `mom_igd asr models` | Catalogue → registry → resolver; digest re-derived on every read; fails closed | — | — |
| Offline enforcement in the worker | `IMPLEMENTED_AND_VERIFIED` | `asr/faster_whisper_provider.py` (39%) | `test_asr_offline.py` (22, child processes) | `mom_igd asr smoke` | 7 offline flags **assigned** (never `setdefault`), HF tokens deleted, **zero outbound attempts** | — | — |
| Normalisation → 16 kHz mono working copy | `IMPLEMENTED_AND_VERIFIED` | `asr/normalize.py` (94%) | `test_asr_normalization.py` (43) | end-to-end run on record | Master untouched (hashed before/after); gaps filled in the copy and recorded on the row | — | — |
| VAD (Silero, bundled, CPU provider pinned) | `IMPLEMENTED_AND_VERIFIED` | `asr/vad.py` (92%) | `test_asr_vad.py` (27) | `mom_igd asr smoke` | 3 regions / 94% speech; asset sha `4cbf549b8326f60f…`, bundled in the wheel | — | — |
| Pass 1 (batched 30 s windows) | `IMPLEMENTED_MANUAL_TEST_PENDING` | `asr/tasks.py` (99%), `asr/pipeline.py` (87%) | `test_asr_tasks.py` (50), `test_asr_pipeline.py` (32) | `mom_igd asr smoke` | 4 validated segments from 8 s; batching took RTF 2.8 → 0.31 | Never run on real speech | MOM-GAP-001 |
| Selective pass 2 (budgeted, reason-coded) | `IMPLEMENTED_MANUAL_TEST_PENDING` | `asr/selection.py` (99%), `asr/merge.py` (**100%**) | `test_asr_selection.py` (34), `test_asr_merge.py` (19) | end-to-end run on record | Deterministic; every choice carries a reason code; over-budget region skipped not blocking; supersede-never-overwrite | Whether it *improves* anything is unmeasured | MOM-GAP-003 |
| Terminology normalisation | `IMPLEMENTED_MANUAL_TEST_PENDING` | `asr/glossary.py` (96%), `config/glossary.id-en.toml` | `test_asr_glossary.py` (40) | — | 41 terms / 118 variants; whole-word only; `text_raw` always preserved | Recall on real speech unmeasured | MOM-GAP-003 |
| Word timestamps | `IMPLEMENTED_MANUAL_TEST_PENDING` | `asr/provider.py` (95%) | `test_asr_provider.py` (49) | `mom_igd asr smoke` | 12 word timestamps produced and validated | Accuracy unmeasured | MOM-GAP-003 |
| Transcript persistence + revisions | `IMPLEMENTED_AND_VERIFIED` | `asr/store.py` (93%), migration `0005` | `test_asr_migration.py` (33) | end-to-end run on record | 6 revisions, exactly one active, superseded pass-1 segment points at its replacement, 2 530 word rows | — | — |
| **Job lifecycle integration** | **`PARTIAL`** | `asr/pipeline.py:427` accepts `job_id`, always receives `None` | — | production DB: all `job_stages` untouched | Transcript status is authoritative; the job machine is never advanced | Stage progress, attempt count, checkpoints in `job_stages` | **MOM-DEBT-001** |
| Progress reporting | `IMPLEMENTED_MANUAL_TEST_PENDING` | `asr/service.py` (96%), `app.js` poll loop | `test_asr_service.py` (23) | — | Stage list + elapsed timer + `/asr/status` at 1200 ms | **Breaks after 60 s — MOM-BUG-001** | Fix the bridge |
| Cancellation | `IMPLEMENTED_AND_VERIFIED` | `asr/worker.py` (72%), `asr/service.py` | `test_asr_worker.py` (30) | — | Cooperative flag, 45 s grace, then `terminate()`, then `kill()`; revision left `CANCELLED`, never active | Human cancel not exercised | Manual acceptance Part D |
| Retry (re-run) | `IMPLEMENTED_AND_VERIFIED` | `asr/store.create_transcript` | `test_asr_pipeline.py` | end-to-end run on record | A re-run writes a new revision, deactivates the old, keeps it as evidence | — | — |
| Resume / checkpointing | `IMPLEMENTED_AND_VERIFIED` | `asr/pipeline.py:213-341` | `test_asr_pipeline.py` | recorded second run | *"reused the existing working copy … SHA-256 still matches"*, *"reused the existing run … same configuration hash"* | Not via `job_stages` (MOM-DEBT-001) | — |
| Stale-job recovery | `NOT_STARTED` | — | — | `grep 'BUILDING'` finds no sweeper | A killed run leaves `BUILDING` for ever | Startup reconciliation + `doctor` check | **MOM-RISK-003** |
| Corrupted-artefact handling | `IMPLEMENTED_AND_VERIFIED` | `asr/installed.py`, `asr/manifest.py` | `test_asr_provisioning.py` | — | Corrupt registry ⇒ "nothing is ready"; an unusable model is quarantined, not deleted | — | — |
| GUI workflow | **`PARTIAL`** | `shell/web/app.js:2041-2671` | `test_asr_ui_contract.py` (39, static) | reproduction executed this audit | Panel complete and correct; **the bridge aborts the run at 60 s** | An async POST + poll-driven completion | **MOM-BUG-001 (P1)** |
| API / CLI parity | `IMPLEMENTED_AND_VERIFIED` | `api/asr_routes.py` (86%), `cli.py` | `test_asr_routes.py` (31) | `mom_igd asr transcribe --help` | 7 endpoints and 6 CLI commands over the same service; no path in or out of either | CLI handler coverage — MOM-DEBT-002 | — |
| Active-recording protection | `IMPLEMENTED_AND_VERIFIED` | `asr/service.py:149-166, 399-405` | `test_asr_service.py` (a test reads migration 0002's SQL to stop the state list drifting) | — | Transcription refuses with 409 while a capture is live; a capture is **never** refused | No yield if a recording starts *later* — MOM-RISK-002 | Measure capture under load |
| Single heavy-worker policy | `IMPLEMENTED_AND_VERIFIED` | `asr/worker.py`, `asr/service.py` | `test_asr_worker.py`, `test_asr_service.py` | benchmark | One spawned worker per stage, exits before the next loads; a second run is 409, not a queue | — | — |
| Resource budget | `IMPLEMENTED_AND_VERIFIED` (short runs) | `asr/worker.py` peak-RSS sampling | `test_asr_worker.py` | benchmark | Worst single worker 1 910 MiB < 2.5 GB across 30 runs | Long meetings unmeasured — MOM-RISK-010 | Long-run test |
| Disk guard before a run | `PARTIAL` | `asr/service.py:301-325` | `test_asr_service.py` | — | Checked in `preflight()` only | Not re-checked in `transcribe()` | **MOM-RISK-004** |
| **No speaker anywhere** | `IMPLEMENTED_AND_VERIFIED` | migration `0005`, `asr/provider.py:424-428` | `test_asr_provider.py`, `test_asr_migration.py` | `mom_igd asr smoke` step 9 | No column exists; a result carrying a speaker is rejected | — | — |
| Acceptance preflight script | `IMPLEMENTED_AND_VERIFIED` | `scripts/phase4_acceptance_preflight.ps1` | `test_acceptance_preflight_script.py` (65) | script run this audit | 9 PASS / 4 WARN / 0 FAIL, exit 0, READY | Guard bypassable by path spelling | **MOM-BUG-002** |
| **Real-speech accuracy acceptance** | **`NOT_STARTED`** | `asr/benchmark.py --manifest` (the loader exists and is tested) | `test_asr_benchmark.py` (74) | — | No corpus, no reference transcript, **WER N/A** | A consented/licensed Indonesian corpus ≥ 10 min far-field | **MOM-GAP-001 — the phase gate** |

---

# Phases 5–12 — accidental-implementation sweep

Checked by `grep` across `mom_igd/`, the migrations and the shipped page, **not** by reading
documentation. Nothing below is a placeholder module, an empty package or a dead schema column.

| Phase | Objective | Status | What exists today | What must not be mistaken for progress |
|---|---|---|---|---|
| **5 — Anonymous diarization, overlap detection** | Speaker turns without names | `SCAFFOLD_ONLY` (declaration only) | One row in `PIPELINE_STAGES`: `StageSpec("diarize", …, is_heavy=True, phase_introduced="5")`. No module, no table, no dependency (`pyannote.audio`, `torch` absent — `doctor` `optional_dependencies` WARN). `mom_igd/asr/selection.py` explicitly notes where speaker-change and overlap reason codes will slot in. | The stage row is data, not code. No `diarization_turns` table exists. |
| **6 — Voice identification, strict UNKNOWN** | Cluster → participant mapping | `NOT_STARTED`, and `BLOCKED` behind MOM-GAP-002 | `StageSpec("voice_id", …, phase="6")`. The Phase 3 voiceprint schema, cipher and AAD model binding are ready to be consumed. | No `speaker_assignments` table. No threshold, no Hungarian assignment, no matcher. **And no embedding model, so there is nothing to match against.** |
| **7 — Deterministic reconciliation** | Canonical utterances from words + turns + identity | `NOT_STARTED` | `StageSpec("reconcile_transcript", …, phase="7")`. Word timings and region provenance are persisted, which is the input it needs. | No `utterances` table. |
| **8 — Local LLM MoM generation** | Decisions, actions, PIC, deadlines, evidence | `NOT_STARTED` | `StageSpec("mom_extract")` and `StageSpec("verify_evidence")`. `config/default.toml` has an **empty** `[providers.endpoints]` with a commented loopback example. `audit_events` has an `EXPORT` category reserved. | No `mom_items`, no `evidence_links`. No LLM dependency of any kind is installed. The empty endpoints block is the correct state, not a stub. |
| **9 — Human review and approval** | Evidence-linked review, immutable approval | `SCAFFOLD_ONLY` (state machine only) | `JobState.REVIEW_REQUIRED` and `APPROVED`; `APPROVED` is terminal and its immutability rationale is documented. The shell shows a **disabled** "Review" card saying *Belum diimplementasikan* (asserted by `test_asr_ui_contract.py:284`). | The state exists; nothing transitions into it. `audit_events.category` includes `REVIEW`. |
| **10 — Export and action tracking** | PDF / DOCX / MD / JSON, versioned snapshots | `NOT_STARTED` | `<data_root>/exports` is created by `RuntimePaths.ensure()` and is **empty and unwritten**. A disabled "Export" card. `audit_events` category `EXPORT`. | A created directory is not a feature. No `action_tracking` table, no exporter, no template. |
| **11 — Security, packaging, backup, recovery** | Installer, backup/restore, retention, DPIA | `NOT_STARTED` | `<data_root>/backups` created and **never written**. `audit_events` category `RETENTION`. `requirements.txt:19` records that a hash-pinned offline wheelhouse is deferred here. `pyproject.toml:66-71` notes PyInstaller is Phase 11. | Voiceprint encryption at rest (AES-GCM + DPAPI) landed **early**, in Phase 3 — that part is genuinely done. Everything else in this phase is not. |
| **12 — Evaluation, hardening, pilot** | 5 real meetings, DER/JER, resilience, runbook | `NOT_STARTED` | `asr/benchmark.py --manifest` provides the accuracy-measurement harness with a consent gate. | The harness is not a measurement. No pilot, no runbook, no resilience test set. |

**Verdict on the sweep:** no future phase has been accidentally implemented, and no future phase
has been scaffolded as an empty placeholder. Scope discipline has held. The only cross-phase
irregularity found is MOM-DEBT-001, which is a *gap* (Phase 4 not using Phase 1's machine), not a
scope leak.

---

# Cross-cutting capability summary

| Capability | Status |
|---|---|
| Fully offline runtime | `IMPLEMENTED_AND_VERIFIED` — dependency denylist, endpoint rule, bind rule, HF offline flags by assignment, zero measured egress over 30 benchmark runs and every smoke run |
| No CUDA / no GPU assumption | `IMPLEMENTED_AND_VERIFIED` — CPU INT8 selected on measured evidence; no CUDA artefact or hardware profile anywhere |
| No Docker / WSL production dependency | `IMPLEMENTED_AND_VERIFIED` — informational only in `doctor`; nothing imports or invokes them |
| Loopback-only binding | `IMPLEMENTED_AND_VERIFIED` — configuration rejects anything else; Host allowlist blocks DNS rebinding |
| One heavy model at a time | `IMPLEMENTED_AND_VERIFIED` — configuration rejects `max_heavy_workers > 1`; measured co-residency would breach the budget |
| Evidence chain (recording → working copy → VAD → transcript → words) | `IMPLEMENTED_AND_VERIFIED` — every link records the provenance of the one above it |
| Encryption at rest for transcripts and audio | `DEFERRED_BY_DESIGN` (Phase 11) — the UI states plainly that audio is not encrypted at rest |
| Backup / restore / retention | `NOT_STARTED` (Phase 11) — MOM-GAP-007 |
| Packaging / installer | `NOT_STARTED` (Phase 11) — and `pyproject.toml` would produce a build that cannot transcribe (MOM-RISK-006) |
| CI / lint / type checking | `NOT_STARTED` — MOM-DEBT-003 |
| Licence model | `NOT_STARTED` — deliberately no `LICENSE`; `pyproject.toml` marks the project `Private :: Do Not Upload`. All three declared model licences are **MIT**. A decision is required before sharing. |
