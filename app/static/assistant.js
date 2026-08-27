/* The data assistant: a question goes to /assistant/ask, and the reply — an
 * explanation, the datasets it used, and a query — renders below. The query
 * gets an "abrir no Query" button that hands it to the console to run. */
(function () {
  "use strict";

  var root = document.querySelector(".asst");
  if (!root) return;
  var ASK = root.getAttribute("data-ask");
  var CONSOLE = root.getAttribute("data-console");
  var DB = root.getAttribute("data-db") || "";
  var form = document.getElementById("asst-form");
  var input = document.getElementById("asst-q");
  var out = document.getElementById("asst-out");
  var send = document.getElementById("asst-send");

  function el(tag, cls, txt) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (txt != null) n.textContent = txt;
    return n;
  }

  document.getElementById("asst-sugs").addEventListener("click", function (e) {
    if (!e.target.classList.contains("asst-sug")) return;
    input.value = e.target.textContent;
    ask();
  });

  form.addEventListener("submit", function (e) { e.preventDefault(); ask(); });
  input.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); ask(); }
  });

  function ask() {
    var q = input.value.trim();
    if (!q) return;
    send.disabled = true;
    out.innerHTML = "";
    out.appendChild(el("div", "asst-skel", "Pensando no caminho e montando a query…"));
    fetch(ASK, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ question: q }),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        send.disabled = false;
        if (!res.ok || !res.body || res.body.error) return fail(res.body || {});
        render(res.body);
      })
      .catch(function () { send.disabled = false; fail({ error: "Falha ao falar com o assistente." }); });
  }

  function fail(body) {
    out.innerHTML = "";
    var box = el("div", "asst-err");
    box.appendChild(el("span", null, body.error || "Não foi possível responder."));
    if (body.detail) box.appendChild(el("code", null, body.detail));
    out.appendChild(box);
  }

  function render(a) {
    out.innerHTML = "";
    if (a.explanation) {
      var c = el("div", "asst-card");
      c.appendChild(el("div", "asst-lbl", "Caminho"));
      c.appendChild(el("div", "asst-expl", a.explanation));
      if (a.datasets && a.datasets.length) {
        var ds = el("div", "asst-ds");
        a.datasets.forEach(function (name) {
          var link = el("a", null, name);
          link.href = datasetUrl(name);
          ds.appendChild(link);
        });
        c.appendChild(ds);
      }
      out.appendChild(c);
    }
    if (a.sql && a.sql.trim()) {
      var s = el("div", "asst-card");
      s.appendChild(el("div", "asst-lbl", "Query"));
      s.appendChild(el("pre", "asst-sql", a.sql));
      var bar = el("div", "asst-sqlbar");
      var run = el("a", "bi-btn sm", "Abrir no Query ▸");
      run.href = CONSOLE + "?tab=query&database=" + encodeURIComponent(DB) + "&sql=" + encodeURIComponent(a.sql);
      var copy = el("button", "bi-btn sm ghost", "Copiar");
      copy.type = "button";
      copy.addEventListener("click", function () {
        navigator.clipboard && navigator.clipboard.writeText(a.sql);
        copy.textContent = "Copiado ✓";
        setTimeout(function () { copy.textContent = "Copiar"; }, 1500);
      });
      bar.appendChild(run); bar.appendChild(copy);
      s.appendChild(bar);
      out.appendChild(s);
    }
  }

  function datasetUrl(name) {
    // /datasets/<name>, a sibling of the console path.
    return CONSOLE.replace(/\/query\/?$/, "/datasets/") + encodeURIComponent(name);
  }
})();
