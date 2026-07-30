"""Keycloak (OpenID Connect) client for the SSO login.

Points at the same realm Superset uses — see `bi360/superset_config.py` on the
BI host, which registers a `v360_login` provider against
`sso.v360.io/realms/v360`. Because the realm (and the Keycloak client) are
shared, a user already signed in to BI 360 is bounced straight back here
without being prompted again.

The claim mapping below deliberately mirrors Superset's
`CustomSsoSecurityManager.oauth_user_info`, so the same person shows up under
the same username in both tools.
"""

from typing import Any, Mapping

from authlib.integrations.starlette_client import OAuth

from .config import get_settings

# Provider name; matches the one Superset registers, purely for familiarity.
PROVIDER = "v360_login"

_oauth: OAuth | None = None


def get_client():
    """The registered OIDC client, built lazily on first use.

    Lazy because the metadata URL is only fetched when a login actually
    happens — importing this module must not require Keycloak to be reachable.
    """
    global _oauth
    if _oauth is None:
        settings = get_settings()
        oauth = OAuth()
        oauth.register(
            name=PROVIDER,
            client_id=settings.sso_client_id,
            client_secret=settings.sso_client_secret,
            server_metadata_url=settings.sso_metadata_url,
            client_kwargs={"scope": settings.sso_scope},
        )
        _oauth = oauth
    return getattr(_oauth, PROVIDER)


def username_from_claims(claims: Mapping[str, Any]) -> str:
    """Pick the username, matching Superset's mapping (`preferred_username`).

    Falls back to email then subject so a realm user with an unusual profile
    can still sign in rather than hitting a hard error.
    """
    for claim in ("preferred_username", "email", "sub"):
        value = claims.get(claim)
        if value:
            return str(value)
    raise ValueError(f"no usable identity claim in token (got: {sorted(claims)})")


def display_name(claims: Mapping[str, Any], fallback: str) -> str:
    name = claims.get("name")
    if name:
        return str(name)
    given, family = claims.get("given_name"), claims.get("family_name")
    if given or family:
        return " ".join(part for part in (given, family) if part)
    return fallback
