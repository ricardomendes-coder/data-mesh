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
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

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
    (
        "0003_users_roles",
        """
        -- Users are created on first login, the way Superset's
        -- AUTH_USER_REGISTRATION does it: the identity provider is the source
        -- of truth for who exists, this table for what they may see.
        CREATE TABLE IF NOT EXISTS users (
            id           bigserial   PRIMARY KEY,
            username     text        NOT NULL UNIQUE,
            email        text        NOT NULL DEFAULT '',
            display_name text        NOT NULL DEFAULT '',
            is_admin     boolean     NOT NULL DEFAULT false,
            is_active    boolean     NOT NULL DEFAULT true,
            auth_via     text        NOT NULL DEFAULT '',
            created_at   timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz
        );

        -- Grants hang off roles rather than users: at a few hundred people,
        -- per-user rows stop being manageable, and moving to roles later would
        -- mean migrating every grant.
        CREATE TABLE IF NOT EXISTS roles (
            id          bigserial   PRIMARY KEY,
            name        text        NOT NULL UNIQUE,
            description text        NOT NULL DEFAULT '',
            -- Roles handed to every newly self-registered user.
            is_default  boolean     NOT NULL DEFAULT false,
            created_at  timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS user_roles (
            user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role_id bigint NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, role_id)
        );

        -- One row per database a role may reach. Deliberately just a name: the
        -- server and credentials stay in env, because a single login already
        -- reaches every database on the instance. The boundary is who may see
        -- which name, not how we connect.
        CREATE TABLE IF NOT EXISTS role_databases (
            role_id       bigint NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
            database_name text   NOT NULL,
            PRIMARY KEY (role_id, database_name)
        );

        -- Seed the current behaviour so enabling this changes nothing for
        -- `analytics` while the other ~49 databases stop being on offer.
        INSERT INTO roles (name, description, is_default)
        VALUES ('Analytics',
                'Default role for new users: read the analytics warehouse.',
                true)
        ON CONFLICT (name) DO NOTHING;

        INSERT INTO role_databases (role_id, database_name)
        SELECT id, 'analytics' FROM roles WHERE name = 'Analytics'
        ON CONFLICT DO NOTHING;
        """,
    ),
    (
        "0004_role_permissions",
        """
        -- Generalises role_databases. A permission is (type, key), so the same
        -- table covers databases, reports, charts, dashboards and whatever gets
        -- added later — a new resource type is a constant, not a migration.
        -- key = '*' means "every resource of this type".
        CREATE TABLE IF NOT EXISTS role_permissions (
            role_id       bigint NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
            resource_type text   NOT NULL,
            resource_key  text   NOT NULL,
            PRIMARY KEY (role_id, resource_type, resource_key)
        );

        CREATE INDEX IF NOT EXISTS role_permissions_role_type_idx
            ON role_permissions (role_id, resource_type);

        -- Carry over anything already granted, then retire the old table.
        INSERT INTO role_permissions (role_id, resource_type, resource_key)
        SELECT role_id, 'database', database_name FROM role_databases
        ON CONFLICT DO NOTHING;

        DROP TABLE IF EXISTS role_databases;

        -- An Admin role that grants everything, so "sees everything" is visible
        -- and manageable in the UI rather than implied by a hidden flag.
        INSERT INTO roles (name, description, is_default)
        VALUES ('Admin', 'Full access to every database and object.', false)
        ON CONFLICT (name) DO NOTHING;

        INSERT INTO role_permissions (role_id, resource_type, resource_key)
        SELECT id, '*', '*' FROM roles WHERE name = 'Admin'
        ON CONFLICT DO NOTHING;

        -- Drop the seeded Analytics role. Its whole purpose was to preserve the
        -- pre-enforcement behaviour during the previous step; keeping it would
        -- silently grant `analytics` to every new user.
        DELETE FROM roles WHERE name = 'Analytics';
        """,
    ),
    (
        "0005_drop_admin_role",
        """
        -- The Admin role duplicated users.is_admin: two ways to say "sees
        -- everything", which can disagree. The flag wins — it also gates the
        -- admin panel, which a role never did — so the role goes.
        --
        -- Anyone holding it keeps their access via the flag if they had it;
        -- this does not silently promote anyone, so check the Users tab after
        -- applying if the role had members.
        DELETE FROM roles WHERE name = 'Admin';
        """,
    ),
    (
        "0006_reports",
        """
        -- Reports authored in the UI rather than in reports.toml. Deliberately
        -- the same shape as `charts` — slug/title/source_db/sql/created_by —
        -- because they are the same idea with a different renderer, and the
        -- store code, permission keys and admin screens all stay uniform.
        --
        -- reports.toml keeps working: file reports are merged in read-only, so
        -- the ones under git review are not lost.
        CREATE TABLE IF NOT EXISTS reports (
            id          bigserial   PRIMARY KEY,
            slug        text        NOT NULL UNIQUE,
            title       text        NOT NULL,
            description text        NOT NULL DEFAULT '',
            source_db   text        NOT NULL,
            sql         text        NOT NULL,
            created_by  text        NOT NULL DEFAULT '',
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        );
        """,
    ),
    (
        "0007_folders",
        """
        -- Folders are presentation, not permission. They decide how the list
        -- pages are grouped and nothing else: no folder resource type, no row
        -- in role_permissions, no expansion when access is resolved. Putting a
        -- chart in a folder can neither grant nor hide it, and the list pages
        -- group only what the viewer was already allowed to see.
        --
        -- Keeping it this way is the whole point. If a folder ever decided who
        -- may see something, "where does this live" and "who may read it"
        -- become one question with one answer, which is exactly the coupling
        -- this schema avoids.
        CREATE TABLE IF NOT EXISTS folders (
            id          bigserial   PRIMARY KEY,
            slug        text        NOT NULL UNIQUE,
            name        text        NOT NULL,
            description text        NOT NULL DEFAULT '',
            position    integer     NOT NULL DEFAULT 0,
            created_by  text        NOT NULL DEFAULT '',
            created_at  timestamptz NOT NULL DEFAULT now()
        );

        -- ON DELETE SET NULL, never CASCADE: deleting a folder must lose the
        -- grouping and never the content. A dropped folder means "these are
        -- ungrouped again", which is why membership is a column on the item
        -- rather than a join table — an item is in one place, like a folder.
        ALTER TABLE charts
            ADD COLUMN IF NOT EXISTS folder_id bigint
            REFERENCES folders(id) ON DELETE SET NULL;
        ALTER TABLE dashboards
            ADD COLUMN IF NOT EXISTS folder_id bigint
            REFERENCES folders(id) ON DELETE SET NULL;
        ALTER TABLE reports
            ADD COLUMN IF NOT EXISTS folder_id bigint
            REFERENCES folders(id) ON DELETE SET NULL;

        CREATE INDEX IF NOT EXISTS charts_folder_idx     ON charts (folder_id);
        CREATE INDEX IF NOT EXISTS dashboards_folder_idx ON dashboards (folder_id);
        CREATE INDEX IF NOT EXISTS reports_folder_idx    ON reports (folder_id);
        """,
    ),
    (
        "0008_dashboard_sections",
        """
        -- Tabs. A dashboard with no sections renders exactly as before, so this
        -- is additive: existing dashboards keep their flat list of tiles.
        CREATE TABLE IF NOT EXISTS dashboard_sections (
            id           bigserial PRIMARY KEY,
            dashboard_id bigint  NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
            title        text    NOT NULL,
            position     integer NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS dashboard_sections_dashboard_idx
            ON dashboard_sections (dashboard_id, position);

        -- SET NULL, not CASCADE: deleting a tab must not delete the charts that
        -- were on it. They fall back to the dashboard's untabbed area, where
        -- they are visible and can be re-filed.
        ALTER TABLE dashboard_items
            ADD COLUMN IF NOT EXISTS section_id bigint
            REFERENCES dashboard_sections(id) ON DELETE SET NULL;

        -- A tile is either a chart or a piece of text. Superset dashboards lean
        -- on markdown and headers to divide a page up, and importing them
        -- without that leaves an unlabelled wall of charts.
        ALTER TABLE dashboard_items
            ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'chart';
        ALTER TABLE dashboard_items
            ADD COLUMN IF NOT EXISTS content text NOT NULL DEFAULT '';
        -- Text tiles have no chart, so the column can no longer be mandatory.
        ALTER TABLE dashboard_items ALTER COLUMN chart_id DROP NOT NULL;

        CREATE INDEX IF NOT EXISTS dashboard_items_section_idx
            ON dashboard_items (section_id, position);
        """,
    ),
    (
        "0009_dashboard_filters",
        """
        -- A filter belongs to a dashboard and rewrites the charts on it.
        --
        -- The mechanism is a token: a chart's SQL contains {{ filters }} where
        -- its WHERE clause accepts more terms, and the token is replaced at
        -- render time with the active filters as *bound parameters*. Values are
        -- never interpolated — `column` is set by whoever edits the dashboard,
        -- but the value comes from the viewer's query string.
        --
        -- Filtering has to happen inside the chart's own query, not by wrapping
        -- it: wrapping an aggregate can't filter on a column that was grouped
        -- away, which is exactly what most of these filters do.
        CREATE TABLE IF NOT EXISTS dashboard_filters (
            id            bigserial PRIMARY KEY,
            dashboard_id  bigint  NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
            -- Used in the URL and as the parameter name, so it must be a plain
            -- identifier: [a-z0-9_].
            key           text    NOT NULL,
            label         text    NOT NULL,
            -- select | daterange | text
            filter_type   text    NOT NULL DEFAULT 'select',
            -- The column (or SQL expression) each chart is filtered on.
            column_expr   text    NOT NULL,
            -- For 'select': a query returning one column of options. Runs
            -- against source_db, which need not be the charts' database.
            values_sql    text    NOT NULL DEFAULT '',
            source_db     text    NOT NULL DEFAULT '',
            default_value text    NOT NULL DEFAULT '',
            -- Empty list = every chart on the dashboard carrying the token.
            -- Otherwise the chart slugs this filter is allowed to touch, which
            -- is how an imported Superset filter keeps its original scope.
            applies_to    jsonb   NOT NULL DEFAULT '[]'::jsonb,
            position      integer NOT NULL DEFAULT 0,
            UNIQUE (dashboard_id, key)
        );

        CREATE INDEX IF NOT EXISTS dashboard_filters_dashboard_idx
            ON dashboard_filters (dashboard_id, position);
        """,
    ),
    (
        "0010_tile_geometry",
        """
        -- Free placement on a 12-column grid, replacing the third/half/full
        -- width. Superset uses a 12-column grid too, which is what makes an
        -- imported dashboard able to keep its original layout exactly.
        --
        -- NULL grid_x means "not placed yet": those tiles flow in `position`
        -- order at their old width, so every dashboard that existed before this
        -- migration renders unchanged until someone moves a tile.
        ALTER TABLE dashboard_items ADD COLUMN IF NOT EXISTS grid_x integer;
        ALTER TABLE dashboard_items ADD COLUMN IF NOT EXISTS grid_y integer;
        ALTER TABLE dashboard_items ADD COLUMN IF NOT EXISTS grid_w integer;
        ALTER TABLE dashboard_items ADD COLUMN IF NOT EXISTS grid_h integer;

        CREATE INDEX IF NOT EXISTS dashboard_items_grid_idx
            ON dashboard_items (dashboard_id, grid_y, grid_x);
        """,
    ),
    (
        "0011_user_locale",
        """
        -- Interface language, per person. Kept on the user rather than in a
        -- cookie so the choice follows them to another browser, and NULL means
        -- "whatever the server default is" rather than pinning English.
        ALTER TABLE users ADD COLUMN IF NOT EXISTS locale text;
        """,
    ),
    (
        "0012_tags",
        """
        -- Tags label a chart or a dashboard so it can be found. Like folders,
        -- they are presentation only: no row in role_permissions, never read by
        -- access_for(). Tagging something can neither reveal nor hide it.
        --
        -- Unlike a folder, a tag is many-to-many — that is the whole point of a
        -- tag. A chart is "financeiro" *and* "mensal", where it lives in exactly
        -- one folder.
        CREATE TABLE IF NOT EXISTS tags (
            id         bigserial   PRIMARY KEY,
            name       text        NOT NULL,
            slug       text        NOT NULL UNIQUE,
            created_by text        NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now()
        );

        -- (type, key) rather than a foreign key per table: the same shape the
        -- permission and folder vocabularies use, so adding a taggable kind is
        -- a constant rather than a migration.
        CREATE TABLE IF NOT EXISTS resource_tags (
            tag_id        bigint NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            resource_type text   NOT NULL,
            resource_key  text   NOT NULL,
            PRIMARY KEY (tag_id, resource_type, resource_key)
        );

        CREATE INDEX IF NOT EXISTS resource_tags_resource_idx
            ON resource_tags (resource_type, resource_key);
        """,
    ),
    (
        "0013_chart_previews",
        """
        -- A rendered chart spec, kept so the previews on the Charts listing
        -- don't re-run every query on every visit.
        --
        -- The spec, not an image: Chart.js draws in the browser, so a picture
        -- would mean running a headless browser server-side — and would lose
        -- the tooltips and the legend. This is a few KB of labels and series
        -- that the same JS renders exactly as it renders a live one.
        --
        -- Cached with an age, never presented as current. A chart's numbers
        -- move when the *data* moves, not when someone edits the chart, so a
        -- snapshot taken at save time would quietly show last month's figures.
        -- The card says how old it is; app/main.py refreshes past the TTL.
        CREATE TABLE IF NOT EXISTS chart_previews (
            chart_id bigint      PRIMARY KEY REFERENCES charts(id) ON DELETE CASCADE,
            spec     jsonb       NOT NULL,
            built_at timestamptz NOT NULL DEFAULT now()
        );
        """,
    ),
    (
        "0014_tile_title_override",
        """
        -- A dashboard can rename a chart for its own purposes. Superset calls
        -- it sliceNameOverride, and 214 of the 1112 imported tiles carry one —
        -- so without this the tile shows a name nobody put there.
        --
        -- On the tile, not the chart: the same chart can appear on two
        -- dashboards under two names, which is exactly why the override exists.
        ALTER TABLE dashboard_items
            ADD COLUMN IF NOT EXISTS title_override text NOT NULL DEFAULT '';
        """,
    ),
    (
        "0015_filter_option_cache",
        """
        -- The values a select filter offers. Each is a
        -- `SELECT DISTINCT col FROM table` over the warehouse, and the
        -- dashboard page used to run all of them, in series, before sending a
        -- byte: on Automatismo that was eleven queries and 292 seconds for a
        -- drawer most visits never open.
        --
        -- Cached because a column's distinct values move on the data's
        -- schedule, not the viewer's — an hour-old list is the same list.
        CREATE TABLE IF NOT EXISTS dashboard_filter_options (
            dashboard_slug text        NOT NULL,
            filter_key     text        NOT NULL,
            options        jsonb       NOT NULL,
            built_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (dashboard_slug, filter_key)
        );
        """,
    ),
    (
        "0017_chart_result_variants",
        """
        -- A chart's own result, cached — now in two shapes rather than one.
        -- The listing thumbnail keeps at most a dozen table rows because it is
        -- a likeness; the chart's own page shows the whole result. Same chart,
        -- same query, different answers, so they cannot share one row.
        --
        -- Existing rows are thumbnails, which is what the default says.
        ALTER TABLE chart_previews
            ADD COLUMN IF NOT EXISTS variant text NOT NULL DEFAULT 'preview';
        ALTER TABLE chart_previews DROP CONSTRAINT IF EXISTS chart_previews_pkey;
        ALTER TABLE chart_previews ADD PRIMARY KEY (chart_id, variant);
        """,
    ),
    (
        "0016_tile_cache",
        """
        -- A rendered dashboard tile, kept so opening a dashboard doesn't
        -- re-run every query on it. On the Automatismo dashboard the fourteen
        -- tiles read one 5.7 GB materialized view and take 9s to 220s each;
        -- one of them dropped its connection at 162s. Superset survives the
        -- same dashboard by caching results in Redis for 24 hours — it isn't
        -- faster, it just isn't asking.
        --
        -- Keyed by the query, not by the tile: `sig` is a digest of the SQL
        -- and its bound parameters, which is exactly what decides the answer.
        -- Two viewers with the same filters share an entry; a different filter
        -- is a different key rather than a wrong hit.
        CREATE TABLE IF NOT EXISTS dashboard_tile_cache (
            item_id  bigint      NOT NULL REFERENCES dashboard_items(id) ON DELETE CASCADE,
            sig      text        NOT NULL,
            payload  jsonb       NOT NULL,
            built_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (item_id, sig)
        );

        CREATE INDEX IF NOT EXISTS dashboard_tile_cache_built_idx
            ON dashboard_tile_cache (built_at);
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
    # Display grouping only — see set_item_folder(). Never read when resolving
    # access, and deliberately not written by save_chart().
    folder_id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    slug = _SLUG_STRIP.sub("-", title.strip().lower()).strip("-")
    return slug or "chart"


def _row_to_chart(row: Any) -> Chart:
    m = row._mapping
    # The listing select omits `sql` — see list_charts().
    has_sql = "sql" in m
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
        sql=m["sql"] if has_sql else "",
        chart_type=m["chart_type"],
        x_column=m["x_column"],
        y_columns=list(y or []),
        created_by=m["created_by"],
        folder_id=m["folder_id"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
    )


_SELECT = (
    "SELECT id, slug, title, description, source_db, sql, chart_type, "
    "x_column, y_columns, created_by, folder_id, created_at, updated_at FROM charts"
)


# The listing select, deliberately without `sql`.
#
# Measured on the real 580-chart catalogue: the same query costs 41.9s with the
# SQL text and 2.3s without. The payload is a similar size either way — the cost
# is in reading 580 large out-of-line values, not the bytes — and nothing that
# lists charts reads .sql: the listing shows title, type, database and tags, the
# dashboard editor shows names, the permissions screen shows slugs.
_LIST_SELECT = (
    "SELECT id, slug, title, description, source_db, chart_type, "
    "x_column, y_columns, created_by, folder_id, created_at, updated_at FROM charts"
)


def list_charts(with_sql: bool = False) -> list[Chart]:
    """Every chart. `sql` comes back empty unless you ask for it.

    Pulling 580 query bodies to render 580 titles is the difference between a
    page that opens and one that doesn't. Anything needing the query text wants
    a single chart — use get_chart().
    """
    select = _SELECT if with_sql else _LIST_SELECT
    with engine().connect() as conn:
        rows = conn.execute(text(f"{select} ORDER BY updated_at DESC"))  # noqa: S608 — fixed
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
                -- folder_id is absent from both the INSERT and the UPDATE on
                -- purpose: saving a chart must never move it. It is returned so
                -- the mapper stays whole, and it is set only by set_item_folder.
                RETURNING id, slug, title, description, source_db, sql, chart_type,
                          x_column, y_columns, created_by, folder_id,
                          created_at, updated_at
                """
            ),
            params,
        ).first()
    return _row_to_chart(row)


