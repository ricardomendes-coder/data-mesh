"""Warm the dashboard tile cache.

Runs every active dashboard's tiles once and stores the result, so the first
person to open a dashboard meets a warm cache instead of a cold 40-200s query.
Meant for a nightly cron right after the materialized views refresh: the robot
pays the cold cost at 3am so no one pays it at 9. Idempotent (a tile already
cached fresh is skipped) and throttled, so re-running only rebuilds what expired.

    python -m tools.warm_tiles
"""
import time
from datetime import datetime, timedelta

from app import filters, store
from app.config import get_settings
from app.main import _dashboard_filters, _looks_empty, _render_tiles, _tile_signature


def main():
    ttl = timedelta(minutes=max(0, get_settings().tile_cache_minutes))
    dashes = store.list_dashboards(include_inactive=False)
    print(f"aquecendo {len(dashes)} dashboards ativos...", flush=True)
    warmed = cached = failed = 0

    for di, d in enumerate(dashes, 1):
        try:
            dash = store.get_dashboard(d.slug)
        except Exception:
            continue
        if not dash:
            continue
        active = filters.resolve(_dashboard_filters(d.slug), {})  # the unfiltered view

        for it in dash.items:
            if it.chart is None:
                continue
            sig = _tile_signature(it, active)
            if not sig:
                continue

            # Skip a snapshot that is still fresh and matches the current chart.
            try:
                hit = store.get_tile_cache(it.id, sig)
            except Exception:
                hit = None
            if hit:
                payload, built_at = hit
                fresh = datetime.now(built_at.tzinfo) - built_at < ttl
                moved = it.chart.updated_at and it.chart.updated_at > built_at
                if fresh and not moved:
                    cached += 1
                    continue

            try:
                tile = _render_tiles([it], active)[0]
            except Exception:
                failed += 1
                continue
            if tile["error"]:
                failed += 1
                continue
            spec = tile["spec"]
            if _looks_empty(spec):
                continue

            payload = {
                "renders_as": spec.renders_as,
                "warnings": spec.warnings,
                "unfiltered": tile["unfiltered"],
            }
            if spec.renders_as == "canvas":
                payload["spec"] = spec.to_dict()
            elif spec.renders_as == "table":
                payload["columns"] = spec.columns
                payload["rows"] = [["" if v is None else str(v) for v in r] for r in spec.rows]
            else:
                payload["value"] = spec.value
                payload["caption"] = spec.caption

            try:
                store.put_tile_cache(it.id, sig, payload)
                warmed += 1
            except Exception:
                failed += 1
            time.sleep(0.4)  # let the warehouse breathe between heavy queries

        print(f"  {di}/{len(dashes)}  {d.title[:42]:42} | warmed={warmed} cached={cached} failed={failed}",
              flush=True)

    print(f"\npronto: {warmed} aquecidos, {cached} já quentes, {failed} falharam", flush=True)


if __name__ == "__main__":
    main()
