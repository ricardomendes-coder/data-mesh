"""The application's own transactional store.

Everything the app itself owns — charts today, dashboards and users next —
lives in a dedicated `report_hub` database with its own owner role, separate
from the `analytics` warehouse the query console reads. Two reasons that
separation matters: the console can write to analytics, and app state must not
share that blast radius; and the Datasets tab enumerates `analytics.public`, so
app tables living there would show up in the catalog as if they were data.

Unlike db.py — which opens and disposes an engine per query because it targets
a different database each time — this module holds one pooled engine, since
app state is read on nearly every request.

Schema changes go in MIGRATIONS as append-only steps. Each runs once and is
recorded; never edit a step that has shipped, add another.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from .config import get_settings

logger = logging.getLogger("report_hub")

_engine = None

MIGRATIONS: list[tuple[str, str]] = [
    (
        "0001_charts",
        """
        CREATE TABLE IF NOT EXISTS charts (
            id           bigserial PRIMARY KEY,
            slug         text        NOT NULL UNIQUE,
            title        text        NOT NULL,
            description  text        NOT NULL DEFAULT '',
            source_db    text        NOT NULL,
            sql          text        NOT NULL,
            chart_type   text        NOT NULL,
            x_column     text        NOT NULL,
            y_columns    jsonb       NOT NULL DEFAULT '[]'::jsonb,
            created_by   text        NOT NULL DEFAULT '',
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_at   timestamptz NOT NULL DEFAULT now()
        )
        """,
    ),
]


def available() -> bool:
    """Whether the app database is configured. Charts degrade to an empty
    state rather than erroring when it isn't."""
    return get_settings().app_db_configured


def _url() -> URL:
    s = get_settings()
    return URL.create(
        drivername=s.db_driver,
        username=s.app_db_user,
        password=s.app_db_password,
        host=s.app_db_host or s.db_host,
        port=s.app_db_port or s.db_port,
        database=s.app_db_name,
    )


def engine():
    global _engine
    if _engine is None:
        _engine = create_engine(_url(), pool_pre_ping=True, pool_size=5, max_overflow=5)
    return _engine


def reset_engine() -> None:
    """Drop the pooled engine — used by tests after changing settings."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def init_schema() -> None:
    """Apply any migrations this database hasn't seen. Safe to call on boot."""
    with engine().begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " name text PRIMARY KEY,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
        )
        applied = {r[0] for r in conn.execute(text("SELECT name FROM schema_migrations"))}
        for name, ddl in MIGRATIONS:
            if name in applied:
                continue
            conn.execute(text(ddl))
            conn.execute(text("INSERT INTO schema_migrations (name) VALUES (:n)"), {"n": name})
            logger.info("Applied migration %s", name)


# ── charts ─────────────────────────────────────────────────────────────────


@dataclass
class Chart:
    slug: str
    title: str
    source_db: str
    sql: str
    chart_type: str
    x_column: str
    y_columns: list[str] = field(default_factory=list)
    description: str = ""
    created_by: str = ""
    id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    slug = _SLUG_STRIP.sub("-", title.strip().lower()).strip("-")
    return slug or "chart"


def _row_to_chart(row: Any) -> Chart:
    m = row._mapping
    y = m["y_columns"]
    # psycopg2 hands jsonb back already decoded; be tolerant of a text column.
    if isinstance(y, str):
        y = json.loads(y)
    return Chart(
        id=m["id"],
        slug=m["slug"],
        title=m["title"],
        description=m["description"],
        source_db=m["source_db"],
        sql=m["sql"],
        chart_type=m["chart_type"],
        x_column=m["x_column"],
        y_columns=list(y or []),
        created_by=m["created_by"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
    )


_SELECT = (
    "SELECT id, slug, title, description, source_db, sql, chart_type, "
    "x_column, y_columns, created_by, created_at, updated_at FROM charts"
)


def list_charts() -> list[Chart]:
    with engine().connect() as conn:
        rows = conn.execute(text(f"{_SELECT} ORDER BY updated_at DESC"))
        return [_row_to_chart(r) for r in rows]


def get_chart(slug: str) -> Chart | None:
    with engine().connect() as conn:
        row = conn.execute(text(f"{_SELECT} WHERE slug = :slug"), {"slug": slug}).first()
        return _row_to_chart(row) if row else None


def unique_slug(title: str) -> str:
    """A slug not already taken, suffixing -2, -3 … as needed."""
    base = slugify(title)
    candidate, n = base, 1
    while get_chart(candidate) is not None:
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def save_chart(chart: Chart) -> Chart:
    """Insert, or update in place when the slug already exists."""
    params = {
        "slug": chart.slug,
        "title": chart.title,
        "description": chart.description,
        "source_db": chart.source_db,
        "sql": chart.sql,
        "chart_type": chart.chart_type,
        "x_column": chart.x_column,
        "y_columns": json.dumps(chart.y_columns),
        "created_by": chart.created_by,
    }
    with engine().begin() as conn:
        row = conn.execute(
            text(
                """
                INSERT INTO charts (slug, title, description, source_db, sql,
                                    chart_type, x_column, y_columns, created_by)
                VALUES (:slug, :title, :description, :source_db, :sql,
                        :chart_type, :x_column, CAST(:y_columns AS jsonb), :created_by)
                ON CONFLICT (slug) DO UPDATE SET
                    title       = EXCLUDED.title,
                    description = EXCLUDED.description,
                    source_db   = EXCLUDED.source_db,
                    sql         = EXCLUDED.sql,
                    chart_type  = EXCLUDED.chart_type,
                    x_column    = EXCLUDED.x_column,
                    y_columns   = EXCLUDED.y_columns,
                    updated_at  = now()
                RETURNING id, slug, title, description, source_db, sql, chart_type,
                          x_column, y_columns, created_by, created_at, updated_at
                """
            ),
            params,
        ).first()
    return _row_to_chart(row)


def delete_chart(slug: str) -> bool:
    with engine().begin() as conn:
        result = conn.execute(text("DELETE FROM charts WHERE slug = :s"), {"s": slug})
        return result.rowcount > 0