def set_user_locale(username: str, locale: str | None) -> bool:
    """Remember someone's interface language."""
    with engine().begin() as conn:
        result = conn.execute(
            text("UPDATE users SET locale = :l WHERE username = :u"),
            {"l": locale, "u": username},
        )
        return result.rowcount > 0


def get_user_locale(username: str) -> str | None:
    """Their stored language, or None to mean "use the server default"."""
    with engine().connect() as conn:
        return conn.execute(
            text("SELECT locale FROM users WHERE username = :u"), {"u": username}
        ).scalar()


def slug_for(table: str, ident: str) -> str | None:
    """Resolve an id-or-slug to a slug.

    URLs address charts and dashboards by **id**, because a slug is derived from
    the title and a rename would break every link and bookmark pointing at it.
    Everything below this line still works in slugs — permissions in particular,
    where a grant reading `capturas-por-dia` is far easier to audit than one
    reading `417`.

    A slug is still accepted so links shared before the switch keep resolving.
    """
    ident = str(ident or "")
    if not ident.isdigit():
        return ident or None
    if table not in ("charts", "dashboards", "reports"):
        raise ValueError(f"not a slugged table: {table!r}")
    with engine().connect() as conn:
        return conn.execute(
            text(f"SELECT slug FROM {table} WHERE id = :i"),  # noqa: S608 — fixed set
            {"i": int(ident)},
        ).scalar()


