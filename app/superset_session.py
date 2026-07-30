"""Delegated authentication: let Superset decide who the user is.

`bi.v360.io/` is Superset and `bi.v360.io/report` is this app, so the browser
already sends Superset's Flask session cookie on our requests. Rather than
verify that cookie ourselves — which would mean holding a copy of Superset's
SECRET_KEY and re-implementing its signing scheme — we hand it straight back to
Superset's own `/api/v1/me/` and use the answer. Superset stays the single
authority on who is logged in.

This mode exists because the shared Keycloak client (`V360-BI`) only permits
exact-match redirect URIs, and registering ours needs realm-admin rights we
don't have. Superset's callback *is* registered, so we borrow its login: an
unauthenticated visitor is bounced to Superset, does the normal Keycloak dance
there, and comes back with a session we can read.

`app/oidc.py` remains the better long-term answer — switch AUTH_MODE back to
"sso" once `https://bi.v360.io/report/auth/callback` is on the Keycloak client.
"""

import logging
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import get_settings

logger = logging.getLogger("report_hub")


async def identify(cookie_value: str) -> dict[str, Any] | None:
    """Ask Superset who owns this session cookie.

    Returns the `/api/v1/me/` result dict, or None if Superset doesn't
    recognize the session (or is unreachable — callers treat both as
    "not logged in" and redirect to the Superset login).
    """
    settings = get_settings()
    url = f"{settings.superset_internal_url.rstrip('/')}/api/v1/me/"
    headers = {
        # We reach nginx by IP, but Superset builds URLs from Host and runs
        # behind ENABLE_PROXY_FIX — send the public name it expects.
        "Host": settings.superset_host_header,
        "Cookie": f"{settings.superset_cookie_name}={cookie_value}",
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=settings.superset_timeout) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError:
        logger.exception("Could not reach Superset at %s to validate the session", url)
        return None

    if response.status_code == 401:
        return None  # the ordinary "not signed in to BI 360" case
    if response.status_code != 200:
        logger.warning(
            "Superset /api/v1/me/ answered %s (expected 200/401)", response.status_code
        )
        return None

    try:
        result = response.json().get("result") or {}
    except ValueError:
        logger.warning("Superset /api/v1/me/ returned a non-JSON body")
        return None

    if result.get("is_anonymous") or not result.get("username"):
        return None
    return result


def login_url() -> str:
    """Superset's login page — it owns the registered Keycloak redirect URI.

    Flask-AppBuilder carries `next` through the OAuth round-trip in its signed
    state, so the user lands back on the hub rather than on the BI home page.
    """
    settings = get_settings()
    if not settings.superset_next_url:
        return settings.superset_login_url
    separator = "&" if "?" in settings.superset_login_url else "?"
    return (
        settings.superset_login_url
        + separator
        + urlencode({"next": settings.superset_next_url})
    )


def claims_from_result(result: dict[str, Any]) -> dict[str, Any]:
    """Shape Superset's /me/ payload like the OIDC claims start_session expects."""
    name = " ".join(
        part for part in (result.get("first_name"), result.get("last_name")) if part
    )
    return {"email": result.get("email"), "name": name or result.get("username")}
