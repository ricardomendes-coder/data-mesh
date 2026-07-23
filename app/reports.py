"""The predefined report and its export formats.

The SQL lives in sql/report.sql so you can edit the query without touching code.
"""

from io import BytesIO
from pathlib import Path

import pandas as pd

from .db import run_query

REPORT_SQL_PATH = Path(__file__).resolve().parent.parent / "sql" / "report.sql"


def load_report_sql() -> str:
    return REPORT_SQL_PATH.read_text()


def get_report() -> pd.DataFrame:
    return run_query(load_report_sql())


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def to_xlsx_bytes(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Report")
    buf.seek(0)
    return buf.read()
