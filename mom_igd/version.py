"""Single source of truth for application identity and schema versions.

Keep this module dependency-free: it is imported by everything, including the
diagnostics code that must still work when the rest of the application cannot
be configured.
"""

from __future__ import annotations

APP_NAME: str = "MoM-IGD"
"""Human-facing application name shown in the shell and in exports."""

APP_VERSION: str = "0.2.0"
"""Application version. Must stay in sync with ``pyproject.toml``.

Minor version tracks the roadmap phase while the product is pre-1.0: 0.1.x was the
Phase 1 foundation, 0.2.x is Phase 2 audio capture. A test asserts the two files
agree, because a version that disagrees with the packaging metadata is worse than
no version at all.
"""

CURRENT_PHASE: str = "2"
"""Roadmap phase this build implements. Used by diagnostics and the shell.

Advancing this changes what ``doctor`` calls a FAIL rather than a WARN, so it is
raised only once the phase's automated gate is green -- never in anticipation.
"""

CONFIG_SCHEMA_VERSION: int = 2
"""Version of the configuration file schema (``config/default.toml``).

Bumped by Phase 2, which added the whole ``[audio]`` section. The validator demands
an exact match, and ``AppConfig`` forbids unknown keys -- so a Phase 1 build handed
this file would otherwise fail with a confusing "unknown key: audio" instead of the
designed "unsupported schema version" message.

A ``local.toml`` that simply omits ``config_schema_version`` keeps working. One that
pins ``= 1`` now fails loudly, which is correct: it asserts a schema this build no
longer speaks.

1 -- Phase 1 foundation.
2 -- Phase 2 adds ``[audio]``.
"""

REGISTRY_SCHEMA_VERSION: int = 1
"""Version of the model registry schema (``models/registry.json``)."""

SCHEMA_VERSION_HEAD: int = 2
"""Highest database migration version shipped with this build.

1 -- Phase 1 foundation (nine tables).
2 -- Phase 2 audio capture: recording lifecycle, device identity, chunk integrity.
"""

USER_AGENT: str = f"{APP_NAME}/{APP_VERSION} (offline; loopback-only)"
"""Identifier used for loopback requests. Never used against a remote host."""


def version_info() -> dict[str, str | int]:
    """Return a serialisable identity block for ``/version`` and diagnostics."""
    return {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "phase": CURRENT_PHASE,
        "config_schema_version": CONFIG_SCHEMA_VERSION,
        "registry_schema_version": REGISTRY_SCHEMA_VERSION,
        "schema_version_head": SCHEMA_VERSION_HEAD,
    }
