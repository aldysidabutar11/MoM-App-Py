"""Single source of truth for application identity and schema versions.

Keep this module dependency-free: it is imported by everything, including the
diagnostics code that must still work when the rest of the application cannot
be configured.
"""

from __future__ import annotations

APP_NAME: str = "MoM-IGD"
"""Human-facing application name shown in the shell and in exports."""

APP_VERSION: str = "0.1.0"
"""Application version. Must stay in sync with ``pyproject.toml``."""

CURRENT_PHASE: str = "1"
"""Roadmap phase this build implements. Used by diagnostics and the shell."""

CONFIG_SCHEMA_VERSION: int = 1
"""Version of the configuration file schema (``config/default.toml``)."""

REGISTRY_SCHEMA_VERSION: int = 1
"""Version of the model registry schema (``models/registry.json``)."""

SCHEMA_VERSION_HEAD: int = 1
"""Highest database migration version shipped with this build."""

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
