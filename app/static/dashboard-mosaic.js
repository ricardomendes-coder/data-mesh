/* Draws each dashboard card's preview as a mosaic: the first few tiles at their
 * real grid positions, a slice of the panel rather than one lone chart.
 *
 * One fetch per card to /dashboards/<slug>/preview, which is cache-only — so
 * scrolling a listing of sixty-five dashboards runs no warehouse queries. A tile
 * whose preview isn't cached yet draws as a faint placeholder, so the mosaic
 * still shows the panel's shape and fills in with real charts as the cache warms.
 */
(function () {
  "use strict";

  var CONCURRENCY = 3;

  function el(tag, cls) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    return n;
  }

  // One tile's payload into a compact drawing: number, tiny table, canvas, or —
  // when nothing is cached yet — a placeholder that keeps the tile's footprint.
  function drawTile(cell, payload, canvasId, pending) {
    if (!payload) {
      cell.appendChild(el("div", "bi-mosaic-ph"));
      return;
    }
    if (payload.error) {
      cell.appendChild(el("div", "bi-mosaic-ph bi-mosaic-ph-err"));
      return;
    }
    if (payload.renders_as === "number") {
      var num = el("div", "bi-mosaic-num");
      num.textContent = payload.value || "—";
      cell.appendChild(num);
      return;
    }
    if (payload.renders_as === "table") {
      var rows = payload.rows || [];
      if (!rows.length) { cell.appendChild(el("div", "bi-mosaic-ph")); return; }
      var wrap = el("div", "bi-mosaic-tbl");
      var table = document.createElement("table");
      var head = table.createTHead().insertRow();
      (payload.columns || []).slice(0, 4).forEach(function (c) {
        var th = document.createElement("th");
        th.textContent = c;
        head.appendChild(th);
      });
      var body = table.createTBody();
      rows.slice(0, 6).forEach(function (r) {
        var tr = body.insertRow();
        r.slice(0, 4).forEach(function (v) { tr.insertCell().textContent = v; });
      });
      wrap.appendChild(table);
      cell.appendChild(wrap);
      return;
    }
    // canvas
    var holder = el("div", "bi-mosaic-canvas");
    var canvas = document.createElement("canvas");
    canvas.id = canvasId;
    holder.appendChild(canvas);
    cell.appendChild(holder);
    // Draw only after the grid is in the document — Chart.js sizes to the
    // canvas's parent, which reads 0×0 (and getElementById misses) while the
    // node is still detached. Defer to the caller once the tree is attached.
    if (window.renderChart && payload.spec) {
      pending.push({ canvas: canvas, spec: payload.spec });
    }
  }

  function render(box, data) {
    box.innerHTML = "";
    var tiles = (data && data.tiles) || [];
    if (!tiles.length) {
      var empty = el("div", "bi-prev-empty");
      empty.textContent = box.getAttribute("data-empty-label") || "";
      box.appendChild(empty);
      return;
    }
    var cols = (data && data.columns) || 12;
    // Vertical span of just these tiles, so the first rows fill the thumbnail.
    var yMin = Infinity, yMax = -Infinity;
    tiles.forEach(function (t) {
      yMin = Math.min(yMin, t.y);
      yMax = Math.max(yMax, t.y + t.h);
    });
    var ySpan = Math.max(1, yMax - yMin);

    var grid = el("div", "bi-mosaic-grid");
    var pending = [];
    tiles.forEach(function (t, i) {
      var cell = el("div", "bi-mosaic-cell");
      cell.style.left = (t.x / cols) * 100 + "%";
      cell.style.width = (t.w / cols) * 100 + "%";
      cell.style.top = ((t.y - yMin) / ySpan) * 100 + "%";
      cell.style.height = (t.h / ySpan) * 100 + "%";
      drawTile(cell, t.payload, (box.getAttribute("data-canvas") || "m") + "-" + i, pending);
      grid.appendChild(cell);
    });
    box.appendChild(grid);
    // Canvases are attached and sized now — draw them.
    pending.forEach(function (p) {
      window.renderChart(p.canvas, p.spec, { compact: true });
    });
  }

  function load(box) {
    var url = box.getAttribute("data-preview-url");
    return fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (!res.ok || !res.body || res.body.error) {
          box.innerHTML = "";
          box.appendChild(el("div", "bi-prev-empty"));
          return;
        }
        render(box, res.body);
      })
      .catch(function () {
        box.innerHTML = "";
        box.appendChild(el("div", "bi-prev-empty"));
      });
  }

  var queue = [];
  var active = 0;
  function next() {
    if (!queue.length || active >= CONCURRENCY) return;
    active++;
    load(queue.shift()).then(function () { active--; next(); });
    next();
  }
  function enqueue(box) {
    if (box.getAttribute("data-loaded")) return;
    box.setAttribute("data-loaded", "1");
    queue.push(box);
    next();
  }

  var cards = document.querySelectorAll("[data-preview-url]");
  if (!("IntersectionObserver" in window)) {
    Array.prototype.slice.call(cards, 0, 8).forEach(enqueue);
    return;
  }
  var observer = new IntersectionObserver(
    function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        observer.unobserve(entry.target);
        enqueue(entry.target);
      });
    },
    { rootMargin: "600px 0px" }
  );
  Array.prototype.forEach.call(cards, function (box) { observer.observe(box); });
})();
