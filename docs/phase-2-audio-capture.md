# Phase 2 — Offline audio capture

Phase 2 captures meeting audio with correct quality, ordering, integrity and
metadata. It deliberately knows **nothing** about who is speaking.

> **One microphone captures the mixed voices of up to nine people.** Phase 2 does
> not separate them, does not identify them, and produces no transcript and no
> Minutes of Meeting. Those arrive in Phases 4–8. Level metering (RMS, peak,
> clipping, silence, noise floor, per-channel activity) is signal-quality
> measurement, not speech detection: it answers *is this recording usable?*, never
> *is someone talking?*

---

## 1. Capture format

| | |
|---|---|
| Sample format | PCM signed 16-bit little-endian (`int16`) |
| Sample rate | The device's **native** rate, usually 48 kHz. Never resampled during capture |
| Channels | The device's native count, capped at 2. A mono microphone is never inflated to stereo; stereo is never downmixed |
| Container | WAV, written with the standard-library `wave` module |
| Compression | None |
| Chunk length | 30 s by default, configurable 10–120 s |

A 16 kHz mono working copy for ASR is a *processing* step (Phase 4), not a capture
step. See [ADR-0006](adr/0006-capture-format-pcm16-device-native.md).

### Storage

Decimal MB, matching how drive capacity is advertised. All *decisions* (free-space
checks, preflight, low-disk abort) are made in raw bytes, so no unit conversion can
change an outcome.

| Format | Per hour | 2-hour meeting |
|---|--:|--:|
| 48 kHz PCM16 mono | 345.6 MB | ~691 MB |
| 48 kHz PCM16 stereo | 691.2 MB | ~1.38 GB |
| 44.1 kHz PCM16 stereo | 635.0 MB | ~1.27 GB |

`bytes_per_second = sample_rate × channels × 2`. With ~197 GB free on `D:`, that is
roughly 140 two-hour stereo meetings before retention matters. **Retention is not
implemented**; nothing is deleted automatically.

---

## 2. Concurrency: callback, queue, writer

```
PortAudio real-time thread          bounded queue            writer thread
──────────────────────────         ─────────────            ─────────────
copy frames  ─────────────────────► ~5 s capacity ──────────► append to .part
enqueue, return                     drops are counted        rotate at boundary
never blocks                        never grows              feed quality meter
```

**The callback copies and enqueues. Nothing else.** No file I/O, no hashing, no
database, no metering, no allocation beyond the frame copy, and it never waits for
the writer. Blocking there makes the driver miss its deadline and the operating
system discards input — audio is lost, permanently, in a meeting that cannot be
repeated.

**The queue is bounded in seconds of audio** (default 5). An unbounded queue would
trade a dropped frame for unbounded memory, which on a 16 GB machine ends in
swapping and losing far more. When it is full, `put_nowait` says so immediately.

**Loss is never hidden.** A full queue means audio was genuinely lost. It is
counted, written to the manifest as an unintentional `gap`, recorded in the
database, surfaced in the UI in red, and audited. No silence is ever fabricated to
paper over the hole: a recording with a known 40 ms gap is useful; one with an
invisible gap is not.

**Locks.** `_writer_lock` guards every `ChunkWriter` and `QualityMeter` access,
because both are reached from two threads (the writer while capturing, the
controller when pausing or stopping) and neither is thread-safe. The audio callback
never takes it. `_writer_idle` marks when the writer has nothing in flight — "queue
is empty" is not sufficient, because the writer pops an item and only then writes
it.

### Measured (fake backend, accelerated)

`python -m mom_igd audio bench --minutes 2 --speed 30`:

| | |
|---|--:|
| Frames written / produced / requested | 960 000 / 960 000 / 960 000 |
| Requested audio delivered | 100 % |
| **Capture drift** (written vs produced) | 0.0 % |
| Dropped frames | 0 |
| xruns | 0 |
| Checksum mismatches | 0 |
| Corrupt chunks | 0 |
| Chunks | 12 |
| Queue high-water | 4 % of capacity |
| Writer mean write | 0.17 ms |
| Chunk finalise, max | 32 ms |
| Process RSS, peak | 33.2 MB |
| Leaked writer threads | 0 |

