"""The Datasets catalog.

Every table, view and materialized view in the target schema is discovered live
from the Postgres catalog, so a new dataset shows up without being registered
anywhere. `datasets.toml` is a curation layer on top of that: it hides noisy
objects, groups datasets into folders, and supplies the descriptions and
example queries the database itself doesn't carry (analytics has no
`COMMENT ON` — 0 of its ~4,800 columns are documented).

The manifest is read fresh on each request, matching how reports.py works, so
edits take effect without a restart.
"""

try:
    import tomllib  # Python 3.11+ (the app targets 3.12)
except ModuleNotFoundError:  # pragma: no cover - backport for Python <= 3.10
    import tomli as tomllib

import logging
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from sqlalchemy import text

from . import db
from .config import get_settings

logger = logging.getLogger("report_hub")
BASE_DIR = Path(__file__).resolve().parent.parent

UNGROUPED = "ungrouped"


@dataclass
class Column:
    name: str
    type: str
    nullable: bool
    default: str | None = None


@dataclass
class Example:
    title: str
    sql: str


@dataclass
class Dataset:
    name: str
    kind: str  # table | view | matview
    column_count: int
    # Planner estimate from pg_class.reltuples — None when the object has never
    # been analyzed (Postgres reports -1). Never an exact count: counting 191
    # tables on every page load isn't worth it.
    approx_rows: int | None = None
    title: str = ""
    description: str = ""
    folder: str | None = None
    examples: list[Example] = field(default_factory=list)

    @property
    def display_title(self) -> str:
        return self.title or self.name

    @property
    def documented(self) -> bool:
        return bool(self.description)


@dataclass
class Folder:
    key: str
    title: str
    description: str = ""
    datasets: list[str] = field(default_factory=list)
    match: list[str] = field(default_factory=list)

    def claims(self, name: str) -> bool:
        return name in self.datasets or any(fnmatchcase(name, p) for p in self.match)


@dataclass
class Manifest:
    hide: list[str] = field(default_factory=list)
    folders: list[Folder] = field(default_factory=list)
    entries: dict[str, dict] = field(default_factory=dict)

    def hidden(self, name: str) -> bool:
        return any(fnmatchcase(name, p) for p in self.hide)

    def folder_for(self, name: str) -> str | None:
        """First folder that claims `name`, or None. Order in the file wins."""
        for folder in self.folders:
            if folder.claims(name):
                return folder.key
        return None


def _manifest_path() -> Path:
    p = Path(get_settings().datasets_file)
    return p if p.is_absolute() else BASE_DIR / p


def load_manifest() -> Manifest:
    """Parse datasets.toml. An absent or empty manifest is fine — everything in
    the schema is still listed, just without folders or descriptions."""
    path = _manifest_path()
    if not path.exists():
        return Manifest()
    with path.open("rb") as f:
        data = tomllib.load(f)

    folders: list[Folder] = []
    seen: set[str] = set()
    for entry in data.get("folder", []):
        key = entry["key"]
        if key in seen:
            raise ValueError(f"Duplicate folder key {key!r} in {path.name}.")
        seen.add(key)
        folders.append(
            Folder(
                key=key,
                title=entry.get("title", key),
                description=entry.get("description", ""),
                datasets=list(entry.get("datasets", [])),
                match=list(entry.get("match", [])),
            )
        )

    entries: dict[str, dict] = {}
    for entry in data.get("dataset", []):
        entries[entry["name"]] = entry

    return Manifest(
        hide=list(data.get("settings", {}).get("hide", [])),
        folders=folders,
        entries=entries,
    )


# ── catalog queries ────────────────────────────────────────────────────────
# Kept as thin functions so they're easy to stub in tests without a Postgres.

_OBJECTS_SQL = """
SELECT c.relname,
       CASE c.relkind WHEN 'm' THEN 'matview' WHEN 'v' THEN 'view' ELSE 'table' END,
       c.reltuples::bigint,
       (SELECT count(*) FROM pg_attribute a
         WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = :schema AND c.relkind IN ('r', 'p', 'v', 'm')
ORDER BY c.relname
"""

_COLUMNS_SQL = """
SELECT a.attname,
       format_type(a.atttypid, a.atttypmod),
       NOT a.attnotnull,
       pg_get_expr(d.adbin, d.adrelid)
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
WHERE n.nspname = :schema AND c.relname = :name
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""

# What each view/matview is built on — Postgres's own dependency tracking
# (pg_depend over the view's rewrite rule), not a guess from parsing SQL. Used
# to follow usage transitively: a panel that reads a view also uses every table
# and view that view selects from.
_DEPS_SQL = """
SELECT DISTINCT dep.relname AS obj, ref.relname AS uses
FROM pg_depend d
JOIN pg_rewrite rw ON rw.oid = d.objid
JOIN pg_class dep ON dep.oid = rw.ev_class
JOIN pg_class ref ON ref.oid = d.refobjid
JOIN pg_namespace ndep ON ndep.oid = dep.relnamespace
JOIN pg_namespace nref ON nref.oid = ref.relnamespace
WHERE d.deptype = 'n'
  AND dep.oid <> ref.oid
  AND dep.relkind IN ('v', 'm')
  AND ref.relkind IN ('r', 'p', 'v', 'm')
  AND ndep.nspname = :schema AND nref.nspname = :schema