def id_for(table: str, slug: str) -> int | None:
    """The id behind a slug — the inverse of slug_for().

    Used when a POST redirects back to a page: the destination should be the
    canonical id URL, not the slug the handler happened to be working in.
    """
    if table not in ("charts", "dashboards", "reports"):
        raise ValueError(f"not a slugged table: {table!r}")
    with engine().connect() as conn:
        return conn.execute(
            text(f"SELECT id FROM {table} WHERE slug = :s"),  # noqa: S608 — fixed set
            {"s": slug},
        ).scalar()


def delete_chart(slug: str) -> bool:
    with engine().begin() as conn:
        result = conn.execute(text("DELETE FROM charts WHERE slug = :s"), {"s": slug})
        return result.rowcount > 0


# ── dashboards ─────────────────────────────────────────────────────────────

# Grid spans a tile may occupy. Text in the database rather than an enum, so
# adding one is a code change instead of a migration.
WIDTHS = ("third", "half", "full")
DEFAULT_WIDTH = "half"


# The grid every dashboard is laid out on. Twelve columns because that divides
# cleanly into halves, thirds and quarters — and because Superset uses twelve,
# so an imported layout maps across without rescaling.
GRID_COLUMNS = 12
# Superset's own unit, so an imported height needs no conversion: meta.height
# lands in grid_h as it stands. It used to be 56px with the Superset value
# divided by seven, which distorted 75 of the 88 distinct heights in the V360
# instance — a dashboard that merely resembled the original.
ROW_HEIGHT_PX = 8  # one grid_h unit
GRID_GUTTER_PX = 16  # between columns, and the gap stacked tiles leave
DEFAULT_TILE = (6, 50)  # w, h — half width, a readable chart height


@dataclass
class DashboardItem:
    """One tile: a chart, or a block of text, placed on the dashboard grid.

    `chart` is None for a text tile — the two are one table because they share
    ordering, geometry and a tab, and splitting them would mean merging two
    ordered lists on every render.

    Geometry is nullable. A tile that has never been placed falls back to the
    old `width` flow, so dashboards built before the grid keep working.
    """

    id: int
    position: int
    chart: Chart | None = None
    width: str = DEFAULT_WIDTH
    kind: str = "chart"  # chart | text | divider
    content: str = ""
    section_id: int | None = None
    grid_x: int | None = None
    grid_y: int | None = None
    grid_w: int | None = None
    grid_h: int | None = None
    # A dashboard may rename a chart for its own purposes — Superset calls it
    # sliceNameOverride and 214 tiles in the V360 instance use one. Empty means
    # "show the chart's own name".
    title_override: str = ""

    @property
    def is_text(self) -> bool:
        return self.kind == "text"

    @property
    def is_divider(self) -> bool:
        return self.kind == "divider"

    @property
    def display_title(self) -> str:
        """What this tile is called *here*."""
        return self.title_override or (self.chart.title if self.chart else "")

    @property
    def placed(self) -> bool:
        return self.grid_x is not None and self.grid_y is not None

    @property
    def span(self) -> int:
        """Columns this tile occupies, falling back to the old width names."""
        if self.grid_w:
            return max(1, min(GRID_COLUMNS, self.grid_w))
        return {"third": 4, "half": 6, "full": 12}.get(self.width, 6)

    @property
    def height_px(self) -> int:
        if self.is_divider:
            return 2 * ROW_HEIGHT_PX
        units = self.grid_h or (14 if self.is_text else DEFAULT_TILE[1])
        return max(1, units) * ROW_HEIGHT_PX


@dataclass
class DashboardSection:
    """A tab. Tiles with a matching section_id belong to it."""

    id: int
    title: str
    position: int = 0
    items: list[DashboardItem] = field(default_factory=list)
    # The URL fragment that opens this tab. Derived from the title rather than
    # from `id`, which is a bigserial: re-importing a dashboard renumbers every
    # tab, so a link somebody saved would quietly open a different one. Filled
    # in by get_dashboard(), which is the only place that can see the siblings
    # a name has to stay unique against.
    anchor: str = ""


@dataclass
class Dashboard:
    slug: str
    title: str
    description: str = ""
    created_by: str = ""
    id: int | None = None
    items: list[DashboardItem] = field(default_factory=list)
    sections: list[DashboardSection] = field(default_factory=list)
    folder_id: int | None = None  # display grouping only
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def loose_items(self) -> list[DashboardItem]:
        """Tiles outside every tab — they render above the tab strip."""
        return [i for i in self.items if i.section_id is None]

    @property
    def has_tabs(self) -> bool:
        return bool(self.sections)


_DASH_SELECT = (
    "SELECT id, slug, title, description, created_by, folder_id, "
    "created_at, updated_at FROM dashboards"
)


