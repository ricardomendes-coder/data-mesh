# Superset → Report Hub migration

Reads the Superset metadata database and rebuilds its dashboards in Report Hub:
charts, tabs, headings and filters.

## Running it

Needs a route to the internal Postgres instance (both the `superset` metadata
database and the warehouses live there), plus the app database in `.env`:

```bash
ssh -Nf -L 15432:<internal-endpoint>:5432 <bastion>

python -m tools.superset_migrate.migrate              # dry run — writes nothing
python -m tools.superset_migrate.migrate --apply      # import
python -m tools.superset_migrate.migrate --wipe       # undo a previous import
python -m tools.superset_migrate.migrate --no-pivot   # skip the slow series probing
```

Credentials come from `INTERNAL_ADMIN_USER` / `INTERNAL_ADMIN_PWD` (override the
host with `SUPERSET_READ_HOST` / `SUPERSET_READ_PORT`). The Superset side is
**read-only** — nothing is ever written back to it.

Everything created is stamped `created_by='superset-import'`, so `--wipe`
removes a run in full and `--apply` can then redo it. That's the intended loop
when this script improves.

## What it does

Superset stores a *spec* — dataset, dimensions, metrics, filters — and builds
SQL when it renders. Report Hub stores SQL. So this rebuilds the query Superset
would have generated:

| Superset | becomes |
| --- | --- |
| `echarts_timeseries_*`, `dist_bar`, `pie`, `mixed_timeseries`, … | a bar / line / area / pie chart |
| `table`, `pivot_table_v2`, `pivot_table` | a **table** tile |
| `big_number_total`, `big_number` | a **big number** tile |
| dashboard tabs | dashboard sections |
| markdown & header blocks | text tiles |
| `filter_select`, `filter_time` native filters | dashboard filters |

**Time series split by a category** are pivoted with conditional aggregation
(`SUM(x) FILTER (WHERE c_id = 'acme')`), because one flat result can't feed a
multi-series chart otherwise. Capped at 12 series — beyond that the chart is
unreadable and the chart is reported instead of imported.

**Every query carries a `{{ filters }}` token** on its WHERE clause. That's what
makes the imported charts filterable: a dashboard filter has to apply *inside*
the aggregation, and only the generator knows where that is. See
`app/filters.py`.

**A filter is only attached to charts whose dataset really has that column**,
checked against Superset's `table_columns`. Pointing a filter at a chart without
the column would just break that tile.

## What it won't do

- **Nothing is imported unless it runs.** Every query is `EXPLAIN`ed against the
  real database first — that resolves every column and plans the query without
  executing it. Failures are written to `failed.json` with the reason.
- Chart types with no Report Hub equivalent (treemap, gauge, sunburst, word
  cloud, bubble, country map, partition) are skipped.
- `filter_timegrain` and `filter_timecolumn` are skipped: they rewrite the
  GROUP BY rather than filtering rows, which the token can't express.
- Charts that are **already broken in Superset** — referencing a dropped table
  or column — fail the EXPLAIN and are reported. That's a finding, not a bug:
  they error in Superset too.
