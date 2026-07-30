"""Per-meeting roster capacity.

Phase 3 originally enforced a single hard-coded nine for every meeting. This file
covers the corrective design:

* the participant **directory** has no size limit at all;
* each **meeting** carries its own ``participant_capacity``, stored on the meeting
  row so it survives a restart;
* nine remains the default, so a database written under the old rule behaves
  identically;
* the ceiling is configuration, and it is a guard rail -- not a claim that that many
  speakers can be told apart;
* **capacity never decides whether audio is recorded**, and lowering it never
  removes anybody.

Everything runs against a real migrated database in a temporary data root.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid as uuid_module
from pathlib import Path

import pytest

from mom_igd.config import AppConfig, ParticipantsConfig
from mom_igd.db import initialize_database
from mom_igd.db.connection import connect
from mom_igd.enrollment.participants import (
    BASELINE_MEETING_CAPACITY,
    FALLBACK_DEFAULT_CAPACITY,
    FALLBACK_MAXIMUM_CAPACITY,
    MINIMUM_MEETING_CAPACITY,
    ParticipantError,
    ParticipantService,
)


@pytest.fixture
def db_path(config: AppConfig, paths) -> Path:
    initialize_database(
        paths.database_path(config.database.filename),
        busy_timeout_ms=config.database.busy_timeout_ms,
        app_version=config.app_version,
    )
    return paths.database_path(config.database.filename)


@pytest.fixture
def factory(db_path: Path, config: AppConfig):
    def _connect() -> sqlite3.Connection:
        return connect(db_path, busy_timeout_ms=config.database.busy_timeout_ms)

    return _connect


@pytest.fixture
def people(factory, config: AppConfig) -> ParticipantService:
    return ParticipantService(factory, config=config)


def _meeting(factory, title: str = "Rapat uji") -> str:
    meeting_uuid = str(uuid_module.uuid4())
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO meetings (title, uuid) VALUES (?, ?)", (title, meeting_uuid)
        )
        conn.commit()
    finally:
        conn.close()
    return meeting_uuid


def _fill(people: ParticipantService, meeting_uuid: str, count: int, prefix: str = "P"):
    created = []
    for index in range(count):
        person = people.create(display_name=f"{prefix} {index:03d}")
        people.add_to_meeting(meeting_uuid, person.uuid)
        created.append(person)
    return created


# ==================================================== the directory is uncapped


def test_the_directory_holds_far_more_than_nine_participants(people) -> None:
    """`create` enforces no cap. Nine was only ever a per-meeting number."""
    for index in range(25):
        people.create(display_name=f"Orang {index:03d}")
    assert people.list(limit=200)["total"] == 25


def test_directory_size_is_independent_of_any_roster(people, factory) -> None:
    meeting_uuid = _meeting(factory)
    _fill(people, meeting_uuid, 9)
    for index in range(15):
        people.create(display_name=f"Tidak diundang {index}")
    assert people.list(limit=200)["total"] == 24
    assert people.meeting_participants(meeting_uuid)["active_count"] == 9


def test_removing_from_a_roster_keeps_the_participant_in_the_directory(
    people, factory
) -> None:
    meeting_uuid = _meeting(factory)
    person = people.create(display_name="Budi")
    people.add_to_meeting(meeting_uuid, person.uuid)
    people.remove_from_meeting(meeting_uuid, person.uuid)
    assert people.get(person.uuid).display_name == "Budi"
    assert people.list()["total"] == 1


# ============================================================== default is nine


def test_an_existing_meeting_defaults_to_nine(people, factory) -> None:
    """Migration 0004 backfills 9, so old databases behave exactly as before."""
    meeting_uuid = _meeting(factory)
    summary = people.meeting_participants(meeting_uuid)
    assert summary["capacity"] == 9 == BASELINE_MEETING_CAPACITY
    assert summary["slots_remaining"] == 9


def test_the_configured_default_and_the_fallback_cannot_drift(config) -> None:
    """The module fallbacks exist only for a config-less service.

    If they ever disagree with the shipped configuration, one of two callers is
    silently using a different number, which is exactly the bug this design
    removed.
    """
    assert FALLBACK_DEFAULT_CAPACITY == (
        config.participants.default_meeting_participant_capacity
    )
    assert FALLBACK_MAXIMUM_CAPACITY == (
        config.participants.maximum_meeting_participant_capacity
    )


def test_a_service_without_config_still_has_a_usable_policy(factory) -> None:
    bare = ParticipantService(factory)
    assert bare.default_capacity == FALLBACK_DEFAULT_CAPACITY
    assert bare.maximum_capacity == FALLBACK_MAXIMUM_CAPACITY


def test_capacity_policy_reports_the_bounds_a_ui_needs(people) -> None:
    policy = people.capacity_policy()
    assert policy["minimum_capacity"] == MINIMUM_MEETING_CAPACITY == 1
    assert policy["maximum_capacity"] == 50
    assert policy["default_capacity"] == 9
    assert policy["baseline_capacity"] == 9


# ========================================================== changing a capacity


@pytest.mark.parametrize("wanted", [1, 10, 12, 20, 30, 50])
def test_capacity_can_be_set_to_any_allowed_value(people, factory, wanted) -> None:
    meeting_uuid = _meeting(factory)
    summary = people.set_meeting_capacity(meeting_uuid, wanted)
    assert summary["capacity"] == wanted
    assert people.meeting_participants(meeting_uuid)["capacity"] == wanted


@pytest.mark.parametrize(
    "bad",
    [0, -1, -50, 51, 100, 1000],
)
def test_a_capacity_outside_the_allowed_range_is_refused(people, factory, bad) -> None:
    meeting_uuid = _meeting(factory)
    with pytest.raises(ParticipantError):
        people.set_meeting_capacity(meeting_uuid, bad)
    assert people.meeting_participants(meeting_uuid)["capacity"] == 9, (
        "a refused change must leave the stored capacity untouched"
    )


@pytest.mark.parametrize(
    "bad",
    [True, False, 1.0, 12.5, "12", "", None, [12], {"capacity": 12}],
)
def test_a_non_integer_capacity_is_refused(people, factory, bad) -> None:
    """`True` matters: bool subclasses int, so it would pass a naive check as 1."""
    meeting_uuid = _meeting(factory)
    with pytest.raises(ParticipantError, match="integer"):
        people.set_meeting_capacity(meeting_uuid, bad)


def test_an_unknown_meeting_is_refused(people) -> None:
    with pytest.raises(ParticipantError, match="No meeting"):
        people.set_meeting_capacity(str(uuid_module.uuid4()), 12)


def test_raising_capacity_admits_a_tenth_participant(people, factory) -> None:
    meeting_uuid = _meeting(factory)
    _fill(people, meeting_uuid, 9)
    tenth = people.create(display_name="Orang kesepuluh")
    with pytest.raises(ParticipantError, match="roster capacity"):
        people.add_to_meeting(meeting_uuid, tenth.uuid)

    people.set_meeting_capacity(meeting_uuid, 15)
    summary = people.add_to_meeting(meeting_uuid, tenth.uuid)
    assert summary["active_count"] == 10
    assert summary["capacity"] == 15
    assert summary["slots_remaining"] == 5


def test_adding_beyond_capacity_is_refused_at_the_new_limit(people, factory) -> None:
    meeting_uuid = _meeting(factory)
    people.set_meeting_capacity(meeting_uuid, 11)
    _fill(people, meeting_uuid, 11)
    assert people.meeting_participants(meeting_uuid)["slots_remaining"] == 0
    extra = people.create(display_name="Kelebihan")
    with pytest.raises(ParticipantError, match="roster capacity"):
        people.add_to_meeting(meeting_uuid, extra.uuid)


def test_lowering_below_the_roster_count_is_refused_and_removes_nobody(
    people, factory
) -> None:
    meeting_uuid = _meeting(factory)
    people.set_meeting_capacity(meeting_uuid, 12)
    _fill(people, meeting_uuid, 8)

    with pytest.raises(ParticipantError, match="already on the roster"):
        people.set_meeting_capacity(meeting_uuid, 5)

    summary = people.meeting_participants(meeting_uuid)
    assert summary["capacity"] == 12, "the rejected value must not be stored"
    assert summary["active_count"] == 8, "nobody may be removed to fit a new number"
    assert len(summary["participants"]) == 8


def test_lowering_to_exactly_the_roster_count_is_allowed(people, factory) -> None:
    meeting_uuid = _meeting(factory)
    people.set_meeting_capacity(meeting_uuid, 20)
    _fill(people, meeting_uuid, 6)
    assert people.set_meeting_capacity(meeting_uuid, 6)["slots_remaining"] == 0


def test_setting_the_same_capacity_twice_is_harmless(people, factory) -> None:
    meeting_uuid = _meeting(factory)
    first = people.set_meeting_capacity(meeting_uuid, 14)
    second = people.set_meeting_capacity(meeting_uuid, 14)
    assert first["capacity"] == second["capacity"] == 14


def test_a_capacity_change_is_audited(people, factory) -> None:
    meeting_uuid = _meeting(factory)
    people.set_meeting_capacity(meeting_uuid, 21)
    conn = factory()
    try:
        row = conn.execute(
            "SELECT action, detail_json FROM audit_events "
            "WHERE action = 'MEETING_CAPACITY_CHANGED' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "a capacity change must be recorded"
    detail = json.loads(row["detail_json"])
    assert detail["previous_capacity"] == 9
    assert detail["capacity"] == 21
    assert detail["meeting_uuid"] == meeting_uuid


def test_an_unchanged_capacity_writes_no_audit_event(people, factory) -> None:
    meeting_uuid = _meeting(factory)
    people.set_meeting_capacity(meeting_uuid, 9)  # already 9
    conn = factory()
    try:
        count = conn.execute(
            "SELECT count(*) AS n FROM audit_events "
            "WHERE action = 'MEETING_CAPACITY_CHANGED'"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert count == 0


# ================================================== meetings are independent


def test_two_meetings_hold_independent_capacities_and_rosters(people, factory) -> None:
    a = _meeting(factory, "Rapat A")
    b = _meeting(factory, "Rapat B")
    people.set_meeting_capacity(a, 12)
    people.set_meeting_capacity(b, 30)

    _fill(people, a, 12, prefix="A")
    _fill(people, b, 3, prefix="B")

    summary_a = people.meeting_participants(a)
    summary_b = people.meeting_participants(b)
    assert (summary_a["capacity"], summary_a["active_count"]) == (12, 12)
    assert (summary_b["capacity"], summary_b["active_count"]) == (30, 3)
    assert summary_a["slots_remaining"] == 0
    assert summary_b["slots_remaining"] == 27

    # A full meeting A must not block meeting B.
    extra = people.create(display_name="Tambahan")
    people.add_to_meeting(b, extra.uuid)
    assert people.meeting_participants(b)["active_count"] == 4
    assert people.meeting_participants(a)["active_count"] == 12


def test_a_participant_may_sit_on_two_rosters(people, factory) -> None:
    a = _meeting(factory, "Rapat A")
    b = _meeting(factory, "Rapat B")
    person = people.create(display_name="Budi")
    people.add_to_meeting(a, person.uuid)
    people.add_to_meeting(b, person.uuid)
    assert people.meeting_participants(a)["active_count"] == 1
    assert people.meeting_participants(b)["active_count"] == 1


def test_changing_one_capacity_leaves_the_other_alone(people, factory) -> None:
    a = _meeting(factory, "Rapat A")
    b = _meeting(factory, "Rapat B")
    people.set_meeting_capacity(a, 40)
    assert people.meeting_participants(b)["capacity"] == 9


# ============================================================== persistence


def test_capacity_survives_a_fresh_service_and_connection(
    people, factory, config
) -> None:
    """Stored on the meeting row, so a restart cannot lose it."""
    meeting_uuid = _meeting(factory)
    people.set_meeting_capacity(meeting_uuid, 15)
    reopened = ParticipantService(factory, config=config)
    assert reopened.meeting_participants(meeting_uuid)["capacity"] == 15


def test_capacity_is_not_recomputed_from_configuration(factory, config) -> None:
    """A later change to the configured default must not retune old meetings."""
    people = ParticipantService(factory, config=config)
    meeting_uuid = _meeting(factory)
    people.set_meeting_capacity(meeting_uuid, 25)

    raised = config.model_copy(
        update={
            "participants": ParticipantsConfig(
                default_meeting_participant_capacity=30,
                maximum_meeting_participant_capacity=50,
            )
        }
    )
    later = ParticipantService(factory, config=raised)
    assert later.default_capacity == 30, "the new default applies to new meetings"
    assert later.meeting_participants(meeting_uuid)["capacity"] == 25, (
        "an existing meeting keeps the capacity it was given"
    )


def test_the_stored_capacity_is_read_from_the_meeting_row(people, factory) -> None:
    meeting_uuid = _meeting(factory)
    people.set_meeting_capacity(meeting_uuid, 33)
    conn = factory()
    try:
        stored = conn.execute(
            "SELECT participant_capacity FROM meetings WHERE uuid = ?", (meeting_uuid,)
        ).fetchone()["participant_capacity"]
    finally:
        conn.close()
    assert stored == 33


def test_the_database_refuses_a_non_positive_capacity(factory) -> None:
    """The DB invariant is `>= 1`, and only that -- the 50 ceiling is config.

    Encoding a business ceiling in a CHECK would mean rebuilding this table the
    first time somebody legitimately needs a larger number.
    """
    meeting_uuid = _meeting(factory)
    conn = factory()
    try:
        for bad in (0, -1):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE meetings SET participant_capacity = ? WHERE uuid = ?",
                    (bad, meeting_uuid),
                )
        # Above the configured ceiling is NOT a database error.
        conn.execute(
            "UPDATE meetings SET participant_capacity = 60 WHERE uuid = ?",
            (meeting_uuid,),
        )
        conn.commit()
    finally:
        conn.close()


# ============================================================== concurrency


def test_concurrent_adds_cannot_exceed_a_raised_capacity(people, factory) -> None:
    """The capacity read and the insert share one transaction.

    Capacity 10, nine seats taken, then two threads race for the last one. A
    check-then-insert implementation lets both through.
    """
    meeting_uuid = _meeting(factory)
    people.set_meeting_capacity(meeting_uuid, 10)
    _fill(people, meeting_uuid, 9)

    contenders = [people.create(display_name=f"Perebut {i}") for i in range(2)]
    outcomes: list[str] = []
    barrier = threading.Barrier(len(contenders))

    def _try(candidate) -> None:
        barrier.wait()
        try:
            people.add_to_meeting(meeting_uuid, candidate.uuid)
            outcomes.append("added")
        except Exception as exc:  # noqa: BLE001 - asserted below
            outcomes.append(type(exc).__name__)

    threads = [threading.Thread(target=_try, args=(c,)) for c in contenders]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert outcomes.count("added") == 1, outcomes
    assert people.meeting_participants(meeting_uuid)["active_count"] == 10


def test_a_concurrent_lowering_cannot_strand_the_roster_over_capacity(
    people, factory
) -> None:
    """One thread lowers capacity while another adds. Neither may leave the roster
    above its own capacity."""
    meeting_uuid = _meeting(factory)
    people.set_meeting_capacity(meeting_uuid, 12)
    _fill(people, meeting_uuid, 8)
    joiner = people.create(display_name="Penyusul")

    barrier = threading.Barrier(2)
    errors: list[str] = []

    def _lower() -> None:
        barrier.wait()
        try:
            people.set_meeting_capacity(meeting_uuid, 9)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"lower:{type(exc).__name__}")

    def _add() -> None:
        barrier.wait()
        try:
            people.add_to_meeting(meeting_uuid, joiner.uuid)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"add:{type(exc).__name__}")

    threads = [threading.Thread(target=_lower), threading.Thread(target=_add)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    final = people.meeting_participants(meeting_uuid)
    assert final["active_count"] <= final["capacity"], (
        f"roster {final['active_count']} exceeds capacity {final['capacity']}: {errors}"
    )


# =========================================== capacity never gates the recording


def test_roster_count_does_not_appear_in_the_capture_path() -> None:
    """Recording must never consult a roster.

    The requirement is explicit: audio captures every voice the microphone hears,
    and an unregistered speaker becomes UNKNOWN later -- it never stops a
    recording. The cheapest durable guarantee is that the capture engine does not
    import the participant module at all.
    """
    import ast

    repo = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for path in sorted((repo / "mom_igd" / "audio").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if "participants" in name or "enrollment" in name:
                    offenders.append(f"{path.name}:{node.lineno} imports {name}")
    assert offenders == [], (
        "the capture engine must not depend on participants or enrollment: "
        f"{offenders}"
    )


def test_preflight_and_capture_profile_ignore_roster_size(
    people, factory, config, paths
) -> None:
    """A full roster, an empty roster and an over-capacity roster all preflight
    identically: capture does not care who is registered."""
    from mom_igd.audio.fake_backend import FakeAudioBackend
    from mom_igd.audio.devices import DeviceDiscoveryService
    from mom_igd.audio.service import RecordingService

    backend = FakeAudioBackend()
    recording = RecordingService(
        config,
        paths,
        backend=backend,
        discovery=DeviceDiscoveryService(backend, endpoint_provider=lambda: []),
    )
    device = recording.list_devices()["devices"][0]
    recording.select_device(device["fingerprint"])
    empty = recording.preflight()

    meeting_uuid = _meeting(factory)
    _fill(people, meeting_uuid, 9)
    full = recording.preflight()

    assert empty.can_start == full.can_start
    assert [i.key for i in empty.items] == [i.key for i in full.items], (
        "preflight must reach the same conclusion regardless of roster size"
    )
    assert empty.failures == full.failures
    assert backend.open_calls == 0, "preflight must open no stream either way"


# ================================================================ the roster UI


@pytest.fixture(scope="module")
def ui() -> dict[str, str]:
    from mom_igd.api.app import WEB_DIR

    return {
        name: (Path(WEB_DIR) / name).read_text(encoding="utf-8")
        for name in ("index.html", "app.js", "app.css")
    }


def test_the_roster_card_shows_count_over_capacity(ui) -> None:
    """`12 / 20`, not a bare number: the operator needs both halves."""
    assert 'id="roster-count-pill"' in ui["index.html"]
    assert "count + ' / ' + capacity" in ui["app.js"]


def test_the_capacity_control_exists_and_is_bounded_by_the_backend(ui) -> None:
    html, script = ui["index.html"], ui["app.js"]
    assert 'id="roster-capacity-input"' in html
    assert 'type="number"' in html
    assert 'id="roster-capacity-save-btn"' in html
    # The min/max shown come from the API, and specifically from the *settable*
    # bounds rather than the raw ceiling: a meeting grandfathered above a lowered
    # ceiling has a different upper bound, and showing the ceiling would offer a
    # range the meeting does not have.
    assert "el.rosterCapacity.min = String(lowest)" in script
    assert "el.rosterCapacity.max = String(highest)" in script
    assert "Number(data.capacity_min_settable)" in script
    assert "Number(data.capacity_max_settable)" in script


def test_the_allowed_range_is_displayed(ui) -> None:
    assert 'id="roster-capacity-range"' in ui["index.html"]
    assert "Nilai yang diperbolehkan" in ui["app.js"]


def test_a_capacity_above_the_old_baseline_warns_about_hardware(ui) -> None:
    """The warning is required verbatim in substance: more seats is not more accuracy."""
    # Whitespace-normalised: the copy is concatenated across source lines, and a
    # contiguous-substring assertion breaks the next time somebody reflows it.
    script = ui["app.js"]
    flat = " ".join(script.replace("' +", "").replace("'", " ").split())
    assert "var BASELINE_CAPACITY = 9" in script
    assert "capacity > BASELINE_CAPACITY" in script
    assert "membutuhkan conference microphone dan pengujian ruangan" in flat
    assert "tidak menjamin akurasi pengenalan suara" in flat


def test_the_ceiling_is_never_presented_as_a_validated_capability(ui) -> None:
    script = ui["app.js"]
    assert "pagar keamanan" in script
    assert "sudah terbukti dikenali dengan akurat" in script


def test_the_ui_never_calls_the_roster_unlimited(ui) -> None:
    combined = " ".join(ui.values()).lower()
    for banned in ("unlimited", "tak terbatas", "tanpa batas", "tidak terbatas"):
        assert banned not in combined, f"the UI must never claim {banned!r}"


def test_directory_and_roster_are_presented_as_two_concepts(ui) -> None:
    html = ui["index.html"]
    assert "Direktori peserta" in html
    assert "Roster rapat" in html
    assert "tidak dibatasi" in html, "the directory must be described as uncapped"


def test_the_ui_states_that_recording_ignores_the_roster(ui) -> None:
    html = ui["index.html"]
    roster = html[html.index('id="roster-card"') :]
    roster = roster[: roster.index("</article>")]
    # Whitespace-normalised: the copy wraps across source lines, and asserting on
    # the raw text would break the next time somebody reflows a paragraph.
    flat = " ".join(roster.split())
    assert "bukan suara mana yang direkam" in flat
    assert "UNKNOWN" in flat, "out-of-roster voices must be described as UNKNOWN"


def test_the_roster_ui_does_not_poll_or_request_per_participant(ui) -> None:
    """A 50-person roster must not become 50 requests."""
    script = ui["app.js"]
    from mom_igd.api.app import WEB_DIR  # noqa: F401  (keeps the import local)

    # The listing carries each meeting's own count and capacity, so rendering the
    # selector needs one call, not one per meeting.
    assert "httpGet('/enrollment/meetings', { limit: 200 })" in script
    for forbidden in ("setInterval", "roster-poll"):
        assert forbidden not in script
    # No fetch inside the option-building loop.
    loop_start = script.index("meetings.forEach(function (m) {")
    loop_end = script.index("});", loop_start)
    body = script[loop_start:loop_end]
    for call in ("httpGet", "httpPost", "httpPatch"):
        assert call not in body, f"the render loop calls {call} per meeting"


def test_the_participant_table_is_searchable_and_paginated(ui) -> None:
    assert 'id="participant-search"' in ui["index.html"]
    assert "limit" in ui["app.js"]


def test_the_capacity_field_is_restored_when_the_server_refuses(ui) -> None:
    """A rejected number must not sit in the box looking saved."""
    script = ui["app.js"]
    save = script[script.index("async function saveCapacity()") :]
    save = save[: save.index("\n  /* ---")]
    assert "el.rosterCapacity.value = String(rosterState.capacity)" in save


def test_an_unregistered_voice_cannot_be_expressed_as_a_capture_failure() -> None:
    """There is no reason code, status or error meaning "speaker not registered".

    If one existed, some code path could refuse audio for an unknown voice, which
    the requirement forbids outright.
    """
    from mom_igd.enrollment.service import ReasonCode

    forbidden = ("UNREGISTERED", "NOT_ON_ROSTER", "UNKNOWN_SPEAKER", "ROSTER_FULL")
    present = [code.value for code in ReasonCode if code.value in forbidden]
    assert present == [], (
        f"these reason codes would let a roster decide what gets recorded: {present}"
    )
