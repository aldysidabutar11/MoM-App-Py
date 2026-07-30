"""Participant registry and meeting membership.

**Identity is the UUID.** ``display_name`` is a label: not a key, not a path
component, not a voiceprint identifier, and deliberately **not unique**. Phase 1
made the name unique; Phase 3 reverses that (migration 0003, ADR-0009) because two
people in one organisation genuinely share a name, and refusing the second one --
or making an operator type "Budi 2" -- corrupts the registry to satisfy an index.

**Deactivation, never deletion.** A participant who has appeared in a meeting is
part of that meeting's history. Deleting the row would either orphan the history
or cascade it away, so the schema forbids it (``ON DELETE RESTRICT``) and the
lifecycle offers deactivation instead. A deactivated participant stays visible in
history, and cannot be added to a new meeting, enrolled, or used by future speaker
identification.

**Data minimisation.** The registry stores what identification needs and nothing
more. ``email`` and ``external_ref`` exist from Phase 1 and remain optional; Phase
3 adds no new personal field. There is no date of birth, no phone number, no
department, no photo -- none of it would make speaker identification work better.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Final

from mom_igd.audit import record_event
from mom_igd.db.connection import maybe_transaction

__all__ = [
    "BASELINE_MEETING_CAPACITY",
    "FALLBACK_DEFAULT_CAPACITY",
    "FALLBACK_MAXIMUM_CAPACITY",
    "MINIMUM_MEETING_CAPACITY",
    "Participant",
    "ParticipantError",
    "ParticipantService",
]

BASELINE_MEETING_CAPACITY: Final[int] = 9
"""The capacity every meeting had before capacity became per-meeting.

Kept as a named constant for exactly two purposes: migration 0004 backfills
existing meetings with it, and the UI warns above it because nine is the only
number the original diarization sizing ever assumed. It is **not** an enforcement
limit -- nothing rejects a roster for exceeding it.
"""

MINIMUM_MEETING_CAPACITY: Final[int] = 1
"""A roster of zero people is not a meeting. Mirrored by the CHECK in 0004."""

FALLBACK_DEFAULT_CAPACITY: Final[int] = 9
FALLBACK_MAXIMUM_CAPACITY: Final[int] = 50
"""Used only when no configuration is supplied.

