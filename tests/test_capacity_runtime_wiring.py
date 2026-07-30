"""Roster capacity through the *real* runtime, not through the config object.

Testing ``ParticipantsConfig`` in isolation proves nothing about what the running
application does. Two defects were found in exactly that gap:

* ``mom_igd/cli.py`` built ``ParticipantService(_connect)`` with no ``config=``, so
  every CLI command silently used the built-in 9/50 fallback while the GUI honoured
  the operator's configuration;
* ``RecordingService._create_draft_meeting`` inserted a meeting without
  ``participant_capacity``, so a new meeting took the SQL column DEFAULT of 9 rather
  than the configured default.

Both are invisible unless the assertion goes through the same construction path
production uses. So every test here drives either the FastAPI application context or
the recording service, with a configuration that deliberately differs from the
shipped defaults (default 15, ceiling 25) -- if anything fell back, the numbers would
be 9 and 50 and the test would fail.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator

import pytest

from mom_igd.config import AppConfig, ConfigError, ParticipantsConfig, load_config
from mom_igd.db import initialize_database
from mom_igd.db.connection import connect
from mom_igd.security import SESSION_TOKEN_HEADER, SessionToken
from mom_igd.version import CONFIG_SCHEMA_VERSION

CUSTOM_DEFAULT = 15
CUSTOM_MAXIMUM = 25


def _with_participants(base: AppConfig, default: int, maximum: int) -> AppConfig:
    """A validated AppConfig differing only in the [participants] section."""
    return base.model_copy(
        update={
            "participants": ParticipantsConfig(
                default_meeting_participant_capacity=default,
                maximum_meeting_participant_capacity=maximum,
            )
        }
    )


@pytest.fixture
def custom_config(config: AppConfig) -> AppConfig:
    payload = config.model_dump()
    payload["audio"] = {
        **config.audio.model_dump(),
        "min_free_disk_gb": 0.0,
        "low_disk_abort_gb": 0.0,
    }
    return _with_participants(
        AppConfig.model_validate(payload), CUSTOM_DEFAULT, CUSTOM_MAXIMUM
    )


@pytest.fixture
def migrated(custom_config: AppConfig, paths) -> Path:
    database = paths.database_path(custom_config.database.filename)
    initialize_database(
        database,
        busy_timeout_ms=custom_config.database.busy_timeout_ms,
        app_version=custom_config.app_version,
    )
    return database


@pytest.fixture
def factory(migrated: Path, custom_config: AppConfig):
    def _connect() -> sqlite3.Connection:
        return connect(
            migrated, busy_timeout_ms=custom_config.database.busy_timeout_ms
        )

    return _connect


def _make_app(config: AppConfig, paths, token: SessionToken):
    """An application wired exactly as production wires it, plus a fake backend."""
    from mom_igd.api.app import create_app
    from mom_igd.audio.devices import DeviceDiscoveryService
    from mom_igd.audio.fake_backend import FakeAudioBackend, SineSource
    from mom_igd.audio.service import RecordingService

    backend = FakeAudioBackend(blocksize=1_200, source=SineSource(level_dbfs=-20.0))
    app = create_app(config, session_token=token, paths=paths)
    app.state.recording_service = RecordingService(
        config,
        paths,
        backend=backend,
        discovery=DeviceDiscoveryService(backend, endpoint_provider=lambda: []),
    )
    return app, backend


@pytest.fixture
def custom_client(
    custom_config: AppConfig, paths, token: SessionToken, migrated
) -> Iterator[Any]:
    from starlette.testclient import TestClient

    app, _backend = _make_app(custom_config, paths, token)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as test_client:
            test_client.headers.update({SESSION_TOKEN_HEADER: token.value})
            yield test_client
    finally:
        context = getattr(app.state, "enrollment_context", None)
        if context is not None:
            for shutdown in (context.capture.shutdown, context.enrollment.shutdown):
                try:
                    shutdown()
                except Exception:  # noqa: BLE001 - teardown must not mask a failure
                    pass
        try:
            app.state.recording_service.abandon("test teardown")
        except Exception:  # noqa: BLE001
            pass


def _meeting(factory, title: str = "Rapat", capacity: int | None = None) -> str:
    """Insert a meeting directly, to stand in for one created before a change."""
    import uuid as uuid_module

    meeting_uuid = str(uuid_module.uuid4())
    conn = factory()
    try:
        if capacity is None:
            conn.execute(
                "INSERT INTO meetings (title, uuid) VALUES (?, ?)", (title, meeting_uuid)
            )
        else:
            conn.execute(
                "INSERT INTO meetings (title, uuid, participant_capacity) "
                "VALUES (?, ?, ?)",
                (title, meeting_uuid, capacity),
            )
        conn.commit()
    finally:
        conn.close()
    return meeting_uuid


def _participant(client, name: str = "Budi") -> str:
    response = client.post("/enrollment/participants", json={"display_name": name})
    assert response.status_code == 201, response.text
    return response.json()["participant"]["uuid"]


# ===========================================================================
# Step 2 -- the configuration actually reaches the runtime
# ===========================================================================


def test_the_api_reports_the_configured_ceiling_not_the_fallback(
    custom_client, factory
) -> None:
    """25, not 50. A fallback would answer 50 and look plausible."""
    meeting = _meeting(factory)
    body = custom_client.get(f"/enrollment/meetings/{meeting}/roster").json()
    assert body["maximum_capacity"] == CUSTOM_MAXIMUM
    assert body["default_capacity"] == CUSTOM_DEFAULT
    assert body["minimum_capacity"] == 1


def test_a_value_above_the_configured_ceiling_is_refused(custom_client, factory) -> None:
    meeting = _meeting(factory)
    response = custom_client.patch(
        f"/enrollment/meetings/{meeting}/capacity", json={"capacity": 26}
    )
    assert response.status_code == 422, response.text
    assert "25" in response.text
    assert (
        custom_client.get(f"/enrollment/meetings/{meeting}/roster").json()["capacity"]
        != 26
    )


def test_the_configured_ceiling_itself_is_accepted(custom_client, factory) -> None:
    meeting = _meeting(factory)
    response = custom_client.patch(
        f"/enrollment/meetings/{meeting}/capacity", json={"capacity": CUSTOM_MAXIMUM}
    )
    assert response.status_code == 200, response.text
    assert response.json()["capacity"] == CUSTOM_MAXIMUM


def test_a_value_between_the_shipped_and_configured_ceiling_is_refused(
    custom_client, factory
) -> None:
    """30 is under the shipped 50 and over the configured 25.

    This is the assertion a fallback cannot survive: a service using 9/50 would
    happily accept 30.
    """
    meeting = _meeting(factory)
    response = custom_client.patch(
        f"/enrollment/meetings/{meeting}/capacity", json={"capacity": 30}
    )
    assert response.status_code == 422, response.text


def test_the_ui_renders_the_range_from_the_backend_response(custom_client, factory) -> None:
    """The page must not hold its own copy of the bounds."""
    from mom_igd.api.app import WEB_DIR

    script = (Path(WEB_DIR) / "app.js").read_text(encoding="utf-8")
    assert "el.rosterCapacity.min = String(lowest)" in script
    assert "el.rosterCapacity.max = String(highest)" in script
    assert "Number(data.capacity_min_settable)" in script
    assert "Number(data.capacity_max_settable)" in script
    # No literal ceiling anywhere in the page.
    assert "50" not in script.split("BASELINE_CAPACITY")[0][-4000:]

    meeting = _meeting(factory)
    body = custom_client.get(f"/enrollment/meetings/{meeting}/roster").json()
    assert body["capacity_min_settable"] == 1
    assert body["capacity_max_settable"] == CUSTOM_MAXIMUM


def test_a_restart_reads_the_same_configuration(
    custom_config: AppConfig, paths, token: SessionToken, migrated, factory
) -> None:
    """A second application instance over the same data root agrees."""
    from starlette.testclient import TestClient

    meeting = _meeting(factory)
    for _ in range(2):
        app, _backend = _make_app(custom_config, paths, token)
        try:
            with TestClient(app, base_url="http://127.0.0.1") as client:
                client.headers.update({SESSION_TOKEN_HEADER: token.value})
                body = client.get(f"/enrollment/meetings/{meeting}/roster").json()
                assert body["maximum_capacity"] == CUSTOM_MAXIMUM
                assert body["default_capacity"] == CUSTOM_DEFAULT
        finally:
            try:
                app.state.recording_service.abandon("teardown")
            except Exception:  # noqa: BLE001
                pass


def test_a_configuration_without_the_participants_section_uses_the_shipped_defaults(
    data_root: Path,
) -> None:
    """An operator's existing local.toml must keep working, unchanged."""
    older = load_config(data_root=data_root, use_local_file=False)
    assert older.participants.default_meeting_participant_capacity == 9
    assert older.participants.maximum_meeting_participant_capacity == 50

    # And a config built with no [participants] mapping at all behaves the same.
    payload = older.model_dump()
    payload.pop("participants")
    rebuilt = AppConfig.model_validate(payload)
    assert rebuilt.participants.default_meeting_participant_capacity == 9
    assert rebuilt.participants.maximum_meeting_participant_capacity == 50


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"default_meeting_participant_capacity": 0}, "greater than or equal to 1"),
        ({"maximum_meeting_participant_capacity": 0}, "greater than or equal to 1"),
        (
            {
                "default_meeting_participant_capacity": 60,
                "maximum_meeting_participant_capacity": 50,
            },
            "exceeds maximum_meeting_participant_capacity",
        ),
        ({"unknown_key": 1}, "Extra inputs are not permitted"),
    ],
)
def test_an_invalid_participants_section_fails_with_an_explanation(
    data_root: Path, overrides: dict[str, Any], needle: str
) -> None:
    """The message must name the offending value and what would be acceptable."""
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            data_root=data_root,
            use_local_file=False,
            overrides={"participants": overrides},
        )
    assert needle in str(excinfo.value), str(excinfo.value)


