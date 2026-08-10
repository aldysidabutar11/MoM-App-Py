# Minutes generation and export

From a completed transcript to a Word document the team can circulate. Everything runs on
this machine; nothing is uploaded and no model is downloaded outside one explicit command.

Design decisions and the measurements behind them are in **ADR-0017** (engine and model)
and **ADR-0018** (pipeline, verifier, memory, export). This page is what an operator needs.

---

## 1. What it produces, and what it does not claim

A **draft** minute with four kinds of entry — Keputusan, Tindak Lanjut, Pembahasan,
Isu — each carrying the **timestamp** and the **verbatim quotation** it came from.

Three things it deliberately will not do:

* **It never invents a person.** A PIC is written only when the recording actually says the
  name. "tidak disebutkan" in the PIC column means *the meeting did not say* — not "to be
  decided".
* **It never identifies who was speaking.** There is no voice recognition in this build. A
  name in a minute got there because somebody said it out loud.
* **It never claims to have been checked.** Every rendering says on its face that a machine
  wrote it and no human has reviewed it.

---

## 2. One-off setup

The model is fetched once, by the only command in this application that uses the network:

```powershell
.\.venv\Scripts\python.exe -m mom_igd asr provision mom-llm --data-dir "D:\MoM-IGD-Models-Phase4"
```

2.33 GiB, Apache-2.0, from `Qwen/Qwen3-4B-GGUF`. It is hash-verified, promoted atomically,
then **load-probed** — a model that hashes perfectly but cannot generate a token is not
recorded as ready.

Confirm:

```powershell
.\.venv\Scripts\python.exe -m mom_igd mom status --data-dir "D:\MoM-IGD-Models-Phase4"
```

If it says `NOT PROVISIONED`, minutes generation answers `MODEL_UNAVAILABLE` and stops.
It will not download anything by itself and will not substitute a different model.

---

## 3. Making a minute

From the GUI: **Buka panel notulen** → pick a transcript → **Buat notulen**.

From the command line:

```powershell
.\.venv\Scripts\python.exe -m mom_igd mom generate <recording-uuid> `
    --data-dir "D:\MoM-IGD-Models-Phase4" --export docx
```

Useful flags:

| flag | effect |
|---|---|
| `--export docx\|html\|markdown\|txt` | repeatable; `--export none` generates without writing a file |
| `--hide-unverified` | leaves unverified points out of the **document**; they stay in the database and the document still reports how many were hidden |
| `--json` | machine-readable result |

Then read it back, or write another format, without loading the model at all:

```powershell
.\.venv\Scripts\python.exe -m mom_igd mom show      <recording-uuid> --data-dir "..."
.\.venv\Scripts\python.exe -m mom_igd mom show      <recording-uuid> --unverified --data-dir "..."
.\.venv\Scripts\python.exe -m mom_igd mom export    <recording-uuid> --format html --data-dir "..."
.\.venv\Scripts\python.exe -m mom_igd mom revisions <recording-uuid> --data-dir "..."
```

Documents are written to `<data_root>\exports`, named by the meeting's UUID and the minute
revision.

---

## 3b. Kop surat, nomor notulen, dan tanda tangan

The exported document can carry your organisation's letterhead, a filing reference and a
signature block. All of it is optional and all of it is presentation: **the draft banner
and the per-item verification marks are not configurable**, because they are the part of
the document that protects the reader.

Put the logo in the branding directory — a **bare filename**, PNG or JPEG, no path:

```powershell
copy kop-intramedika.png "D:\MoM-IGD-Models-Phase4\branding\"
```

Then set it in `config\local.toml` (never edit `default.toml` for site settings):

```toml
[mom.document]
organisation = "PT INTRAMEDIKA"
organisation_subtitle = "Instalasi Gawat Darurat · Jl. Contoh No. 1, Jakarta"
logo_filename = "kop-intramedika.png"

document_number_format = "NOT/IGD/{year}/{month}/{seq:03d}"

