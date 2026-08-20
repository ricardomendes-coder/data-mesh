"""Turn a Superset chart spec into SQL that Report Hub can run.

Superset stores a *spec* — a dataset plus dimensions, metrics and filters — and
builds the query when it renders. Report Hub stores SQL. This rebuilds the query
Superset would have generated, for the shapes that actually occur in the V360
instance; anything else raises Unsupported rather than being guessed at.

Every generated query ends its WHERE clause with a `{{ filters }}` token so the
dashboard's filters have somewhere to land (see app/filters.py). That placement
is the whole reason the migration is worth doing in SQL rather than by hand: the
filter has to go *inside* the aggregation, and only the generator knows where.
"""

import json
import re
from dataclasses import dataclass

FILTER_TOKEN = "{{ filters }}"

GRAIN = {
    "PT1S": "second",
    "PT1M": "minute",
    "PT1H": "hour",
    "P1D": "day",
    "P1W": "week",
    "P1M": "month",
    "P3M": "quarter",
    "P1Y": "year",
    "P0.25Y": "quarter",
    "P1DT0H": "day",
}

# Superset viz_type -> Report Hub chart_type.
VIZ = {
    "echarts_timeseries_line": "line",
    "echarts_timeseries": "line",
    "echarts_timeseries_smooth": "line",
    "line": "line",
    "dual_line": "line",
    "mixed_timeseries": "line",
    "echarts_area": "area",
    "area": "area",
    "echarts_timeseries_bar": "bar",
    "dist_bar": "bar",
    "bar": "bar",
    "pie": "pie",
    # Tabular and single-value charts, which Report Hub now has tiles for.
    "table": "table",
    "pivot_table_v2": "table",
    "pivot_table": "table",
    "time_table": "table",
    "big_number_total": "number",
    "big_number": "number",
}

OPS = {
    "==": "=",
    "EQUALS": "=",
    "!=": "<>",
    "NOT_EQUALS": "<>",
    ">": ">",
    "GREATER_THAN": ">",
    "<": "<",
    "LESS_THAN": "<",
    ">=": ">=",
    "GREATER_THAN_OR_EQUAL": ">=",
    "<=": "<=",
    "LESS_THAN_OR_EQUAL": "<=",
}


class Unsupported(Exception):
    """This chart can't be faithfully rebuilt as one SQL query."""


