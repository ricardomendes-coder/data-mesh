/* Renders every tile on a dashboard from one embedded payload.
 *
 * The page ships a map of canvas id -> chart spec (already built server-side by
 * app/charts.py), so the browser does no querying and no colour decisions.
 * Tiles whose query failed have no canvas and no entry here.
 */
(function () {
  "use strict";
  var node = document.getElementById("dashboard-data");
  if (!node || !window.renderChart) return;

  var specs;
  try {
    specs = JSON.parse(node.textContent);
  } catch (e) {
    return;
  }

  Object.keys(specs).forEach(function (id) {
    if (specs[id]) window.renderChart(id, specs[id]);
  });
})();
