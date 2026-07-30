"""Single source of truth for application identity and schema versions.

Keep this module dependency-free: it is imported by everything, including the
diagnostics code that must still work when the rest of the application cannot
be configured.
"""

from __future__ import annotations

APP_NAME: str = "MoM-IGD"
"""Human-facing application name shown in the shell and in exports."""

APP_VERSION: str = "0.3.0"
"""Application version. Must stay in sync with ``pyproject.toml``.

Minor version tracks the roadmap phase while the product is pre-1.0: 0.1.x was the
Phase 1 foundation, 0.2.x Phase 2 audio capture, 0.3.x Phase 3 participants,
consent and voice enrollment. A test asserts the two files
agree, because a version that disagrees with the packaging metadata is worse than
no version at all.
"""

CURRENT_PHASE: str = "3"
"""Roadmap phase this build implements. Used by diagnostics and the shell.

Advancing this changes what ``doctor`` calls a FAIL rather than a WARN, so it is
raised only once the phase's automated gate is green -- never in anticipation.
"""

CONFIG_SCHEMA_VERSION: int = 4
"""Version of the configuration file schema (``config/default.toml``).

The validator demands an exact match, and ``AppConfig`` forbids unknown keys -- so
an older build handed a newer file would otherwise fail with a confusing "unknown
key" instead of the designed "unsupported schema version" message.

A ``local.toml`` that simply omits ``config_schema_version`` keeps working, and one
that omits ``[participants]`` also keeps working: every key in that section has a
default, so an operator's existing overrides are never reset by the bump. A file
that *pins* an older version fails loudly, which is correct -- it asserts a schema
this build no longer speaks.

1 -- Phase 1 foundation.
2 -- Phase 2 adds ``[audio]``.
3 -- Phase 3 corrective: adds ``[participants]`` (roster capacity default and the
     configurable safety ceiling), replacing the hard-coded nine.
4 -- Phase 4 adds ``[asr]``: thread counts and decode settings measured on the target
     device, the pass-2 selection thresholds and budget, VAD tuning, and the
     terminology glossary. No key in it selects a provider implementation.
"""

REGISTRY_SCHEMA_VERSION: int = 1
"""Version of the model registry schema (``models/registry.json``)."""

SCHEMA_VERSION_HEAD: int = 5
"""Highest database migration version shipped with this build.

1 -- Phase 1 foundation (nine tables).
2 -- Phase 2 audio capture: recording lifecycle, device identity, chunk integrity.
3 -- Phase 3 participants, biometric consent, encrypted voiceprints.
4 -- Phase 3 corrective: per-meeting ``participant_capacity``, defaulting to 9.
5 -- Phase 4 offline ASR: working copy, VAD run, speech regions, transcript
     revisions, segments and word timings. No speaker column -- that is Phase 5/6.
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
