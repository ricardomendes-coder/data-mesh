"""Predefined reports and their export formats.

Reports are declared in a TOML manifest (see reports.toml) so you can add or
edit them without touching code. Each report names a `database` on the server
and its SQL (a `sql_file` path, or inline `sql`). The manifest and SQL files
are read fresh on each request, so edits take effect on the next export.
"""

try:
    import tomllib  # Python 3.11+ (the app targets 3.12)
except ModuleNotFoundError:  # pragma: no cover - backport for Python <= 3.10
    import tomli as tomllib
import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pandas as pd

from . import db
from .config import get_settings

logger = logging.getLogger("report_hub")
BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Report:
    key: str
    title: str
    database: str
    sql_file: str | None = None
    sql: str | None = None
    description: str = ""
    # False for reports from reports.toml: those are edited in git, not here.
    editable: bool = False

    def resolve_sql(self) -> str:
        """Return the report's SQL, reading the .sql file fresh if configured."""
        if self.sql_file:
            return (BASE_DIR / self.sql_file).read_text()
        if self.sql:
            return self.sql
        raise ValueError(f"Report {self.key!r} has neither 'sql_file' nor 'sql'.")


def _manifest_path() -> Path:
    p = Path(get_settings().reports_file)
    return p if p.is_absolute() else BASE_DIR / p


def load_reports() -> list[Report]:
    """Parse the reports manifest. Returns [] if the manifest is absent."""
    path = _manifest_path()
    if not path.exists():
        return []
    with path.open("rb") as f:
        data = tomllib.load(f)

    reports: list[Report] = []
    seen: set[str] = set()
    for entry in data.get("report", []):
        key = entry["key"]
        if key in seen:
            raise ValueError(f"Duplicate report key {key!r} in {path.name}.")
        seen.add(key)
        reports.append(
            Report(
                key=key,
                title=entry.get("title", key),
                database=entry["database"],
                sql_file=entry.get("sql_file"),
                sql=entry.get("sql"),
                description=entry.get("description", ""),
                editable=False,
            )
        )
    return reports


def all_reports() -> list[Report]:
    """File reports plus the ones authored in the UI.

    Two sources on purpose. Reports in reports.toml stay in git with history
    and review; reports created in the app live in `report_hub` and are
    editable by whoever has the feature. A file report always wins a key
    collision — the reviewed definition is the authoritative one.
    """
    from . import store

    out = list(load_reports())
    file_keys = {r.key for r in out}
    if not store.available():
        return out
    try:
        for saved in store.list_reports():
            if saved.slug in file_keys:
                logger.warning(
                    "Report %r exists in both reports.toml and the database; the file wins.",
                    saved.slug,
                )
                continue
            out.append(
                Report(
                    key=saved.slug,
                    title=saved.title,
                    database=saved.source_db,
                    sql=saved.sql,
                    description=saved.description,
                    editable=True,
                )
            )
    except Exception:
        logger.exception("Could not read saved reports; showing file reports only")
    return out


def get_report(key: str) -> Report | None:
    return next((r for r in all_reports() if r.key == key), None)


def get_report_df(key: str, max_rows: int | None = None) -> pd.DataFrame:
    """Run the report identified by `key` against its database."""
    report = get_report(key)
    if report is None:
        raise KeyError(key)
    result = db.execute(report.resolve_sql(), report.database, max_rows=max_rows)
    if not result.returns_rows:
        raise ValueError(f"Report {key!r} did not return rows.")
    return result.to_dataframe()


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def to_xlsx_bytes(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Report")
    buf.seek(0)
    return buf.read()