**Capture drift compares what the writer persisted against what the device
produced** — that is what "no frame lost" means. It deliberately does *not* compare
against what the harness *planned* to produce: at a high `--speed` the fake generator
can run out of wall clock, and charging that to the capture path made a healthy run
report `FAIL`. Coverage is reported separately, and a run that delivered under 99 % of
the requested audio says `INCOMPLETE COVERAGE` rather than passing quietly.

The speed multiplier a machine can sustain is finite: on the development laptop
`--minutes 60 --speed 120` delivers only ~21 % of the requested audio (the capture
drift over the audio it *did* deliver is still 0.0 %). Prefer a combination that
reports 100 % coverage.

**CPU average, CPU p95 and capture RSS on the production device are `NOT
MEASURED`.** An accelerated fake run deliberately saturates the pipeline and says
nothing about a real 1× recording. Those require the manual real-time soak in §8.

---

## 3. Durability order

Every step exists because of a specific failure it prevents.

1. callback → bounded queue
2. writer appends raw PCM to `chunk_NNNNNN.pcm.part`
3. `chunk_NNNNNN.meta.json` is written **before any audio**, recording sample rate,
   channels and format — without it a partial left by a crash is an anonymous blob
   and the audio is unrecoverable in practice
4. at the chunk boundary the partial is flushed and `fsync`ed
5. a valid WAV is built at `chunk_NNNNNN.wav.tmp` from the partial's whole frames
6. the temporary WAV is `fsync`ed, then SHA-256 hashed **from disk**
7. `os.replace` moves it into place — atomic, same volume
8. the database row and the manifest line are written
9. only then are the partial and its metadata removed

Two consequences follow:

* **Any `.wav` that exists is complete.** It was renamed into place only after being
  fully written, flushed and hashed. A crash can leave a `.part` or a `.tmp`,
  never a half-written `.wav`.
* **The partial is raw PCM, not a WAV.** A WAV needs its header patched with the
  final length, so a crash mid-recording would leave an invalid file. Raw PCM has
  no header to patch.

The writer **refuses to overwrite an existing final chunk**. A sequence collision
discards the new data rather than destroying audio that is already verified.

See [ADR-0007](adr/0007-chunking-checksums-and-crash-recovery.md).

---

## 4. File layout and manifest

```
<data_root>/recordings/<meeting_uuid>/<recording_uuid>/
├─ chunk_000000.wav          finalised, hashed, immutable
├─ chunk_000001.wav
├─ manifest.jsonl            append-only, fsync per line (authoritative)
├─ manifest.json             summary + chain hash, written at finalisation
└─ quarantine/               ambiguous or corrupt evidence, never deleted
```

**Names are UUIDs and sequence numbers only.** No meeting title, no participant
name: file names leak into backups, file pickers and error messages.

The manifest is **JSON Lines** so a crash can only damage the final line, which is
detectable and discardable. A single JSON document would have to be rewritten on
every chunk, and a crash during that rewrite would destroy the record of every
chunk before it.

Each chunk record carries: sequence · filename · start/end frame · frame count ·
duration · UTC start/end · monotonic start/end · sample rate · channels · sample
format · byte count · SHA-256 · dropped frames · xrun count · status · recovery
status · finalisation flag · peak/RMS dBFS · clipped samples.

`manifest.json` adds a **chain hash** over the ordered chunk list, so editing the
manifest to match a tampered chunk is detected too. `python -m mom_igd audio verify`
recomputes every hash from disk and additionally compares the database mirror
against the manifest.

Database paths are always **relative** to the data root, and both `relative_dir`
and `filename` carry CHECK constraints rejecting absolute paths, `..` and
separators.

---

## 5. Recovery

On an explicit `audio recover` (or the next `preflight`, which reports pending
work):

* find recordings with a `.part`, an abandoned `.tmp`, or a manifest with no summary;
* read the metadata sidecar for the format;
* recover **only whole frames**; a trailing fragment is discarded and the exact byte
  count recorded;
* build a valid WAV, hash it, append a `RECOVERED` manifest record;
* **never overwrite a valid final chunk** — a stale partial for a sequence that is
  already final is quarantined instead;
* **never delete evidence silently** — anything ambiguous or corrupt moves to
  `quarantine/` with a reason file;
* update the database and write an audit event;
* **idempotent** — a second pass changes nothing.

Recovery is honest about limits: a partial with no metadata sidecar and no known
fallback format cannot be interpreted, so it is quarantined rather than guessed at.

---

## 6. Device identity

