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

    # ---- SSH tunnel: the box running this container reaches the DB over SSH ----
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_key_path: str = "/app/secrets/ssh_key"
    ssh_key_passphrase: str | None = None

    # ---- Database (address as seen FROM the SSH host) ----
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_name: str = ""
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
