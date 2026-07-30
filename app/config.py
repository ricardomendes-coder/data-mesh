from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (see .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- App ----
    session_secret: str = "change-me-in-production"
    app_title: str = "Report Hub"

    # Session cookie name. MUST NOT be "session": this app is served from the
    # same origin as Superset (bi.v360.io/report vs bi.v360.io/), and Flask's
    # default cookie is also called "session" — sharing the name means each app
    # overwrites the other's login.
    session_cookie_name: str = "report_hub_session"
    # Browser-visible path the cookie is scoped to. Behind the nginx /report/
    # mount this can be tightened to "/report" so the cookie is never sent on
    # Superset requests. Left at "/" so plain `uvicorn app.main:app` also works.
    session_cookie_path: str = "/"

    # ---- Authentication ----
    # sso      -> Keycloak only (the production setting)
    # password -> local username/password only
    # both     -> Keycloak, with the local form still reachable at /login/local
    #             as a break-glass path if the IdP is unavailable
    auth_mode: Literal["sso", "password", "both"] = "sso"

    # ---- Keycloak / OIDC ----
    # Same realm Superset authenticates against, so a user already signed in to
    # BI 360 arrives here without a second prompt. Credentials come from the
    # existing Superset client (see bi360/web.env on the BI host).
    sso_client_id: str = ""
    sso_client_secret: str = ""
    sso_metadata_url: str = (
        "https://sso.v360.io/realms/v360/.well-known/openid-configuration"
    )
    sso_scope: str = "openid email profile"
    # Must byte-for-byte match a Valid Redirect URI on the Keycloak client. Leave
    # empty to derive it from the request — that only works if the ALB sends
    # X-Forwarded-Proto, so setting it explicitly in production is safer.
    sso_redirect_uri: str = ""
    # Off by default: the Keycloak client is shared with Superset, so an
    # RP-initiated logout would sign the user out of BI 360 as well.
    sso_single_logout: bool = False
    sso_logout_redirect_uri: str = ""

    # ---- Bootstrap admin (only created on first startup if no users exist) ----
    initial_admin_user: str | None = None
    initial_admin_password: str | None = None

    # ---- Reports ----
    # Manifest declaring the available reports (see reports.toml).
    reports_file: str = "reports.toml"

    # ---- Database (direct connection from wherever this app runs) ----
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    # Default database: the one preselected in the query picker.
    db_name: str = ""
    # Catalog database used only to enumerate the others for the picker.
    db_catalog: str = "postgres"
    db_user: str = ""
    db_password: str = ""
    # SQLAlchemy driver string. Change this to target another database:
    #   Postgres      -> postgresql+psycopg2
    #   MySQL/MariaDB -> mysql+pymysql      (add `pymysql` to requirements.txt)
    #   SQL Server    -> mssql+pyodbc       (add `pyodbc` + an ODBC driver)
    db_driver: str = "postgresql+psycopg2"

    @property
    def sso_enabled(self) -> bool:
        """SSO is only live once a client id/secret is actually configured."""
        return self.auth_mode in ("sso", "both") and bool(
            self.sso_client_id and self.sso_client_secret
        )

    @property
    def password_login_enabled(self) -> bool:
        return self.auth_mode in ("password", "both")


@lru_cache
def get_settings() -> Settings:
    return Settings()