``ParticipantService`` accepts an optional config so a test can construct it with
nothing but a connection factory. Configuration remains the single source of truth
whenever it is present; these exist so the absence of it is not a crash. They are
deliberately equal to the shipped defaults, so the two cannot silently disagree --
``tests/test_participants_capacity.py`` asserts that.
"""

_MAX_NAME = 120
_MAX_ROLE = 80
_MAX_EMAIL = 254
_MAX_REF = 120
_MAX_NOTES = 1000
_MAX_SEAT = 40


class ParticipantError(RuntimeError):
    """A participant operation was refused. The message names the reason."""


@dataclass(frozen=True, slots=True)
class Participant:
    """One registered person. Carries no biometric data."""

    id: int
    uuid: str
    display_name: str
    role: str | None
    email: str | None
    external_ref: str | None
    is_active: bool
    notes: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Participant:
        return cls(
            id=int(row["id"]),
            uuid=str(row["uuid"] or ""),
            display_name=str(row["display_name"]),
            role=row["role"],
            email=row["email"],
            external_ref=row["external_ref"],
            is_active=bool(int(row["is_active"])),
            notes=row["notes"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form. The integer id is deliberately omitted.

        Clients address a participant by UUID. Exposing the autoincrement id would
        invite callers to store it, and it is an internal detail that says how many
        people have ever been registered.
        """
        return {
            "uuid": self.uuid,
            "display_name": self.display_name,
            "role": self.role,
            "email": self.email,
            "external_ref": self.external_ref,
            "is_active": self.is_active,
            "notes": self.notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _clean(value: str | None, *, limit: int, field: str, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ParticipantError(f"{field} is required.")
        return None
    text = str(value).strip()
    if not text:
        if required:
            raise ParticipantError(
                f"{field} must not be blank. Provide a name the operator will "
                "recognise in the participant list."
            )
        return None
    if len(text) > limit:
        raise ParticipantError(
            f"{field} must be at most {limit} characters, got {len(text)}."
        )
    if any(ord(ch) < 32 for ch in text):
        raise ParticipantError(
            f"{field} must not contain control characters."
        )
    return text


class ParticipantService:
    """Participant lifecycle and meeting membership.

    **Two domains, deliberately separate.** The *directory* is every person ever
    registered and has no size limit -- :meth:`create` enforces no cap at all. A
    *roster* is who is expected in one meeting, and that has a capacity, stored on
    the meeting row so it survives a restart and so changing the configured default
    never retunes a meeting created before the change.
    """

    def __init__(self, connection_factory: Any, *, config: Any = None) -> None:
        self._connect = connection_factory
        self._config = config

    # -- capacity policy ----------------------------------------------------

    @property
    def default_capacity(self) -> int:
        """Capacity a newly created meeting starts with."""
        if self._config is None:
            return FALLBACK_DEFAULT_CAPACITY
        return int(self._config.participants.default_meeting_participant_capacity)

    @property
    def maximum_capacity(self) -> int:
        """Configured safety ceiling.

        A guard rail against a typo, not a validated capability: nothing here
        claims this many speakers can be told apart.
        """
        if self._config is None:
            return FALLBACK_MAXIMUM_CAPACITY
        return int(self._config.participants.maximum_meeting_participant_capacity)

    def capacity_policy(self) -> dict[str, Any]:
        """The numbers a UI needs to render a bounded capacity control."""
        return {
            "minimum_capacity": MINIMUM_MEETING_CAPACITY,
            "maximum_capacity": self.maximum_capacity,
            "default_capacity": self.default_capacity,
            "baseline_capacity": BASELINE_MEETING_CAPACITY,
        }

    def _validate_capacity(self, value: Any) -> int:
        """Accept only a genuine positive integer. **Does not apply the ceiling.**

        ``bool`` is rejected explicitly because it is a subclass of ``int`` in
        Python, so ``True`` would otherwise sail through as a capacity of 1. A
        float is rejected even when it is whole: ``20.0`` arriving here means a
        caller is guessing at the type, and silently truncating is how a 20.7
        becomes a 20 nobody asked for.

        The ceiling is deliberately *not* checked here. Whether a value is
        permissible depends on the meeting -- a meeting stored above a since-lowered
        ceiling may still be reduced -- so that decision lives in
        :meth:`settable_capacity_bounds`, which has the meeting in hand.
        """
        if isinstance(value, bool) or not isinstance(value, int):
            raise ParticipantError(
                f"capacity must be an integer, not {type(value).__name__} "
                f"({value!r}). Acceptable values start at {MINIMUM_MEETING_CAPACITY}."
            )
        if value < MINIMUM_MEETING_CAPACITY:
            raise ParticipantError(
                f"capacity={value} is below the minimum of "
                f"{MINIMUM_MEETING_CAPACITY}. A roster of zero people is not a "
                "meeting."
            )
        return value

    # -- the lowered-ceiling (grandfather) policy ---------------------------
    #
    # A meeting can legitimately hold a capacity above the *current* configured
    # ceiling: it was set while the ceiling was higher, and lowering a configuration
    # value must not reach back and rewrite stored data.
    #
    # The policy, stated once here and used by the service, the API and the UI:
    #
    #   * the stored capacity is **grandfathered** -- never clamped, never silently
    #     adjusted, and no participant is ever removed to make it fit;
    #   * the operator may **lower** it, to any value at or above the current active
    #     roster count;
    #   * the operator may **not raise** it any further while it is above the
    #     ceiling -- so every permitted change moves toward compliance;
    #   * the state is reported explicitly, so a UI never shows a range that implies
    #     a larger value would be accepted.
    #
    # Rejected alternatives: clamping to the ceiling on read (silently loses the
    # operator's setting), refusing every change until they come below the ceiling
    # (leaves no path when the roster itself exceeds the ceiling), and evicting
    # roster members (destroys history to satisfy a setting).

    def _bounds_from(self, stored: int, active: int) -> dict[str, Any]:
        ceiling = self.maximum_capacity
        above = stored > ceiling
        lowest = max(MINIMUM_MEETING_CAPACITY, active)
        highest = stored if above else ceiling
        notice = None
        if above:
            notice = (
                f"This meeting's stored capacity of {stored} is above the currently "
                f"configured safety ceiling of {ceiling}. The stored value is kept as "
                "it is -- nothing is clamped and no participant is removed. It may be "
                f"lowered (to at least {lowest}), but it cannot be raised while it is "
                "above the ceiling."
            )
        return {
            "capacity_above_ceiling": above,
            "capacity_min_settable": lowest,
            "capacity_max_settable": highest,
            # Reachable when the roster itself already exceeds what may be set.
            "capacity_changeable": lowest <= highest,
            "capacity_notice": notice,
        }

    def settable_capacity_bounds(self, meeting_uuid: str) -> dict[str, Any]:
        """What this meeting's capacity may currently be set to.

        One source of truth: the API validates against it, the service enforces it
        and the UI renders it, so none of the three can disagree.
        """
        conn = self._connect()
        try:
            meeting_id = self._meeting_id(conn, meeting_uuid)
            stored = self._stored_capacity(conn, meeting_id)
            active = self._active_count(conn, meeting_id)
        finally:
            conn.close()
        return {
            "meeting_uuid": meeting_uuid,
            "capacity": stored,
            "active_count": active,
            **self._bounds_from(stored, active),
            **self.capacity_policy(),
        }

    # -- reading ------------------------------------------------------------

    def _row_by_uuid(
        self, conn: sqlite3.Connection, participant_uuid: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM participants WHERE uuid = ?", (participant_uuid,)
        ).fetchone()
        if row is None:
            raise ParticipantError(f"No participant with uuid={participant_uuid!r}.")
        return row

    def get(self, participant_uuid: str) -> Participant:
        conn = self._connect()
        try:
            return Participant.from_row(self._row_by_uuid(conn, participant_uuid))
        finally:
            conn.close()

    def list(
        self,
        *,
        search: str | None = None,
        include_inactive: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Paginated registry listing.

        Bounded on purpose. The directory has no size limit -- it holds everyone
        ever registered -- so an unbounded query would be fine on day one and wrong
        for a registry that has accumulated hundreds over years of meetings.
        """
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        clauses: list[str] = []
        params: list[Any] = []
        if not include_inactive:
            clauses.append("is_active = 1")
        if search:
            term = str(search).strip()
            if term:
                if len(term) > _MAX_NAME:
                    raise ParticipantError(
                        f"Search term must be at most {_MAX_NAME} characters."
                    )
                # LIKE with escaped wildcards: a search for "100%" must not become
                # a pattern that matches everything.
                escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                clauses.append(
                    "(display_name LIKE ? ESCAPE '\\' OR role LIKE ? ESCAPE '\\')"
                )
                params += [f"%{escaped}%", f"%{escaped}%"]
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self._connect()
        try:
            total = int(
                conn.execute(
                    f"SELECT count(*) AS n FROM participants{where}", tuple(params)
                ).fetchone()["n"]
            )
            rows = conn.execute(
                f"SELECT * FROM participants{where} "
                "ORDER BY is_active DESC, display_name COLLATE NOCASE, id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return {
                "total": total,
                "limit": limit,
                "offset": offset,
                "participants": [Participant.from_row(r).to_dict() for r in rows],
            }
        finally:
            conn.close()

    # -- writing ------------------------------------------------------------

    def create(
        self,
        *,
        display_name: str,
        role: str | None = None,
        email: str | None = None,
        external_ref: str | None = None,
        notes: str | None = None,
    ) -> Participant:
        """Register a participant. Duplicate display names are allowed."""
        name = _clean(display_name, limit=_MAX_NAME, field="display_name", required=True)
        clean_role = _clean(role, limit=_MAX_ROLE, field="role")
        clean_email = _clean(email, limit=_MAX_EMAIL, field="email")
        clean_ref = _clean(external_ref, limit=_MAX_REF, field="external_ref")
        clean_notes = _clean(notes, limit=_MAX_NOTES, field="notes")
        participant_uuid = str(uuid.uuid4())

        conn = self._connect()
        try:
            with maybe_transaction(conn):
                cursor = conn.execute(
                    "INSERT INTO participants (display_name, role, email, "
                    "external_ref, notes, uuid) VALUES (?,?,?,?,?,?)",
                    (name, clean_role, clean_email, clean_ref, clean_notes, participant_uuid),
                )
                participant_id = int(cursor.lastrowid or 0)
                record_event(
                    conn,
                    category="PARTICIPANT",
                    action="PARTICIPANT_CREATED",
                    entity_type="participant",
                    entity_id=participant_id,
                    detail={"participant_uuid": participant_uuid},
                )
                return Participant.from_row(
                    conn.execute(
                        "SELECT * FROM participants WHERE id = ?", (participant_id,)
                    ).fetchone()
                )
        finally:
            conn.close()

    def update(
        self,
        participant_uuid: str,
        *,
        display_name: str | None = None,
        role: str | None = None,
        email: str | None = None,
        external_ref: str | None = None,
        notes: str | None = None,
    ) -> Participant:
        """Edit descriptive fields. The UUID and the identity never change."""
        conn = self._connect()
        try:
            with maybe_transaction(conn):
                row = self._row_by_uuid(conn, participant_uuid)
                updates: dict[str, Any] = {}
                if display_name is not None:
                    updates["display_name"] = _clean(
                        display_name, limit=_MAX_NAME, field="display_name", required=True
                    )
                for field, value, limit in (
                    ("role", role, _MAX_ROLE),
                    ("email", email, _MAX_EMAIL),
                    ("external_ref", external_ref, _MAX_REF),
                    ("notes", notes, _MAX_NOTES),
                ):
                    if value is not None:
                        updates[field] = _clean(value, limit=limit, field=field)
                if not updates:
                    return Participant.from_row(row)
                assignments = ", ".join(f"{k} = ?" for k in updates)
                conn.execute(
                    f"UPDATE participants SET {assignments}, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                    (*updates.values(), int(row["id"])),
                )
                record_event(
                    conn,
                    category="PARTICIPANT",
                    action="PARTICIPANT_UPDATED",
                    entity_type="participant",
                    entity_id=int(row["id"]),
                    detail={
                        "participant_uuid": participant_uuid,
                        # Field names only. The values are personal data and the
                        # audit trail is not the place to duplicate them.
                        "fields": sorted(updates),
                    },
                )
                return Participant.from_row(
                    conn.execute(
                        "SELECT * FROM participants WHERE id = ?", (int(row["id"]),)
                    ).fetchone()
                )
        finally:
            conn.close()

    def set_active(
        self, participant_uuid: str, *, active: bool, reason: str | None = None
    ) -> Participant:
        """Deactivate or reactivate. **Never deletes.**

        Deactivation is the supported alternative to deletion: history keeps its
        references, while the person drops out of every forward-looking path --
        new meetings, new enrollments, and future speaker identification.
        """
        conn = self._connect()
        try:
            with maybe_transaction(conn):
                row = self._row_by_uuid(conn, participant_uuid)
                participant_id = int(row["id"])
                if bool(int(row["is_active"])) == active:
                    return Participant.from_row(row)
                conn.execute(
                    "UPDATE participants SET is_active = ?, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                    (1 if active else 0, participant_id),
                )
                if not active:
                    # Drop out of every meeting that has not happened yet. History
                    # keeps its rows; the membership simply stops being active.
                    conn.execute(
                        "UPDATE meeting_participants SET is_active = 0, "
                        "removed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                        "WHERE participant_id = ? AND is_active = 1",
                        (participant_id,),
                    )
                record_event(
                    conn,
                    category="PARTICIPANT",
                    action=(
                        "PARTICIPANT_REACTIVATED" if active else "PARTICIPANT_DEACTIVATED"
                    ),
                    entity_type="participant",
                    entity_id=participant_id,
                    detail={
                        "participant_uuid": participant_uuid,
                        "reason": (reason or None) and str(reason)[:300],
                    },
                )
                return Participant.from_row(
                    conn.execute(
                        "SELECT * FROM participants WHERE id = ?", (participant_id,)
                    ).fetchone()
                )
        finally:
            conn.close()

    # -- meeting membership -------------------------------------------------

    def meetings(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Meetings a roster can be attached to, newest first.

        Bounded like the directory listing. Returns the meeting UUID and never the
        integer row id: the id is an internal detail and must not travel to a
        client that could then address a meeting by it.
        """
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        conn = self._connect()
        try:
            total = int(
                conn.execute("SELECT count(*) AS n FROM meetings").fetchone()["n"]
            )
            rows = conn.execute(
                "SELECT m.uuid, m.title, m.participant_capacity, m.created_at, "
                "  (SELECT count(*) FROM meeting_participants mp "
                "    WHERE mp.meeting_id = m.id AND mp.is_active = 1) AS active_count "
                "FROM meetings m ORDER BY m.id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return {
                "total": total,
                "limit": limit,
                "offset": offset,
                "meetings": [
                    {
                        "meeting_uuid": str(r["uuid"]),
                        "title": str(r["title"]),
                        "capacity": int(r["participant_capacity"]),
                        "active_count": int(r["active_count"]),
                        "created_at": str(r["created_at"]),
                    }
                    for r in rows
                ],
                **self.capacity_policy(),
            }
        finally:
            conn.close()

    def set_meeting_capacity(self, meeting_uuid: str, capacity: Any) -> dict[str, Any]:
        """Change one meeting's roster capacity.

        Lowering it below the number of people already on the roster is **refused**,
        not resolved by removing anybody. Silently dropping a participant to satisfy
        a new number would destroy roster history to make a setting fit, and the
        operator would have no idea who vanished.

        The read and the write share one transaction, for the same reason
        :meth:`add_to_meeting` does: a check outside the transaction is a race.
        """
        wanted = self._validate_capacity(capacity)
        conn = self._connect()
        try:
            with maybe_transaction(conn):
                meeting_id = self._meeting_id(conn, meeting_uuid)
                active = self._active_count(conn, meeting_id)
                previous = self._stored_capacity(conn, meeting_id)
                bounds = self._bounds_from(previous, active)

                if wanted < active:
                    raise ParticipantError(
                        f"capacity={wanted} is below the {active} participant(s) "
                        f"already on the roster of meeting {meeting_uuid}. Remove "
                        "someone from the roster first; capacity changes never "
                        "remove a participant."
                    )
                if wanted > bounds["capacity_max_settable"]:
                    if bounds["capacity_above_ceiling"]:
                        raise ParticipantError(
                            f"capacity={wanted} would raise meeting {meeting_uuid} "
                            f"above its stored capacity of {previous}, which is "
                            "already above the configured safety ceiling of "
                            f"{self.maximum_capacity}. The stored value is kept as "
                            "it is, but it may only be lowered from here -- to at "
                            f"least {bounds['capacity_min_settable']}. Raise "
                            "[participants].maximum_meeting_participant_capacity "
                            "first if a larger room is genuinely required."
                        )
                    raise ParticipantError(
                        f"capacity={wanted} exceeds the configured safety ceiling of "
                        f"{self.maximum_capacity}. Raise "
                        "[participants].maximum_meeting_participant_capacity if a "
                        "larger room is genuinely required -- note that a larger "
                        "roster does not improve speaker-recognition accuracy."
                    )
                if previous != wanted:
                    conn.execute(
                        "UPDATE meetings SET participant_capacity = ?, "
                        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                        "WHERE id = ?",
                        (wanted, meeting_id),
                    )
                    record_event(
                        conn,
                        category="MEETING",
                        action="MEETING_CAPACITY_CHANGED",
                        entity_type="meeting",
                        entity_id=meeting_id,
                        detail={
                            "meeting_uuid": meeting_uuid,
                            "previous_capacity": previous,
                            "capacity": wanted,
                            "active_count": active,
                        },
                    )
                return self._membership_summary(conn, meeting_id, meeting_uuid)
        finally:
            conn.close()

    def meeting_participants(self, meeting_uuid: str) -> dict[str, Any]:
        conn = self._connect()
        try:
            meeting_id = self._meeting_id(conn, meeting_uuid)
            rows = conn.execute(
                "SELECT p.*, mp.seat_label, mp.is_active AS membership_active, "
                "mp.added_at, mp.removed_at FROM meeting_participants mp "
                "JOIN participants p ON p.id = mp.participant_id "
                "WHERE mp.meeting_id = ? ORDER BY mp.is_active DESC, mp.id",
                (meeting_id,),
            ).fetchall()
            members = []
            for row in rows:
                entry = Participant.from_row(row).to_dict()
                entry["seat_label"] = row["seat_label"]
                entry["membership_active"] = bool(int(row["membership_active"]))
                entry["added_at"] = str(row["added_at"])
                entry["removed_at"] = row["removed_at"]
                members.append(entry)
            summary = self._membership_summary(conn, meeting_id, meeting_uuid)
            summary["participants"] = members
            return summary
        finally:
            conn.close()

    def add_to_meeting(
        self, meeting_uuid: str, participant_uuid: str, *, seat_label: str | None = None
    ) -> dict[str, Any]:
        """Link a participant to a meeting, enforcing **that meeting's** capacity.

        The capacity read, the membership count and the insert happen in **one**
        transaction. Checking first and inserting afterwards would let two
        concurrent requests both see one free slot and both take it -- the exact
        race a transactional check exists to prevent. ``BEGIN IMMEDIATE`` (via
        ``maybe_transaction``) takes the write lock before the count, so the second
        transaction reads the first one's committed state.
        """
        seat = _clean(seat_label, limit=_MAX_SEAT, field="seat_label")
        conn = self._connect()
        try:
            with maybe_transaction(conn):
                meeting_id = self._meeting_id(conn, meeting_uuid)
                row = self._row_by_uuid(conn, participant_uuid)
                participant_id = int(row["id"])
                if not int(row["is_active"]):
                    raise ParticipantError(
                        f"Participant {participant_uuid} is deactivated and cannot be "
                        "added to a meeting. Reactivate them first."
                    )

                existing = conn.execute(
                    "SELECT id, is_active FROM meeting_participants "
                    "WHERE meeting_id = ? AND participant_id = ?",
                    (meeting_id, participant_id),
                ).fetchone()
                if existing is not None and int(existing["is_active"]):
                    # Idempotent: a double-clicked Add is not an error.
                    return self._membership_summary(conn, meeting_id, meeting_uuid)

                active = self._active_count(conn, meeting_id)
                capacity = self._stored_capacity(conn, meeting_id)
                if active >= capacity:
                    raise ParticipantError(
                        f"Meeting {meeting_uuid} already has {active} active "
                        f"participant(s), which is its roster capacity of "
                        f"{capacity}. Raise this meeting's capacity (up to "
                        f"{self.maximum_capacity}) or remove someone before adding "
                        "another. Note that roster capacity does not affect "
                        "recording: audio always captures the whole room."
                    )

                if existing is None:
                    conn.execute(
                        "INSERT INTO meeting_participants "
                        "(meeting_id, participant_id, seat_label) VALUES (?,?,?)",
                        (meeting_id, participant_id, seat),
                    )
                else:
                    # Re-adding someone previously removed reuses the row, so the
                    # unique (meeting, participant) index stays satisfied.
                    conn.execute(
                        "UPDATE meeting_participants SET is_active = 1, removed_at = NULL,"
                        " seat_label = ?, added_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                        " WHERE id = ?",
                        (seat, int(existing["id"])),
                    )
                record_event(
                    conn,
                    category="MEETING",
                    action="MEETING_PARTICIPANT_ADDED",
                    entity_type="meeting",
                    entity_id=meeting_id,
                    detail={
                        "meeting_uuid": meeting_uuid,
                        "participant_uuid": participant_uuid,
                    },
                )
                return self._membership_summary(conn, meeting_id, meeting_uuid)
        finally:
            conn.close()

    def remove_from_meeting(
        self, meeting_uuid: str, participant_uuid: str
    ) -> dict[str, Any]:
        """Deactivate a membership. The row survives as history."""
        conn = self._connect()
        try:
            with maybe_transaction(conn):
                meeting_id = self._meeting_id(conn, meeting_uuid)
                participant_id = int(self._row_by_uuid(conn, participant_uuid)["id"])
                row = conn.execute(
                    "SELECT id, is_active FROM meeting_participants "
                    "WHERE meeting_id = ? AND participant_id = ?",
                    (meeting_id, participant_id),
                ).fetchone()
                if row is None:
                    raise ParticipantError(
                        f"Participant {participant_uuid} is not linked to meeting "
                        f"{meeting_uuid}."
                    )
                if int(row["is_active"]):
                    conn.execute(
                        "UPDATE meeting_participants SET is_active = 0, "
                        "removed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                        (int(row["id"]),),
                    )
                    record_event(
                        conn,
                        category="MEETING",
                        action="MEETING_PARTICIPANT_REMOVED",
                        entity_type="meeting",
                        entity_id=meeting_id,
                        detail={
                            "meeting_uuid": meeting_uuid,
                            "participant_uuid": participant_uuid,
                        },
                    )
                return self._membership_summary(conn, meeting_id, meeting_uuid)
        finally:
            conn.close()

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _meeting_id(conn: sqlite3.Connection, meeting_uuid: str) -> int:
        row = conn.execute(
            "SELECT id FROM meetings WHERE uuid = ?", (meeting_uuid,)
        ).fetchone()
        if row is None:
            raise ParticipantError(f"No meeting with uuid={meeting_uuid!r}.")
        return int(row["id"])

    @staticmethod
    def _active_count(conn: sqlite3.Connection, meeting_id: int) -> int:
        return int(
            conn.execute(
                "SELECT count(*) AS n FROM meeting_participants "
                "WHERE meeting_id = ? AND is_active = 1",
                (meeting_id,),
            ).fetchone()["n"]
        )

    @staticmethod
    def _stored_capacity(conn: sqlite3.Connection, meeting_id: int) -> int:
        """This meeting's capacity, from the meeting row.

        Never the configured default: that is only the value a *new* meeting
        starts with. Reading configuration here would retune every historical
        meeting the moment somebody edited a TOML file.
        """
        return int(
            conn.execute(
                "SELECT participant_capacity FROM meetings WHERE id = ?",
                (meeting_id,),
            ).fetchone()["participant_capacity"]
        )

    def _membership_summary(
        self, conn: sqlite3.Connection, meeting_id: int, meeting_uuid: str
    ) -> dict[str, Any]:
        active = self._active_count(conn, meeting_id)
        capacity = self._stored_capacity(conn, meeting_id)
        row = conn.execute(
            "SELECT title FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        return {
            "meeting_uuid": meeting_uuid,
            "meeting_title": str(row["title"]),
            "active_count": active,
            "capacity": capacity,
            # Clamped at zero: a roster that already exceeds a lowered capacity has
            # no negative number of free seats, it has none.
            "slots_remaining": max(0, capacity - active),
            "over_capacity": active > capacity,
            **self._bounds_from(capacity, active),
            **self.capacity_policy(),
        }
