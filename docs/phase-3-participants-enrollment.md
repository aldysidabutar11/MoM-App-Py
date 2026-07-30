# Phase 3 — participants, biometric consent, secure voice enrollment

What this phase builds, why each decision was made, and what it deliberately does
not do. Decisions live in
[ADR-0009](adr/0009-participant-identity-and-append-only-consent.md),
[ADR-0010](adr/0010-voiceprint-encryption-aes-gcm-under-dpapi.md),
[ADR-0011](adr/0011-voiceprint-storage-crash-consistency-and-deletion.md) and
[ADR-0012](adr/0012-enrollment-capture-in-python-no-raw-audio-retention.md).

> **Real voice enrollment is not possible in this build.** No speaker embedding model
> has been selected or provisioned, so enrollment refuses with `MODEL_UNAVAILABLE`
> *before* the microphone is opened. See
> [`phase-3-speaker-model-selection.md`](phase-3-speaker-model-selection.md).

---

## 1. Scope

**Built:** participant registry with UUID identity and **no size limit** · per-meeting
roster with a **configurable capacity** (default 9, configurable safety ceiling default
50) · append-only biometric consent with versioned, hashed text ·
AES-256-GCM voiceprint encryption under a DPAPI-protected key · crash-consistent
voiceprint storage with recovery and quarantine · consent revocation that actually
deletes the ciphertext · an eleven-state enrollment machine · Python-side capture
sharing the Phase 2 device path and lock · 21 token-protected API routes · participant
and enrollment UI with a consent dialog · diagnostics · CLI.

**Not built, deliberately:** speaker *identification* (Phase 6) · matching meeting
audio to a name · ASR · diarization · model-based VAD · LLM · MoM generation · export ·
encryption of meeting audio or transcripts (Phase 11) · retention enforcement
(Phase 11).

Phase 3 **creates** voice templates. Phase 6 **compares** them. A test forbids
`identify_speaker`, `match_speaker` and `cluster_speakers` inside
`mom_igd/enrollment/`, because that distinction is easy to blur: an innocuous-looking
"is this the same speaker?" helper would become speaker identification without the
calibrated thresholds, injective assignment or UNKNOWN outcome Phase 6 requires.

---

## 1a. Six things that are easy to conflate

These are distinct, and most of the mistakes in this area come from treating two of
them as one.

| Term | What it is | Bounded by |
|---|---|---|
| **Participant directory** | Every person ever registered, active or not | **Nothing.** It has no size limit |
| **Meeting roster** | Who is expected in *one* meeting; a subset of the directory | That meeting's `participant_capacity` |
| **Roster capacity** | How many *seats* a meeting's roster has. Stored per meeting, default 9, adjustable up to a configurable ceiling (default 50) | `>= 1` in the schema; the ceiling in configuration |
| **Actual attendee count** | How many active members that roster currently holds (`active_count`). **Not** the capacity — a capacity of 15 with ten members needs ten voiceprints, not fifteen | The capacity |
| **Enrolled / recognisable participant** | A roster member who is active, has active consent, and owns an `ACTIVE`, production-eligible voiceprint | Requires an approved embedding model — none exists yet |
| **`UNKNOWN` voice** | Any voice in the recording that is not matched to an enrolled participant: not on the roster, no consent, no voiceprint, or simply unrecognised | Nothing. It is **labelled, never discarded** |

Removing somebody from a roster does not remove them from the directory. Roster size
never decides what is recorded: capture takes the whole room signal, always.

---

## 2. Layering

```
mom_igd/enrollment/
  keys.py            DPAPI via ctypes; KeyProtector, FakeKeyProtector, MasterKey
  cipher.py          AES-256-GCM envelope, canonical AAD, atomic write
  store.py           crash-consistent persistence, verify, revocation delete, recovery
  consent.py         consent text v1.0-draft, its hash, append-only ConsentService
  participants.py    ParticipantService, transactional nine-per-meeting cap
  provider.py        SpeakerEmbeddingProvider contract + output validation
  fake_provider.py   deterministic / stable-speaker / drifting / broken test doubles
  quality.py         enrollment gates: PASS / WARN / REJECT / NOT_MEASURED
  capture.py         EnrollmentCaptureController — Python-side capture, bounded buffer
  service.py         the eleven-state machine; the only path to an active voiceprint

mom_igd/api/enrollment_routes.py       21 token-protected routes
mom_igd/diagnostics/enrollment_checks.py   side-effect-free checks
mom_igd/db/migrations/0003_participants_consent_voiceprints.sql
```

