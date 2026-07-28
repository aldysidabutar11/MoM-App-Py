# MoM-IGD — offline Minutes of Meeting

A fully offline desktop application for producing Minutes of Meeting from
in-person meetings held in one physical room. Native Windows 11, CPU-first, no
cloud API, no CUDA, no Docker in production.

> **Status: Phase 1 — application foundation only.**
> There is deliberately **no audio recording, no ASR, no diarization, no speaker
> identification, no LLM, no MoM generation and no export** in this build. What
> exists is the foundation those features will be built on: configuration, a
> runtime path service, SQLite with migrations, a deterministic workflow state
> machine, a loopback-only API, environment diagnostics, and a static desktop
> shell. See [Phase boundaries](#phase-1-boundaries).

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
(4 direct dependencies: `fastapi`, `uvicorn`, `psutil`, `pywebview`);
`requirements-dev.txt` adds `pytest`, `pytest-cov` and `httpx`. Instructions for
refreshing them are in the header of each file. A hash-pinned offline wheelhouse
is deferred to Phase 11.

**Nothing else may be installed.** No cloud SDK, no AI runtime, no audio
library. `tests/test_offline_policy.py` fails the build if one appears.

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

# database (the only command that creates the runtime data tree)
.\.venv\Scripts\python.exe -m mom_igd db init
.\.venv\Scripts\python.exe -m mom_igd db version
.\.venv\Scripts\python.exe -m mom_igd db verify              # pragmas, checksums, audit chain

# configuration and model registry
.\.venv\Scripts\python.exe -m mom_igd config show
.\.venv\Scripts\python.exe -m mom_igd registry show

# headless backend smoke test (no GUI, no microphone, no model, no network)
.\.venv\Scripts\python.exe -m mom_igd smoke

# backend in the foreground, loopback only
.\.venv\Scripts\python.exe -m mom_igd serve

# desktop window (blocks until closed)
.\.venv\Scripts\python.exe -m mom_igd shell
```

### `doctor` on an unprepared interpreter

```powershell
py -3.12 -m mom_igd doctor
```

This works from the repository root **even on a bare interpreter with none of the
project dependencies installed** — which is the situation `doctor` is most useful
in. When a Phase 1 runtime dependency is missing it falls back to a reduced,
standard-library-only report that still checks the interpreter version, the Store
shim, the OS, the CPU, the disk and the runtime data path, and then tells you
exactly what is missing and how to install it:

```
MoM-IGD 0.1.0 - environment diagnostics (REDUCED: runtime dependencies missing) ...
[FAIL] runtime_dependencies   4 of 5 Phase 1 runtime dependencies are not
                              importable by this interpreter (pydantic, fastapi,
                              uvicorn, webview). ...
Exit code: 1
```

For everything else, use the `.venv` interpreter — it is the reproducible
environment. Any other command run without the dependencies reports a clear
one-line diagnosis rather than a traceback.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success (`doctor`: no FAIL; warnings are expected in Phase 1) |
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
├─ recordings\  audio masters and chunk manifests  (Phase 2)
├─ exports\     generated MoM documents            (Phase 10)
├─ logs\
├─ models\      model binaries                     (Phase 4A onwards)
├─ temp\
└─ backups\
```

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
│  └─ adr/                    architecture decision records 0001-0005
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
│  ├─ db/                     connection pragmas + migrations (0001_initial.sql)
│  ├─ diagnostics/            model.py (stdlib-only types) · doctor.py (full)
│  │                          · bootstrap.py (reduced, no third-party import)
│  ├─ jobs/                   workflow state machine (declaration + persistence)
│  └─ shell/                  pywebview launcher + static web/ assets
├─ tests/                     the Phase 1 test suite
├─ requirements.txt           pinned runtime closure
├─ requirements-dev.txt       pinned dev/test closure
└─ pyproject.toml
```

Future directories (capture engine, ASR/diarization/speaker providers,
reconciliation, MoM extraction, exporters, review UI, evaluation datasets) are
described in `docs/architecture.md` and are **not** scaffolded as empty
placeholders. Each arrives with the phase that implements it.

---

## Security and privacy posture (Phase 1)

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

Model binaries, meeting recordings, voiceprints, generated MoM documents,
runtime databases and secrets are never committed. `.gitignore` and
`.gitattributes` enforce this; `.gitattributes` also marks every binary format
so that `core.autocrlf=true` cannot corrupt an audio file, a model or a checksum.

---

## Phase 1 boundaries

**Implemented:** configuration and validation · central runtime path service ·
SQLite with WAL, foreign keys and versioned transactional migrations ·
nine foundational tables · deterministic workflow state machine with audit ·
append-only hash-chained audit trail · loopback API with session token ·
environment diagnostics · empty model registry · static desktop shell ·
headless smoke test · test suite.

**Not implemented, by design:** audio recording · audio device capture ·
FLAC/WAV writing · VAD · ASR · diarization · voice enrollment · voice
identification · LLM integration · MoM generation · PDF/Word/JSON/Markdown
export · action tracking · encryption at rest · consent workflow · model
download · OpenVINO installation or benchmarking · retention enforcement ·
firewall configuration.

**No AI provider or model has been selected.** ASR, diarization,
speaker-embedding and LLM choices are deferred to a real-device benchmark in
Phase 4A — see
[ADR-0005](docs/adr/0005-ai-provider-selection-deferred-to-phase-4a.md).

### Before Phase 2 production acceptance

A **USB conference microphone** (omnidirectional, placed at the centre of the
table) is required. The internal laptop array is an Intel Smart Sound digital
microphone array whose beamforming and noise suppression actively suppress
non-dominant speakers — that destroys diarization for nine participants and
makes voiceprints inconsistent. The internal microphone is acceptable for
**early development only**.

---

## Licence

None chosen. Treat this repository as **private and internal**. No `LICENSE`
file exists, deliberately.
