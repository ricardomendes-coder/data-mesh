"""Database access (direct connection).

The app connects straight to DB_HOST:DB_PORT (see config.py). This is the
right model when the app runs somewhere with network access to the database
(e.g. an EC2 instance in the same VPC as the DB).
"""

from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from .config import get_settings


def _engine():
    s = get_settings()
    url = URL.create(
        drivername=s.db_driver,
        username=s.db_user,
        password=s.db_password,
        host=s.db_host,
        port=s.db_port,
        database=s.db_name,
    )
    return create_engine(url)


def run_query(sql: str) -> pd.DataFrame:
    """Run a row-returning query (SELECT) and return the rows as a DataFrame.

    Used by the predefined report. Raises if `sql` does not return rows.
    """
    engine = _engine()
    try:
        with engine.connect() as conn:
            return pd.read_sql(text(sql), conn)
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


def execute(sql: str) -> QueryResult:
    """Run an arbitrary SQL statement.

    Row-returning statements come back as columns + rows; write/DDL statements
    are committed and reported via rowcount. Whatever the DB rejects is raised
    to the caller so the console can show the real error.
    """
    engine = _engine()
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
