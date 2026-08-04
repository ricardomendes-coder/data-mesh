from collections.abc import Mapping
from typing import Any

from fastapi import Request


class NotAuthenticated(Exception):
    """Raised by `require_login` when there is no logged-in user."""


def get_current_user(request: Request) -> str | None:
    return request.session.get("user")


def require_login(request: Request) -> str:
    """FastAPI dependency: returns the username or triggers a redirect to /login."""
    user = get_current_user(request)
    if not user:
        raise NotAuthenticated()
    return user


def start_session(
    request: Request,
    username: str,
    *,
    via: str,
    claims: Mapping[str, Any] | None = None,
) -> None:
    """Mark the session as logged in. `via` is "sso" or "password".

    Clears first so a pre-login session (e.g. the OAuth state authlib parked
    there) can't be reused across the privilege change.
    """
    request.session.clear()
    request.session["user"] = username
    request.session["auth_via"] = via
    if claims:
        if claims.get("email"):
            request.session["email"] = str(claims["email"])
        if claims.get("name"):
            request.session["name"] = str(claims["name"])


def end_session(request: Request) -> None:
    request.session.clear()
