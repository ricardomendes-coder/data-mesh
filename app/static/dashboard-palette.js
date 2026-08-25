/* The editor's chart library: drag a chart from the palette onto a grid to add
 * it as a tile where you drop it. Pairs with dashboard-layout.js — this places
 * the tile, that moves and resizes it. One POST to /items/place, then the page
 * reloads so the new tile renders through the same macro as every other.
 *
 * Deliberately dependency-free and reload-on-drop: replicating the tile macro
 * in JS would be a second renderer to keep in step. A reload is a beat slower
 * but always draws the tile exactly as the server would.
 */
(function () {
  "use strict";

  var palette = document.getElementById("bi-palette");
  var grids = document.querySelectorAll(".bi-tiles-editing");
  if (!palette || !grids.length) return;

  var addUrl = palette.getAttribute("data-add");
  var COLUMNS = parseInt(palette.getAttribute("data-columns"), 10) || 12;
  var ROW_PX = parseInt(palette.getAttribute("data-row-px"), 10) || 8;

  // Client-side filter over the (possibly long) chart list.
  var search = palette.querySelector("[data-role=search]");
  var chips = Array.prototype.slice.call(palette.querySelectorAll(".bi-palchip"));
  if (search) {
    search.addEventListener("input", function () {
      var q = search.value.trim().toLowerCase();
      chips.forEach(function (c) {
        var hit = !q || c.getAttribute("data-title").toLowerCase().indexOf(q) >= 0;
        c.style.display = hit ? "" : "none";
      });
    });
  }

  // The chart being dragged. Kept in a variable as well as dataTransfer because
  // dragover can't read the payload in every browser, and we need it to allow
  // the drop.
  var draggingSlug = null;
  chips.forEach(function (chip) {
    chip.addEventListener("dragstart", function (e) {
      draggingSlug = chip.getAttribute("data-chart-slug");
      if (e.dataTransfer) {
        e.dataTransfer.setData("text/plain", draggingSlug);
        e.dataTransfer.effectAllowed = "copy";
      }
    });
    chip.addEventListener("dragend", function () { draggingSlug = null; });
  });

  var busy = false;

  Array.prototype.forEach.call(grids, function (grid) {
    grid.addEventListener("dragover", function (e) {
      if (draggingSlug == null) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
      grid.classList.add("bi-drop-active");
    });
    grid.addEventListener("dragleave", function (e) {
      // Only clear when the pointer actually left the grid, not on a child.
      if (e.target === grid) grid.classList.remove("bi-drop-active");
    });
    grid.addEventListener("drop", function (e) {
      grid.classList.remove("bi-drop-active");
      var slug = draggingSlug ||
        (e.dataTransfer && e.dataTransfer.getData("text/plain"));
      if (!slug || busy) return;
      e.preventDefault();
      busy = true;

      var rect = grid.getBoundingClientRect();
      var colW = rect.width / COLUMNS || 1;
      var x = Math.max(0, Math.min(COLUMNS - 1, Math.floor((e.clientX - rect.left) / colW)));
      var y = Math.max(0, Math.floor((e.clientY - rect.top) / ROW_PX));
      var section = grid.getAttribute("data-section") || "";

      fetch(addUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          chart_slug: slug,
          section_id: section ? parseInt(section, 10) : null,
          x: x,
          y: y,
          w: 6,
          h: 5,
        }),
      })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
        .then(function (res) {
          if (res.ok && res.body && res.body.ok) {
            window.location.reload();
          } else {
            busy = false;
            alert((res.body && res.body.error) || "Não foi possível adicionar o gráfico.");
          }
        })
        .catch(function () {
          busy = false;
          alert("Falha ao adicionar o gráfico.");
        });
    });
  });
})();
