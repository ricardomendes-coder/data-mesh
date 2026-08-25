/* Tagging is picking from a list, not typing.
 *
 * Tags are created in Administration → Tags and nowhere else, so this editor
 * only ever offers words that already exist. A field that invents a tag on
 * submit is how a vocabulary turns into "financeiro", "Financeiro " and
 * "finaceiro" meaning the same thing and finding three different sets.
 *
 * The form still posts the server-rendered <input name="tags">, comma
 * separated — the route and the store never learn this happened. The list is
 * built here, from one copy of the vocabulary shared by the whole page,
 * because a listing can hold 580 cards and each carrying its own copy of the
 * options is a megabyte of markup saying the same few words.
 */
(function () {
  "use strict";

  // A <details> popover only toggles from its summary; on its own it never
  // closes when you click away or press Escape, and it leaves one open behind
  // the cards that follow it. Close any open tag editor on an outside click or
  // Escape. Registered before the vocab check so it works regardless.
  function closeOpen(except) {
    Array.prototype.forEach.call(
      document.querySelectorAll("details.bi-tagedit[open]"),
      function (d) { if (d !== except) d.removeAttribute("open"); }
    );
  }
  document.addEventListener("click", function (e) {
    var here = e.target.closest ? e.target.closest("details.bi-tagedit") : null;
    closeOpen(here);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" || e.keyCode === 27) closeOpen(null);
  });

  // Lift the whole card (or table row) above its neighbours while its editor is
  // open, so the popover isn't painted behind the cards that come after it. A
  // class rather than :has() so it works on every engine.
  Array.prototype.forEach.call(document.querySelectorAll("details.bi-tagedit"), function (d) {
    d.addEventListener("toggle", function () {
      var host = d.closest(".bi-card") || d.closest("tr");
      if (host) host.classList.toggle("bi-tagediting", d.open);
    });
  });

  var node = document.getElementById("bi-tagvocab");
  if (!node) return;
  var vocab;
  try {
    vocab = JSON.parse(node.textContent) || [];
  } catch (e) {
    return;
  }

  function key(name) {
    return String(name || "").trim().toLowerCase();
  }

  function enhance(form) {
    var field = form.querySelector('input[name="tags"]');
    if (!field || field.getAttribute("data-picker")) return;
    field.setAttribute("data-picker", "1");

    // What the item carries now, kept as the vocabulary's own spelling so a
    // save can't quietly rename anything.
    var chosen = {};
    String(field.value || "")
      .split(",")
      .forEach(function (raw) {
        if (key(raw)) chosen[key(raw)] = true;
      });

    field.type = "hidden";
    var menu = document.createElement("div");
    menu.className = "bi-tagmenu";

    if (!vocab.length) {
      var empty = document.createElement("p");
      empty.className = "bi-tagmenu-empty";
      empty.textContent = form.getAttribute("data-empty-label") || "";
      menu.appendChild(empty);
    }

    function sync() {
      var names = vocab.filter(function (name) {
        return chosen[key(name)];
      });
      field.value = names.join(", ");
    }

    vocab.forEach(function (name) {
      var label = document.createElement("label");
      label.className = "bi-chip-check bi-tagpick";
      var box = document.createElement("input");
      box.type = "checkbox";
      box.checked = !!chosen[key(name)];
      var text = document.createElement("span");
      text.textContent = name;
      box.addEventListener("change", function () {
        if (box.checked) chosen[key(name)] = true;
        else delete chosen[key(name)];
        sync();
      });
      label.appendChild(box);
      label.appendChild(text);
      menu.appendChild(label);
    });

    field.parentNode.insertBefore(menu, field);
    // A tag the item carries that is no longer in the vocabulary would have no
    // checkbox, and since this posts the full set, saving would silently drop
    // it. Rebuilding from the boxes on load makes that visible instead: what
    // you see is exactly what a save will keep.
    sync();
  }

  Array.prototype.forEach.call(document.querySelectorAll("form.bi-tagform"), enhance);
})();