---

## 3. Schema (migration 0003)

Four new tables; no Phase 1 or 2 table is dropped, and `0001`/`0002` are untouched.

| Table | Holds |
|---|---|
| `meeting_participants` | who is expected in which meeting; unique per pair |
| `consent_events` | **append-only** consent history |
| `enrollment_sessions` | one attempt, its quality metrics and reason code |
| `voiceprints` | non-biometric metadata plus a pointer to an encrypted envelope |

`participants` gains `uuid` (backfilled, unique) and **loses**
`ux_participants_display_name` — a deliberate reversal of a Phase 1 decision, so two
people may share a name (ADR-0009).

**No biometric data is in the database.** The centroid, dispersion and per-sample
embeddings live inside the AES-GCM ciphertext at
`<data_root>/voiceprints/<voiceprint-uuid>.vpx`. The row keeps shape (`embedding_dim`,
`sample_count`), provenance (model name/version/hash, device fingerprint/transport),
the envelope hash and a lifecycle status. Shape is not content: knowing a template has
192 dimensions reveals nothing about a voice.

### Voiceprint lifecycle

| Status | Meaning | Usable? |
|---|---|---|
| `PENDING_WRITE` | save in flight; the crash-recovery anchor | no |
| `ACTIVE` | the one template Phase 6 may use | **yes** |
| `DEVELOPMENT_ONLY` | usable for development; **not** production eligible | yes, non-production |
| `SUPERSEDED` | replaced by re-enrollment; ciphertext already deleted | no |
| `RE_ENROLL_REQUIRED` | device changed, or an interrupted save was recovered | no |
| `REVOKED` | consent withdrawn; ciphertext deleted | no |
| `DELETE_PENDING` | consent withdrawn but deletion **failed**; retryable | no |
| `INTEGRITY_FAILED` | envelope did not authenticate | no |

`usable` is an explicit allow-list of two states, so any status added later defaults
to *not* usable.

---

## 4. Consent

Version **`1.0-draft`**, Bahasa Indonesia, ten numbered clauses, SHA-256 recorded with
every event. The `-draft` suffix is load-bearing: `doctor --production` reads it to
decide that organisational review is outstanding.

The text discloses, and tests assert each: the voice becomes a biometric template ·
processing is local and offline · the purpose is limited to one stated use · raw
enrollment audio is **not** retained · the right to withdraw · what withdrawal does to
the template · that historical meeting data is **not** auto-deleted · that the speaker
becomes `UNKNOWN` afterwards · that DPAPI does not stop code running as the same user ·
that SSD deletion is not physical erasure.

Granting requires the caller to echo the hash of the text it displayed. A mismatch is
refused, because there is no safe way to guess which wording was on screen. Granting
twice is idempotent — a double-clicked button cannot litter the history.

**This is a mechanism, not a compliance certificate.** The application does not claim
legal compliance, and `doctor --production` fails on the draft status until the
organisation records approval.

---

## 5. Encryption

AES-256-GCM per payload; a random 256-bit master key protected by Windows DPAPI
(current user, `CRYPTPROTECT_UI_FORBIDDEN`, reached through `ctypes` — no pywin32).

The AAD binds each ciphertext to its `voiceprint_uuid`, `participant_id`, envelope
schema and the model's name/version/SHA-256. Moving an envelope between participants
fails to authenticate — which is the attack that matters, because a successful swap
would make Phase 6 confidently identify one person as another.

Verified rejections: wrong participant · wrong voiceprint UUID · changed model ·
tampered ciphertext · truncated ciphertext · truncated envelope · unknown schema ·
wrong master key — **and** wrong key with the `key_id` header stripped, proving
security rests on the AEAD tag rather than a plaintext field.

### Limits, stated plainly

* DPAPI does **not** stop anything running as the same Windows user.
* `PRAGMA secure_delete` plus a WAL checkpoint overwrite freed database pages, but on
  an SSD neither reaches the physical NAND.
* A backup taken before a revocation still contains the template.

**BitLocker is a Phase 11 requirement, not optional hardening.** Backup and
key-escrow policy belong there too. A key lost with the Windows profile means every
voiceprint must be re-enrolled — deliberately, because a recoverable key is a weaker
key.

