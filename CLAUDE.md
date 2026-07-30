# CLAUDE.md — working rules for AI agents in this repository

Read this before changing anything. These are hard constraints derived from the
product requirements and the Phase 0 audit, not style preferences.

## What this project is

A **fully offline** desktop application that turns a recording of an in-person
meeting (one room, Indonesian with English technical terms) into a reviewed,
structured Minutes of Meeting.

Each meeting has its own participant roster with a configurable capacity. Nine is
the **default**, kept for backward compatibility -- it is not a limit of the
product. The participant directory itself is not size-limited, and the default
safety ceiling is 50 participants per meeting. The system is never described as
"unlimited".

**Current phase: 3 — participants, biometric consent, voice enrollment.** See
`docs/architecture.md` for the full roadmap, `docs/phase-2-audio-capture.md` for
the capture engine, `docs/phase-3-participants-enrollment.md` for enrollment, and
`README.md` for what exists.

## Non-negotiable constraints

1. **No cloud. Ever.** No cloud SDK, no hosted inference API, no telemetry, no
   crash reporter that phones home, no auto-update, no CDN, no remote font, no
   external script or stylesheet. There is **no cloud fallback** — the product is
   useless if it silently depends on a network.
2. **No CUDA.** The target GPU is Intel Iris Xe. `torch.cuda.is_available()` is
   `False` on the production device. Reject any dependency, model artefact or
   code path that assumes an NVIDIA GPU.
3. **No Docker or WSL2 in production.** They are informational only. Never start,
   stop or configure them, and never create or modify `.wslconfig`.
4. **Never change system configuration.** No firewall rules, no registry writes,
   no global environment variables, no system-wide package installs. Dependency
   installs go into `.venv` only.
5. **Python 3.12 only.** Not 3.14 (AI wheels are unavailable), not the Microsoft
   Store shim (filesystem redirection breaks packaging and native loading).
6. **One heavy model at a time**, in its own short-lived worker process.
   `resources.max_heavy_workers` above 1 is rejected by configuration validation.
7. **Loopback only.** The API binds `127.0.0.1`. Never `0.0.0.0`, never a LAN
   address. There is no configuration option to change this.
8. **The legacy project `D:\Aldy\Project APP VTT` is not a code source.** It is a
   cloud (`google-genai`) application — precisely the architecture this project
   rejects. Do not read, copy, modify or delete anything in it. Its general
   FastAPI + pywebview + SQLite *shape* may be considered as a reference; its
   code, database, configuration and credentials may not.
9. **Never commit** model binaries, meeting recordings, voiceprints, generated
   MoM documents, runtime databases, logs or secrets.
10. **Do not print or echo** the contents of `.env`, key, token or certificate
    files.
11. **Never open the microphone without an explicit user action.** Not on import,
    not at application startup, not in `doctor`, not in a test, and not during
    device discovery. Enumerating devices must not open a stream. The one
    deliberate exception is `audio probe`/`audio calibrate`, which exist *because*
    the operator asked to test the microphone.
12. **Never change an audio device's settings.** No gain, no AGC, no
    enhancements, no default-device change, no registry write. The Windows
    endpoint registry is read **read-only**, for transport evidence only. When a
    level is wrong, tell the operator which setting to adjust.

## Scope discipline

The single most important rule: **do not implement a future phase.**

Phase 2 must not contain VAD, ASR, diarization, voice enrollment, voice
identification, speaker labelling, LLM calls, MoM generation, exporters, action
tracking, encryption at rest, a consent workflow, retention enforcement, model
downloads, or OpenVINO installation/benchmarking. It must not resample, produce
a 16 kHz working copy, or write FLAC — those belong to Phase 4
(`normalize_audio`).

Do not scaffold future modules as empty placeholders either. An empty package
invites a half-implementation. Future structure belongs in
`docs/architecture.md` until the phase that builds it.

If a task seems to require crossing a phase boundary, stop and say so rather than
quietly widening scope.

## Architectural invariants to preserve

* **`mom_igd/paths.py` owns every runtime path.** Never build a runtime path by
  hand elsewhere. Never create a directory as an import side effect — only
  `RuntimePaths.ensure()`, called from an explicit initialisation path, may do it.
* **`jobs` is the single owner of workflow state.** `meetings` has no state
  column on purpose. Do not add one.
* **A state change and its audit event are written in one transaction.** Use
  `mom_igd.db.connection.maybe_transaction`, which joins the caller's transaction
  if one is open. Never call `BEGIN` directly in new code.
