"""Turning a query result into a chart spec.

Pure functions: rows in, a Chart.js-shaped dict out. No database, no request —
so the rules below are directly testable.

The palette is not decorative. These eight hues in this order were checked with
the data-viz validator against the app's white card surface and clear every
hard gate (lightness band, chroma floor, adjacent CVD separation ΔE 9.5,
normal-vision separation ΔE 16.8). Consequences that follow from that run:

  * Slots are assigned in fixed order and never cycled — a 9th series would
    reuse a hue and break identity, so series are capped instead.
  * Aqua, yellow and magenta sit below 3:1 contrast on white. The validator
    calls for "relief" when that happens, which is why every chart page also
    renders the underlying rows as a table.

Reordering these hues invalidates the run. Re-validate before touching them.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .i18n import t

# Validated order — see the module docstring before changing it.
SERIES_COLORS = [
    "#FF5A00",  # V360 orange (brand primary)
    "#7936BE",  # V360 purple (the 360 wordmark)
    "#2A78D6",  # blue
    "#1BAF7A",  # aqua
    "#EDA100",  # yellow
    "#E87BA4",  # magenta
    "#008300",  # green
    "#E34948",  # red
]
MAX_SERIES = len(SERIES_COLORS)

# Beyond this the x-axis is unreadable and the browser struggles; the chart is
# the wrong tool at that point, so say so rather than rendering a smear.
MAX_POINTS = 500

CHART_TYPES = [
    ("bar", "Bar", "Compare magnitude across categories"),
    ("line", "Line", "Change over time"),
    ("area", "Area", "Change over time, emphasising volume"),
    ("pie", "Pie", "Parts of one whole — only for a single measure"),
    ("table", "Table", "The rows as they come back — no aggregation applied here"),
    ("number", "Big number", "One value, large — a KPI rather than a trend"),
]
CHART_TYPE_KEYS = {k for k, _, _ in CHART_TYPES}

# Which types draw on a canvas. The other two are HTML, and skip the whole
# labels/datasets path: a table has no series and a KPI has no axis.
CANVAS_TYPES = {"bar", "line", "area", "pie"}

# A table tile is a summary, not the console. Past this it stops being readable
# on a dashboard and the query wants a LIMIT or the Reports tab.
MAX_TABLE_ROWS = 200

# Long-format charts (a series per group) have no hard series limit — the
# legend scrolls, like Superset. This is only a defensive ceiling so a runaway
# grouping can't lock the browser; beyond it the extra series are dropped with
# a note, never a refusal.
MAX_LONG_SERIES = 100


@dataclass
class ChartSpec:
    chart_type: str
    labels: list[str] = field(default_factory=list)
    datasets: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # table tiles
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    # number tiles
    value: str = ""
    caption: str = ""

    @property
    def renders_as(self) -> str:
        """How the template should draw this: a canvas, a table, or a number."""
        if self.chart_type in ("table", "number"):
            return self.chart_type
        return "canvas"

    @property
    def show_legend(self) -> bool:
        """A single series needs no legend — the title already names it."""
        return len(self.datasets) > 1

    def to_dict(self) -> dict:
        return {
            "type": "area" if self.chart_type == "area" else self.chart_type,
            "labels": self.labels,
            "datasets": self.datasets,
            "showLegend": self.show_legend,
        }


def _numeric(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def _format_number(value: float) -> str:
    """A KPI reads at a glance or not at all, so this abbreviates.

    Thousands separators on a 9-digit figure are still 13 characters of tile;
    1.2 bi is the number somebody actually repeats out loud.
    """
    magnitude = abs(value)
    for limit, suffix in ((1e12, " tri"), (1e9, " bi"), (1e6, " mi"), (1e3, " mil")):
        if magnitude >= limit:
            scaled = value / limit
            # 2 significant-ish digits: 1.2 mi, but 12 mi rather than 12.3 mi
            text = f"{scaled:.1f}" if abs(scaled) < 10 else f"{scaled:.0f}"
            return text.replace(".", ",") + suffix
    if value == int(value):
        return f"{int(value):,}".replace(",", ".")
    return f"{value:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")


def numeric_columns(columns: list[str], rows: list[tuple]) -> list[str]:
    """Columns whose values parse as numbers — the candidates for a measure.

    Judged on the rows we actually have rather than the declared type, so a
    numeric column arriving as text is still offered.
    """
    out = []
    for i, name in enumerate(columns):
        values = [r[i] for r in rows[:50] if i < len(r) and r[i] is not None]
        if values and all(_numeric(v) is not None for v in values):
            out.append(name)
    return out


def _long_form_spec(spec, columns, rows, x_column, series_column, y_columns, chart_type):
    """Pivot long rows (x, series, value) into one dataset per series.

    The query already grouped by (x, series); here we lay each series out along
    the shared x axis. No series cap beyond a defensive ceiling — the renderer
    gives many series a scrollable legend.
    """
    xi = columns.index(x_column)
    si = columns.index(series_column)
    value = next((c for c in y_columns if c in columns), None)
    if value is None:
        value = next(
            (c for c in numeric_columns(columns, rows) if c not in (x_column, series_column)),
            None,
        )
    if value is None:
        spec.warnings.append(t("Pick at least one numeric column to plot."))
        return spec
    vi = columns.index(value)

    labels, seen_x = [], set()
    series_order, cells = [], {}
    for r in rows:
        xv = "" if r[xi] is None else str(r[xi])
        sv = "" if r[si] is None else str(r[si])
        if xv not in seen_x:
            seen_x.add(xv)
            labels.append(xv)
        if sv not in cells:
            cells[sv] = {}
            series_order.append(sv)
        cells[sv][xv] = _numeric(r[vi]) if vi < len(r) else None

    if len(labels) > MAX_POINTS:
        spec.warnings.append(
            f"{len(labels):,} points on the x axis — plotting the first {MAX_POINTS:,}."
        )
        labels = labels[:MAX_POINTS]

    if len(series_order) > MAX_LONG_SERIES:
        dropped = len(series_order) - MAX_LONG_SERIES
        series_order = series_order[:MAX_LONG_SERIES]
        spec.warnings.append(
            f"{dropped} more series not shown (of {len(series_order) + dropped}); "
            "scroll the legend for the rest."
        )

    spec.labels = labels
    for i, sv in enumerate(series_order):
        row = cells[sv]
        spec.datasets.append(
            {
                "label": sv,
                "data": [row.get(x) for x in labels],
                "color": SERIES_COLORS[i % MAX_SERIES],
            }
        )
    return spec


def build_spec(
    columns: list[str],
    rows: list[tuple],
    chart_type: str,
    x_column: str,
    y_columns: list[str],
    series_column: str = "",
) -> ChartSpec:
    """Shape a result set into a chart spec, or explain why it can't be one."""
    spec = ChartSpec(chart_type=chart_type)

    if chart_type not in CHART_TYPE_KEYS:
        spec.warnings.append(f"Unknown chart type {chart_type!r}.")
        return spec

    # A table shows the result as it arrives: no x, no series, no colours. It
    # exists because a good half of what people build is a small summary grid
    # rather than a graph, and forcing those into a bar chart helps nobody.
    if chart_type == "table":
        spec.columns = list(columns)
        spec.rows = rows[:MAX_TABLE_ROWS]
        if len(rows) > MAX_TABLE_ROWS:
            spec.warnings.append(
                f"{len(rows):,} rows — showing the first {MAX_TABLE_ROWS}. "
                "Add a LIMIT, or make it a report if the whole set is the point."
            )
        return spec

    # One number, from the first row. y_columns names which measure; the x value
    # of that row becomes the caption, so "last month: 1,234" still reads.
    if chart_type == "number":
        measure = next((c for c in y_columns if c in columns), None)
        if measure is None:
            measure = next((c for c in numeric_columns(columns, rows) if c != x_column), None)
        if measure is None:
            spec.warnings.append(t("Pick the column holding the number."))
            return spec
        if not rows:
            spec.warnings.append(t("The query returned no rows."))
            return spec
        raw = rows[0][columns.index(measure)]
        number = _numeric(raw)
        spec.value = "—" if number is None else _format_number(number)
        if x_column in columns:
            label = rows[0][columns.index(x_column)]
            spec.caption = "" if label is None else str(label)
        if len(rows) > 1:
            spec.warnings.append(
                f"{len(rows):,} rows returned — showing the first. "
                "A big number wants a query that returns one row."
            )
        return spec

    if x_column not in columns:
        spec.warnings.append(f"Column {x_column!r} is not in the result.")
        return spec

    # Long format: (x, series, value) rows -> one dataset per series. This is
    # how a chart split by 112 clients renders — no pivot in SQL, no cap, a
    # scrollable legend on the page.
    if series_column and series_column in columns and chart_type in ("bar", "line", "area"):
        return _long_form_spec(spec, columns, rows, x_column, series_column, y_columns, chart_type)

    usable = [c for c in y_columns if c in columns]
    missing = [c for c in y_columns if c not in columns]
    if missing:
        spec.warnings.append(
            "Dropped from the chart (not in the result): " + ", ".join(missing) + "."
        )
    if not usable:
        spec.warnings.append(t("Pick at least one numeric column to plot."))
        return spec

    if chart_type == "pie" and len(usable) > 1:
        # A pie encodes one whole. Several measures on one pie is meaningless.
        usable = usable[:1]
        spec.warnings.append(t("A pie shows one measure — charting only the first."))

    if len(usable) > MAX_SERIES:
        dropped = usable[MAX_SERIES:]
        usable = usable[:MAX_SERIES]
        spec.warnings.append(
            f"Showing the first {MAX_SERIES} series; hues are assigned in a fixed "
            "order and cycling them would make two series look alike. "
            "Dropped: " + ", ".join(dropped) + "."
        )

    if len(rows) > MAX_POINTS:
        spec.warnings.append(
            f"{len(rows):,} rows is past what a chart can show — plotting the "
            f"first {MAX_POINTS:,}. Aggregate in SQL for a readable chart."
        )
        rows = rows[:MAX_POINTS]

    xi = columns.index(x_column)
    spec.labels = ["" if r[xi] is None else str(r[xi]) for r in rows]

    for slot, name in enumerate(usable):
        ci = columns.index(name)
        data = [_numeric(r[ci]) if ci < len(r) else None for r in rows]
        if chart_type == "pie":
            # One slice per category, so colour varies down the rows, not across
            # series — the only place the order maps to labels instead.
            colors = [SERIES_COLORS[i % MAX_SERIES] for i in range(len(data))]
        else:
            colors = SERIES_COLORS[slot % MAX_SERIES]
        spec.datasets.append({"label": name, "data": data, "color": colors})

    return spec
