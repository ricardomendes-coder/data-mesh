/* Renders the real chart inside each card on the Charts listing.
 *
 * Same shape as dashboard-render.js: the page arrives with skeletons and each
 * card fetches its own data, a few at a time. A catalogue of forty charts is
 * forty queries, so doing them up front would make the page useless — and
 * firing all forty at once would just move the queue into the warehouse.
 */
(function () {
  "use strict";

  var CONCURRENCY = 4;
  var grid = document.querySelector("[data-preview]");
  if (!grid) return;
  var base = grid.getAttribute("data-preview");

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function fail(box, message, detail) {
    box.innerHTML = "";
    var err = el("div", "bi-tile-err");
    err.appendChild(el("span", null, message));
    if (detail) err.appendChild(el("code", "bi-tile-err-detail", detail));
    box.appendChild(err);
  }

  function draw(box, payload) {
    box.innerHTML = "";
    if (payload.renders_as === "number") {
      var wrap = el("div", "bi-tile-number");
      wrap.appendChild(el("div", "bi-kpi", payload.value || "—"));
      if (payload.caption) wrap.appendChild(el("div", "bi-kpi-cap", payload.caption));
      box.appendChild(wrap);
      return;
    }
    if (payload.renders_as === "table") {
      // A bare header row over nothing reads as a broken preview. Say it.
      if (!(payload.rows || []).length) {
        box.appendChild(el("div", "bi-prev-nodata", box.getAttribute("data-empty-label") || ""));
        return;
      }
      var holder = el("div", "bi-tile-table");
      var table = el("table", "bi-restbl");
      var head = table.createTHead().insertRow();
      (payload.columns || []).forEach(function (c) { head.appendChild(el("th", null, c)); });
      var body = table.createTBody();
      (payload.rows || []).forEach(function (r) {
        var tr = body.insertRow();
        r.forEach(function (cell) { tr.insertCell().textContent = cell; });
      });
      holder.appendChild(table);
      box.appendChild(holder);
      return;
    }
    var canvasBox = el("div", "bi-tile-canvas");
    var canvas = document.createElement("canvas");
    canvas.id = box.getAttribute("data-canvas");
    canvasBox.appendChild(canvas);
    box.appendChild(canvasBox);
    if (window.renderChart && payload.spec) window.renderChart(canvas.id, payload.spec, { compact: true });
  }

  function load(box, force) {
    var id = box.getAttribute("data-chart");
    var url = base + "/" + id + "/data" + (force ? "?refresh=1" : "");
    return fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        var payload = res.body || {};
        // Same distinction the dashboard tiles make: a chart that is merely
        // slow must not read as a broken one.
        if (!res.ok) {
          fail(box, payload.error || grid.getAttribute("data-err-timeout") || "", payload.detail);
          return;
        }
        if (payload.error) {
          fail(box, payload.error, payload.detail);
          return;
        }
        draw(box, payload);
        // Previews are cached, so say how old this one is rather than letting
        // a month-old number pass for today's.
        if (payload.age) {
          var age = el("div", "bi-prev-age", payload.age);
          if (payload.cached) {
            var refresh = el("button", "bi-prev-refresh", "\u21bb");
            refresh.type = "button";
            refresh.title = box.getAttribute("data-refresh-label") || "";
            refresh.addEventListener("click", function () {
              box.removeAttribute("data-loaded");
              box.innerHTML = '<div class="bi-tile-skel"><span></span><span></span><span></span></div>';
              load(box, true);
            });
            age.appendChild(refresh);
          }
          box.appendChild(age);
        }
      })
      .catch(function () {
        box.innerHTML = "";
        box.appendChild(el("div", "bi-tile-err", ""));
      });
  }

  /* Only what you can see.
   *
   * A card's preview is a real query, and this catalogue has 580 charts —
   * fetching them all on load is 580 queries for the dozen you actually look
   * at, and the page never finishes. So a card is fetched when it comes near
   * the viewport, and once only. */
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

  var cards = document.querySelectorAll("[data-chart]");
  if (!("IntersectionObserver" in window)) {
    // No observer (old engine): fall back to loading the first screenful, not
    // everything — the point is to never fire hundreds of queries at once.
    Array.prototype.slice.call(cards, 0, 12).forEach(enqueue);
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
    // Start a screen early so a chart is usually ready by the time it arrives.
    { rootMargin: "600px 0px" }
  );
  Array.prototype.forEach.call(cards, function (box) { observer.observe(box); });
})();
