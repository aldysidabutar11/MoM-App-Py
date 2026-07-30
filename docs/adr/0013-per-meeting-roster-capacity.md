# ADR-0013 — Per-meeting roster capacity, with a configurable safety ceiling

* **Status:** Accepted
* **Phase:** 3 (corrective)
* **Supersedes:** the fixed nine-participant cap introduced in ADR-0009's
  implementation (`MAX_ACTIVE_PARTICIPANTS_PER_MEETING`)

## Context

Phase 3 shipped a single module constant:

```python
MAX_ACTIVE_PARTICIPANTS_PER_MEETING: Final[int] = 9
```

It was enforced for every meeting, in the insert transaction, and its docstring
called it "a hard cap from the product requirements".

Nine came from somewhere real: it is the room size the diarization and voice-id
design was sized for, and the number the Phase 0 acceptance criteria used. But it
was written into the code as though it were a property of the product rather than
the default the first deployment happens to need. A different room seats a different
number of people, and there is nothing about a tenth participant that the
architecture cannot represent — the constant simply refused them.

Two further problems were latent in that design:

1. **It conflated two different things.** "How many people are registered in this
   organisation" and "how many people are expected at this meeting" are unrelated
   questions. The cap sat on meeting membership, but every message, counter and
   document described it as a limit on participants, which invited the reading that
   the tenth employee could not be registered at all.
2. **It invited the wrong inference about recording.** If the product has a
   nine-person maximum, an operator reasonably concludes that a tenth voice is
   somehow not captured. That was never true and must never become true.

## Decision

### 1. The participant directory has no size limit

`ParticipantService.create()` enforces no cap and never did. That is now explicit,
documented, and covered by a test that registers 25 people. The directory holds
everyone ever registered; removing somebody from a roster does not remove them from
it, and deactivation is still the only lifecycle operation that hides anyone.

### 2. Roster capacity is per meeting, and it is stored

Migration 0004 adds `meetings.participant_capacity INTEGER NOT NULL DEFAULT 9
CHECK (participant_capacity >= 1)`.

Stored, not derived, and specifically **not** read from configuration at use time.
An operator who sets a room to 20 must find 20 after restarting, and a later change
to the configured default must not silently retune every meeting recorded before it.
`_stored_capacity()` reads the meeting row; `default_capacity` is only consulted when
a *new* meeting is created.

`DEFAULT 9` backfills every existing meeting, so a database recorded under the old
fixed cap behaves exactly as it did before the upgrade.

### 3. The ceiling is configuration, not schema

```toml
[participants]
default_meeting_participant_capacity = 9
maximum_meeting_participant_capacity = 50
```

The `CHECK` constraint is `>= 1` and nothing more. Encoding `<= 50` in the schema
would mean rebuilding `meetings` — with its foreign keys, indexes and cascades — the
first time somebody legitimately needs 60. What belongs in the database is the
invariant that can never become false: a roster cannot hold a negative or zero
number of people. What belongs in configuration is the business guard rail.

`config_schema_version` moves 2 → 3. Every key in the new section has a default, so
a `local.toml` that omits the section — or omits the version key — keeps working and
nothing an operator configured is reset.

### 4. Lowering capacity below the roster is refused, never resolved

A capacity change never removes a participant. Silently dropping somebody to make a
new number fit would destroy roster history to satisfy a setting, and the operator
would have no way to know who vanished. The API answers `409`.

### 5. The HTTP status split is by *kind* of problem

| Situation | Status |
|---|---|
| Not an integer, below 1, or above the ceiling | `422` — the value is unacceptable regardless of the meeting |
| Below the number already on the roster | `409` — the value conflicts with this meeting's state |
| No such meeting | `404` |

The body type is `StrictInt`, not `int`. FastAPI's default coercion accepts `true` as
`1` and `"12"` as `12`, so a plain `int` would take a boolean or a quoted number as a
roster size. That was found by a test, not by review.

### 6. A ceiling lowered below a stored capacity grandfathers the meeting

A meeting can hold a capacity above the *current* configured ceiling: it was set
while the ceiling was higher. Lowering a configuration value must not reach back and
rewrite stored data, so:

* the stored capacity is **kept** — never clamped on read, never silently adjusted,
  and **no participant is ever removed** to make it fit;
* the operator may **lower** it, to any value at or above the current active roster
  count;
* the operator may **not raise** it further while it is above the ceiling, so every
  permitted change moves toward compliance;
* the state is reported explicitly (`capacity_above_ceiling`, `capacity_notice`,
  `capacity_min_settable`, `capacity_max_settable`) so the API and the UI never show
  a range that implies a larger value would be accepted.