"""


def _engine():
    return db._engine(get_settings().datasets_database)


def _fetch_objects() -> list[tuple]:
    s = get_settings()
    engine = _engine()
    try:
        with engine.connect() as conn:
            return [
                tuple(r) for r in conn.execute(text(_OBJECTS_SQL), {"schema": s.datasets_schema})
            ]
    finally:
        engine.dispose()


def _fetch_columns(name: str) -> list[tuple]:
    s = get_settings()
    engine = _engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(_COLUMNS_SQL), {"schema": s.datasets_schema, "name": name})
            return [tuple(r) for r in rows]
    finally:
        engine.dispose()


def _fetch_preview(name: str, limit: int) -> tuple[list[str], list[tuple]]:
    """`SELECT * FROM <dataset> LIMIT n`, under a statement timeout.

    `name` is only ever a value the catalog just handed us (callers resolve it
    through get_dataset first) and it is quoted by the dialect on the way in,
    so it cannot carry SQL. limit/timeout are coerced to int for the same
    reason — Postgres won't take bind parameters in LIMIT or SET.
    """
    s = get_settings()
    engine = _engine()
    try:
        preparer = engine.dialect.identifier_preparer
        qualified = preparer.quote(name)
        if s.datasets_schema:
            qualified = f"{preparer.quote(s.datasets_schema)}.{qualified}"
        with engine.connect() as conn:
            conn.execute(text(f"SET LOCAL statement_timeout = {int(s.dataset_timeout_ms)}"))
            result = conn.execute(text(f"SELECT * FROM {qualified} LIMIT {int(limit)}"))
            return list(result.keys()), [tuple(r) for r in result.fetchall()]
    finally:
        engine.dispose()


# ── public API ─────────────────────────────────────────────────────────────


def list_datasets() -> list[Dataset]:
    """Every visible dataset in the schema, decorated with manifest metadata."""
    manifest = load_manifest()
    out: list[Dataset] = []
    for name, kind, reltuples, column_count in _fetch_objects():
        if manifest.hidden(name):
            continue
        entry = manifest.entries.get(name, {})
        out.append(
            Dataset(
                name=name,
                kind=kind,
                column_count=column_count,
                # Postgres reports -1 for "never analyzed"; show nothing rather
                # than a made-up zero.
                approx_rows=int(reltuples) if reltuples is not None and reltuples >= 0 else None,
                title=entry.get("title", ""),
                description=entry.get("description", "").strip(),
                folder=manifest.folder_for(name),
                examples=[
                    Example(title=e.get("title", "Example"), sql=e.get("sql", "").strip())
                    for e in entry.get("example", [])
                ],
            )
        )
    return out


def get_dataset(name: str) -> Dataset | None:
    """Look `name` up in the live catalog. None if it doesn't exist or is hidden.

    This is the gate every dataset-scoped route goes through, so an arbitrary
    path segment can never reach a query.
    """
    return next((d for d in list_datasets() if d.name == name), None)


def get_columns(name: str) -> list[Column]:
    return [
        Column(name=c, type=t, nullable=bool(n), default=d) for c, t, n, d in _fetch_columns(name)
    ]


def view_definition(name: str) -> str:
    """The SELECT that builds a view/matview, for grounding an AI description.
    Empty string for a plain table or on any error — best-effort context."""
    s = get_settings()
    engine = _engine()
    try:
        with engine.connect() as conn:
            d = conn.execute(
                text(
                    "SELECT pg_get_viewdef((quote_ident(:s) || '.' || quote_ident(:n))::regclass, true)"
                ),
                {"s": s.datasets_schema, "n": name},
            ).scalar()
        return (d or "")[:3500]
    except Exception:
        return ""
    finally:
        engine.dispose()


def dependency_map() -> dict[str, set[str]]:
    """For each view/matview in the schema, the set of relations it reads from.

    Straight from pg_depend, so it is Postgres's own bookkeeping, not a parse of
    the definition. Lets usage be followed transitively — a dataset only ever
    read *inside* a view that a panel uses is still used.
    """
    s = get_settings()
    engine = _engine()
    out: dict[str, set[str]] = {}
    try:
        with engine.connect() as conn:
            for obj, uses in conn.execute(text(_DEPS_SQL), {"schema": s.datasets_schema}):
                out.setdefault(obj, set()).add(uses)
    finally:
        engine.dispose()
    return out


def get_preview(name: str, limit: int | None = None) -> tuple[list[str], list[tuple]]:
    limit = limit or get_settings().dataset_preview_rows
    return _fetch_preview(name, limit)


def group(datasets: list[Dataset]) -> list[tuple[Folder, list[Dataset]]]:
    """Bucket datasets by folder, in manifest order, with the leftovers last.

    Folders with no members are dropped — an empty manifest therefore renders
    as a single flat "All datasets" group.
    """
    manifest = load_manifest()
    by_key: dict[str, list[Dataset]] = {f.key: [] for f in manifest.folders}
    leftovers: list[Dataset] = []
    for d in datasets:
        if d.folder in by_key:
            by_key[d.folder].append(d)
        else:
            leftovers.append(d)

    grouped = [(f, by_key[f.key]) for f in manifest.folders if by_key[f.key]]
    if leftovers:
        title = "Ungrouped" if grouped else "All datasets"
        grouped.append((Folder(key=UNGROUPED, title=title), leftovers))
    return grouped
