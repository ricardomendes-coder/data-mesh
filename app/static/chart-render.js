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

  /* ---- Generic label formatting. A format is a free pattern, not a fixed
     preset — the common cases (01/05/2026, 10%, 1,2 mi, R$ 1.234,56) are just
     patterns among all the ones you can write. Applied at draw time to the tick
     and tooltip; the stored data never changes. An empty pattern, or a value
     that isn't the expected shape, passes straight through.

     Dates use tokens: YYYY YY MMMM MMM MM M DD D HH mm ss.
     Numbers use a pattern: #,##0.00 (grouping + decimals), a trailing % for
     percent (x100), a trailing 'a' for compact (mil/mi/bi/tri), and any literal
     text around it as prefix/suffix — e.g. "R$ #,##0.00" or "0.0a". ---- */

  var MONTHS_BR = ["jan", "fev", "mar", "abr", "mai", "jun",
                   "jul", "ago", "set", "out", "nov", "dez"];
  var MONTHS_LONG = ["janeiro", "fevereiro", "março", "abril", "maio", "junho",
                     "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"];

  function dateParts(s) {
    var m = /^(\d{4})[-/](\d{2})[-/](\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?/.exec(String(s));
    if (!m) return null;
    return { Y: m[1], MM: m[2], DD: m[3], HH: m[4] || "00", mi: m[5] || "00", ss: m[6] || "00" };
  }

  function fmtDate(s, pattern) {
    var p = dateParts(s);
    if (!p || !pattern) return s;
    var mo = parseInt(p.MM, 10);
    var map = {
      YYYY: p.Y, YY: p.Y.slice(2),
      MMMM: MONTHS_LONG[mo - 1] || p.MM, MMM: MONTHS_BR[mo - 1] || p.MM,
      MM: p.MM, M: String(mo),
      DD: p.DD, D: String(parseInt(p.DD, 10)),
      HH: p.HH, mm: p.mi, ss: p.ss,
    };
    return pattern.replace(/YYYY|YY|MMMM|MMM|MM|DD|HH|mm|ss|M|D/g, function (t) { return map[t]; });
  }

  // Thousands separator, pt-BR (12345 -> "12.345").
  function ptGroup(intStr) {
    return String(intStr).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  }

  function compactUnit(val, decimals) {
    var units = [[1e12, "tri"], [1e9, "bi"], [1e6, "mi"], [1e3, "mil"]], a = Math.abs(val);
    for (var i = 0; i < units.length; i++) {
      if (a >= units[i][0]) return (val / units[i][0]).toFixed(decimals).replace(".", ",") + " " + units[i][1];
    }
    return null; // below a thousand: format normally
  }

  function fmtNumber(v, pattern) {
    var n = Number(v);
    if (!isFinite(n) || !pattern) return v;
    var m = /[#0][#0.,]*/.exec(pattern);
    var prefix = m ? pattern.slice(0, m.index) : "";
    var numTok = m ? m[0] : "0";
    var rest = m ? pattern.slice(m.index + numTok.length) : pattern;
    var percent = /%/.test(pattern);
    var compact = /a/i.test(rest);
    var suffix = rest.replace(/[%a]/gi, "");
    var dot = numTok.indexOf(".");
    var decimals = dot >= 0 ? numTok.length - dot - 1 : 0;
    var grouping = numTok.indexOf(",") >= 0;
    var val = percent ? n * 100 : n;
    var neg = val < 0 ? "-" : "";
    val = Math.abs(val);
    var body = compact ? compactUnit(val, decimals) : null;
    if (body === null) {
      var parts = val.toFixed(decimals).split(".");
      if (grouping) parts[0] = ptGroup(parts[0]);
      body = parts.length > 1 ? parts[0] + "," + parts[1] : parts[0];
    }
    return neg + prefix + body + (percent ? "%" : "") + suffix;
  }

  function usable(pattern) {
    return !!pattern;
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

  function scales(spec, compact, o) {
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
    // Grid lines follow the editor: x off and y on by default, either overridable.
    var grid = o.grid || {};
    var xGrid = grid.x === true;
    var yGrid = grid.y !== false;
    var xf = o.xFormat;
    var yf = o.valueFormat;
    var xTicks = {
      color: INK_MUTED,
      font: { family: FONT, size: 11 },
      maxRotation: 45,
      autoSkipPadding: 12,
    };
    if (usable(xf)) {
      xTicks.callback = function (value) {
        return fmtDate(this.getLabelForValue(value), xf);
      };
    }
    var yTicks = { color: INK_MUTED, font: { family: FONT, size: 11 } };
    if (usable(yf)) {
      yTicks.callback = function (value) {
        return fmtNumber(value, yf);
      };
    }
    return {
      x: {
        grid: { display: xGrid, color: GRID, drawBorder: false },
        border: { color: AXIS },
        ticks: xTicks,
      },
      y: {
        beginAtZero: true,
        grid: { display: yGrid, color: GRID, drawBorder: false },
        border: { display: false },
        ticks: yTicks,
      },
    };
  }

  // opts.compact: a thumbnail — no legend of any kind (a scrolling 100-series
  // legend is what made preview cards unreadable), no axis text, no tooltip.
  window.renderChart = function (canvasId, spec, opts) {
    opts = opts || {};
    var compact = !!opts.compact;
    // Accept a canvas element or an id. An element lets a caller render a canvas
    // it just built but hasn't given a unique id — and skips a DOM lookup that
    // fails for a node not yet attached to the document.
    var el =
      typeof canvasId === "string" ? document.getElementById(canvasId) : canvasId;
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

    // Display options (legend placement, subtitle, grid, axis formats). Absent
    // for a chart nobody has customised yet — then the defaults below apply.
    var o = spec.options || {};
    var legendPos = o.legend && o.legend.position;
    var wantLegend;
    if (legendPos === "hidden") wantLegend = false;
    else if (legendPos) wantLegend = true; // an explicit place means "show it"
    else wantLegend = !!spec.showLegend;   // default: only when >1 series
    var showLegend = !compact && wantLegend && !manySeries;
    var xf = o.xFormat;
    var yf = o.valueFormat;

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
            // One series needs no legend by default — the chart title names it,
            // and many series use the HTML legend below. The editor overrides
            // both: a chosen position shows it, "hidden" removes it. Never in a
            // thumbnail.
            display: showLegend,
            position: legendPos && legendPos !== "hidden" ? legendPos : "bottom",
            labels: {
              color: "#515669",
              font: { family: FONT, size: 12 },
              usePointStyle: true,
              pointStyle: "circle",
              boxWidth: 8,
              padding: 16,
            },
          },
          // An optional line under the title, straight from the editor.
          subtitle: {
            display: !compact && !!o.subtitle,
            text: o.subtitle || "",
            color: INK_MUTED,
            font: { family: FONT, size: 12, weight: "normal" },
            padding: { bottom: 8 },
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
            // The tooltip speaks the same formats as the axes, so a hovered
            // point reads "01/05/2026 — 10%", not the raw stored values.
            callbacks: {
              title: usable(xf)
                ? function (items) {
                    return items.length ? fmtDate(items[0].label, xf) : "";
                  }
                : undefined,
              label: usable(yf)
                ? function (ctx) {
                    var lbl = ctx.dataset && ctx.dataset.label ? ctx.dataset.label + ": " : "";
                    var val = ctx.parsed && ctx.parsed.y != null ? ctx.parsed.y : ctx.parsed;
                    return lbl + fmtNumber(val, yf);
                  }
                : undefined,
            },
          },
        },
        scales: scales(spec, compact, o),
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
