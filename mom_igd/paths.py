"""Central runtime path service.

Every filesystem location used at runtime is derived here. No other module may
construct a runtime path by hand -- that rule is what keeps user recordings,
databases and model binaries out of the source tree.

Design rules enforced by this module:

* The default runtime data root is ``D:\\MoM-IGD-Data``, but it is never
  hardcoded as the only valid location: it can be overridden through the
  ``MOM_IGD_DATA_DIR`` environment variable or through application
  configuration.
* The data root must be an absolute, normalised path.
* The data root may not be the repository (or anything inside it), and the
  repository may not be inside the data root. Source and runtime data are
  strictly separated.
* The data root may not be a bare filesystem anchor such as ``D:\\``.
* Directories are created only through the explicit :meth:`RuntimePaths.ensure`
  initialisation call -- importing this module never touches the filesystem.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "DEFAULT_DATA_ROOT",
    "ENV_DATA_DIR",
    "RUNTIME_SUBDIRS",
    "PathValidationError",
    "RuntimePaths",
    "repo_root",
    "resolve_data_root",
]

DEFAULT_DATA_ROOT: Final[Path] = Path(r"D:\MoM-IGD-Data")
"""Default runtime data root (ADR-0003). Overridable; never the only option."""

ENV_DATA_DIR: Final[str] = "MOM_IGD_DATA_DIR"
"""Environment variable that overrides the configured runtime data root."""

RUNTIME_SUBDIRS: Final[tuple[str, ...]] = (
    "db",
    "recordings",
    "exports",
    "logs",
    "models",
    "temp",
    "backups",
    "voiceprints",
    "keys",
    "working",
    "branding",
)
"""Runtime subdirectories created under the data root by :meth:`ensure`.

``voiceprints`` and ``keys`` arrived with Phase 3. Creating the *directories* is
harmless and happens with every other one; what must stay lazy is the **content** --
the DPAPI-protected master key is generated only when an explicit enrollment needs
it, never by ``ensure()``, an import or ``doctor`` (ADR-0010).

``branding`` holds the operator's letterhead logo, and nothing else. It is a
directory the operator *writes into* rather than one the application fills: the only
thing read from it is an image named in configuration, and it is read at export time
only. An absent or unreadable logo is not an error -- the letterhead falls back to text.