**A PortAudio index is not an identity.** Indices shift when a device is plugged,
unplugged or after a reboot. Devices are identified by a fingerprint over host API,
normalised name and input-channel count; the index is looked up fresh every time.

**Never fall back silently.** If the chosen microphone is gone, the answer names the
device that was expected and lists what is present instead. Recording a nine-person
meeting through the wrong microphone cannot be undone.

**Never guess the transport.** `USB` is reported only when Windows says so, read
read-only from `HKLM\...\MMDevices\Audio\Capture\*\Properties`
(`PKEY_Device_EnumeratorName`). A microphone named "USB Audio Device" may not be
one, and the production gate depends on this being real. Unverifiable transport is
reported as `UNKNOWN` and the operator is asked to confirm.

Matching is on **exact equality** of the endpoint description with the head of the
PortAudio name, and **active endpoints are preferred** over the stale records
Windows keeps for every device ever attached. Substring matching was tried and
rejected: a Bluetooth endpoint described simply as "Microphone" matched almost every
device name and made the built-in array resolve to an ambiguous bus.

Rejected as unusable, each with a specific reason: output-only devices · devices
reporting zero input channels · loopback / "Stereo Mix" devices · virtual aggregate
endpoints ("Microsoft Sound Mapper", "Primary Sound Capture Driver") which follow
the Windows default and could change mid-recording · devices Windows does not list
as capture endpoints (PortAudio's WDM-KS host API exposes "PC Speaker" with input
channels) · devices Windows reports as disabled or unplugged.

See [ADR-0008](adr/0008-device-identity-and-no-silent-fallback.md).

### Verified on the development machine

```
IDX  NAME                                        HOST API             CH   RATE  TRANSPORT
  9  Microphone Array (Intel® Smart Sound …)     Windows WASAPI        2  48000  INTERNAL
  5  Microphone Array (Intel® Smart Sound …)     Windows DirectSound   4  44100  INTERNAL
  1* Microphone Array (Intel® Smart              MME                   4  44100  INTERNAL

Verified USB capture devices: 0
```

Seventeen enumerated devices were correctly excluded. **No USB conference
microphone is attached**, so the production gate is not satisfied.

---

## 7. Lifecycle

Recording lifecycle (`recordings.status`, migration 0002):

```
IDLE → PREFLIGHT → ARMED → RECORDING ⇄ PAUSED
                                ↓         ↓
                            STOPPING → FINALIZING → RECORDED ■
                                ↓         ↓
                            FAILED ⇄ RECOVERABLE          CANCELLED ■
```

Coupled to the Phase 1 job state machine, which remains the owner of the meeting
workflow:

* **the job reaches `RECORDING` only after the stream is actually open** — a
  workflow state that ran ahead of the audio would make the audit trail lie;
* it reaches `RECORDED` only after every chunk, checksum, manifest entry and
  database row is final;
* an unrecoverable error moves it to `FAILED`;
* **pause is a recording sub-state**, not a job state.

The route through the job graph is computed from the declared transition table
(`transition_path`), not hardcoded: Phase 1 routes `DRAFT → RECORDING` via `READY`,
and the capture layer should not have to know that.

**One recording per data root**, guarded twice: a lock file (so a second *process*
is refused before it opens the microphone) and a partial unique index in the
database (so a second *row* cannot exist). Either alone is insufficient — the index
cannot stop a second process from grabbing the device, and the lock file cannot
survive a stale process id, so a lock whose owner is gone is cleared rather than
wedging the application forever.

`Start` is idempotent (a second press opens no second stream). `Stop` is idempotent.
Application shutdown with a recording in progress finalises it rather than dropping
it.

`Stop` reports `recording_active: false` in the very response that carries the
closing summary, so the panel re-enables `Start` and unlocks the meeting-title field
immediately instead of waiting for the next poll.

### Which meeting does a recording belong to?

A recording is a child of a meeting, but **Meeting setup is a Phase 9 screen**. On a
fresh install there is therefore no meeting row to attach to, and asking the operator
for an internal database id would be both unanswerable and wrong.

So the panel asks for a **meeting title**, free text, and `POST
/audio/recordings/start` takes **no `meeting_id`**:

* the service creates a minimal **draft meeting** in the same transaction as the
  recording row and the job, and audits it as `meeting.draft_created` in the
  `MEETING` category;
* a blank title becomes `Rapat <YYYY-MM-DD HH:MM> UTC`, because the schema forbids a
  blank one;
