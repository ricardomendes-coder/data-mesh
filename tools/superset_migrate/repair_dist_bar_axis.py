"""Repair the dist_bar/bar charts whose time FILTER was migrated onto the axis.

Background: a categorical bar (Superset `dist_bar`/`bar`) plots its `groupby`
on the x-axis; `granularity_sqla` is only the time-range filter. An earlier
translator promoted that timestamp to the axis, producing
`GROUP BY (raw_timestamp, series)` — millions of groups, a query that never
returned. translate.py now gates that promotion to real time-series viz types.

This script re-translates every dashboard slice both the OLD way (the gate
widened to include dist_bar/bar, reproducing exactly what is stored) and the
NEW way, and updates only the report_hub charts whose stored SQL matches an OLD
translation that changed. Matching on exact SQL is immune to duplicate titles:
identical SQL is the same logical query, a different query has different SQL.

    python -m tools.superset_migrate.repair_dist_bar_axis            # dry run
    python -m tools.superset_migrate.repair_dist_bar_axis --apply    # write
"""
import json
import os
import sys
from collections import defaultdict

from app import store
from tools.superset_migrate import migrate as M
from tools.superset_migrate import translate as T

APPLY = "--apply" in sys.argv


def _load():
    rows = list(
        M.q(
            """
            SELECT DISTINCT s.id, s.slice_name, s.viz_type, s.params,
                   s.datasource_id, t.table_name, t.schema, t.sql AS dataset_sql,
                   d.database_name
            FROM slices s
            JOIN tables t ON t.id = s.datasource_id AND s.datasource_type = 'table'
            JOIN dbs d ON d.id = t.database_id
            JOIN dashboard_slices ds ON ds.slice_id = s.id
            ORDER BY s.id
            """
        )
    )
    saved = defaultdict(dict)
    for tid, n, e in M.q("SELECT table_id, metric_name, expression FROM sql_metrics"):
        saved[tid][n] = e
    calc = defaultdict(dict)
    for tid, n, e in M.q(
        "SELECT table_id, column_name, expression FROM table_columns "
        "WHERE expression IS NOT NULL AND expression <> ''"
    ):
        calc[tid][n] = e
    return rows, saved, calc


def _translate_all(rows, saved, calc):
    out = {}
    for sid, name, viz, params, ds_id, tn, sch, dsql, dbn in rows:
        try:
            out[sid] = T.translate(
                {"params": params, "viz_type": viz},
                {"table_name": tn, "schema": sch, "sql": dsql},
                saved.get(ds_id, {}),
                calc_columns=calc.get(ds_id, {}),
            )
        except T.Unsupported:
            continue
    return out


def main():
    rows, saved, calc = _load()

    new = _translate_all(rows, saved, calc)

    # OLD behaviour = nothing is categorical, so dist_bar promotes its
    # granularity onto the axis again — exactly what is stored in report_hub.
    original = T.CATEGORICAL_VIZ
    T.CATEGORICAL_VIZ = set()
    try:
        old = _translate_all(rows, saved, calc)
    finally:
        T.CATEGORICAL_VIZ = original

    changed = [
        (sid, old[sid], new[sid])
        for sid in new
        if sid in old and old[sid]["sql"] != new[sid]["sql"]
    ]
    # A dist_bar with granularity but no groupby now fails where it used to
    # produce a (wrong) time bar: it is in `old` but not `new`.
    now_fail = [sid for sid in old if sid not in new]
    print(f"slices whose SQL changes with the fix: {len(changed)}")
    print(f"slices that now fail to translate (no dimension left): {len(now_fail)}")

    by_sql = defaultdict(list)
    for ch in store.list_charts(with_sql=True):
        if (ch.created_by or "") == "superset-import":
            by_sql[ch.sql].append(ch)

    updates, unmatched = [], 0
    for sid, o, n in changed:
        targets = by_sql.get(o["sql"], [])
        if not targets:
            unmatched += 1
            continue
        for ch in targets:
            updates.append((ch, n))

    print(f"report_hub charts to update: {len(updates)} | unmatched slices: {unmatched}")
    for ch, n in updates[:12]:
        print(
            f"  {ch.slug[:48]:48}  x {ch.x_column!r} -> {n['x_column']!r}"
            f"  series {ch.series_column!r} -> {(n.get('series_column') or '')!r}"
        )

    if not APPLY:
        print("\nDRY RUN — nothing written. Re-run with --apply to write.")
        return

    # A reversible backup of exactly what we are about to overwrite.
    backup = [
        {
            "slug": ch.slug,
            "sql": ch.sql,
            "x_column": ch.x_column,
            "y_columns": ch.y_columns,
            "series_column": ch.series_column,
        }
        for ch, _ in updates
    ]
    path = os.path.join(os.path.dirname(__file__), "repair_dist_bar_backup.json")
    if os.path.exists(path):
        # Never clobber an earlier backup: a resumed run would only capture the
        # charts still unfixed, losing the originals of the ones already written.
        print(f"backup already present, keeping it: {path}")
    else:
        with open(path, "w") as fh:
            json.dump(backup, fh, ensure_ascii=False, indent=1)
        print(f"backup written: {path} ({len(backup)} charts)")

    done = 0
    for ch, n in updates:
        # `ch` came from list_charts(with_sql=True) with every field save_chart
        # needs, so update it in place — no second round trip per chart.
        ch.sql = n["sql"]
        ch.x_column = n["x_column"]
        ch.y_columns = n["y_columns"]
        ch.series_column = n.get("series_column") or ""
        store.save_chart(ch)
        done += 1
    print(f"\nAPPLIED — {done} charts updated (their tile caches invalidated).")


if __name__ == "__main__":
    main()