``working`` arrived with Phase 4 and holds 16 kHz mono derivatives of master
recordings. It is deliberately *not* inside ``recordings``: a working copy is a
reproducible cache, and keeping it out of the master tree means a backup of the
evidence does not carry a duplicate of it, and reclaiming disk does not mean
walking into directories that hold the only copy of a meeting.
"""

_WRITE_PROBE_PREFIX: Final[str] = ".mom_igd_write_probe_"

_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
"""Canonical lower-case UUID. Used to validate identifiers that become filenames."""


class PathValidationError(ValueError):
    """Raised when a candidate runtime path violates a separation rule."""


def repo_root() -> Path:
    """Return the repository root that contains this package.

    ``mom_igd/paths.py`` -> ``mom_igd/`` -> repository root.

    Note for Phase 11: when the application is frozen with PyInstaller this
    resolves to the extraction directory, which is still never a valid data
    root, so the separation rule keeps holding.
    """
    return Path(__file__).resolve().parent.parent


def _normalise(raw: str | os.PathLike[str]) -> Path:
    """Expand, absolutise and normalise a candidate path without touching disk."""
    text = os.fspath(raw).strip().strip('"').strip("'")
    if not text:
        raise PathValidationError("Runtime data root is empty.")
    if "\0" in text:
        raise PathValidationError("Runtime data root contains a NUL byte.")

    expanded = os.path.expandvars(text)
    if "%" in expanded and expanded != text:
        # expandvars leaves unresolved %VAR% untouched; catch that explicitly so
        # a typo in configuration does not become a literal directory name.
        pass
    candidate = Path(expanded).expanduser()

    if not candidate.is_absolute():
        raise PathValidationError(
            f"Runtime data root must be an absolute path, got {text!r}. "
            "Relative paths are rejected because they would depend on the "
            "current working directory."
        )

    # resolve(strict=False) normalises '..', casing and short names without
    # requiring the directory to exist yet.
    return Path(os.path.normpath(candidate.resolve()))


def _validate(candidate: Path) -> Path:
    """Apply the source/runtime separation rules to a normalised path."""
    if candidate == Path(candidate.anchor):
        raise PathValidationError(
            f"Runtime data root may not be a filesystem root ({candidate}). "
            "Choose a dedicated directory such as D:\\MoM-IGD-Data."
        )

    root = repo_root()
    if candidate == root:
        raise PathValidationError(
            f"Runtime data root may not be the repository itself ({root}). "
            "Runtime data must live outside the source tree."
        )
    if candidate.is_relative_to(root):
        raise PathValidationError(
            f"Runtime data root {candidate} is inside the repository {root}. "
            "Recordings, databases and model binaries must never be written "
            "into the source tree."
        )
    if root.is_relative_to(candidate):
        raise PathValidationError(
            f"Repository {root} is inside the candidate data root {candidate}. "
            "That would place runtime data around the source tree; choose a "
            "dedicated directory instead."
        )
    return candidate


def resolve_data_root(
    explicit: str | os.PathLike[str] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> Path:
    """Resolve the runtime data root.

    Precedence: ``explicit`` argument, then ``MOM_IGD_DATA_DIR``, then
    :data:`DEFAULT_DATA_ROOT`.

    Args:
        explicit: Value from application configuration, if any.
        env: Environment mapping to read; defaults to ``os.environ``. Injectable
            so tests never have to mutate the real process environment.

    Raises:
        PathValidationError: If the resulting path violates a separation rule.
    """
    environ = os.environ if env is None else env

    raw: str | os.PathLike[str] | None = explicit
    source = "configuration"
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        from_env = environ.get(ENV_DATA_DIR)
        if from_env and from_env.strip():
            raw = from_env
            source = f"environment variable {ENV_DATA_DIR}"
        else:
            raw = DEFAULT_DATA_ROOT
            source = "built-in default"

    try:
        return _validate(_normalise(raw))
    except PathValidationError as exc:
        raise PathValidationError(f"{exc} (source: {source})") from None


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Resolved runtime locations derived from a single validated data root."""

    root: Path

    # -- construction -------------------------------------------------------

    @classmethod
    def from_data_root(
        cls,
        explicit: str | os.PathLike[str] | None = None,
        *,
        env: dict[str, str] | None = None,
    ) -> RuntimePaths:
        """Build from an explicit value / environment / default, with validation."""
        return cls(root=resolve_data_root(explicit, env=env))

    # -- derived locations --------------------------------------------------

    @property
    def db_dir(self) -> Path:
        return self.root / "db"

    @property
    def recordings_dir(self) -> Path:
        return self.root / "recordings"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def models_dir(self) -> Path:
        """Directory for model *binaries*. Never inside the repository."""
        return self.root / "models"

    @property
    def temp_dir(self) -> Path:
        return self.root / "temp"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def voiceprints_dir(self) -> Path:
        """Encrypted voiceprint envelopes. Never plaintext, never a display name."""
        return self.root / "voiceprints"

    @property
    def keys_dir(self) -> Path:
        """DPAPI-protected key material. Never committed, never logged."""
        return self.root / "keys"

    @property
    def branding_dir(self) -> Path:
        """Operator-supplied letterhead assets. Read-only to this application."""
        return self.root / "branding"

    def branding_asset(self, filename: str) -> Path:
        """Resolve one branding file by **bare filename**, never by path.

        A configured value is still a value from outside the code, so it is treated the
        way every other external identifier here is: the shape is enforced rather than
        trusted. Without this, `logo_filename = "..\\..\\keys\\master.bin"` would make the
        letterhead a file-read primitive pointed at the key store.
        """
        name = str(filename).strip()
        if (
            not name
            or "/" in name
            or "\\" in name
            or ".." in name
            or ":" in name
            or name.startswith(".")
        ):
            raise PathValidationError(
                f"Branding asset must be a bare filename inside the branding directory, "
                f"got {filename!r}. Put the file in {self.branding_dir} and name it "
                "without any path separator."
            )
        return self.branding_dir / name

    @property
    def working_dir(self) -> Path:
        """16 kHz mono working copies. Derived, reproducible, never the evidence."""
        return self.root / "working"

    def working_copy_path(self, recording_uuid: str) -> Path:
        """Return the working-copy path for one recording.

        The file name is the recording UUID, for the same reason a voiceprint's is:
        a meeting title or a participant name must never reach the filesystem. The
        UUID shape is enforced rather than trusted, so an identifier cannot smuggle
        a traversal through.
        """
        if not _UUID_RE.match(recording_uuid):
            raise PathValidationError(
                f"Recording identifier must be a lower-case UUID, got "
                f"{recording_uuid!r}."
            )
        return self.working_dir / f"{recording_uuid}-16k-mono.wav"

    def voiceprint_path(self, voiceprint_uuid: str) -> Path:
        """Return the envelope path for one voiceprint.

        The file name is the UUID and nothing else: a participant's name must never
        reach the filesystem, where it would leak into backups, file pickers and
        error messages. The UUID shape is enforced rather than trusted, so a
        traversal attempt cannot be smuggled through an identifier.
        """
        if not _UUID_RE.match(voiceprint_uuid):
            raise PathValidationError(
                f"Voiceprint identifier must be a lower-case UUID, got "
                f"{voiceprint_uuid!r}."
            )
        return self.voiceprints_dir / f"{voiceprint_uuid}.vpx"

    @property
    def all_dirs(self) -> tuple[Path, ...]:
        return (self.root, *(self.root / name for name in RUNTIME_SUBDIRS))

    def database_path(self, filename: str = "mom_igd.db") -> Path:
        """Return the SQLite database path for ``filename`` inside ``db/``."""
        if not filename or "/" in filename or "\\" in filename:
            raise PathValidationError(
                f"Database filename must be a bare file name, got {filename!r}."
            )
        return self.db_dir / filename

    def log_file(self, name: str = "mom_igd.log") -> Path:
        if not name or "/" in name or "\\" in name:
            raise PathValidationError(f"Log filename must be a bare name, got {name!r}.")
        return self.logs_dir / name

    # -- explicit initialisation -------------------------------------------

    def ensure(self) -> RuntimePaths:
        """Create the data root and every runtime subdirectory.

        This is the *only* place the application creates the runtime tree. It is
        called from an explicit initialisation path (``db init``, ``serve``,
        ``shell``), never as an import side effect.
        """
        for directory in self.all_dirs:
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def exists(self) -> bool:
        return self.root.is_dir()

    def missing_dirs(self) -> tuple[Path, ...]:
        return tuple(d for d in self.all_dirs if not d.is_dir())

    def is_writable(self) -> bool:
        """Probe write access without creating the runtime tree.

        If the data root exists, write a probe file inside it. If it does not,
        probe the nearest existing ancestor, because that is what determines
        whether :meth:`ensure` can succeed.
        """
        target = self.root
        while not target.is_dir():
            parent = target.parent
            if parent == target:  # reached the anchor without finding a directory
                return False
            target = parent

        probe = target / f"{_WRITE_PROBE_PREFIX}{uuid.uuid4().hex}"
        try:
            probe.write_bytes(b"")
        except OSError:
            return False
        else:
            try:
                probe.unlink()
            except OSError:  # pragma: no cover - probe written but not removable
                pass
            return True

    def describe(self) -> dict[str, str | bool | list[str]]:
        """Serialisable summary for diagnostics and the desktop shell."""
        return {
            "root": str(self.root),
            "exists": self.exists(),
            "writable": self.is_writable(),
            "missing": [str(p) for p in self.missing_dirs()],
            "db_dir": str(self.db_dir),
            "recordings_dir": str(self.recordings_dir),
            "branding_dir": str(self.branding_dir),
            "exports_dir": str(self.exports_dir),
            "logs_dir": str(self.logs_dir),
            "models_dir": str(self.models_dir),
            "temp_dir": str(self.temp_dir),
            "backups_dir": str(self.backups_dir),
        }
