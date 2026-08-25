"""Build the chart-preview snapshots the listings show, so a dashboard mosaic or
a chart card is a cache read on first view instead of a live warehouse query.

By default it warms the tiles the mosaics need — the first few chart tiles of
each dashboard. Pass --all to also warm every chart for the Charts listing.
Idempotent and gentle: an already-cached, unchanged preview is skipped, and it
pauses between queries, so it is safe to run off-hours after a data refresh.

Run it in the deployed environment (where the warehouse and report_hub are
configured); it uses the same preview builder the app does.

    python -m tools.warm_previews            # mosaic tiles only
    python -m tools.warm_previews --all      # every chart too
"""
import sys
import time

from app import store
from app.main import DASHBOARD_MOSAIC_TILES, _chart_preview

ALL = "--all" in sys.argv


def target_slugs():
    """Chart slugs to warm, mosaic tiles first, de-duplicated in order."""
    seen: set[str] = set()
    out: list[str] = []
    for d in store.list_dashboards():
        try:
            dash = store.get_dashboard(d.slug)
        except Exception:
            continue
        if not dash:
            continue
        # The exact tiles the mosaic shows: one coherent space (loose tiles, or
        # the first section that has charts), so warming matches what renders.
        ci = [it for it in dash.items if it.chart is not None]
        loose = [it for it in ci if it.section_id is None]
        if loose:
            group = loose
        else:
            first_section = ci[0].section_id if ci else None
            group = [it for it in ci if it.section_id == first_section]
        for it in group[:DASHBOARD_MOSAIC_TILES]:
            if it.chart.slug not in seen:
                seen.add(it.chart.slug)
                out.append(it.chart.slug)
    if ALL:
        for c in store.list_charts():
            if c.slug not in seen:
                seen.add(c.slug)
                out.append(c.slug)
    return out


def main():
    slugs = target_slugs()
    print(f"warming {len(slugs)} chart previews{' (all charts)' if ALL else ''}...", flush=True)
    built = cached = failed = 0
    for i, slug in enumerate(slugs, 1):
        chart = store.get_chart(slug)
        if chart is None:
            continue
        _payload, state = _chart_preview(chart)  # builds + caches when missing or stale
        if state == "cached":
            cached += 1
        elif state == "fresh":
            built += 1
        else:
            failed += 1
        if i % 20 == 0 or i == len(slugs):
            print(f"  {i}/{len(slugs)}  built={built} cached={cached} failed={failed}", flush=True)
        time.sleep(0.3)  # let the warehouse breathe between queries
    print(f"done: {built} built, {cached} already warm, {failed} failed", flush=True)


if __name__ == "__main__":
    main()
