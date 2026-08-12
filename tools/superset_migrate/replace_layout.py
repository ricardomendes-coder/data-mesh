"""Recompute tile positions for already-imported dashboards.

Cheaper than a full re-import when only the geometry was wrong: it re-reads each
Superset dashboard's layout and rewrites the grid, leaving the charts, filters
and tabs exactly as they are.

    python -m tools.superset_migrate.replace_layout
"""

import re
import sys

from sqlalchemy import text

from app import store

from . import translate as T
from .migrate import STAMP, q

DASH_RE = re.compile(r"Migrated from Superset dashboard #(\d+)")
CHART_RE = re.compile(r"Migrated from Superset chart #(\d+)")


def main() -> None:
    layouts = {d[0]: d[1] for d in q("SELECT id, position_json FROM dashboards")}

    with store.engine().connect() as c:
        dashboards = c.execute(
            text("SELECT slug, description FROM dashboards WHERE created_by = :s"),
            {"s": STAMP},
        ).all()

    total_placed = 0
    for slug, description in dashboards:
        m = DASH_RE.search(description or "")
        if not m:
            continue
        boxes = T.geometry(layouts.get(int(m.group(1)), ""))
        if not boxes:
            continue

        dash = store.get_dashboard(slug)
        if dash is None:
            continue

        # Text tiles keep the order they were inserted in, which is the order
        # markdown_blocks produced them — so they line up positionally.
        text_nodes = [k for k in boxes if k.startswith("node:")]
        text_items = [i for i in dash.items if i.kind == "text"]

        placements = []
        for item in dash.items:
            key = None
            if item.chart is not None:
                slice_match = CHART_RE.search(item.chart.description or "")
                if slice_match:
                    key = f"chart:{slice_match.group(1)}"
            elif item in text_items:
                index = text_items.index(item)
                if index < len(text_nodes):
                    key = text_nodes[index]
            box = boxes.get(key) if key else None
            if box:
                placements.append(
                    {
                        "id": item.id,
                        "x": box[0],
                        "y": box[1],
                        "w": box[2],
                        "h": box[3],
                        "section_id": item.section_id,
                    }
                )

        if placements:
            n = store.save_layout(slug, placements)
            total_placed += n
            print(f"  {slug[:44]:46} {n:3} tiles repositioned", flush=True)

    print(f"\nrepositioned {total_placed} tiles across {len(dashboards)} dashboards")


if __name__ == "__main__":
    sys.exit(main())
