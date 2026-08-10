# ADR-0018 — The minutes pipeline: windows, a non-LLM verifier, and what a draft claims

* **Status:** Accepted
* **Relates to:** ADR-0004 (one heavy worker), ADR-0009 (identity), ADR-0015 (provisioning),
  ADR-0016 (transcription pipeline), ADR-0017 (minutes engine)

## Context

ADR-0017 chose the engine. What remained was the shape of the pipeline around it: what the
model is shown, what it is allowed to assert, who checks it, what is stored, and what the
resulting document is permitted to claim.

The governing fact is that a language model produces fluent text whether or not the text is
true, and a minute is a document people act on. A wrong figure gets budgeted. A wrong
deadline gets missed. **A wrong name gets someone blamed for work they never agreed to.**
Every decision below is downstream of that ordering.

## Decision

### 1. The model proposes; a non-LLM verifier decides

Every extracted item carries a **verbatim quote** and the **segment ids** it came from, and
`mom_igd/mom/verify.py` — which imports no model and must never import one — checks the
pairing by string matching before the item is stored.

Asking a language model whether a language model's output is faithful produces a confident
yes at a rate that tracks fluency, not truth. A check performed by the same class of system
that produced the claim is not a check.

Four things are verified, in descending order of the damage getting them wrong does:

* **The owner.** A distinctive part of the name must actually appear in the transcript,
  honorifics stripped, because "Pak" grounds nothing. Ungrounded, the owner is **removed**
  and the removal is recorded on the item and counted on the minute.
* **The quote.** Located in the cited segments (`VERIFIED`), located elsewhere in the same
  window and re-cited (`REBOUND`), or not located at all (`UNVERIFIED`).
* **The due date.** Grounded against the window that stated the commitment, or removed.
  Accepting one from elsewhere in the meeting would attach another item's deadline to this
  one. **Every word that names a date must have been said** -- not merely one of them. The
  first version accepted any matching token, and the model mis-sampled "hari Kamis" as
  **"hari Kam4"**, which passed on the strength of "hari" and would have printed a deadline
  that exists in no language. Scaffolding words ("hari", "sebelum", "paling lambat") are
  stripped before the check for exactly that reason.
* **Numbers in the summary.** Every digit-string must exist in the items the summary was
  written from. A fabricated figure in an executive summary is read by people who never
  reach the detail.

Matching tolerates what differs between a quote and a transcript — punctuation, casing, an
inserted filler — and rejects a different sentence. The near-match threshold is a
**contiguous** token-overlap ratio, so a quote assembled from words scattered across the
window fails even when every word is present. That case is not hypothetical: it is what a
hallucinated quote looks like.

**An unverified item is kept, shown and marked.** Deleting it hides from the reviewer that
the model produced it; keeping it unmarked presents a guess as a record. Neither is
acceptable, so the state is a stored column and every renderer is required to show it.

### 2. The roster canonicalises a spoken name and can never introduce one

The participant roster is **not** in the prompt. Handing a model a list of names is handing
it names to attach to unattributed statements, which is precisely the failure the owner
rule exists to prevent — and a model that has learned the shape of a minute knows an action
item usually has an owner.

The roster is consulted **after** grounding has already succeeded against the transcript,
and only to correct the spelling of a name the meeting genuinely said: "Bu Sinta" becomes
"Sinta Wijaya", so the item is searchable by the name the organisation uses. A name that
was never spoken is removed even when it is on the roster. A test asserts both directions.

**No speaker attribution anywhere.** Phase 4 assigns none and this stage invents none: an
owner is recorded only because the transcript *names* one. There is no `speaker` column and
no foreign key from `minute_items` to `participants` — linking them would invite resolving
an ambiguous first name to whoever is on the roster, and "whoever was speaking" is a guess
about who is responsible for what.

### 2b. A decision the meeting took back is flagged, never silently listed

A meeting reverses itself. Observed on a real run: *"UAT will run on the spare server"* at
03:07, and *"cancel that decision"* at 04:13 — both rendered as plain decisions, one above
the other, neither marked. A reader skims the decisions section and actions whichever they
see first, so this is more dangerous than a duplicate and more dangerous than an omission.

