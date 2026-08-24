/* Submits the Charts / Dashboards search a beat after you stop typing.
 *
 * The search is server-side now — it filters the whole catalogue and then
 * paginates, which a client-side filter of the twenty rendered cards could not
 * do. Debouncing keeps it feeling live without a reload on every keystroke, and
 * the caret is restored to the end after the results come back.
 */
(function () {
  "use strict";

  var form = document.querySelector("[data-search-form]");
  var input = form && form.querySelector("[data-search-input]");
  if (!form || !input) return;

  // After a search reload the input is repainted; put the cursor back at the end.
  if (input.value) {
    input.focus();
    var v = input.value;
    input.value = "";
    input.value = v;
  }

  var timer;
  input.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(function () { form.submit(); }, 400);
  });
  // Enter (or the debounce firing) submits; either way, cancel the other.
  form.addEventListener("submit", function () { clearTimeout(timer); });
})();
