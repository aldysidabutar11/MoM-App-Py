# ADR-0011 — Voiceprint storage: crash consistency, and what deletion means

* **Status:** Accepted
* **Phase:** 3

## Context

A voiceprint lives in two places at once: an AES-256-GCM envelope on the filesystem
and a metadata row in SQLite. **These cannot be enrolled in one atomic
transaction**, and this project does not pretend otherwise.

That leaves two problems. What happens when a save is interrupted, and what
"deleted" actually means when consent is withdrawn.

## Decision

### The save protocol is ordered so every interruption is *identifiable*

Not atomic — identifiable. Each step exists because of the crash it survives:

1. build the payload in memory
2. seal it with AES-256-GCM
3. write `<uuid>.vpx.tmp`
4. flush + `fsync` — the bytes are durable before anything references them
5. hash the envelope
6. insert the row as **`PENDING_WRITE`**, carrying the expected path and hash
7. atomic `os.replace` into `<uuid>.vpx`
8. re-read the final file and verify size and hash **from disk**
9. only now mark it `ACTIVE` / `DEVELOPMENT_ONLY`
10. audit event

**Step 6 before step 7 is the load-bearing choice.** It means a crash can leave a
pending row whose file has not yet appeared — recoverable, because the row says what
to look for — but never a finished file that nothing knows about. The
`voiceprints_live_has_envelope` CHECK requires `PENDING_WRITE` to carry its expected
path and hash for exactly this reason: without them, a pending row could not be told
apart from a corrupt one and recovery would have to guess. Guessing about biometric
data is not acceptable.

**Step 8 before step 9** means a row is never marked usable on the strength of what
we *intended* to write, only on what is actually readable.

### What recovery concludes

| Observed | Conclusion |
|---|---|
| temp file, no row | abandoned save → **quarantine** the file |
| pending row, final file valid | rename survived → keep the bytes, mark `RE_ENROLL_REQUIRED` |
| pending row, final missing | crash before rename → `INTEGRITY_FAILED`, clean the temp |
| pending row, hash mismatch | truncated or altered → `INTEGRITY_FAILED` |
| active row, file missing | envelope lost → `INTEGRITY_FAILED` |
| active row, hash mismatch | tampered → `INTEGRITY_FAILED` |
| final file, no row at all | orphan → **quarantine** |

Two details are deliberate.

**A recovered pending row with provably correct bytes becomes
`RE_ENROLL_REQUIRED`, not `ACTIVE`.** The bytes are trustworthy, but the enrollment
that produced them never finished: its quality verdict and production eligibility
were never established, and inventing them would be fabricated evidence.

**Nothing is deleted by recovery.** An ambiguous or unattributable file moves to
`voiceprints/quarantine/` with a written reason, exactly as Phase 2 handles an
ambiguous audio partial. Deleting evidence to tidy a directory is not a trade this
project makes. Recovery is idempotent: a second pass over a healthy store changes
nothing.

**No partial voiceprint is ever eligible.** `VoiceprintStatus.usable` is defined as
an explicit allow-list of two states, so a status added later defaults to *not*
usable — the safe direction for biometric data.

### One live template per participant, and re-enrollment destroys the old one

A partial unique index enforces at most one `ACTIVE`/`DEVELOPMENT_ONLY` row per
participant. Without it a failed re-enrollment could leave two live templates for one
person and Phase 6 would have no defensible way to choose.

Re-enrollment supersedes the previous template **and deletes its envelope**, inside
the activation transaction. A superseded template is biometric data nobody has a
reason to keep: the replacement is what will be used, so retaining the old one widens
the blast radius of a future disclosure for no benefit.

### Deletion on revocation is deliberately destructive, and honest when it fails

Order, which must not be swapped:

1. append the `REVOKED` consent event — **committed first**
2. the participant is ineligible from that instant, whatever happens next
3. delete the envelope, with `secure_delete` on and a WAL checkpoint after
4. clear the pointer and the hash
5. audit, with no biometric payload

If the unlink fails, the row becomes **`DELETE_PENDING`**: still unusable, still
ineligible, and retryable. It is never left `ACTIVE` because a filesystem error
occurred — a permission problem must not translate into continued use of a template
someone withdrew permission for.

The pointer is cleared **only** on a successful delete. Clearing it on failure would
strand the leftover ciphertext: `retry_pending_cleanup()` would have nothing to
unlink, and the file would sit on disk with no record that it exists. (This was a real
bug, caught in review, and there is now a test for it.)

`retry_pending_cleanup()` is idempotent and safe at startup. It opens no microphone,
creates no key, never revives consent, and never touches a meeting recording.

### One fail-closed eligibility policy

`EnrollmentService.eligibility()` is the single answer to "may Phase 6 compare against
this participant?" It requires an active participant, active consent and a usable
template, and returns `False` with reasons for anything it cannot positively confirm.
Phase 6 must call it rather than re-deriving eligibility from raw rows — two
implementations of this question would eventually disagree, and the disagreement would
be a wrong identification.

## Consequences

**Good.** A killed process cannot produce a usable half-written template. Corruption
is detected, localised and provable. Revocation actually removes the ciphertext, and
says so honestly when it cannot. Quarantine preserves evidence.

**Bad / accepted.** Two `fsync`s and a read-back per save — irrelevant at nine
templates. A `DELETE_PENDING` row needs operator attention, surfaced by `doctor` and
the CLI rather than resolved silently. Quarantined files accumulate until someone
looks at them, which is the intended prompt. And the deletion guarantee is bounded by
[ADR-0010](0010-voiceprint-encryption-aes-gcm-under-dpapi.md): on an SSD, and in any
backup taken beforehand, the bytes may physically persist.
