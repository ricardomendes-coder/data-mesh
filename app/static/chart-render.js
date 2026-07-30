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

  function buildDatasets(spec) {
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
      base.borderWidth = 2;
      base.pointRadius = 4;
      base.pointHoverRadius = 6;
      base.pointBackgroundColor = ds.color;
      base.pointBorderColor = "#ffffff";
      base.pointBorderWidth = 2;
      base.tension = 0.25;
      base.fill = spec.type === "area";
      if (spec.type === "area") base.backgroundColor = hexToRgba(ds.color, 0.16);
      return base;
    });
  }

  function scales(spec) {
    if (spec.type === "pie") return undefined;
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

  window.renderChart = function (canvasId, spec) {
    var el = document.getElementById(canvasId);
    if (!el || !window.Chart) return null;
    if (el._chart) el._chart.destroy();

    var chartType = spec.type === "area" ? "line" : spec.type;
    el._chart = new Chart(el.getContext("2d"), {
      type: chartType,
      data: { labels: spec.labels, datasets: buildDatasets(spec) },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            // One series needs no legend — the chart title names it.
            display: !!spec.showLegend,
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
        scales: scales(spec),
      },
    });
    return el._chart;
  };
})();