The detector is lexical and adds a caution; it never deletes. A **later** item must contain
a reversal word ("batalkan", "diralat", "diganti", …) and then be linked to the decision by
one of two patterns.

The first version linked them by counting shared distinctive words, and measurement killed
it: the reversal shared **exactly one** word with the decision it cancelled, and that word
was the filler *"begitu"*. People do not restate a decision in order to cancel it — **they
point at it**: *"keputusan tadi kita batalkan"*. The subject words live in the earlier
sentence and the reversal words in the later one, so word overlap is close to the worst
available signal.

So the rule is: a reversal word, **plus** either a back-reference ("tadi", "sebelumnya"),
in which case the referent is the *nearest preceding decision*, or three shared distinctive
words for the less common restated form. The content-word floor is three characters, not
four — "UAT" is exactly the token that links two sentences about the same thing, and a
four-character floor drops every acronym in a technical meeting.

A flagged decision is excluded from the summary and reported in the warnings. A false
positive costs a caution a reviewer settles from the quote already beside it; a false
negative leaves the minute where it was. Nothing is removed, and no model is involved.

### 3. Extraction is windowed; the summary is written from verified items only

A ninety-minute meeting is about twelve thousand words. A small model asked to extract
decisions from all of it at once returns a handful from the first few minutes and nothing
from the rest — the failure is not truncation, it is attention thinning out, and it is
silent. So the transcript is cut into overlapping windows of about 4700 tokens, each read
separately, with every line marked `[S12]` so a citation has something to mean.

Windows overlap by fifteen seconds, because a decision stated across a cut would otherwise
be seen by neither side. The duplicates that produces are merged afterwards by string
similarity and time proximity; the omission could not have been recovered at all.

Merging is calibrated on measured pairs, not intuition: real overlap duplicates score
**0.96**, two different agenda points score **0.86**, and two genuine *paraphrases* of one
decision score **0.54**. So the text threshold sits at 0.90 (0.95 for short texts, where
one differing word is a small fraction of the characters and the whole of the meaning), and
paraphrases are caught by a different route — a shared citation plus matching **quotes**,
which are verbatim transcript and barely can diverge. A containment check merges an item
that wholly restates another, which the ratio misses whenever the two lengths differ much.

The prompt carries one **worked example**. A small model follows a shown shape far more
reliably than a described one, and the errors it fixes were observed rather than
anticipated: an agenda listing extracted as a decision, and a schedule change filed as an
action because it was phrased actively. Measured on the same transcript, the sharpened
prompt took nineteen items down to eleven with no real content lost. The example's names
and dates appear in no transcript, so if the model ever copies from it the verifier fails
to locate the quote and the item is marked `UNVERIFIED` rather than believed.

**The summary is generated from the verified item list, never from the transcript.** That
is the load-bearing choice of the reduce stage: a summary written from verified items
cannot introduce a fact that was not already checked against the audio, so the least
checkable output of the model is derived from its most checked one. Written from the
transcript instead, it would put an unverifiable paragraph at the top of the document —
exactly where a reader trusts most and checks least.

**Verification runs in the parent process, never in the worker.** The worker is a dumb
prompt executor: prompts in, text out. Everything else is ordinary code in the parent. That
costs a second model load (2.3 s, page cache warm) and buys two things — the verifier
cannot be influenced by the thing it checks, and the entire pipeline is testable end to end
with a fake prompt runner. Which is why the branches that matter are tested: a truncated
window, a window that returned nothing, a window whose answer was not JSON, a hallucinated
quote, an invented owner. A model-dependent test would be too slow and too
non-deterministic to assert on any of them.

### 4. Peak memory is 5.1 GB, and the 2.5 GB budget does not survive contact with a 4B model

Measured on the target device from llama.cpp's own allocation report:

| | |
|---|---|
| mapped model weights | 2363 MiB |
| **`CPU_REPACK` copy** | **1683 MiB** |
| KV cache (`n_ctx` 8192) | 1152 MiB |
| compute buffer (`n_batch` 256) | 153 MiB |
| **total** | **5351 MiB** |

Observed peak working set at `n_ctx` 6144 was **5084 MiB** against a predicted 5063 — so
the model of where the memory goes is right, and the 8192 figure is that prediction plus
the measured KV difference.

