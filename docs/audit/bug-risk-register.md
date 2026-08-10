# Bug and risk register — MoM-IGD

**Audit date:** 2026-08-05 · **Commit audited:** `4674ea4` · **Working tree:** clean
**Audit mode:** read-only. No source, test, migration, configuration or dependency file was changed.

Every entry below is backed by an artefact in this repository, a command output recorded during
this audit, or a reproduction that was actually executed. Where something was **not** executed,
the entry says so and is classified accordingly. Nothing here is inferred from documentation
alone.

---

## Summary

| Severity | Count |
|---|--:|
| **P0** | **0** |
| P1 | 4 |
| P2 | 18 |
| P3 | 9 |
| **Total** | **31** |

| Category | Count |
|---|--:|
| `CONFIRMED_BUG` | 2 |
| `LIKELY_BUG` | 1 |
| `RISK` | 10 |
| `TECHNICAL_DEBT` | 9 |
| `EXPECTED_WARNING` | 5 groups |
| `ACCEPTANCE_GAP` | 9 |

**There are zero P0 findings.** No data loss, no privacy breach, no security breach and no
silently-wrong critical result was found. The single P1 defect breaks a workflow; it does not
corrupt or lose anything.

---

## Severity definitions used

* **P0** — data loss, privacy/security breach, application unusable, or a critical result that is
  wrong without warning.
* **P1** — a primary workflow fails, or a high-probability serious risk.
* **P2** — an important function is impaired but a workaround exists.
* **P3** — minor issue, UX, cleanup or improvement.

---

# 1. Confirmed bugs

## MOM-BUG-001 — The desktop shell aborts every transcription that takes longer than 60 seconds

| | |
|---|---|
| **Category** | `CONFIRMED_BUG` |
| **Severity** | **P1** |
| **Subsystem** | Desktop shell ↔ API bridge (Phase 4 GUI workflow) |
| **Confidence** | **High — reproduced during this audit against the real production code path** |
| **Suggested owner** | Desktop UI/API workstream |

### Evidence

* `mom_igd/shell/launcher.py:152` — `_PROXY_TIMEOUT_S: Final[float] = 60.0`
* `mom_igd/shell/launcher.py:275` — `urllib.request.urlopen(request, timeout=_PROXY_TIMEOUT_S)`
* `mom_igd/api/asr_routes.py:155-184` — `POST /asr/transcribe` runs the **entire pipeline
  synchronously** inside the request (`result = _guard(service.transcribe, recording_uuid)`).
* `mom_igd/shell/launcher.py:86` — `/asr/transcribe` is on the shell POST allowlist, i.e. the page
  issues it through the 60 s proxy.
* `mom_igd/shell/web/app.js:2578` — `var response = await post('/asr/transcribe', {...});`
* `config/default.toml:258` — `worker_timeout_seconds = 10800.0`. The pipeline is designed for runs
  of up to **three hours**; the bridge that carries them gives up after **one minute**.

### Reproduction (executed 2026-08-05, in the scratchpad, touching nothing)

A stub loopback HTTP server that answers after 65 s was driven through the real
`ShellApi._send()`:

```
_PROXY_TIMEOUT_S      = 60.0
server response delay = 65.0s
elapsed               = 60.0s
envelope              = {'ok': False, 'status': 0, 'error': 'TimeoutError: timed out'}
```

### Steps to reproduce in the product

1. `python -m mom_igd shell --data-dir "D:\MoM-IGD-Models-Phase4"`.
2. Record a meeting of **3 minutes or longer** (this is exactly step D.1 of
   `docs/phase-4-manual-acceptance.md`).
3. Open the transcription panel, run preflight, press **Proses transkripsi**.

### Expected behaviour

The panel shows the run completing, renders the stage list, cost card and transcript.

### Actual behaviour

At exactly 60 seconds the bridge raises `TimeoutError`. `app.js` `run()` then executes
`busy = false; stopPolling(); stopElapsed();`, prints `Gagal` in the pill and shows
`TimeoutError: timed out` as the error. **The pipeline keeps running to completion in the
uvicorn threadpool** and writes a correct transcript, but the UI has stopped polling and will
never learn about it. A second press of the button returns HTTP 409 `AsrBusyError`, which reads
as a second, different failure.

### Why the automated suite did not catch it

`tests/test_asr_ui_contract.py` is entirely **static string assertions** against `index.html`,
`app.js` and `app.css`. No test drives `ShellApi` against the API, and no test asserts anything
about `_PROXY_TIMEOUT_S`. Confirmed by `grep` over `tests/`: the only matches for `timeout` are
`busy_timeout_ms`, worker timeouts and thread joins.

### Impact

The GUI transcription workflow — the primary Phase 4 deliverable — is unusable for any real
meeting. Measured RTF is 0.31 end to end (`docs/benchmarks.md`), so the 60 s ceiling is reached
at roughly **190 seconds of audio**; with pass 2 active the project's own end-to-end run measured
RTF 1.18, which reaches it at about **51 seconds of audio**. The documented manual acceptance
procedure (Part C, 60–90 s) may pass; Part D (3–5 minutes) will not. A 30-minute meeting needs
about 6.4 minutes of pipeline time.