def qi(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def lit(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _column_sql(entry, calc: dict | None = None) -> tuple[str, str]:
    """SQL and label for a dimension. `calc` maps a Superset calculated column
    (a CASE-WHEN etc. defined in the tool, not the table) to its expression, so
    a groupby on one of those expands instead of pointing at a column that
    doesn't physically exist — 82 of the failed charts referenced one."""
    calc = calc or {}
    if isinstance(entry, str):
        if entry in calc:
            return f"({calc[entry]})", entry
        return qi(entry), entry
    if isinstance(entry, dict):
        if entry.get("sqlExpression"):
            return entry["sqlExpression"], entry.get("label") or "expr"
        if entry.get("column_name"):
            name = entry["column_name"]
            expr = f"({calc[name]})" if name in calc else qi(name)
            return expr, entry.get("label") or name
    raise Unsupported(f"unrecognised column entry: {entry!r}")


def _metric_sql(metric, saved: dict[str, str], calc: dict | None = None) -> tuple[str, str]:
    calc = calc or {}
    if isinstance(metric, str):
        if metric in saved:
            return saved[metric], metric
        # Superset's built-in COUNT(*) metric, named "count", lives nowhere in
        # sql_metrics — it's implicit. Very common; without it the chart reads
        # as "no metric".
        if metric.lower() == "count":
            return "COUNT(*)", "count"
        raise Unsupported(f"unknown saved metric {metric!r}")
    if not isinstance(metric, dict):
        raise Unsupported(f"unrecognised metric: {metric!r}")
    label = metric.get("label") or "value"
    kind = metric.get("expressionType")
    if kind == "SQL":
        expression = metric.get("sqlExpression")
        if not expression:
            raise Unsupported("SQL metric with no expression")
        return expression, label
    if kind == "SIMPLE" or (metric.get("aggregate") and metric.get("column")):
        agg = (metric.get("aggregate") or "").upper()
        name = (metric.get("column") or {}).get("column_name")
        if not agg or not name:
            raise Unsupported("incomplete simple metric")
        col = f"({calc[name]})" if name in calc else qi(name)
        if agg == "COUNT_DISTINCT":
            return f"COUNT(DISTINCT {col})", label
        if agg not in ("COUNT", "SUM", "AVG", "MIN", "MAX"):
            raise Unsupported(f"unsupported aggregate {agg!r}")
        return f"{agg}({col})", label
    raise Unsupported(f"unsupported metric expressionType {kind!r}")


def _filter_sql(flt, calc: dict | None = None) -> str | None:
    calc = calc or {}
    if not isinstance(flt, dict):
        return None
    def col(name):
        return f"({calc[name]})" if name in calc else qi(name)
    if (flt.get("clause") or "WHERE").upper() != "WHERE":
        return None
    if flt.get("expressionType") == "SQL":
        expression = flt.get("sqlExpression")
        return f"({expression})" if expression else None
    subject, operator = flt.get("subject"), flt.get("operator")
    if not subject or not operator:
        return None
    comparator, op = flt.get("comparator"), operator.upper()
    if op in ("IS NULL", "IS_NULL"):
        return f"{col(subject)} IS NULL"
    if op in ("IS NOT NULL", "IS_NOT_NULL"):
        return f"{col(subject)} IS NOT NULL"
    if op in ("IN", "NOT IN", "NOT_IN"):
        values = comparator if isinstance(comparator, list) else [comparator]
        values = [v for v in values if v is not None]
        if not values:
            return None
        negate = "NOT " if op.startswith("NOT") else ""
        return f"{col(subject)} {negate}IN ({', '.join(lit(v) for v in values)})"
    if op in ("LIKE", "ILIKE"):
        return f"{col(subject)} {op} {lit(comparator)}"
    if op in OPS and comparator is not None:
        return f"{col(subject)} {OPS[op]} {lit(comparator)}"
    return None


def source_sql(dataset: dict) -> str:
    if dataset.get("sql") and dataset["sql"].strip():
        return "(\n" + dataset["sql"].strip().rstrip(";") + "\n) AS src"
    schema = dataset.get("schema") or "public"
    return f"{qi(schema)}.{qi(dataset['table_name'])}"


def _assemble(select, source, where, group_terms, order, limit) -> str:
    """Build the statement, always leaving a place for dashboard filters."""
    sql = "SELECT " + ",\n       ".join(select)
    sql += f"\n  FROM {source}"
    # The token goes on the WHERE clause even when there is nothing to filter
    # yet — `WHERE 1=1 {{ filters }}` is what makes the chart filterable later.
    sql += "\n WHERE " + ("\n   AND ".join(where) if where else "1=1")
    sql += f"\n   {FILTER_TOKEN}"
    if group_terms:
        sql += "\n GROUP BY " + ", ".join(group_terms)
    if order:
        sql += f"\n ORDER BY {order}"
    if limit:
        sql += f"\n LIMIT {limit}"
    return sql


def translate(
    slice_row: dict, dataset: dict, saved_metrics: dict[str, str], series_values=None,
    calc_columns: dict | None = None,
) -> dict:
    params = json.loads(slice_row["params"] or "{}")
    calc = calc_columns or {}
    viz = slice_row["viz_type"]
    if viz not in VIZ:
        raise Unsupported(f"viz_type {viz!r} has no Report Hub equivalent")
    kind = VIZ[viz]

    limit = params.get("row_limit") or 10000
    try:
        limit = max(1, min(int(limit), 10000))
    except (TypeError, ValueError):
        limit = 10000

    where = [f for f in (_filter_sql(f, calc) for f in (params.get("adhoc_filters") or [])) if f]
    source = source_sql(dataset)

    # ── raw table mode: columns straight through, no aggregation ──
    raw_columns = params.get("all_columns") or []
    if kind == "table" and (params.get("query_mode") == "raw" or raw_columns):
        if not raw_columns:
            raise Unsupported("raw table with no columns")
        select, labels = [], []
        for entry in raw_columns:
            expression, label = _column_sql(entry, calc)
            select.append(f"{expression} AS {qi(label)}")
            labels.append(label)
        sql = _assemble(select, source, where, [], None, limit)
        return {
            "sql": sql,
            "chart_type": "table",
            "x_column": labels[0],
            "y_columns": labels,
            "needs_series": False,
            "series_expr": None,
            "where": where,
        }

    # ── metrics ──
    raw_metrics = params.get("metrics")
    if raw_metrics is None and params.get("metric") is not None:
        raw_metrics = [params["metric"]]
    if isinstance(raw_metrics, (str, dict)):
        raw_metrics = [raw_metrics]
    if not raw_metrics:
        # A table can be just its grouped dimensions — "the distinct combos of
        # these columns", with no aggregate. Only a table: a bar/line with no
        # measure is meaningless. 48 migrated tables are this shape.
        if kind == "table":
            raw_metrics = []
        else:
            raise Unsupported("chart has no metric")
    metrics = [_metric_sql(m, saved_metrics, calc) for m in raw_metrics]

    # ── a single number: one metric, no dimension ──
    if kind == "number":
        expression, label = metrics[0]
        select = [f"{expression} AS {qi(label)}"]
        sql = _assemble(select, source, where, [], None, 1)
        return {
            "sql": sql,
            "chart_type": "number",
            "x_column": "",
            "y_columns": [label],
            "needs_series": False,
            "series_expr": None,
            "where": where,
        }

    # ── dimensions ──
    time_expr = time_label = None
    grain = GRAIN.get(params.get("time_grain_sqla") or "")
    x_axis = params.get("x_axis")
    if x_axis:
        expression, label = _column_sql(x_axis, calc)
        time_expr = f"date_trunc('{grain}', {expression})" if grain else expression
        time_label = label
    elif params.get("granularity_sqla"):
        col = params["granularity_sqla"]
        if isinstance(col, str) and col.strip():
            gcol = f"({calc[col]})" if col in calc else qi(col)
            time_expr = f"date_trunc('{grain}', {gcol})" if grain else gcol
            time_label = col

    groupby = [g for g in (params.get("groupby") or []) if g]
    # A pivot table's rows and columns are both just dimensions once flattened.
    for key in ("groupbyRows", "groupbyColumns"):
        groupby += [g for g in (params.get(key) or []) if g]
    dims = [_column_sql(g, calc) for g in groupby]
    for extra in params.get("columns") or []:
        if extra:
            dims.append(_column_sql(extra, calc))

    if viz == "pie":
        time_expr = None
    # A table shows time as a plain column: it has no axis to pivot around.
    # Skipped when that column is already a dimension, or the table gets the
    # same header twice — Superset hides the duplicate, a plain grid can't.
    if kind == "table" and time_expr:
        if time_label not in {label for _, label in dims}:
            dims.insert(0, (time_expr, time_label))
        time_expr = None

    if not time_expr and not dims:
        raise Unsupported("chart has no dimension to plot against")

    # ── time + series: LONG format (one row per time × series) ──
    #
    # This used to pivot each series into its own column with a FILTER, which
    # meant probing the distinct series values first, capping at 12, and giving
    # up on any metric a regex couldn't rewrite (ratios, especially). Long
    # format sidesteps all of it: emit (time, series, value) rows and let the
    # renderer group them, with a scrollable legend. No cap, no probe, and the
    # value can be any expression — so ratios and two-way splits just work.
    if time_expr and dims and kind != "table":
        if len(metrics) != 1:
            # One value column per row; several measures don't fit long format.
            raise Unsupported("time+series chart with multiple metrics")
        # Two split dimensions collapse into one series key ("A / B"), which is
        # how you'd read them off a legend anyway.
        if len(dims) == 1:
            series_expr, series_label = dims[0]
        else:
            parts = ", ".join(f"({e})::text" for e, _ in dims)
            series_expr = f"concat_ws(' / ', {parts})"
            series_label = " / ".join(lbl for _, lbl in dims)
        agg, value_label = metrics[0]
        select = [
            f"{time_expr} AS {qi(time_label)}",
            f"{series_expr} AS {qi(series_label)}",
            f"{agg} AS {qi(value_label)}",
        ]
        # No row cap here: the grouped result is (distinct time × distinct
        # series), and truncating it would drop whole series. build_spec bounds
        # what actually gets drawn.
        sql = _assemble(select, source, where, [time_expr, series_expr], "1 ASC", None)
        return {
            "sql": sql,
            "chart_type": kind,
            "x_column": time_label,
            "y_columns": [value_label],
            "series_column": series_label,
            "needs_series": False,
            "series_expr": None,
            "where": where,
        }

    # ── one dimension (or a flat table) ──
    select, group_terms, y_columns = [], [], []
    if time_expr:
        select.append(f"{time_expr} AS {qi(time_label)}")
        group_terms.append(time_expr)
        x_column = time_label
    else:
        expression, label = dims[0]
        select.append(f"{expression} AS {qi(label)}")
        group_terms.append(expression)
        x_column = label
        for expression, label in dims[1:]:
            select.append(f"{expression} AS {qi(label)}")
            group_terms.append(expression)
    for expression, label in metrics:
        select.append(f"{expression} AS {qi(label)}")
        y_columns.append(label)

    order = (
        "1 ASC"
        if time_expr
        else f"{len(select)} {'DESC' if params.get('order_desc', True) else 'ASC'}"
    )
    sql = _assemble(select, source, where, group_terms, order, limit)
    return {
        "sql": sql,
        "chart_type": kind,
        "x_column": x_column,
        "y_columns": y_columns,
        "needs_series": False,
        "series_expr": None,
        "where": where,
    }


# ── dashboard layout ────────────────────────────────────────────────────────


def tabs_of(position_json: str) -> tuple[dict[int, str], list[str]]:
    """(chart id -> tab title, tab titles in order) from a dashboard layout.

    Superset's position_json is a flat map of node id -> node, each with its
    children. Walking down from the root and remembering the nearest enclosing
    TAB is enough to tell which tab a chart sits on.
    """
    try:
        nodes = json.loads(position_json or "{}")
    except Exception:
        return {}, []
    if not isinstance(nodes, dict):
        return {}, []

    order: list[str] = []
    of_chart: dict[int, str] = {}

    def walk(node_id: str, tab: str | None):
        node = nodes.get(node_id)
        if not isinstance(node, dict):
            return
        kind = node.get("type")
        meta = node.get("meta") or {}
        if kind == "TAB":
            tab = (meta.get("text") or meta.get("defaultText") or "Tab").strip() or "Tab"
            if tab not in order:
                order.append(tab)
        elif kind == "CHART":
            chart_id = meta.get("chartId")
            if isinstance(chart_id, int) and tab:
                of_chart[chart_id] = tab
        for child in node.get("children") or []:
            walk(child, tab)

    # Exactly one root. ROOT_ID *contains* GRID_ID, so walking both visits every
    # node twice — which for geometry() meant the cursor accumulated a second
    # time and every tile landed a full dashboard-height below where it belongs.
    root = "ROOT_ID" if "ROOT_ID" in nodes else "GRID_ID"
    if root in nodes:
        walk(root, None)
    # Some layouts hang tabs off the header rather than the grid.
    for node_id, node in nodes.items():
        if isinstance(node, dict) and node.get("type") == "TABS":
            walk(node_id, None)
    return of_chart, order


# Superset's grid is 12 columns wide and its height unit is 8px; Report Hub's
# row is 56px. Dividing by 7 converts one to the other, so a chart Superset drew
# at height 50 (400px) becomes 7 rows (392px) — the same size on screen.
SUPERSET_ROW_UNITS = 7
DEFAULT_SUPERSET_HEIGHT = 50


def geometry(position_json: str) -> dict:
    """node key -> (x, y, w, h) on a 12-column grid, mirroring the original.

    Superset stores a tree of TABS > TAB > ROW > CHART, where a ROW lays its
    children out left to right and each child carries its own width. Walking it
    in order and accumulating x within a row, y between rows, reproduces the
    layout exactly — which is the point: an imported dashboard should look like
    the one people already know.

    Keys are `chart:<id>` for charts and `node:<id>` for markdown and headers,
    because a markdown block has no chart id to key on.
    """
    try:
        nodes = json.loads(position_json or "{}")
    except Exception:
        return {}
    if not isinstance(nodes, dict):
        return {}

    out: dict[str, tuple[int, int, int, int]] = {}
    # y is tracked per tab: every tab starts at the top of its own pane.
    cursor: dict[str | None, int] = {}

    def key_of(node_id: str, node: dict) -> str | None:
        meta = node.get("meta") or {}
        if node.get("type") == "CHART" and isinstance(meta.get("chartId"), int):
            return f"chart:{meta['chartId']}"
        if node.get("type") in ("MARKDOWN", "HEADER"):
            return f"node:{node_id}"
        return None

    def height_units(meta: dict) -> int:
        raw = meta.get("height") or DEFAULT_SUPERSET_HEIGHT
        try:
            return max(1, round(float(raw) / SUPERSET_ROW_UNITS))
        except (TypeError, ValueError):
            return 7

    def walk(node_id: str, tab: str | None):
        node = nodes.get(node_id)
        if not isinstance(node, dict):
            return
        kind = node.get("type")
        meta = node.get("meta") or {}
        children = node.get("children") or []

        if kind == "TAB":
            tab = (meta.get("text") or meta.get("defaultText") or "Tab").strip() or "Tab"
            cursor.setdefault(tab, 0)
            for child in children:
                walk(child, tab)
            return

        if kind == "ROW":
            x = 0
            y = cursor.get(tab, 0)
            tallest = 0
            for child in children:
                child_node = nodes.get(child)
                if not isinstance(child_node, dict):
                    continue
                child_meta = child_node.get("meta") or {}
                width = child_meta.get("width") or 4
                try:
                    width = max(1, min(12, int(width)))
                except (TypeError, ValueError):
                    width = 4
                # Text is stored short on purpose. Superset reserves a tall
                # block for markdown; we render it in a line or two and let the
                # tile size to its content, so keeping the original height would
                # only reserve rows that stay empty. The stored value is what
                # decides occupancy, and therefore what _compact() can reclaim.
                if child_node.get("type") in ("MARKDOWN", "HEADER"):
                    height = 2
                else:
                    height = height_units(child_meta)
                # A row wider than the grid wraps rather than overflowing.
                if x + width > 12:
                    x = 0
                    y += tallest or height
                    tallest = 0
                child_key = key_of(child, child_node)
                if child_key:
                    out[child_key] = (x, y, width, height)
                x += width
                tallest = max(tallest, height)
                # Nested rows/columns: recurse so nothing is silently dropped.
                if child_node.get("type") not in ("CHART", "MARKDOWN", "HEADER"):
                    walk(child, tab)
            cursor[tab] = y + (tallest or 1)
            return

        for child in children:
            walk(child, tab)

    cursor.setdefault(None, 0)
    # Exactly one root. ROOT_ID *contains* GRID_ID, so walking both visits every
    # node twice — which for geometry() meant the cursor accumulated a second
    # time and every tile landed a full dashboard-height below where it belongs.
    root = "ROOT_ID" if "ROOT_ID" in nodes else "GRID_ID"
    if root in nodes:
        walk(root, None)
    return _compact(out)


def _compact(boxes: dict) -> dict:
    """Squeeze out bands of empty rows, keeping every relative position.

    Superset reserves generous height for markdown and headers; Report Hub
    renders those far more compactly, which leaves a dead band where the text
    used to be. Collapsing runs of unoccupied rows to a single blank row keeps
    side-by-side tiles together and the reading order intact, while removing
    space that only existed because the original block was taller.
    """
    if not boxes:
        return boxes
    occupied: set[int] = set()
    for _x, y, _w, h in boxes.values():
        occupied.update(range(y, y + max(1, h)))

    mapping: dict[int, int] = {}
    cursor = 0
    blank_run = 0
    for row in range(max(occupied) + 1):
        if row in occupied:
            mapping[row] = cursor
            cursor += 1
            blank_run = 0
        else:
            blank_run += 1
            if blank_run == 1:  # keep one row of breathing space, drop the rest
                cursor += 1
    return {key: (x, mapping.get(y, y), w, h) for key, (x, y, w, h) in boxes.items()}


# ── the layout, read once ──────────────────────────────────────────────────
#
# Superset's own grid constants. Mirroring the numbers is the entire point:
# meta.width is in columns of twelve, meta.height in units of GRID_BASE_UNIT,
# and stacked blocks are separated by one gutter. Keeping those units instead
# of rounding them into a coarser row is what makes an imported dashboard the
# same dashboard rather than one that merely resembles it.

GRID_COLUMNS = 12
GRID_BASE_UNIT = 8  # px per height unit — the renderer uses the same number
GRID_GUTTER_UNITS = 2  # the 16px between stacked blocks, in height units

DEFAULT_CHART_HEIGHT = 50
DEFAULT_WIDTH = 4
# Headers and dividers carry no height of their own; Superset gives them a
# fixed band that depends only on the heading size.
HEADER_UNITS = {"SMALL_HEADER": 4, "MEDIUM_HEADER": 5, "LARGE_HEADER": 6}
HEADER_LEVEL = {"SMALL_HEADER": 3, "MEDIUM_HEADER": 2, "LARGE_HEADER": 1}
DEFAULT_HEADER_UNITS = 5
DIVIDER_UNITS = 2

CONTAINERS = ("ROOT", "GRID", "TABS", "TAB", "ROW", "COLUMN")


@dataclass
class Tile:
    """One block of a dashboard, where Superset draws it."""

    kind: str  # chart | text | divider
    node_id: str
    tab: str | None
    x: int
    y: int
    w: int
    h: int
    chart_id: int | None = None
    title: str = ""
    content: str = ""


# Why a chart didn't make it, in words a reader of the dashboard can use. The
# raw reason is a psycopg2 traceback or an internal phrase; the dashboard shows
# the short form, translated, in the hole the chart left behind.
MISSING_REASONS = (
    ("UndefinedColumn", "a column it reads no longer exists"),
    ("UndefinedTable", "a table it reads no longer exists"),
    ("too many series", "too many series to draw"),
    ("more than one series dimension", "it is split two ways at once"),
    ("no metric", "no measure could be recognised"),
    ("multiple metrics", "several measures on one time series"),
    ("cannot pivot", "its measure cannot be split into series"),
    ("viz_type", "this chart type has no equivalent yet"),
    ("did not plan", "its query no longer runs"),
)


def missing_reason(raw: str) -> str:
    """One short phrase for why a chart is absent. Falls back to the generic."""
    for needle, phrase in MISSING_REASONS:
        if needle in (raw or ""):
            return phrase
    return "it could not be translated"


def layout_of(position_json: str) -> tuple[list[Tile], list[str]]:
    """Every block Superset draws, at Superset's coordinates. One traversal.

    geometry(), tabs_of() and markdown_blocks() each walked this same tree and
    had to agree with one another about what counted as a node and which tab it
    sat under. They didn't: a node reachable by two paths was emitted twice, and
    110 duplicate text blocks had to be deleted by hand afterwards. The tree is
    the dashboard, so it gets read once and answers every question.

    Returns the tiles in reading order and the tab names in the order Superset
    shows them. Nested tabs come back as "Outer / Inner" — Report Hub's sections
    are flat, and a path keeps two tabs that share a name apart.
    """
    try:
        nodes = json.loads(position_json or "{}")
    except Exception:
        return [], []
    if not isinstance(nodes, dict):
        return [], []

    tiles: list[Tile] = []
    tab_order: list[str] = []
    seen: set[str] = set()

    def width_of(node: dict, available: int) -> int:
        """How many columns this child takes of the space it was handed."""
        # A header or a divider has no width of its own: it spans its parent.
        if node.get("type") in ("HEADER", "DIVIDER"):
            return available
        try:
            want = int((node.get("meta") or {}).get("width") or DEFAULT_WIDTH)
        except (TypeError, ValueError):
            want = DEFAULT_WIDTH
        return max(1, min(available, want))

    def height_of(kind: str, meta: dict) -> int:
        if kind == "DIVIDER":
            return DIVIDER_UNITS
        if kind == "HEADER":
            return HEADER_UNITS.get(meta.get("headerSize"), DEFAULT_HEADER_UNITS)
        try:
            return max(1, int(meta.get("height") or DEFAULT_CHART_HEIGHT))
        except (TypeError, ValueError):
            return DEFAULT_CHART_HEIGHT

    def stack(children: list, tab: str | None, x: int, y: int, width: int) -> int:
        """Children one under another, gutter between. Returns height used."""
        cursor = y
        for child in children:
            used = place(child, tab, x, cursor, width)
            if used:
                cursor += used + GRID_GUTTER_UNITS
        return max(0, cursor - y - GRID_GUTTER_UNITS)

    def place(node_id: str, tab: str | None, x: int, y: int, width: int) -> int:
        node = nodes.get(node_id)
        if not isinstance(node, dict):
            return 0
        kind = node.get("type")
        meta = node.get("meta") or {}
        children = node.get("children") or []

        if kind in ("ROOT", "GRID"):
            return stack(children, tab, x, y, width)

        if kind == "TABS":
            # Every pane starts at the top of its own tab, so a TABS block
            # takes no room in the flow its parent is laying out.
            for child in children:
                place(child, tab, x, y, width)
            return 0

        if kind == "TAB":
            name = str(meta.get("text") or meta.get("defaultText") or "Tab").strip() or "Tab"
            path = f"{tab} / {name}" if tab else name
            if path not in tab_order:
                tab_order.append(path)
            stack(children, path, 0, 0, GRID_COLUMNS)
            return 0

        if kind == "ROW":
            cursor_x, row_y, tallest = x, y, 0
            for child in children:
                child_node = nodes.get(child)
                if not isinstance(child_node, dict):
                    continue
                cw = width_of(child_node, width)
                # A row wider than the grid wraps instead of overflowing.
                if cursor_x > x and cursor_x + cw > x + width:
                    cursor_x = x
                    row_y += tallest + GRID_GUTTER_UNITS
                    tallest = 0
                tallest = max(tallest, place(child, tab, cursor_x, row_y, cw))
                cursor_x += cw
            return (row_y - y) + tallest

        if kind == "COLUMN":
            try:
                own = int(meta.get("width") or width)
            except (TypeError, ValueError):
                own = width
            return stack(children, tab, x, y, max(1, min(width, own)))

        # ── leaves ──
        if node_id in seen:
            # The same node reached twice is a bug in the walk, not content.
            return 0
        height = height_of(kind, meta)

        if kind == "CHART":
            chart_id = meta.get("chartId")
            if not isinstance(chart_id, int):
                return height
            seen.add(node_id)
            tiles.append(
                Tile(
                    kind="chart",
                    node_id=node_id,
                    tab=tab,
                    x=x,
                    y=y,
                    w=width,
                    h=height,
                    chart_id=chart_id,
                    # The dashboard may rename a chart for its own purposes;
                    # 214 of them do. That name is the one people read here.
                    title=str(meta.get("sliceNameOverride") or meta.get("sliceName") or "").strip(),
                )
            )
            return height

        if kind == "MARKDOWN":
            seen.add(node_id)
            tiles.append(
                Tile(
                    kind="text",
                    node_id=node_id,
                    tab=tab,
                    x=x,
                    y=y,
                    w=width,
                    h=height,
                    content=str(meta.get("code") or ""),
                )
            )
            return height

        if kind == "HEADER":
            text_ = str(meta.get("text") or "").strip()
            if not text_:
                return height
            seen.add(node_id)
            level = HEADER_LEVEL.get(meta.get("headerSize"), 2)
            tiles.append(
                Tile(
                    kind="text",
                    node_id=node_id,
                    tab=tab,
                    x=x,
                    y=y,
                    w=width,
                    h=height,
                    content=f"{'#' * level} {text_}",
                )
            )
            return height

        if kind == "DIVIDER":
            seen.add(node_id)
            tiles.append(
                Tile(kind="divider", node_id=node_id, tab=tab, x=x, y=y, w=width, h=height)
            )
            return height

        return 0

    # ROOT_ID contains GRID_ID, so walking both visits every node twice.
    root = "ROOT_ID" if "ROOT_ID" in nodes else "GRID_ID"
    if root in nodes:
        place(root, None, 0, 0, GRID_COLUMNS)
    return tiles, tab_order


def markdown_blocks(position_json: str) -> list[tuple[str | None, str, str]]:
    """(tab title, text, node id) for each markdown/header block, in order.

    The node id is what `geometry()` keys those blocks by, so the importer can
    place them exactly where Superset had them.
    """
    try:
        nodes = json.loads(position_json or "{}")
    except Exception:
        return []
    if not isinstance(nodes, dict):
        return []
    out: list[tuple[str | None, str, str]] = []
    seen: set[str] = set()

    def walk(node_id: str, tab: str | None):
        node = nodes.get(node_id)
        if not isinstance(node, dict):
            return
        kind = node.get("type")
        meta = node.get("meta") or {}
        if kind == "TAB":
            tab = (meta.get("text") or meta.get("defaultText") or "Tab").strip() or "Tab"
        elif kind in ("MARKDOWN", "HEADER"):
            text_value = (meta.get("code") or meta.get("text") or "").strip()
            # A node reachable from two branches would otherwise be emitted
            # twice and land as two stacked copies on the dashboard.
            if text_value and node_id not in seen:
                seen.add(node_id)
                out.append((tab, text_value, node_id))
        for child in node.get("children") or []:
            walk(child, tab)

    # Exactly one root. ROOT_ID *contains* GRID_ID, so walking both visits every
    # node twice — which for geometry() meant the cursor accumulated a second
    # time and every tile landed a full dashboard-height below where it belongs.
    root = "ROOT_ID" if "ROOT_ID" in nodes else "GRID_ID"
    if root in nodes:
        walk(root, None)
    return out


# ── native filters ──────────────────────────────────────────────────────────

# Superset filter type -> Report Hub filter type. The ones left out change the
# shape of the query rather than filtering it (a time *grain* or time *column*
# picker rewrites the GROUP BY), which the token mechanism can't express.
FILTER_KIND = {
    "filter_select": "select",
    "filter_time": "daterange",
    "filter_range": "text",
}


def _slug_key(name: str, taken: set[str]) -> str:
    key = re.sub(r"[^a-z0-9_]+", "_", (name or "filter").strip().lower()).strip("_")
    key = re.sub(r"_+", "_", key) or "filter"
    if not re.match(r"^[a-z]", key):
        key = "f_" + key
    key = key[:40]
    candidate, n = key, 2
    while candidate in taken:
        candidate = f"{key[:37]}_{n}"
        n += 1
    taken.add(candidate)
    return candidate


def filters_of(json_metadata: str) -> list[dict]:
    """Report-Hub-shaped filter definitions from a dashboard's native filters."""
    try:
        meta = json.loads(json_metadata or "{}")
    except Exception:
        return []
    out, taken = [], set()
    for f in meta.get("native_filter_configuration") or []:
        if not isinstance(f, dict):
            continue
        kind = FILTER_KIND.get(f.get("filterType"))
        if kind is None:
            continue
        targets = f.get("targets") or []
        column = None
        if targets and isinstance(targets[0], dict):
            column = (targets[0].get("column") or {}).get("name")
        if not column:
            continue
        name = f.get("name") or column
        default = ""
        mask = ((f.get("defaultDataMask") or {}).get("filterState") or {}).get("value")
        if isinstance(mask, list):
            # JSON null arrives as Python None, and str(None) is the literal
            # "None" — a value Postgres then tries to read. Against a boolean
            # column that is a hard error, and because defaults apply on
            # arrival it failed every tile the filter scoped, on every visit:
            #   invalid input syntax for type boolean: "None"
            # An absent default is absent, not the word for it.
            mask = next((v for v in mask if v is not None), None)
        if isinstance(mask, (str, int, float)):  # bool included, deliberately
            default = str(mask)
        scope = f.get("scope") or {}
        out.append(
            {
                "key": _slug_key(name, taken),
                "label": str(name)[:120],
                "filter_type": kind,
                "column_expr": column,
                "default_value": default,
                "excluded": [c for c in (scope.get("excluded") or []) if isinstance(c, int)],
                "superset_id": f.get("id"),
            }
        )
    return out