def _row_to_dashboard(row: Any) -> Dashboard:
    m = row._mapping
    return Dashboard(
        id=m["id"],
        slug=m["slug"],
        title=m["title"],
        description=m["description"],
        created_by=m["created_by"],
        folder_id=m["folder_id"],
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
        # LEFT JOIN, not JOIN: a text tile has no chart and must still come back.
        items = conn.execute(
            text(
                """
                SELECT i.id, i.position, i.width, i.kind, i.content, i.section_id,
                       i.grid_x, i.grid_y, i.grid_w, i.grid_h, i.title_override,
                       c.id AS c_id, c.slug, c.title, c.description, c.source_db,
                       c.sql, c.chart_type, c.x_column, c.y_columns, c.created_by,
                       c.created_at, c.updated_at
                FROM dashboard_items i
                LEFT JOIN charts c ON c.id = i.chart_id
                WHERE i.dashboard_id = :id
                -- Placed tiles first, in grid order; the rest keep their old
                -- flow order underneath.
                ORDER BY coalesce(i.grid_y, 9999), coalesce(i.grid_x, 9999),
                         i.position, i.id
                """
            ),
            {"id": dash.id},
        ).fetchall()
        sections = conn.execute(
            text(
                "SELECT id, title, position FROM dashboard_sections "
                "WHERE dashboard_id = :id ORDER BY position, id"
            ),
            {"id": dash.id},
        ).fetchall()

    for r in items:
        m = r._mapping
        chart = None
        if m["c_id"] is not None:
            y = m["y_columns"]
            if isinstance(y, str):
                y = json.loads(y)
            chart = Chart(
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
            )
        dash.items.append(
            DashboardItem(
                id=m["id"],
                position=m["position"],
                width=m["width"] if m["width"] in WIDTHS else DEFAULT_WIDTH,
                kind=m["kind"] or "chart",
                content=m["content"] or "",
                section_id=m["section_id"],
                grid_x=m["grid_x"],
                grid_y=m["grid_y"],
                grid_w=m["grid_w"],
                grid_h=m["grid_h"],
                title_override=m["title_override"] or "",
                chart=chart,
            )
        )

    # Tiles are attached to their tab here rather than in a second query per
    # section: the whole layout arrives in the two reads above.
    by_section: dict[int, list[DashboardItem]] = {}
    for item in dash.items:
        if item.section_id is not None:
            by_section.setdefault(item.section_id, []).append(item)
    dash.sections = [
        DashboardSection(
            id=s._mapping["id"],
            title=s._mapping["title"],
            position=s._mapping["position"],
            items=by_section.get(s._mapping["id"], []),
        )
        for s in sections
    ]

    name_tab_anchors(dash.sections)
    return dash


def name_tab_anchors(sections: list[DashboardSection]) -> None:
    """Give each tab the URL fragment that opens it, in place.

    From the title, and unique within the dashboard. Two tabs really can share
    a name — Superset allows it — so the second gets a suffix, the way
    unique_slug() handles charts.
    """
    used: dict[str, int] = {}
    for index, section in enumerate(sections, 1):
        # Accents are folded rather than stripped: these titles are Portuguese,
        # and slugify()'s rule turns "Recém-Tombados" into "rec-m-tombados" and
        # "Operação" into "opera-o". An anchor is read by people.
        #
        # slugify() itself keeps the old rule on purpose — its output is a
        # stored key that permission and tag rows point at, so changing it
        # would rename every chart and orphan those rows. An anchor is computed
        # at render time and belongs to nobody, so it can just be better.
        folded = unicodedata.normalize("NFKD", section.title.strip().lower())
        folded = "".join(c for c in folded if not unicodedata.combining(c))
        # Not slugify() either: its empty-title fallback is the literal "chart",
        # which would be a lie on a tab. A title with nothing sluggable in it —
        # an emoji, say — falls back to its position instead.
        base = _SLUG_STRIP.sub("-", folded).strip("-") or f"tab-{index}"
        used[base] = used.get(base, 0) + 1
        section.anchor = base if used[base] == 1 else f"{base}-{used[base]}"


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
                -- folder_id deliberately untouched; see save_chart().
                RETURNING id, slug, title, description, created_by, folder_id,
                          created_at, updated_at
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


def add_section(dashboard_slug: str, title: str) -> int | None:
    """Append a tab. Returns its id, or None if the dashboard is gone."""
    title = (title or "").strip()
    if not title:
        return None
    with engine().begin() as conn:
        dash_id = conn.execute(
            text("SELECT id FROM dashboards WHERE slug = :s"), {"s": dashboard_slug}
        ).scalar()
        if dash_id is None:
            return None
        position = conn.execute(
            text(
                "SELECT coalesce(max(position), -1) + 1 FROM dashboard_sections "
                "WHERE dashboard_id = :id"
            ),
            {"id": dash_id},
        ).scalar()
        new_id = conn.execute(
            text(
                "INSERT INTO dashboard_sections (dashboard_id, title, position) "
                "VALUES (:d, :t, :p) RETURNING id"
            ),
            {"d": dash_id, "t": title[:120], "p": position},
        ).scalar()
        _touch(conn, dash_id)
        return new_id


def delete_section(dashboard_slug: str, section_id: int) -> bool:
    """Drop a tab. Its tiles survive and fall back to the untabbed area."""
    with engine().begin() as conn:
        dash_id = conn.execute(
            text("SELECT id FROM dashboards WHERE slug = :s"), {"s": dashboard_slug}
        ).scalar()
        if dash_id is None:
            return False
        result = conn.execute(
            text("DELETE FROM dashboard_sections WHERE id = :i AND dashboard_id = :d"),
            {"i": section_id, "d": dash_id},
        )
        _touch(conn, dash_id)
        return result.rowcount > 0


def add_text_item(
    dashboard_slug: str,
    content: str,
    section_id: int | None = None,
    width: str = "full",
    kind: str = "text",
) -> int | None:
    """Append a chartless tile — a heading, a note, or a rule between charts.

    Returns the new tile's id, so an importer can place it on the grid.
    """
    if width not in WIDTHS:
        width = "full"
    if kind not in ("text", "divider"):
        kind = "text"
    with engine().begin() as conn:
        dash_id = conn.execute(
            text("SELECT id FROM dashboards WHERE slug = :s"), {"s": dashboard_slug}
        ).scalar()
        if dash_id is None:
            return None
        position = conn.execute(
            text(
                "SELECT coalesce(max(position), -1) + 1 FROM dashboard_items "
                "WHERE dashboard_id = :id"
            ),
            {"id": dash_id},
        ).scalar()
        new_id = conn.execute(
            text(
                "INSERT INTO dashboard_items "
                "(dashboard_id, chart_id, position, width, kind, content, section_id) "
                "VALUES (:d, NULL, :p, :w, :k, :c, :s) RETURNING id"
            ),
            {"d": dash_id, "p": position, "w": width, "c": content, "s": section_id, "k": kind},
        ).scalar()
        _touch(conn, dash_id)
        return new_id


def save_layout(dashboard_slug: str, placements: list[dict]) -> int:
    """Write the grid geometry for a whole dashboard in one go.

    `placements` is [{id, x, y, w, h, section_id}, ...] straight from the
    editor. One transaction: a half-applied layout would leave tiles on top of
    each other. Ids that don't belong to this dashboard are ignored rather than
    trusted, since they arrive from the browser.
    """
    if not placements:
        return 0
    with engine().begin() as conn:
        dash_id = conn.execute(
            text("SELECT id FROM dashboards WHERE slug = :s"), {"s": dashboard_slug}
        ).scalar()
        if dash_id is None:
            return 0
        mine = {
            r[0]
            for r in conn.execute(
                text("SELECT id FROM dashboard_items WHERE dashboard_id = :d"), {"d": dash_id}
            )
        }
        sections = {
            r[0]
            for r in conn.execute(
                text("SELECT id FROM dashboard_sections WHERE dashboard_id = :d"), {"d": dash_id}
            )
        }
        written = 0
        for order, p in enumerate(placements):
            try:
                item_id = int(p["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if item_id not in mine:
                continue
            section_id = p.get("section_id")
            section_id = int(section_id) if str(section_id or "").isdigit() else None
            if section_id is not None and section_id not in sections:
                section_id = None
            conn.execute(
                text(
                    "UPDATE dashboard_items SET grid_x = :x, grid_y = :y, grid_w = :w, "
                    "grid_h = :h, position = :p, section_id = :s WHERE id = :i"
                ),
                {
                    "x": max(0, min(GRID_COLUMNS - 1, int(p.get("x", 0) or 0))),
                    "y": max(0, int(p.get("y", 0) or 0)),
                    "w": max(1, min(GRID_COLUMNS, int(p.get("w", 6) or 6))),
                    "h": max(1, int(p.get("h", 5) or 5)),
                    "p": order,
                    "s": section_id,
                    "i": item_id,
                },
            )
            written += 1
        _touch(conn, dash_id)
        return written


def set_item_section(dashboard_slug: str, item_id: int, section_id: int | None) -> bool:
    """Move one tile onto a tab, or out of every tab with None."""
    with engine().begin() as conn:
        dash_id = conn.execute(
            text("SELECT id FROM dashboards WHERE slug = :s"), {"s": dashboard_slug}
        ).scalar()
        if dash_id is None:
            return False
        if section_id is not None:
            owned = conn.execute(
                text("SELECT 1 FROM dashboard_sections WHERE id = :i AND dashboard_id = :d"),
                {"i": section_id, "d": dash_id},
            ).first()
            if owned is None:
                return False  # never file a tile onto another dashboard's tab
        result = conn.execute(
            text("UPDATE dashboard_items SET section_id = :s WHERE id = :i AND dashboard_id = :d"),
            {"s": section_id, "i": item_id, "d": dash_id},
        )
        _touch(conn, dash_id)
        return result.rowcount > 0


def add_item(
    dashboard_slug: str,
    chart_slug: str,
    width: str = DEFAULT_WIDTH,
    section_id: int | None = None,
    title_override: str = "",
) -> int | None:
    """Append a chart to a dashboard. None if either no longer exists.

    Returns the new tile's id so an importer can place it on the grid.

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
            return None
        next_pos = conn.execute(
            text(
                "SELECT coalesce(max(position), -1) + 1 FROM dashboard_items "
                "WHERE dashboard_id = :id"
            ),
            {"id": dash_id},
        ).scalar()
        new_id = conn.execute(
            text(
                "INSERT INTO dashboard_items "
                "(dashboard_id, chart_id, position, width, kind, section_id, title_override) "
                "VALUES (:d, :c, :p, :w, 'chart', :s, :t) RETURNING id"
            ),
            {
                "d": dash_id,
                "c": chart_id,
                "p": next_pos,
                "w": width,
                "s": section_id,
                "t": title_override,
            },
        ).scalar()
        _touch(conn, dash_id)
    return new_id


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


# ── users, roles, permissions ──────────────────────────────────────────────

# A permission is (resource_type, resource_key). ANY as the key means "every
# resource of this type"; ANY as the type as well means "everything", which is
# what the seeded Admin role holds.
ANY = "*"

DATABASE = "database"
DATASET = "dataset"
REPORT = "report"
CHART = "chart"
DASHBOARD = "dashboard"
FEATURE = "feature"

RESOURCE_TYPES = (DATABASE, DATASET, REPORT, CHART, DASHBOARD, FEATURE)

# Capabilities that aren't a stored object. Adding one here is all it takes to
# make it grantable — no migration, since permissions are just (type, key).
FEATURES: tuple[tuple[str, str], ...] = (
    ("sql_console", "Run ad-hoc SQL in the query console"),
    ("chart_builder", "Create and edit charts"),
    ("dashboard_builder", "Create and edit dashboards"),
    ("report_builder", "Create and edit reports from SQL"),
    ("dataset_catalog", "Browse the dataset catalog"),
)
FEATURE_KEYS = tuple(k for k, _ in FEATURES)

RESOURCE_LABELS = {
    DATABASE: "Databases",
    DATASET: "Datasets",
    REPORT: "Reports",
    CHART: "Charts",
    DASHBOARD: "Dashboards",
    FEATURE: "Features",
}


@dataclass
class Role:
    name: str
    description: str = ""
    is_default: bool = False
    id: int | None = None
    # resource_type -> keys granted (may contain ANY)
    permissions: dict[str, list[str]] = field(default_factory=dict)
    member_count: int = 0

    def keys(self, resource_type: str) -> list[str]:
        return self.permissions.get(resource_type, [])

    def grants_everything(self) -> bool:
        return ANY in self.permissions.get(ANY, [])


@dataclass
class User:
    username: str
    email: str = ""
    display_name: str = ""
    is_admin: bool = False
    is_active: bool = True
    auth_via: str = ""
    id: int | None = None
    roles: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    last_seen_at: datetime | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.username


def _row_to_user(row: Any) -> User:
    m = row._mapping
    return User(
        id=m["id"],
        username=m["username"],
        email=m["email"],
        display_name=m["display_name"],
        is_admin=m["is_admin"],
        is_active=m["is_active"],
        auth_via=m["auth_via"],
        created_at=m["created_at"],
        last_seen_at=m["last_seen_at"],
    )


_USER_SELECT = (
    "SELECT id, username, email, display_name, is_admin, is_active, auth_via, "
    "created_at, last_seen_at FROM users"
)


def upsert_user(username: str, email: str = "", display_name: str = "", auth_via: str = "") -> User:
    """Record a login, creating the user on first sight.

    Self-registration, as Superset does it. A brand-new user is given every
    role marked `is_default`; an existing user's roles are never touched here,
    so an admin's changes are not undone by the next login.
    """
    with engine().begin() as conn:
        existing = conn.execute(
            text("SELECT id FROM users WHERE username = :u"), {"u": username}
        ).scalar()

        row = conn.execute(
            text(
                """
                INSERT INTO users (username, email, display_name, auth_via, last_seen_at)
                VALUES (:u, :e, :d, :a, now())
                ON CONFLICT (username) DO UPDATE SET
                    -- Only overwrite from the IdP when it actually told us
                    -- something; a password login supplies neither.
                    email        = COALESCE(NULLIF(EXCLUDED.email, ''), users.email),
                    display_name = COALESCE(NULLIF(EXCLUDED.display_name, ''), users.display_name),
                    auth_via     = EXCLUDED.auth_via,
                    last_seen_at = now()
                RETURNING id, username, email, display_name, is_admin, is_active,
                          auth_via, created_at, last_seen_at
                """
            ),
            {"u": username, "e": email, "d": display_name, "a": auth_via},
        ).first()

        if existing is None:
            conn.execute(
                text(
                    "INSERT INTO user_roles (user_id, role_id) "
                    "SELECT :uid, id FROM roles WHERE is_default "
                    "ON CONFLICT DO NOTHING"
                ),
                {"uid": row._mapping["id"]},
            )
            logger.info("Registered new user %r via %s", username, auth_via)

    user = _row_to_user(row)
    user.roles = _roles_for(user.id)
    return user


def _roles_for(user_id: int) -> list[str]:
    with engine().connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT r.name FROM roles r JOIN user_roles ur ON ur.role_id = r.id "
                    "WHERE ur.user_id = :id ORDER BY r.name"
                ),
                {"id": user_id},
            )
        ]


def get_user(username: str) -> User | None:
    with engine().connect() as conn:
        row = conn.execute(text(f"{_USER_SELECT} WHERE username = :u"), {"u": username}).first()
    if row is None:
        return None
    user = _row_to_user(row)
    user.roles = _roles_for(user.id)
    return user


def is_admin(username: str) -> bool:
    """Checked live rather than trusted from the session, so revoking admin
    takes effect immediately instead of at the user's next login."""
    with engine().connect() as conn:
        return bool(
            conn.execute(
                text("SELECT is_admin FROM users WHERE username = :u AND is_active"),
                {"u": username},
            ).scalar()
        )


def list_users() -> list[User]:
    with engine().connect() as conn:
        rows = conn.execute(text(f"{_USER_SELECT} ORDER BY username")).fetchall()
        grants = conn.execute(
            text(
                "SELECT ur.user_id, r.name FROM user_roles ur "
                "JOIN roles r ON r.id = ur.role_id ORDER BY r.name"
            )
        ).fetchall()
    by_user: dict[int, list[str]] = {}
    for uid, name in grants:
        by_user.setdefault(uid, []).append(name)
    users_out = []
    for row in rows:
        u = _row_to_user(row)
        u.roles = by_user.get(u.id, [])
        users_out.append(u)
    return users_out


def set_user_admin(username: str, value: bool) -> bool:
    with engine().begin() as conn:
        result = conn.execute(
            text("UPDATE users SET is_admin = :v WHERE username = :u"),
            {"v": value, "u": username},
        )
        return result.rowcount > 0


def set_user_active(username: str, value: bool) -> bool:
    with engine().begin() as conn:
        result = conn.execute(
            text("UPDATE users SET is_active = :v WHERE username = :u"),
            {"v": value, "u": username},
        )
        return result.rowcount > 0


def set_user_roles(username: str, role_names: list[str]) -> bool:
    """Replace a user's roles wholesale — the admin form posts the full set."""
    with engine().begin() as conn:
        uid = conn.execute(
            text("SELECT id FROM users WHERE username = :u"), {"u": username}
        ).scalar()
        if uid is None:
            return False
        conn.execute(text("DELETE FROM user_roles WHERE user_id = :id"), {"id": uid})
        if role_names:
            conn.execute(
                text(
                    "INSERT INTO user_roles (user_id, role_id) "
                    "SELECT :id, id FROM roles WHERE name = ANY(:names) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"id": uid, "names": list(role_names)},
            )
    return True


def list_roles() -> list[Role]:
    with engine().connect() as conn:
        rows = conn.execute(
            text("SELECT id, name, description, is_default FROM roles ORDER BY name")
        ).fetchall()
        perms = conn.execute(
            text(
                "SELECT role_id, resource_type, resource_key FROM role_permissions "
                "ORDER BY resource_type, resource_key"
            )
        ).fetchall()
        counts = dict(
            conn.execute(text("SELECT role_id, count(*) FROM user_roles GROUP BY 1")).fetchall()
        )
    by_role: dict[int, dict[str, list[str]]] = {}
    for rid, rtype, rkey in perms:
        by_role.setdefault(rid, {}).setdefault(rtype, []).append(rkey)
    out = []
    for r in rows:
        m = r._mapping
        out.append(
            Role(
                id=m["id"],
                name=m["name"],
                description=m["description"],
                is_default=m["is_default"],
                permissions=by_role.get(m["id"], {}),
                member_count=counts.get(m["id"], 0),
            )
        )
    return out


def create_role(name: str, description: str = "", is_default: bool = False) -> Role | None:
    name = name.strip()
    if not name:
        return None
    with engine().begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO roles (name, description, is_default) "
                "VALUES (:n, :d, :f) ON CONFLICT (name) DO NOTHING "
                "RETURNING id, name, description, is_default"
            ),
            {"n": name, "d": description, "f": is_default},
        ).first()
    if row is None:
        return None
    m = row._mapping
    return Role(
        id=m["id"], name=m["name"], description=m["description"], is_default=m["is_default"]
    )


def delete_role(role_id: int) -> bool:
    with engine().begin() as conn:
        result = conn.execute(text("DELETE FROM roles WHERE id = :id"), {"id": role_id})
        return result.rowcount > 0


def set_role_default(role_id: int, value: bool) -> bool:
    with engine().begin() as conn:
        result = conn.execute(
            text("UPDATE roles SET is_default = :v WHERE id = :id"),
            {"v": value, "id": role_id},
        )
        return result.rowcount > 0


def set_role_permissions(role_id: int, resource_type: str, keys: list[str]) -> bool:
    """Replace one resource type's grants for a role.

    Scoped to a single type on purpose: the admin form posts one section at a
    time, so saving the Databases section can never wipe the Charts section.
    """
    if resource_type not in RESOURCE_TYPES:
        return False
    cleaned = sorted({k.strip() for k in keys if k and k.strip()})
    with engine().begin() as conn:
        exists = conn.execute(text("SELECT 1 FROM roles WHERE id = :id"), {"id": role_id}).scalar()
        if not exists:
            return False
        conn.execute(
            text("DELETE FROM role_permissions WHERE role_id = :id AND resource_type = :t"),
            {"id": role_id, "t": resource_type},
        )
        for key in cleaned:
            conn.execute(
                text(
                    "INSERT INTO role_permissions (role_id, resource_type, resource_key) "
                    "VALUES (:id, :t, :k) ON CONFLICT DO NOTHING"
                ),
                {"id": role_id, "t": resource_type, "k": key},
            )
    return True


@dataclass
class Access:
    """What one user may reach — resolved once per request, then asked.

    `everything` short-circuits every check: it's true for a user flagged admin
    and for anyone holding a role with the ('*','*') grant.
    """

    username: str
    everything: bool = False
    granted: dict[str, set[str]] = field(default_factory=dict)

    def allows(self, resource_type: str, key: str) -> bool:
        if self.everything:
            return True
        keys = self.granted.get(resource_type, set())
        return ANY in keys or key in keys

    def keys(self, resource_type: str) -> set[str]:
        """Explicit keys for a type. Meaningless when `everything` is set or the
        type is granted with ANY — callers filter a known list instead."""
        return self.granted.get(resource_type, set())

    def has_any(self, resource_type: str) -> bool:
        return self.everything or bool(self.granted.get(resource_type))

    def filter(self, resource_type: str, candidates):
        """Narrow a list of real resources to the ones this user may see."""
        if self.everything or ANY in self.granted.get(resource_type, set()):
            return list(candidates)
        keys = self.granted.get(resource_type, set())
        return [c for c in candidates if c in keys]


# Nobody: what an unknown, inactive or un-roled user gets. Fail closed.
NO_ACCESS = Access(username="", everything=False, granted={})


def access_for(username: str) -> Access:
    """Resolve a user's effective permissions: the union of their roles'.

    One query. Callers hold the result for the request rather than asking per
    check, so a page with several checks doesn't mean several round trips.
    """
    with engine().connect() as conn:
        row = conn.execute(
            text("SELECT id, is_admin, is_active FROM users WHERE username = :u"),
            {"u": username},
        ).first()
        if row is None or not row._mapping["is_active"]:
            return Access(username=username)
        if row._mapping["is_admin"]:
            return Access(username=username, everything=True)

        granted: dict[str, set[str]] = {}
        rows = conn.execute(
            text(
                "SELECT DISTINCT rp.resource_type, rp.resource_key FROM role_permissions rp "
                "JOIN user_roles ur ON ur.role_id = rp.role_id "
                "WHERE ur.user_id = :id"
            ),
            {"id": row._mapping["id"]},
        )
        for rtype, rkey in rows:
            granted.setdefault(rtype, set()).add(rkey)

        # A role holding ('*','*') grants everything, same as the admin flag.
        if ANY in granted.get(ANY, set()):
            return Access(username=username, everything=True)

    return Access(username=username, granted=granted)


# ── tags ───────────────────────────────────────────────────────────────────
#
# Labels for finding things. Deliberately outside the permission model — see
# migration 0012 — so tagging a chart never changes who can see it.

TAGGABLE = (CHART, DASHBOARD)


@dataclass
class Tag:
    name: str
    slug: str
    id: int | None = None
    count: int = 0  # how many things carry it, for the filter bar
    # Split by kind, so the management screen can say what a delete will strip
    # before it strips it. Only filled in by list_tags() over every type.
    chart_count: int = 0
    dashboard_count: int = 0


def list_tags(resource_type: str | None = None) -> list[Tag]:
    """Every tag, with a usage count. Ordered by use, then name.

    Narrowed to a resource type, a tag nothing carries drops out — the WHERE
    lands after the join, so the filter bar on a listing never offers a chip
    that would filter to nothing. Called with no type, every tag comes back
    including the unused ones, which is what the management screen wants.
    """
    where = "WHERE rt.resource_type = :t" if resource_type else ""
    params = {"chart": CHART, "dash": DASHBOARD}
    if resource_type:
        params["t"] = resource_type
    with engine().connect() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT g.id, g.name, g.slug, count(rt.tag_id) AS n,
                       count(*) FILTER (WHERE rt.resource_type = :chart)  AS charts,
                       count(*) FILTER (WHERE rt.resource_type = :dash)   AS dashboards
                FROM tags g
                LEFT JOIN resource_tags rt ON rt.tag_id = g.id {where}
                GROUP BY g.id, g.name, g.slug
                ORDER BY n DESC, lower(g.name)
                """  # noqa: S608 — `where` is a fixed literal, not input
            ),
            params,
        ).fetchall()
    return [
        Tag(id=r[0], name=r[1], slug=r[2], count=r[3], chart_count=r[4], dashboard_count=r[5])
        for r in rows
    ]


def create_tag(name: str, created_by: str = "") -> Tag | None:
    """A tag with nothing on it yet. None when the name is empty or taken.

    Tags also come into being by being typed onto a chart; this is for setting
    up the vocabulary first, so people pick from words the team agreed on
    rather than inventing a fourth spelling of "financeiro".
    """
    name = " ".join(str(name or "").split())[:60]
    slug = slugify(name)
    if not name or not slug:
        return None
    with engine().begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO tags (name, slug, created_by) VALUES (:n, :s, :c) "
                "ON CONFLICT (slug) DO NOTHING RETURNING id, name, slug"
            ),
            {"n": name, "s": slug, "c": created_by},
        ).first()
    return None if row is None else Tag(id=row[0], name=row[1], slug=row[2])


def delete_tag(slug: str) -> bool:
    """Delete a tag and take it off everything carrying it.

    resource_tags cascades on the foreign key, so the taggings go with it. That
    is the whole blast radius: a tag was never part of who-can-see-what, so no
    chart, dashboard or role changes hands here — they just lose a label.
    """
    with engine().begin() as conn:
        deleted = conn.execute(text("DELETE FROM tags WHERE slug = :s"), {"s": slug}).rowcount
    return bool(deleted)


def tags_for(resource_type: str, keys: list[str]) -> dict[str, list[Tag]]:
    """key -> its tags, for a whole listing in one query."""
    if not keys:
        return {}
    with engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT rt.resource_key, g.id, g.name, g.slug FROM resource_tags rt "
                "JOIN tags g ON g.id = rt.tag_id "
                "WHERE rt.resource_type = :t AND rt.resource_key = ANY(:keys) "
                "ORDER BY lower(g.name)"
            ),
            {"t": resource_type, "keys": list(keys)},
        ).fetchall()
    out: dict[str, list[Tag]] = {}
    for key, tag_id, name, slug in rows:
        out.setdefault(key, []).append(Tag(id=tag_id, name=name, slug=slug))
    return out


def keys_with_tag(resource_type: str, tag_slug: str) -> list[str]:
    """Which resources carry a tag — the filter on a listing page."""
    with engine().connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT rt.resource_key FROM resource_tags rt "
                    "JOIN tags g ON g.id = rt.tag_id "
                    "WHERE rt.resource_type = :t AND g.slug = :s"
                ),
                {"t": resource_type, "s": tag_slug},
            )
        ]


def set_tags(resource_type: str, key: str, names: list[str]) -> bool:
    """Replace a resource's tags, creating any that are new.

    Attaches tags that already exist and ignores the rest. Creating one is a
    separate, admin-only act (create_tag, /admin/tags): the vocabulary is
    supposed to be a short list everyone shares, and a field that invents a tag
    on submit is how you end up with "financeiro", "Financeiro " and
    "finaceiro" meaning the same thing and finding different sets.

    Still takes names rather than ids because that is what the form posts, and
    matching is by slug — so case and spacing don't have to be exact.
    """
    if resource_type not in TAGGABLE:
        return False
    wanted: list[str] = []
    seen: set[str] = set()
    for raw in names:
        slug = slugify(" ".join(str(raw or "").split())[:60])
        if slug and slug not in seen:
            seen.add(slug)
            wanted.append(slug)

    with engine().begin() as conn:
        tag_ids = []
        if wanted:
            found = {
                r[0]: r[1]
                for r in conn.execute(
                    text("SELECT slug, id FROM tags WHERE slug = ANY(:s)"), {"s": wanted}
                )
            }
            # Order follows what was submitted, not what the database returns.
            tag_ids = [found[s] for s in wanted if s in found]
            unknown = [s for s in wanted if s not in found]
            if unknown:
                logger.info(
                    "Ignored unknown tag(s) %r on %s %r — tags are created in admin",
                    unknown,
                    resource_type,
                    key,
                )

        conn.execute(
            text("DELETE FROM resource_tags WHERE resource_type = :t AND resource_key = :k"),
            {"t": resource_type, "k": key},
        )
        for tag_id in tag_ids:
            conn.execute(
                text(
                    "INSERT INTO resource_tags (tag_id, resource_type, resource_key) "
                    "VALUES (:g, :t, :k) ON CONFLICT DO NOTHING"
                ),
                {"g": tag_id, "t": resource_type, "k": key},
            )
        # Untagging the last chart used to delete the tag itself, on the
        # grounds that an unused tag is noise. It can't now: a tag created on
        # the management screen starts with nothing on it, and this runs on
        # every tag edit anywhere — so the sweep would delete the vocabulary
        # somebody had just sat down and defined. Unused tags are removed
        # deliberately, by delete_tag(), and they stay out of the filter bars
        # on their own because list_tags(resource_type) doesn't return them.
    return True


# ── cached chart previews ──────────────────────────────────────────────────
#
# The rendered spec of a chart, kept so the Charts listing doesn't re-run every
# query on every visit.
#
# The spec, not an image: Chart.js draws in the browser, so a picture would mean
# running a headless browser server-side and would lose the tooltips and the
# legend. This is a few KB of labels and series that the same JS draws exactly
# as it draws a live one.
#
# Always served with its age. A chart's numbers move when the *data* moves, not
# when someone edits the chart, so a snapshot taken at save time would quietly
# show last month's figures as if they were today's.


def first_chart_of(dashboard_slugs: list[str]) -> dict[str, int]:
    """dashboard slug -> the id of its first chart, for the listing preview.

    One query for the whole page. "First" is the tile that renders first: grid
    order when the dashboard has been laid out, insertion order otherwise —
    which is the chart somebody recognises the dashboard by.
    """
    if not dashboard_slugs:
        return {}
    with engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT ON (d.slug) d.slug, i.chart_id
                FROM dashboards d
                JOIN dashboard_items i ON i.dashboard_id = d.id
                WHERE d.slug = ANY(:slugs) AND i.chart_id IS NOT NULL
                ORDER BY d.slug,
                         coalesce(i.grid_y, 9999), coalesce(i.grid_x, 9999),
                         i.position, i.id
                """
            ),
            {"slugs": list(dashboard_slugs)},
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def get_filter_options(dashboard_slug: str) -> dict[str, tuple[list, datetime]]:
    """Cached option lists for a dashboard's filters, keyed by filter key."""
    with engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT filter_key, options, built_at FROM dashboard_filter_options "
                "WHERE dashboard_slug = :s"
            ),
            {"s": dashboard_slug},
        ).fetchall()
    out: dict[str, tuple[list, datetime]] = {}
    for key, options, built_at in rows:
        if isinstance(options, str):
            options = json.loads(options)
        out[key] = (list(options or []), built_at)
    return out