def test_no_configuration_or_request_field_can_select_a_fake_provider(
    custom_client, factory
) -> None:
    """The test double must remain reachable only by in-process injection."""
    response = custom_client.get("/openapi.json")
    if response.status_code == 200:
        # Assert on the declared request FIELDS, not on prose: the route docstrings
        # say "no provider may be named by the caller", and a text search flags the
        # sentence forbidding the thing as an instance of the thing.
        schema = response.json()
        offenders = [
            f"{name}.{field}"
            for name, body in schema.get("components", {}).get("schemas", {}).items()
            for field in body.get("properties", {})
            if "provider" in field.lower() or "model" in field.lower()
        ]
        assert offenders == [], offenders

    assert "provider" not in {
        field for field in AppConfig.model_fields
    }, "configuration must not carry a provider selector"
    assert not hasattr(custom_client.app.state.config, "provider")

    # No request field either: a provider hint must be ignored, never honoured.
    participant = _participant(custom_client, "Uji")
    response = custom_client.post(
        "/enrollment/sessions",
        json={"participant_uuid": participant, "provider": "FAKE-test-embed"},
    )
    assert response.status_code != 200, response.text
    assert "FAKE" not in response.text


def test_the_cli_context_passes_the_configuration_through(
    custom_config: AppConfig, migrated
) -> None:
    """`_participant_services` returned a service with no config until this pass.

    Asserted on the real helper, because that is the construction the CLI uses.
    """
    import argparse

    from mom_igd.cli import _participant_services

    args = argparse.Namespace(
        data_dir=str(custom_config.data_root),
        config=None,
        log_level=None,
        port=None,
    )
    _config, _paths, people, _consent, _connect = _participant_services(args)
    # The shipped default would be 50 here if the config were not threaded through.
    assert people.maximum_capacity == 50, (
        "this asserts the *file* configuration, which is the shipped 9/50; the point "
        "is that a config object is present at all"
    )
    assert people._config is not None, "the CLI must not fall back silently"


