"""Database access (direct connection).

The app connects straight to DB_HOST:DB_PORT (see config.py). All targets live
on the same server and share one login — only the *database* varies, so queries
take an optional `database` argument that overrides just that part of the URL.
"""

from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import create_engine, event, text
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
    engine = create_engine(build_url(database))
    _tune_work_mem(engine)
    return engine


def _tune_work_mem(engine) -> None:
    """Give this app's warehouse connections a larger work_mem than the server
    default, so big sorts/hashes stay in memory instead of spilling to disk.

    Per-connection via a SET on checkout, not the RDS parameter group: the
    instance is shared, and work_mem is allocated per sort per connection, so a
    global bump multiplies memory across every system on it. Scoped here it only
    ever affects our own queries. See settings.warehouse_work_mem.
    """
    mem = get_settings().warehouse_work_mem
    if not mem:
        return

    @event.listens_for(engine, "connect")
    def _set(dbapi_conn, _record):
        with dbapi_conn.cursor() as cur:
            # A literal, not a bound param: SET doesn't take parameters. The
            # value is our own config, never user input.
            cur.execute(f"SET work_mem = '{mem}'")


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
    # True when the statement had more rows than `max_rows` allowed us to take.
    # `rowcount` is then the number fetched, not the number that existed — we
    # deliberately never count the full set, since that means running it twice.
    truncated: bool = False

    def to_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows, columns=self.columns)


def execute(
    sql: str,
    database: str | None = None,
    max_rows: int | None = None,
    params: dict | None = None,
) -> QueryResult:
    """Run an arbitrary SQL statement against `database`.

    Row-returning statements come back as columns + rows; write/DDL statements
    are committed and reported via rowcount. Whatever the DB rejects is raised
    to the caller so the console can show the real error.

    `max_rows` is a hard ceiling on what is pulled into memory. It is enforced
    with a server-side cursor (`stream_results`) plus fetchmany, NOT by wrapping
    the SQL in an outer LIMIT: wrapping breaks on trailing semicolons, multiple
    statements and non-SELECTs, and a user could defeat it anyway. Streaming
    means an unbounded `SELECT *` over a 1.8M-row table transfers only the rows
    we actually take.

    `params` are bound values, never string-formatted in. Dashboard filters use
    this: the filter's *column* comes from the filter definition (which only an
    editor sets), while the *value* comes from whoever is looking at the page —
    so the value has to travel as a parameter or it is an injection point.
    """
    engine = _engine(database)
    try:
        with engine.connect() as conn:
            if max_rows is not None:
                conn = conn.execution_options(stream_results=True, max_row_buffer=1000)
            result = conn.execute(text(sql), params or {})
            if result.returns_rows:
                columns = list(result.keys())
                if max_rows is None:
                    rows = [tuple(r) for r in result.fetchall()]
                    truncated = False
                else:
                    # One extra row is the truncation probe: if it comes back,
                    # there was more than the caller allowed.
                    batch = result.fetchmany(max_rows + 1)
                    truncated = len(batch) > max_rows
                    rows = [tuple(r) for r in batch[:max_rows]]
                    result.close()
                return QueryResult(
                    returns_rows=True,
                    columns=columns,
                    rows=rows,
                    rowcount=len(rows),
                    truncated=truncated,
                )
            conn.commit()
            return QueryResult(returns_rows=False, rowcount=result.rowcount)
    finally:
        engine.dispose()