def put_filter_options(dashboard_slug: str, filter_key: str, options: list) -> None:
    with engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO dashboard_filter_options "
                "(dashboard_slug, filter_key, options, built_at) "
                "VALUES (:s, :k, cast(:o AS jsonb), now()) "
                "ON CONFLICT (dashboard_slug, filter_key) DO UPDATE "
                "SET options = excluded.options, built_at = excluded.built_at"
            ),
            {"s": dashboard_slug, "k": filter_key, "o": json.dumps(list(options))},
        )


def get_tile_cache(item_id: int, sig: str) -> tuple[dict, datetime] | None:
    """A tile's last rendered payload for this exact query, if we kept one."""
    with engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT payload, built_at FROM dashboard_tile_cache "
                "WHERE item_id = :i AND sig = :s"
            ),
            {"i": item_id, "s": sig},
        ).first()
    if row is None:
        return None
    payload = row[0]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload, row[1]


def put_tile_cache(item_id: int, sig: str, payload: dict) -> None:
    with engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO dashboard_tile_cache (item_id, sig, payload, built_at) "
                "VALUES (:i, :s, cast(:p AS jsonb), now()) "
                "ON CONFLICT (item_id, sig) DO UPDATE "
                "SET payload = excluded.payload, built_at = excluded.built_at"
            ),
            {"i": item_id, "s": sig, "p": json.dumps(payload)},
        )


