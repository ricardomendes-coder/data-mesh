/* The dashboard filter drawer: searchable multi-selects, saved views, close.
 *
 * Supersedes filter-options.js. The old control was <select multiple size=1>
 * with up to 1000 options and no search — impossible to use. Each <select>
 * stays the form control (so the GET form still posts the right values with
 * this script absent), hidden, with a chip picker built over it. Options are
 * fetched from filter-options only when the drawer opens, because each is a
 * SELECT DISTINCT over a large table.
 */
(function () {
  "use strict";

  var drawer = document.querySelector(".bi-filterdrawer");
  if (!drawer) return;

  var url = drawer.getAttribute("data-options-url");
  var searchLabel = drawer.getAttribute("data-search-label") || "";
  var allLabel = drawer.getAttribute("data-all-label") || "";
  var MENU_CAP = 200; // cap the rendered rows; typing narrows past it

  function el(tag, cls) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    return n;
  }
  function norm(s) {
    return (s || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  }

  // Close on the ×, on the scrim, and on Escape.
  drawer.querySelectorAll("[data-drawer-close]").forEach(function (b) {
    b.addEventListener("click", function () { drawer.removeAttribute("open"); });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && drawer.open) drawer.removeAttribute("open");
  });

  // Build a picker over every select up front, from what's already chosen, so
  // it works before (and without) the fetch. Refreshed once options arrive.
  var pickers = {};
  drawer.querySelectorAll("select[data-options]").forEach(function (sel) {
    pickers[sel.getAttribute("data-options")] = buildPicker(sel);
  });

  var loaded = false;
  function load() {
    if (loaded) return;
    loaded = true;
    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (body) {
        var options = (body && body.options) || {};
        Object.keys(pickers).forEach(function (key) {
          if (options[key]) pickers[key].setOptions(options[key]);
        });
      })
      .catch(function () { /* the selects keep what the URL gave them */ });
  }
  drawer.addEventListener("toggle", function () { if (drawer.open) load(); });
  if (drawer.open) load();

  function buildPicker(sel) {
    sel.hidden = true; // still the form control, just not shown
    var wrap = el("div", "bi-ms");
    var control = el("div", "bi-ms-control");
    var chips = el("span", "bi-ms-chips");
    var input = el("input", "bi-ms-input");
    input.type = "text";
    var menu = el("div", "bi-ms-menu");
    menu.hidden = true;
    control.appendChild(chips);
    control.appendChild(input);
    wrap.appendChild(control);
    wrap.appendChild(menu);
    sel.parentNode.insertBefore(wrap, sel.nextSibling);

    function chosen() {
      return Array.prototype.filter.call(sel.options, function (o) { return o.selected; })
        .map(function (o) { return o.value; });
    }
    function toggle(val, on) {
      Array.prototype.forEach.call(sel.options, function (o) {
        if (o.value === val) o.selected = on;
      });
      renderChips();
      renderMenu();
    }
    function renderChips() {
      chips.innerHTML = "";
      var picked = chosen();
      picked.forEach(function (val) {
        var chip = el("span", "bi-ms-chip");
        chip.appendChild(document.createTextNode(val));
        var x = el("button", "bi-ms-chip-x");
        x.type = "button";
        x.textContent = "×";
        x.addEventListener("click", function (e) { e.stopPropagation(); toggle(val, false); });
        chip.appendChild(x);
        chips.appendChild(chip);
      });
      input.placeholder = picked.length ? "" : allLabel;
    }
    function renderMenu() {
      var q = norm(input.value.trim());
      menu.innerHTML = "";
      var shown = 0;
      var more = 0;
      Array.prototype.forEach.call(sel.options, function (o) {
        if (!o.value) return;
        if (q && norm(o.value).indexOf(q) === -1) return;
        if (shown >= MENU_CAP) { more++; return; }
        shown++;
        var row = el("label", "bi-ms-opt");
        var cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = o.selected;
        cb.addEventListener("change", function () { toggle(o.value, cb.checked); });
        row.appendChild(cb);
        row.appendChild(document.createTextNode(o.value));
        menu.appendChild(row);
      });
      if (shown === 0) {
        var none = el("div", "bi-ms-none");
        none.textContent = "—";
        menu.appendChild(none);
      } else if (more > 0) {
        var hint = el("div", "bi-ms-more");
        hint.textContent = "+" + more + "…";
        menu.appendChild(hint);
      }
    }

    control.addEventListener("click", function () { menu.hidden = false; input.focus(); });
    input.addEventListener("input", function () { menu.hidden = false; renderMenu(); });
    document.addEventListener("click", function (e) {
      if (!wrap.contains(e.target)) menu.hidden = true;
    });

    renderChips();
    renderMenu();

    return {
      setOptions: function (values) {
        var picked = chosen();
        sel.innerHTML = "";
        var all = values.slice();
        // A chosen value no longer in the list stays selectable, or applying
        // the form would silently drop the filter.
        picked.forEach(function (c) { if (all.indexOf(c) === -1) all.push(c); });
        all.forEach(function (v) {
          var o = document.createElement("option");
          o.value = v;
          o.textContent = v;
          o.selected = picked.indexOf(v) !== -1;
          sel.appendChild(o);
        });
        input.placeholder = searchLabel;
        renderChips();
        renderMenu();
      },
    };
  }
})();
