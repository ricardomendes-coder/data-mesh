"""Database access (direct connection).

The app connects straight to DB_HOST:DB_PORT (see config.py). All targets live
on the same server and share one login — only the *database* varies, so queries
take an optional `database` argument that overrides just that part of the URL.
"""

from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from .config import get_settings

# Databases that exist on the server but are never useful to target.
_SYSTEM_DATABASES = {"rdsadmin"}


def build_url(database: str | None = None) -> URL:
    """Build the SQLAlchemy URL for `database` (defaults to settings.db_name)."""
    s = get_settings()
    return URL.create(
        drivername=s.db_driver,
        username=s.db_user,
        password=s.db_password,
        host=s.db_host,
        port=s.db_port,
        database=database or s.db_name,
    )


def _engine(database: str | None = None):
    return create_engine(build_url(database))


def run_query(sql: str, database: str | None = None) -> pd.DataFrame:
    """Run a row-returning query (SELECT) and return the rows as a DataFrame.

    Used by reports. Raises if `sql` does not return rows.
    """
    engine = _engine(database)
    try:
        with engine.connect() as conn:
            return pd.read_sql(text(sql), conn)
    finally:
        engine.dispose()


def list_databases() -> list[str]:
    """Return the databases available on the server, for the ad-hoc DB picker.

    Connects to the catalog database (settings.db_catalog, usually "postgres")
    and reads pg_database. Raises on failure so the caller can degrade the UI.
    """
    engine = _engine(get_settings().db_catalog)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT datname FROM pg_database "
                    "WHERE datistemplate = false AND datallowconn "
                    "ORDER BY datname"
                )
            )
            return [r[0] for r in rows if r[0] not in _SYSTEM_DATABASES]
    finally:
        engine.dispose()


@dataclass
class QueryResult:
    """The outcome of an arbitrary statement.

    For a row-returning statement (SELECT, RETURNING, ...): `returns_rows` is
    True and `columns`/`rows` hold the data. For anything else (INSERT/UPDATE/
    DELETE/DDL): `returns_rows` is False and `rowcount` is the affected count.
    """

    returns_rows: bool
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    rowcount: int = -1

    def to_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows, columns=self.columns)


def execute(sql: str, database: str | None = None) -> QueryResult:
    """Run an arbitrary SQL statement against `database`.

    Row-returning statements come back as columns + rows; write/DDL statements
    are committed and reported via rowcount. Whatever the DB rejects is raised
    to the caller so the console can show the real error.
    """
    engine = _engine(database)
    try:
        with engine.connect() as conn:
            result = conn.execute(text(sql))
            if result.returns_rows:
                columns = list(result.keys())
                rows = [tuple(r) for r in result.fetchall()]
                return QueryResult(
                    returns_rows=True,
                    columns=columns,
                    rows=rows,
                    rowcount=len(rows),
                )
            conn.commit()
            return QueryResult(returns_rows=False, rowcount=result.rowcount)
    finally:
        engine.dispose()