def drop_tile_cache(item_id: int, sig: str | None = None) -> None:
    """Forget a tile's cached answer — one query's, or all of them."""
    sql = "DELETE FROM dashboard_tile_cache WHERE item_id = :i"
    params: dict = {"i": item_id}
    if sig is not None:
        sql += " AND sig = :s"
        params["s"] = sig
    with engine().begin() as conn:
        conn.execute(text(sql), params)


def get_chart_preview(chart_id: int, variant: str = "preview") -> tuple[dict, datetime] | None:
    """A chart's cached result. `variant` picks the thumbnail or the full one."""
    with engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT spec, built_at FROM chart_previews "
                "WHERE chart_id = :c AND variant = :v"
            ),
            {"c": chart_id, "v": variant},
        ).first()
    if row is None:
        return None
    spec = row[0]
    if isinstance(spec, str):
        spec = json.loads(spec)
    return spec, row[1]


def put_chart_preview(chart_id: int, spec: dict, variant: str = "preview") -> None:
    with engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO chart_previews (chart_id, variant, spec, built_at) "
                "VALUES (:c, :v, cast(:s AS jsonb), now()) "
                "ON CONFLICT (chart_id, variant) DO UPDATE "
                "SET spec = excluded.spec, built_at = excluded.built_at"
            ),
            {"c": chart_id, "v": variant, "s": json.dumps(spec)},
        )


