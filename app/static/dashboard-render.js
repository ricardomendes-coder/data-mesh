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

  function fail(box, message, detail) {
    box.innerHTML = "";
    var err = el("div", "bi-tile-err");
    err.appendChild(el("span", null, message));
    // The real database error, for whoever can fix the chart — this is a
    // login-gated internal tool, the same reason the query console shows it.
    if (detail) err.appendChild(el("code", "bi-tile-err-detail", detail));
    box.appendChild(err);
  }

  function load(box, force) {
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
    // A forced refresh has to say so *and* survive an existing query
    // string, which is why this is appended rather than assigned.
    if (force) url += (url.indexOf("?") === -1 ? "?" : "&") + "refresh=1";

    return fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        var payload = res.body || {};
        // A server-reported SQL error and "the request never came back" are
        // very different things, and printing one sentence for both is how a
        // tile that is merely slow reads as a broken chart.
        if (!res.ok) {
          fail(box, payload.error || label("timeout"), payload.detail);
          return;
        }
        if (payload.error) {
          fail(box, payload.error, payload.detail);
          return;
        }
        if (payload.renders_as === "table") renderTable(box, payload);
        else if (payload.renders_as === "number") renderNumber(box, payload);
        else renderCanvas(box, payload, canvasId);
        // Cached results carry their age and a way to force a fresh one.
        // Superset caches the same queries for 24 hours and never says so; a
        // number that looks current and is a day old is the failure mode this
        // whole tool exists to avoid.
        stamp(box, payload);

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
        fail(box, label("unreachable"));
      });
  }


  function label(name) {
    var host = document.querySelector("[data-tiles]");
    return (host && host.getAttribute("data-err-" + name)) || "";
  }

  function stamp(box, payload) {
    if (!payload.age) return;
    var host = document.querySelector("[data-tiles]");
    var age = el("div", "bi-prev-age");
    age.appendChild(el("span", null, payload.age));
    if (payload.cached) {
      var refresh = el("button", "bi-prev-refresh", "\u21bb");
      refresh.type = "button";
      refresh.title = (host && host.getAttribute("data-refresh-label")) || "";
      refresh.addEventListener("click", function () {
        box.innerHTML = '<div class="bi-tile-skel"><span></span><span></span><span></span></div>';
        load(box, true);
      });
      age.appendChild(refresh);
    }
    box.appendChild(age);
  }

  // One global scheduler: every tile that needs loading — the ones here on
  // arrival, and the ones on a tab opened later — goes through a single queue
  // with a single concurrency cap. They all hit the same warehouse, so a shared
  // cap stops a freshly-opened tab from firing a second burst on top of the
  // first. A tile carries `data-queued` while it waits and `data-loaded` once
  // it has run, so nothing is fetched twice.
  var queue = [];
  var active = 0;

  function pump() {
    while (active < CONCURRENCY && queue.length) {
      active++;
      (function (item) {
        load(item.box, item.force).then(function () {
          item.box.setAttribute("data-loaded", "1");
          item.box.removeAttribute("data-queued");
          active--;
          if (item.onEach) item.onEach();
          pump();
        });
      })(queue.shift());
    }
  }

  function enqueue(boxes, force, onEach) {
    boxes.forEach(function (box) {
      if (box.getAttribute("data-queued") === "1") return;
      if (box.getAttribute("data-loaded") === "1" && !force) return;
      box.setAttribute("data-queued", "1");
      queue.push({ box: box, force: force, onEach: onEach });
    });
    pump();
  }

  function tilesIn(root) {
    return Array.prototype.slice.call(root.querySelectorAll("[data-tile]"));
  }

  // A tile is "here now" unless it sits on a hidden tab. Loading only the
  // visible pane on arrival is the whole point: a seven-tab dashboard used to
  // fire every tab's queries at once, most for tabs no one opened.
  function onVisiblePane(box) {
    var pane = box.closest(".pane[data-pane]");
    return !pane || pane.classList.contains("active");
  }

  // The tab strip calls this the first time a pane is shown. Already-loaded
  // tiles are skipped, so flipping back and forth between tabs costs nothing.
  window.dashboardLoadPane = function (pane) {
    if (pane) enqueue(tilesIn(pane), false);
  };

  function start() {
    enqueue(tilesIn(document).filter(onVisiblePane), false);
    wireRefreshAll();
  }

  /* "Atualizar" in the topbar: rebuild only the tiles on screen — the visible
     pane (plus any loose tiles above the tabs). Tabs you opened earlier but
     aren't looking at, and tabs never opened, are left as they are: a refresh
     shouldn't re-run a heavy query for something you can't even see. Can't be
     fired twice at once. */
  function wireRefreshAll() {
    var button = document.getElementById("refresh-dashboard");
    if (!button) return;
    button.addEventListener("click", function () {
      if (button.getAttribute("aria-busy") === "true") return;
      var boxes = Array.prototype.slice
        .call(document.querySelectorAll('[data-tile][data-loaded="1"]'))
        .filter(onVisiblePane);
      if (!boxes.length) return;
      button.setAttribute("aria-busy", "true");
      var pending = boxes.length;
      boxes.forEach(function (box) {
        box.innerHTML =
          '<div class="bi-tile-skel"><span></span><span></span><span></span></div>';
        box.removeAttribute("data-loaded");
      });
      enqueue(boxes, true, function () {
        if (--pending <= 0) button.removeAttribute("aria-busy");
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
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