The surprise is `CPU_REPACK`: llama.cpp re-lays the q4_K weights out for AVX-512 and keeps
that second copy resident. It is **not optional in this wheel** — `GGML_NO_REPACK`,
`LLAMA_NO_REPACK` and `GGML_CPU_REPACK` were each set and each measured, and the buffer
stayed at 1683.28 MiB in all three.

What *was* reduced, on evidence:

* `n_batch` 512 → 256 saves 281 MiB for **no measurable throughput change** (7.11 against
  7.18 tokens a second on the same prompt). Free, so taken.
`n_ctx` went the other way, and the reason is worth recording because the intuition is
backwards. Context was first set to **6144** to save 288 MiB. But a window is `n_ctx` minus
a fixed 3448-token reserve, so lowering the context attacks the *remainder*, not the total,
and the number of model calls rises non-linearly. Projected for a 90-minute meeting:

| `n_ctx` | window | windows | projected wall time | peak |
|---|---|---|---|---|
| 4096 | 648 | 81 | **142 minutes** | 4775 MiB |
| 6144 | 2696 | 17 | 32 minutes | 5063 MiB |
| 8192 | 4744 | 9 | **18 minutes** | 5351 MiB |

288 MiB to halve the run is an easy trade on a machine measured with 8.1 GB free, so the
default is 8192. The configuration **floor is 6144**: a value that silently makes the
application eight times slower is a trap, not a tuning knob.

**The 2.5 GB heavy-worker budget in ADR-0016 cannot be met and is not claimed.** The
weights plus their repacked copy are 4.0 GB before a single token of context. That budget
was measured against ASR models; a four-billion-parameter model does not fit in it. What is
claimed instead is 5.1 GB, in a spawned process that exits when the run ends, on a machine
with 16 GB — of which 8.1 GB was free with the desktop running. `doctor` reports the figure
so an operator sizing a machine does not have to find it in an ADR.

KV-cache quantisation (`q8_0` with flash attention) was measured at a further 405 MiB
saving and **not adopted**: it changes the attention kernel for a quality effect that
cannot be measured here, and 405 MiB does not change the conclusion when repack dominates.

### 5. Nothing is dropped quietly

A window that failed to parse, one that returned no answer, one that hit the sixteen-item
ceiling, one truncated twice — each becomes a warning naming the affected minutes of the
meeting, and coverage is reported as a percentage on the document itself.

Coverage is measured in **segment time**, and getting that wrong is instructive. The first
version summed window spans against the transcript's last timestamp and reported **68 % on
a run where every segment had been read**: silence before the first segment belongs to no
window and never could, and segment times are not monotonic, so the last segment's end is
not the window's end. A warning that fires on a complete run is worse than no warning,
because it teaches the operator to ignore warnings.

A truncated window is halved and retried **once**. Twice truncated is reported, not retried
into a loop; the meeting is not going to get shorter.

### 6. Revisions, not updates — and no `APPROVED`

Re-running writes revision *n+1* and deactivates the previous one. A partial unique index
makes "at most one current" a database guarantee, so two concurrent runs cannot both end up
current, and a `BUILDING` row that crashed cannot be left behind as the meeting's minute
(a `CHECK` enforces that too). Same reasoning as transcripts: a reviewer who has read a
minute must be able to see what changed underneath them.

`minutes.status` has no `APPROVED` value. Approval is a human act with its own audit
requirements and this phase does not implement it; leaving the value available would let
something write it.

### 7. Export is a document model with four renderers, and no dependency

Markdown, HTML, DOCX and plain text are built from **one** block list. Four hand-assembled
layouts is how three of them end up missing the draft banner, and the one that drifts is
always the format nobody opened during testing but everybody forwards.

Three things every rendering carries, enforced in the builder rather than trusted to each
writer: the **draft banner**, the **verification mark** on every unverified item, and the
**coverage line** when part of the transcript produced nothing. A parametrised test asserts
all three in all four formats.

DOCX is written with stdlib `zipfile` and hand-authored OOXML — a Word document is a zip of
XML, and about two hundred lines avoids adding `python-docx` to a project that scrutinises
every dependency. Output is **deterministic**: every zip entry gets a fixed timestamp, so
the SHA-256 recorded on the export row identifies the file. Child order inside `w:pPr` is a
schema sequence, not a preference; out of order, Word refuses the file with an
"unreadable content" prompt that names nothing, and a test walks the sequence.

