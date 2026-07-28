"""Session-token handling for the loopback API.

The token is generated once per application start and exists only in process
memory. It is deliberately never written to:

* source code,
* the database,
* log files,
* URL query strings,
* static UI assets,
* browser ``localStorage`` / ``sessionStorage`` / cookies.

:class:`SessionToken` wraps the secret so that an accidental ``print``, f-string
or ``logging`` call cannot leak it: ``__str__`` and ``__repr__`` both return a
redacted placeholder, and the real value is reachable only through the explicit
``.value`` attribute.

Phase 1 scope: no encryption at rest, no key management, no consent workflow.
Those belong to Phase 11 and Phase 3 respectively.
"""

from __future__ import annotations

import hmac
import secrets
from typing import Final

__all__ = [
    "REDACTED",
    "SESSION_TOKEN_HEADER",
    "SessionToken",
    "redact",
]

SESSION_TOKEN_HEADER: Final[str] = "X-MoM-Session-Token"
"""Header the shell uses to present the session token. Never a query parameter."""

REDACTED: Final[str] = "<redacted>"

_TOKEN_BYTES: Final[int] = 32  # 256 bits of entropy
_MIN_TOKEN_CHARS: Final[int] = 32


class SessionToken:
    """A process-local bearer secret that resists accidental disclosure."""

    __slots__ = ("_value",)

    def __init__(self, value: str | None = None) -> None:
        if value is None:
            value = secrets.token_urlsafe(_TOKEN_BYTES)
        if not isinstance(value, str) or len(value) < _MIN_TOKEN_CHARS:
            raise ValueError(
                f"Session token must be a string of at least {_MIN_TOKEN_CHARS} "
                "characters."
            )
        self._value = value

    # -- controlled access --------------------------------------------------

    @property
    def value(self) -> str:
        """The secret itself. Every read is an explicit, auditable call site."""
        return self._value

    def matches(self, candidate: str | None) -> bool:
        """Constant-time comparison against a presented credential."""
        if not candidate:
            return False
        return hmac.compare_digest(self._value, candidate)

    def header(self) -> dict[str, str]:
        """Build the request header used by the shell's in-process proxy."""
        return {SESSION_TOKEN_HEADER: self._value}

    # -- leak resistance ----------------------------------------------------

    def __str__(self) -> str:  # pragma: no cover - trivial
        return REDACTED

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"SessionToken({REDACTED})"

    def __format__(self, format_spec: str) -> str:  # pragma: no cover - trivial
        return REDACTED

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SessionToken):
            return hmac.compare_digest(self._value, other._value)
        return NotImplemented

    def __hash__(self) -> int:  # pragma: no cover - trivial
        return hash(("SessionToken", self._value))

    def __reduce__(self):  # pragma: no cover - defensive
        raise TypeError(
            "SessionToken is not serialisable: it must never be pickled, cached "
            "or written to disk."
        )


def redact(text: str, *secrets_to_hide: str | SessionToken) -> str:
    """Replace every occurrence of the given secrets with :data:`REDACTED`."""
    result = text
    for item in secrets_to_hide:
        raw = item.value if isinstance(item, SessionToken) else item
        if raw:
            result = result.replace(raw, REDACTED)
    return result
