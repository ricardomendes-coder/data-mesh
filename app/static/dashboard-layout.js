/* Drag and resize tiles on the dashboard grid, and save the result.
 *
 * Deliberately small and dependency-free: the grid is 12 CSS columns, a tile
 * carries its x/y/w/h in data attributes, and dragging maps the pointer to a
 * cell. No layout library, because the whole app ships without a build step.
 *
 * Placement is "put it where you dropped it" — tiles may sit side by side or
 * leave a gap, exactly as arranged. The grid rows are explicit, so nothing
 * reflows behind you after a drop.
 */
(function () {
  "use strict";

  // One grid per tab, so a tile's coordinates keep the meaning they have on
  // the view page. They used to share a single grid: every tab starts at y=0,
  // so the tabs piled onto each other, and since a save posts every tile one
  // small drag rewrote the whole dashboard into that flattened arrangement.
  var grids = document.querySelectorAll(".bi-tiles-editing");
  if (!grids.length) return;
  var grid = grids[0];  // any of them: they share columns, row height and URL

  var COLUMNS = parseInt(grid.getAttribute("data-columns"), 10) || 12;
  // One row is 8px, Superset's unit, and rows do not gap — vertical spacing is
  // carried in the coordinates. Columns still gap, so cellSize() below only
  // subtracts the horizontal gutters.
  var ROW_PX = parseInt(grid.getAttribute("data-row-px"), 10) || 8;
  var GAP_PX = 16;
  var ROW_GAP_PX = 0;
  var saveUrl = grid.getAttribute("data-save");
  var status = document.getElementById("layout-status");

  var dragging = null;

  function cellSize(tile) {
    // Measure the grid this tile actually sits in — they can differ in width
    // once tabs are laid out separately.
    var host = (tile && tile.closest(".bi-tiles-editing")) || grid;
    return (host.clientWidth - GAP_PX * (COLUMNS - 1)) / COLUMNS;
  }

  function readTile(tile) {
    return {
      x: parseInt(tile.getAttribute("data-x"), 10) || 0,
      y: parseInt(tile.getAttribute("data-y"), 10) || 0,
      w: parseInt(tile.getAttribute("data-w"), 10) || 6,
      // 50, not 5: a row is 8px now, so the old default was a 40px sliver.
      h: parseInt(tile.getAttribute("data-h"), 10) || 50,
    };
  }

  function place(tile, box) {
    tile.setAttribute("data-x", box.x);
    tile.setAttribute("data-y", box.y);
    tile.setAttribute("data-w", box.w);
    tile.setAttribute("data-h", box.h);
    tile.style.gridColumn = box.x + 1 + " / span " + box.w;
    tile.style.gridRow = box.y + 1 + " / span " + box.h;
  }

  /* Everything starts placed: a dashboard imported or built before the grid has
     tiles with no coordinates, and they'd all pile into cell 1 the moment one
     was dragged. So the first thing the editor does is give every tile the
     position it is already being shown at. */
  function seed() {
    // Per grid, and only the tiles that have no coordinates yet. This used to
    // run over one shared grid and — because the template never emitted
    // data-placed — reflowed *every* tile into a left-to-right stream on load.
    // The next drag saved that stream, which is how one small edit rewrote a
    // whole imported dashboard.
    Array.prototype.forEach.call(grids, function (host) {
      var x = 0;
      var y = 0;
      var rowHeight = 0;
      // Start below whatever is already placed, so a new tile lands in free
      // space instead of on top of an imported one.
      Array.prototype.forEach.call(host.querySelectorAll(".bi-tile[data-placed]"), function (t) {
        var b = readTile(t);
        y = Math.max(y, b.y + b.h + 2);
      });
      Array.prototype.forEach.call(host.querySelectorAll(".bi-tile"), function (tile) {
        if (tile.hasAttribute("data-placed")) return;
        var box = readTile(tile);
        if (x + box.w > COLUMNS) {
          x = 0;
          y += rowHeight || box.h;
          rowHeight = 0;
        }
        box.x = x;
        box.y = y;
        place(tile, box);
        tile.setAttribute("data-placed", "1");
        x += box.w;
        rowHeight = Math.max(rowHeight, box.h);
      });
    });
  }

  function markDirty() {
    if (status) {
      status.textContent = "Unsaved changes";
      status.className = "bi-sub bi-dirty";
    }
  }

  function collect() {
    // Every grid, not just the one that was dragged: save_layout writes the
    // dashboard's whole geometry, so omitting the other tabs would blank them.
    var all = document.querySelectorAll(".bi-tiles-editing .bi-tile");
    return Array.prototype.map.call(all, function (tile) {
      var box = readTile(tile);
      return {
        id: parseInt(tile.getAttribute("data-item"), 10),
        x: box.x, y: box.y, w: box.w, h: box.h,
        section_id: tile.getAttribute("data-section") || null,
      };
    }).filter(function (t) { return !isNaN(t.id); })
      .sort(function (a, b) { return a.y - b.y || a.x - b.x; });
  }

  function save() {
    if (status) status.textContent = "Saving…";
    return fetch(saveUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tiles: collect() }),
    })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r); })
      .then(function (j) {
        if (status) {
          status.textContent = "Layout saved (" + j.saved + " tiles)";
          status.className = "bi-sub";
        }
      })
      .catch(function () {
        if (status) {
          status.textContent = "Could not save the layout";
          status.className = "bi-sub bi-dirty";
        }
      });
  }

  function startDrag(event, tile, mode) {
    event.preventDefault();
    var cell = cellSize(tile);
    dragging = {
      tile: tile,
      mode: mode,
      startX: event.clientX,
      startY: event.clientY,
      box: readTile(tile),
      cell: cell,
    };
    tile.classList.add("bi-dragging");
  }

  document.addEventListener("pointerdown", function (event) {
    var handle = event.target.closest(".bi-drag");
    var resizer = event.target.closest(".bi-resize");
    var tile = event.target.closest(".bi-tile");
    if (!tile || !tile.closest(".bi-tiles-editing")) return;
    if (resizer) startDrag(event, tile, "resize");
    else if (handle) startDrag(event, tile, "move");
  });

  document.addEventListener("pointermove", function (event) {
    if (!dragging) return;
    var dx = Math.round((event.clientX - dragging.startX) / (dragging.cell + GAP_PX));
    var dy = Math.round((event.clientY - dragging.startY) / (ROW_PX + ROW_GAP_PX));
    var box = {
      x: dragging.box.x, y: dragging.box.y,
      w: dragging.box.w, h: dragging.box.h,
    };
    if (dragging.mode === "move") {
      box.x = Math.max(0, Math.min(COLUMNS - box.w, dragging.box.x + dx));
      box.y = Math.max(0, dragging.box.y + dy);
    } else {
      box.w = Math.max(1, Math.min(COLUMNS - box.x, dragging.box.w + dx));
      box.h = Math.max(1, dragging.box.h + dy);
    }
    place(dragging.tile, box);
  });

  document.addEventListener("pointerup", function () {
    if (!dragging) return;
    dragging.tile.classList.remove("bi-dragging");
    dragging = null;
    markDirty();
    save();
  });

  seed();
  var button = document.getElementById("save-layout");
  if (button) button.addEventListener("click", save);
})();
