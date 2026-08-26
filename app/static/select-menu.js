/* A styled dropdown to replace the native <select> across the studio — one look
 * everywhere, and a search box that re-filters freely (the native datalist only
 * re-listed after you cleared what you'd typed).
 *
 * enhanceSelect(select, opts) drives a real <select> (keeps its value and change
 * event, so existing handlers are untouched). enhanceCombo(input, opts) drives a
 * free-text field with a re-openable suggestion list — for the format patterns,
 * where you may type anything.
 *
 * The popup is portalled to <body> and positioned fixed, so it is never clipped
 * by the inspector's own scroll; it closes on scroll, resize, Escape or an
 * outside click.
 */
(function () {
  "use strict";

  var open = null; // the menu currently showing

  function el(tag, cls, txt) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (txt != null) n.textContent = txt;
    return n;
  }

  function place(m) {
    var r = m.anchor.getBoundingClientRect();
    var pop = m.pop;
    pop.style.position = "fixed";
    pop.style.left = r.left + "px";
    pop.style.minWidth = r.width + "px";
    // Flip above when there isn't room below.
    var below = window.innerHeight - r.bottom;
    pop.style.maxHeight = Math.max(160, Math.min(300, (below > 220 ? below : r.top) - 12)) + "px";
    if (below < 220 && r.top > below) {
      pop.style.top = "auto";
      pop.style.bottom = (window.innerHeight - r.top + 4) + "px";
    } else {
      pop.style.bottom = "auto";
      pop.style.top = (r.bottom + 4) + "px";
    }
  }

  function show(m) {
    if (open && open !== m) hide(open);
    document.body.appendChild(m.pop);
    m.pop.hidden = false;
    m.wrap.classList.add("sm-open");
    place(m);
    open = m;
    if (m.search) { m.search.value = ""; m.filter(""); m.search.focus(); }
    else { m.filter(m.input ? m.input.value.toLowerCase() : ""); }
    m.setActive(m.firstVisible());
    m.scrollActive();
  }
  function hide(m) {
    if (!m) return;
    m.pop.hidden = true;
    if (m.pop.parentNode) m.pop.parentNode.removeChild(m.pop);
    m.wrap.classList.remove("sm-open");
    if (open === m) open = null;
  }

  document.addEventListener("mousedown", function (e) {
    if (open && !open.wrap.contains(e.target) && !open.pop.contains(e.target)) hide(open);
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && open) hide(open); });
  window.addEventListener("scroll", function () { if (open) hide(open); }, true);
  window.addEventListener("resize", function () { if (open) hide(open); });

  function menu(wrap, anchor, pop, list) {
    var m = { wrap: wrap, anchor: anchor, pop: pop, list: list, opts: [], activeIdx: -1, search: null, input: null };
    m.firstVisible = function () { for (var i = 0; i < m.opts.length; i++) if (!m.opts[i].hidden) return i; return -1; };
    m.setActive = function (i) { m.opts.forEach(function (o, idx) { o.classList.toggle("sm-active", idx === i); }); m.activeIdx = i; };
    m.scrollActive = function () { var a = m.opts[m.activeIdx]; if (a && a.scrollIntoView) a.scrollIntoView({ block: "nearest" }); };
    m.filter = function (q) {
      var any = false;
      m.opts.forEach(function (o) { var hit = !q || o.getAttribute("data-t").indexOf(q) >= 0; o.hidden = !hit; if (hit) any = true; });
      if (!any) m.opts.forEach(function (o) { o.hidden = false; });
    };
    m.nav = function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        var vis = []; m.opts.forEach(function (o, i) { if (!o.hidden) vis.push(i); });
        var pos = vis.indexOf(m.activeIdx) + (e.key === "ArrowDown" ? 1 : -1);
        if (pos < 0) pos = 0; if (pos >= vis.length) pos = vis.length - 1;
        if (vis.length) { m.setActive(vis[pos]); m.scrollActive(); }
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (m.activeIdx >= 0 && !m.opts[m.activeIdx].hidden) m.opts[m.activeIdx].click();
      }
    };
    return m;
  }

  window.enhanceSelect = function (sel, opts) {
    opts = opts || {};
    if (sel.dataset.smDone) return; sel.dataset.smDone = "1";
    var wrap = el("div", "sm"); sel.parentNode.insertBefore(wrap, sel); wrap.appendChild(sel);
    sel.classList.add("sm-native");
    var trigger = el("button", "sm-trigger"); trigger.type = "button";
    var label = el("span", "sm-label");
    trigger.appendChild(label); trigger.appendChild(el("span", "sm-caret", "▾")); wrap.appendChild(trigger);
    var pop = el("div", "sm-pop"); pop.hidden = true;
    var list;
    var m = menu(wrap, trigger, pop, null);
    if (opts.search) {
      m.search = el("input", "sm-search"); m.search.type = "text";
      m.search.placeholder = opts.searchPlaceholder || "buscar…";
      pop.appendChild(m.search);
      m.search.addEventListener("input", function () { m.filter(m.search.value.toLowerCase()); m.setActive(m.firstVisible()); });
      m.search.addEventListener("keydown", m.nav);
    }
    list = el("div", "sm-list"); pop.appendChild(list); m.list = list;

    function updateLabel() {
      var o = sel.options[sel.selectedIndex];
      label.textContent = o && o.value !== "" ? o.textContent : (opts.placeholder || (o ? o.textContent : ""));
      label.classList.toggle("sm-ph", !o || o.value === "");
    }
    function rebuild() {
      list.innerHTML = ""; m.opts = [];
      Array.prototype.forEach.call(sel.options, function (o, i) {
        if (opts.skipEmpty && o.value === "") return;
        var opt = el("div", "sm-opt", o.textContent);
        opt.setAttribute("data-i", i); opt.setAttribute("data-t", o.textContent.toLowerCase());
        if (i === sel.selectedIndex) opt.classList.add("sm-sel");
        opt.addEventListener("click", function () {
          sel.selectedIndex = i; sel.dispatchEvent(new Event("change", { bubbles: true }));
          updateLabel(); m.opts.forEach(function (x) { x.classList.toggle("sm-sel", +x.getAttribute("data-i") === i); });
          hide(m); trigger.focus();
        });
        list.appendChild(opt); m.opts.push(opt);
      });
      updateLabel();
    }
    trigger.addEventListener("click", function () { pop.hidden ? show(m) : hide(m); });
    trigger.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") { e.preventDefault(); show(m); }
    });
    pop.addEventListener("keydown", m.nav);
    rebuild();
    sel._smSync = function () { rebuild(); }; // if code changes the value/options
    return m;
  };

  window.enhanceCombo = function (input, suggestions) {
    if (input.dataset.smDone) return; input.dataset.smDone = "1";
    if (input.getAttribute("list")) input.removeAttribute("list"); // kill the native one
    var wrap = el("div", "sm sm-combo"); input.parentNode.insertBefore(wrap, input); wrap.appendChild(input);
    input.classList.add("sm-combo-input");
    var caret = el("button", "sm-caret sm-combo-caret", "▾"); caret.type = "button"; wrap.appendChild(caret);
    var pop = el("div", "sm-pop"); pop.hidden = true;
    var list = el("div", "sm-list"); pop.appendChild(list);
    var m = menu(wrap, input, pop, list);
    (suggestions || []).forEach(function (s) {
      var o = el("div", "sm-opt", s); o.setAttribute("data-t", s.toLowerCase());
      o.addEventListener("mousedown", function (e) {
        e.preventDefault(); input.value = s; input.dispatchEvent(new Event("input", { bubbles: true })); hide(m); input.focus();
      });
      list.appendChild(o); m.opts.push(o);
    });
    input.addEventListener("focus", function () { if (pop.hidden) show(m); });
    input.addEventListener("input", function () { if (pop.hidden) show(m); else { m.filter(input.value.toLowerCase()); m.setActive(m.firstVisible()); } });
    caret.addEventListener("mousedown", function (e) { e.preventDefault(); pop.hidden ? (input.focus()) : hide(m); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { if (pop.hidden) show(m); m.nav(e); }
      else if (e.key === "Enter" && !pop.hidden && m.activeIdx >= 0 && !m.opts[m.activeIdx].hidden) {
        e.preventDefault(); m.opts[m.activeIdx].dispatchEvent(new MouseEvent("mousedown"));
      }
    });
    return m;
  };
})();
