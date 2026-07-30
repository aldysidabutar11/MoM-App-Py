# MoM-IGD — offline Minutes of Meeting

A fully offline desktop application for producing Minutes of Meeting from
in-person meetings held in one physical room. Native Windows 11, CPU-first, no
cloud API, no CUDA, no Docker in production.

> **Current phase: 3 — participants, biometric consent, voice enrollment.**
> The Phase 1 foundation (configuration, runtime paths, SQLite with migrations, a
> workflow state machine, a loopback-only API, diagnostics, a static shell) is in
> place, and this build **records audio**: device discovery, preflight and
> calibration, chunked PCM16 recording with checksums and a manifest, crash
> recovery, pause/resume, and a recording UI.
>
> There is still deliberately **no ASR, no diarization, no speaker
> identification, no LLM, no MoM generation and no export**, and audio is **not
> encrypted at rest** yet. See [Phase boundaries](#phase-boundaries).

---

## Target device

| | |
|---|---|
| OS | Windows 11 64-bit |
| CPU | Intel Core i7-1260P (12 cores / 16 logical) |
| RAM | 16 GB |
| GPU | Intel Iris Xe integrated — **no NVIDIA GPU, no CUDA** |
| Storage | NVMe SSD |
| Runtime | Native Windows. **Not** Docker Desktop, **not** WSL2 |

Heavy AI processing happens *after* the meeting, one model at a time, in a
short-lived worker process. During a meeting the device only does lightweight
work. See [`docs/architecture.md`](docs/architecture.md).

---

## Setup

### 1. Python 3.12 (required)

This project targets **official Python 3.12** from python.org, installed
**per-user**. Python 3.14 is too new for the AI wheels needed from Phase 4
onwards, and the Microsoft Store distribution applies filesystem redirection and
app-container sandboxing that break PyInstaller packaging and native library
loading.

Verify what you have:

```powershell
py -0p
py -3.12 --version
```

If 3.12 is missing, install it per-user (one-time, requires internet):

```powershell
winget install --id Python.Python.3.12 --exact --scope user
```

Then re-check. The interpreter must resolve under
`%LOCALAPPDATA%\Programs\Python\Python312`, **not** under `WindowsApps`.

### 2. Virtual environment

```powershell
cd D:\Aldy\MoM-IGD
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe --version      # expect Python 3.12.x
```

### 3. Dependencies

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Both lock files pin exact versions. `requirements.txt` is the runtime closure
(5 direct dependencies: `fastapi`, `uvicorn`, `psutil`, `pywebview`,
`sounddevice`); `requirements-dev.txt` adds `pytest`, `pytest-cov`,
`pytest-timeout` and `httpx`. Instructions for refreshing them are in the header
of each file. A hash-pinned offline wheelhouse is deferred to Phase 11.

`sounddevice` arrived with Phase 2 and bundles PortAudio V19.7 — a self-contained
DLL in the wheel, no system-wide install, no driver, no service. It is imported
**lazily**, so every command still works on a machine without it.

**Nothing else may be installed.** No cloud SDK, no AI runtime, and no NumPy,
`soundfile` or `librosa` — the capture path uses raw bytes and the standard
library on purpose ([ADR-0006](docs/adr/0006-capture-format-pcm16-device-native.md)).
`tests/test_offline_policy.py` fails the build if one appears.

---

## Commands

All commands run from the repository root. The `doctor` command is deliberately
lightweight: it imports neither FastAPI, uvicorn nor pywebview, and it works
even when every future-phase dependency is missing.

```powershell
# environment readiness: PASS / WARN / FAIL
.\.venv\Scripts\python.exe -m mom_igd doctor
.\.venv\Scripts\python.exe -m mom_igd doctor --json
.\.venv\Scripts\python.exe -m mom_igd doctor --strict        # exit 2 on any WARN
.\.venv\Scripts\python.exe -m mom_igd doctor --production    # USB mic + calibration gate

# database (the only command that creates the runtime data tree)
.\.venv\Scripts\python.exe -m mom_igd db init
.\.venv\Scripts\python.exe -m mom_igd db version
.\.venv\Scripts\python.exe -m mom_igd db verify              # pragmas, checksums, audit chain

# configuration and model registry
.\.venv\Scripts\python.exe -m mom_igd config show
.\.venv\Scripts\python.exe -m mom_igd registry show

# headless backend smoke test (no GUI, no microphone, no model, no network)
.\.venv\Scripts\python.exe -m mom_igd smoke

# audio capture (Phase 2)
.\.venv\Scripts\python.exe -m mom_igd audio devices          # opens no stream
.\.venv\Scripts\python.exe -m mom_igd audio devices --all     # + rejected, with reasons
.\.venv\Scripts\python.exe -m mom_igd audio probe             # preflight only
.\.venv\Scripts\python.exe -m mom_igd audio probe --open-test # OPENS THE MICROPHONE briefly
.\.venv\Scripts\python.exe -m mom_igd audio calibrate         # OPENS THE MICROPHONE 10-15 s
.\.venv\Scripts\python.exe -m mom_igd audio verify [UUID]     # chunk checksums + manifest chain
.\.venv\Scripts\python.exe -m mom_igd audio recover           # salvage interrupted recordings
.\.venv\Scripts\python.exe -m mom_igd audio smoke             # fake backend, no hardware
.\.venv\Scripts\python.exe -m mom_igd audio bench --minutes 60 --speed 120

# backend in the foreground, loopback only
.\.venv\Scripts\python.exe -m mom_igd serve

# desktop window (blocks until closed)
.\.venv\Scripts\python.exe -m mom_igd shell
```

### Recording a meeting

The microphone is **never opened automatically** — not on import, not at startup,
not by `doctor`, not by `audio devices`, and never in a test. Only `audio probe
--open-test`, `audio calibrate` and an explicit Start open a stream.

```powershell
.\.venv\Scripts\python.exe -m mom_igd audio devices          # 1. see what is available
.\.venv\Scripts\python.exe -m mom_igd shell                  # 2. panel: pick a device
.\.venv\Scripts\python.exe -m mom_igd audio probe            # 3. disk, device, permission
.\.venv\Scripts\python.exe -m mom_igd audio calibrate        # 4. speak; get a verdict
```

Then in the shell's recording panel: type a **meeting title**, run preflight, and
press **Start**. There is deliberately no `audio select` subcommand — a device is
chosen in the panel, or pinned for headless use by setting
`audio.preferred_device_fingerprint` in `config/local.toml`.

**You do not need to create a meeting first.** Meeting setup is a Phase 9 screen, so
`Start` creates a minimal draft meeting for the recording — audited, in the same
transaction — and a blank title becomes a UTC timestamp. Folders are always
`<meeting-uuid>/<recording-uuid>/`, so a title may contain a participant's name
without that name reaching the filesystem.

Recording itself is driven from the UI (or the loopback API) rather than the CLI,
because it needs a live level meter and a Stop button. The full operator protocol,
the calibration-evidence rules, the Windows troubleshooting table and the recovery
runbook are in
[`docs/phase-2-audio-capture.md`](docs/phase-2-audio-capture.md).

If a recording is interrupted — process killed, power lost, device unplugged —
`audio recover` salvages every complete frame from the partial chunk, quarantines
anything ambiguous rather than deleting it, and is safe to run repeatedly.

### `doctor` on an unprepared interpreter

```powershell
py -3.12 -m mom_igd doctor
```

This works from the repository root **even on a bare interpreter with none of the
project dependencies installed** — which is the situation `doctor` is most useful
in. When a core runtime dependency is missing it falls back to a reduced,
standard-library-only report that still checks the interpreter version, the Store
shim, the OS, the CPU, the disk and the runtime data path, and then tells you
exactly what is missing and how to install it:

```
MoM-IGD 0.2.0 - environment diagnostics (REDUCED: runtime dependencies missing) ...
[FAIL] runtime_dependencies   4 of 5 core runtime dependencies are not
                              importable by this interpreter (pydantic, fastapi,
                              uvicorn, webview). ...
Exit code: 1
```

`sounddevice` is not in that gating set on purpose: it is imported lazily, so the
full doctor still runs without it and reports the missing audio backend as a
`FAIL` with an install hint — rather than discarding every other check.

For everything else, use the `.venv` interpreter — it is the reproducible
environment. Any other command run without the dependencies reports a clear
one-line diagnosis rather than a traceback.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success (`doctor`: no FAIL; warnings are expected in Phase 2) |
| 1 | a required check failed, or the command could not complete |
| 2 | `doctor --strict` and at least one WARN (no FAIL) |
| 3 | configuration is invalid |

---

## Tests

```powershell
# whole suite
.\.venv\Scripts\python.exe -m pytest

# with coverage
.\.venv\Scripts\python.exe -m pytest --cov=mom_igd --cov-report=term-missing

# skip the tests that bind a real loopback socket
.\.venv\Scripts\python.exe -m pytest -m "not slow"
```

The suite needs **no internet, no microphone, no AI model, no Docker and no
OpenVINO**, and never writes to the real runtime data directory — a
session-scoped guard in `tests/conftest.py` fails the run if `D:\MoM-IGD-Data`
changes. Every test gets its own temporary data root.

---

## Runtime data

Source code and user data are strictly separated. Nothing the application
produces at runtime is ever written into this repository.

```
D:\MoM-IGD-Data\            <- default; override with MOM_IGD_DATA_DIR
├─ db\          mom_igd.db (+ -wal, -shm)
├─ recordings\  <meeting-uuid>\<recording-uuid>\
│                 chunk_000001.wav      complete, checksummed
│                 manifest.jsonl        append-only, one line per chunk
│                 manifest.json         summary + hash chain
│                 quarantine\           evidence, never deleted
├─ exports\     generated MoM documents            (Phase 10)
├─ logs\
├─ models\      model binaries                     (Phase 4A onwards)
├─ temp\        recording.lock (single-recording guard)
└─ backups\
```

A visible `.wav` is by definition complete: audio is written to
`chunk_NNNNNN.pcm.part`, `fsync`ed, hashed from disk and atomically renamed. A
crash can leave a `.part` or a `.tmp`, never a half-written chunk
([ADR-0007](docs/adr/0007-chunking-checksums-and-crash-recovery.md)).

**Storage:** 345.6 MB per hour mono, 691.2 MB per hour stereo, at 48 kHz / 16-bit.
Preflight refuses to start a recording without headroom for the planned duration
plus a margin.

The path service (`mom_igd/paths.py`) refuses a relative path, a bare drive
root, the repository itself, anything inside the repository, and any parent
directory that would contain the repository. Directories are created only by an
explicit initialisation call (`db init`, `serve`, `shell`) — importing a module
or running `doctor` never touches the filesystem.

Override the location for one command or for the session:

```powershell
.\.venv\Scripts\python.exe -m mom_igd db init --data-dir E:\MoM-Data
$env:MOM_IGD_DATA_DIR = 'E:\MoM-Data'
```

Precedence: `--data-dir` > `MOM_IGD_DATA_DIR` > `config/default.toml` > built-in default.

---

## Repository structure

```
MoM-IGD/
├─ config/
│  └─ default.toml            versioned defaults (local.toml overrides, git-ignored)
├─ docs/
│  ├─ architecture.md         full architecture and phase roadmap
│  ├─ phase-0-summary.md      evidence-based summary of the Phase 0 audit
│  ├─ phase-2-audio-capture.md  capture engine, runbook, manual acceptance
│  └─ adr/                    architecture decision records 0001-0008
├─ models/
│  ├─ registry.json           versioned model DECLARATION (empty in Phase 1)
│  └─ README.md               schema and rules; binaries never live here
├─ mom_igd/
│  ├─ __main__.py, cli.py     entry point and command dispatch
│  ├─ version.py              identity and schema versions
│  ├─ config.py               layered configuration + validation
│  ├─ paths.py                central runtime path service
│  ├─ offline_policy.py       dependency, endpoint and bind-address policy
│  ├─ security.py             session token (memory only)
│  ├─ logging_setup.py        logging with secret redaction
│  ├─ audit.py                append-only, hash-chained audit trail
│  ├─ registry.py             model registry schema and validation
│  ├─ smoke.py                headless backend smoke test
│  ├─ api/                    loopback FastAPI app, routes, deps, server
│  ├─ audio/                  Phase 2 capture engine (see below)
│  ├─ db/                     connection pragmas + migrations (0001, 0002)
│  ├─ diagnostics/            model.py (stdlib-only types) · doctor.py (full)
│  │                          · bootstrap.py (reduced, no third-party import)
│  │                          · audio_checks.py (Phase 2 device/capture checks)
│  ├─ jobs/                   workflow state machine (declaration + persistence)
│  └─ shell/                  pywebview launcher + static web/ assets
├─ tests/                     the Phase 1 + Phase 2 test suite
├─ requirements.txt           pinned runtime closure
├─ requirements-dev.txt       pinned dev/test closure
└─ pyproject.toml
```

The capture engine, `mom_igd/audio/`:

```
backend.py            AudioBackend protocol · CaptureProfile · StreamStats
  sounddevice_backend.py  the real device (lazy import, no NumPy)
  fake_backend.py         deterministic PCM sources, no hardware needed
devices.py            fingerprint identity · Windows endpoint evidence
quality.py            RMS / peak / clipping meter with operator advice
frame_queue.py        bounded queue, capacity in SECONDS of audio
writer.py             exact chunk rotation · PCM .part → WAV → checksum
manifest.py           JSON Lines records + summary with a hash chain
recovery.py           salvage partials, quarantine the ambiguous
preflight.py          disk / device / permission checks
calibration.py        level check with an actionable verdict
session.py            callback → queue → single writer thread
service.py            lifecycle, single-recording lock, DB + job coupling
bench.py              capture smoke and accelerated soak
```

Remaining future directories (ASR/diarization/speaker providers, reconciliation,
MoM extraction, exporters, review UI, evaluation datasets) are described in
`docs/architecture.md` and are **not** scaffolded as empty placeholders. Each
arrives with the phase that implements it.

---

## Security and privacy posture (Phase 2)

* The API binds `127.0.0.1` only. A wildcard, LAN or public address is rejected
  by configuration validation, not merely discouraged.
* A `Host`-header allowlist rejects DNS-rebinding attempts. This is what makes
  "Swagger is not exposed outside loopback" enforceable rather than incidental.
* `/health` and `/version` are unauthenticated by documented policy: they must
  answer before the shell has a token, and they disclose only name, version,
  phase and coarse booleans. Everything else, including `/doctor` and
  `/internal/ready`, requires a per-process session token.
* The session token exists only in process memory. It is never written to source,
  the database, a log, a URL or query string, a static asset, `localStorage`,
  `sessionStorage`, a cookie or the DOM. A credential presented in a query
  string is refused with HTTP 400 **even when it is correct**.
* Offline-ness is enforced at the application level: a dependency denylist, a
  provider-endpoint rule (local path or loopback URL only), and a loopback bind
  rule. There is **no global `socket.socket` monkey-patch** — see
  [ADR-0002](docs/adr/0002-offline-runtime-definition.md).
* Operating-system firewall hardening is **deferred to Phase 11**.
* **Data retention is not implemented yet.** Nothing is deleted automatically;
  retention, encryption at rest and the consent workflow are Phase 3 / Phase 11.

### Recorded audio (read this before recording a real meeting)

* **Audio is stored unencrypted.** A recording of a meeting is personal data;
  under Indonesia's UU PDP No. 27/2022 it must be protected. Encryption at rest
  (AES-256-GCM with the key held by Windows DPAPI) is Phase 11. Until then the
  recordings directory is only as protected as the Windows account it sits under.
  The UI states this in the recording panel rather than leaving it to be
  discovered.
* **The consent workflow does not exist yet** (Phase 3). Obtain and record consent
  by your existing process before recording participants.
* **Nothing is uploaded, and nothing is deleted automatically.** Audio is written
  to the local runtime data directory and stays there until someone removes it.
* **No speaker identity is derived in Phase 2.** No voiceprint, no biometric
  template, no transcript — only audio, checksums and a manifest.
* The microphone is opened only by an explicit operator action, and no device
  setting (gain, AGC, enhancements, default status) is ever changed.

Model binaries, meeting recordings, voiceprints, generated MoM documents,
runtime databases and secrets are never committed. `.gitignore` and
`.gitattributes` enforce this; `.gitattributes` also marks every binary format
so that `core.autocrlf=true` cannot corrupt an audio file, a model or a checksum.

---

## Phase boundaries

**Phase 1 — implemented:** configuration and validation · central runtime path
service · SQLite with WAL, foreign keys and versioned transactional migrations ·
nine foundational tables · deterministic workflow state machine with audit ·
append-only hash-chained audit trail · loopback API with session token ·
environment diagnostics · empty model registry · static desktop shell ·
headless smoke test · test suite.

**Phase 2 — implemented:** device discovery with fingerprint identity and
registry-verified transport · explicit device selection with **no silent
fallback** · preflight (disk, device, permission) and calibration with an
actionable verdict · PCM16 chunked recording at the device's native rate ·
bounded queue and a single writer thread · per-chunk SHA-256 · JSON-Lines
manifest with a hash chain · explicit gap accounting · pause / resume / stop /
abandon · crash recovery with quarantine · single-recording lock · recording API,
UI panel and CLI · `doctor --production` gate · fake-backend test suite and soak
tooling.

**Phase 3 — implemented:** participant directory with UUID identity and **no size
limit** · per-meeting roster with a **configurable capacity** (default 9, safety
ceiling default 50) · append-only biometric consent with versioned, hashed text and
a grant / revoke / re-grant lifecycle · AES-256-GCM voiceprint envelopes under a
DPAPI-protected master key · crash-consistent voiceprint storage with recovery and
quarantine · consent revocation that deletes the ciphertext · an eleven-state
enrollment machine reusing the Phase 2 capture path and its lock · enrollment quality
gates · a narrow speaker-embedding provider boundary · 24 token-protected API routes
· participant, roster and consent UI · diagnostics · CLI · migrations 0003 and 0004.

Phase 3 **creates** voice templates. It does not compare them — there is no speaker
identification here.

**Still not implemented, by design:** VAD · ASR · diarization · voice
identification · speaker labelling · LLM integration · MoM generation ·
PDF/Word/JSON/Markdown export · action tracking · encryption of meeting audio and
transcripts · model download · OpenVINO installation or benchmarking · retention
enforcement · firewall configuration · resampling and the 16 kHz ASR working copy
(Phase 4) · FLAC.

**What roster size does and does not mean.** A meeting's roster decides who the
*known speaker candidates* are. It never decides what is recorded: capture always
takes the whole room signal, and a voice with no voiceprint — or one belonging to
nobody on the roster — is labelled `UNKNOWN` from Phase 6 onwards rather than
discarded. Raising a roster's capacity does not improve recognition accuracy, and no
head count has been validated in a real room yet.

**Directory, roster, capacity and attendees are four different things.** The
*directory* holds everyone ever registered and has no size limit. A *roster* is who is
expected in one meeting. *Capacity* is how many seats that roster has — stored per
meeting, default 9, adjustable up to a configurable ceiling (default 50). The
*attendee count* is how many members the roster actually holds, and that — not the
capacity — is how many voiceprints the meeting needs. `doctor` measures it per roster
and per person: it checks that each active member owns their *own* production-eligible
voiceprint, so a pile of templates belonging to people outside the roster proves
nothing.

If the configured ceiling is later lowered below a meeting's stored capacity, that
meeting is **grandfathered**: the stored value is kept, it may be lowered but not
raised, nothing is clamped, and no participant is ever removed.

**No AI provider or model has been selected.** ASR, diarization,
speaker-embedding and LLM choices are deferred to a real-device benchmark in
Phase 4A — see
[ADR-0005](docs/adr/0005-ai-provider-selection-deferred-to-phase-4a.md).

### Phase 2 production acceptance is not granted yet

A **USB conference microphone** (omnidirectional, placed at the centre of the
table) is required. The internal laptop array is an Intel Smart Sound digital
microphone array whose beamforming and noise suppression actively suppress
non-dominant speakers — that destroys diarization around a table and makes
voiceprints inconsistent, and it gets worse as the room gets larger. The internal microphone is acceptable for
**early development only**.

The development machine currently has **no verified USB capture device**, so the
production gate has not been satisfied:

```powershell
.\.venv\Scripts\python.exe -m mom_igd audio devices          # confirm transport = USB
.\.venv\Scripts\python.exe -m mom_igd audio calibrate        # record the evidence
.\.venv\Scripts\python.exe -m mom_igd doctor --production     # must report 0 FAIL
```

`doctor --production` fails without a registry-verified USB device and a calibration
that is `GOOD`, **taken on that same selected device**, and **less than 30 days old**.
A stored `GOOD` verdict alone is not accepted — a stale reading of the laptop array
must not vouch for a microphone plugged in this morning.

The engine is complete and verified against a deterministic fake backend; what
remains is measurement on real hardware — 3 × 60 minutes with no unrecorded frame
loss, CPU below 5 % and RSS below 300 MB. Those numbers are reported as
**NOT MEASURED** until then, never estimated.

---

## Licence

None chosen. Treat this repository as **private and internal**. No `LICENSE`
file exists, deliberately.
