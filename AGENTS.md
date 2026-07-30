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
5. The capture engine has its own document,
   **`docs/phase-2-audio-capture.md`**, plus ADR-0006, ADR-0007 and ADR-0008.
   Read those before changing anything under `mom_igd/audio/`.
6. Participants, consent and enrollment have
   **`docs/phase-3-participants-enrollment.md`**, plus ADR-0009 through ADR-0013
   and `docs/phase-3-speaker-model-selection.md`. Read those before changing
   anything under `mom_igd/enrollment/`. That package handles biometric data:
   a mistake there is a privacy incident, not a bug.

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
.\.venv\Scripts\python.exe -m mom_igd audio smoke     # capture engine, fake backend
git diff --check
git status --short
```

All four must pass before reporting work complete. Report the actual output —
never summarise a failure away.

A `FAIL` that names an external blocker — no USB conference microphone, no
approved embedding model, consent text still a draft — is the honest answer, not
something to engineer away. Never turn one into a `WARN` to make a run green.

Never pipe pytest through `2>&1 | Select-Object`: PowerShell turns each stderr log
line into an `ErrorRecord` and the run looks like it has hung.

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
| Open the microphone without an explicit user action | Import, startup, `doctor`, tests and device discovery must all stay silent |
| Change a device's gain, AGC, enhancements or default status | Not ours to touch; advise the operator instead |
| Do file I/O, logging or DB work inside the audio callback | A blocked callback loses real audio |
| Fabricate silence to cover a gap | Every downstream timestamp would be wrong (ADR-0007) |
| Fall back to another microphone when the selected one is gone | Silently records the wrong room (ADR-0008) |
| Claim `USB` without registry evidence | The production gate would become meaningless |
| Add NumPy, `soundfile`, `librosa` or `audioop` to the capture path | ADR-0006; `audioop` is gone in 3.13 |
| Edit an applied migration (`0001`–`0004`) | Applied and checksummed — add `000N_*.sql` |
| Unanchor a runtime-data pattern in `.gitignore` | A bare `audio/` silently ignored `mom_igd/audio/` once |
| Write an embedding as plaintext, JSON, NumPy, pickle, log line or audit detail | It is biometric data; only the sealed `.vpx` envelope may hold it (ADR-0010) |
| Treat Base64 as encryption | Encoding is not confidentiality |
| Use a display name as a key, directory, filename or URL segment | Identity is the participant UUID (ADR-0009) |
| Make a name a unique column | Two people may share a name |
| Persist enrollment audio, or send raw audio over HTTP | Bounded memory only, discarded after embedding (ADR-0012) |
| Capture voice in the page (`getUserMedia`, WebAudio, `MediaRecorder`) | Enrollment audio never enters the browser (ADR-0012) |
| Add a config, env var or request parameter that selects a fake provider | The test double may only be injected in-process by a test |
| Mutate a consent flag | `consent_events` is append-only; state is derived (ADR-0009) |
| Give enrollment its own capture lock file | The shared Phase 2 lock is what makes recording XOR enrollment true |
| Create the master key on import, startup or in `doctor` | Only an explicit enrollment may create it |
| Keep a voiceprint after consent is revoked | Revocation deletes the envelope (ADR-0011) |
| Claim a voiceprint is production-ready when it came from a test double | It is stored `DEVELOPMENT_ONLY`, never `ACTIVE` |
| Treat nine as a participant limit | It is the per-meeting **default**; capacity is stored per meeting (ADR-0013) |
| Put the 50 safety ceiling in a `CHECK` constraint | Changing it would force a table rebuild; it is configuration |
| Claim any roster size has validated accuracy, or say "unlimited" | No head count has been tested in a real room |
| Let roster size affect whether audio is recorded | Capture takes the whole room; off-roster voices become `UNKNOWN` |
| Import `mom_igd.enrollment` from `mom_igd/audio/` | That coupling is how a roster starts gating capture |
| Remove a participant to make a lowered capacity fit | Refuse with `409` instead (ADR-0013) |
| Clamp a stored capacity to a lowered ceiling | Grandfather it: keep, allow lowering, refuse raising (ADR-0013 §6) |
| Construct `ParticipantService` without `config=` | It silently falls back to 9/50 while the GUI honours the file |
| Let a new meeting take the column `DEFAULT 9` | Write the configured default explicitly; the DEFAULT is for backfill |
| Measure readiness against capacity, or as a global voiceprint count | Capacity is seats; coverage is per roster member (ADR-0013 §7) |
| Put a display name or meeting title in a diagnostic | Reports get pasted into tickets; a UUID is enough |
| Add a `display` rule that can defeat `[hidden]` in `app.css` | It made both modals cover the app and swallow every click |

## Phase 3 scope, in one line

Phases 1–2 **plus** participants, consent and enrollment: a participant
directory with UUID identity and no size limit · a per-meeting roster whose capacity is
configurable (default 9, ceiling default 50) · append-only biometric consent
with grant / revoke / re-grant · one-at-a-time voice enrollment over the Phase 2
capture path · enrollment quality gates · a narrow speaker-embedding provider
boundary with **no approved model yet** · AES-256-GCM voiceprint envelopes under a
DPAPI-protected master key · crash-consistent storage, re-enrollment and
deletion-on-revocation · participant/consent UI, protected loopback API, CLI,
diagnostics and migrations `0003` and `0004`.

Phase 3 **creates** templates. It does not compare them: there is no speaker
identification, no diarization, no ASR, no LLM and no export here. Resampling and
the 16 kHz working copy still belong to Phase 4.

## When blocked

Stop and report. Include the exact command, its exact output, and what you
believe the cause is. Do not:

* widen the scope to work around it,
* disable or dilute a test,
* install an unapproved dependency,
* change a system setting.

A clear blocker report is a successful outcome. A silently widened scope is not.
