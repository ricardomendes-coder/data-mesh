/* Turns the tag field into chips you can add to and take from.
 *
 * The thing that posts is still the server-rendered <input name="tags">, comma
 * separated, exactly as before — this script hides it and keeps it in sync.
 * So the editor degrades to the plain text box rather than to nothing, and the
 * route on the other side never learns that any of this happened.
 *
 * Multiple tags always worked; the single pre-filled text box just never said
 * so. Once an item had one tag the placeholder was gone, and nothing suggested
 * a second was allowed.
 */
(function () {
  "use strict";

  function clean(raw) {
    return String(raw || "").trim().replace(/\s+/g, " ").slice(0, 60);
  }

  function parse(value) {
    var out = [];
    var seen = {};
    String(value || "").split(",").forEach(function (raw) {
      var name = clean(raw);
      var key = name.toLowerCase();
      if (name && !seen[key]) {
        seen[key] = 1;
        out.push(name);
      }
    });
    return out;
  }

  function enhance(form) {
    var field = form.querySelector('input[name="tags"]');
    if (!field || field.getAttribute("data-chips")) return;
    field.setAttribute("data-chips", "1");

    var names = parse(field.value);
    var removeLabel = form.getAttribute("data-remove-label") || "";

    var box = document.createElement("div");
    box.className = "bi-tagbox";
    var chips = document.createElement("span");
    chips.className = "bi-tagchips";
    var entry = document.createElement("input");
    entry.type = "text";
    entry.className = "bi-taginput";
    entry.setAttribute("list", "bi-tagvocab");
    entry.setAttribute("autocomplete", "off");
    entry.placeholder = form.getAttribute("data-add-label") || "";
    entry.setAttribute("aria-label", entry.placeholder);

    box.appendChild(chips);
    box.appendChild(entry);
    field.type = "hidden";
    field.parentNode.insertBefore(box, field);

    function draw() {
      field.value = names.join(", ");
      chips.innerHTML = "";
      names.forEach(function (name, i) {
        var chip = document.createElement("span");
        chip.className = "bi-tag bi-tag-on";
        chip.appendChild(document.createTextNode(name));
        var x = document.createElement("button");
        x.type = "button";
        x.className = "bi-tagx";
        x.title = removeLabel;
        x.textContent = "×";
        x.addEventListener("click", function () {
          names.splice(i, 1);
          draw();
          entry.focus();
        });
        chip.appendChild(x);
        chips.appendChild(chip);
      });
    }

    function add(raw) {
      var name = clean(raw);
      entry.value = "";
      if (!name) return;
      var key = name.toLowerCase();
      var already = names.some(function (n) {
        return n.toLowerCase() === key;
      });
      // Same word twice is one tag — slugify would merge them anyway, so
      // showing two identical chips would just be a lie about what got saved.
      if (!already) names.push(name);
      draw();
    }

    entry.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === ",") {
        // Enter in a form means submit. Here it means "that's one tag" — the
        // Save button is how you finish.
        e.preventDefault();
        add(entry.value);
      } else if (e.key === "Backspace" && !entry.value && names.length) {
        names.pop();
        draw();
      }
    });
    // Picking from the datalist doesn't go through keydown.
    entry.addEventListener("change", function () {
      add(entry.value);
    });
    // A half-typed word when Save is pressed is a tag the person meant to add.
    // Dropping it silently is how an editor loses someone's trust.
    form.addEventListener("submit", function () {
      add(entry.value);
    });

    draw();
  }

  Array.prototype.forEach.call(document.querySelectorAll("form.bi-tagform"), enhance);
})();