place = "Jakarta"
show_signature_block = true
signature_roles = ["Notulis", "Pemimpin Rapat", "Mengetahui"]
footer_note = "Dokumen internal"
```

What each part does:

| setting | effect |
|---|---|
| `organisation` | Letterhead name. **Empty means no letterhead at all.** |
| `logo_filename` | Drawn at 0.62 in tall, width from the image's own aspect ratio. A path is refused. Missing, unreadable, not an image, or over 2 MB → the text letterhead is used and the export still succeeds. |
| `document_number_format` | Placeholders `{year}` `{month}` `{day}` `{seq}`. The sequence restarts each month. `""` turns numbering off. |
| `place` | Written above the signatures with the **meeting's** date: "Jakarta, 10 Agustus 2026". |
| `signature_roles` | Up to four blank columns. No name is ever printed on one. |

Two behaviours worth knowing:

* **The filing reference is assigned once.** Revision 2 of the same meeting inherits
  revision 1's number rather than taking a new one — a reference that renumbers itself
  is not a reference, and the copy already in somebody's inbox has to keep resolving.
* **Word gets a real running header and footer.** The reference and the meeting title
  repeat on every page, and the page number is a live `PAGE`/`NUMPAGES` field that Word
  recalculates. The letterhead with its logo stays on page one only.

For a PDF: open the `.docx` in Word and **Save as PDF**. That is the intended route — the
draft is meant to be read and corrected first, so the PDF is made after the review, not
before it. The HTML also prints cleanly if you prefer a browser.

---

## 4. Reading the result: what the marks mean

| mark | meaning | what to do |
|---|---|---|
| *(none)* | the quotation was found in the transcript segment it cites | normal |
| **kutipan ditemukan di segmen lain** | the quotation is real, but the model cited the wrong segment; the citation was corrected | normal; the timestamp is now right |
| **kutipan tidak persis sama dengan transkrip** | close but not word-for-word | worth a glance |
| **BELUM TERVERIFIKASI** | the quotation could not be found at all | **check this against the recording before using it** |
| **nama PIC ... dihapus** | the model proposed a name the recording never says; it was removed | fill in the real owner yourself if there is one |
| **tenggat ... dihapus** | same, for a date | as above |
| **dua bagian rekaman menyebut PIC berbeda** | two parts of the meeting disagree | resolve it with the people involved |
| **PERHATIAN: keputusan ini tampaknya dibatalkan atau diubah pada HH:MM:SS** | a later part of the meeting appears to reverse this decision | **read both, and delete whichever no longer applies** |

At the bottom of every document, **Catatan Pemeriksaan** gives the counts, the coverage
percentage, and any process warnings. A coverage figure below 100 % means part of the
transcript could not be processed and the missing range is named in the warnings.

A reversed decision is also kept out of the summary, so the executive paragraph never
states something the meeting later cancelled.

If the summary contains a number that appears in no point, the document says so explicitly.
That check exists because a fabricated figure in an executive summary is read by people who
never reach the detail.

---

## 5. How long it takes, and what it costs

Measured on the target device (i7-1260P, 12 physical cores, 16 GB):

| | |
|---|---|
| model load | 2.3 s |
| generation | 6–7 tokens/second under the grammar |
| 3-minute transcript, 1 window | 142 s wall, 6 items |
| 30-minute meeting (projected) | ~4 windows, **~9 minutes** |
| 60-minute meeting (projected) | ~6 windows, **~13 minutes** |
| 90-minute meeting (projected) | ~9 windows, **~18 minutes** |
| **peak worker memory** | **~5.4 GB** |

The projections are arithmetic over the measured rates, not timed runs of meetings that
length. Treat them as planning figures.

Peak memory is the number to plan around. It **exceeds** the 2.5 GB heavy-worker budget the
ASR models were measured against, and no setting brings it under: the weights are 2.3 GB
and llama.cpp keeps a second, repacked 1.7 GB copy that cannot be disabled in this build
(ADR-0018 §4). It lives in a process that exits when the run finishes. On a 16 GB machine
with the desktop running there was 8.1 GB free, so it fits — but do not run it beside
anything else large.

**Do not lower `[mom].context_tokens` to save memory.** It is the setting with the largest
effect on run time and the effect is backwards from the intuition: a window is the context
minus a fixed reserve, so a smaller context cuts the *usable* part disproportionately. At
6144 a 90-minute meeting takes about 32 minutes instead of 18, and 4096 is refused by
configuration validation because it would take over two hours.

Transcription and minutes never run at the same time (one heavy model at a time), so a
90-minute meeting is roughly **20 minutes of transcription plus about 18 minutes of
minutes**.

**Recording always wins.** Minutes generation is refused while a capture is live; a capture
is never refused because minutes are being generated. Record the next meeting whenever you
need to.

---

## 6. Re-running

Re-running writes a **new revision** and deactivates the previous one. Nothing is edited in
place and nothing is overwritten, so you can always see what changed. `mom revisions` lists
them; `mom show --revision N` reads an older one.

Temperature is zero and the seed is fixed, so the same transcript produces the same minute.

---

## 7. When something is wrong

| symptom | cause | fix |
|---|---|---|
| `MODEL_UNAVAILABLE` | the model is not provisioned or failed its probe | `asr provision mom-llm` |
| `NO_TRANSCRIPT` | the recording has no completed transcript | run `asr transcribe` first |
| refused, "rekaman ... sedang berjalan" | a capture is live | stop the recording |
| refused, "satu proses berat sudah berjalan" | a transcription or another minute is running | wait |
| `NO_ITEMS` | the model found no decisions, actions, discussions or issues | check the transcript is real speech and not silence |
| many `BELUM TERVERIFIKASI` | the transcript quality is poor, so quotations do not match | improve the recording; see `docs/phase-4-offline-asr.md` |
| coverage below 100 % | a window failed to parse or was truncated | the warnings name the affected minutes; re-run |

---

## 8. What is deliberately not here

* **Who said what.** Diarization (Phase 5) and voice identification (Phase 6).
* **Approval.** A minute is a `DRAFT` and there is no code path that marks one approved —
  the review workflow is Phase 9.
* **Editing.** Minutes are read-only in this build. Edit the exported document.
* **A quality measurement.** Nothing here measures how good the minutes are, and there is
  no reference minute to measure against. The machinery is tested; the writing is not
  scored. Read the draft before you send it.