There is **no data loss** — the transcript is written correctly — which is why this is P1 and
not P0. The failure mode is a false report of failure.

### Affected files

`mom_igd/shell/launcher.py`, `mom_igd/shell/web/app.js`, `mom_igd/api/asr_routes.py`

### Workaround

Run the pipeline from the CLI instead:
`python -m mom_igd asr transcribe <uuid> --data-dir "D:\MoM-IGD-Models-Phase4"`, then press
**Muat transkrip tersimpan** in the panel. The CLI has no proxy in the path.

### Direction of the fix (not applied)

The cleanest fix matches what the code already claims to do. `mom_igd/api/asr_routes.py`'s own
module docstring says *"The GUI polls `GET /asr/status` for progress rather than holding a
request open"* — but the POST does hold it open. Make that true:

* `POST /asr/transcribe` starts the run on a background thread and returns `202 Accepted`
  immediately with the recording UUID. `AsrService` already owns a `TranscriptionHandle` with
  `finished`, `result` and `error`, and `/asr/status` already reports `last_result`; the page
  already polls it every 1200 ms.
* `app.js` `run()` then stops awaiting completion and lets the poll loop drive the UI to the
  terminal state.
* Keep the synchronous behaviour for the CLI, which calls `AsrService.transcribe()` directly.

A larger `_PROXY_TIMEOUT_S` is **not** an adequate fix: there is no value that is both long enough
for a three-hour meeting and short enough to detect a dead backend.

### Regression tests required

1. An integration test that starts the real backend (as `tests/test_server_lifecycle.py` does),
   points a real `ShellApi` at it, and asserts that a transcription that outlives
   `_PROXY_TIMEOUT_S` still reaches a terminal state in the UI contract.
2. A unit test asserting `POST /asr/transcribe` returns without waiting for the pipeline.
3. A static assertion that no path on `ALLOWED_POST_PATHS` can block longer than
   `_PROXY_TIMEOUT_S` by design (i.e. that `/asr/transcribe` is asynchronous).

---

## MOM-BUG-002 — The acceptance preflight's production-root guard is bypassed by three path spellings

| | |
|---|---|
| **Category** | `CONFIRMED_BUG` |
| **Severity** | **P2** |
| **Subsystem** | `scripts/phase4_acceptance_preflight.ps1` |
| **Confidence** | **High — the comparison was evaluated directly during this audit** |
| **Suggested owner** | Documentation/Release Engineering workstream |

### Evidence

`scripts/phase4_acceptance_preflight.ps1:105-107`:

```powershell
$normalisedTarget     = $DataDir.TrimEnd('\', '/')
$normalisedProduction = $ProductionRoot.TrimEnd('\', '/')
if ($normalisedTarget -ieq $normalisedProduction) { ... exit 2 }
```

The guard is a **string** comparison. `mom_igd/paths.py:_normalise()` (lines 97-121) resolves the
same value with `Path(...).resolve()` and `os.path.normpath`, so the Python side accepts spellings
the guard does not recognise.

### Reproduction (executed 2026-08-05 — string comparison only; production was never targeted)

```
D:\MoM-IGD-Data          -> refused=True
D:\MoM-IGD-Data\         -> refused=True
d:\mom-igd-data          -> refused=True
D:/MoM-IGD-Data          -> refused=False
D:\MoM-IGD-Data\.        -> refused=False
D:\.\MoM-IGD-Data        -> refused=False
```

### Expected behaviour

`-DataDir` naming the production root in any spelling exits 2 with `REFUSED`.

### Actual behaviour

`-DataDir 'D:/MoM-IGD-Data'` (and two other spellings) passes the guard and the script proceeds
to run `db version`, `db verify`, `doctor`, `asr models`, `asr verify`, `audio devices` and
`asr smoke` against the production data root.

### Impact

Bounded but real. Every command the script runs is read-only — none migrates, provisions or
deletes — so no destructive change occurs. What does happen: the production SQLite database is
opened, creating/updating `mom_igd.db-wal` and `mom_igd.db-shm`, and the operator receives a
`READY FOR MANUAL FUNCTIONAL TESTING` verdict that appears to validate production. The script's
own documentation and 65 tests present the guard as absolute; a guard that is advertised as
absolute and is not is worse than a documented soft warning.

### Affected files

`scripts/phase4_acceptance_preflight.ps1`, `tests/test_acceptance_preflight_script.py`

### Direction of the fix (not applied)

