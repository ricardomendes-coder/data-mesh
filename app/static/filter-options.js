/* Fills the filter dropdowns the first time the drawer is opened.
 *
 * These option lists are `SELECT DISTINCT` over the warehouse — on one V360
 * dashboard, eleven of them totalling 292 seconds. The page used to wait on
 * all of that before rendering, for a drawer most visits never open. So it
 * waits until somebody actually opens it, and the server caches the result.
 *
 * The selects already carry whatever the URL selected, so a filtered link
 * works with this script absent or still in flight.
 */
(function () {
  "use strict";

  var drawer = document.querySelector(".bi-filterdrawer[data-options-url]");
  if (!drawer) return;

  var url = drawer.getAttribute("data-options-url");
  var loadingLabel = drawer.getAttribute("data-loading-label") || "";
  var started = false;

  function fill(options) {
    Object.keys(options || {}).forEach(function (key) {
      var select = drawer.querySelector('select[data-options="' + key + '"]');
      if (!select) return;
      // What the viewer already picked, so refilling never drops a choice.
      var chosen = {};
      Array.prototype.forEach.call(select.options, function (o) {
        if (o.selected && o.value) chosen[o.value] = true;
      });
      var first = select.options[0];
      select.innerHTML = "";
      if (first) select.appendChild(first);
      (options[key] || []).forEach(function (value) {
        var option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        if (chosen[value]) option.selected = true;
        select.appendChild(option);
      });
      // A value that is selected but no longer in the list still has to be
      // there, or applying the form would silently drop the filter.
      Object.keys(chosen).forEach(function (value) {
        if (select.querySelector('option[value="' + CSS.escape(value) + '"]')) return;
        var option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        option.selected = true;
        select.appendChild(option);
      });
      select.disabled = false;
    });
  }

  function load() {
    if (started) return;
    started = true;
    var note = document.createElement("div");
    note.className = "bi-sub bi-filter-loading";
    note.textContent = loadingLabel;
    var form = drawer.querySelector(".bi-filterpanel");
    if (form) form.insertBefore(note, form.firstChild);

    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (body) { fill(body && body.options); })
      .catch(function () { /* the selects keep what the URL gave them */ })
      .then(function () { if (note.parentNode) note.parentNode.removeChild(note); });
  }

  drawer.addEventListener("toggle", function () {
    if (drawer.open) load();
  });
  if (drawer.open) load();
})();
