"""Seed the participant directory from a file, so a name is chosen and not typed.

The directory is the list an operator picks from when a meeting starts. Typing the
same twenty names before every meeting is how "Pak Sudarmin" becomes "pak sudarman"
in one meeting and "P. Sudarmin" in the next, and the minutes carry whichever was typed
that day.

WHY A KEY AND NOT THE NAME

Re-running must not register everybody a second time, which needs something stable to
recognise a row by. That something is emphatically **not** the display name: migration
0003 dropped the UNIQUE index on `display_name` on purpose, because two people in one
organisation genuinely share a name and refusing the second one -- or making somebody
invent "Budi 2" -- corrupts the registry to satisfy an index (ADR-0009).

So each entry carries its own `key`, stored in `external_ref` as ``seed:<key>``. That
column exists for exactly this: a reference to something outside this database. Identity
remains the UUID, the name remains a label, and two seeded people may share a name as
long as their keys differ.

WHAT IT WILL NOT DO

It never deactivates and never deletes. Removing a line from the file leaves the
participant in the directory, because by then they may be on the roster of meetings that
already happened, and a directory that quietly forgets an attendee makes those meetings
unreadable. `participant deactivate` is the deliberate action for that, and it does not
delete the row either.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

#: Written into `external_ref`. Namespaced so a participant seeded from a file is
#: distinguishable from one whose external reference came from an HR system.
SEED_PREFIX: Final[str] = "seed:"

#: Searched in order. The first that exists wins. `participants.local.toml` is
#: gitignored and `participants.toml` is not, so the choice between keeping the list
#: private and sharing it with a clone is made by which filename it is given.
DEFAULT_FILENAMES: Final[tuple[str, ...]] = (
    "participants.local.toml",
    "participants.toml",
)


class SeedError(RuntimeError):
    """The seed file is missing, unreadable, or says something impossible."""


@dataclass(frozen=True, slots=True)
class SeedEntry:
    key: str
    name: str
    role: str | None = None

    @property
    def external_ref(self) -> str:
        return f"{SEED_PREFIX}{self.key}"


@dataclass(frozen=True, slots=True)
class SeedOutcome:
    """What one import did, in terms an operator can check against the file."""

    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    source: Path | None = None
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.source) if self.source else None,
            "dry_run": self.dry_run,
            "created": list(self.created),
            "updated": list(self.updated),
            "unchanged": list(self.unchanged),
            "total": len(self.created) + len(self.updated) + len(self.unchanged),
        }


def resolve_seed_file(explicit: Path | None, config_dir: Path) -> Path:
    """Find the seed file, and say what was looked for when there is none."""
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise SeedError(
                f"Seed file not found: {path}. Pass a readable TOML file, or omit "
                f"--file to use {' or '.join(DEFAULT_FILENAMES)} in {config_dir}."
            )
        return path

    for name in DEFAULT_FILENAMES:
        candidate = config_dir / name
        if candidate.is_file():
            return candidate

    raise SeedError(
        "No participant seed file. Copy config/participants.example.toml to "
        f"{config_dir / DEFAULT_FILENAMES[0]}, put the names in it, and run this "
        "again. Acceptable filenames: " + ", ".join(DEFAULT_FILENAMES) + "."
    )


def load_seed(path: Path) -> tuple[SeedEntry, ...]:
    """Parse and validate the file. Every rejection names the offending line."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SeedError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise SeedError(f"{path} could not be read: {exc}") from exc

    rows = raw.get("participant")
    if not isinstance(rows, list) or not rows:
        raise SeedError(
            f"{path} defines no participants. Each entry is a `[[participant]]` block "
            'with `key` and `name`, for example:\n\n[[participant]]\nkey = "ayu"\n'
            'name = "Ayu"'
        )

    entries: list[SeedEntry] = []
    seen: dict[str, int] = {}
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise SeedError(f"{path}: entry {index} is not a `[[participant]]` table.")
        key = str(row.get("key", "")).strip()
        name = str(row.get("name", "")).strip()
        role = str(row.get("role", "")).strip() or None
        if not key:
            raise SeedError(
                f"{path}: entry {index} ({name or 'unnamed'}) has no `key`. A key is a "
                'short stable label such as "sudarmin"; it is what makes re-running '
                "this import add nobody twice."
            )
        if not name:
            raise SeedError(f"{path}: entry {index} (key {key!r}) has no `name`.")
        if key in seen:
            raise SeedError(
                f"{path}: key {key!r} is used by entry {seen[key]} and entry {index}. "
                "Keys must differ; two people may share a `name`, but not a `key`."
            )
        seen[key] = index
        entries.append(SeedEntry(key=key, name=name, role=role))
    return tuple(entries)


def import_participants(
    service: Any,
    entries: tuple[SeedEntry, ...],
    *,
    dry_run: bool = False,
    source: Path | None = None,
) -> SeedOutcome:
    """Register everyone in `entries` who is not registered already.

    `service` is a `ParticipantService`. Passed in rather than constructed so the
    caller owns the configuration -- the same reason every other entry point takes
    one, and what keeps the built-in 9/50 defaults from leaking in.
    """
    existing = _seeded_by_key(service)
    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []

    for entry in entries:
        current = existing.get(entry.key)
        if current is None:
            if not dry_run:
                service.create(
                    display_name=entry.name,
                    role=entry.role,
                    external_ref=entry.external_ref,
                )
            created.append(entry.name)
            continue

        # Present already. Only a changed name or role is worth a write: somebody
        # correcting a spelling in the file expects the directory to follow, and
        # expects the participant to keep the identity everything else points at.
        same_name = current["display_name"] == entry.name
        same_role = (current.get("role") or None) == entry.role
        if same_name and same_role:
            unchanged.append(entry.name)
            continue
        if not dry_run:
            # `update` reads `None` as "leave this field alone", which would make a
            # role impossible to remove: deleting the `role` line from the file would
            # silently keep the old one and this would report a change that did not
            # happen. An empty string is cleaned to NULL, so the file stays the
            # authority on both directions.
            service.update(
                current["uuid"],
                display_name=entry.name,
                role=entry.role if entry.role is not None else "",
            )
        updated.append(entry.name)

    return SeedOutcome(
        created=tuple(created),
        updated=tuple(updated),
        unchanged=tuple(unchanged),
        source=source,
        dry_run=dry_run,
    )


def _seeded_by_key(service: Any) -> dict[str, dict[str, Any]]:
    """Every participant this importer has registered, keyed by seed key.

    Paged, because `list` is bounded on purpose -- the directory holds everyone ever
    registered and an unbounded query is fine on day one and wrong in year three.
    Inactive rows are included: somebody who was deactivated has still been seeded,
    and re-registering them under a second UUID is not what re-running should do.
    """
    found: dict[str, dict[str, Any]] = {}
    offset = 0
    page_size = 200
    while True:
        page = service.list(include_inactive=True, limit=page_size, offset=offset)
        rows = page.get("participants") or []
        for row in rows:
            ref = row.get("external_ref") or ""
            if ref.startswith(SEED_PREFIX):
                found.setdefault(ref[len(SEED_PREFIX) :], row)
        if len(rows) < page_size:
            return found
        offset += page_size
