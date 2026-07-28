"""Request dependencies: configuration access and session-token enforcement."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from mom_igd.config import AppConfig
from mom_igd.paths import RuntimePaths
from mom_igd.security import SESSION_TOKEN_HEADER, SessionToken

__all__ = [
    "ConfigDep",
    "PathsDep",
    "get_config",
    "get_paths",
    "require_session_token",
]


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


def get_paths(request: Request) -> RuntimePaths:
    return request.app.state.paths


ConfigDep = Annotated[AppConfig, Depends(get_config)]
PathsDep = Annotated[RuntimePaths, Depends(get_paths)]


async def require_session_token(
    request: Request,
    presented: Annotated[str | None, Header(alias=SESSION_TOKEN_HEADER)] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Require the per-process session token.

    Accepted transports: the ``X-MoM-Session-Token`` header, or
    ``Authorization: Bearer <token>``. The token in a **query string is refused
    outright** (400, not 401) even if it is correct: query strings end up in
    access logs, browser history and referrers, so accepting one would silently
    undermine the policy that the token is never written anywhere.
    """
    for forbidden in ("token", "session_token", "access_token", "api_key"):
        if forbidden in request.query_params:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Credential in query parameter {forbidden!r} is not accepted. "
                    f"Send the token in the {SESSION_TOKEN_HEADER} header."
                ),
            )

    token: SessionToken | None = getattr(request.app.state, "session_token", None)
    if token is None:  # pragma: no cover - create_app always sets one
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session token is not initialised.",
        )

    candidate = presented
    if not candidate and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            candidate = value.strip()

    if not token.matches(candidate):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid session token.",
            headers={"WWW-Authenticate": f"{SESSION_TOKEN_HEADER}"},
        )