The HTML fetches nothing — no stylesheet, script, font or image. A remote asset would be a
network call from a document an offline system produced, and it would break the moment the
file left the machine. A letterhead logo is inlined as a `data:` URI for the same reason,
and embedded as a real media part in the DOCX.

**Letterhead, filing reference and signature block are presentation, and are configurable.
The draft banner and the verification marks are neither.** That division is the whole point
of allowing branding at all: an official-looking heading is precisely what makes a reader
assume a person wrote the document, so the correction has to arrive above the content and
must not be switchable. A parametrised test asserts the banner and the marks survive in all
four formats *with* branding applied.

The signature block prints **no names** — blank columns and a rule — and says in the
document that the application approved nothing. Offering to fill in a notetaker's name
would be the application asserting who took responsibility, which is exactly what it
cannot know and what Phase 9 exists to record.

The **filing reference** is stored (migration 0007), not derived at render time, and a
later revision inherits it. Derived, the same minute would renumber itself as soon as
another meeting was minuted first, so the copy in somebody's inbox and the copy exported
next week would carry different references while being the same document.

Image dimensions are read from the PNG header and the JPEG SOF marker directly, about forty
lines, rather than adding Pillow for two integers. Anything unrecognised, unreadable or
over 2 MB is ignored and the text letterhead stands: an export must not fail because
somebody moved a logo. Getting that right required fixing a real defect — the warning that
made it non-fatal used `extra={"filename": ...}`, which collides with a reserved
`LogRecord` attribute and raises `KeyError`, so every one of those paths was fatal until a
test said otherwise.

**PDF is deliberately absent.** Word's Save-as-PDF produces one from the `.docx`, and the
route matters: the draft is meant to be read and corrected before it is circulated, so the
PDF is made after the review. A one-click PDF of an unreviewed draft is a convenience
pointed the wrong way, and it would cost a new dependency to build.

Filenames carry the meeting's **UUID**, never its title (ADR-0009): two meetings called
"Rapat Mingguan" must not collide, and a display name is not an identifier.

## Consequences

* A ninety-minute meeting is about nine windows and projects to **eighteen minutes**, on
  top of roughly twenty minutes of transcription. That projection is arithmetic over
  measured rates -- 6.8 tokens a second generated, roughly 500 a second evaluated -- not a
  timed end-to-end run of a ninety-minute meeting, which has not been done.
* Generation is refused while a capture is live; a capture is **never** refused because a
  minute is running. The list of live capture states is imported from the transcription
  service rather than restated — a local copy of it drifted immediately, inventing a state
  the schema does not have and omitting four real ones, and a test now reads migration
  0002's partial unique index to keep the single copy honest.
* `mom_igd/mom/` imports nothing from `mom_igd.enrollment`, and `mom_igd/audio/` imports
  nothing from `mom_igd.mom`. Roster size never gates a minute, exactly as it never gates
  recording.
* Every export is recorded with its SHA-256, its format, and whether it contained
  unverified items — so "which revision is this document, and what was in it?" stays
  answerable after the file has been forwarded.

## What this does not license

**No quality claim.** Nothing here measures how good the minutes are, and there is no
reference minute to measure against. The demonstration run on realistic Indonesian meeting
text produced six items, all verified, with both named owners and both stated deadlines
correct and no owner invented for the action the meeting left unassigned — that is
*evidence the machinery works*, on one typed transcript, and it is not an accuracy result.
It says nothing about ASR either, because the text was typed rather than transcribed.

**Every minute is a draft.** No code path in this package writes an approval, and every
rendering says on its face that a machine wrote it and nobody has checked it.

## Alternatives rejected

**An LLM judge to verify the extraction.** Costs as much as the extraction, and is itself
unverifiable.

**Dropping unverified items.** A cleaner document that hides what the model did.

**Putting the roster in the prompt.** Better owner coverage, and it manufactures the one
error that matters most.

**One extraction call over the whole transcript.** Simplest, and it silently returns
nothing from the last hour of a long meeting.

**`python-docx`.** A dependency to vet, pin and carry offline for four block types.
