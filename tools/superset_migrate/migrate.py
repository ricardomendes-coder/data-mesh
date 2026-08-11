"""Migrate Superset dashboards into Report Hub.

    python -m tools.superset_migrate.migrate            # dry run, writes nothing
    python -m tools.superset_migrate.migrate --apply    # write to report_hub
    python -m tools.superset_migrate.migrate --wipe     # remove a previous run

Everything written is stamped created_by='superset-import', so a run can be
undone in full. Re-running --apply after a --wipe is the supported way to redo
the migration once this script improves.

Nothing is imported without proof it runs: every generated query is EXPLAINed
against the real database first, which resolves every column and plans the query
without executing it. Charts that don't plan are reported, not imported.
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

from app import filters as app_filters  # noqa: E402
from app import store  # noqa: E402

from . import translate as T  # noqa: E402

STAMP = "superset-import"

# Superset connection names -> the database name Report Hub knows. Every
# Superset connection points at the same instance; only the label differs.
DB_MAP = {"analytics_db": "analytics", "PostgreSQL": "analytics", "robots_api_log": "robots_api"}

SERIES_CAP = 12  # past this a pivoted chart is unreadable, so it isn't migrated


def superset_url(db: str) -> str:
    """Read-only connection to the Superset metadata (and to the warehouses)."""
    user = os.environ.get("SUPERSET_READ_USER") or os.environ.get("INTERNAL_ADMIN_USER")
    pwd = os.environ.get("SUPERSET_READ_PWD") or os.environ.get("INTERNAL_ADMIN_PWD")
    host = os.environ.get("SUPERSET_READ_HOST", "127.0.0.1")
    port = os.environ.get("SUPERSET_READ_PORT", "15432")
    if not user or not pwd:
        sys.exit("Set INTERNAL_ADMIN_USER / INTERNAL_ADMIN_PWD (see the README).")
    return f"postgresql+psycopg2://{quote_plus(user)}:{quote_plus(pwd)}@{host}:{port}/{db}"


_engines: dict[str, object] = {}


def engine(db: str):
    if db not in _engines:
        _engines[db] = create_engine(superset_url(db), pool_pre_ping=True)
    return _engines[db]


def q(sql: str, db: str = "superset", **params):
    with engine(db).connect() as c:
        return c.execute(text(sql), params).fetchall()


def explain(db: str, sql: str) -> str | None:
    """None if the query plans, else the first line of the error."""
    # The token is Report Hub's, not SQL — strip it before asking the planner.
    sql = app_filters.strip_token(sql)
    try:
        with engine(db).connect() as c:
            c.execute(text("SET statement_timeout = '20s'"))
            c.execute(text("EXPLAIN " + sql))
        return None
    except Exception as exc:
        return str(exc).split("\n")[0][:180]


def series_values(db: str, source: str, expr: str, where: list[str]):
    sql = f"SELECT {expr} AS v, count(*) FROM {source}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" GROUP BY 1 ORDER BY 2 DESC LIMIT {SERIES_CAP + 1}"
    try:
        with engine(db).connect() as c:
            c.execute(text("SET statement_timeout = '10s'"))
            rows = c.execute(text(sql)).fetchall()
    except Exception:
        return None
    values = [r[0] for r in rows if r[0] is not None]
    return values if values and len(values) <= SERIES_CAP else None


def load_charts(pivot: bool):
    """Translate every chart that sits on a dashboard. Returns (ok, failed)."""
    rows = q(
        """
        SELECT DISTINCT s.id, s.slice_name, s.viz_type, s.params, s.datasource_id,
               t.table_name, t.schema, t.sql AS dataset_sql, d.database_name
        FROM slices s
        JOIN tables t ON t.id = s.datasource_id AND s.datasource_type = 'table'
        JOIN dbs d ON d.id = t.database_id
        JOIN dashboard_slices ds ON ds.slice_id = s.id
        ORDER BY s.id
        """
    )
    saved = defaultdict(dict)
    for table_id, name, expression in q(
        "SELECT table_id, metric_name, expression FROM sql_metrics"
    ):
        saved[table_id][name] = expression

    ok, failed, reasons = [], [], Counter()
    total = len(rows)
    for i, row in enumerate(rows, 1):
        if i % 50 == 0:
            print(f"  ...{i}/{total}  ok={len(ok)} failed={len(failed)}", flush=True)
        sid, name, viz, params, ds_id, table_name, schema, dataset_sql, dbname = row
        db = DB_MAP.get(dbname, dbname)
        slice_row = {"params": params, "viz_type": viz}
        dataset = {"table_name": table_name, "schema": schema, "sql": dataset_sql}
        try:
            spec = T.translate(slice_row, dataset, saved.get(ds_id, {}))
        except T.Unsupported as exc:
            if "needs its series values resolved" not in str(exc):
                reasons[str(exc)[:70]] += 1
                failed.append((sid, name, viz, db, str(exc)[:110]))
                continue
            if not pivot:
                reasons["time+series (pivot pass skipped)"] += 1
                failed.append((sid, name, viz, db, "needs pivot"))
                continue
            try:
                probe = T.translate(slice_row, dataset, saved.get(ds_id, {}), series_values=["_"])
                values = series_values(
                    db, T.source_sql(dataset), probe["series_expr"], probe["where"]
                )
            except T.Unsupported as exc2:
                reasons[str(exc2)[:70]] += 1
                failed.append((sid, name, viz, db, str(exc2)[:110]))
                continue
            if not values:
                reasons["too many series to pivot"] += 1
                failed.append((sid, name, viz, db, "too many series"))
                continue
            try:
                spec = T.translate(slice_row, dataset, saved.get(ds_id, {}), series_values=values)
            except T.Unsupported as exc3:
                reasons[str(exc3)[:70]] += 1
                failed.append((sid, name, viz, db, str(exc3)[:110]))
                continue

        error = explain(db, spec["sql"])
        if error:
            reasons["did not plan: " + error.split(":")[0][:38]] += 1
            failed.append((sid, name, viz, db, "EXPLAIN: " + error))
            continue
        ok.append({"id": sid, "name": name, "viz": viz, "db": db,
                   "dataset_id": ds_id, "source": T.source_sql(dataset), **spec})
    return ok, failed, reasons


def dataset_columns() -> dict[int, set[str]]:
    """dataset id -> its column names, for deciding which charts a filter fits."""
    out = defaultdict(set)
    for table_id, name in q("SELECT table_id, column_name FROM table_columns"):
        out[table_id].add(name)
    return out


def wipe():
    with store.engine().begin() as c:
        for table in ("dashboards", "charts"):
            n = c.execute(
                text(f"DELETE FROM {table} WHERE created_by = :s"), {"s": STAMP}
            ).rowcount
            print(f"  removed {n} from {table}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write to report_hub")
    ap.add_argument("--wipe", action="store_true", help="remove a previous import and stop")
    ap.add_argument("--no-pivot", action="store_true", help="skip the slow series probing")
    args = ap.parse_args()

    if args.wipe:
        wipe()
        return

    charts, failed, reasons = load_charts(pivot=not args.no_pivot)
    print(f"\ntranslated and verified : {len(charts)}")
    print(f"could not translate     : {len(failed)}")
    print("\n-- by tile type --")
    for kind, n in Counter(c["chart_type"] for c in charts).most_common():
        print(f"  {kind:8} {n}")
    print("\n-- why the rest didn't make it --")
    for reason, n in reasons.most_common(12):
        print(f"  {n:4}  {reason}")

    dashboards = q(
        "SELECT id, dashboard_title, position_json, json_metadata FROM dashboards ORDER BY id"
    )
    links = defaultdict(list)
    for dash_id, slice_id in q("SELECT dashboard_id, slice_id FROM dashboard_slices"):
        links[dash_id].append(slice_id)

    by_slice = {c["id"]: c for c in charts}
    live = {d[0]: d for d in dashboards if any(s in by_slice for s in links.get(d[0], []))}
    print(f"\ndashboards with at least one migrated chart: {len(live)}")

    if not args.apply:
        print("\n(dry run — nothing written; pass --apply)")
        return
    if not store.available():
        sys.exit("report_hub is not reachable — check APP_DB_* and the tunnel")

    columns_of = dataset_columns()

    # ── charts ──
    slug_of: dict[int, str] = {}
    for n, c in enumerate(charts, 1):
        slug = store.unique_slug(c["name"] or f"chart-{c['id']}", exists=store.get_chart)
        saved = store.save_chart(store.Chart(
            slug=slug,
            title=(c["name"] or f"Chart {c['id']}")[:200],
            description=f"Migrated from Superset chart #{c['id']} ({c['viz']}).",
            source_db=c["db"],
            sql=c["sql"],
            chart_type=c["chart_type"],
            x_column=c["x_column"],
            y_columns=c["y_columns"],
            created_by=STAMP,
        ))
        slug_of[c["id"]] = saved.slug
        if n % 50 == 0:
            print(f"  ...{n}/{len(charts)} charts", flush=True)
    print(f"created {len(slug_of)} charts")

    # ── dashboards, with their tabs, text and filters ──
    # Every write is its own round trip, and over an SSH tunnel that is the slow
    # part of the run — so this reports per dashboard rather than going quiet
    # for twenty minutes.
    made = tiles = tabs = texts = filters_made = 0
    total_dash = len(live)
    for dash_id, (_, title, position_json, json_metadata) in live.items():
        title = (title or f"Dashboard {dash_id}")[:200]
        slug = store.unique_slug(title, exists=store.get_dashboard)
        store.save_dashboard(store.Dashboard(
            slug=slug, title=title,
            description=f"Migrated from Superset dashboard #{dash_id}.",
            created_by=STAMP,
        ))
        made += 1

        tab_of_chart, tab_order = T.tabs_of(position_json)
        section_id: dict[str, int] = {}
        for tab_title in tab_order:
            new_id = store.add_section(slug, tab_title)
            if new_id:
                section_id[tab_title] = new_id
                tabs += 1

        # Headings and notes, so an imported dashboard keeps its structure.
        for tab_title, body in T.markdown_blocks(position_json):
            if store.add_text_item(slug, body[:2000], section_id=section_id.get(tab_title)):
                texts += 1

        mine = [s for s in links.get(dash_id, []) if s in slug_of]
        width = "half" if len(mine) > 1 else "full"
        for slice_id in mine:
            tab_title = tab_of_chart.get(slice_id)
            if store.add_item(slug, slug_of[slice_id], width=width,
                              section_id=section_id.get(tab_title)):
                tiles += 1

        # A filter only reaches charts whose dataset actually has that column —
        # pointing one at a chart without it would just break that tile.
        for f in T.filters_of(json_metadata):
            scope = [
                slug_of[s] for s in mine
                if s not in f["excluded"]
                and f["column_expr"] in columns_of.get(by_slice[s]["dataset_id"], set())
            ]
            if not scope:
                continue
            # Offer the options from the dataset the filter targets, so the
            # dropdown lists what the charts can actually be filtered to.
            target = next(
                (by_slice[s] for s in mine
                 if f["column_expr"] in columns_of.get(by_slice[s]["dataset_id"], set())),
                by_slice[mine[0]],
            )
            column = T.qi(f["column_expr"])
            values_sql = ""
            if f["filter_type"] == "select":
                values_sql = (
                    f"SELECT DISTINCT {column} FROM {target['source']} "
                    f"WHERE {column} IS NOT NULL ORDER BY 1 LIMIT 1000"
                )
            filters_made += store.add_filter(slug, store.DashboardFilter(
                key=f["key"], label=f["label"], column_expr=f["column_expr"],
                filter_type=f["filter_type"], values_sql=values_sql,
                source_db=target["db"], default_value=f["default_value"],
                applies_to=scope,
            ))

        print(
            f"  [{made}/{total_dash}] {title[:44]:46} "
            f"{len(mine):3} tiles  {len(section_id):2} tabs  "
            f"{filters_made:3} filters so far",
            flush=True,
        )

    # A tab whose charts all failed to migrate would render as an empty pane —
    # worse than not being there, because it looks like the data is missing
    # rather than the chart. The tiles it would have held are in failed.json.
    with store.engine().begin() as c:
        dropped = c.execute(text("""
            DELETE FROM dashboard_sections s
            WHERE s.dashboard_id IN (SELECT id FROM dashboards WHERE created_by = :stamp)
              AND NOT EXISTS (SELECT 1 FROM dashboard_items i WHERE i.section_id = s.id)
        """), {"stamp": STAMP}).rowcount
    tabs -= dropped

    print(f"created {made} dashboards, {tabs} tabs ({dropped} empty ones dropped), "
          f"{tiles} tiles, {texts} text blocks, {filters_made} filters")

    with open(os.path.join(os.path.dirname(__file__), "failed.json"), "w") as fh:
        json.dump(failed, fh, ensure_ascii=False, indent=1)
    print(f"wrote failed.json ({len(failed)})")


if __name__ == "__main__":
    main()
