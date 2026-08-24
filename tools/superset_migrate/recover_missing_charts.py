"""Recover the charts that failed migration only because their OLD query timed
out during verification, and now translate cleanly under the fixed translator.

They were never written to report_hub; the migration left a `missing` marker in
each of their dashboard slots (correct geometry, the chart's name in
`title_override`). Recovery therefore does NOT re-run the whole migration (which
would wipe every cache and re-create every chart with a new id, dropping tags
and per-chart grants). Instead it:

  1. re-translates the still-missing slices with the current translator,
  2. inserts just those chart rows, and
  3. flips their `missing` markers into chart tiles in place — no delete, so no
     cache cascade and no lost associations.

    python -m tools.superset_migrate.recover_missing_charts            # dry run
    python -m tools.superset_migrate.recover_missing_charts --apply    # write
"""
import sys
from collections import defaultdict

from sqlalchemy import text

from app import store
from tools.superset_migrate import migrate as M
from tools.superset_migrate import translate as T
from tools.superset_migrate.repair_dist_bar_axis import _load, _translate_all

APPLY = "--apply" in sys.argv


def main():
    rows, saved, calc = _load()
    new = _translate_all(rows, saved, calc)

    original = T.CATEGORICAL_VIZ
    T.CATEGORICAL_VIZ = set()
    try:
        old = _translate_all(rows, saved, calc)
    finally:
        T.CATEGORICAL_VIZ = original

    changed = {
        sid: (old[sid], new[sid])
        for sid in new
        if sid in old and old[sid]["sql"] != new[sid]["sql"]
    }

    by_sql = defaultdict(list)
    for ch in store.list_charts(with_sql=True):
        if (ch.created_by or "") == "superset-import":
            by_sql[ch.sql].append(ch)

    # The slices that changed AND have no report_hub chart carrying their fixed
    # SQL: they never migrated. (Matching the fixed SQL, not the old one, so the
    # already-repaired 101 — which now store the fixed SQL — are excluded.)
    missing = [sid for sid, (_o, n) in changed.items() if not by_sql.get(n["sql"])]

    meta = {
        r[0]: {"name": r[1], "viz": r[2], "db": M.DB_MAP.get(r[8], r[8])} for r in rows
    }

    # slice -> the Superset dashboards it sits on, and their titles
    sup_title = {did: (t or "") for did, t in M.q(
        "SELECT id, dashboard_title FROM dashboards"
    )}
    dashes_of = defaultdict(list)
    for did, sid in M.q("SELECT dashboard_id, slice_id FROM dashboard_slices"):
        dashes_of[sid].append(did)

    # report_hub dashboards, by the (truncated) title the migration stored
    rh_by_title = defaultdict(list)
    for d in store.list_dashboards():
        if (d.created_by or "") == "superset-import":
            rh_by_title[d.title].append(d.slug)

    # Every missing marker in one query, indexed by (dashboard slug, its title) —
    # far cheaper than get_dashboard() per candidate over the tunnel.
    marker = defaultdict(list)
    with store.engine().begin() as cx:
        for dslug, item_id, tov in cx.execute(
            text(
                "SELECT d.slug, i.id, i.title_override "
                "FROM dashboard_items i JOIN dashboards d ON d.id = i.dashboard_id "
                "WHERE i.kind = 'missing'"
            )
        ):
            marker[(dslug, tov or "")].append(item_id)

    plan = []  # (sid, name, spec, [(dash_slug, item_id), ...])
    orphans = []
    for sid in missing:
        name = meta[sid]["name"]
        want = (name or "")[:200]
        slots = []
        for did in dashes_of.get(sid, []):
            for dslug in rh_by_title.get((sup_title.get(did) or "")[:200], []):
                for item_id in marker.get((dslug, want), []):
                    slots.append((dslug, item_id))
        if slots:
            plan.append((sid, name, new[sid], slots))
        else:
            orphans.append((sid, name))

    print(f"still-missing slices that now translate: {len(missing)}")
    print(f"  with a marker to flip: {len(plan)} | no marker found: {len(orphans)}")
    total_slots = sum(len(s) for _, _, _, s in plan)
    print(f"  chart rows to create: {len(plan)} | markers to flip: {total_slots}\n")
    for sid, name, spec, slots in plan:
        print(f"  {name[:52]:52} -> {len(slots)} slot(s)  x={spec['x_column']!r}")
    if orphans:
        print("\n  NO MARKER (skipped):")
        for sid, name in orphans:
            print(f"    {name[:60]}")

    if not APPLY:
        print("\nDRY RUN — nothing written. Re-run with --apply to write.")
        return

    taken = store.all_slugs("charts")
    done_charts = done_slots = 0
    for sid, name, spec, slots in plan:
        base = store.slugify(name or f"chart-{sid}")
        slug, k = base, 1
        while slug in taken:
            k += 1
            slug = f"{base}-{k}"
        taken.add(slug)
        saved_chart = store.save_chart(
            store.Chart(
                slug=slug,
                title=(name or f"Chart {sid}")[:200],
                description=f"Recovered from Superset chart #{sid} ({meta[sid]['viz']}).",
                source_db=meta[sid]["db"],
                sql=spec["sql"],
                chart_type=spec["chart_type"],
                x_column=spec["x_column"],
                y_columns=spec["y_columns"],
                series_column=spec.get("series_column", ""),
                created_by=M.STAMP,
            )
        )
        done_charts += 1
        with store.engine().begin() as cx:
            for _dslug, item_id in slots:
                cx.execute(
                    text(
                        "UPDATE dashboard_items "
                        "SET kind = 'chart', chart_id = :c, content = '' "
                        "WHERE id = :i AND kind = 'missing'"
                    ),
                    {"c": saved_chart.id, "i": item_id},
                )
                done_slots += 1
    print(f"\nAPPLIED — {done_charts} charts created, {done_slots} markers flipped.")


if __name__ == "__main__":
    main()
