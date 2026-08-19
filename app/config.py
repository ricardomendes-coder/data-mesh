from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (see .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- App ----
    session_secret: str = "change-me-in-production"
    app_title: str = "Report Hub"
    # Interface language. Portuguese by default because that's what the people
    # using this speak; English stays available per user. See app/i18n.py.
    default_locale: str = "pt"
    # How long a cached chart preview stays usable. Previews are for
    # recognising a chart, not for reading its numbers; the card shows the age.
    preview_cache_minutes: int = 60
    # How long a filter's option list stays usable. Longer than a preview: the
    # distinct values of a column change on the data's schedule, and rebuilding
    # them cost this app 292 seconds on one dashboard.
    filter_options_cache_minutes: int = 360
    # How long a rendered dashboard tile stays usable. Matched to the source,
    # not to a round number: the heavy dashboards read matviews the pipeline
    # REFRESHes about once a day (see the airflow refresh_mviews DAGs), so the
    # numbers simply don't move between refreshes — a 1h cache re-ran an 84s
    # query dozens of times a day to return the identical result. A day is the
    # cadence of the data; the "Atualizar" button on the dashboard forces a
    # rebuild for anyone who wants this minute's figures. Superset caches the
    # same queries for 24h too, the difference being the tile shows its age.
    tile_cache_minutes: int = 1440

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
    # superset -> delegate to the Superset login on the same origin (no
    #             Keycloak client change needed; see app/superset_session.py)
    # sso      -> talk to Keycloak directly (needs our redirect URI registered)
    # password -> local username/password only
    # both     -> "sso" plus the local form (kept as an alias; prefer
    #             ENABLE_LOCAL_LOGIN, which works with every mode)
    auth_mode: Literal["superset", "sso", "password", "both"] = "sso"

    # Break-glass switch, independent of auth_mode: re-exposes the bcrypt form
    # at /login/local when the identity provider is unreachable.
    enable_local_login: bool = False

    # How long a signed-in session stays valid. Matters most in "superset" mode,
    # where the Superset session is checked once at login and not re-checked on
    # every request — this bounds how long a hub session can outlive it.
    session_max_age: int = 12 * 60 * 60

    # ---- Delegated auth (AUTH_MODE=superset) ----
    # bi.v360.io/ is Superset and bi.v360.io/report is this app, so the browser
    # sends Superset's cookie here too. We hand it back to Superset's
    # /api/v1/me/ and let Superset say who the caller is.
    # Docker bridge gateway: the host's nginx, reachable from the container.
    superset_internal_url: str = "http://172.17.0.1"
    superset_host_header: str = "bi.v360.io"
    superset_cookie_name: str = "session"  # Flask's default; Superset keeps it
    superset_login_url: str = "https://bi.v360.io/login/"
    superset_next_url: str = "https://bi.v360.io/report/"
    superset_logout_url: str = "https://bi.v360.io/logout/"
    superset_timeout: float = 8.0

    # ---- Keycloak / OIDC ----
    # Same realm Superset authenticates against, so a user already signed in to
    # BI 360 arrives here without a second prompt. Credentials come from the
    # existing Superset client (see bi360/web.env on the BI host).
    sso_client_id: str = ""
    sso_client_secret: str = ""
    sso_metadata_url: str = "https://sso.v360.io/realms/v360/.well-known/openid-configuration"
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

    # ---- Query console limits ----
    # Hard ceiling on rows fetched by an ad-hoc query. A user can ask for less
    # (there's a box in the console) but never more — without it, one
    # `SELECT * FROM anticipation` pulls 1.8M rows into the app's memory.
    query_max_rows: int = 100_000
    # What the limit box starts at.
    query_default_rows: int = 1_000
    # Rows actually sent to the browser. The rest are reachable via Export:
    # embedding 100k rows would produce tens of MB of HTML and lock the tab.
    query_display_rows: int = 2_000
    # Rows per page in the result table.
    query_page_size: int = 50

    # ---- Reports ----
    # Manifest declaring the available reports (see reports.toml).
    reports_file: str = "reports.toml"

    # ---- Datasets ----
    # Curation layer over the live catalog: folders, descriptions, example
    # queries, hide list. Everything works without it — see datasets.toml.
    datasets_file: str = "datasets.toml"
    # Database + schema the catalog is built from. Everything in analytics
    # lives in `public`; there are no other schemas to walk.
    datasets_database: str = "analytics"
    datasets_schema: str = "public"
    dataset_preview_rows: int = 50
    # Ceiling on a preview/count so a scan of a huge table can't hang a page.
    dataset_timeout_ms: int = 10_000

    # ---- Application database ----
    # The app's own transactional store: charts (and later dashboards, users).
    # A dedicated database + owner role on the internal RDS, following the same
    # convention as gitlab/gitlab_user and keycloak/keycloak_user there. Kept
    # well away from `analytics`, which is a 300GB warehouse the query console
    # can write to — app state must not share that blast radius.
    # Host/port fall back to the DB_* connection when blank (same server).
    app_db_host: str = ""
    app_db_port: int = 0
    app_db_name: str = "report_hub"
    app_db_user: str = "report_hub_user"
    app_db_password: str = ""

    @property
    def app_db_configured(self) -> bool:
        return bool(self.app_db_password and (self.app_db_host or self.db_host))

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
    def superset_auth_enabled(self) -> bool:
        return self.auth_mode == "superset"

    @property
    def sso_enabled(self) -> bool:
        """SSO is only live once a client id/secret is actually configured."""
        return self.auth_mode in ("sso", "both") and bool(
            self.sso_client_id and self.sso_client_secret
        )

    @property
    def password_login_enabled(self) -> bool:
        return self.auth_mode in ("password", "both") or self.enable_local_login


@lru_cache
def get_settings() -> Settings:
    return Settings()
