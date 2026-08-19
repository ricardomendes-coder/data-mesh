/* Live filter for the Charts and Dashboards listings.
 *
 * Everything is already on the page (cards or rows), so this hides what
 * doesn't match rather than reloading — a reload would re-run the lazy preview
 * fetches on every keystroke. Accent-insensitive, because the titles are
 * Portuguese and nobody types "operação" to find "Operacao".
 */
(function () {
  "use strict";

  var input = document.querySelector("[data-search]");
  if (!input) return;

  function norm(s) {
    return (s || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  }

  var empty = document.querySelector("[data-search-empty]");

  function apply() {
    var q = norm(input.value.trim());
    var shown = 0;
    document.querySelectorAll("[data-search-item]").forEach(function (el) {
      var hay = norm(el.getAttribute("data-search-text") || el.textContent);
      var match = !q || hay.indexOf(q) !== -1;
      el.hidden = !match;
      if (match) shown++;
    });
    // The note only makes sense once something has been typed.
    if (empty) empty.hidden = shown > 0 || !q;
  }

  input.addEventListener("input", apply);
})();
