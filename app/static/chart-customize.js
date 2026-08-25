/* The chart's "Personalizar" panel: legend placement, a subtitle, grid lines
 * and axis label formats. Every change re-renders the chart live (through
 * biChartApplyOptions in chart-detail.js — no query re-run), and Save persists
 * the options blob. Display-only: it never touches the chart's SQL.
 */
(function () {
  "use strict";

  var form = document.getElementById("bi-customize");
  if (!form) return;

  var saveUrl = form.getAttribute("data-save");
  var stored = {};
  try { stored = JSON.parse(form.getAttribute("data-options") || "{}") || {}; } catch (e) {}

  function field(name) { return form.querySelector("[name=" + name + "]"); }
  var els = {
    legend: field("legend"),
    subtitle: field("subtitle"),
    gridx: field("gridx"),
    gridy: field("gridy"),
    xfmt: field("xfmt"),
    yfmt: field("yfmt"),
  };

  // Initialise from the stored options, falling back to the render defaults
  // (no legend override, x grid off, y grid on, automatic formats).
  els.legend.value = (stored.legend && stored.legend.position) || "";
  els.subtitle.value = stored.subtitle || "";
  els.gridx.checked = !!(stored.grid && stored.grid.x === true);
  els.gridy.checked = !(stored.grid && stored.grid.y === false);
  els.xfmt.value = (stored.xAxis && stored.xAxis.format) || "";
  els.yfmt.value = (stored.yAxis && stored.yAxis.format) || "";

  function collect() {
    var o = {};
    if (els.legend.value) o.legend = { position: els.legend.value };
    if (els.subtitle.value.trim()) o.subtitle = els.subtitle.value.trim();
    o.grid = { x: els.gridx.checked, y: els.gridy.checked };
    if (els.xfmt.value) o.xAxis = { format: els.xfmt.value };
    if (els.yfmt.value) o.yAxis = { format: els.yfmt.value };
    return o;
  }

  var status = form.querySelector(".bi-customize-status");
  var saveBtn = form.querySelector("[type=submit]");

  function preview() {
    if (window.biChartApplyOptions) window.biChartApplyOptions(collect());
    if (saveBtn) saveBtn.disabled = false;
    if (status) { status.textContent = ""; status.className = "bi-customize-status"; }
  }

  Object.keys(els).forEach(function (k) {
    var ev = k === "subtitle" ? "input" : "change";
    els[k].addEventListener(ev, preview);
  });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (saveBtn) saveBtn.disabled = true;
    if (status) { status.textContent = "salvando…"; status.className = "bi-customize-status"; }
    fetch(saveUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(collect()),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (!res.ok || (res.body && res.body.error)) {
          if (status) {
            status.textContent = (res.body && res.body.error) || "erro ao salvar";
            status.className = "bi-customize-status err";
          }
          if (saveBtn) saveBtn.disabled = false;
          return;
        }
        if (status) { status.textContent = "salvo ✓"; status.className = "bi-customize-status ok"; }
      })
      .catch(function () {
        if (status) { status.textContent = "erro ao salvar"; status.className = "bi-customize-status err"; }
        if (saveBtn) saveBtn.disabled = false;
      });
  });
})();
