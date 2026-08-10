"""Seeding the participant directory: pick a name, never type one.

The value is not the twenty-two rows. It is that the operator running a recording does
not retype the same names before every meeting -- which is how "Pak Sudarmin" becomes
"pak sudarman" in one minute and "P. Sudarmin" in the next, and the document carries
whichever was typed that day.

Two properties carry the weight here and both have their own test: running the import
again must add nobody twice, and the display name must never become the thing that
identifies a person. The second is why the first needs a key at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mom_igd.enrollment.seed import (
    DEFAULT_FILENAMES,
    SEED_PREFIX,
    SeedError,
    import_participants,
    load_seed,
    resolve_seed_file,
)


@pytest.fixture
def service(config, paths, conn):
    from mom_igd.db.connection import connect
    from mom_igd.enrollment.participants import ParticipantService

    def _connect():
        return connect(
            paths.database_path(config.database.filename),
            busy_timeout_ms=config.database.busy_timeout_ms,
        )

    # `config=` is not optional: without it the service falls back to its built-in
    # 9/50 and two runtimes disagree about the same policy.
    return ParticipantService(_connect, config=config)


def write_seed(directory: Path, body: str, name: str = "participants.local.toml") -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


TWO = """
[[participant]]
key = "ayu"
name = "Ayu"

