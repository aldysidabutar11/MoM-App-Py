# AGENTS.md

Operating instructions for any autonomous or semi-autonomous agent working in
this repository. `CLAUDE.md` holds the full rule set; this file is the short
checklist and the boundaries that must never be crossed.

## Ground truth

1. The **filesystem** is the source of truth, not a previous conversation.
2. **`docs/architecture.md`** defines the phase boundaries.
3. **`docs/adr/`** records decisions already made. Do not silently reverse one —
   propose a new ADR instead.
4. The current phase is stated in `mom_igd/version.py` (`CURRENT_PHASE`) and in
   `README.md`.

## Before editing

```powershell
cd D:\Aldy\MoM-IGD
git status --short
.\.venv\Scripts\python.exe --version                 # must be 3.12.x
.\.venv\Scripts\python.exe -m mom_igd doctor
.\.venv\Scripts\python.exe -m pytest -q
```

If `doctor` reports a `FAIL` or the suite is red, fix that before starting new
work. Do not build on a broken foundation.

## After editing

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m mom_igd doctor
.\.venv\Scripts\python.exe -m mom_igd smoke
git diff --check
git status --short
```

All three must pass before reporting work complete. Report the actual output —
never summarise a failure away.

## Hard boundaries

| Never | Why |
|---|---|
| Add a cloud SDK or any outbound HTTP client on a runtime path | The product must work with no network (ADR-0002) |
| Assume CUDA / an NVIDIA GPU | Target device is Intel Iris Xe |
| Add a Docker or WSL2 runtime dependency | Native Windows only (ADR-0001) |
| Create or edit `.wslconfig` | Out of scope, and a system change |
| Stop or restart Docker, WSL, a browser or any user process | Not ours to touch |
| Change firewall, registry or global environment | System changes are out of scope |
| Install anything outside `.venv` | Ask first |
| Download a model | Provisioning is a separate, explicit, later step |
| Use Python 3.14 or the Store shim | Wheel availability / sandboxing |
| Raise `max_heavy_workers` above 1 | 16 GB RAM budget (ADR-0004) |
| Bind anything to `0.0.0.0` or a LAN address | Loopback only |
| Log, persist or URL-encode the session token | Memory only |
| Touch `D:\Aldy\Project APP VTT` | Legacy cloud project; not a code source |
| Commit a model, recording, voiceprint, database, log or secret | `.gitignore` enforces this |
| `git add` / `commit` / `push` / `tag` / branch / add a remote | Only on explicit request |
| Create a `LICENSE` file | No licence has been chosen |
| Implement a later phase, or scaffold it as empty placeholders | Scope discipline |
| Weaken a test to turn it green | Requirements decide, not convenience |

## Phase 1 scope, in one line

Foundation only: configuration · runtime paths · SQLite + migrations · workflow
state machine · audit trail · loopback API + session token · diagnostics · empty
model registry · static shell · headless smoke test · tests · docs.

Everything audio-, AI-, export- or encryption-related belongs to a later phase.

## When blocked

Stop and report. Include the exact command, its exact output, and what you
believe the cause is. Do not:

* widen the scope to work around it,
* disable or dilute a test,
* install an unapproved dependency,
* change a system setting.

A clear blocker report is a successful outcome. A silently widened scope is not.
