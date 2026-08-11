/* Live preview for the chart builder.
 *
 * The query result is delivered once and re-mapped in the browser, so changing
 * the x-axis or a measure is instant and never re-runs the SQL. This mirrors
 * the mechanical part of build_spec() in app/charts.py; the authoritative
 * version — including the warnings — stays on the server and runs again on
 * Run and on Save. The palette is handed over in the payload rather than
 * duplicated here, so there is only ever one set of hexes.
 */
(function () {
  "use strict";

  var node = document.getElementById("chart-data");
  if (!node) return;
  var payload = JSON.parse(node.textContent);
  if (!payload.spec) return;

  var typeEl = document.getElementById("chart_type");
  var xEl = document.getElementById("x_column");
  var titleEl = document.getElementById("title");
  var warnEl = document.getElementById("chart-warnings");
  var checks = Array.prototype.slice.call(
    document.querySelectorAll('input[name="y_columns"]')
  );

  function selectedMeasures() {
    return checks.filter(function (c) { return c.checked; })
                 .map(function (c) { return c.value; });
  }

  function toNumber(v) {
    if (v === null || v === undefined || v === "") return null;
    var n = typeof v === "number" ? v : parseFloat(String(v).replace(/,/g, ""));
    return isNaN(n) ? null : n;
  }

  /* Mirrors _format_number() in app/charts.py. Kept in step by eye rather than
     shared: the preview is a convenience, and the server formats again on save. */
  function formatNumber(v) {
    var units = [[1e12, " tri"], [1e9, " bi"], [1e6, " mi"], [1e3, " mil"]];
    var mag = Math.abs(v);
    for (var i = 0; i < units.length; i++) {
      if (mag >= units[i][0]) {
        var s = v / units[i][0];
        return (Math.abs(s) < 10 ? s.toFixed(1) : s.toFixed(0)).replace(".", ",") + units[i][1];
      }
    }
    if (v === Math.round(v)) return String(v).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
    return v.toFixed(2).replace(".", ",");
  }

  function buildSpec() {
    var type = typeEl.value;
    var x = xEl.value;
    var measures = selectedMeasures();
    var warnings = [];
    var xi = payload.columns.indexOf(x);

    // The two HTML types don't produce a canvas spec at all.
    if (type === "table") {
      return { spec: null, html: "table", warnings: [] };
    }
    if (type === "number") {
      if (!measures.length) {
        return { spec: null, html: null, warnings: ["Pick the column holding the number."] };
      }
      return { spec: null, html: "number", warnings: [] };
    }

    if (xi < 0 || !measures.length) {
      return { spec: null, warnings: ["Pick an x-axis column and at least one measure."] };
    }
    if (type === "pie" && measures.length > 1) {
      measures = measures.slice(0, 1);
      warnings.push("A pie shows one measure — charting only the first.");
    }
    if (measures.length > payload.maxSeries) {
      warnings.push(
        "Showing the first " + payload.maxSeries + " series; hues are assigned " +
        "in a fixed order and cycling them would make two series look alike."
      );
      measures = measures.slice(0, payload.maxSeries);
    }
    var labels = payload.rows.map(function (r) {
      return r[xi] === null || r[xi] === undefined ? "" : String(r[xi]);
    });
    var datasets = measures.map(function (name, slot) {
      var ci = payload.columns.indexOf(name);
      var data = payload.rows.map(function (r) { return toNumber(r[ci]); });
      var color =
        type === "pie"
          ? data.map(function (_, i) { return payload.colors[i % payload.colors.length]; })
          : payload.colors[slot % payload.colors.length];
      return { label: name, data: data, color: color };
    });
    return {
      spec: { type: type, labels: labels, datasets: datasets, showLegend: datasets.length > 1 },
      warnings: warnings,
    };
  }

  function syncSaveFields(type, x, measures) {
    document.getElementById("save-title").value = titleEl ? titleEl.value : "";
    document.getElementById("save-type").value = type;
    document.getElementById("save-x").value = x;
    var box = document.getElementById("save-y");
    box.innerHTML = "";
    measures.forEach(function (m) {
      var i = document.createElement("input");
      i.type = "hidden";
      i.name = "y_columns";
      i.value = m;
      box.appendChild(i);
    });
  }

  function updateTypeHint() {
    var hintEl = document.getElementById("type-hint");
    if (!hintEl || !typeEl) return;
    var opt = typeEl.options[typeEl.selectedIndex];
    hintEl.textContent = opt ? opt.getAttribute("data-hint") || "" : "";
  }

  var canvasEl = document.getElementById("preview");
  var htmlEl = document.getElementById("preview-html");
  var MAX_TABLE_ROWS = 200;

  function renderTable() {
    var t = document.createElement("table");
    t.className = "bi-restbl";
    var thead = t.createTHead().insertRow();
    payload.columns.forEach(function (c) {
      var th = document.createElement("th");
      th.textContent = c;
      thead.appendChild(th);
    });
    var body = t.createTBody();
    payload.rows.slice(0, MAX_TABLE_ROWS).forEach(function (r) {
      var tr = body.insertRow();
      r.forEach(function (cell) {
        tr.insertCell().textContent = cell === null || cell === undefined ? "null" : String(cell);
      });
    });
    htmlEl.innerHTML = "";
    htmlEl.className = "bi-tile-table";
    htmlEl.appendChild(t);
  }

  function renderNumber() {
    var name = selectedMeasures()[0];
    var ci = payload.columns.indexOf(name);
    var first = payload.rows[0];
    var n = first ? toNumber(first[ci]) : null;
    var xi = payload.columns.indexOf(xEl.value);
    var caption = first && xi >= 0 && first[xi] != null ? String(first[xi]) : "";
    htmlEl.className = "bi-tile-number";
    htmlEl.innerHTML = "";
    var v = document.createElement("div");
    v.className = "bi-kpi bi-kpi-lg";
    v.textContent = n === null ? "—" : formatNumber(n);
    htmlEl.appendChild(v);
    if (caption) {
      var c = document.createElement("div");
      c.className = "bi-kpi-cap";
      c.textContent = caption;
      htmlEl.appendChild(c);
    }
  }

  /* The x-axis and measures mean different things per type — or nothing at all
     for a table — so the sidebar follows the selection rather than offering
     controls that quietly do nothing. */
  function syncControls(type) {
    var xBox = document.querySelector('[data-cfg="x"]');
    var yBox = document.querySelector('[data-cfg="y"]');
    var label = document.getElementById("measure-label");
    if (xBox) xBox.hidden = type === "table";
    if (yBox) yBox.hidden = type === "table";
    if (label) {
      label.textContent = type === "number"
        ? "Which column holds the number"
        : "Measures (numeric columns)";
    }
    if (xBox && type === "number") {
      xBox.querySelector("label").textContent = "Caption (optional)";
    } else if (xBox) {
      xBox.querySelector("label").textContent = "X axis (labels)";
    }
  }

  function draw() {
    updateTypeHint();
    var type = typeEl.value;
    syncControls(type);
    var built = buildSpec();
    var all = (payload.warnings || []).concat(built.warnings);
    warnEl.innerHTML = "";
    all.forEach(function (w) {
      var d = document.createElement("div");
      d.className = "bi-err";
      d.textContent = w;
      warnEl.appendChild(d);
    });

    var isHtml = built.html === "table" || built.html === "number";
    canvasEl.hidden = isHtml;
    htmlEl.hidden = !isHtml;
    if (built.html === "table") renderTable();
    else if (built.html === "number") renderNumber();
    else if (built.spec) window.renderChart("preview", built.spec);

    syncSaveFields(type, xEl.value, selectedMeasures());
  }

  [typeEl, xEl].concat(checks).forEach(function (el) {
    if (el) el.addEventListener("change", draw);
  });
  if (titleEl) titleEl.addEventListener("input", function () {
    document.getElementById("save-title").value = titleEl.value;
  });

  draw();
})();
