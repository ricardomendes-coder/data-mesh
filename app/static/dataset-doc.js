/* Admin doc editor on a dataset page: generate a draft with the AI, edit it,
 * save it. Descriptions/examples saved here override datasets.toml. */
(function () {
  "use strict";

  var box = document.getElementById("ds-doc");
  if (!box) return;
  var GEN = box.getAttribute("data-gen");
  var SAVE = box.getAttribute("data-save");
  var el = function (id) { return document.getElementById(id); };
  var status = el("ds-doc-status");

  function setStatus(msg, cls) { status.textContent = msg || ""; status.className = "ds-doc-status" + (cls ? " " + cls : ""); }

  // prefill the examples textarea from the current doc
  try {
    var seed = JSON.parse(document.getElementById("ds-examples-json").textContent || "[]");
    el("ds-examples").value = JSON.stringify(seed, null, 2);
  } catch (e) { el("ds-examples").value = "[]"; }

  function readExamples() {
    var raw = el("ds-examples").value.trim();
    if (!raw) return [];
    return JSON.parse(raw); // may throw — caller catches
  }

  var gen = el("ds-gen");
  if (gen) {
    gen.addEventListener("click", function () {
      gen.disabled = true; setStatus("gerando com IA…");
      fetch(GEN, { method: "POST", headers: { Accept: "application/json" } })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
        .then(function (res) {
          gen.disabled = false;
          if (!res.ok || !res.body || res.body.error) { setStatus((res.body && res.body.error) || "falhou", "err"); return; }
          el("ds-title").value = res.body.title || "";
          el("ds-desc").value = res.body.description || "";
          el("ds-examples").value = JSON.stringify(res.body.examples || [], null, 2);
          setStatus("rascunho gerado e salvo ✓ — revise e salve as edições", "ok");
        })
        .catch(function () { gen.disabled = false; setStatus("falha ao gerar", "err"); });
    });
  }

  el("ds-save").addEventListener("click", function () {
    var examples;
    try { examples = readExamples(); }
    catch (e) { setStatus("Exemplos: JSON inválido — " + e.message, "err"); return; }
    var btn = el("ds-save"); btn.disabled = true; setStatus("salvando…");
    fetch(SAVE, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ title: el("ds-title").value, description: el("ds-desc").value, examples: examples }),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        btn.disabled = false;
        if (res.ok && res.body && res.body.ok) { setStatus("salvo ✓ — recarregue para ver", "ok"); }
        else { setStatus((res.body && res.body.error) || "erro ao salvar", "err"); }
      })
      .catch(function () { btn.disabled = false; setStatus("erro ao salvar", "err"); });
  });
})();
