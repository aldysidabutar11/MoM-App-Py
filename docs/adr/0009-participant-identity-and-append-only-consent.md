# ADR-0009 — Participant identity by UUID, and append-only biometric consent

* **Status:** Accepted
* **Phase:** 3

## Context

Phase 3 registers the people who attend a meeting and records their permission to
have a voice template built. Two questions had to be settled before any of it could
be written: what identifies a participant, and how is consent stored.

Phase 1 had already answered the first question, and answered it wrongly. It made
`participants.display_name` a `UNIQUE` index.

## Decision

### Identity is a UUID; the display name is a label and is **not** unique

Migration 0003 adds `participants.uuid`, backfills it, indexes it uniquely, and
**drops `ux_participants_display_name`**.

Two people in one organisation genuinely share a name. With a unique index the
second "Budi" cannot be registered at all, and the operator's only escape is to
invent "Budi 2" — corrupting the registry to satisfy an index. Worse, it invites the
name to be treated as a key elsewhere, and a name is exactly the wrong thing to key
on: it changes, it repeats, and it is personal data that would then leak into every
join, log line and filename.

So the name is descriptive only. It is never a primary key, never a directory or
file name, never part of a URL path, and never a voiceprint identifier. The
envelope on disk is `<voiceprint-uuid>.vpx`, and a test asserts a participant's name
does not appear anywhere under the data root.

Reversing a Phase 1 decision is recorded here deliberately rather than done quietly:
a future migration that re-adds a unique index on `display_name` is reversing *this*
decision and must justify it.

### Deactivation, never deletion

`meeting_participants.participant_id` is `ON DELETE RESTRICT`, so a participant who
has appeared in a meeting cannot be deleted. Deleting them would either orphan the
meeting record or cascade it away, and a meeting's attendance list is part of what
makes its minutes trustworthy.

The lifecycle offers `is_active` instead. A deactivated participant stays visible in
history and drops out of every forward-looking path: no new meeting, no new
enrollment, and no eligibility for future speaker identification. Deactivating also
closes their open meeting memberships, so a stale membership cannot silently reserve
one of the nine seats.

### Nine active participants per meeting, enforced in the transaction

SQLite cannot express "at most nine rows per `meeting_id`" as a constraint, so the
count and the insert happen inside one `BEGIN IMMEDIATE`. Checking before the
transaction is a race: two concurrent requests both see eight and both insert,
producing a tenth participant. A UI-only check is not enforcement at all.

What the schema *can* guarantee — and does — is that the same participant is never
linked to one meeting twice.

### Consent is an append-only event log, not a flag

There is no `consents` table with a mutable `granted` boolean. Current state is
derived: the latest `consent_events` row for a participant wins, ordered by
autoincrement id rather than timestamp, because two events in the same millisecond
would tie on time.

A boolean flag would let one `UPDATE` erase the fact that consent was ever given, or
ever withdrawn. For biometric data that history *is* the record: an operator has to
be able to answer "when did she agree, to exactly what wording, and when did she
change her mind?" months later, and a flag cannot answer any part of that.

Each event stores the consent **version** and the **SHA-256 of the exact text**.
Recording "v1" proves nothing if the wording of v1 can drift; the hash is what lets
a later reader tell whether a stored consent refers to the text in front of them.
The hash is computed over UTF-8 with normalised line endings, because this
repository is developed with `core.autocrlf=true` and a hash that changed on
checkout would make every stored consent look superseded after a fresh clone.

Granting requires the caller to echo back the hash of the text it actually
displayed. That is what stops a UI recording consent to wording nobody saw; a
mismatch is refused rather than reconciled, because there is no safe way to guess
which version was on screen.

### A re-grant does not revive a deleted voiceprint

Revocation deletes the encrypted template. A later re-grant is permission to enrol
*again*; it cannot resurrect what was deleted, and pretending otherwise would mean
holding biometric data the person had withdrawn permission for. Enrollment after a
re-grant therefore starts from scratch, and the eligibility policy reports
`NO_USABLE_VOICEPRINT` until it completes.

The consent event id is also bound into the enrollment session. If consent is
revoked and re-granted *during* an enrollment, the id no longer matches and the
session is abandoned: a different consent event is a different agreement, and the
template must be bound to the one that is current.

## Consequences

**Good.** The registry can hold real people with real names. History cannot be
orphaned. The consent record answers audit questions rather than merely asserting a
state. The nine-participant cap cannot be raced past.

**Bad / accepted.** Deriving consent state costs a query rather than a column read —
irrelevant at nine participants. The event log grows monotonically; Phase 11
retention will need an explicit, audited policy for pruning it, which is the right
place for that decision rather than here. A `1.0-draft` consent text is shipped
un-reviewed, and `doctor --production` fails on exactly that until the organisation
records its approval — an honest gap rather than a false claim of compliance.