def drop_chart_preview(chart_id: int, variant: str | None = None) -> None:
    """Forget a chart's cached result — one shape of it, or every shape."""
    sql = "DELETE FROM chart_previews WHERE chart_id = :c"
    params: dict = {"c": chart_id}
    if variant is not None:
        sql += " AND variant = :v"
        params["v"] = variant
    with engine().begin() as conn:
        conn.execute(text(sql), params)


# ── dashboard filters ──────────────────────────────────────────────────────


@dataclass
class DashboardFilter:
    """One control on a dashboard's filter bar. See app/filters.py."""

    key: str
    label: str
    column_expr: str
    filter_type: str = "select"
    values_sql: str = ""
    source_db: str = ""
    default_value: str = ""
    applies_to: list[str] = field(default_factory=list)
    position: int = 0
    id: int | None = None


def _row_to_filter(row: Any) -> DashboardFilter:
    m = row._mapping
    scope = m["applies_to"]
    if isinstance(scope, str):
        scope = json.loads(scope)
    return DashboardFilter(
        id=m["id"],
        key=m["key"],
        label=m["label"],
        filter_type=m["filter_type"],
        column_expr=m["column_expr"],
        values_sql=m["values_sql"],
        source_db=m["source_db"],
        default_value=m["default_value"],
        applies_to=list(scope or []),
        position=m["position"],
    )


_FILTER_SELECT = (
    "SELECT id, key, label, filter_type, column_expr, values_sql, source_db, "
    "default_value, applies_to, position FROM dashboard_filters"
)


def list_filters(dashboard_slug: str) -> list[DashboardFilter]:
    with engine().connect() as conn:
        rows = conn.execute(
            text(
                f"{_FILTER_SELECT} WHERE dashboard_id = "
                "(SELECT id FROM dashboards WHERE slug = :s) ORDER BY position, id"
            ),
            {"s": dashboard_slug},
        ).fetchall()
    return [_row_to_filter(r) for r in rows]


def add_filter(dashboard_slug: str, flt: DashboardFilter) -> bool:
    """Add a filter. False when the dashboard is gone or the key is taken."""
    with engine().begin() as conn:
        dash_id = conn.execute(
            text("SELECT id FROM dashboards WHERE slug = :s"), {"s": dashboard_slug}
        ).scalar()
        if dash_id is None:
            return False
        position = conn.execute(
            text(
                "SELECT coalesce(max(position), -1) + 1 FROM dashboard_filters "
                "WHERE dashboard_id = :id"
            ),
            {"id": dash_id},
        ).scalar()
        try:
            conn.execute(
                text(
                    "INSERT INTO dashboard_filters "
                    "(dashboard_id, key, label, filter_type, column_expr, values_sql, "
                    " source_db, default_value, applies_to, position) "
                    "VALUES (:d, :k, :l, :t, :c, :v, :db, :def, CAST(:scope AS jsonb), :p)"
                ),
                {
                    "d": dash_id,
                    "k": flt.key,
                    "l": flt.label,
                    "t": flt.filter_type,
                    "c": flt.column_expr,
                    "v": flt.values_sql,
                    "db": flt.source_db,
                    "def": flt.default_value,
                    "scope": json.dumps(list(flt.applies_to or [])),
                    "p": position,
                },
            )
        except IntegrityError:
            return False  # duplicate key on this dashboard
        _touch(conn, dash_id)
        return True


def update_filter(dashboard_slug: str, filter_id: int, **fields) -> bool:
    """Edit a filter in place.

    The key is deliberately not editable: it appears in the query string, so
    changing it would break every link somebody has already shared.
    """
    allowed = ("label", "column_expr", "filter_type", "values_sql", "source_db", "default_value")
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return False
    assignments = ", ".join(f"{k} = :{k}" for k in sets)
    with engine().begin() as conn:
        result = conn.execute(
            text(
                f"UPDATE dashboard_filters SET {assignments} "  # noqa: S608 — fixed key list
                "WHERE id = :fid AND dashboard_id = "
                "(SELECT id FROM dashboards WHERE slug = :slug)"
            ),
            {**sets, "fid": filter_id, "slug": dashboard_slug},
        )
        return result.rowcount > 0


