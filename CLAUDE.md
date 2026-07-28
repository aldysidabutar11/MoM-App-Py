# CLAUDE.md — working rules for AI agents in this repository

Read this before changing anything. These are hard constraints derived from the
product requirements and the Phase 0 audit, not style preferences.

## What this project is

A **fully offline** desktop application that turns a recording of an in-person
meeting (one room, up to nine registered participants, Indonesian with English
technical terms) into a reviewed, structured Minutes of Meeting.

**Current phase: 1 — application foundation.** See `docs/architecture.md` for the
full roadmap and `README.md` for what exists.

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

## Scope discipline

The single most important rule: **do not implement a future phase.**

Phase 1 must not contain audio capture, device enumeration, FLAC/WAV writing,
VAD, ASR, diarization, voice enrollment, voice identification, LLM calls, MoM
generation, exporters, action tracking, encryption at rest, a consent workflow,
model downloads, or OpenVINO installation/benchmarking.

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

## Doctor classification contract

* `PASS` — required by the current phase, and satisfied.
* `WARN` — optional, informational, or required only in a **future** phase.
* `FAIL` — required by the current phase, and not satisfied.

In Phase 1 a missing microphone, audio library, AI library, model or OpenVINO
installation is **always `WARN`, never `FAIL`**. Docker/WSL presence and memory
use are **informational only**.

Exit codes: `0` no FAIL · `1` any FAIL · `2` `--strict` with a WARN.

## Testing rules

* Tests must not require the internet, a microphone, an AI model, Docker or
  OpenVINO, and must not depend on execution order.
* Tests must use temporary directories only. The real runtime data directory
  (`D:\MoM-IGD-Data`) is guarded by a session fixture; touching it fails the run.
* Use `use_local_file=False` when loading configuration in a test, so a
  developer's `config/local.toml` cannot change the outcome.
* **Do not weaken a test to make it pass.** If a test and the implementation
  disagree, the Phase 1 requirements decide which one is wrong.
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
.\.venv\Scripts\python.exe -m mom_igd db init
.\.venv\Scripts\python.exe -m mom_igd smoke
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest --cov=mom_igd --cov-report=term-missing
```

## Style

Match the surrounding code: type hints, `from __future__ import annotations`,
module docstrings that explain *why*, comments only where the reason is not
obvious from the code. Error messages must name the offending value **and** what
would be acceptable — a validation failure should teach the reader how to fix it.
