"""Database access (direct connection, no SSH tunnel)."""

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from .config import get_settings


def run_query(sql: str) -> pd.DataFrame:
    s = get_settings()
    url = URL.create(
        drivername=s.db_driver,
        username=s.db_user,
        password=s.db_password,
        host=s.db_host,
        port=s.db_port,
        database=s.db_name,
    )
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return pd.read_sql(text(sql), conn)
    finally:
        engine.dispose()