def delete_filter(dashboard_slug: str, filter_id: int) -> bool:
    with engine().begin() as conn:
        result = conn.execute(
            text(
                "DELETE FROM dashboard_filters WHERE id = :i AND dashboard_id = "
                "(SELECT id FROM dashboards WHERE slug = :s)"
            ),
            {"i": filter_id, "s": dashboard_slug},
        )
        return result.rowcount > 0


# ── reports (authored in the UI) ───────────────────────────────────────────


@dataclass
class Report:
    """A saved query rendered as a downloadable report.

    Same shape as Chart on purpose — the difference is what renders it, not
    what it is. `editable` is False for reports that came from reports.toml,
    which stay under git review and can't be changed from the UI.
    """

    slug: str
    title: str
    source_db: str
    sql: str
    description: str = ""
    created_by: str = ""
    id: int | None = None
    editable: bool = True
    folder_id: int | None = None  # display grouping only
    created_at: datetime | None = None
    updated_at: datetime | None = None


_REPORT_SELECT = (
    "SELECT id, slug, title, description, source_db, sql, created_by, "
    "folder_id, created_at, updated_at FROM reports"
)


def _row_to_report(row: Any) -> Report:
    m = row._mapping
    return Report(
        id=m["id"],
        slug=m["slug"],
        title=m["title"],
        description=m["description"],
        source_db=m["source_db"],
        sql=m["sql"],
        created_by=m["created_by"],
        folder_id=m["folder_id"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
    )


def list_reports() -> list[Report]:
    with engine().connect() as conn:
        rows = conn.execute(text(f"{_REPORT_SELECT} ORDER BY title"))
        return [_row_to_report(r) for r in rows]


def get_report(slug: str) -> Report | None:
    with engine().connect() as conn:
        row = conn.execute(text(f"{_REPORT_SELECT} WHERE slug = :s"), {"s": slug}).first()
        return _row_to_report(row) if row else None


def save_report(report: Report) -> Report:
    with engine().begin() as conn:
        row = conn.execute(
            text(
                """
                INSERT INTO reports (slug, title, description, source_db, sql, created_by)
                VALUES (:slug, :title, :description, :source_db, :sql, :created_by)
                ON CONFLICT (slug) DO UPDATE SET
                    title       = EXCLUDED.title,
                    description = EXCLUDED.description,
                    source_db   = EXCLUDED.source_db,
                    sql         = EXCLUDED.sql,
                    updated_at  = now()
                -- folder_id deliberately untouched; see save_chart().
                RETURNING id, slug, title, description, source_db, sql, created_by,
                          folder_id, created_at, updated_at
                """
            ),
            {
                "slug": report.slug,
                "title": report.title,
                "description": report.description,
                "source_db": report.source_db,
                "sql": report.sql,
                "created_by": report.created_by,
            },
        ).first()
    return _row_to_report(row)


def delete_report(slug: str) -> bool:
    with engine().begin() as conn:
        result = conn.execute(text("DELETE FROM reports WHERE slug = :s"), {"s": slug})
        return result.rowcount > 0


# ── folders (presentation only) ────────────────────────────────────────────
#
# Nothing in this section is consulted by access_for(). Folders group the list
# pages and nothing more: they are applied *after* permission filtering, so the
# most a folder can do is decide under which heading an already-visible item
# appears. There is deliberately no FOLDER resource type — see RESOURCE_TYPES.


@dataclass
class Folder:
    name: str
    slug: str = ""
    description: str = ""
    position: int = 0
    created_by: str = ""
    id: int | None = None


# The three tables carrying a folder_id. Datasets are absent on purpose: they
# live in another database and are grouped by datasets.toml, which also holds
# their descriptions and example queries.
FOLDERED = (CHART, DASHBOARD, REPORT)
_FOLDER_TABLE = {CHART: "charts", DASHBOARD: "dashboards", REPORT: "reports"}


def _row_to_folder(row: Any) -> Folder:
    m = row._mapping
    return Folder(
        id=m["id"],
        slug=m["slug"],
        name=m["name"],
        description=m["description"],
        position=m["position"],
        created_by=m["created_by"],
    )


_FOLDER_COLUMNS = "id, slug, name, description, position, created_by"
_FOLDER_SELECT = f"SELECT {_FOLDER_COLUMNS} FROM folders"


def list_folders() -> list[Folder]:
    """Folders in display order. `position` first so an admin can order them,
    name as the tiebreak so two new folders never flip around between loads."""
    with engine().connect() as conn:
        rows = conn.execute(text(f"{_FOLDER_SELECT} ORDER BY position, name"))
        return [_row_to_folder(r) for r in rows]


def get_folder(slug: str) -> Folder | None:
    with engine().connect() as conn:
        row = conn.execute(text(f"{_FOLDER_SELECT} WHERE slug = :s"), {"s": slug}).first()
        return _row_to_folder(row) if row else None


def create_folder(name: str, description: str = "", created_by: str = "") -> Folder | None:
    name = name.strip()
    if not name:
        return None
    slug = unique_slug(name, exists=get_folder)
    with engine().begin() as conn:
        # New folders land at the bottom rather than jumping to the top.
        last = conn.execute(text("SELECT coalesce(max(position), 0) FROM folders")).scalar()
        row = conn.execute(
            text(
                "INSERT INTO folders (slug, name, description, position, created_by) "
                "VALUES (:slug, :name, :description, :position, :created_by) "
                f"RETURNING {_FOLDER_COLUMNS}"
            ),
            {
                "slug": slug,
                "name": name,
                "description": description.strip(),
                "position": (last or 0) + 1,
                "created_by": created_by,
            },
        ).first()
    return _row_to_folder(row)


def update_folder(folder_id: int, name: str, description: str = "") -> bool:
    """Rename in place. The slug is left alone so any link to it keeps working."""
    name = name.strip()
    if not name:
        return False
    with engine().begin() as conn:
        result = conn.execute(
            text("UPDATE folders SET name = :n, description = :d WHERE id = :id"),
            {"n": name, "d": description.strip(), "id": folder_id},
        )
        return result.rowcount > 0


def delete_folder(folder_id: int) -> bool:
    """Delete the folder. Its contents survive and become ungrouped — the FK is
    ON DELETE SET NULL, so this can never take a chart or report with it."""
    with engine().begin() as conn:
        result = conn.execute(text("DELETE FROM folders WHERE id = :id"), {"id": folder_id})
        return result.rowcount > 0


def move_folder(folder_id: int, direction: str) -> bool:
    """Swap this folder's position with its neighbour above or below."""
    if direction not in ("up", "down"):
        return False
    with engine().begin() as conn:
        ordered = [
            (r[0], r[1])
            for r in conn.execute(text("SELECT id, position FROM folders ORDER BY position, name"))
        ]
        index = next((i for i, (fid, _) in enumerate(ordered) if fid == folder_id), None)
        if index is None:
            return False
        target = index - 1 if direction == "up" else index + 1
        if not 0 <= target < len(ordered):
            return False
        # Rewrite the whole order rather than swapping two values: positions
        # seeded from other paths can collide, and a swap of equal values is a
        # no-op the user reads as a bug.
        ordered[index], ordered[target] = ordered[target], ordered[index]
        for position, (fid, _) in enumerate(ordered):
            conn.execute(
                text("UPDATE folders SET position = :p WHERE id = :id"),
                {"p": position, "id": fid},
            )
    return True


def set_item_folder(resource_type: str, key: str, folder_id: int | None) -> bool:
    """Move one item into a folder, or out of every folder with None.

    The only writer of folder_id. Saving a chart, dashboard or report never
    touches it, so editing a thing cannot silently move it.
    """
    table = _FOLDER_TABLE.get(resource_type)
    if table is None:
        return False
    with engine().begin() as conn:
        if folder_id is not None:
            exists = conn.execute(
                text("SELECT 1 FROM folders WHERE id = :id"), {"id": folder_id}
            ).first()
            if exists is None:
                return False
        result = conn.execute(
            text(f"UPDATE {table} SET folder_id = :f WHERE slug = :s"),  # noqa: S608 — fixed map
            {"f": folder_id, "s": key},
        )
        return result.rowcount > 0


def folder_counts() -> dict[int, int]:
    """How many items sit in each folder, across all three types.

    Admin-only: it counts everything that exists, ignoring who may see it, and
    is shown on the folder admin screen so an empty folder is obvious.
    """
    union = " UNION ALL ".join(
        f"SELECT folder_id FROM {t} WHERE folder_id IS NOT NULL" for t in _FOLDER_TABLE.values()
    )
    with engine().connect() as conn:
        rows = conn.execute(text(f"SELECT folder_id, count(*) FROM ({union}) x GROUP BY 1"))
        return dict(rows.all())
