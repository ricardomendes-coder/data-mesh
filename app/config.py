from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (see .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- App ----
    session_secret: str = "change-me-in-production"
    app_title: str = "Report Hub"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
