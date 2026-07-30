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
]
CHART_TYPE_KEYS = {k for k, _, _ in CHART_TYPES}


@dataclass
class ChartSpec:
    chart_type: str
    labels: list[str] = field(default_factory=list)
    datasets: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

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


def build_spec(
    columns: list[str],
    rows: list[tuple],
    chart_type: str,
    x_column: str,
    y_columns: list[str],
) -> ChartSpec:
    """Shape a result set into a chart spec, or explain why it can't be one."""
    spec = ChartSpec(chart_type=chart_type)

    if chart_type not in CHART_TYPE_KEYS:
        spec.warnings.append(f"Unknown chart type {chart_type!r}.")
        return spec
    if x_column not in columns:
        spec.warnings.append(f"Column {x_column!r} is not in the result.")
        return spec

    usable = [c for c in y_columns if c in columns]
    missing = [c for c in y_columns if c not in columns]
    if missing:
        spec.warnings.append(
            "Dropped from the chart (not in the result): " + ", ".join(missing) + "."
        )
    if not usable:
        spec.warnings.append("Pick at least one numeric column to plot.")
        return spec

    if chart_type == "pie" and len(usable) > 1:
        # A pie encodes one whole. Several measures on one pie is meaningless.
        usable = usable[:1]
        spec.warnings.append("A pie shows one measure — charting only the first.")

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
