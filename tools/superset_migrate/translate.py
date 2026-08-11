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

FILTER_TOKEN = "{{ filters }}"

GRAIN = {
    "PT1S": "second", "PT1M": "minute", "PT1H": "hour",
    "P1D": "day", "P1W": "week", "P1M": "month",
    "P3M": "quarter", "P1Y": "year", "P0.25Y": "quarter", "P1DT0H": "day",
}

# Superset viz_type -> Report Hub chart_type.
VIZ = {
    "echarts_timeseries_line": "line", "echarts_timeseries": "line",
    "echarts_timeseries_smooth": "line", "line": "line", "dual_line": "line",
    "mixed_timeseries": "line",
    "echarts_area": "area", "area": "area",
    "echarts_timeseries_bar": "bar", "dist_bar": "bar", "bar": "bar",
    "pie": "pie",
    # Tabular and single-value charts, which Report Hub now has tiles for.
    "table": "table", "pivot_table_v2": "table", "pivot_table": "table",
    "time_table": "table",
    "big_number_total": "number", "big_number": "number",
}

OPS = {
    "==": "=", "EQUALS": "=", "!=": "<>", "NOT_EQUALS": "<>",
    ">": ">", "GREATER_THAN": ">", "<": "<", "LESS_THAN": "<",
    ">=": ">=", "GREATER_THAN_OR_EQUAL": ">=",
    "<=": "<=", "LESS_THAN_OR_EQUAL": "<=",
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


def _column_sql(entry) -> tuple[str, str]:
    if isinstance(entry, str):
        return qi(entry), entry
    if isinstance(entry, dict):
        if entry.get("sqlExpression"):
            return entry["sqlExpression"], entry.get("label") or "expr"
        if entry.get("column_name"):
            return qi(entry["column_name"]), entry.get("label") or entry["column_name"]
    raise Unsupported(f"unrecognised column entry: {entry!r}")


def _metric_sql(metric, saved: dict[str, str]) -> tuple[str, str]:
    if isinstance(metric, str):
        if metric in saved:
            return saved[metric], metric
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
        if agg == "COUNT_DISTINCT":
            return f"COUNT(DISTINCT {qi(name)})", label
        if agg not in ("COUNT", "SUM", "AVG", "MIN", "MAX"):
            raise Unsupported(f"unsupported aggregate {agg!r}")
        return f"{agg}({qi(name)})", label
    raise Unsupported(f"unsupported metric expressionType {kind!r}")


def _filter_sql(flt) -> str | None:
    if not isinstance(flt, dict):
        return None
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
        return f"{qi(subject)} IS NULL"
    if op in ("IS NOT NULL", "IS_NOT_NULL"):
        return f"{qi(subject)} IS NOT NULL"
    if op in ("IN", "NOT IN", "NOT_IN"):
        values = comparator if isinstance(comparator, list) else [comparator]
        values = [v for v in values if v is not None]
        if not values:
            return None
        negate = "NOT " if op.startswith("NOT") else ""
        return f"{qi(subject)} {negate}IN ({', '.join(lit(v) for v in values)})"
    if op in ("LIKE", "ILIKE"):
        return f"{qi(subject)} {op} {lit(comparator)}"
    if op in OPS and comparator is not None:
        return f"{qi(subject)} {OPS[op]} {lit(comparator)}"
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


def translate(slice_row: dict, dataset: dict, saved_metrics: dict[str, str],
              series_values=None) -> dict:
    params = json.loads(slice_row["params"] or "{}")
    viz = slice_row["viz_type"]
    if viz not in VIZ:
        raise Unsupported(f"viz_type {viz!r} has no Report Hub equivalent")
    kind = VIZ[viz]

    limit = params.get("row_limit") or 10000
    try:
        limit = max(1, min(int(limit), 10000))
    except (TypeError, ValueError):
        limit = 10000

    where = [f for f in (_filter_sql(f) for f in (params.get("adhoc_filters") or [])) if f]
    source = source_sql(dataset)

    # ── raw table mode: columns straight through, no aggregation ──
    raw_columns = params.get("all_columns") or []
    if kind == "table" and (params.get("query_mode") == "raw" or raw_columns):
        if not raw_columns:
            raise Unsupported("raw table with no columns")
        select, labels = [], []
        for entry in raw_columns:
            expression, label = _column_sql(entry)
            select.append(f"{expression} AS {qi(label)}")
            labels.append(label)
        sql = _assemble(select, source, where, [], None, limit)
        return {"sql": sql, "chart_type": "table", "x_column": labels[0],
                "y_columns": labels, "needs_series": False, "series_expr": None,
                "where": where}

    # ── metrics ──
    raw_metrics = params.get("metrics")
    if raw_metrics is None and params.get("metric") is not None:
        raw_metrics = [params["metric"]]
    if isinstance(raw_metrics, (str, dict)):
        raw_metrics = [raw_metrics]
    if not raw_metrics:
        raise Unsupported("chart has no metric")
    metrics = [_metric_sql(m, saved_metrics) for m in raw_metrics]

    # ── a single number: one metric, no dimension ──
    if kind == "number":
        expression, label = metrics[0]
        select = [f"{expression} AS {qi(label)}"]
        sql = _assemble(select, source, where, [], None, 1)
        return {"sql": sql, "chart_type": "number", "x_column": "",
                "y_columns": [label], "needs_series": False, "series_expr": None,
                "where": where}

    # ── dimensions ──
    time_expr = time_label = None
    grain = GRAIN.get(params.get("time_grain_sqla") or "")
    x_axis = params.get("x_axis")
    if x_axis:
        expression, label = _column_sql(x_axis)
        time_expr = f"date_trunc('{grain}', {expression})" if grain else expression
        time_label = label
    elif params.get("granularity_sqla"):
        col = params["granularity_sqla"]
        if isinstance(col, str) and col.strip():
            time_expr = f"date_trunc('{grain}', {qi(col)})" if grain else qi(col)
            time_label = col

    groupby = [g for g in (params.get("groupby") or []) if g]
    # A pivot table's rows and columns are both just dimensions once flattened.
    for key in ("groupbyRows", "groupbyColumns"):
        groupby += [g for g in (params.get(key) or []) if g]
    dims = [_column_sql(g) for g in groupby]
    for extra in (params.get("columns") or []):
        if extra:
            dims.append(_column_sql(extra))

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

    # ── time + series: one column per series, or the x axis repeats ──
    if time_expr and dims and kind != "table":
        if series_values is None:
            raise Unsupported("time+series chart needs its series values resolved")
        if len(dims) > 1:
            raise Unsupported("more than one series dimension")
        if len(metrics) != 1:
            raise Unsupported("time+series chart with multiple metrics")
        series_expr = dims[0][0]
        agg = metrics[0][0]
        select = [f"{time_expr} AS {qi(time_label)}"]
        y_columns = []
        for value in series_values:
            # `value` is bound as a default: re.sub calls this immediately, but
            # binding it makes that explicit rather than relying on it.
            def _pivot(m, _v=value, _s=series_expr):
                return f"{m.group(1)}({m.group(2)}) FILTER (WHERE {_s} = {lit(_v)})"

            pivoted = re.sub(r"^(\w+)\((.*)\)$", _pivot, agg, count=1)
            if pivoted == agg:
                raise Unsupported("cannot pivot this metric expression")
            select.append(f"{pivoted} AS {qi(str(value))}")
            y_columns.append(str(value))
        sql = _assemble(select, source, where, [time_expr], "1 ASC", limit)
        return {"sql": sql, "chart_type": kind, "x_column": time_label,
                "y_columns": y_columns, "needs_series": True,
                "series_expr": series_expr, "where": where}

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

    order = "1 ASC" if time_expr else f"{len(select)} {'DESC' if params.get('order_desc', True) else 'ASC'}"
    sql = _assemble(select, source, where, group_terms, order, limit)
    return {"sql": sql, "chart_type": kind, "x_column": x_column,
            "y_columns": y_columns, "needs_series": False, "series_expr": None,
            "where": where}


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

    for root in ("ROOT_ID", "GRID_ID"):
        if root in nodes:
            walk(root, None)
    # Some layouts hang tabs off the header rather than the grid.
    for node_id, node in nodes.items():
        if isinstance(node, dict) and node.get("type") == "TABS":
            walk(node_id, None)
    return of_chart, order


def markdown_blocks(position_json: str) -> list[tuple[str | None, str]]:
    """(tab title, text) for each markdown/header block, in reading order."""
    try:
        nodes = json.loads(position_json or "{}")
    except Exception:
        return []
    if not isinstance(nodes, dict):
        return []
    out: list[tuple[str | None, str]] = []

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
            if text_value:
                out.append((tab, text_value))
        for child in node.get("children") or []:
            walk(child, tab)

    for root in ("ROOT_ID", "GRID_ID"):
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
        if isinstance(mask, list) and mask:
            default = str(mask[0])
        elif isinstance(mask, (str, int, float)):
            default = str(mask)
        scope = f.get("scope") or {}
        out.append({
            "key": _slug_key(name, taken),
            "label": str(name)[:120],
            "filter_type": kind,
            "column_expr": column,
            "default_value": default,
            "excluded": [c for c in (scope.get("excluded") or []) if isinstance(c, int)],
            "superset_id": f.get("id"),
        })
    return out
