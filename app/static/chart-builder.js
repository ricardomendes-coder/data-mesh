/* The chart studio.
 *
 * Two ways to get data, both feeding the same wells, preview and format panel:
 *
 *   Dataset (visual) — pick a table, its columns arrive from the catalog with
 *   no query, drag them into Eixo X / Séries / Valores, choose an aggregation
 *   per measure. The studio writes the GROUP BY query for you (server side) and
 *   runs it; any mapping change rebuilds it.
 *
 *   SQL (advanced) — write the query yourself. Run fetches its rows once and the
 *   mapping is re-pivoted in the browser, so changing a well is instant.
 *
 * The preview uses the same renderChart() the dashboards use, so what you see is
 * what gets saved. The server rebuilds the spec authoritatively on Save.
 */
(function () {
  "use strict";

  var root = document.querySelector(".st");
  var initNode = document.getElementById("studio-init");
  if (!root || !initNode) return;
  var init = JSON.parse(initNode.textContent);
  var RUN_URL = root.getAttribute("data-run");
  var SAVE_URL = root.getAttribute("data-save");
  var COLUMNS_URL = root.getAttribute("data-columns");
  var BUILD_URL = root.getAttribute("data-build");

  var AGG_LABEL = {};
  (init.aggregations || []).forEach(function (a) { AGG_LABEL[a.key] = a.label; });
  var DATASET_NAMES = {};
  (init.datasets || []).forEach(function (d) { DATASET_NAMES[d.name] = 1; });
  var hasCatalog = (init.datasets || []).length > 0 && !!init.catalogDb;

  var state = {
    mode: init.builderMode === "dataset" && hasCatalog ? "dataset" : "sql",
    slug: init.slug || "",
    type: init.chart_type || "bar",
    x: init.x_column || null,
    series: init.series_column || null,
    values: (init.y_columns || []).slice(),
    aggs: {},        // dataset mode: { column: aggKey }
    table: "",       // dataset mode: selected dataset
    colKinds: {},    // { column: "num" | "date" | "text" }
    data: null,      // { columns, rows, numeric }
    sql: init.sql || "",
  };

  var el = function (id) { return document.getElementById(id); };
  function esc(t) {
    return String(t).replace(/[<>&"]/g, function (c) {
      return { "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c];
    });
  }

  /* ---------- number formatting for the number/table preview ---------- */
  function grp(s) { return String(s).replace(/\B(?=(\d{3})+(?!\d))/g, "."); }
  function compactNum(v) {
    var u = [[1e12, " tri"], [1e9, " bi"], [1e6, " mi"], [1e3, " mil"]], a = Math.abs(v);
    for (var i = 0; i < u.length; i++) {
      if (a >= u[i][0]) { var s = v / u[i][0]; return (Math.abs(s) < 10 ? s.toFixed(1) : s.toFixed(0)).replace(".", ",") + u[i][1]; }
    }
    return v === Math.round(v) ? grp(v) : v.toFixed(2).replace(".", ",");
  }
  function compactUnit(val, decimals) {
    var u = [[1e12, "tri"], [1e9, "bi"], [1e6, "mi"], [1e3, "mil"]], a = Math.abs(val);
    for (var i = 0; i < u.length; i++) {
      if (a >= u[i][0]) return (val / u[i][0]).toFixed(decimals).replace(".", ",") + " " + u[i][1];
    }
    return null;
  }
  function fmtNumberPattern(v, pattern) {
    var n = Number(v); if (!isFinite(n) || !pattern) return v;
    var m = /[#0][#0.,]*/.exec(pattern);
    var prefix = m ? pattern.slice(0, m.index) : "";
    var numTok = m ? m[0] : "0";
    var rest = m ? pattern.slice(m.index + numTok.length) : pattern;
    var percent = /%/.test(pattern);
    var compact = /a/i.test(rest);
    var suffix = rest.replace(/[%a]/gi, "");
    var dot = numTok.indexOf("."); var decimals = dot >= 0 ? numTok.length - dot - 1 : 0;
    var grouping = numTok.indexOf(",") >= 0;
    var val = percent ? n * 100 : n; var neg = val < 0 ? "-" : ""; val = Math.abs(val);
    var body = compact ? compactUnit(val, decimals) : null;
    if (body === null) {
      var parts = val.toFixed(decimals).split(".");
      if (grouping) parts[0] = grp(parts[0]);
      body = parts.length > 1 ? parts[0] + "," + parts[1] : parts[0];
    }
    return neg + prefix + body + (percent ? "%" : "") + suffix;
  }
  function toNumber(v) {
    if (v === null || v === undefined || v === "") return null;
    var n = typeof v === "number" ? v : parseFloat(String(v).replace(/,/g, ""));
    return isNaN(n) ? null : n;
  }
  function measureAlias(agg, col) { return (AGG_LABEL[agg] || agg) + " de " + col; }

  /* ---------- field kinds ---------- */
  function inferKind(col) {
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
  function kindOf(col) { return state.colKinds[col] || "text"; }
  function kindLabel(k) { return k === "num" ? "número" : k === "date" ? "data" : "texto"; }
  function defaultAgg(col) { return kindOf(col) === "num" ? "sum" : "count"; }

  /* ---------- status ---------- */
  function setStatus(msg, cls) {
    var s = el("st-status"); s.textContent = msg || ""; s.className = "st-status" + (cls ? " " + cls : "");
  }
  function showRunError(msg, detail) {
    el("st-empty").hidden = true; el("st-preview").hidden = true; el("st-preview-html").hidden = true;
    var w = el("st-warnings"); w.innerHTML = "";
    var box = document.createElement("div"); box.className = "st-err";
    box.innerHTML = "<span>" + esc(msg) + "</span>";
    if (detail) { var c = document.createElement("code"); c.textContent = detail; box.appendChild(c); }
    w.appendChild(box);
  }

  /* ---------- SQL mode: Run ---------- */
  var runBtn = el("st-run");
  var lastRunSql = null;
  function run() {
    var sql = el("st-sql").value.trim();
    var dbv = el("st-db").value;
    if (!sql) { setStatus("Escreva uma consulta SQL.", "err"); return; }
    lastRunSql = sql; state.sql = sql;
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
        setData(res.body, false);
        setStatus(res.body.truncated ? "mostrando os primeiros " + init.maxPoints + " pontos" : "", "");
      })
      .catch(function () {
        runBtn.disabled = false; runBtn.textContent = "▶ Rodar";
        showRunError("Não foi possível rodar a consulta.");
      });
  }

  /* ---------- dataset mode: build ---------- */
  var buildTimer = null;
  function scheduleBuild() {
    if (buildTimer) clearTimeout(buildTimer);
    buildTimer = setTimeout(doBuild, 350);
  }
  function doBuild() {
    if (state.mode !== "dataset" || !state.table) { draw(); return; }
    var measures = state.values.map(function (c) { return { column: c, agg: state.aggs[c] || defaultAgg(c) }; });
    if (!state.x && !measures.length) { state.data = null; draw(); return; }
    setStatus("montando…", "");
    fetch(BUILD_URL, {
      method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ table: state.table, x: state.x || "", series: state.series || "", measures: measures }),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (!res.ok || !res.body || res.body.error) {
          showRunError((res.body && res.body.error) || "A consulta falhou.", res.body && res.body.detail);
          if (res.body && res.body.sql) showGenSql(res.body.sql);
          return;
        }
        // Result columns are dim(s) + measure aliases; keep our mapping as the
        // catalog columns and resolve to aliases only when drawing.
        state.data = { columns: res.body.columns, rows: res.body.rows, numeric: res.body.numeric_columns || [] };
        state.sql = res.body.sql;
        showGenSql(res.body.sql);
        draw();
        setStatus(res.body.truncated ? "mostrando os primeiros " + init.maxPoints + " pontos" : "", "");
      })
      .catch(function () { showRunError("Não foi possível montar a consulta."); });
  }
  function showGenSql(sql) {
    var g = el("st-gensql"); g.hidden = false; el("st-gensql-text").textContent = sql || "";
  }

  function setData(body, fromCatalog) {
    state.data = { columns: body.columns, rows: body.rows, numeric: body.numeric_columns || [] };
    state.colKinds = {};
    state.data.columns.forEach(function (c) { state.colKinds[c] = inferKind(c); });
    if (state.x && state.data.columns.indexOf(state.x) < 0) state.x = null;
    if (state.series && state.data.columns.indexOf(state.series) < 0) state.series = null;
    state.values = state.values.filter(function (v) { return state.data.columns.indexOf(v) >= 0; });
    if (!state.x && state.data.columns.length) state.x = state.data.columns[0];
    if (!state.values.length && state.data.numeric.length) {
      var first = state.data.numeric[0];
      if (first === state.x && state.data.numeric.length > 1) first = state.data.numeric[1];
      state.values = [first];
    }
    buildChips(); paintWells(); draw();
  }

  /* ---------- dataset picker ---------- */
  function loadColumns(table) {
    setStatus("carregando campos…", "");
    fetch(COLUMNS_URL + "?table=" + encodeURIComponent(table), { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (!res.ok || !res.body || res.body.error) { setStatus((res.body && res.body.error) || "erro ao ler campos", "err"); return; }
        setStatus("", "");
        state.colKinds = {};
        state.catalogCols = res.body.columns.map(function (c) { state.colKinds[c.name] = c.kind; return c.name; });
        // drop mappings that don't belong to the new dataset
        if (state.x && state.colKinds[state.x] === undefined) state.x = null;
        if (state.series && state.colKinds[state.series] === undefined) state.series = null;
        state.values = state.values.filter(function (v) { return state.colKinds[v] !== undefined; });
        buildChips(); paintWells(); scheduleBuild();
      })
      .catch(function () { setStatus("erro ao ler campos", "err"); });
  }

  /* ---------- field chips ---------- */
  var dragField = null;
  function chipColumns() {
    return state.mode === "dataset" ? (state.catalogCols || []) : (state.data ? state.data.columns : []);
  }
  function buildChips() {
    var box = el("st-chips"); box.innerHTML = "";
    var cols = chipColumns();
    cols.forEach(function (col) {
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
    el("st-fields-hint").textContent = cols.length ? cols.length + " colunas" :
      (state.mode === "dataset" ? "escolha uma tabela" : "rode a consulta →");
  }

  /* ---------- wells ---------- */
  function phText(key) {
    return key === "series" ? "um campo de texto" : key === "values" ? "arraste um número" : "arraste um campo";
  }
  function aggSelect(field) {
    if (state.mode !== "dataset") return "";
    var cur = state.aggs[field] || defaultAgg(field);
    var opts = (init.aggregations || []).map(function (a) {
      return '<option value="' + a.key + '"' + (a.key === cur ? " selected" : "") + ">" + esc(a.label) + "</option>";
    }).join("");
    return '<select class="st-pill-agg" data-field="' + esc(field) + '">' + opts + "</select> ";
  }
  function pill(key, field) {
    return '<span class="st-pill" data-well="' + key + '" data-field="' + esc(field) + '">' +
      '<span class="dot ' + kindOf(field) + '"></span>' + (key === "values" ? aggSelect(field) : "") + esc(field) +
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
    var lv = el("lbl-values");
    if (lv) lv.innerHTML = "Valores " + (state.values.length > 1 ? "<span class='opt'>(" + state.values.length + ")</span>" : "<span class='req'>•</span>");
  }
  function accepts(well, field) {
    var acc = well.getAttribute("data-accept"), k = kindOf(field);
    if (acc === "num") return state.mode === "dataset" ? true : k === "num"; // any col can be a measure once aggregated
    if (acc === "cat") return k !== "num";
    return true;
  }
  // In dataset mode a mapping change rebuilds the query; in SQL mode it just
  // re-pivots the rows we already have.
  function refresh() { if (state.mode === "dataset") scheduleBuild(); else draw(); }
  function assign(key, field) {
    if (key === "x") state.x = field;
    else if (key === "series") state.series = field;
    else if (state.values.indexOf(field) < 0) state.values.push(field);
    paintWells(); refresh();
  }
  function clearField(key, field) {
    if (key === "x") state.x = null;
    else if (key === "series") state.series = null;
    else state.values = state.values.filter(function (v) { return v !== field; });
    paintWells(); refresh();
  }
  function autoAssign(field) {
    var k = kindOf(field);
    if (state.mode === "dataset") { if (k === "num") assign("values", field); else if (!state.x) assign("x", field); else assign("series", field); }
    else { if (k === "num") assign("values", field); else if (!state.x) assign("x", field); else assign("series", field); }
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
  el("st-wells").addEventListener("change", function (e) {
    if (!e.target.classList.contains("st-pill-agg")) return;
    state.aggs[e.target.getAttribute("data-field")] = e.target.value;
    scheduleBuild();
  });

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
    syncWellLabels(); applyFormatVisibility(); draw();
  });
  function setViz(type) {
    document.querySelectorAll(".st-vbtn").forEach(function (v) { v.classList.toggle("on", v.getAttribute("data-type") === type); });
    syncWellLabels(); applyFormatVisibility();
  }
  function syncWellLabels() {
    var lx = el("lbl-x"); if (!lx) return;
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
    var xf = el("f-xfmt").value.trim(); if (xf) o.xFormat = xf;
    var vf = el("f-vfmt").value.trim(); if (vf) o.valueFormat = vf;
    return o;
  }
  function applyFormatVisibility() {
    var t = state.type, cartesian = t === "bar" || t === "line" || t === "area";
    var show = {
      legend: cartesian || t === "pie", subtitle: cartesian || t === "pie",
      xformat: cartesian, valueformat: cartesian || t === "pie" || t === "number", grid: cartesian,
    };
    document.querySelectorAll("#st-format .st-fld[data-fmt]").forEach(function (f) {
      f.hidden = !show[f.getAttribute("data-fmt")];
    });
    var lbl = el("lbl-valuefmt");
    if (lbl) lbl.textContent = t === "number" ? "Formato do número" : cartesian ? "Formato do eixo Y" : "Formato dos valores";
    el("st-fmt-note").hidden = t !== "table";
  }
  var TEXT_FMT = { "f-subtitle": 1, "f-xfmt": 1, "f-vfmt": 1 };
  ["f-legend", "f-subtitle", "f-xfmt", "f-vfmt", "f-gridx", "f-gridy"].forEach(function (id) {
    el(id).addEventListener(TEXT_FMT[id] ? "input" : "change", draw);
  });

  /* ---------- spec building (mirrors app/charts.py build_spec) ---------- */
  // The result columns for the measures — the alias in dataset mode, the raw
  // column in SQL mode. x and series are the same column in both.
  function resolvedValues() {
    if (state.mode === "dataset") return state.values.map(function (c) { return measureAlias(state.aggs[c] || defaultAgg(c), c); });
    return state.values.slice();
  }
  function buildSpec() {
    if (!state.data) return { warnings: [] };
    var d = state.data, type = state.type, warnings = [];
    if (type === "table") return { html: "table" };
    var vals = resolvedValues();
    if (type === "number") {
      if (!vals.length) return { warnings: ["Escolha a coluna do número (Valores)."] };
      return { html: "number" };
    }
    if (!state.x || !vals.length) return { warnings: ["Defina Eixo X e Valores."] };
    var xi = d.columns.indexOf(state.x);
    if (xi < 0) return { warnings: ["A coluna do Eixo X não está no resultado."] };

    if (state.series) {
      var si = d.columns.indexOf(state.series), vi = d.columns.indexOf(vals[0]);
      if (si < 0 || vi < 0) return { warnings: ["Séries ou Valores não estão no resultado."] };
      if (vals.length > 1) warnings.push("Com Séries, só o primeiro valor é plotado.");
      var labels = [], seenX = {}, order = [], cells = {};
      d.rows.forEach(function (r) {
        var xv = r[xi] == null ? "" : String(r[xi]), sv = r[si] == null ? "" : String(r[si]);
        if (!(xv in seenX)) { seenX[xv] = 1; labels.push(xv); }
        if (!(sv in cells)) { cells[sv] = {}; order.push(sv); }
        cells[sv][xv] = toNumber(r[vi]);
      });
      if (order.length > init.maxSeries) { warnings.push("Mostrando as primeiras " + init.maxSeries + " séries."); order = order.slice(0, init.maxSeries); }
      var ds = order.map(function (sv, slot) {
        return { label: sv, color: init.colors[slot % init.colors.length], data: labels.map(function (xv) { return cells[sv][xv] == null ? null : cells[sv][xv]; }) };
      });
      return { spec: { type: type, labels: labels, datasets: ds, showLegend: ds.length > 1 }, warnings: warnings };
    }

    var measures = vals.slice();
    if (type === "pie" && measures.length > 1) { measures = measures.slice(0, 1); warnings.push("Pizza mostra um valor — usando o primeiro."); }
    if (measures.length > init.maxSeries) { warnings.push("Mostrando as primeiras " + init.maxSeries + " séries."); measures = measures.slice(0, init.maxSeries); }
    var labs = d.rows.map(function (r) { return r[xi] == null ? "" : String(r[xi]); });
    var datasets = measures.map(function (name, slot) {
      var ci = d.columns.indexOf(name);
      var data = d.rows.map(function (r) { return toNumber(r[ci]); });
      var color = type === "pie" ? data.map(function (_, i) { return init.colors[i % init.colors.length]; }) : init.colors[slot % init.colors.length];
      return { label: name, data: data, color: color };
    });
    return { spec: { type: type, labels: labs, datasets: datasets, showLegend: datasets.length > 1 }, warnings: warnings };
  }

  /* ---------- draw ---------- */
  function draw() {
    var built = buildSpec();
    var canvas = el("st-preview"), html = el("st-preview-html"), empty = el("st-empty");
    var w = el("st-warnings"); w.innerHTML = "";
    (built.warnings || []).forEach(function (msg) { var d = document.createElement("div"); d.className = "st-warn"; d.textContent = "⚠ " + msg; w.appendChild(d); });
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
    var name = resolvedValues()[0], ci = state.data.columns.indexOf(name), first = state.data.rows[0];
    var n = first ? toNumber(first[ci]) : null;
    var xi = state.x ? state.data.columns.indexOf(state.x) : -1;
    var cap = first && xi >= 0 && first[xi] != null ? String(first[xi]) : "";
    var vf = opts.valueFormat;
    var text = n === null ? "—" : (vf ? fmtNumberPattern(n, vf) : compactNum(n));
    host.innerHTML = '<div class="big">' + esc(text) + "</div>" + (cap ? '<div class="cap">' + esc(cap) + "</div>" : "");
  }

  /* ---------- save ---------- */
  function save() {
    var title = el("st-title").value.trim();
    if (!title) { setStatus("Dê um título ao gráfico.", "err"); el("st-title").focus(); return; }
    var vals = resolvedValues();
    if (state.type !== "table" && state.type !== "number" && (!state.x || !vals.length)) { setStatus("Defina Eixo X e Valores antes de salvar.", "err"); return; }
    if (state.type === "number" && !vals.length) { setStatus("Escolha a coluna do número.", "err"); return; }
    // In SQL mode save exactly what's in the editor — keeping the {{ filters }}
    // token a dashboard chart carries. In dataset mode save the generated SQL.
    var sqlToSave = state.mode === "dataset" ? state.sql : el("st-sql").value.trim();
    if (!sqlToSave) { setStatus("Rode a consulta antes de salvar.", "err"); return; }
    var body = new URLSearchParams();
    body.set("sql", sqlToSave);
    body.set("source_db", el("st-db").value);
    body.set("title", title);
    body.set("chart_type", state.type);
    body.set("x_column", state.x || "");
    vals.forEach(function (v) { body.append("y_columns", v); });
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

  /* ---------- mode + database ---------- */
  function setMode(mode) {
    state.mode = mode;
    document.querySelectorAll(".st-mode").forEach(function (b) { b.classList.toggle("on", b.getAttribute("data-mode") === mode); });
    el("st-dsmode").hidden = mode !== "dataset";
    el("st-sqlmode").hidden = mode === "dataset";
    // recompute kinds for the current source and repaint
    if (mode === "sql" && state.data) { state.colKinds = {}; state.data.columns.forEach(function (c) { state.colKinds[c] = inferKind(c); }); }
    buildChips(); paintWells();
    if (mode === "dataset" && state.table) scheduleBuild(); else if (mode === "sql") draw();
  }
  function applyDbCatalog() {
    var isCat = hasCatalog && el("st-db").value === init.catalogDb;
    el("st-modes").hidden = !isCat;
    if (!isCat && state.mode === "dataset") setMode("sql");
  }

  /* ---------- boot ---------- */
  runBtn.addEventListener("click", run);
  el("st-save").addEventListener("click", save);
  el("st-sql").addEventListener("keydown", function (e) { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); run(); } });
  el("st-sql").addEventListener("blur", function () { var sql = el("st-sql").value.trim(); if (sql && sql !== lastRunSql) run(); });
  el("st-db").addEventListener("change", applyDbCatalog);
  document.getElementById("st-modes").addEventListener("click", function (e) {
    var b = e.target.closest(".st-mode"); if (b) setMode(b.getAttribute("data-mode"));
  });
  el("st-dataset").addEventListener("change", function () {
    var v = el("st-dataset").value.trim();
    if (DATASET_NAMES[v]) { state.table = v; loadColumns(v); }
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
  el("f-xfmt").value = o.xFormat || "";
  el("f-vfmt").value = o.valueFormat || "";
  el("f-gridx").checked = !!(o.grid && o.grid.x === true);
  el("f-gridy").checked = !(o.grid && o.grid.y === false);

  applyDbCatalog();
  setMode(state.mode);
  // an existing (SQL) chart runs itself so its preview is there to greet you
  if (state.mode === "sql" && (init.sql || "").trim()) run();
})();