* **Never use `sqlite3.executescript` for migrations.** It issues an implicit
  `COMMIT`, which would defeat the transactional guarantee. Use
  `split_sql_statements`.
* **Never edit an applied migration.** Checksums are recorded and verified; add a
  new migration instead.
* **The session token stays in memory.** Never log it, persist it, put it in a
  URL, or hand it to JavaScript. The shell proxies authenticated calls through
  Python (`ShellApi.api_get`) with a closed path allowlist.
* **No global `socket.socket` monkey-patch.** Offline-ness is enforced by the
  dependency denylist, the provider-endpoint rule and the loopback bind rule.
  See ADR-0002 for why patching sockets is rejected.
* **`doctor` must stay import-light, and must work on a bare interpreter.** It
  must not import FastAPI, uvicorn, pywebview or httpx, and must not crash when a
  dependency is missing. `mom_igd/diagnostics/model.py` and
  `mom_igd/diagnostics/bootstrap.py` are **standard-library only** — never add a
  third-party import to either, or `py -3.12 -m mom_igd doctor` breaks on a fresh
  machine. Heavy imports belong inside the subcommand that needs them.

### Phase 2 invariants (`mom_igd/audio/`)

* **The device callback copies and enqueues. Nothing else.** No file I/O, no
  logging, no database access, no lock it can wait on, and no exception may reach
  PortAudio. A blocked callback is a dropped frame in real audio.
* **One writer thread owns `ChunkWriter` and `QualityMeter`.** Any other thread
  that touches them takes `_writer_lock` and waits for `_writer_idle` first. "The
  queue is empty" does not mean "the writer has finished" — it pops, then writes.
* **A gap is recorded, never filled.** Never synthesise silence to cover dropped
  frames or a pause. An invisible gap shifts every downstream timestamp and breaks
  the evidence chain that Phase 8 depends on.
* **The manifest is authoritative; the database mirrors it.** `audio verify`
  reports a divergence — it never silently reconciles one.
* **Never overwrite or delete audio.** The writer refuses to replace an existing
  final chunk; recovery quarantines anything ambiguous instead of deleting it. The
  durability order in ADR-0007 is a contract: metadata before audio, `fsync`
  before hashing, hash from disk, atomic rename, partial removed last.
* **Identify a device by fingerprint, never by PortAudio index.** Indices move
  when a device is replugged. If the selected device is absent, **raise** — never
  fall back to another microphone (ADR-0008).
* **Never assert a transport you have not verified.** `USB` comes from the Windows
  endpoint registry or the answer is `UNKNOWN`.
* **`sounddevice` is imported lazily**, inside the function that needs it, so
  `doctor` and the CLI still work when PortAudio is missing. **NumPy is not a
  dependency** — `RawInputStream` gives bytes, and the meter uses `array`.
* **`audioop` is banned.** It was removed in Python 3.13; using it would block the
  next interpreter upgrade.

### Phase 3 invariants (`mom_igd/enrollment/`)

* **Nine is a default, not a limit.** Roster capacity is stored **per meeting**
  (`meetings.participant_capacity`, migration 0004) and read from the meeting row --
  never recomputed from configuration, which would retune historical meetings. The
  participant *directory* has no size limit at all.
* **The safety ceiling lives in configuration**
  (`[participants].maximum_meeting_participant_capacity`, default 50), not in a
  `CHECK` constraint. The database invariant is only `participant_capacity >= 1`;
  encoding a business ceiling would force a table rebuild to change it.
* **The ceiling is not a validated capability.** Never claim a roster size has been
  proven accurate, and never describe the system as "unlimited".
* **Roster size never gates recording.** Nothing under `mom_igd/audio/` may import
  `mom_igd.enrollment` or the participant module. Capture takes the whole room signal;
  an unenrolled or off-roster voice becomes `UNKNOWN` (Phase 6), never dropped. Do not
  add a reason code meaning "speaker not registered".
* **Lowering a capacity below the roster is refused (`409`)**, never resolved by
  removing anybody.
* **A stored capacity above a since-lowered ceiling is grandfathered.** Never clamp on
  read, never adjust silently, never remove a participant. It may be lowered, not
  raised. `settable_capacity_bounds()` is the single source of truth the API, the
  service and the UI all read (ADR-0013 §6).
* **A new meeting takes the *configured* default**, written explicitly. The `DEFAULT 9`
  on the column exists only so migration 0004 can backfill; relying on it silently
  ignores an operator's configuration.
