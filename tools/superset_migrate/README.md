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
| divider blocks | divider tiles |
| `filter_select`, `filter_time` native filters | dashboard filters |

## The layout is copied, not approximated

`layout_of()` reads `position_json` **once** and returns every block with the
box Superset gives it. One traversal rather than three: `geometry()`,
`tabs_of()` and `markdown_blocks()` each used to walk the same tree and had to
agree with one another about what counted as a node and which tab it sat under.
They didn't — a node reachable by two paths was emitted twice, and 110
duplicate text blocks had to be deleted by hand after a run.

Coordinates stay in Superset's own units: `meta.width` in columns of twelve,
`meta.height` in units of 8px, one gutter between stacked blocks. Nothing is
rounded into a coarser row (of the 88 distinct heights in the V360 instance, 75
don't divide evenly by seven) and no blank space is squeezed out. `ROW` lays
its children across, `COLUMN` stacks them, and nesting — which reaches nine
levels deep here — keeps its offsets relative to the parent.

Two details that are easy to miss:

- **`sliceNameOverride`** — a dashboard can rename a chart for its own purposes,
  and 214 of the 1112 tiles do. That name is stored on the tile, not the chart,
  because the same chart can appear on two dashboards under two names.
- **Nested tabs** become `"Outer / Inner"`, so two tabs sharing a name in
  different parents stay apart in Report Hub's flat sections.

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
