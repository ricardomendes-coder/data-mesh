/* Pages a result table that's already in the DOM.
 *
 * The rows are rendered server-side (bounded by query_display_rows) and this
 * only toggles visibility, so paging is instant and needs no round trip. It
 * exists because a few thousand <tr> shown at once makes the page unusable —
 * scrolling, find-in-page and the browser's layout all suffer.
 *
 * Hiding rather than removing keeps Ctrl-F over the current page honest and
 * means no data is lost if the script fails: without JS every row simply
 * shows, which is the old behaviour rather than a broken one.
 */
(function () {
  "use strict";

  var table = document.getElementById("result-table");
  var pager = document.getElementById("result-pager");
  if (!table || !pager) return;

  var rows = Array.prototype.slice.call(table.tBodies[0].rows);
  var size = parseInt(table.getAttribute("data-page-size"), 10) || 50;
  if (rows.length <= size) return; // one page — leave the pager hidden

  var pages = Math.ceil(rows.length / size);
  var page = 0;
  var info = pager.querySelector(".bi-pager-info");

  function render() {
    var start = page * size;
    var end = start + size;
    rows.forEach(function (row, i) {
      row.hidden = i < start || i >= end;
    });
    info.textContent =
      "Page " + (page + 1) + " of " + pages +
      "  ·  rows " + (start + 1).toLocaleString() + "–" +
      Math.min(end, rows.length).toLocaleString() +
      " of " + rows.length.toLocaleString();
    pager.querySelectorAll("[data-page]").forEach(function (b) {
      var which = b.getAttribute("data-page");
      b.disabled =
        ((which === "first" || which === "prev") && page === 0) ||
        ((which === "last" || which === "next") && page === pages - 1);
    });
  }

  pager.addEventListener("click", function (event) {
    var button = event.target.closest("[data-page]");
    if (!button) return;
    var which = button.getAttribute("data-page");
    if (which === "first") page = 0;
    else if (which === "prev") page = Math.max(0, page - 1);
    else if (which === "next") page = Math.min(pages - 1, page + 1);
    else if (which === "last") page = pages - 1;
    render();
    table.parentElement.scrollTop = 0;
  });

  pager.hidden = false;
  render();
})();
