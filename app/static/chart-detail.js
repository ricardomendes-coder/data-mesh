/* Fills a chart's own page after it has painted.
 *
 * This page used to run the chart's SQL while rendering, so opening one of the
 * heavier charts meant a minute or more of blank browser with no SQL, no
 * title, nothing. The dashboards were fixed for exactly this and the fix never
 * reached here. Now the page arrives complete except for the numbers, and the
 * numbers come from the same endpoint the listing uses — in `full=1` mode,
 * which returns every row rather than the dozen a thumbnail needs.
 */
(function () {
  "use strict";

  var host = document.getElementById("chart-detail");
  if (!host) return;

  var base = host.getAttribute("data-url");
  var shell = document.getElementById("chart-shell");
  var table = document.getElementById("chart-table");
  var meta = document.getElementById("chart-rowcount");

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function label(name) {
    return host.getAttribute("data-" + name) || "";
  }

  function fail(message, detail) {
    shell.innerHTML = "";
    var box = el("div", "bi-err");
    box.appendChild(el("span", null, message));
    if (detail) box.appendChild(el("code", "bi-tile-err-detail", detail));
    shell.appendChild(box);
  }

  function drawChart(payload) {
    shell.innerHTML = "";
    if (payload.renders_as === "number") {
      shell.className = "bi-chartbox bi-chartbox-lg bi-kpi-box";
      shell.appendChild(el("div", "bi-kpi bi-kpi-lg", payload.value || "—"));
      if (payload.caption) shell.appendChild(el("div", "bi-kpi-cap", payload.caption));
      return;
    }
    if (payload.renders_as === "table") {
      // The Data pane below *is* the chart; a second copy would be noise.
      shell.hidden = true;
      return;
    }
    var canvas = document.createElement("canvas");
    canvas.id = "chart";
    canvas.height = 360;
    shell.appendChild(canvas);
    if (window.renderChart && payload.spec) window.renderChart("chart", payload.spec);
  }

  function drawRows(payload) {
    table.innerHTML = "";
    var columns = payload.columns || [];
    var rows = payload.rows || [];
    if (!columns.length) return;
    var head = table.createTHead().insertRow();
    columns.forEach(function (c) { head.appendChild(el("th", null, c)); });
    var body = table.createTBody();
    rows.forEach(function (r) {
      var tr = body.insertRow();
      r.forEach(function (cell) {
        var td = tr.insertCell();
        if (cell === "" || cell === null) td.appendChild(el("span", "bi-null", "null"));
        else td.textContent = cell;
      });
    });
    meta.textContent = rows.length + " " + label("rows-label");
  }

  function stamp(payload) {
    if (!payload.age) return;
    var age = el("div", "bi-prev-age");
    age.appendChild(el("span", null, payload.age));
    if (payload.cached) {
      var refresh = el("button", "bi-prev-refresh", "↻");
      refresh.type = "button";
      refresh.title = label("refresh-label");
      refresh.addEventListener("click", function () { load(true); });
      age.appendChild(refresh);
    }
    meta.appendChild(age);
  }

  function load(force) {
    shell.hidden = false;
    shell.className = "bi-chartbox bi-chartbox-lg";
    shell.innerHTML = '<div class="bi-tile-skel"><span></span><span></span><span></span></div>';
    meta.textContent = "";
    var url = base + "?full=1" + (force ? "&refresh=1" : "");
    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        var payload = res.body || {};
        // "It never came back" and "the database said no" are different
        // things, and one sentence for both makes a slow chart look broken.
        if (!res.ok) return fail(payload.error || label("err-timeout"), payload.detail);
        if (payload.error) return fail(payload.error, payload.detail);
        drawChart(payload);
        drawRows(payload);
        stamp(payload);
        (payload.warnings || []).forEach(function (w) {
          host.insertBefore(el("div", "bi-err", w), host.firstChild);
        });
      })
      .catch(function () { fail(label("err-failed")); });
  }

  load(false);
})();
