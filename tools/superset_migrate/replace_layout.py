"""Recompute tile positions for already-imported dashboards.

Cheaper than a full re-import when only the geometry is wrong: it re-reads each
Superset dashboard's layout and rewrites the grid, leaving the charts, filters
and tabs exactly as they are.

    python -m tools.superset_migrate.replace_layout            # every import
    python -m tools.superset_migrate.replace_layout slug slug  # just these

Reads the layout through translate.layout_of — the same single traversal the
importer uses. It used to call the old geometry(), which rounded heights into a
coarser row, squeezed out blank bands and dropped dividers; running it would
have quietly undone the very thing it is meant to repair.
"""

import re
import sys
from collections import defaultdict, deque

from sqlalchemy import text

from app import store

from . import translate as T
from .migrate import STAMP, q

DASH_RE = re.compile(r"Migrated from Superset dashboard #(\d+)")
CHART_RE = re.compile(r"Migrated from Superset chart #(\d+)")


def _placements(dash, tiles, sections_by_title) -> list[dict]:
    """Match stored tiles to the blocks Superset draws, and box them.

    Charts match on the Superset slice id carried in their description. A chart
    can legitimately appear twice on one dashboard, so matches are consumed in
    order rather than looked up. Text matches on its content — order alone put
    a heading under the wrong tab when a block failed to import.
    """
    by_slice: dict[str, deque] = defaultdict(deque)
    by_text: dict[str, deque] = defaultdict(deque)
    dividers: deque = deque()
    for t in tiles:
        if t.kind == "chart":
            by_slice[str(t.chart_id)].append(t)
        elif t.kind == "divider":
            dividers.append(t)
        else:
            by_text[(t.content or "")[:2000]].append(t)

    out = []
    for item in dash.items:
        found = None
        if item.chart is not None:
            m = CHART_RE.search(item.chart.description or "")
            if m and by_slice[m.group(1)]:
                found = by_slice[m.group(1)].popleft()
        elif item.kind == "divider":
            found = dividers.popleft() if dividers else None
        else:
            queue = by_text.get(item.content or "")
            if queue:
                found = queue.popleft()
        if found is None:
            continue
        out.append(
            {
                "id": item.id,
                "x": found.x,
                "y": found.y,
                "w": found.w,
                "h": found.h,
                # The tab comes from the layout too: a tile moved to the wrong
                # tab would otherwise keep its wrong home and its new box.
                "section_id": sections_by_title.get(found.tab, item.section_id),
            }
        )
    return out


def main() -> None:
    only = {a for a in sys.argv[1:] if not a.startswith("-")}
    layouts = {d[0]: d[1] for d in q("SELECT id, position_json FROM dashboards")}

    with store.engine().connect() as c:
        dashboards = c.execute(
            text("SELECT slug, description FROM dashboards WHERE created_by = :s"),
            {"s": STAMP},
        ).all()

    total = touched = 0
    for slug, description in dashboards:
        if only and slug not in only:
            continue
        m = DASH_RE.search(description or "")
        if not m:
            continue
        tiles, _tabs = T.layout_of(layouts.get(int(m.group(1)), ""))
        if not tiles:
            continue
        dash = store.get_dashboard(slug)
        if dash is None:
            continue
        sections_by_title = {s.title: s.id for s in dash.sections}
        placements = _placements(dash, tiles, sections_by_title)
        if placements:
            n = store.save_layout(slug, placements)
            total += n
            touched += 1
            print(f"  {slug[:44]:46} {n:3} tiles repositioned", flush=True)

    print(f"\nrepositioned {total} tiles across {touched} dashboard(s)")


if __name__ == "__main__":
    sys.exit(main())