Normalise before comparing, e.g. `[System.IO.Path]::GetFullPath()` on both sides (which collapses
`/`, `\.` and `\.\`), and additionally refuse any target that *contains* or *is contained by* the
production root.

### Regression tests required

Extend `tests/test_acceptance_preflight_script.py` with a data-driven case list covering
`D:/MoM-IGD-Data`, `D:\MoM-IGD-Data\.`, `D:\.\MoM-IGD-Data`, `D:\MoM-IGD-Data\..\MoM-IGD-Data`
and a UNC/extended-length spelling. Assert `GetFullPath` (or equivalent) appears in the guard.

---

# 2. Likely bugs

## MOM-LIKELY-001 — `_await_queue_drain` violates its own precondition on the timeout path

| | |
|---|---|
| **Category** | `LIKELY_BUG` |
| **Severity** | **P2** |
| **Subsystem** | `mom_igd/audio/session.py` (Phase 2 capture) |
| **Confidence** | Medium-high on the precondition violation; not reproduced, because it needs a >10 s writer stall |
| **Suggested owner** | Audio/ML workstream |

### Evidence

`mom_igd/audio/session.py:498-531`:

```python
def _await_queue_drain(self, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(self._queue) == 0 and self._writer_idle.is_set():
            return True
        if not self.writer_alive():
            break
        time.sleep(_QUEUE_POLL_SECONDS / 2.0)
    return self._drain_into_writer()          # <-- also reached when the deadline expires
```

`_drain_into_writer`'s own docstring (line 519-521) states: *"Only safe once the writer thread has
exited, or when it never ran."* On the deadline path the writer thread **is still alive**.

Both `pause()` (line 318) and `stop()` (line 381) call `_await_queue_drain()` while the writer
thread is running.

### Failure scenario

A disk stall or antivirus scan makes the writer take longer than 10 s to drain the (5 s capacity)
queue during `stop()`. The controlling thread then pops and writes the remaining blocks itself
while the writer thread is still popping and writing. `_consume` takes `_writer_lock`, so
individual block writes are atomic — but the **order** is not guaranteed: block N+2 written by
the caller can land before block N+1 written by the writer thread. The chunk still hashes and
verifies (it is hashed after the fact), so the corruption would be silent and would only surface
as scrambled audio in the transcript.

### Impact

Low probability, high consequence: silently reordered PCM inside one chunk of a real meeting,
with a manifest that verifies.

### Affected files

`mom_igd/audio/session.py`

### Direction of the fix (not applied)

On the deadline path, do not drain from the caller. Either escalate (set `_stop_event`, join the
writer thread with its own timeout, and only then drain), or record a gap and abandon the
remaining blocks — which matches the phase's "a gap is recorded, never filled" rule better than
writing them out of order.

### Regression test required

A test with a `ChunkWriter` whose `write()` blocks on a controllable event, asserting that
`stop()` never calls `_drain_into_writer()` while `writer_alive()` is true.

---

# 3. Risks (not yet confirmed bugs)

## MOM-RISK-001 — The voiceprint master key has no backup, escrow or export path

| | |
|---|---|
| **Category** | `RISK` · **P1** · Security/Privacy |
| **Evidence** | `mom_igd/enrollment/keys.py:287-329` (`load()` never re-creates); `mom_igd/paths.py:236` (`backups_dir` exists); `grep` finds **no writer** to `backups/` anywhere in `mom_igd/` |

DPAPI binds the key to one Windows user on one machine. A profile rebuild, a machine replacement,
or loss of `keys/voiceprint_master.dpapi` makes every sealed voiceprint permanently unreadable —
the code says so explicitly and refuses to mint a replacement, which is correct. What does not
exist is any backup or escrow mechanism, and `<data_root>/backups` is created but never written.
Today the exposure is nil (production holds **0 voiceprints**, verified below), but it must be
resolved before the first real enrollment. Owner: Security/Privacy. Phase 11 owns the mechanism;
the decision (escrow? re-enrolment as the accepted recovery? both?) is needed earlier.

## MOM-RISK-002 — Transcription does not yield when a recording starts

| | |
|---|---|
| **Category** | `RISK` · **P2** · Concurrency/Performance |
| **Evidence** | `mom_igd/asr/service.py:149-166, 399-405`; `grep -i 'nice(\|priority\|SetPriorityClass'` over `mom_igd/` returns **no process-priority call anywhere** |

**This is the explicit question the audit brief asked.** The answer, from the code:

* Transcription checks `active_capture()` **once, at start** (`service.py:399`). If a capture is
  live it refuses with `RecordingInProgressError` → HTTP 409.
* If a recording starts **while** a transcription is running, nothing happens. The run does not
  yield, pause, cancel or lower its priority. It continues at normal Windows priority with
  `pass1_cpu_threads = 12` / `pass2_cpu_threads = 12` on a 12-physical-core machine, and a
  measured peak worker RSS of up to 1 910 MiB.
* The asymmetry is deliberate and documented (`asr/service.py:11-15`): a recording must never be
  refused. That decision is sound. The unmeasured part is the consequence.

**What is unknown:** no test and no benchmark has measured capture quality — dropped frames,
queue high-water mark, xruns — while a pass-2 decode saturates all cores and holds ~1.9 GB on a
16 GB machine that `doctor` currently reports as having 1.8–2.6 GB free. The capture path has
real mitigations (a 5-second bounded queue that drops and counts rather than blocking, a writer
thread, and a `degraded` flag on the recording), so loss would be *recorded* rather than silent —
but it would still be loss.

**Classified as a risk, not a bug**, because it has not been reproduced. It must be measured
before any pilot. See MOM-GAP-006.

## MOM-RISK-003 — A killed transcription leaves a `BUILDING` transcript row forever

| | |
|---|---|
| **Category** | `RISK` · **P2** · Data integrity |
| **Evidence** | `mom_igd/asr/store.py:267-309` opens the revision `BUILDING`; `mom_igd/asr/pipeline.py:756-779` only writes `FAILED`/`CANCELLED` from an **exception** path; `grep 'BUILDING'` over `mom_igd/` finds no recovery, no sweeper and no `doctor` check |

Kill the process (Task Manager, power loss) mid-run and the row stays `BUILDING`, `is_active = 0`
for ever. The operational impact is small — `list_transcribable` LEFT JOINs on `is_active = 1`, so
the stale row is invisible and re-running simply creates revision N+1 — but the rows accumulate,
nothing reports them, and Phase 7/8 evidence queries will have to know to ignore them.
Contrast with the audio side, which *does* have `audio_stale_recordings` in `doctor` and an
`audio recover` command.

## MOM-RISK-004 — `transcribe` does not re-check free disk

| | |
|---|---|
| **Category** | `RISK` · **P2** · Data integrity |
| **Evidence** | `mom_igd/asr/service.py:301-325` checks `free_gb >= 2.0` in `preflight()` only; `transcribe()` (line 371) checks the worker slot and the active capture, not disk |

The GUI always runs preflight first (the button is disabled until it passes), so the GUI path is
covered. The CLI (`asr transcribe`) and a direct `POST /asr/transcribe` are not. A 16 kHz mono
working copy is ~115 MB/hour and is written before anything else; a three-hour meeting on a full
volume fails partway through normalisation.

## MOM-RISK-005 — PID reuse can wedge the single-recording lock

| | |
|---|---|
| **Category** | `RISK` · **P2** · Concurrency |
| **Evidence** | `mom_igd/audio/service.py:261-273` — `_owner_alive()` returns `psutil.pid_exists(pid)` |

If the PID recorded in `temp/recording.lock` has been recycled by an unrelated process, the lock
reads as live and `acquire()` refuses with *"Another recording is already in progress"*. The
operator cannot record and there is no documented remedy, no `audio unlock` command, and no
mention in the README troubleshooting table. Probability is low (Windows PID reuse plus a stale
lock file plus a crash), consequence is a blocked meeting.
Mitigation direction: also record the process start time or a boot id in the lock file and require
both to match.

## MOM-RISK-006 — `pyproject.toml` does not declare the Phase 4 runtime dependencies

| | |
|---|---|
| **Category** | `RISK` · **P2** · Packaging |
| **Evidence** | `pyproject.toml:39-46` lists 6 dependencies and stops at `cryptography`; its comment at lines 34-37 still says *"Deferred to their implementation phases (must NOT appear yet): faster-whisper, ctranslate2, … av … numpy"*. `requirements.txt:21-22` says *"DIRECT (8): … faster-whisper, av"*. `mom_igd/offline_policy.py:127-160` records that these **left** the deferred list in Phase 4. |

`requirements.txt` is correct and is what the developer workflow uses, so nothing is broken today.
But `pyproject.toml` is the packaging metadata: anything built from it (`pip install .`, and the
PyInstaller work planned for Phase 11) produces an application that cannot transcribe, and the
stale comment actively tells a reader that faster-whisper must not be present.

## MOM-RISK-007 — The production migration 3 → 5 has no rehearsed plan and nothing to roll back to

| | |
|---|---|
| **Category** | `RISK` · **P2** · Data |
| **Evidence** | Verified from a **copy** of the production database (see §6): `user_version = 3`, migrations 1–3 applied, `integrity_check ok`, `foreign_key_check` clean. `mom_igd/db/migrator.py:19-20` — *"There is no production downgrade path … Recovery is by restore from `<data_root>/backups`"*. Nothing writes to `backups/`. |

Migrations 0004 and 0005 are additive and transactional, so the risk is modest. But the stated
recovery mechanism (restore from backup) does not exist, and the migration has never been
rehearsed against a copy of the real database. Two production meetings and 5 chunks are at stake.

## MOM-RISK-008 — Stale capability booleans in two API responses

| | |
|---|---|
| **Category** | `RISK` · **P3** · Correctness of reporting |
| **Evidence** | `mom_igd/api/routes.py:195-203` — `/internal/ready` returns `audio_capture: False, asr: False`, with the comment *"Phase 1 implements none of these"*. `mom_igd/audio/service.py:1046-1052` — `/audio/recordings/status` returns `capabilities.transcript: False`. |

Both are wrong: Phase 2 capture and Phase 4 ASR are implemented. Nothing consumes these fields
today, which is why it is P3 — but a readiness endpoint that reports capabilities it does not have
is exactly the kind of thing an integrator would trust.

## MOM-RISK-009 — Transcript text crosses the pywebview bridge through JavaScript string interpolation

| | |
|---|---|
| **Category** | `RISK` · **P3** · Security (third-party surface) |
| **Evidence** | `.venv/Lib/site-packages/webview/util.py:239-250` — the result is `json.dumps`'d, then `.replace('\\','\\\\').replace("'","\\'")`, then embedded in a single-quoted JS literal passed to `window.evaluate_js`. |

No defect was found in the application: `app.js` uses `textContent` exclusively (asserted by
`tests/test_asr_ui_contract.py:149`), and `json.dumps` already escapes the dangerous characters.
Recorded because arbitrary meeting speech flows through a third-party escaping routine, so this
becomes something to re-check whenever pywebview is upgraded.

## MOM-RISK-010 — Long-meeting scalability is unmeasured

| | |
|---|---|
| **Category** | `RISK` · **P2** · Performance |
| **Evidence** | `docs/benchmarks.md` — every measurement is on 60 s of synthetic audio; the longest real end-to-end run recorded anywhere is 24 seconds (`docs/phase-4-progress.md:130-147`). |

Unknowns for a two-hour meeting: peak RSS with thousands of regions in one worker payload
(regions cross the process boundary as a list of dicts); `transcript_words` row count (the 24 s
synthetic run alone produced 2 530 word rows); SQLite file growth; the cost of
`sha256_file()` on a ~345 MB working copy at every re-run; and whether `worker_timeout_seconds =
10800` is reached. The architecture doc predicts 1.5–3× meeting duration for post-processing; that
prediction has never been tested.

---

# 4. Technical debt

## MOM-DEBT-001 — Phase 4 runs entirely outside the job state machine — **P2**

`mom_igd/jobs/state_machine.py` is complete, tested (94% coverage) and declares
`PIPELINE_STAGES` including `asr_pass1`, `asr_pass2_selective` and `normalize_terminology`. A
`grep` for `set_stage_status|transition_job|next_pending_stage|save_checkpoint|load_checkpoint`
across `mom_igd/` shows the **only** production caller is `mom_igd/audio/service.py:66`
(`transition_job`). The ASR pipeline never touches `jobs` or `job_stages`:
`AsrService.transcribe()` is called without `job_id` from both the route
(`asr_routes.py:173`) and the CLI, and `TranscriptionPipeline.run(..., job_id=None)` passes it
straight through to a nullable column.

This contradicts two stated invariants — CLAUDE.md's *"`jobs` is the single owner of workflow
state"* and `docs/architecture.md:110`'s *"api → DB **sole writer** of `jobs` / `job_stages`
state"*. The production database confirms it: both jobs sit at state `RECORDED`, and all 20
`job_stages` rows are untouched.

Consequences to plan for, not to panic about: no stage-level progress, no `attempt_count`, no
checkpoint/resume through `save_checkpoint`, and no `QUEUED → PROCESSING → REVIEW_REQUIRED`
lifecycle. Phase 9 (review/approval) and Phase 10 (export of an approved snapshot) both assume
that lifecycle exists. Decide deliberately: either wire Phase 4 into the machine, or amend the
invariant to say `transcripts.status` is authoritative for transcription and the job machine
begins at Phase 7.

## MOM-DEBT-002 — `cli.py` is 62% covered, and the `asr transcribe` handler is largely untested — **P2**

Measured this audit: `mom_igd/cli.py` 1002 statements, **347 missed**, 62% — the largest uncovered
production surface in the tree, and larger than `provision.py` and `faster_whisper_provider.py`
combined. The missing ranges (1609-2117) include the `asr transcribe`, `asr transcript` and
`asr revisions` handlers and much of the participant command surface. `docs/phase-4-progress.md`'s
coverage reconciliation (§4) explains `provision`, `faster_whisper_provider`, `smoke`, `benchmark`
and `worker` but does not mention `cli.py` at all. The CLI is the operator's primary interface and
the documented workaround for MOM-BUG-001.

## MOM-DEBT-003 — No CI, no lint configuration, no type checking — **P2**

Verified absent: `.github/`, `.gitlab-ci.yml`, `azure-pipelines.yml`, `.ruff.toml`, `ruff.toml`,
`setup.cfg`, `mypy.ini`, `.pre-commit-config.yaml`, `tox.ini`. `pyproject.toml` contains no
`[tool.ruff]`, `[tool.mypy]` or equivalent. The code carries `# noqa: BLE001`, `# noqa: S310` and
`PLC0415` markers, so a linter was clearly used at some point — but its configuration is not
committed and nothing runs it. With one author this was survivable; with a team it is not. The
10-minute test suite also has no automated trigger.

## MOM-DEBT-004 — GUI tests assert on file text, never on behaviour — **P3**

`test_asr_ui_contract.py`, `test_static_ui.py`, `test_participants_ui.py`, `test_revoke_modal.py`
and `test_audio_api_ui.py` read `index.html` / `app.js` / `app.css` as strings and assert
substrings and regexes. They are genuinely valuable — the `[hidden]` cascade test encodes a real
past defect — but they cannot catch a runtime failure, and MOM-BUG-001 is the proof. There is no
test anywhere that drives `ShellApi` against a running backend.

## MOM-DEBT-005 — Documentation drift — **P3**

* `docs/architecture.md:218` places `asr_pass2_selective` in Phase **5**;
  `mom_igd/jobs/state_machine.py:157` says Phase **4**. The code is right.
* `docs/architecture.md:416-424`'s recording-lifecycle diagram shows `SELECTED`, `PREFLIGHT_OK`,
  `CALIBRATED`, `STARTING`, `COMPLETED` — none of which exist in `RecordingLifecycle`
  (`mom_igd/audio/service.py:84-97`).
* `AGENTS.md:87` — *"Edit an applied migration (`0001`–`0004`)"*; migration `0005` is applied.
* `docs/phase-4-progress.md` §4 omits `cli.py` from the coverage reconciliation (MOM-DEBT-002).

## MOM-DEBT-006 — Deprecated Starlette constant in two enrollment routes — **P3**

`mom_igd/api/enrollment_routes.py:344` and `:508` use `HTTP_422_UNPROCESSABLE_ENTITY`, which
raises `StarletteDeprecationWarning` in the test run. `mom_igd/api/asr_routes.py:86` already uses
`HTTP_422_UNPROCESSABLE_CONTENT`. Three of the 14 warnings in the suite come from our own code;
the rest are `starlette.testclient` and anyio internals.

## MOM-DEBT-007 — Misleading variable name in a pass-2 skip message — **P3**

`mom_igd/asr/pipeline.py:860` — `longest = min((region.duration_ms for region in
selection.flagged), default=0)`. The output text is correct (*"the shortest is …"*); only the
name is wrong. Worth fixing because the surrounding code is otherwise unusually careful.

## MOM-DEBT-008 — Test doubles ship inside the production package — **P3**

`mom_igd/enrollment/keys.py:332` (`FakeKeyProtector`), `mom_igd/enrollment/fake_provider.py` and
`mom_igd/asr/fake_provider.py` are importable from the shipped package. Verified that **no**
configuration key or environment variable can select any of them (`grep` for
`getenv|environ.get` across `mom_igd/` finds only `MOM_IGD_DATA_DIR`, `MOM_IGD_TRACEBACK`, the
Hugging Face offline flags and the ONNX telemetry flag), and `EnrollmentService._resolve_provider`
(`service.py:393-413`) explicitly refuses a provider whose `is_test_double` is true. Recorded only
as packaging surface to strip in Phase 11.

## MOM-DEBT-009 — No governance files — **P3**

Absent: `LICENSE` (deliberate — `pyproject.toml:12-13` records that no licence has been chosen),
`CONTRIBUTING.md`, `CHANGELOG.md`, `CODEOWNERS`, issue and PR templates, `.editorconfig`. The
deliberate absence of `LICENSE` should be a decision item before the repository is shared, not a
silence.

---

# 5. Expected warnings (not defects)

These are correct behaviour at this phase. They are listed so a new team member does not open a
ticket for them.

### 5.1 `doctor` warnings

Observed twice during this audit against `D:\MoM-IGD-Models-Phase4`: **24 PASS / 11 WARN / 0 FAIL**
(direct run) and **25 PASS / 10 WARN / 0 FAIL** (inside the preflight, minutes later). The one-check
difference is `ram`, which crossed the 2048 MB threshold between the two runs because Docker/WSL
was holding 3 149 MB. `ram` is a **warning threshold only** and can never become a FAIL
(`mom_igd/diagnostics/doctor.py:254-267`), so advancing `CURRENT_PHASE` would not change it. The
documented figure of 25/10/0 is reproducible; it is not a regression.

| Key | Why it is expected |
|---|---|
| `ram` | Docker/WSL resident; close them before a long run |
| `model_registry` | 0 models declared — correct, no speaker-embedding model is approved (ADR-0005) |
| `optional_dependencies` | 4 of 7 future dependencies absent (openvino, pyannote.audio, torch, …) |
| `usb_conference_microphone` | No USB device; internal array only |
| `voiceprint_key_store` | No master key yet — correct before the first enrollment |
| `consent_text` | Version `1.0-draft`, not legally reviewed → see MOM-GAP-005 |
| `speaker_embedding_model` | None declared → see MOM-GAP-002 |
| `participant_registry` | No participant registered |
| `production_voiceprints` | 1 meeting, empty roster |
| `docker_wsl_presence` / `docker_wsl_memory` | Informational only |

### 5.2 Preflight warnings

**9 PASS / 4 WARN / 0 FAIL**, exit 0, verdict `READY FOR MANUAL FUNCTIONAL TESTING`. The documented
figure is 10 PASS / 3 WARN — the same `free_ram` check moved, for the same reason. Warnings:
`free_ram`, `doctor:usb_conference_mic`, `doctor:consent_text`, `audio_devices`.

### 5.3 Test-suite warnings

14 warnings, all `StarletteDeprecationWarning`: 1 from `starlette.testclient` + httpx, 10 from
anyio's dispatch of the deprecated 422 constant, 3 from our own `enrollment_routes.py`
(MOM-DEBT-006). No `DeprecationWarning` from `mom_igd.*` is raised — `filterwarnings =
["error::DeprecationWarning:mom_igd.*"]` in `pyproject.toml` would have failed the run.

### 5.4 Coverage below target

84% against a 90% target, honestly reported in `docs/phase-4-progress.md` §4 with a
module-by-module reconciliation. Four modules that need a network connection or a 464 MiB model
account for most of the gap. Not a defect; see MOM-DEBT-002 for the part that is not explained.

### 5.5 Docker and WSL present on the machine

Informational. `docker`, `docker-compose` and `wsl` are on `PATH` and 21 processes hold 3 149 MB.
Neither is a production dependency and nothing in the application uses them.

---

# 6. Acceptance gaps

## MOM-GAP-001 — Real-speech accuracy has never been measured — **P1**

No WER (clean or far-field), no reference transcript, no evaluation corpus.
`docs/benchmarks.md:112-116` marks every accuracy row `N/A — PENDING`. Every number that exists
was measured on **synthetic audio** and is a measurement of engine throughput only. The
pass-1/pass-2 beam split (1 and 5) was chosen on throughput evidence alone and is explicitly
provisional. **This is the single reason `CURRENT_PHASE` stays at `3`, and the decision is
correct.** Blocker for: declaring Phase 4 complete, any production claim, and the release gate.

## MOM-GAP-002 — No speaker-embedding model has been selected — **P1**

`docs/phase-3-speaker-model-selection.md:3-24`: 0 models declared, 0 artefacts present, 0
candidates evaluated, 0 benchmarks run, 0 licences reviewed, and **no real voiceprint has ever
been produced by this build**. `mom_igd/enrollment/provider.py:1-8` refuses production enrollment
with `ModelUnavailableError` before the microphone is opened — which is the right behaviour, and
it means Phase 3 is functionally blocked, not finished. Blocker for: Phase 6 voice identification,
and therefore for named speakers in a MoM.

## MOM-GAP-003 — Technical-term recall, timestamp accuracy and pass-2 improvement unmeasured — **P2**

The mechanisms exist (`asr bench --manifest`, `merge.text_changed_regions`, glossary
`replacements`) and are tested; nothing has been measured against real speech. The one recorded
observation — "1 region came back different" — was on synthesised formants and is not evidence.

## MOM-GAP-004 — No USB conference microphone; Phase 2 production acceptance not granted — **P2**

`doctor` and the preflight both report internal arrays only. `README.md:533` records that Phase 2
production acceptance (3 × 60 min on real hardware, CPU < 5%, RSS < 300 MB) has not been granted.
`doctor --production` is the gate and has not been passed. The internal Intel Smart Sound array
applies beamforming that suppresses speakers not facing the laptop — acceptable for a functional
test, not for accuracy evidence.

## MOM-GAP-005 — Consent text is a draft; no legal review, no DPIA — **P2**

`doctor` reports `consent_text` version `1.0-draft`. Voiceprints are *data pribadi bersifat
spesifik* under UU PDP No. 27/2022. Required before any real enrollment: legal/compliance sign-off
on the wording, a stated retention period, a documented erasure path, and a DPIA. Retention
enforcement does not exist in code (Phase 11).

## MOM-GAP-006 — Manual GUI acceptance has not been run — **P2**

`docs/phase-4-manual-acceptance.md` Parts C and D exist and are good, but no result form has been
returned. Note that **Part D will hit MOM-BUG-001**; fix that first or the operator will spend the
session diagnosing a known defect. Add to the procedure: capture quality while a transcription
runs (MOM-RISK-002) and a long-meeting resource run (MOM-RISK-010).

## MOM-GAP-007 — No backup, no restore, no retention enforcement, no packaging — **P2**

All Phase 11. `backups_dir` is created and never written; there is no installer, no offline
wheelhouse (`requirements.txt:19` — deferred), no air-gapped install procedure and no recovery
drill. Together with MOM-RISK-001 and MOM-RISK-007 this is the largest cluster of missing
operational safety.

## MOM-GAP-008 — Production is still schema 3 and the migration is unplanned — **P2**

Verified from a copy: `user_version = 3`, migrations 1-3, `integrity_check ok`,
`foreign_key_check` clean, 2 meetings, 2 recordings (both `RECORDED`), 5 chunks, 26 audit events,
0 participants, 0 voiceprints, 0 consent events. Migrating to 5 needs a written plan, a rehearsal
against a copy, a backup taken first, and a rollback decision. Note that migration 0004 will
backfill both existing meetings with capacity **9** from the column DEFAULT — intended, but worth
stating in the plan.

## MOM-GAP-009 — No long-meeting resource test — **P3**

See MOM-RISK-010. The longest end-to-end pipeline run on record is 24 seconds.

---

# 7. Areas examined and found sound

Recorded so the team knows where **not** to spend review effort, and so a later regression is
visible against a baseline.

| Area | Finding |
|---|---|
| Loopback enforcement | Two independent layers: validated bind address + `LoopbackHostMiddleware` Host allowlist. `smoke` step 8 proves a non-loopback `Host` gets 403. |
| Session token | 256-bit, memory-only, `__str__`/`__repr__`/`__format__` redact, `__reduce__` raises, constant-time compare, refused in a query string with 400 even when correct. `smoke` steps 5-7 and 10 all pass. |
| Route authorisation | All four routers (`/audio`, `/enrollment`, `/asr`, protected) carry `Depends(require_session_token)` at router level. Only `/health` and `/version` are public, and both disclose booleans only. |
| Shell proxy allowlist | Exact-match sets plus anchored UUID templates per HTTP method; a `?` or `#` in the path is refused outright; no `/asr/*` wildcard; `provision` unreachable. |
| SQL injection | Every query uses bound parameters. The three f-string SQL constructions (`store.update_transcript`, `state_machine`, `asr/service.active_capture`) interpolate only keys validated against a frozen allowlist or `?` placeholder counts. |
| Path traversal | `paths.py` owns every runtime path; UUID shape is enforced with a regex before it becomes a filename; `installed._escapes_store` checks POSIX **and** Windows flavours (a real past defect). |
| Offline enforcement | `assert_offline_environment()` uses assignment, not `setdefault`, and deletes inherited HF tokens. Dependency denylist, endpoint rule and bind rule all tested. `asr smoke` records **zero outbound attempts**. |
| Model integrity | Catalogue → installed registry → resolver, three distinct layers; readiness requires a load-and-decode probe, not a directory scan; the manifest digest is re-derived on every read. `asr verify` re-hashed **every byte** of 2 011 MiB during this audit and matched. |
| Master audio immutability | Normalisation writes a new file under `working/`; a test hashes every chunk before and after a full run. |
| Audit trail | Append-only, hash-chained, state change and event in one transaction via `maybe_transaction`. `db verify` reports the chain intact. The tail-truncation limit is documented and asserted. |
| Voiceprint crypto | AES-256-GCM with AAD binding the envelope to voiceprint UUID, participant id, schema and model identity; fresh 96-bit nonce per seal; atomic write with `fsync`; DPAPI with a domain separator and `CRYPTPROTECT_UI_FORBIDDEN`. |
| Dynamic participant capacity | No hidden 9 or 15 anywhere. Capacity is per-meeting (`meetings.participant_capacity`), the DB invariant is only `>= 1`, the ceiling is configuration (default 50), `settable_capacity_bounds()` is the single source of truth for API, service and UI, and a grandfathered capacity is never clamped. |
| Capture concurrency | Callback copies and enqueues only; bounded queue drops and counts; one writer thread owns the writer and meter; `_writer_idle` distinguishes "queue empty" from "writer finished". (One precondition gap — MOM-LIKELY-001.) |
| No speaker in Phase 4 | No column in migration 0005, and `validate_transcription` rejects any result carrying one. `asr smoke` step 9 asserts it. |
| Double-click / duplicate submit | The recording panel guards Start before the first `await`; the transcription panel uses a `once()` re-entry guard; the service is idempotent and the DB has a partial unique index. |
| Polling | Every panel chains its next poll from the previous one — no repeating timers, so no overlap. Two of the three panels also stop on `pagehide`/`beforeunload`. |
| DOM injection | `app.js` uses `textContent` exclusively; no `innerHTML`, `insertAdjacentHTML`, `document.write`, `fetch`, `XMLHttpRequest`, `WebSocket` or `EventSource`. Asserted statically. |
| Repository hygiene | 188 tracked files, no binaries, no recordings, no models, no secrets. `.gitignore` covers `*.wav`, `*.db*`, `/recordings/`, `/voiceprints/`, `/keys/`, `models/**`, `.env*`, `config/local.toml`, with runtime-data patterns correctly anchored. |
| Production data root | Byte-identical before and after this audit — same inventory, sizes, mtimes and SHA-256. |

---

## Appendix — commands run during this audit

All read-only. Every ASR command targeted `D:\MoM-IGD-Models-Phase4`, never the production root.

| Command | Result |
|---|---|
| `git status --short` / `git diff` / `git diff --cached` | clean, no output |
| `git log --oneline -15` | 5 commits, HEAD `4674ea4` |
| `python -m compileall -q mom_igd tests` | exit 0 |
| `python -m pip check` | No broken requirements found |
| `python -m pytest -q --cov=mom_igd --cov-report=term-missing` | **2263 passed, 0 failed, 0 skipped**, 14 warnings, 602 s, **84%** |
| `python -m mom_igd smoke` | **PASS 11/11** |
| `python -m mom_igd audio smoke` | **PASS 9/9** |
| `python -m mom_igd doctor --json` | **24 PASS / 11 WARN / 0 FAIL**, exit 0 |
| `python -m mom_igd asr models` | 2 provisioned, both `OK` |
| `python -m mom_igd asr verify` | every byte re-hashed, both models match |
| `python -m mom_igd asr smoke` | **PASS 11/11**, zero outbound attempts |
| `scripts\phase4_acceptance_preflight.ps1` | **9 PASS / 4 WARN / 0 FAIL**, exit 0, READY |
| SHA-256 of migrations `0001`–`0005` | recorded in the handoff audit, §3 |
| Production DB inspected **from a copy** | `user_version=3`, integrity ok, FK clean |