* the title is capped at 200 characters and is **never** used to build a path — the
  on-disk layout is `<meeting-uuid>/<recording-uuid>/`, so a title may safely contain
  a participant's name without that name reaching the filesystem;
* a double-clicked `Start` produces **one** meeting, one recording and one job: the
  whole of `start()` holds the state lock, and the second call sees the active
  session and returns its status;
* passing an explicit `meeting_id` still works, for a later phase that has a real
  meeting to attach to. An unknown id is refused with a message that names the
  alternative rather than a bare failure.

Participants, enrolment, consent and the real meeting editor remain Phase 3 / Phase 9.
Phase 2 creates the minimum row a recording cannot exist without, and nothing more.

---

## 8. Commands

```powershell
# devices and readiness -- none of these opens the microphone
.\.venv\Scripts\python.exe -m mom_igd audio devices
.\.venv\Scripts\python.exe -m mom_igd audio devices --all --json
.\.venv\Scripts\python.exe -m mom_igd audio probe --minutes 60

# these DO open the microphone, and only when you run them
.\.venv\Scripts\python.exe -m mom_igd audio probe --open-test
.\.venv\Scripts\python.exe -m mom_igd audio calibrate

# integrity and recovery
.\.venv\Scripts\python.exe -m mom_igd audio verify
.\.venv\Scripts\python.exe -m mom_igd audio verify <recording_uuid>
.\.venv\Scripts\python.exe -m mom_igd audio recover

# no microphone, no GUI
.\.venv\Scripts\python.exe -m mom_igd audio smoke
.\.venv\Scripts\python.exe -m mom_igd audio bench --minutes 10 --speed 60

# readiness gates
.\.venv\Scripts\python.exe -m mom_igd doctor
.\.venv\Scripts\python.exe -m mom_igd doctor --production
```

### What makes a calibration count as evidence

`doctor --production` does not accept a stored `GOOD` verdict on its own. A stale
measurement of the laptop array would otherwise vouch for a USB microphone plugged in
that morning, and the gate would certify something nobody had measured. All of the
following must hold:

| Requirement | Why |
|---|---|
| Verdict is `GOOD` | `TOO_QUIET`, `TOO_LOUD`, `CLIPPING` and `NO_SIGNAL` are unusable |
| Evidence carries a `device_fingerprint` | Without it the record cannot be tied to a microphone at all |
| That fingerprint equals the **selected** device | Calibrate what you will actually record with |
| The device is still present | It may have been unplugged after calibration |
| Its transport is a **verified** `USB` | The whole point of the production gate |
| Timestamp is parseable, and **≤ 30 days old** | Room acoustics, mic position and Windows levels drift |

Each failure is reported with the specific reason and the command that fixes it.
Calibration stores **only metadata** — level statistics, verdict, device identity and
a timestamp. **No calibration audio is written by default**, and the API answers with
a boolean `audio_saved`, never a path.

The 30-day limit lives in `CALIBRATION_MAX_AGE_DAYS`
(`mom_igd/diagnostics/audio_checks.py`).

### Manual acceptance protocol

Run in order, with the production microphone in its meeting position:

1. `audio devices` — confirm the intended microphone appears and its transport reads
   `USB`, verified by Windows.
2. `audio calibrate` — speak from the far end of the table. Verdict must be `GOOD`,
   clipping 0 %, every channel active. Select the device **first**: the evidence is
   bound to whichever device is selected.
3. `python -m mom_igd shell` — open the desktop window, then **Buka panel perekaman**.
4. Select the device, run preflight, type a **meeting title** (or leave it blank for a
   UTC timestamp), then press **Start**. Record 60 seconds with people speaking from
   several seats. No meeting needs to exist beforehand.
5. **Pause**, wait ~10 s, **Resume**, speak again.
6. **Stop** and confirm.
7. `audio verify <recording_uuid>` — all chunks verified, chain hash present, no
   database mismatch.
8. Play the WAV files and confirm every seating position is audible.
9. Kill the application mid-recording (Task Manager), reopen it, run
   `audio recover`, and confirm the salvaged chunk verifies.
10. Real-time soak: record 30–60 minutes at 1×. Watch CPU, RSS, dropped frames and
    xruns. **This is what fills in the `NOT MEASURED` targets.**

---

## 9. Windows microphone troubleshooting