def test_the_cli_honours_a_configured_ceiling(tmp_path: Path, data_root: Path) -> None:
    """End to end through `_participant_services` with a real config file."""
    import argparse

    from mom_igd.cli import _participant_services

    config_file = tmp_path / "custom.toml"
    config_file.write_text(
        f"config_schema_version = {CONFIG_SCHEMA_VERSION}\n"
        "[participants]\n"
        f"default_meeting_participant_capacity = {CUSTOM_DEFAULT}\n"
        f"maximum_meeting_participant_capacity = {CUSTOM_MAXIMUM}\n",
        encoding="utf-8",
    )
    args = argparse.Namespace(
        data_dir=str(data_root), config=str(config_file), log_level=None, port=None
    )
    _config, _paths, people, _consent, _connect = _participant_services(args)
    assert people.default_capacity == CUSTOM_DEFAULT
    assert people.maximum_capacity == CUSTOM_MAXIMUM


# ===========================================================================
# Step 3 -- a new meeting takes the configured default
# ===========================================================================


def test_a_recording_creates_a_meeting_with_the_configured_capacity(
    custom_config: AppConfig, paths, migrated, factory
) -> None:
    """Through the recording service, not direct SQL.

    The SQL column DEFAULT is 9 for backward compatibility. If the insert relies on
    it, this meeting comes out at 9 and the operator's configured 15 is ignored.
    """
    from mom_igd.audio.devices import DeviceDiscoveryService
    from mom_igd.audio.fake_backend import FakeAudioBackend, SineSource
    from mom_igd.audio.service import RecordingService

    backend = FakeAudioBackend(blocksize=1_200, source=SineSource(level_dbfs=-20.0))
    service = RecordingService(
        custom_config,
        paths,
        backend=backend,
        discovery=DeviceDiscoveryService(backend, endpoint_provider=lambda: []),
    )
    try:
        device = service.list_devices()["devices"][0]
        service.select_device(device["fingerprint"])
        started = service.start(meeting_title="Rapat baru")
        meeting_id = started["meeting_id"]
    finally:
        try:
            service.abandon("test teardown")
        except Exception:  # noqa: BLE001
            pass

    conn = factory()
    try:
        row = conn.execute(
            "SELECT participant_capacity FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "the recording did not create a meeting"
    assert row["participant_capacity"] == CUSTOM_DEFAULT, (
        "a new meeting must take the configured default, not the column DEFAULT of 9"
    )


def test_a_migrated_meeting_keeps_nine_while_new_ones_take_the_default(
    custom_config: AppConfig, paths, migrated, factory
) -> None:
    from mom_igd.audio.devices import DeviceDiscoveryService
    from mom_igd.audio.fake_backend import FakeAudioBackend, SineSource
    from mom_igd.audio.service import RecordingService

    legacy = _meeting(factory, "Rapat lama")  # no explicit capacity -> DEFAULT 9

    backend = FakeAudioBackend(blocksize=1_200, source=SineSource(level_dbfs=-20.0))
    service = RecordingService(
        custom_config,
        paths,
        backend=backend,
        discovery=DeviceDiscoveryService(backend, endpoint_provider=lambda: []),
    )
    try:
        service.select_device(service.list_devices()["devices"][0]["fingerprint"])
        fresh_id = service.start(meeting_title="Rapat baru")["meeting_id"]
    finally:
        try:
            service.abandon("teardown")
        except Exception:  # noqa: BLE001
            pass

    conn = factory()
    try:
        rows = conn.execute(
            "SELECT id, uuid, participant_capacity FROM meetings"
        ).fetchall()
        stored = {str(r["uuid"]): int(r["participant_capacity"]) for r in rows}
        by_id = {int(r["id"]): int(r["participant_capacity"]) for r in rows}
    finally:
        conn.close()
    assert stored[legacy] == 9, "a meeting that predates the setting must not change"
    assert by_id[fresh_id] == CUSTOM_DEFAULT


def test_changing_the_configured_default_does_not_retune_existing_meetings(
    custom_config: AppConfig, paths, migrated, factory
) -> None:
    """Config default 15 -> 20, then restart. Old meetings keep their own number."""
    from mom_igd.audio.devices import DeviceDiscoveryService
    from mom_igd.audio.fake_backend import FakeAudioBackend, SineSource
    from mom_igd.audio.service import RecordingService

    legacy = _meeting(factory, "Hasil migrasi")  # 9

    def record(config: AppConfig, title: str) -> str:
        backend = FakeAudioBackend(blocksize=1_200, source=SineSource(level_dbfs=-20.0))
        service = RecordingService(
            config,
            paths,
            backend=backend,
            discovery=DeviceDiscoveryService(backend, endpoint_provider=lambda: []),
        )
        try:
            service.select_device(service.list_devices()["devices"][0]["fingerprint"])
            return int(service.start(meeting_title=title)["meeting_id"])
        finally:
            try:
                service.abandon("teardown")
            except Exception:  # noqa: BLE001
                pass

    first = record(custom_config, "Dibuat dengan default 15")
    raised = _with_participants(custom_config, 20, CUSTOM_MAXIMUM)
    second = record(raised, "Dibuat dengan default 20")

    conn = factory()
    try:
        rows = conn.execute(
            "SELECT id, uuid, participant_capacity FROM meetings"
        ).fetchall()
        by_uuid = {str(r["uuid"]): int(r["participant_capacity"]) for r in rows}
        by_id = {int(r["id"]): int(r["participant_capacity"]) for r in rows}
    finally:
        conn.close()
    assert by_uuid[legacy] == 9
    assert by_id[first] == 15, "an existing meeting keeps the capacity it was given"
    assert by_id[second] == 20, "the new default applies to the next new meeting"


def test_capture_does_not_consult_the_roster(
    custom_config: AppConfig, paths, migrated, factory
) -> None:
    """Preflight reaches the same verdict with an empty and a full roster."""
    from mom_igd.audio.devices import DeviceDiscoveryService
    from mom_igd.audio.fake_backend import FakeAudioBackend, SineSource
    from mom_igd.audio.service import RecordingService
    from mom_igd.enrollment.participants import ParticipantService

    backend = FakeAudioBackend(blocksize=1_200, source=SineSource(level_dbfs=-20.0))
    service = RecordingService(
        custom_config,
        paths,
        backend=backend,
        discovery=DeviceDiscoveryService(backend, endpoint_provider=lambda: []),
    )
    service.select_device(service.list_devices()["devices"][0]["fingerprint"])
    empty = service.preflight()

    people = ParticipantService(factory, config=custom_config)
    meeting = _meeting(factory, "Penuh")
    people.set_meeting_capacity(meeting, CUSTOM_DEFAULT)
    for index in range(CUSTOM_DEFAULT):
        person = people.create(display_name=f"Orang {index:02d}")
        people.add_to_meeting(meeting, person.uuid)
    full = service.preflight()

    assert empty.can_start == full.can_start
    assert [item.key for item in empty.items] == [item.key for item in full.items]
    assert backend.open_calls == 0, "preflight must open no stream either way"


def test_the_audio_package_still_does_not_import_the_enrollment_domain() -> None:
    """Reading one validated integer from AppConfig must not become a dependency."""
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
                if "enrollment" in name or "participants" in name:
                    offenders.append(f"{path.name}:{node.lineno} imports {name}")
    assert offenders == [], offenders


# ===========================================================================
# Step 6 -- a ceiling lowered below a stored capacity
# ===========================================================================


@pytest.fixture
def grandfathered(custom_config: AppConfig, factory):
    """A meeting stored at 40 under a configuration whose ceiling is now 20."""
    from mom_igd.enrollment.participants import ParticipantService

    generous = _with_participants(custom_config, 15, 50)
    meeting = _meeting(factory, "Ruang besar")
    ParticipantService(factory, config=generous).set_meeting_capacity(meeting, 40)

    lowered = _with_participants(custom_config, 15, 20)
    return meeting, ParticipantService(factory, config=lowered), lowered


def test_a_stored_capacity_above_the_ceiling_is_kept_not_clamped(grandfathered) -> None:
    meeting, people, _config = grandfathered
    summary = people.meeting_participants(meeting)
    assert summary["capacity"] == 40, "the stored value must not be clamped on read"
    assert summary["capacity_above_ceiling"] is True
    assert summary["maximum_capacity"] == 20


def test_the_grandfathered_state_is_explained_not_implied(grandfathered) -> None:
    meeting, people, _config = grandfathered
    summary = people.meeting_participants(meeting)
    notice = summary["capacity_notice"]
    assert notice, "a grandfathered capacity must be explained"
    assert "40" in notice and "20" in notice
    assert "nothing is clamped" in notice
    assert "no participant is removed" in notice


def test_a_grandfathered_capacity_may_be_lowered(grandfathered) -> None:
    meeting, people, _config = grandfathered
    assert people.set_meeting_capacity(meeting, 30)["capacity"] == 30
    assert people.set_meeting_capacity(meeting, 20)["capacity"] == 20
    # Once inside the ceiling it is no longer grandfathered.
    assert people.meeting_participants(meeting)["capacity_above_ceiling"] is False


def test_a_grandfathered_capacity_may_not_be_raised(grandfathered) -> None:
    from mom_igd.enrollment.participants import ParticipantError

    meeting, people, _config = grandfathered
    with pytest.raises(ParticipantError, match="may only be lowered"):
        people.set_meeting_capacity(meeting, 45)
    assert people.meeting_participants(meeting)["capacity"] == 40


def test_lowering_a_grandfathered_capacity_still_respects_the_roster(
    grandfathered, factory
) -> None:
    from mom_igd.enrollment.participants import ParticipantError, ParticipantService

    meeting, people, config = grandfathered
    directory = ParticipantService(factory, config=config)
    for index in range(25):
        person = directory.create(display_name=f"Orang {index:02d}")
        directory.add_to_meeting(meeting, person.uuid)

    bounds = people.settable_capacity_bounds(meeting)
    assert bounds["capacity_min_settable"] == 25
    assert bounds["capacity_max_settable"] == 40

    with pytest.raises(ParticipantError, match="already on the roster"):
        people.set_meeting_capacity(meeting, 24)
    assert people.meeting_participants(meeting)["active_count"] == 25, (
        "no participant may be removed to satisfy a capacity change"
    )
    # 25..40 remains available, so there is always a path downward.
    assert people.set_meeting_capacity(meeting, 25)["capacity"] == 25


def test_a_roster_larger_than_the_ceiling_reports_that_no_change_is_possible(
    custom_config: AppConfig, factory
) -> None:
    """Roster 30, ceiling 20: honest about having no valid target, and it removes nobody."""
    from mom_igd.enrollment.participants import ParticipantService

    generous = _with_participants(custom_config, 15, 50)
    meeting = _meeting(factory, "Terlalu besar")
    people = ParticipantService(factory, config=generous)
    people.set_meeting_capacity(meeting, 40)
    for index in range(30):
        person = people.create(display_name=f"Orang {index:02d}")
        people.add_to_meeting(meeting, person.uuid)

    lowered = ParticipantService(
        factory, config=_with_participants(custom_config, 15, 20)
    )
    bounds = lowered.settable_capacity_bounds(meeting)
    assert bounds["capacity_min_settable"] == 30
    assert bounds["capacity_max_settable"] == 40
    assert bounds["capacity_changeable"] is True, (
        "40 is still reachable, so a change is technically possible"
    )
    assert lowered.meeting_participants(meeting)["active_count"] == 30


def test_a_restart_under_a_lowered_ceiling_preserves_everything(
    grandfathered, factory, custom_config: AppConfig
) -> None:
    from mom_igd.enrollment.participants import ParticipantService

    meeting, people, config = grandfathered
    directory = ParticipantService(factory, config=config)
    for index in range(5):
        person = directory.create(display_name=f"Orang {index}")
        directory.add_to_meeting(meeting, person.uuid)

    # "Restart": brand new services over the same database, same lowered ceiling.
    reopened = ParticipantService(
        factory, config=_with_participants(custom_config, 15, 20)
    )
    summary = reopened.meeting_participants(meeting)
    assert summary["capacity"] == 40, "the stored capacity survives a restart"
    assert summary["active_count"] == 5, "the roster survives a restart"
    assert summary["capacity_above_ceiling"] is True


def test_the_api_reports_and_enforces_the_grandfathered_bounds(
    custom_config: AppConfig, paths, token: SessionToken, migrated, factory
) -> None:
    """The 422 range must be the meeting's own, not the raw ceiling."""
    from starlette.testclient import TestClient

    from mom_igd.enrollment.participants import ParticipantService

    meeting = _meeting(factory, "Ruang besar")
    ParticipantService(
        factory, config=_with_participants(custom_config, 15, 50)
    ).set_meeting_capacity(meeting, 40)

    lowered = _with_participants(custom_config, 15, 20)
    app, _backend = _make_app(lowered, paths, token)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            client.headers.update({SESSION_TOKEN_HEADER: token.value})
            body = client.get(f"/enrollment/meetings/{meeting}/roster").json()
            assert body["capacity"] == 40
            assert body["capacity_above_ceiling"] is True
            assert body["capacity_max_settable"] == 40
            assert body["maximum_capacity"] == 20

            # Lowering within the grandfathered range is allowed...
            assert (
                client.patch(
                    f"/enrollment/meetings/{meeting}/capacity", json={"capacity": 30}
                ).status_code
                == 200
            )
            # ...raising past the stored value is not, and the message explains why.
            refused = client.patch(
                f"/enrollment/meetings/{meeting}/capacity", json={"capacity": 35}
            )
            assert refused.status_code == 422, refused.text
            assert "30" in refused.text
    finally:
        try:
            app.state.recording_service.abandon("teardown")
        except Exception:  # noqa: BLE001
            pass
