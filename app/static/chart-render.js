/* Renders a chart spec (from app/charts.py) with the vendored Chart.js.
 *
 * Mark and chrome values follow the data-viz guidance: 2px lines, 4px rounded
 * data-ends, 8px markers, a gap between adjacent bars, recessive grid and axis
 * ink, and the hover tooltip left on — an HTML chart is interactive by default.
 * Series colours arrive already assigned from the validated palette; this file
 * never picks a colour.
 */
(function () {
  "use strict";

  var INK_MUTED = "#868E96";
  var GRID = "#E6E8EA";
  var AXIS = "#C3C2B7";
  var FONT =
    "Inter, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";

  function hexToRgba(hex, alpha) {
    var h = String(hex).replace("#", "");
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    var n = parseInt(h, 16);
    return (
      "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) +
      "," + alpha + ")"
    );
  }

  function buildDatasets(spec, compact) {
    // Points are the expensive part of a line draw: one filled circle per point
    // per series, so a 100-series week chart draws ~5,000 of them. Drop them for
    // a thumbnail or a many-series chart — the shape reads fine without, and the
    // draw gets much cheaper.
    var light = compact || (spec.datasets || []).length > 8;
    return spec.datasets.map(function (ds) {
      var base = { label: ds.label, data: ds.data };
      if (spec.type === "pie") {
        base.backgroundColor = ds.color;
        // A surface-coloured ring keeps adjacent slices from bleeding together.
        base.borderColor = "#ffffff";
        base.borderWidth = 2;
        return base;
      }
      if (spec.type === "bar") {
        base.backgroundColor = ds.color;
        base.borderColor = ds.color;
        base.borderRadius = 4;      // rounded data-end
        base.borderSkipped = false; // ...anchored to the baseline
        base.maxBarThickness = 46;
        return base;
      }
      // line / area
      base.borderColor = ds.color;
      base.borderWidth = compact ? 1.5 : 2;
      base.pointRadius = light ? 0 : 4;
      base.pointHoverRadius = light ? 0 : 6;
      base.pointBackgroundColor = ds.color;
      base.pointBorderColor = "#ffffff";
      base.pointBorderWidth = 2;
      base.tension = 0.25;
      base.fill = spec.type === "area";
      if (spec.type === "area") base.backgroundColor = hexToRgba(ds.color, 0.16);
      return base;
    });
  }

  function scales(spec, compact) {
    if (spec.type === "pie") return undefined;
    // A thumbnail is read as a shape, not a table: drop the tick labels so the
    // plot area gets the whole card instead of sharing it with axis text.
    if (compact) {
      return {
        x: { grid: { display: false, drawBorder: false }, border: { display: false },
             ticks: { display: false } },
        y: { beginAtZero: true, grid: { color: GRID, drawBorder: false },
             border: { display: false }, ticks: { display: false } },
      };
    }
    return {
      x: {
        grid: { display: false, drawBorder: false },
        border: { color: AXIS },
        ticks: {
          color: INK_MUTED,
          font: { family: FONT, size: 11 },
          maxRotation: 45,
          autoSkipPadding: 12,
        },
      },
      y: {
        beginAtZero: true,
        grid: { color: GRID, drawBorder: false },
        border: { display: false },
        ticks: { color: INK_MUTED, font: { family: FONT, size: 11 } },
      },
    };
  }

  // opts.compact: a thumbnail — no legend of any kind (a scrolling 100-series
  // legend is what made preview cards unreadable), no axis text, no tooltip.
  window.renderChart = function (canvasId, spec, opts) {
    opts = opts || {};
    var compact = !!opts.compact;
    var el = document.getElementById(canvasId);
    if (!el || !window.Chart) return null;
    if (el._chart) el._chart.destroy();
    if (el._legend && el._legend.parentNode) {
      el._legend.parentNode.removeChild(el._legend);
      el._legend = null;
    }

    var chartType = spec.type === "area" ? "line" : spec.type;
    // Many series get a scrollable HTML legend instead of the on-canvas one,
    // which would eat the whole chart and can't scroll. Like Superset. Never in
    // a thumbnail, where it would eat the whole card.
    var HTML_LEGEND_AT = 8;
    var manySeries =
      !compact && !!spec.showLegend && (spec.datasets || []).length > HTML_LEGEND_AT;
    el._chart = new Chart(el.getContext("2d"), {
      type: chartType,
      data: { labels: spec.labels, datasets: buildDatasets(spec, compact) },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        // No entry animation: a page can hold a dozen charts, and animating
        // them all in is where the load jank comes from — the number, not the
        // pixels. Many-series charts also drop to a nearest-point tooltip: an
        // index tooltip recomputes every one of a hundred series on each
        // mousemove (measured at ~77ms a move), and listing a hundred rows in a
        // tooltip is useless anyway.
        animation: false,
        interaction: manySeries
          ? { mode: "nearest", intersect: false }
          : { mode: "index", intersect: false },
        plugins: {
          legend: {
            // One series needs no legend — the chart title names it. Many
            // series use the HTML legend below instead of this one. A thumbnail
            // shows none.
            display: !compact && !!spec.showLegend && !manySeries,
            position: "bottom",
            labels: {
              color: "#515669",
              font: { family: FONT, size: 12 },
              usePointStyle: true,
              pointStyle: "circle",
              boxWidth: 8,
              padding: 16,
            },
          },
          tooltip: {
            enabled: !compact,
            backgroundColor: "#00002D",
            titleFont: { family: FONT, size: 12 },
            bodyFont: { family: FONT, size: 12 },
            padding: 10,
            cornerRadius: 8,
            displayColors: true,
            boxWidth: 8,
            boxHeight: 8,
            usePointStyle: true,
          },
        },
        scales: scales(spec, compact),
      },
    });
    if (manySeries) buildHtmlLegend(el, el._chart);
    return el._chart;
  };

  /* A scrollable HTML legend for charts with many series. Each entry toggles
     its series; the box scrolls rather than pushing the chart off screen. */
  function buildHtmlLegend(canvas, chart) {
    if (canvas._legend && canvas._legend.parentNode) {
      canvas._legend.parentNode.removeChild(canvas._legend);
    }
    var box = document.createElement("div");
    box.className = "bi-chart-legend";
    (chart.data.datasets || []).forEach(function (ds, i) {
      var item = document.createElement("button");
      item.type = "button";
      item.className = "bi-legend-item";
      var dot = document.createElement("span");
      dot.className = "bi-legend-dot";
      dot.style.background = ds.borderColor || ds.backgroundColor || "#888";
      var label = document.createElement("span");
      label.textContent = ds.label;
      item.appendChild(dot);
      item.appendChild(label);
      item.addEventListener("click", function () {
        var vis = chart.isDatasetVisible(i);
        chart.setDatasetVisibility(i, !vis);
        item.classList.toggle("off", vis);
        chart.update();
      });
      box.appendChild(item);
    });
    // Sits right after the canvas, inside the same chart box.
    if (canvas.parentNode) canvas.parentNode.appendChild(box);
    canvas._legend = box;
  }
})();
