"""Put a marker where a chart Superset draws could not be imported.

Every tile keeps the position Superset gave it, so the gap is there either way
— this only says what is missing and why, in the slot the chart would occupy.

    python -m tools.superset_migrate.fill_gaps            # report only
    python -m tools.superset_migrate.fill_gaps --apply

Reads the whole picture in three queries and writes in one transaction. The
first version called get_dashboard() per dashboard and inserted one marker at a
time: 63 full reads and 451 transactions across an SSH tunnel, which is minutes
of round trips for a few kilobytes of rows.
"""

import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

from sqlalchemy import text

from app import store

from . import translate as T
from .migrate import STAMP, q

DASH_RE = re.compile(r"Migrated from Superset dashboard #(\d+)")
CHART_RE = re.compile(r"Migrated from Superset chart #(\d+)")


def main() -> None:
    apply = "--apply" in sys.argv
    failed_path = Path(__file__).with_name("failed.json")
    failed = json.loads(failed_path.read_text()) if failed_path.exists() else []
    why = {row[0]: (row[1], row[4]) for row in failed}
    print(f"{len(why)} charts on record as not imported", flush=True)

    layouts = {d[0]: d[1] for d in q("SELECT id, position_json FROM dashboards")}
    print(f"read {len(layouts)} Superset layouts", flush=True)

    with store.engine().connect() as c:
        dashboards = c.execute(
            text("SELECT id, slug, description FROM dashboards WHERE created_by = :s"),
            {"s": STAMP},
        ).all()
        # One row per tile, deliberately not grouped: the same chart can sit on
        # a dashboard twice, and collapsing the duplicates made the second
        # occurrence look absent — it got a "not imported" marker on top of a
        # chart that was right there.
        items = c.execute(
            text(
                "SELECT i.dashboard_id, i.kind, c.description "
                "FROM dashboard_items i LEFT JOIN charts c ON c.id = i.chart_id "
                "WHERE i.dashboard_id = ANY(:ids)"
            ),
            {"ids": [d[0] for d in dashboards]},
        ).all()
        sections = c.execute(
            text(
                "SELECT dashboard_id, title, id FROM dashboard_sections "
                "WHERE dashboard_id = ANY(:ids)"
            ),
            {"ids": [d[0] for d in dashboards]},
        ).all()
        next_pos = dict(
            c.execute(
                text(
                    "SELECT dashboard_id, coalesce(max(position), -1) + 1 FROM dashboard_items "
                    "WHERE dashboard_id = ANY(:ids) GROUP BY 1"
                ),
                {"ids": [d[0] for d in dashboards]},
            ).all()
        )
    print(f"read {len(dashboards)} imported dashboards, {len(items)} tiles", flush=True)

    slices_of: dict[int, deque] = defaultdict(deque)
    already_filled = set()
    for dash_id, kind, description in items:
        if kind == "missing":
            already_filled.add(dash_id)
        m = CHART_RE.search(description or "")
        if m:
            slices_of[dash_id].append(m.group(1))
    section_of = {(d, title): sid for d, title, sid in sections}

    markers: list[dict] = []
    per_dash: dict[str, int] = {}
    for dash_id, slug, description in dashboards:
        m = DASH_RE.search(description or "")
        if not m or dash_id in already_filled:
            continue
        tiles, tab_order = T.layout_of(layouts.get(int(m.group(1)), ""))
        # A tab whose charts all failed to import was dropped as empty. Marking
        # those charts gives it content again, so it has to exist — otherwise
        # the markers land untabbed and the dashboard grows a tab strip that
        # doesn't match Superset's.
        if apply and tab_order:
            for title, sid in store.sync_sections(slug, tab_order).items():
                section_of[(dash_id, title)] = sid
        have = defaultdict(int)
        for key in slices_of[dash_id]:
            have[key] += 1
        position = next_pos.get(dash_id, 0)
        for tile in tiles:
            if tile.kind != "chart":
                continue
            key = str(tile.chart_id)
            if have[key]:
                have[key] -= 1
                continue
            name, raw = why.get(tile.chart_id, (tile.title, ""))
            markers.append(
                {
                    "dashboard_id": dash_id,
                    "section_id": section_of.get((dash_id, tile.tab)),
                    "title": (name or tile.title or "")[:200],
                    "content": T.missing_reason(raw),
                    "x": tile.x,
                    "y": tile.y,
                    "w": tile.w,
                    "h": tile.h,
                    "position": position,
                }
            )
            position += 1
            per_dash[slug] = per_dash.get(slug, 0) + 1

    for slug, n in sorted(per_dash.items(), key=lambda kv: -kv[1])[:12]:
        print(f"  {slug[:42]:44} {n:3} gap(s)", flush=True)

    if not apply:
        print(f"\nwould add {len(markers)} marker(s) across {len(per_dash)} dashboard(s)")
        print("(dry run — pass --apply)")
        return
    written = store.add_gap_markers(markers)
    print(f"\nadded {written} marker(s) across {len(per_dash)} dashboard(s)")


if __name__ == "__main__":
    sys.exit(main())