* **Always pass `config=` to `ParticipantService`.** Without it the service falls back
  to its built-in 9/50 and two runtimes disagree about the same policy.
* **`doctor` counts attendees, not seats, and checks whose voice it is.** Coverage
  joins each active roster member to *that participant's own* live voiceprint. Never
  reintroduce a global count or a "minimum voiceprints" constant (ADR-0013 §7).
* **Identity is the participant UUID.** A display name is never a key, path
  component, filename, URL segment or unique column (ADR-0009).
* **`[hidden]` must keep beating every author `display` rule in `app.css`.** The
  attribute works only through the UA stylesheet, so `display: flex` on a class
  silently defeats `show(node, false)` -- that is what left the revoke dialog on
  screen swallowing every click. The `!important` rule at the top of the file is load
  bearing.

## Doctor classification contract

* `PASS` — required by the current phase, and satisfied.
* `WARN` — optional, informational, or required only in a **future** phase.
* `FAIL` — required by the current phase, and not satisfied.

In Phase 2 a missing AI library, model or OpenVINO installation is still **always
`WARN`, never `FAIL`**. Docker/WSL presence and memory use are **informational
only**.

What changed in Phase 2: the audio backend and a usable capture device are now
**required by the current phase**, so their absence is a `FAIL` in the default
run. A missing *USB* microphone is a `WARN` in the default run and a `FAIL` under
`doctor --production` — that flag is the production gate, and it also requires
recorded calibration evidence.

Exit codes: `0` no FAIL · `1` any FAIL · `2` `--strict` with a WARN.

## Testing rules

* Tests must not require the internet, a microphone, an AI model, Docker or
  OpenVINO, and must not depend on execution order. Audio tests use
  `FakeAudioBackend`; **no test fixture may contain a human voice recording** —
  PCM is generated deterministically.
* Tests must use temporary directories only. The real runtime data directory
  (`D:\MoM-IGD-Data`) is guarded by a session fixture; touching it fails the run.
* Use `use_local_file=False` when loading configuration in a test, so a
  developer's `config/local.toml` cannot change the outcome.
* **Do not weaken a test to make it pass.** If a test and the implementation
  disagree, the current phase's requirements decide which one is wrong.
* **A test that starts a capture session must stop it.** Use the `make_session`
  fixture, which stops every session and asserts no writer thread leaked. One
  leaked thread cascades into unrelated failures several tests later.
* **Pace `FakeStream.pump()`.** Pumping in one burst delivers audio faster than
  real time and legitimately overflows the queue — that is the queue working, not
  a bug. Use the `_pump`/`_await_chunks` helpers: reaching a frame count happens
  before the chunk is finalised.
* `pytest-timeout` is configured (`--timeout=60 --timeout-method=thread`) so a
  deadlock produces a stack trace instead of a hung run. It found a real one.
* Avoid `importlib.reload` on project modules in tests: it rebuilds exception
  classes and breaks `pytest.raises` in unrelated tests later in the session.
  Use a child process instead.
* A test that starts a real server must stop it in a `finally` block. A leaked
  thread cascades into unrelated failures.

## Git rules

* `git init` is allowed if the repository is not initialised.
* **Never run** `git add`, `git commit`, `git push`, `git tag`, branch creation
  or remote configuration unless the user explicitly asks in that message.
* Untracked files are the expected state during a phase.

## Commands

```powershell
.\.venv\Scripts\python.exe -m mom_igd doctor
.\.venv\Scripts\python.exe -m mom_igd doctor --production   # USB mic + calibration gate
.\.venv\Scripts\python.exe -m mom_igd db init
.\.venv\Scripts\python.exe -m mom_igd smoke
.\.venv\Scripts\python.exe -m mom_igd audio devices
.\.venv\Scripts\python.exe -m mom_igd audio smoke           # fake backend, no hardware
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest --cov=mom_igd --cov-report=term-missing
```

Do **not** pipe pytest through `2>&1 | Select-Object` in PowerShell: it wraps every
stderr log line in an `ErrorRecord` and the run appears to hang. Use
`Start-Process` with `-RedirectStandardOutput` if you need the output in a file.

## Style

Match the surrounding code: type hints, `from __future__ import annotations`,
module docstrings that explain *why*, comments only where the reason is not
obvious from the code. Error messages must name the offending value **and** what
would be acceptable — a validation failure should teach the reader how to fix it.