`ParticipantService.settable_capacity_bounds()` is the single source of truth. The
API validates against it, the service enforces it inside its transaction, and the UI
renders it — so the three cannot disagree, and the `422` message states the range the
*meeting* actually has rather than the raw ceiling.

Rejected: clamping on read (silently discards the operator's setting), refusing every
change until the stored value is under the ceiling (leaves no path when the roster
itself exceeds the ceiling), and evicting roster members (destroys history to satisfy
a setting).

### 7. `doctor` measures attendees, not seats — and checks *whose* voice is enrolled

The production-readiness question is per roster and per person. Two earlier versions
of the check got it wrong:

* the first demanded a hard-coded nine;
* the second used the largest configured roster **capacity**, so a meeting with
  capacity 15 and ten people on its roster was reported as needing fifteen templates —
  inventing five attendees who do not exist.

Both were also *global counts*: fifteen voiceprints belonging to people who are not
on the roster would have satisfied them while every actual attendee stayed
unrecognised.

Coverage is now computed per roster by joining each active member to **that same
participant's** own live voiceprint. A member counts as covered only when the
participant is active, the membership is active, their latest consent event is a
grant, and they own a voiceprint that is `ACTIVE` and `production_eligible`.

Two limits are stated rather than papered over. The schema has no signal that
distinguishes an upcoming meeting from a historical one — `meetings` has no state
column by design (migration 0001), and adding one to make a diagnostic prettier would
be the wrong reason to change the schema — so the check reports **every** roster and
names the worst one instead of guessing which meeting matters. And no display name or
meeting title appears in the output: a diagnostic gets pasted into tickets, and a
UUID is enough to act on.

There is deliberately **no** module-level "minimum voiceprints" constant any more. A
fallback number would only be reachable when there is no roster at all, and in that
case the honest answer is that nothing is required yet.

### 8. Capacity never decides what is recorded

This is the invariant that matters most, and it is enforced structurally: nothing
under `mom_igd/audio/` imports `mom_igd.enrollment` or the participant module, and a
test asserts that. Capture takes the whole room signal. Preflight reaches the same
verdict with an empty roster and a full one.

A voice with no voiceprint, or one belonging to nobody on the roster, becomes
`UNKNOWN` from Phase 6 onwards — labelled, not discarded. There is deliberately no
reason code meaning "speaker not registered", because its existence would let some
future code path refuse audio for an unknown voice.

## Consequences

* Nine is now a **default**, kept for backward compatibility, and described that way
  everywhere. It is no longer a claim about the product.
* Raising a roster's capacity does not improve accuracy, and the UI says so above
  the old baseline: *"Kapasitas lebih besar membutuhkan conference microphone dan
  pengujian ruangan. Menambah roster tidak menjamin akurasi pengenalan suara."*
* The 50 ceiling is **not** a validated capability. Nine has never been validated in
  a real room either. Real speaker recognition still needs an approved embedding
  model, a USB conference microphone, calibration evidence, consent review and room
  acceptance — all still outstanding.
* The application never describes any roster as "unlimited". A test asserts that the
  UI contains no such word in any language it ships.
* `doctor` no longer reports a fabricated voiceprint requirement. On an empty
  database it says there is nothing to enrol, which changed `doctor --production`
  from five FAILs to four on a fresh install. The gate is not weakened: the
  microphone, calibration, consent-text and model checks still fail, so the exit code
  is still 1 — the fifth failure was simply not real.
* Two defects were found while writing the tests for this, both invisible to a test
  of `ParticipantsConfig` in isolation: the CLI built its `ParticipantService`
  without passing the configuration at all (so every CLI command silently used the
  built-in 9/50 while the GUI honoured the operator's file), and new meetings created
  by the recording path took the SQL column DEFAULT of 9 rather than the configured
  default. Both are now asserted through the real application and recording paths.

## Alternatives rejected

**Keep the constant and raise it to 50.** Trades one arbitrary global number for a
larger arbitrary global number, and still forces a two-person meeting to advertise
fifty seats.

**A separate `meeting_settings` table.** One scalar fact with a 1:1 lifetime would
need its own insert on every meeting creation and a left join on every read, and it
would allow the two rows to disagree.

**Derive capacity from configuration at read time.** Simplest to write, and wrong:
editing one TOML value would retune every historical meeting, including ones already
recorded and processed.

**Let a lowered capacity evict the newest roster members.** Convenient, and
destructive. The operator did not ask to remove anybody.