---

## 6. Enrollment

### State machine

```
CREATED → CONSENT_REQUIRED → READY → CAPTURING ⇄ VALIDATING
                                          ↓
                                      EMBEDDING → ENCRYPTING → COMPLETED
   any non-terminal state → REJECTED | CANCELLED | FAILED
```

State names match the `enrollment_sessions.state` CHECK constraint exactly, and a test
reads `sqlite_master` to prove it — drift would break every `UPDATE`. Every
non-terminal state can reach a terminal one, so a session can always be abandoned
rather than wedging the capture lock.

### Order of operations

```
consent + participant + MODEL + device + calibration + capture lock   (before the mic)
  → capture 5 samples (Python-side, bounded memory)
  → per-sample quality gates
  → embed
  → consumer-side embedding validation
  → RE-CHECK consent / participant / device / calibration
  → seal (AES-256-GCM)
  → store (crash-consistent)
  → verify
  → COMPLETED
```

The re-check exists because a five-sample enrollment takes about a minute and a person
can withdraw consent during it. Storing the template afterwards would mean keeping
biometric data they had already refused. On any change the buffer is dropped, the
session ends with an enumerated reason code, and nothing is stored.

### Capture

Audio is captured **in the Python process** through the Phase 2 backend. The browser
has no microphone access at all — no `getUserMedia`, no audio in a request body, no
audio on disk (ADR-0012). Five samples, ~8–12 s each, ≥ 30 s of speech in total,
bounded in memory with a hard **byte** ceiling, released on every terminal path.

Enrollment takes the **same** `SingleRecordingLock` at the same path as a meeting
recording, so `meeting recording XOR enrollment` holds across processes.
`read_live_holder()` is the one public answer to "is the microphone in use?" —
callers must not use `.held`, because the lock object is shared and `held` is true even
when this process holds it for the *other* activity.

### Quality gates

Per sample: duration · not silent · no clipping · level not too low · peak headroom
(warn) · mostly speech · silence share · estimated SNR · **zero** dropped frames ·
**zero** xruns.

Whole enrollment: total speech ≥ 30 s · device consistency · calibration freshness and
verdict · production device (warn) · **intra-speaker cosine ≥ 0.80**.

