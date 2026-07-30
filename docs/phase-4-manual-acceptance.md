# Phase 4 — manual acceptance guide

**Status: PHASE 4 READY FOR MANUAL FUNCTIONAL TESTING — ACCURACY ACCEPTANCE PENDING.**

This is the operator's script for the one thing automated tests cannot do: speak into a
microphone and check that what comes back is a usable transcript. Everything else about
Phase 4 has been verified automatically (2 000+ tests) and against the real models with
generated audio; what is missing is **a human voice**, and nothing in this repository can
supply that.

Read section 0 before you start. It says what this test does and does not establish, and
that distinction is the point of the whole exercise.

---

## 0. What this test does and does not establish

**It establishes** that the application records, normalises, segments, transcribes,
re-transcribes the uncertain parts, normalises technical terms, persists everything and
survives a restart — on this machine, with no network access.

**It does not establish accuracy.** A functional test tells you the machinery works. It
cannot tell you the word error rate, because that needs a *reference transcript* — a
human-written record of exactly what was said — and comparing the model against its own
output measures self-consistency, not correctness. Accuracy acceptance needs
[section 6](#6-what-is-still-missing-after-this-test).

**Use the internal microphone if that is all you have.** It is fine for a functional test.
It is **not** acceptable as accuracy evidence for a real meeting: the Intel Smart Sound
array applies beamforming and noise suppression that actively suppress speakers who are not
facing the laptop, and that gets worse the larger the room. A single omnidirectional USB
conference microphone at the centre of the table is required before any production claim.

**Nothing you do here touches production data.** Everything runs against the acceptance
root `D:\MoM-IGD-Models-Phase4`. The production root `D:\MoM-IGD-Data` is deliberately
untouched, still at schema 3, and the preflight script refuses to run against it.

---

## Part A — Run the preflight

```powershell
cd D:\Aldy\MoM-IGD

powershell -ExecutionPolicy Bypass `
  -File .\scripts\phase4_acceptance_preflight.ps1
```

It checks the interpreter, free disk and memory, the database schema and audit chain,
`doctor`, both models (re-hashing every byte), the capture devices, and a real-model
offline smoke test. It changes nothing, downloads nothing and never opens the microphone.

**Read the verdict:**

| Verdict | Meaning |
|---|---|
| `READY FOR MANUAL FUNCTIONAL TESTING`, exit 0 | continue to Part B |
| a `WARN` line | expected at this phase. Read it, then continue |
| `NOT READY`, exit 1 | an engineering failure. Stop; testing a broken environment produces evidence about the environment, not the application |
| `REFUSED`, exit 2 | you pointed it at the production root |

The `WARN` lines you should expect on this machine, and why each is fine here:

* **`free_ram`** — the pass-2 model peaks near 1.9 GB. Close other applications before a
  long test.
* **`doctor:usb_conference_mic`** — no USB conference microphone. Fine for a functional
  test; see section 0.
* **`doctor:consent_text`** — the biometric consent wording is still a draft. It matters
  for *enrollment*, which this test does not exercise.
* **`audio_devices`** — internal microphones only. Same as above.

If you skipped the smoke to save time (`-SkipAsrSmoke`), that also shows as a `WARN`.

---

## Part B — Open the application

```powershell
.\.venv\Scripts\python.exe -m mom_igd shell `
  --data-dir "D:\MoM-IGD-Models-Phase4"
```

The window blocks until you close it. Nothing listens on anything but `127.0.0.1`.

**There is already one recording in this data root**, titled
*phase-4 end-to-end verification* (UUID starting `b890c906`). That is a 24-second
**synthesised** artefact from the automated end-to-end run — not a recording of anybody. It
is kept as evidence. You can open its transcript to see what the panel looks like with data
in it; the text is meaningless, because synthesised formants are not speech. Ignore it
otherwise.

---

## Part C — Functional speech test

The main test. Budget 15 minutes.

### C.1 Record

1. Click **Buka panel perekaman**.
2. Choose a microphone in the **Microphone** card and press **Gunakan perangkat ini**.
3. Press **Jalankan preflight** and confirm it passes (disk, device, permission).
4. Press **Kalibrasi** and speak normally for the 12 seconds. Check the verdict: aim for
   `BAIK`. If it says too quiet or too loud, adjust the Windows input level — the
   application deliberately never changes your device settings, it only tells you which
   one is wrong.
5. Type a **meeting title**, for example `Uji fungsional Phase 4`.
6. Press **Mulai merekam**.
7. **Speak Indonesian naturally for 60–90 seconds.** Do not read a script in a monotone;
   speak the way you would in a meeting. Include these terms, because the terminology
   normaliser and the technical-term handling are what you are testing:

   | Include | Example |
   |---|---|
   | `API` | "integrasi API-nya belum selesai" |
   | `database` | "database pasien sudah dimigrasikan" |
   | `deployment` | "deployment ke server produksi hari Kamis" |
   | `BPJS` | "klaim BPJS bulan ini naik" |
   | `server` | "server di ruang IT perlu di-restart" |
   | an action item | "Pak Budi tolong siapkan laporan" |
   | a date | "tanggal 15 Agustus" |
   | a time | "jam sepuluh pagi" |
   | a number or amount | "sekitar dua ratus lima puluh juta rupiah" |

8. **Press Jeda (pause) once**, wait about five seconds, then press **Lanjutkan (resume)**
   and keep talking for another 15 seconds. This is the gap-handling path: a pause is a
   real hole in the master timeline, and the working copy fills it with recorded silence so
   every later timestamp still lines up.
9. Press **Berhenti** (stop).
10. Press **Verifikasi integritas** and confirm every chunk checksum matches the manifest.

**Write down the recording UUID** shown after the stop — you will see it in the
transcription panel's list, but it is useful to have.

### C.2 Transcribe

11. Click **Buka panel transkripsi**.
12. The **Model** card should show pass 1 and pass 2 as ready.
13. Choose your recording in **Rekaman selesai**. It should be at the top of the list.
14. Check the detail card: duration, chunk count, manifest `VERIFIED`.
15. Press **Jalankan preflight**. Every blocking check must pass. `model_pass2` failing is
    not blocking; anything else is.
16. Press **Proses transkripsi**. The button is disabled until the server says the
    recording is eligible and the preflight passed — that is deliberate.
17. Watch the panel:
    * the pill reads **Berjalan**,
    * the **elapsed time** counts up,
    * stages appear one by one: `validate_audio`, `normalize_audio`, `vad`, `asr_pass1`,
      `asr_pass2_selective`, `normalize_terminology`,
    * the window stays responsive — you can scroll and click while it runs.
18. Expect roughly **10–40 seconds** for 90 seconds of audio. The measured real-time
    factor is about 0.31 including model load, and pass 2 adds a 1.5 GB model load when it
    has anything to do.
19. When it finishes, check the transcript:
    * text is present and recognisably what you said,
    * each line has a **timestamp**,
    * uncertain lines carry a **rendah** (low-confidence) badge,
    * every line shows **`UNASSIGNED`** where a speaker name would go,
    * **no invented speaker names anywhere** — if you see one, that is a serious defect,
      report it,
    * the **Pass 2** card shows the budget, what was used, and the reason codes for each
      region it re-transcribed,
    * technical terms are spelled correctly (`deploy`, not `deploi`).
20. Check the cost card: RTF, peak worker memory (expect under 2 000 MiB), and the number
    of terms the glossary corrected.

### C.3 Restart

21. Close the window completely.
22. Reopen it with the same command from Part B.
23. Open the transcription panel and select the same recording.
24. Press **Muat transkrip tersimpan**. The transcript must come back complete, with the
    same timestamps.

---

## Part D — Cancel and re-run test

Budget 10 minutes. This tests that stopping a long job leaves the database in a state you
can understand, and that re-running does not redo work that is provably still valid.

1. Record a **second** meeting, 3–5 minutes this time. Same panel, same steps.
2. Start the transcription.
3. **While a stage is running**, press **Batalkan**. Cancellation is cooperative and lands
   at the next boundary, so it may take a few seconds — the pill should read
   *pembatalan diminta* in the meantime.
4. Confirm the pill ends at **Gagal** with an error naming `CANCELLED`, and that no
   transcript appears.
5. Close and reopen the application.
6. Select the same recording. Press **Jalankan preflight**, then **Proses transkripsi**
   again (the button now reads *Proses transkripsi ulang* if a revision already exists).
7. **Confirm in the stage list** that `normalize_audio` says *reused the existing working
   copy* and `vad` says *reused the existing run*. That is the checkpointing: the audio
   derivation and the segmentation are not redone, because their recorded SHA-256 and
   configuration hash still match.
8. Let it finish. Confirm exactly **one** transcript revision is active — the panel's
   detail card shows the revision count, and only the newest is marked `(aktif)`.

---

## Part E — Result form

Fill this in and send it back. An honest "no" is worth more than an optimistic "yes".

| # | Check | Result | Notes |
|---|---|---|---|
| 1 | Preflight verdict and exit code | | |
| 2 | GUI starts, no stuck overlay or dark modal | | |
| 3 | Microphone selection works | | |
| 4 | Audio preflight passes | | |
| 5 | Calibration verdict | | dBFS if shown |
| 6 | Recording starts and the level meter moves | | |
| 7 | Pause and resume both work | | |
| 8 | Stop finalises; integrity verification PASS | | |
| 9 | Recording appears in the transcription list | | |
| 10 | Transcription preflight passes | | |
| 11 | Transcription starts from the button | | |
| 12 | Progress moves through every stage | | list any that failed |
| 13 | Elapsed time counts up | | |
| 14 | Application stays responsive during the run | | |
| 15 | Wall-clock time for the run | | seconds |
| 16 | Peak worker memory reported | | MiB |
| 17 | Transcript contains text | | |
| 18 | Timestamps present | | |
| 19 | Low-confidence lines marked | | how many |
| 20 | Every line says `UNASSIGNED` | | |
| 21 | **No invented speaker names** | | |
| 22 | Technical terms spelled correctly | | which ones were wrong |
| 23 | Pass 2 ran / was skipped, and the reason shown | | |
| 24 | Transcript survives a restart | | |
| 25 | Cancel changes the state correctly | | |
| 26 | Re-run reuses the working copy and VAD run | | |
| 27 | Exactly one active revision after the re-run | | |
| 28 | No network activity observed | | |
| 29 | Any error message seen | | copy it verbatim |
| 30 | **Subjective quality of the transcript** | | your honest impression, not a number |

For 30, describe what you noticed — which words were wrong, whether numbers and dates
survived, whether English technical terms came through. That impression is not a
measurement and will not be recorded as one, but it is the most useful single thing you can
report, because it says where to look next.

---

## 6. What is still missing after this test

Even with every box above ticked, these remain open:

1. **Accuracy is unmeasured.** Needs a reference transcript. See
   [`examples/asr-evaluation-manifest.example.json`](examples/asr-evaluation-manifest.example.json)
   and the commands in the next section.
2. **A USB conference microphone.** Required before any production accuracy claim.
3. **Speaker separation.** Phase 5 (diarization) and Phase 6 (voice identification).
   Until then every segment is `UNASSIGNED`, on purpose.
4. **Legal review of the consent wording.** Version 1.0 is a draft.
5. **The production data root is still at schema 3.** It will be migrated only after you
   confirm this functional test, and as a separate, deliberate step.

### Building an evaluation corpus

```powershell
# 1. Hash your audio.
Get-FileHash -Algorithm SHA256 "D:\Eval\rapat-01.wav"

# 2. Copy the template and fill in every field.
Copy-Item .\docs\examples\asr-evaluation-manifest.example.json D:\Eval\corpus.json

# 3. Check the manifest before spending an hour of decode time on it.
.\.venv\Scripts\python.exe -m mom_igd asr bench `
  --data-dir "D:\MoM-IGD-Models-Phase4" `
  --manifest "D:\Eval\corpus.json" --validate-only

# 4. Run the real benchmark.
.\.venv\Scripts\python.exe -m mom_igd asr bench `
  --data-dir "D:\MoM-IGD-Models-Phase4" `
  --manifest "D:\Eval\corpus.json" `
  --out "D:\Eval\accuracy-result.json"
```

The manifest **must** declare `consent_status` as `granted`, `public-licensed` or
`synthetic` for every sample. Benchmarking somebody's voice is processing biometric data,
and the loader refuses audio without recorded consent — that is not a formality.

Keep the audio and the transcripts **outside this repository**. Nothing in
`D:\Aldy\MoM-IGD` may ever contain a recording of a person.

### Real-speech smoke, once you have one consented recording

```powershell
.\.venv\Scripts\python.exe -m mom_igd asr smoke `
  --data-dir "D:\MoM-IGD-Models-Phase4" `
  --audio "D:\Eval\rapat-01-16k-mono.wav"
```

The file must already be 16 kHz mono PCM16. The smoke deliberately does **not** convert it:
resampling is a pipeline stage with its own provenance, and a diagnostic should not quietly
alter an operator's audio.
