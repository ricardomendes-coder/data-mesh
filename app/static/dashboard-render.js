/* Fills a dashboard's tiles in after the page has painted.
 *
 * The page used to arrive with every query already run, sequentially — twenty
 * tiles meant waiting on the sum of twenty queries before seeing anything. Now
 * the HTML comes back immediately with skeletons and each tile fetches its own
 * data, a few at a time. A slow tile delays itself and nothing else.
 *
 * The concurrency cap matters: the tiles all hit the same warehouse, and firing
 * thirty queries at once is slower for everyone than a steady handful.
 */
(function () {
  "use strict";

  var CONCURRENCY = 4;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function renderTable(box, payload) {
    var wrap = el("div", "bi-tile-table");
    var table = el("table", "bi-restbl");
    var head = table.createTHead().insertRow();
    (payload.columns || []).forEach(function (c) {
      head.appendChild(el("th", null, c));
    });
    var body = table.createTBody();
    (payload.rows || []).forEach(function (r) {
      var tr = body.insertRow();
      r.forEach(function (cell) {
        tr.insertCell().textContent = cell;
      });
    });
    wrap.appendChild(table);
    box.innerHTML = "";
    box.appendChild(wrap);
  }

  function renderNumber(box, payload) {
    box.innerHTML = "";
    var wrap = el("div", "bi-tile-number");
    wrap.appendChild(el("div", "bi-kpi", payload.value || "—"));
    if (payload.caption) wrap.appendChild(el("div", "bi-kpi-cap", payload.caption));
    box.appendChild(wrap);
  }

  function renderCanvas(box, payload, canvasId) {
    box.innerHTML = "";
    var holder = el("div", "bi-tile-canvas");
    var canvas = document.createElement("canvas");
    canvas.id = canvasId;
    holder.appendChild(canvas);
    box.appendChild(holder);
    if (window.renderChart && payload.spec) window.renderChart(canvasId, payload.spec);
  }

  function fail(box, message) {
    box.innerHTML = "";
    var err = el("div", "bi-tile-err");
    err.appendChild(el("span", null, message));
    box.appendChild(err);
  }

  function load(box) {
    var tile = box.closest(".bi-tile");
    var grid = box.closest(".bi-tiles");
    var itemId = box.getAttribute("data-tile");
    var canvasId = box.getAttribute("data-canvas");
    // The base comes from the grid, not from location.pathname: on the editor
    // the path is /dashboards/x/edit, and deriving the URL from it asked for
    // /dashboards/x/edit/tiles/11 — a 404 that every tile reported as a failed
    // query. Carry the query string so a tile fetched after an Apply shows the
    // same numbers as the rest of the page.
    var base = (grid && grid.getAttribute("data-tiles")) || window.location.pathname + "/tiles";
    var url = base + "/" + itemId + window.location.search;

    return fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        var payload = res.body || {};
        if (!res.ok || payload.error) {
          fail(box, payload.error || "This chart's query failed.");
          return;
        }
        if (payload.renders_as === "table") renderTable(box, payload);
        else if (payload.renders_as === "number") renderNumber(box, payload);
        else renderCanvas(box, payload, canvasId);

        if (tile) {
          var warn = tile.querySelector(".bi-tile-warn");
          if (warn && payload.warnings && payload.warnings.length) {
            warn.textContent = payload.warnings.join(" ");
            warn.hidden = false;
          }
          var flag = tile.querySelector(".bi-tile-unfiltered");
          if (flag && payload.unfiltered) flag.hidden = false;
        }
      })
      .catch(function () {
        fail(box, "Could not load this chart.");
      });
  }

  function run() {
    var queue = Array.prototype.slice.call(document.querySelectorAll("[data-tile]"));
    var active = 0;

    function next() {
      if (!queue.length) return;
      if (active >= CONCURRENCY) return;
      var box = queue.shift();
      active++;
      load(box).then(function () {
        active--;
        next();
      });
      next();
    }
    for (var i = 0; i < CONCURRENCY; i++) next();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }

  /* Per-tile "Show SQL", so you can see what a chart does without leaving. */
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-sql-toggle]");
    if (!button) return;
    var tile = button.closest(".bi-tile");
    var pre = tile && tile.querySelector(".bi-tile-sql");
    if (!pre) return;
    pre.hidden = !pre.hidden;
    button.textContent = pre.hidden ? "Show SQL" : "Hide SQL";
  });
})();
