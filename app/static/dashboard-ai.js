/* "Painel com IA": a prompt describes the dashboard, the server asks the agent
 * to plan it, creates the charts + dashboard, and we jump into the editor. */
(function () {
  "use strict";

  var dlg = document.getElementById("dash-ai-dlg");
  var open = document.getElementById("dash-ai-open");
  if (!dlg || !open || !dlg.showModal) return;
  var GEN = dlg.getAttribute("data-gen");
  var prompt = document.getElementById("dash-ai-prompt");
  var gen = document.getElementById("dash-ai-gen");
  var status = document.getElementById("dash-ai-status");

  function setStatus(msg, cls) { status.textContent = msg || ""; status.className = "dash-ai-status" + (cls ? " " + cls : ""); }

  open.addEventListener("click", function () { setStatus(""); dlg.showModal(); prompt.focus(); });

  function generate() {
    var q = prompt.value.trim();
    if (!q) { setStatus("Descreva o painel.", "err"); return; }
    gen.disabled = true;
    setStatus("montando o painel… isso pode levar alguns segundos");
    fetch(GEN, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ prompt: q }),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (res.ok && res.body && res.body.ok) {
          setStatus("pronto — abrindo o editor…");
          window.location.href = res.body.url;
        } else {
          gen.disabled = false;
          setStatus((res.body && res.body.error) || "não foi possível montar o painel", "err");
        }
      })
      .catch(function () { gen.disabled = false; setStatus("falha ao montar o painel", "err"); });
  }

  gen.addEventListener("click", generate);
  prompt.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); generate(); }
  });
})();
