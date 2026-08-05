/* Select-all / clear for a permission section.
 *
 * Scoped to the clicked button's own <form>, because each resource type is a
 * separate form — a page-wide handler would tick Charts while you were editing
 * Databases.
 *
 * The "all <type>" wildcard box is deliberately excluded: it means "every
 * resource of this type, including ones created later", which is a different
 * grant from ticking today's items. Rolling it into select-all would silently
 * change what you're saving.
 */
(function () {
  "use strict";

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-select]");
    if (!button) return;

    var form = button.closest("form");
    if (!form) return;

    var wanted = button.getAttribute("data-select") === "all";
    var boxes = form.querySelectorAll('input[type="checkbox"][name="keys"]');
    Array.prototype.forEach.call(boxes, function (box) {
      if (box.closest(".bi-chip-any")) return; // leave the wildcard alone
      box.checked = wanted;
    });
  });
})();