[[participant]]
key = "ts"
name = "TS"
role = "Notulis"
"""


# ===========================================================================
# Reading the file
# ===========================================================================


def test_a_well_formed_file_is_read(tmp_path: Path) -> None:
    entries = load_seed(write_seed(tmp_path, TWO))
    assert [entry.name for entry in entries] == ["Ayu", "TS"]
    assert entries[0].role is None
    assert entries[1].role == "Notulis"
    assert entries[0].external_ref == f"{SEED_PREFIX}ayu"


def test_an_entry_without_a_key_is_refused_by_name(tmp_path: Path) -> None:
    """The error has to name the offending line, or the operator hunts for it."""
    path = write_seed(tmp_path, '[[participant]]\nname = "Ayu"\n')
    with pytest.raises(SeedError, match="Ayu"):
        load_seed(path)


def test_an_entry_without_a_name_is_refused(tmp_path: Path) -> None:
    path = write_seed(tmp_path, '[[participant]]\nkey = "ayu"\n')
    with pytest.raises(SeedError, match="ayu"):
        load_seed(path)


def test_two_entries_sharing_a_key_are_refused(tmp_path: Path) -> None:
    """Silently keeping one of them would drop somebody from every meeting list."""
    path = write_seed(
        tmp_path,
        '[[participant]]\nkey = "budi"\nname = "Budi Santoso"\n\n'
        '[[participant]]\nkey = "budi"\nname = "Budi Rahardjo"\n',
    )
    with pytest.raises(SeedError, match="entry 1 and entry 2"):
        load_seed(path)


def test_an_empty_file_says_what_an_entry_looks_like(tmp_path: Path) -> None:
    with pytest.raises(SeedError, match=r"\[\[participant\]\]"):
        load_seed(write_seed(tmp_path, "# nobody here\n"))


def test_malformed_toml_is_refused_with_the_parser_s_reason(tmp_path: Path) -> None:
    with pytest.raises(SeedError, match="not valid TOML"):
        load_seed(write_seed(tmp_path, "[[participant]\nkey =\n"))


# ===========================================================================
# Finding the file
# ===========================================================================


def test_the_private_filename_is_preferred_over_the_shared_one(tmp_path: Path) -> None:
    """`participants.local.toml` is gitignored; `participants.toml` is not.

    Preferring the ignored one means an operator who has both does not accidentally
    seed from the copy that a clone would carry.
    """
    write_seed(tmp_path, TWO, name="participants.toml")
    private = write_seed(tmp_path, TWO, name="participants.local.toml")
    assert resolve_seed_file(None, tmp_path) == private
    assert DEFAULT_FILENAMES[0] == "participants.local.toml"


def test_the_shared_filename_is_used_when_there_is_no_private_one(tmp_path: Path) -> None:
    shared = write_seed(tmp_path, TWO, name="participants.toml")
    assert resolve_seed_file(None, tmp_path) == shared


def test_no_file_at_all_says_how_to_make_one(tmp_path: Path) -> None:
    with pytest.raises(SeedError, match="participants.example.toml"):
        resolve_seed_file(None, tmp_path)


def test_an_explicit_missing_file_is_not_silently_replaced_by_a_default(
    tmp_path: Path,
) -> None:
    """Falling back would seed from a file the operator did not name."""
    write_seed(tmp_path, TWO)
    with pytest.raises(SeedError, match="Seed file not found"):
        resolve_seed_file(tmp_path / "typo.toml", tmp_path)


# ===========================================================================
# Importing
# ===========================================================================


def test_everyone_in_the_file_is_registered(service, tmp_path: Path) -> None:
    outcome = import_participants(service, load_seed(write_seed(tmp_path, TWO)))
    assert sorted(outcome.created) == ["Ayu", "TS"]
    listing = service.list(limit=200)
    assert listing["total"] == 2
    assert {row["display_name"] for row in listing["participants"]} == {"TS", "Ayu"}


def test_running_it_again_registers_nobody_twice(service, tmp_path: Path) -> None:
    """The property that makes this safe to put in a setup script."""
    entries = load_seed(write_seed(tmp_path, TWO))
    import_participants(service, entries)
    again = import_participants(service, entries)
    assert again.created == ()
    assert again.updated == ()
    assert sorted(again.unchanged) == ["Ayu", "TS"]
    assert service.list(limit=200)["total"] == 2


def test_a_dry_run_writes_nothing(service, tmp_path: Path) -> None:
    outcome = import_participants(
        service, load_seed(write_seed(tmp_path, TWO)), dry_run=True
    )
    assert sorted(outcome.created) == ["Ayu", "TS"]
    assert outcome.dry_run is True
    assert service.list(limit=200)["total"] == 0


def test_the_display_name_is_not_the_identity(service, tmp_path: Path) -> None:
    """Two people, one name. Migration 0003 dropped the UNIQUE index for this.

    Refusing the second -- or making somebody type "Budi 2" -- corrupts the registry
    to satisfy an index. The key tells them apart; the name is a label (ADR-0009).
    """
    entries = load_seed(
        write_seed(
            tmp_path,
            '[[participant]]\nkey = "budi-s"\nname = "Budi"\n\n'
            '[[participant]]\nkey = "budi-r"\nname = "Budi"\n',
        )
    )
    outcome = import_participants(service, entries)
    assert outcome.created == ("Budi", "Budi")
    people = service.list(limit=200)["participants"]
    assert len({row["uuid"] for row in people}) == 2, "two people, two identities"


def test_correcting_a_spelling_keeps_the_same_person(service, tmp_path: Path) -> None:
    """The UUID must survive, because rosters and audit rows already point at it."""
    import_participants(
        service, load_seed(write_seed(tmp_path, '[[participant]]\nkey = "nb"\nname = "Pak Sudarman"\n'))
    )
    before = service.list(limit=200)["participants"][0]

    outcome = import_participants(
        service,
        load_seed(write_seed(tmp_path, '[[participant]]\nkey = "nb"\nname = "Pak Sudarmin"\n')),
    )
    assert outcome.updated == ("Pak Sudarmin",)
    after = service.list(limit=200)["participants"]
    assert len(after) == 1, "a corrected spelling must not register a second person"
    assert after[0]["uuid"] == before["uuid"]
    assert after[0]["display_name"] == "Pak Sudarmin"


def test_removing_a_role_from_the_file_removes_it_from_the_directory(
    service, tmp_path: Path
) -> None:
    """`update` reads None as "leave alone", so an omitted role must clear it."""
    import_participants(
        service,
        load_seed(write_seed(tmp_path, '[[participant]]\nkey = "ts"\nname = "TS"\nrole = "Notulis"\n')),
    )
    assert service.list(limit=200)["participants"][0]["role"] == "Notulis"

    import_participants(
        service, load_seed(write_seed(tmp_path, '[[participant]]\nkey = "ts"\nname = "TS"\n'))
    )
    assert service.list(limit=200)["participants"][0]["role"] is None


def test_dropping_somebody_from_the_file_never_removes_them(service, tmp_path: Path) -> None:
    """They may already be on the roster of a meeting that happened.

    A directory that quietly forgets an attendee makes that meeting unreadable
    afterwards. `participant deactivate` is the deliberate action, and it does not
    delete the row either.
    """
    import_participants(service, load_seed(write_seed(tmp_path, TWO)))
    import_participants(
        service, load_seed(write_seed(tmp_path, '[[participant]]\nkey = "ayu"\nname = "Ayu"\n'))
    )
    names = {row["display_name"] for row in service.list(limit=200)["participants"]}
    assert names == {"Ayu", "TS"}


def test_a_deactivated_person_is_not_registered_again(service, tmp_path: Path) -> None:
    """Otherwise re-running the import undoes a deactivation by making a new row."""
    entries = load_seed(write_seed(tmp_path, TWO))
    import_participants(service, entries)
    victim = next(
        row for row in service.list(limit=200)["participants"] if row["display_name"] == "TS"
    )
    service.set_active(victim["uuid"], active=False, reason="left the company")

    again = import_participants(service, entries)
    assert "TS" not in again.created
    assert service.list(limit=200)["total"] == 2


def test_a_participant_registered_by_hand_is_left_alone(service, tmp_path: Path) -> None:
    """Only rows this importer created carry a seed reference, and only those move."""
    service.create(display_name="Ayu", role="Tamu")
    import_participants(service, load_seed(write_seed(tmp_path, TWO)))

    people = service.list(limit=200)["participants"]
    assert len(people) == 3, "the hand-made row must not be adopted by a matching name"
    by_hand = [row for row in people if row["external_ref"] is None]
    assert len(by_hand) == 1 and by_hand[0]["role"] == "Tamu"


# ===========================================================================
# The shipped files
# ===========================================================================


def test_the_example_file_parses_and_shows_both_forms() -> None:
    """It is the only documentation of the format that cannot go stale silently."""
    example = Path(__file__).resolve().parents[1] / "config" / "participants.example.toml"
    entries = load_seed(example)
    assert len(entries) >= 2
    assert any(entry.role for entry in entries), "one entry must show a role"
    assert any(entry.role is None for entry in entries), "one must show it omitted"


def test_the_real_list_is_never_committed() -> None:
    """This repository is public. A name published to the internet stays published."""
    import subprocess

    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(  # noqa: S603 - fixed argv, repository path
        ["git", "ls-files", "config/"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if tracked.returncode != 0:  # pragma: no cover - not a git checkout
        pytest.skip("not a git working tree")
    files = set(tracked.stdout.split())
    assert "config/participants.local.toml" not in files, (
        "the real participant list is tracked; colleagues' names would be published"
    )


def test_no_real_name_from_the_local_list_reaches_a_tracked_file() -> None:
    """The list is gitignored; the names must not arrive by another door.

    They did. Three names from the operator's own roster were used as fixtures in this
    very file and as examples in the module it tests -- written during the session whose
    stated purpose was keeping that list out of a public repository. Nobody noticed
    until a pre-push scan, because `.gitignore` protects a path, not a string.

    Skips on a clone, where the local list does not exist.
    """
    import subprocess
    import tomllib

    root = Path(__file__).resolve().parents[1]
    local = root / "config" / "participants.local.toml"
    if not local.is_file():
        pytest.skip("no local participant list on this machine")

    try:
        names = [
            entry["name"]
            for entry in tomllib.loads(local.read_text(encoding="utf-8"))["participant"]
        ]
    except (tomllib.TOMLDecodeError, KeyError, TypeError):
        pytest.skip("the local participant list is not readable as a seed file")

    listing = subprocess.run(  # noqa: S603 - fixed argv, repository path
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, timeout=30
    )
    if listing.returncode != 0:  # pragma: no cover - not a git checkout
        pytest.skip("not a git working tree")

    # The repository path contains the developer's own name, so every tracked file
    # mentioning a directory would fire on it. Their own name is not the concern here;
    # publishing a colleague's is.
    owner = root.parts[-2] if len(root.parts) >= 2 else ""

    offenders: dict[str, list[str]] = {}
    for relative in listing.stdout.split():
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        found = sorted(
            name
            for name in names
            if name != owner
            and re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", text)
        )
        if found:
            offenders[relative] = found
    assert offenders == {}, (
        f"real participant names would be published: {offenders}. Use a placeholder in "
        "examples and fixtures; the point of the gitignored list is that these names "
        "stay off a public remote."
    )
