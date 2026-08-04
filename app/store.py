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
    (
        "0002_dashboards",
        """
        CREATE TABLE IF NOT EXISTS dashboards (
            id          bigserial   PRIMARY KEY,
            slug        text        NOT NULL UNIQUE,
            title       text        NOT NULL,
            description text        NOT NULL DEFAULT '',
            created_by  text        NOT NULL DEFAULT '',
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS dashboard_items (
            id           bigserial PRIMARY KEY,
            -- CASCADE on both sides is deliberate. Deleting a dashboard drops
            -- its layout; deleting a chart removes it from every dashboard
            -- rather than leaving a tile that can never render.
            dashboard_id bigint NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
            chart_id     bigint NOT NULL REFERENCES charts(id)     ON DELETE CASCADE,
            position     integer NOT NULL DEFAULT 0,
            -- Grid span: full | half | third. Text rather than an enum so a new
            -- width is a code change, not a migration.
            width        text    NOT NULL DEFAULT 'half'
        );

        CREATE INDEX IF NOT EXISTS dashboard_items_dashboard_position_idx
            ON dashboard_items (dashboard_id, position);
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


def unique_slug(title: str, exists=None) -> str:
    """A slug not already taken, suffixing -2, -3 … as needed.

    `exists` is the lookup to test against, so charts and dashboards each get a
    slug unique within their own table rather than sharing a namespace.
    """
    exists = exists or get_chart
    base = slugify(title)
    candidate, n = base, 1
    while exists(candidate) is not None:
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


# ── dashboards ─────────────────────────────────────────────────────────────

# Grid spans a tile may occupy. Text in the database rather than an enum, so
# adding one is a code change instead of a migration.
WIDTHS = ("third", "half", "full")
DEFAULT_WIDTH = "half"


@dataclass
class DashboardItem:
    """One tile: a chart placed on a dashboard at a position and width."""

    id: int
    chart: Chart
    position: int
    width: str = DEFAULT_WIDTH


@dataclass
class Dashboard:
    slug: str
    title: str
    description: str = ""
    created_by: str = ""
    id: int | None = None
    items: list[DashboardItem] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


_DASH_SELECT = (
    "SELECT id, slug, title, description, created_by, created_at, updated_at FROM dashboards"
)


def _row_to_dashboard(row: Any) -> Dashboard:
    m = row._mapping
    return Dashboard(
        id=m["id"],
        slug=m["slug"],
        title=m["title"],
        description=m["description"],
        created_by=m["created_by"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
    )


def list_dashboards() -> list[Dashboard]:
    """Dashboards with a tile count, most recently updated first.

    Tiles are not loaded here — the index only needs the count, and loading
    every chart for every dashboard would be a query per tile.
    """
    with engine().connect() as conn:
        rows = conn.execute(text(f"{_DASH_SELECT} ORDER BY updated_at DESC")).fetchall()
        dashboards = [_row_to_dashboard(r) for r in rows]
        counts = dict(
            conn.execute(
                text("SELECT dashboard_id, count(*) FROM dashboard_items GROUP BY 1")
            ).fetchall()
        )
    for d in dashboards:
        d.items = [None] * counts.get(d.id, 0)  # placeholder: only len() is used
    return dashboards


def get_dashboard(slug: str, with_items: bool = True) -> Dashboard | None:
    """A dashboard and, by default, its tiles joined to their charts."""
    with engine().connect() as conn:
        row = conn.execute(text(f"{_DASH_SELECT} WHERE slug = :s"), {"s": slug}).first()
        if row is None:
            return None
        dash = _row_to_dashboard(row)
        if not with_items:
            return dash
        # One join rather than a query per tile.
        items = conn.execute(
            text(
                """
                SELECT i.id, i.position, i.width,
                       c.id AS c_id, c.slug, c.title, c.description, c.source_db,
                       c.sql, c.chart_type, c.x_column, c.y_columns, c.created_by,
                       c.created_at, c.updated_at
                FROM dashboard_items i
                JOIN charts c ON c.id = i.chart_id
                WHERE i.dashboard_id = :id
                ORDER BY i.position, i.id
                """
            ),
            {"id": dash.id},
        ).fetchall()

    for r in items:
        m = r._mapping
        y = m["y_columns"]
        if isinstance(y, str):
            y = json.loads(y)
        dash.items.append(
            DashboardItem(
                id=m["id"],
                position=m["position"],
                width=m["width"] if m["width"] in WIDTHS else DEFAULT_WIDTH,
                chart=Chart(
                    id=m["c_id"],
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
                ),
            )
        )
    return dash


def save_dashboard(dash: Dashboard) -> Dashboard:
    """Insert, or update title/description in place when the slug exists."""
    with engine().begin() as conn:
        row = conn.execute(
            text(
                """
                INSERT INTO dashboards (slug, title, description, created_by)
                VALUES (:slug, :title, :description, :created_by)
                ON CONFLICT (slug) DO UPDATE SET
                    title       = EXCLUDED.title,
                    description = EXCLUDED.description,
                    updated_at  = now()
                RETURNING id, slug, title, description, created_by, created_at, updated_at
                """
            ),
            {
                "slug": dash.slug,
                "title": dash.title,
                "description": dash.description,
                "created_by": dash.created_by,
            },
        ).first()
    return _row_to_dashboard(row)


def delete_dashboard(slug: str) -> bool:
    with engine().begin() as conn:
        result = conn.execute(text("DELETE FROM dashboards WHERE slug = :s"), {"s": slug})
        return result.rowcount > 0


def _touch(conn, dashboard_id: int) -> None:
    """Bump updated_at so the index ordering reflects layout edits too."""
    conn.execute(
        text("UPDATE dashboards SET updated_at = now() WHERE id = :id"), {"id": dashboard_id}
    )


def add_item(dashboard_slug: str, chart_slug: str, width: str = DEFAULT_WIDTH) -> bool:
    """Append a chart to a dashboard. False if either no longer exists.

    A chart may appear more than once — the same series at two widths, or
    alongside a variant — so there's no uniqueness constraint to trip over.
    """
    if width not in WIDTHS:
        width = DEFAULT_WIDTH
    with engine().begin() as conn:
        ids = conn.execute(
            text(
                """
                SELECT (SELECT id FROM dashboards WHERE slug = :d),
                       (SELECT id FROM charts     WHERE slug = :c)
                """
            ),
            {"d": dashboard_slug, "c": chart_slug},
        ).first()
        dash_id, chart_id = ids[0], ids[1]
        if dash_id is None or chart_id is None:
            return False
        next_pos = conn.execute(
            text(
                "SELECT coalesce(max(position), -1) + 1 FROM dashboard_items "
                "WHERE dashboard_id = :id"
            ),
            {"id": dash_id},
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO dashboard_items (dashboard_id, chart_id, position, width) "
                "VALUES (:d, :c, :p, :w)"
            ),
            {"d": dash_id, "c": chart_id, "p": next_pos, "w": width},
        )
        _touch(conn, dash_id)
    return True


def remove_item(dashboard_slug: str, item_id: int) -> bool:
    with engine().begin() as conn:
        result = conn.execute(
            text(
                """
                DELETE FROM dashboard_items
                WHERE id = :i
                  AND dashboard_id = (SELECT id FROM dashboards WHERE slug = :d)
                """
            ),
            {"i": item_id, "d": dashboard_slug},
        )
        if result.rowcount:
            conn.execute(
                text("UPDATE dashboards SET updated_at = now() WHERE slug = :d"),
                {"d": dashboard_slug},
            )
        return result.rowcount > 0


def set_item_width(dashboard_slug: str, item_id: int, width: str) -> bool:
    if width not in WIDTHS:
        return False
    with engine().begin() as conn:
        result = conn.execute(
            text(
                """
                UPDATE dashboard_items SET width = :w
                WHERE id = :i
                  AND dashboard_id = (SELECT id FROM dashboards WHERE slug = :d)
                """
            ),
            {"w": width, "i": item_id, "d": dashboard_slug},
        )
        return result.rowcount > 0


def move_item(dashboard_slug: str, item_id: int, delta: int) -> bool:
    """Shift a tile earlier (-1) or later (+1) by swapping with its neighbour.

    Positions are rewritten densely from the current order first, so rows that
    arrived with duplicate or gapped positions still reorder predictably.
    """
    with engine().begin() as conn:
        dash_id = conn.execute(
            text("SELECT id FROM dashboards WHERE slug = :d"), {"d": dashboard_slug}
        ).scalar()
        if dash_id is None:
            return False

        ids = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT id FROM dashboard_items WHERE dashboard_id = :d ORDER BY position, id"
                ),
                {"d": dash_id},
            )
        ]
        if item_id not in ids:
            return False
        i = ids.index(item_id)
        j = i + delta
        if j < 0 or j >= len(ids):
            return False  # already at the end it's being moved toward
        ids[i], ids[j] = ids[j], ids[i]

        for position, iid in enumerate(ids):
            conn.execute(
                text("UPDATE dashboard_items SET position = :p WHERE id = :i"),
                {"p": position, "i": iid},
            )
        _touch(conn, dash_id)
    return True
