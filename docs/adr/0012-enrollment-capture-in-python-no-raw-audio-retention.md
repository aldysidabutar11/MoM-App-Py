# ADR-0012 — Enrollment capture stays in Python; no raw enrollment audio is retained

* **Status:** Accepted
* **Phase:** 3

## Context

Enrollment needs roughly 30–60 seconds of a person's voice. Two independent questions
follow: **where does that audio get captured**, and **is any of it kept**.

The application already has a desktop shell (pywebview/WebView2) with a page that
drives the recording panel, and a Phase 2 capture engine in Python. Either could in
principle record the samples.

## Decision

### The browser never touches the microphone

Capture runs inside the Python process, through the Phase 2 backend and the
already-selected device. The page sends "record a sample" and receives levels, a
duration and a quality verdict. It never receives audio.

The alternative — `getUserMedia` in the page — was rejected. It would mean:

* a second, independent microphone path with **different device rules**, bypassing the
  Phase 2 fingerprint identity, the no-silent-fallback rule and the transport
  verification that the production gate depends on;
* a biometric sample in browser memory, where it can be cached, dumped in a crash
  report, or read by anything with script access to the page;
* the sample travelling over HTTP as base64 or multipart, which puts voice data in
  request logs and proxy buffers for no benefit;
* a second browser permission prompt on a device where the operator has already
  chosen the microphone.

None of that buys anything, because the microphone is already reachable from Python.
So there is **no audio upload endpoint**, and a test asserts every enrollment route
accepts `application/json` only. Another test greps the shipped page for
`getUserMedia`, `MediaRecorder`, `AudioContext` and friends.

`EnrollmentService.add_sample()` takes PCM bytes, and that is safe precisely because
the only caller is `EnrollmentCaptureController` in the same process. A test asserts
no route calls `add_sample` directly, so PCM has no route by which to arrive over
HTTP.

### The capture callback copies and returns

Same discipline as Phase 2, for the same reason: a blocked callback is lost audio. The
callback copies bytes into a bounded buffer and increments a counter. No file I/O, no
embedding, no encryption, no database access, no lock it can wait on.

A driver overflow is counted as an **xrun only, never as a frame count**. The driver
does not report how many frames it discarded, so any number would be invented. The
quality gate rejects on any xrun at all, so the sample is refused either way — without
fabricated evidence.

### Ceilings are enforced in bytes

`MAX_SAMPLE_BYTES` and `MAX_TOTAL_CAPTURE_BYTES` are byte limits, not second limits,
so a device that misreports its sample rate cannot grow the buffer past the ceiling by
claiming a short duration. An overflow **rejects the sample** rather than truncating
it silently: a truncated biometric sample is worse than a missing one, because it
looks valid.

### Raw enrollment audio is never written to disk

It exists only in a bounded in-memory buffer, and only until the embedding is
computed. It is released on success, rejection, cancellation, exception and
application shutdown alike — the cleanup lives in one `finally` so a branch added
later cannot forget it.

Total enrollment audio is under a minute, so there is no reason to spool it. Keeping
it would mean holding the most sensitive possible form of the data — the actual voice,
not a template — for no functional gain, and every copy of it would then need the
encryption, deletion and retention story that the template already has. The consent
text states this to the participant, and a test walks the data root for `.wav`,
`.pcm`, `.raw`, `.part` and `.tmp` after a full enrollment.

If a future phase ever genuinely needs to keep enrollment audio, it must be encrypted
from the first byte and it needs its own ADR reversing this one.

### One capture at a time, refused rather than queued

A concurrent second `capture_sample` is **refused immediately** via a non-blocking
claim. An earlier version held one lock for the whole seven-second capture, which
meant a second call queued behind it and then recorded an extra sample the operator
never asked for — and a Cancel from the UI thread blocked on it too. `abort()`
therefore deliberately takes no claim lock, so Cancel takes effect at once.

### Mutual exclusion with meeting recording

Enrollment takes the **same** `SingleRecordingLock` at the **same** path
(`temp/recording.lock`) that `RecordingService` uses, so
`meeting recording XOR enrollment` holds across processes — which an in-process
boolean cannot do. The controller does **not** acquire it a second time; the service
holds it for the whole session, and a duplicate `O_EXCL` acquire in the same process
would abort a healthy enrollment.

`SingleRecordingLock.read_live_holder()` is the one public answer to "is the microphone
actually in use?". Callers must not use `.held`: the lock object is *shared*, so
`held` is true whenever this process owns it — including when it owns it for the other
activity, which is exactly the case to refuse. A lock left by a killed process reports
`None`, because wedging enrollment forever is a worse failure than the one the lock
prevents.

### Readiness is checked before the microphone opens

Consent, participant status, **model availability**, device identity, calibration
freshness and the capture lock are all verified in `start()`, before any stream is
opened. With no embedding model provisioned, `start()` fails with
`MODEL_UNAVAILABLE` and `backend.open_calls` does not change — nobody is asked to
speak for a template that cannot be built.

Consent, participant status, device identity and calibration are then re-checked
**again** immediately before encryption. A five-sample enrollment takes about a
minute, and a person can withdraw consent during it; storing the template afterwards
would mean keeping biometric data they had already refused. On any change the buffer
is dropped, the session moves to a terminal state with an enumerated reason code, and
nothing is stored.

## Consequences

**Good.** One microphone path, with one set of device rules. No voice data in the
browser, in an HTTP body, or on disk. A crash cannot leave a recoverable voice
recording behind. Cancel is immediate.

**Bad / accepted.** `capture_sample` is a synchronous request that holds a connection
for the sample duration; acceptable because a sample is 8–12 seconds and the operator
is watching a wizard step, and an asynchronous handle would add a second lifecycle to
get wrong. The page cannot show a true real-time meter — it renders the level of each
*completed* sample instead, which is enough to tell an operator to move closer or
speak up. And a failed enrollment leaves no audio to inspect afterwards, which is the
intended trade: the diagnosis is the quality gate's reason, not a recording.
