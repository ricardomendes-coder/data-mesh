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

  function buildSpec() {
    var type = typeEl.value;
    var x = xEl.value;
    var measures = selectedMeasures();
    var warnings = [];
    var xi = payload.columns.indexOf(x);
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

  function draw() {
    updateTypeHint();
    var built = buildSpec();
    var all = (payload.warnings || []).concat(built.warnings);
    warnEl.innerHTML = "";
    all.forEach(function (w) {
      var d = document.createElement("div");
      d.className = "bi-err";
      d.textContent = w;
      warnEl.appendChild(d);
    });
    if (built.spec) window.renderChart("preview", built.spec);
    syncSaveFields(typeEl.value, xEl.value, selectedMeasures());
  }

  [typeEl, xEl].concat(checks).forEach(function (el) {
    if (el) el.addEventListener("change", draw);
  });
  if (titleEl) titleEl.addEventListener("input", function () {
    document.getElementById("save-title").value = titleEl.value;
  });

  draw();
})();
