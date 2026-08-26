/* The chart studio.
 *
 * One screen: write the SQL and Run it, drag the result's columns into the
 * Eixo X / Séries / Valores wells, pick a visualization, format it — the
 * preview redraws live in the browser off a single fetch of the rows. Only Run
 * and Save touch the server; a mapping or format change never re-runs the SQL.
 *
 * The preview uses the very same renderChart() the dashboards use, so what you
 * see is what gets saved. The spec building here mirrors build_spec() in
 * app/charts.py; the server rebuilds it authoritatively on Save.
 */
(function () {
  "use strict";

  var root = document.querySelector(".st");
  var initNode = document.getElementById("studio-init");
  if (!root || !initNode) return;
  var init = JSON.parse(initNode.textContent);
  var RUN_URL = root.getAttribute("data-run");
  var SAVE_URL = root.getAttribute("data-save");

  var state = {
    mode: init.mode,
    slug: init.slug || "",
    type: init.chart_type || "bar",
    x: init.x_column || null,
    series: init.series_column || null,
    values: (init.y_columns || []).slice(),
    data: null, // {columns, rows, numeric} after a Run
  };

  var el = function (id) { return document.getElementById(id); };
  function esc(t) {
    return String(t).replace(/[<>&"]/g, function (c) {
      return { "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c];
    });
  }

  /* ---------- formatters for the number/table preview (canvas uses
     renderChart's own, via options) ---------- */
  function grp(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, "."); }
  function compactNum(v) {
    var u = [[1e12, " tri"], [1e9, " bi"], [1e6, " mi"], [1e3, " mil"]], a = Math.abs(v);
    for (var i = 0; i < u.length; i++) {
      if (a >= u[i][0]) { var s = v / u[i][0]; return (Math.abs(s) < 10 ? s.toFixed(1) : s.toFixed(0)).replace(".", ",") + u[i][1]; }
    }
    return v === Math.round(v) ? grp(v) : v.toFixed(2).replace(".", ",");
  }
  function fmtY(v, p) {
    var n = Number(v); if (!isFinite(n)) return v;
    switch (p) {
      case "integer": return (n < 0 ? "-" : "") + grp(Math.abs(Math.round(n)));
      case "compact": return compactNum(n);
      case "percent": return Math.round(n * 100) + "%";
      case "percent1": return (n * 100).toFixed(1).replace(".", ",") + "%";
      case "currency-brl": return "R$ " + Math.abs(n).toFixed(2).split(".").map(function (x, i) { return i ? x : grp(x); }).join(",");
      default: return compactNum(n);
    }
  }
  function toNumber(v) {
    if (v === null || v === undefined || v === "") return null;
    var n = typeof v === "number" ? v : parseFloat(String(v).replace(/,/g, ""));
    return isNaN(n) ? null : n;
  }

  /* ---------- field kinds ---------- */
  function kindOf(col) {
    if (!state.data) return "text";
    if (state.data.numeric.indexOf(col) >= 0) return "num";
    var ci = state.data.columns.indexOf(col);
    for (var i = 0; i < state.data.rows.length; i++) {
      var v = state.data.rows[i][ci];
      if (v !== null && v !== undefined) {
        return /^\d{4}[-/]\d{2}[-/]\d{2}/.test(String(v)) ? "date" : "text";
      }
    }
    return "text";
  }
  function kindLabel(k) { return k === "num" ? "número" : k === "date" ? "data" : "texto"; }

  /* ---------- Run ---------- */
  var runBtn = el("st-run");
  function setStatus(msg, cls) {
    var s = el("st-status"); s.textContent = msg || ""; s.className = "st-status" + (cls ? " " + cls : "");
  }
  function run() {
    var sql = el("st-sql").value.trim();
    var dbv = el("st-db").value;
    if (!sql) { setStatus("Escreva uma consulta SQL.", "err"); return; }
    runBtn.disabled = true; runBtn.textContent = "…";
    var body = new URLSearchParams(); body.set("sql", sql); body.set("source_db", dbv);
    fetch(RUN_URL, { method: "POST", headers: { Accept: "application/json" }, body: body })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        runBtn.disabled = false; runBtn.textContent = "▶ Rodar";
        if (!res.ok || !res.body || res.body.error) {
          showRunError((res.body && res.body.error) || "A consulta falhou.", res.body && res.body.detail);
          return;
        }
        state.data = { columns: res.body.columns, rows: res.body.rows, numeric: res.body.numeric_columns || [] };
        // Keep mappings that still exist; drop the ones the new query lost.
        if (state.x && state.data.columns.indexOf(state.x) < 0) state.x = null;
        if (state.series && state.data.columns.indexOf(state.series) < 0) state.series = null;
        state.values = state.values.filter(function (v) { return state.data.columns.indexOf(v) >= 0; });
        // First run of a fresh chart: a sensible default mapping.
        if (!state.x && state.data.columns.length) state.x = state.data.columns[0];
        if (!state.values.length && state.data.numeric.length) {
          var first = state.data.numeric[0];
          if (first === state.x && state.data.numeric.length > 1) first = state.data.numeric[1];
          state.values = [first];
        }
        buildChips(); paintWells(); draw();
        if (res.body.truncated) {
          setStatus("mostrando os primeiros " + init.maxPoints + " pontos", "");
        } else { setStatus("", ""); }
      })
      .catch(function () {
        runBtn.disabled = false; runBtn.textContent = "▶ Rodar";
        showRunError("Não foi possível rodar a consulta.");
      });
  }
  function showRunError(msg, detail) {
    el("st-empty").hidden = true; el("st-preview").hidden = true; el("st-preview-html").hidden = true;
    var w = el("st-warnings"); w.innerHTML = "";
    var box = document.createElement("div"); box.className = "st-err";
    box.innerHTML = "<span>" + esc(msg) + "</span>";
    if (detail) { var c = document.createElement("code"); c.textContent = detail; box.appendChild(c); }
    w.appendChild(box);
  }

  /* ---------- field chips ---------- */
  var dragField = null;
  function buildChips() {
    var box = el("st-chips"); box.innerHTML = "";
    state.data.columns.forEach(function (col) {
      var k = kindOf(col);
      var chip = document.createElement("div");
      chip.className = "st-chip"; chip.setAttribute("draggable", "true"); chip.dataset.field = col;
      chip.innerHTML = '<span class="dot ' + k + '"></span><span class="nm">' + esc(col) +
        '</span><span class="kd">' + kindLabel(k) + "</span>";
      chip.addEventListener("dragstart", function (e) {
        dragField = col; chip.classList.add("dragging");
        e.dataTransfer.effectAllowed = "copy"; e.dataTransfer.setData("text/plain", col);
      });
      chip.addEventListener("dragend", function () { dragField = null; chip.classList.remove("dragging"); });
      chip.addEventListener("dblclick", function () { autoAssign(col); });
      box.appendChild(chip);
    });
    el("st-fields-hint").textContent = state.data.columns.length + " colunas";
  }

  /* ---------- wells ---------- */
  function phText(key) {
    return key === "series" ? "um campo de texto" : key === "values" ? "arraste um número" : "arraste um campo";
  }
  function pill(key, field) {
    return '<span class="st-pill" data-well="' + key + '" data-field="' + esc(field) + '">' +
      '<span class="dot ' + kindOf(field) + '"></span>' + esc(field) +
      '<button class="x" title="remover" aria-label="remover">✕</button></span>';
  }
  function fieldsOf(key) {
    return key === "x" ? (state.x ? [state.x] : []) :
      key === "series" ? (state.series ? [state.series] : []) : state.values;
  }
  function paintWells() {
    document.querySelectorAll(".st-well").forEach(function (well) {
      var key = well.getAttribute("data-well"), drop = well.querySelector(".st-drop-well");
      var fields = fieldsOf(key);
      drop.innerHTML = fields.length
        ? fields.map(function (f) { return pill(key, f); }).join("")
        : '<span class="ph">' + phText(key) + "</span>";
    });
    // Value(s) label hints how many measures are on
    var lv = el("lbl-values");
    if (lv) lv.innerHTML = "Valores " + (state.values.length > 1 ? "<span class='opt'>(" + state.values.length + ")</span>" : "<span class='req'>•</span>");
  }
  function accepts(well, field) {
    var acc = well.getAttribute("data-accept"), k = kindOf(field);
    if (acc === "num") return k === "num";
    if (acc === "cat") return k !== "num";
    return true;
  }
  function assign(key, field) {
    if (key === "x") state.x = field;
    else if (key === "series") state.series = field;
    else if (state.values.indexOf(field) < 0) state.values.push(field);
    paintWells(); draw();
  }
  function clearField(key, field) {
    if (key === "x") state.x = null;
    else if (key === "series") state.series = null;
    else state.values = state.values.filter(function (v) { return v !== field; });
    paintWells(); draw();
  }
  function autoAssign(field) {
    var k = kindOf(field);
    if (k === "num") assign("values", field);
    else if (!state.x) assign("x", field);
    else assign("series", field);
  }

  document.querySelectorAll(".st-well").forEach(function (well) {
    var drop = well.querySelector(".st-drop-well"), key = well.getAttribute("data-well");
    drop.addEventListener("dragover", function (e) {
      if (dragField && accepts(well, dragField)) { e.preventDefault(); drop.classList.add("over"); }
    });
    drop.addEventListener("dragleave", function () { drop.classList.remove("over"); });
    drop.addEventListener("drop", function (e) {
      drop.classList.remove("over");
      var f = dragField || e.dataTransfer.getData("text/plain");
      if (f && accepts(well, f)) { e.preventDefault(); assign(key, f); }
    });
  });
  el("st-wells").addEventListener("click", function (e) {
    if (!e.target.classList.contains("x")) return;
    var p = e.target.closest(".st-pill");
    if (p) clearField(p.getAttribute("data-well"), p.getAttribute("data-field"));
  });

  // drop a field on the canvas → number to Valores, otherwise Eixo X
  var card = el("st-card");
  card.addEventListener("dragover", function (e) { if (dragField) { e.preventDefault(); card.classList.add("over"); } });
  card.addEventListener("dragleave", function (e) { if (e.target === card || e.target.id === "st-drop") card.classList.remove("over"); });
  card.addEventListener("drop", function (e) {
    card.classList.remove("over");
    var f = dragField || e.dataTransfer.getData("text/plain"); if (!f) return;
    e.preventDefault(); assign(kindOf(f) === "num" ? "values" : "x", f);
  });

  /* ---------- visualization ---------- */
  el("st-viz").addEventListener("click", function (e) {
    var b = e.target.closest(".st-vbtn"); if (!b) return;
    document.querySelectorAll(".st-vbtn").forEach(function (v) { v.classList.toggle("on", v === b); });
    state.type = b.getAttribute("data-type");
    syncWellLabels(); draw();
  });
  function setViz(type) {
    document.querySelectorAll(".st-vbtn").forEach(function (v) { v.classList.toggle("on", v.getAttribute("data-type") === type); });
    syncWellLabels();
  }
  function syncWellLabels() {
    // For a number/table the X well means something softer; keep it simple and
    // just re-label the X well so it never claims to be required when it isn't.
    var lx = el("lbl-x");
    if (!lx) return;
    if (state.type === "number") lx.innerHTML = "Legenda <span class='opt'>(opcional)</span>";
    else if (state.type === "table") lx.innerHTML = "Eixo X <span class='opt'>(ignorado)</span>";
    else lx.innerHTML = "Eixo X <span class='req'>•</span>";
  }

  /* ---------- format options ---------- */
  function collectOptions() {
    var o = {};
    var lg = el("f-legend").value; if (lg) o.legend = { position: lg };
    var sub = el("f-subtitle").value.trim(); if (sub) o.subtitle = sub;
    o.grid = { x: el("f-gridx").checked, y: el("f-gridy").checked };
    var xf = el("f-xfmt").value; if (xf) o.xAxis = { format: xf };
    var yf = el("f-yfmt").value; if (yf) o.yAxis = { format: yf };
    return o;
  }
  ["f-legend", "f-subtitle", "f-xfmt", "f-yfmt", "f-gridx", "f-gridy"].forEach(function (id) {
    var node = el(id); node.addEventListener(id === "f-subtitle" ? "input" : "change", draw);
  });

  /* ---------- spec building (mirrors app/charts.py build_spec) ---------- */
  function buildSpec() {
    if (!state.data) return { warnings: [] };
    var d = state.data, type = state.type, warnings = [];
    if (type === "table") return { html: "table" };
    if (type === "number") {
      if (!state.values.length) return { warnings: ["Escolha a coluna do número (Valores)."] };
      return { html: "number" };
    }
    if (!state.x || !state.values.length) return { warnings: ["Defina Eixo X e Valores."] };
    var xi = d.columns.indexOf(state.x);
    if (xi < 0) return { warnings: ["A coluna do Eixo X não está no resultado."] };

    if (state.series) {
      var si = d.columns.indexOf(state.series), measure = state.values[0], vi = d.columns.indexOf(measure);
      if (si < 0 || vi < 0) return { warnings: ["Séries ou Valores não estão no resultado."] };
      if (state.values.length > 1) warnings.push("Com Séries, só o primeiro valor é plotado.");
      var labels = [], seenX = {}, order = [], cells = {};
      d.rows.forEach(function (r) {
        var xv = r[xi] == null ? "" : String(r[xi]), sv = r[si] == null ? "" : String(r[si]);
        if (!(xv in seenX)) { seenX[xv] = 1; labels.push(xv); }
        if (!(sv in cells)) { cells[sv] = {}; order.push(sv); }
        cells[sv][xv] = toNumber(r[vi]);
      });
      if (order.length > init.maxSeries) {
        warnings.push("Mostrando as primeiras " + init.maxSeries + " séries.");
        order = order.slice(0, init.maxSeries);
      }
      var ds = order.map(function (sv, slot) {
        return {
          label: sv, color: init.colors[slot % init.colors.length],
          data: labels.map(function (xv) { return cells[sv][xv] == null ? null : cells[sv][xv]; }),
        };
      });
      return { spec: { type: type, labels: labels, datasets: ds, showLegend: ds.length > 1 }, warnings: warnings };
    }

    var measures = state.values.slice();
    if (type === "pie" && measures.length > 1) { measures = measures.slice(0, 1); warnings.push("Pizza mostra um valor — usando o primeiro."); }
    if (measures.length > init.maxSeries) { warnings.push("Mostrando as primeiras " + init.maxSeries + " séries."); measures = measures.slice(0, init.maxSeries); }
    var labs = d.rows.map(function (r) { return r[xi] == null ? "" : String(r[xi]); });
    var datasets = measures.map(function (name, slot) {
      var ci = d.columns.indexOf(name);
      var data = d.rows.map(function (r) { return toNumber(r[ci]); });
      var color = type === "pie"
        ? data.map(function (_, i) { return init.colors[i % init.colors.length]; })
        : init.colors[slot % init.colors.length];
      return { label: name, data: data, color: color };
    });
    return { spec: { type: type, labels: labs, datasets: datasets, showLegend: datasets.length > 1 }, warnings: warnings };
  }

  /* ---------- draw ---------- */
  function draw() {
    var built = buildSpec();
    var canvas = el("st-preview"), html = el("st-preview-html"), empty = el("st-empty");
    var w = el("st-warnings"); w.innerHTML = "";
    (built.warnings || []).forEach(function (msg) {
      var d = document.createElement("div"); d.className = "st-warn"; d.textContent = "⚠ " + msg; w.appendChild(d);
    });
    var opts = collectOptions();

    if (built.html === "table") { empty.hidden = true; canvas.hidden = true; html.hidden = false; renderTable(html); return; }
    if (built.html === "number") { empty.hidden = true; canvas.hidden = true; html.hidden = false; renderNumber(html, opts); return; }
    if (built.spec) {
      empty.hidden = true; html.hidden = true; canvas.hidden = false;
      built.spec.options = opts;
      if (window.renderChart) window.renderChart("st-preview", built.spec);
      return;
    }
    canvas.hidden = true; html.hidden = true; empty.hidden = false;
  }
  function renderTable(host) {
    host.className = "st-tablewrap";
    var t = document.createElement("table"); t.className = "st-mtable";
    var head = t.createTHead().insertRow();
    state.data.columns.forEach(function (c) { var th = document.createElement("th"); th.textContent = c; head.appendChild(th); });
    var body = t.createTBody();
    state.data.rows.slice(0, 200).forEach(function (r) {
      var tr = body.insertRow();
      r.forEach(function (cell) { tr.insertCell().textContent = cell === null || cell === undefined ? "" : String(cell); });
    });
    host.innerHTML = ""; host.appendChild(t);
  }
  function renderNumber(host, opts) {
    host.className = "st-kpi";
    var name = state.values[0], ci = state.data.columns.indexOf(name), first = state.data.rows[0];
    var n = first ? toNumber(first[ci]) : null;
    var xi = state.x ? state.data.columns.indexOf(state.x) : -1;
    var cap = first && xi >= 0 && first[xi] != null ? String(first[xi]) : "";
    var yf = opts.yAxis && opts.yAxis.format;
    host.innerHTML = '<div class="big">' + (n === null ? "—" : esc(fmtY(n, yf))) + "</div>" +
      (cap ? '<div class="cap">' + esc(cap) + "</div>" : "");
  }

  /* ---------- save ---------- */
  function save() {
    var title = el("st-title").value.trim();
    if (!title) { setStatus("Dê um título ao gráfico.", "err"); el("st-title").focus(); return; }
    if (state.type !== "table" && state.type !== "number" && (!state.x || !state.values.length)) {
      setStatus("Defina Eixo X e Valores antes de salvar.", "err"); return;
    }
    if (state.type === "number" && !state.values.length) { setStatus("Escolha a coluna do número.", "err"); return; }
    var body = new URLSearchParams();
    body.set("sql", el("st-sql").value);
    body.set("source_db", el("st-db").value);
    body.set("title", title);
    body.set("chart_type", state.type);
    body.set("x_column", state.x || "");
    state.values.forEach(function (v) { body.append("y_columns", v); });
    body.set("series_column", state.series || "");
    body.set("options", JSON.stringify(collectOptions()));
    if (state.slug) body.set("slug", state.slug);
    setStatus("salvando…", "");
    var btn = el("st-save"); btn.disabled = true;
    fetch(SAVE_URL, { method: "POST", headers: { Accept: "application/json" }, body: body })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (res.ok && res.body && res.body.ok) { setStatus("salvo ✓", "ok"); window.location.href = res.body.url; }
        else { btn.disabled = false; setStatus((res.body && res.body.error) || "erro ao salvar", "err"); }
      })
      .catch(function () { btn.disabled = false; setStatus("erro ao salvar", "err"); });
  }

  /* ---------- boot ---------- */
  runBtn.addEventListener("click", run);
  el("st-save").addEventListener("click", save);
  el("st-sql").addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); run(); }
  });

  // prefill from init
  var dbSel = el("st-db");
  if (init.source_db) { for (var i = 0; i < dbSel.options.length; i++) { if (dbSel.options[i].value === init.source_db) dbSel.selectedIndex = i; } }
  el("st-sql").value = init.sql || "";
  el("st-title").value = init.title || "";
  setViz(state.type);
  var o = init.options || {};
  el("f-legend").value = (o.legend && o.legend.position) || "";
  el("f-subtitle").value = o.subtitle || "";
  el("f-xfmt").value = (o.xAxis && o.xAxis.format) || "";
  el("f-yfmt").value = (o.yAxis && o.yAxis.format) || "";
  el("f-gridx").checked = !!(o.grid && o.grid.x === true);
  el("f-gridy").checked = !(o.grid && o.grid.y === false);
  paintWells();

  // an existing chart runs itself so its preview is there to greet you
  if ((init.sql || "").trim()) run();
})();