`NOT_MEASURED` is a real verdict. A steady signal has no quiet passage, so the noise
floor equals the signal and SNR is genuinely unmeasurable — reporting 0 dB would flunk
a healthy sample. The speech-active ratio is therefore **energy-relative** (within
20 dB of the sample's own RMS) rather than noise-floor-relative.

**Only the 0.80 cosine floor has a documented origin.** Every level threshold is a
provisional engineering default, labelled as such in
`EnrollmentQualityThresholds.to_dict()`, and uncalibrated against a verified USB
microphone. There is no model-based VAD.

### Production eligibility

`ACTIVE` (production eligible) requires a Windows-verified **USB** transport, a stable
fingerprint, valid calibration evidence, active consent, verified model provenance and
passing gates. Anything else yields **`DEVELOPMENT_ONLY`**, which a CHECK constraint
forces to `production_eligible = 0`. The built-in laptop array always produces
`DEVELOPMENT_ONLY`: its beamforming suppresses speakers who are not facing the laptop,
which is unusable for a table of people -- and worse the larger the table gets.

---


### Roster capacity and its bounds

Capacity is stored on the meeting row (migration 0004), read from there, and never
recomputed from configuration -- so changing the configured default never retunes a
meeting created before the change. A *new* meeting takes the configured default, which
the recording path writes explicitly rather than relying on the column DEFAULT.

`settable_capacity_bounds()` is the single source of truth for what a capacity may be
set to. The API validates against it, the service enforces it inside its transaction,
and the UI renders it.

**A ceiling lowered below a stored capacity grandfathers the meeting.** The stored
value is kept -- never clamped, never silently adjusted, and no participant is ever
removed. It may be lowered (to at least the active roster count) but not raised while
it is above the ceiling, and the state is reported explicitly so the UI never offers a
range the meeting does not have. See ADR-0013 §6.

### Managing roster membership

The roster card shows the meeting selector, an `active_count / capacity` counter, the
remaining slots, the list of active members with their status, consent and voiceprint
badges, and a bounded, searchable view of the directory to add from. Add is
idempotent, remove is safe to repeat, and both refresh the counter from the server
rather than adjusting it locally.

`Tambahkan ke roster` is disabled when the roster is full, and a `409` from a stale
page still refreshes rather than leaving a wrong counter on screen. An inactive
participant is not offered, and adding one is refused with an explanation.

### What `doctor` means by roster coverage

Coverage is **per roster and identity-aware**: each active member is joined to *that
same participant's* own live voiceprint. A member counts as covered only when the
participant is active, the membership is active, their latest consent event is a
grant, and they own a voiceprint that is `ACTIVE` and `production_eligible`.

Consequences worth stating plainly:

* **Empty seats are not missing voiceprints.** Capacity 15 with a roster of ten needs
  ten templates.
* **A global count proves nothing.** Fifteen voiceprints belonging to people outside a
  roster do not make that roster ready.
* **An empty roster asks for nothing.** No number is invented for a meeting nobody has
  been added to.
* The schema carries no upcoming/historical meeting state, so no single meeting is
  assumed to be the relevant one: every roster is reported and the worst one named.
* No display name or meeting title appears in the output.

---

## 7. Revocation

```
append REVOKED consent event  (committed first)
  → participant ineligible from that instant
  → delete the envelope (secure_delete + WAL checkpoint)
  → clear the pointer and hash
  → audit, with no biometric payload
```

If the unlink fails the row becomes `DELETE_PENDING`: still unusable, still
ineligible, retryable. It is never left `ACTIVE` because of a filesystem error. The
pointer is cleared only on success — clearing it on failure would strand the leftover
ciphertext with no record it exists.

A re-grant does **not** revive a deleted template; a fresh enrollment is required.
`EnrollmentService.eligibility()` is the single fail-closed policy Phase 6 must call
rather than re-deriving eligibility from rows.

---

## 8. Commands

```powershell
# read-only: no microphone, no key creation, no model load, no DB write
.\.venv\Scripts\python.exe -m mom_igd participant list
.\.venv\Scripts\python.exe -m mom_igd participant list --search Budi --json
.\.venv\Scripts\python.exe -m mom_igd participant consent <UUID>
.\.venv\Scripts\python.exe -m mom_igd participant enrollment <UUID>
.\.venv\Scripts\python.exe -m mom_igd participant voiceprint <UUID> --verify
.\.venv\Scripts\python.exe -m mom_igd participant cleanup

# mutating
.\.venv\Scripts\python.exe -m mom_igd participant create "Budi Santoso" --role Ketua
.\.venv\Scripts\python.exe -m mom_igd participant update <UUID> --role Anggota
.\.venv\Scripts\python.exe -m mom_igd participant deactivate <UUID>
.\.venv\Scripts\python.exe -m mom_igd participant deactivate <UUID> --reactivate
.\.venv\Scripts\python.exe -m mom_igd participant cleanup --retry

# consent: requires an exact typed phrase. There is no --yes.
.\.venv\Scripts\python.exe -m mom_igd participant consent <UUID> --action grant
    # prints the full text + hash, then refuses; re-run with:
.\.venv\Scripts\python.exe -m mom_igd participant consent <UUID> --action grant --confirm "SAYA SETUJU"
.\.venv\Scripts\python.exe -m mom_igd participant consent <UUID> --action revoke --confirm "CABUT"

# readiness gates
.\.venv\Scripts\python.exe -m mom_igd doctor
.\.venv\Scripts\python.exe -m mom_igd doctor --production
```

Enrollment itself runs from the shell's participant panel, because it needs a live
progress display and a Cancel button.

---

## 9. Manual acceptance protocol

**Do not attempt a real enrollment until a model is provisioned, a verified USB
conference microphone is attached, calibration is `GOOD`, and the consent text has been
reviewed.** Steps 1–8 are safe today; step 9 is blocked by design.

1. Close the application.
2. Back up `D:\MoM-IGD-Data\db\mom_igd.db` (and `-wal`, `-shm` if present).
3. Verify the backup opens and reports its schema version.
4. Run `db init` to migrate 2 → 3.
5. Confirm the two existing meetings and five WAV chunks are still present.
6. Run `doctor` — expect `0 FAIL`.
7. Open the shell, open the participant panel, create and edit participants.
8. Read the consent dialog in full; grant, then revoke, and confirm the revoke warning
   lists every consequence.
9. Confirm the enrollment wizard shows **`MODEL_UNAVAILABLE`** with Start disabled,
   and that no microphone indicator appears.

### 9a. Revoke dialog (needs a human, and a browser)

These properties cannot be asserted without a live DOM. The automated suite proves the
CSS cascade, the handler wiring, the guard ordering and the request shape
(`tests/test_revoke_modal.py`), but pointer hit-testing and real focus movement need a
person:

1. Open the participant panel. **No dialog should be visible.** If a modal is on screen
   before you have asked for one, the `[hidden]` rule has been broken again -- that was
   the original bug.
2. Select a participant with active consent and press **Cabut persetujuan**.
3. The dialog must name the participant: *"Anda akan mencabut persetujuan milik:
   **Nama — Peran**"*. An empty name means nothing was selected, and the confirm button
   must be disabled.
4. Press **Batal**. The dialog closes, nothing is sent, and focus returns to the button
   that opened it.
5. Re-open it, then press **Escape**. It must close the same way.
6. Re-open it and press **Ya, cabut persetujuan** twice in quick succession. Exactly one
   revocation must be recorded -- check with
   `python -m mom_igd participant consent <uuid>`.
7. Confirm the consent badge becomes **DICABUT**, the voiceprint badge updates, and the
   participant, their meetings and their recordings are all still present.
8. For a participant with no voiceprint, the result line must say plainly that there was
   no template to delete. That is a success, not an error.

### 9b. Roster membership and capacity (needs a human)

Run this against a **temporary** data root, never `D:\MoM-IGD-Data`:

```powershell
$T = "D:\MoM-IGD-Test-Phase3-$(Get-Date -Format yyyyMMdd-HHmmss)"
.\.venv\Scripts\python.exe -m mom_igd db init    --data-dir $T
.\.venv\Scripts\python.exe -m mom_igd db version --data-dir $T   # expect 4 of 4
.\.venv\Scripts\python.exe -m mom_igd db verify  --data-dir $T   # audit chain intact
.\.venv\Scripts\python.exe -m mom_igd doctor     --data-dir $T   # expect 0 FAIL
.\.venv\Scripts\python.exe -m mom_igd shell      --data-dir $T
```

1. Create more than nine participants in the directory. **None should be refused** —
   the directory has no size limit.
2. In **Roster rapat**, choose a meeting. A recording must exist for a meeting to
   exist; start and stop one first if the selector is empty.
3. Set the capacity to 15 and save. The allowed range shown must be `1–50`.
4. Under **Tambahkan dari direktori peserta**, search and press
   **Tambahkan ke roster** ten times for ten different people.
5. The counter must read **`10 / 15`** and the member list must show ten rows with
   status, consent and voiceprint badges.
6. Press **Keluarkan dari roster** on one member. The counter must read `9 / 15`, and
   that person must **still be in the directory**.
7. Add the same person back. It must succeed.
8. Fill the roster to 15. **Tambahkan ke roster** must then be disabled.
9. Try to set the capacity to 5. It must be **refused**, and **nobody may disappear**.
10. Close the application, reopen it, and confirm the capacity is still 15 **and the
    roster still holds its members**.

### 9c. Configured capacity (needs a human)

1. Create `config/local.toml` with:

   ```toml
   [participants]
   default_meeting_participant_capacity = 15
   maximum_meeting_participant_capacity = 25
   ```

2. Restart. A **new** meeting (created by starting a recording) must come out at
   capacity **15**, and the allowed range must read `1–25`.
3. An existing meeting must keep the capacity it already had — changing the default
   must not retune it.
4. Setting a capacity of 26 must be refused.

Exact commands are in the Phase 3 final report and in
[`phase-3-progress.md`](phase-3-progress.md).

---

## 10. Outstanding blockers

1. **No speaker embedding model** selected or provisioned — real enrollment impossible.
2. **No verified USB conference microphone** — any enrollment would be
   `DEVELOPMENT_ONLY`.
3. **Consent text `1.0-draft`** not reviewed by legal/compliance.
4. **Zero production-eligible voiceprints**, against a requirement of however many the
   largest configured roster holds (9 by default).
5. **No real-room multi-speaker acceptance test.** Nine has never been validated in a
   real room, and neither has any larger number. Raising a roster's capacity does not
   change that, and the application does not claim otherwise.

All five are reported by `doctor --production`, which fails on them deliberately.
