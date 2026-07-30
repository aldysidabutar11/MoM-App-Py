"""Managing who is on a meeting's roster, from the API and from the GUI.

The endpoints existed since Phase 3; the **UI did not use them**. The roster card
could pick a meeting, show a counter and change the capacity, but there was no way to
put anybody on the roster or take them off, and no member list at all. This file
covers the completed loop.

Nothing here creates a duplicate endpoint: add is `POST
/enrollment/meetings/{uuid}/participants`, remove is the matching `DELETE`, and both
were already on the shell allowlist as anchored UUID templates.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import uuid as uuid_module
from pathlib import Path
from typing import Any, Iterator

import pytest

from mom_igd.api.app import WEB_DIR
from mom_igd.config import AppConfig
from mom_igd.db import initialize_database
from mom_igd.db.connection import connect
from mom_igd.enrollment.participants import ParticipantService
from mom_igd.security import SESSION_TOKEN_HEADER, SessionToken


@pytest.fixture
def audio_config(config: AppConfig) -> AppConfig:
    payload = config.model_dump()
    payload["audio"] = {
        **config.audio.model_dump(),
        "min_free_disk_gb": 0.0,
        "low_disk_abort_gb": 0.0,
    }
    return AppConfig.model_validate(payload)


@pytest.fixture
def migrated(audio_config: AppConfig, paths) -> Path:
    database = paths.database_path(audio_config.database.filename)
    initialize_database(
        database,
        busy_timeout_ms=audio_config.database.busy_timeout_ms,
        app_version=audio_config.app_version,
    )
    return database


@pytest.fixture
def factory(migrated: Path, audio_config: AppConfig):
    def _connect() -> sqlite3.Connection:
        return connect(migrated, busy_timeout_ms=audio_config.database.busy_timeout_ms)

    return _connect


@pytest.fixture
def people(factory, audio_config: AppConfig) -> ParticipantService:
    return ParticipantService(factory, config=audio_config)


@pytest.fixture
def app_and_backend(audio_config: AppConfig, paths, token: SessionToken, migrated):
    from mom_igd.api.app import create_app
    from mom_igd.audio.devices import DeviceDiscoveryService
    from mom_igd.audio.fake_backend import FakeAudioBackend, SineSource
    from mom_igd.audio.service import RecordingService

    backend = FakeAudioBackend(blocksize=1_200, source=SineSource(level_dbfs=-20.0))
    app = create_app(audio_config, session_token=token, paths=paths)
    app.state.recording_service = RecordingService(
        audio_config,
        paths,
        backend=backend,
        discovery=DeviceDiscoveryService(backend, endpoint_provider=lambda: []),
    )
    yield app, backend
    context = getattr(app.state, "enrollment_context", None)
    if context is not None:
        for shutdown in (context.capture.shutdown, context.enrollment.shutdown):
            try:
                shutdown()
            except Exception:  # noqa: BLE001
                pass
    try:
        app.state.recording_service.abandon("test teardown")
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def client(app_and_backend, token: SessionToken) -> Iterator[Any]:
    from starlette.testclient import TestClient

    app, _backend = app_and_backend
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        test_client.headers.update({SESSION_TOKEN_HEADER: token.value})
        yield test_client


@pytest.fixture
def anon(app_and_backend) -> Iterator[Any]:
    from starlette.testclient import TestClient

    app, _backend = app_and_backend
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


@pytest.fixture(scope="module")
def ui() -> dict[str, str]:
    return {
        name: (Path(WEB_DIR) / name).read_text(encoding="utf-8")
        for name in ("index.html", "app.js", "app.css")
    }


def _meeting(factory, title: str = "Rapat") -> str:
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


def _participant(client, name: str = "Budi", role: str | None = "Anggota") -> str:
    body: dict[str, Any] = {"display_name": name}
    if role:
        body["role"] = role
    response = client.post("/enrollment/participants", json=body)
    assert response.status_code == 201, response.text
    return response.json()["participant"]["uuid"]


def _add(client, meeting: str, participant: str):
    return client.post(
        f"/enrollment/meetings/{meeting}/participants",
        json={"participant_uuid": participant},
    )


def _remove(client, meeting: str, participant: str):
    return client.delete(f"/enrollment/meetings/{meeting}/participants/{participant}")


def _roster(client, meeting: str) -> dict[str, Any]:
    response = client.get(f"/enrollment/meetings/{meeting}/roster")
    assert response.status_code == 200, response.text
    return response.json()


# ===========================================================================
# API: the add / remove loop
# ===========================================================================


def test_the_counter_walks_from_nine_to_ten_and_back(client, factory) -> None:
    """The exact sequence in the manual acceptance script."""
    meeting = _meeting(factory)
    for index in range(9):
        _add(client, meeting, _participant(client, f"Orang {index:02d}"))
    assert _roster(client, meeting)["active_count"] == 9

    tenth = _participant(client, "Kesepuluh")
    assert _add(client, meeting, tenth).status_code == 409, "capacity 9 is still in force"

    assert (
        client.patch(
            f"/enrollment/meetings/{meeting}/capacity", json={"capacity": 15}
        ).status_code
        == 200
    )
    added = _add(client, meeting, tenth)
    assert added.status_code == 200, added.text
    assert (added.json()["active_count"], added.json()["capacity"]) == (10, 15)

    removed = _remove(client, meeting, tenth)
    assert removed.status_code == 200
    assert (removed.json()["active_count"], removed.json()["capacity"]) == (9, 15)


def test_a_removed_participant_can_be_re_added(client, factory) -> None:
    meeting = _meeting(factory)
    participant = _participant(client)
    _add(client, meeting, participant)
    _remove(client, meeting, participant)
    again = _add(client, meeting, participant)
    assert again.status_code == 200, again.text
    assert again.json()["active_count"] == 1
    # The unique (meeting, participant) index must not be violated by the re-add.
    assert len(_roster(client, meeting)["participants"]) == 1


def test_adding_twice_is_idempotent(client, factory) -> None:
    """A double-clicked Add must not create a second membership."""
    meeting = _meeting(factory)
    participant = _participant(client)
    first = _add(client, meeting, participant)
    second = _add(client, meeting, participant)
    assert first.status_code == second.status_code == 200
    assert second.json()["active_count"] == 1


def test_removing_twice_is_safe(client, factory) -> None:
    meeting = _meeting(factory)
    participant = _participant(client)
    _add(client, meeting, participant)
    assert _remove(client, meeting, participant).status_code == 200
    again = _remove(client, meeting, participant)
    assert again.status_code == 200, "a repeated remove must not become a server error"
    assert again.json()["active_count"] == 0


def test_adding_beyond_capacity_is_refused_over_the_api(client, factory) -> None:
    meeting = _meeting(factory)
    client.patch(f"/enrollment/meetings/{meeting}/capacity", json={"capacity": 3})
    for index in range(3):
        assert _add(client, meeting, _participant(client, f"Orang {index}")).status_code == 200
    refused = _add(client, meeting, _participant(client, "Kelebihan"))
    assert refused.status_code == 409, refused.text
    assert "roster capacity" in refused.text
    assert _roster(client, meeting)["active_count"] == 3


def test_an_inactive_participant_cannot_be_added_and_the_reason_is_clear(
    client, factory
) -> None:
    meeting = _meeting(factory)
    participant = _participant(client, "Nonaktif")
    assert client.post(
        f"/enrollment/participants/{participant}/deactivate"
    ).status_code == 200
    refused = _add(client, meeting, participant)
    assert refused.status_code == 409, refused.text
    assert "deactivated" in refused.text.lower()
    assert "reactivate" in refused.text.lower()


def test_removing_from_a_roster_keeps_the_participant_in_the_directory(
    client, factory
) -> None:
    meeting = _meeting(factory)
    participant = _participant(client, "Tetap ada")
    _add(client, meeting, participant)
    _remove(client, meeting, participant)

    assert client.get(f"/enrollment/participants/{participant}").status_code == 200
    listing = client.get("/enrollment/participants").json()
    assert any(p["uuid"] == participant for p in listing["participants"])


def test_lowering_capacity_below_the_roster_is_still_refused(client, factory) -> None:
    meeting = _meeting(factory)
    client.patch(f"/enrollment/meetings/{meeting}/capacity", json={"capacity": 12})
    for index in range(7):
        _add(client, meeting, _participant(client, f"Orang {index}"))
    refused = client.patch(
        f"/enrollment/meetings/{meeting}/capacity", json={"capacity": 4}
    )
    assert refused.status_code == 409
    roster = _roster(client, meeting)
    assert roster["capacity"] == 12
    assert roster["active_count"] == 7


def test_the_roster_rows_carry_consent_and_voiceprint_state(client, factory) -> None:
    """One request per screen. A badge per row must not mean a request per row."""
    from mom_igd.enrollment.consent import CONSENT_TEXT_SHA256

    meeting = _meeting(factory)
    participant = _participant(client, "Budi", role="Ketua")
    _add(client, meeting, participant)
    client.post(
        f"/enrollment/participants/{participant}/consent/grant",
        json={"acknowledged_text_sha256": CONSENT_TEXT_SHA256},
    )

    member = _roster(client, meeting)["participants"][0]
    assert member["display_name"] == "Budi"
    assert member["role"] == "Ketua"
    assert member["membership_active"] is True
    assert member["consent"]["active"] is True
    assert "voiceprint" in member


def test_two_meetings_keep_independent_rosters(client, factory) -> None:
    first, second = _meeting(factory, "Rapat A"), _meeting(factory, "Rapat B")
    shared = _participant(client, "Hadir di dua rapat")
    only_a = _participant(client, "Hanya A")
    _add(client, first, shared)
    _add(client, first, only_a)
    _add(client, second, shared)

    assert _roster(client, first)["active_count"] == 2
    assert _roster(client, second)["active_count"] == 1
    _remove(client, first, shared)
    assert _roster(client, first)["active_count"] == 1
    assert _roster(client, second)["active_count"] == 1, (
        "removing from one roster must not touch the other"
    )


def test_concurrent_adds_cannot_exceed_capacity_over_the_api(client, factory) -> None:
    meeting = _meeting(factory)
    client.patch(f"/enrollment/meetings/{meeting}/capacity", json={"capacity": 10})
    for index in range(9):
        _add(client, meeting, _participant(client, f"Orang {index}"))

    contenders = [_participant(client, f"Perebut {i}") for i in range(3)]
    codes: list[int] = []
    barrier = threading.Barrier(len(contenders))

    def _race(participant: str) -> None:
        barrier.wait()
        codes.append(_add(client, meeting, participant).status_code)

    threads = [threading.Thread(target=_race, args=(p,)) for p in contenders]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert codes.count(200) == 1, codes
    assert _roster(client, meeting)["active_count"] == 10


def test_the_roster_survives_a_restart(
    audio_config: AppConfig, paths, token: SessionToken, migrated, factory
) -> None:
    from starlette.testclient import TestClient

    from mom_igd.api.app import create_app
    from mom_igd.audio.devices import DeviceDiscoveryService
    from mom_igd.audio.fake_backend import FakeAudioBackend
    from mom_igd.audio.service import RecordingService

    meeting = _meeting(factory)
    people = ParticipantService(factory, config=audio_config)
    people.set_meeting_capacity(meeting, 15)
    for index in range(10):
        person = people.create(display_name=f"Orang {index:02d}")
        people.add_to_meeting(meeting, person.uuid)

    backend = FakeAudioBackend()
    app = create_app(audio_config, session_token=token, paths=paths)
    app.state.recording_service = RecordingService(
        audio_config,
        paths,
        backend=backend,
        discovery=DeviceDiscoveryService(backend, endpoint_provider=lambda: []),
    )
    try:
        with TestClient(app, base_url="http://127.0.0.1") as fresh:
            fresh.headers.update({SESSION_TOKEN_HEADER: token.value})
            roster = _roster(fresh, meeting)
            assert roster["capacity"] == 15
            assert roster["active_count"] == 10
            assert len(roster["participants"]) == 10
    finally:
        try:
            app.state.recording_service.abandon("teardown")
        except Exception:  # noqa: BLE001
            pass


def test_a_directory_larger_than_fifty_stays_searchable(client, factory) -> None:
    """The ceiling bounds a roster, never the directory."""
    for index in range(60):
        _participant(client, f"Orang {index:03d}", role=None)
    listing = client.get("/enrollment/participants", params={"limit": 25}).json()
    assert listing["total"] == 60
    assert len(listing["participants"]) == 25, "the page must stay bounded"

    found = client.get(
        "/enrollment/participants", params={"search": "Orang 057", "limit": 25}
    ).json()
    assert found["total"] == 1
    assert found["participants"][0]["display_name"] == "Orang 057"


def test_no_internal_row_id_reaches_a_membership_response(client, factory) -> None:
    meeting = _meeting(factory)
    participant = _participant(client)
    for body in (
        _add(client, meeting, participant).text,
        client.get(f"/enrollment/meetings/{meeting}/roster").text,
        _remove(client, meeting, participant).text,
    ):
        assert '"id"' not in body
        assert '"meeting_id"' not in body
        assert '"participant_id"' not in body
        assert not re.search(r'"[a-z_]*(?<!uu)id"\s*:\s*\d', body), body[:200]


def test_the_membership_routes_require_the_session_token(anon, factory) -> None:
    meeting = _meeting(factory)
    stranger = str(uuid_module.uuid4())
    assert anon.get(f"/enrollment/meetings/{meeting}/roster").status_code == 401
    assert anon.post(
        f"/enrollment/meetings/{meeting}/participants",
        json={"participant_uuid": stranger},
    ).status_code == 401
    assert anon.delete(
        f"/enrollment/meetings/{meeting}/participants/{stranger}"
    ).status_code == 401


def test_the_membership_paths_are_reachable_only_through_the_exact_allowlist() -> None:
    from mom_igd.shell.launcher import (
        ALLOWED_DELETE_PATTERNS,
        ALLOWED_GET_PATTERNS,
        ALLOWED_POST_PATHS,
        ALLOWED_POST_PATTERNS,
        ALLOWED_PROXY_PATHS,
        _permitted,
    )

    good_meeting = "0189d3f1-1c2e-4a5b-8c7d-9e0f1a2b3c4d"
    good_person = "0189d3f1-1c2e-4a5b-8c7d-9e0f1a2b3c4e"
    assert _permitted(
        f"/enrollment/meetings/{good_meeting}/participants",
        ALLOWED_POST_PATHS,
        ALLOWED_POST_PATTERNS,
    )
    assert _permitted(
        f"/enrollment/meetings/{good_meeting}/participants/{good_person}",
        frozenset(),
        ALLOWED_DELETE_PATTERNS,
    )
    for bad in (
        f"/enrollment/meetings/{good_meeting}/participants/",
        f"/enrollment/meetings/{good_meeting}/participants/x",
        f"/enrollment/meetings/{good_meeting.upper()}/participants",
        f"/enrollment/meetings/{good_meeting}/participants?token=abc",
        "/enrollment/meetings/../participants",
    ):
        assert not _permitted(bad, ALLOWED_POST_PATHS, ALLOWED_POST_PATTERNS), bad
        assert not _permitted(bad, frozenset(), ALLOWED_DELETE_PATTERNS), bad
        assert not _permitted(bad, ALLOWED_PROXY_PATHS, ALLOWED_GET_PATTERNS), bad


# ===========================================================================
# GUI: the controls exist, are wired, and render safely
# ===========================================================================


def _js_function(script: str, name: str) -> str:
    pattern = re.compile(
        r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{"
    )
    match = pattern.search(script)
    assert match is not None, f"function {name}() is missing from app.js"
    depth = 0
    for index in range(match.end() - 1, len(script)):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                return script[match.end() : index]
    raise AssertionError(f"unbalanced braces in {name}()")


def test_the_roster_card_has_a_member_list_and_an_add_panel(ui) -> None:
    html = ui["index.html"]
    for element_id in (
        "roster-meeting-select",
        "roster-count-pill",
        "roster-rows",
        "roster-empty",
        "roster-slots",
        "roster-add-search",
        "roster-add-search-btn",
        "roster-candidates",
        "roster-capacity-input",
        "roster-capacity-save-btn",
    ):
        assert f'id="{element_id}"' in html, element_id


def test_the_member_table_shows_status_consent_and_voiceprint(ui) -> None:
    script = ui["app.js"]
    body = _js_function(script, "renderRosterMembers")
    assert "consentBadge(entry)" in body
    assert "voiceprintBadge(entry)" in body
    assert "NONAKTIF" in body or "stateBadge" in body
    assert "Keluarkan dari roster" in body


def test_add_and_remove_are_wired_to_the_existing_endpoints(ui) -> None:
    script = ui["app.js"]
    add = _js_function(script, "addToRoster")
    remove = _js_function(script, "removeFromRoster")
    assert "'/enrollment/meetings/' + rosterMeetingUuid + '/participants'" in add
    assert "participant_uuid: entry.uuid" in add
    assert (
        "'/enrollment/meetings/' + rosterMeetingUuid + '/participants/' + entry.uuid"
        in remove
    )
    assert "httpDelete(" in remove


def test_add_and_remove_use_uuids_never_integer_ids(ui) -> None:
    script = ui["app.js"]
    for name in ("addToRoster", "removeFromRoster", "renderRosterMembers"):
        body = _js_function(script, name)
        assert ".id" not in body.replace("rosterMeetingUuid", ""), name
        assert "participant_id" not in body, name
        assert "meeting_id" not in body, name


def test_add_is_disabled_when_the_roster_is_full(ui) -> None:
    script = ui["app.js"]
    candidates = _js_function(script, "searchCandidates")
    assert "add.disabled = rosterFull" in candidates
    members = _js_function(script, "renderRosterMembers")
    assert "rosterFull = members.length >= capacity" in members, (
        "fullness must be derived from the server's own counts"
    )


def test_a_conflict_still_refreshes_rather_than_leaving_a_stale_counter(ui) -> None:
    add = _js_function(ui["app.js"], "addToRoster")
    tail = add[add.index("if (!envelope.ok)") :]
    assert "loadRoster()" in tail, "a 409 usually means the page's counter is stale"
    assert "searchCandidates()" in tail


def test_add_and_remove_refresh_the_roster_and_the_counter(ui) -> None:
    script = ui["app.js"]
    for name in ("addToRoster", "removeFromRoster"):
        body = _js_function(script, name)
        assert "renderRoster(" in body, name
        assert "searchCandidates()" in body, name
        assert "loadMeetings()" in body, f"{name} must refresh the selector labels"


def test_both_actions_run_through_the_single_flight_guard(ui) -> None:
    script = ui["app.js"]
    assert "once(function () { return addToRoster(entry); })" in script
    assert "once(function () { return removeFromRoster(entry); })" in script


def test_the_candidate_list_is_bounded_and_searchable(ui) -> None:
    script = ui["app.js"]
    assert "var CANDIDATE_PAGE = 25" in script
    body = _js_function(script, "searchCandidates")
    assert "limit: CANDIDATE_PAGE" in body
    assert "include_inactive: false" in body, (
        "an inactive participant cannot join a roster, so do not offer them"
    )
    assert "query.search = text" in body


def test_the_candidate_list_makes_no_request_per_participant(ui) -> None:
    """One call for the page, not one per row, and no polling."""
    body = _js_function(ui["app.js"], "searchCandidates")
    loop = body[body.index("candidates.forEach(function (entry) {") :]
    for call in ("httpGet", "httpPost", "httpPatch", "httpDelete"):
        assert call not in loop, f"the render loop calls {call} per candidate"
    members = _js_function(ui["app.js"], "renderRosterMembers")
    for call in ("httpGet", "httpPost", "httpPatch", "httpDelete"):
        assert call not in members, f"renderRosterMembers calls {call} per row"
    assert "setInterval" not in ui["app.js"]


def test_members_already_on_the_roster_are_not_offered_again(ui) -> None:
    body = _js_function(ui["app.js"], "searchCandidates")
    assert "onRoster[p.uuid]" in body


def test_the_roster_ui_renders_no_markup_from_data(ui) -> None:
    script = ui["app.js"]
    for name in (
        "renderRosterMembers",
        "searchCandidates",
        "addToRoster",
        "removeFromRoster",
    ):
        body = _js_function(script, name)
        assert "innerHTML" not in body, name
        assert "outerHTML" not in body, name
        assert "document.write" not in body, name
        for banned in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
            assert banned not in body, f"{name} uses {banned}"


def test_the_roster_ui_sends_no_audio_token_or_key(ui) -> None:
    script = ui["app.js"]
    for name in ("renderRosterMembers", "searchCandidates", "addToRoster", "removeFromRoster"):
        body = _js_function(script, name).lower()
        for banned in (
            "getusermedia",
            "audiocontext",
            "mediarecorder",
            "token",
            "nonce",
            "ciphertext",
            "embedding",
            "dpapi",
        ):
            assert banned not in body, f"{name} mentions {banned}"


def test_the_ui_states_that_removal_does_not_delete_anything(ui) -> None:
    html = ui["index.html"]
    roster = html[html.index('id="roster-card"') :]
    roster = roster[: roster.index("</article>")]
    flat = " ".join(roster.split())
    assert "tidak</strong> menghapusnya dari" in flat or "tidak menghapusnya dari" in flat
    assert "direktori" in flat.lower()