The application **never changes a Windows setting** — no gain, no AGC, no
enhancement, no permission, no registry, no power plan. It reports and recommends.

| Symptom | Cause | Fix |
|---|---|---|
| `No usable capture device` | Everything enumerated was output-only, loopback, virtual or disabled | Plug in the microphone; Settings → System → Sound → Input, enable it |
| Verdict `NO_SIGNAL`, 100 % silence | Muted, or microphone access denied | Check the hardware mute; Settings → Privacy → Microphone → allow desktop apps |
| Verdict `TOO_QUIET` | Input level low, or microphone too far | Raise the level in Sound → Input → Device properties; move the mic to the table centre |
| Verdict `CLIPPING` | Input level too high | Lower the level. Clipping destroys information permanently |
| One channel `INACTIVE` | Mis-wired or genuinely mono-on-stereo device | Check the cable; a mono device on one channel is expected |
| `device is present but disabled or unplugged in Windows` | Windows marks the endpoint inactive | Enable it in Sound settings, then `audio devices` again |
| `not registered as a Windows capture endpoint` | A WDM-KS output node exposing input channels | Choose the WASAPI entry instead |
| Transport `UNKNOWN` | No matching Windows endpoint, or ambiguous | Confirm manually which microphone it is |
| Distant speakers inaudible | Built-in array beamforming | Use a USB conference microphone at the table centre |
| xruns / dropped frames | Machine under load | Close browsers and stop Docker Desktop; a WSL2 VM reserves ~7.6 GiB |

**The built-in array is development only.** Its beamforming and noise suppression
suppress speakers who are not facing the laptop, which loses voices in a
nine-person meeting and makes voiceprints inconsistent for Phase 6.

---

## 10. Operational runbook

**Before a meeting.** `doctor --production` → `audio devices` → `audio calibrate` →
open the shell → preflight → Start.

**Disk.** Recording refuses to start below `audio.min_free_disk_gb` (5 GB default)
and finalises cleanly below `audio.low_disk_abort_gb` (1 GB). Preflight reports how
many minutes the remaining space holds.

**Microphone unplugged mid-recording.** The application does **not** switch devices.
Chunks already finalised stay valid, the open partial is preserved, the recording
moves to `RECOVERABLE`, and the UI says what happened. Reconnect the device and run
`audio recover`.

**Crash or forced termination.** Next start reports pending recovery. Run
`audio recover`; it is idempotent.

**Database and manifest disagree.** The manifest wins — it is written next to the
audio, by the thread that wrote the audio, before the database transaction commits.
`audio verify` reports the mismatch rather than reconciling it silently.

**Security posture.** Audio is stored **unencrypted** until the security phase.
Voiceprints, consent and encryption at rest are Phase 3 / Phase 11. **Do not use a
sensitive meeting recording as test data** before those protections and the
corresponding consent exist. Test fixtures are deterministic generated PCM — never
a recording of a human voice.

---

## 11. Configuration

`config/default.toml`, `[audio]`. Every value is validated on load; an invalid one
aborts startup with a message naming the setting.

| Key | Default | Note |
|---|---|---|
| `preferred_device_fingerprint` | `""` | **Empty on purpose.** A fingerprint identifies one microphone on one machine; put it in `config/local.toml` |
| `preferred_sample_rate` | unset | Unset means the device's native rate — no resampling |
| `max_channels` | 2 | Phase 2 ceiling |
| `chunk_seconds` | 30 | 10–120 |
| `queue_seconds` | 5.0 | 0.25–60 |
| `min_free_disk_gb` | 5.0 | Refuses to start below this |
| `low_disk_abort_gb` | 1.0 | Must not exceed `min_free_disk_gb` |
| `calibration_seconds` | 12 | 10–15 |
| `silence/too_quiet/too_loud` dBFS | −60 / −45 / −1 | Must be strictly ordered |
| `status_poll_hz` | 3.0 | 1–4 |
| `production_requires_usb` | `true` | The production gate |

---

## 12. What Phase 2 does not do

No VAD or speech segmentation · no ASR · no diarization · no speaker embedding or
voiceprint · no speaker identification · no LLM · no MoM generation · no export ·
no model download · no encryption at rest · no consent workflow · no retention
enforcement · no cloud, telemetry or Docker runtime.

`tests/test_cli.py::test_no_phase_3_or_later_module_was_created` and
`test_phase_2_capture_engine_contains_no_speech_or_ai_code` enforce this.
