/* Calendar page: airport/city suggestions + 3 months side by side. */
(function () {
  "use strict";

  var HORIZON_DAYS = 330;
  var EAGER_MONTHS = 3;
  var MAX_AIRPORTS = 6;

  /* The decimal separator varies by language (tr: ",", en: "."). */
  var DEC = (0.5).toLocaleString(FLX.locale()).charAt(1) || ".";

  var SYMBOLS = {
    EUR: "€", USD: "$", GBP: "£", TRY: "₺", CHF: "CHF",
    PLN: "zł", CZK: "Kč", HUF: "Ft", RON: "lei", BGN: "лв",
    SEK: "kr", NOK: "kr", DKK: "kr", CAD: "C$", AUD: "A$",
    JPY: "¥", AED: "AED"
  };

  var state = {
    from: { codes: [], name: "", near: false },
    to: { codes: [], name: "", near: false },
    direct: true,
    cur: "EUR",
    gen: 0,
    cells: {},
    lo: null,
    hi: null,
    io: null
  };

  var combos = {};

  function $(sel) { return document.querySelector(sel); }

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function iso(d) {
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" +
      pad(d.getDate());
  }

  function todayIso() {
    var t = new Date();
    return iso(new Date(t.getFullYear(), t.getMonth(), t.getDate()));
  }

  function monthList() {
    var now = new Date();
    var end = new Date(now.getFullYear(), now.getMonth(),
                       now.getDate() + HORIZON_DAYS);
    var y = now.getFullYear();
    var m = now.getMonth();          // zero-based
    var out = [];
    while (y < end.getFullYear() ||
           (y === end.getFullYear() && m <= end.getMonth())) {
      out.push(y + "-" + pad(m + 1));
      m += 1;
      if (m > 11) { m = 0; y += 1; }
    }
    return out;
  }

  function monthParts(key) {
    return [parseInt(key.slice(0, 4), 10), parseInt(key.slice(5, 7), 10)];
  }

  function fullDate(ds) {
    return FLX.fullDate(ds);
  }

  function ageText(sec) {
    sec = Math.max(0, parseInt(sec, 10) || 0);
    if (sec < 90) { return FLX.t("age_now"); }
    var mins = Math.round(sec / 60);
    if (mins < 90) { return FLX.t("age_min", { n: mins }); }
    return FLX.t("age_hour", { n: Math.round(mins / 60) });
  }

  /* ---------- currency ---------- */
  function symbol() { return SYMBOLS[state.cur] || state.cur; }

  function priceCell(p) {
    var n = Math.round(p);
    var txt;
    if (n >= 10000) {
      // 16,500 -> "16.5K", 16,000 -> "16K" (the suffix is always uppercase)
      var k = n / 1000;
      txt = (k % 1 === 0 ? String(k) : k.toFixed(1).replace(".", DEC)) +
        FLX.k();
    } else {
      txt = n.toLocaleString(FLX.locale());
    }
    return txt + " " + symbol();
  }

  /* ---------- selection / chips ---------- */
  function selText(sel) {
    if (!sel.codes.length) { return ""; }
    var codes = sel.codes.join(", ");
    return sel.name ? sel.name + " (" + codes + ")" : codes;
  }

  function parseCodes(text) {
    var raw = (text || "").toUpperCase().replace(/\s+/g, "");
    if (!raw) { return null; }
    var parts = raw.split(",");
    var out = [];
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i];
      if (!/^[A-Z]{3}$/.test(p)) { return null; }
      if (out.indexOf(p) < 0) { out.push(p); }
    }
    if (!out.length || out.length > MAX_AIRPORTS) { return null; }
    return out;
  }

  function overlapCodes(a, b) {
    return a.filter(function (c) { return b.indexOf(c) >= 0; });
  }

  function renderChips(which) {
    var host = $("#" + which + "-chips");
    if (!host) { return; }
    host.innerHTML = "";
    state[which].codes.forEach(function (code) {
      var chip = document.createElement("span");
      chip.className = "chip";
      chip.appendChild(document.createTextNode(code));
      var x = document.createElement("button");
      x.type = "button";
      x.className = "chipx";
      x.textContent = "×";
      x.setAttribute("aria-label", FLX.t("chip_remove", { code: code }));
      x.addEventListener("click", function (e) {
        e.preventDefault();
        removeCode(which, code);
      });
      chip.appendChild(x);
      host.appendChild(chip);
    });
  }

  function clearSelection(which, clearInput) {
    var sel = state[which];
    sel.codes = [];
    sel.name = "";
    renderChips(which);
    clearNearbyNote(which);
    if (clearInput) {
      var input = $("#" + which);
      if (input) { input.value = ""; }
    }
  }

  function removeCode(which, code) {
    var sel = state[which];
    sel.codes = sel.codes.filter(function (c) { return c !== code; });
    if (!sel.codes.length) {
      clearSelection(which, true);
      return;
    }
    var input = $("#" + which);
    if (input) { input.value = selText(sel); }
    renderChips(which);
    clearNearbyNote(which);
    if (bothSelected() && !overlapCodes(state.from.codes,
                                        state.to.codes).length) {
      start(true);
    }
  }

  function bothSelected() {
    return state.from.codes.length > 0 && state.to.codes.length > 0;
  }

  /* ---------- nearby airports (Skyscanner city group) ---------- */
  function nearbyChecked(which) {
    var cb = $("#" + which + "-nearby");
    return !!(cb && cb.checked);
  }

  function clearNearbyNote(which) {
    var note = $("#" + which + "-nearby-note");
    if (note) { note.textContent = ""; }
  }

  function renderNearbyNote(which, effective) {
    var note = $("#" + which + "-nearby-note");
    if (!note) { return; }
    if (!state[which].near) { note.textContent = ""; return; }
    var base = state[which].codes;
    var extra = (effective || []).filter(function (c) {
      return base.indexOf(c) < 0;
    });
    note.textContent = extra.length
      ? FLX.t("near_extra", { codes: extra.join(", ") })
      : FLX.t("near_none");
  }

  /* ---------- suggestions ---------- */
  function closeList(which) {
    var c = combos[which];
    if (!c) { return; }
    clearTimeout(c.timer);
    c.seq += 1;
    c.items = [];
    c.active = -1;
    c.list.hidden = true;
    c.input.setAttribute("aria-expanded", "false");
    c.input.removeAttribute("aria-activedescendant");
  }

  function markActive(which) {
    var c = combos[which];
    var nodes = c.list.querySelectorAll(".sug");
    for (var i = 0; i < nodes.length; i++) {
      var on = i === c.active;
      nodes[i].classList.toggle("active", on);
      nodes[i].setAttribute("aria-selected", on ? "true" : "false");
    }
    if (c.active >= 0 && nodes[c.active]) {
      c.input.setAttribute("aria-activedescendant",
                           which + "-opt-" + c.active);
      nodes[c.active].scrollIntoView({ block: "nearest" });
    } else {
      c.input.removeAttribute("aria-activedescendant");
    }
  }

  function renderList(which) {
    var c = combos[which];
    c.list.innerHTML = "";
    if (!c.items.length) {
      closeList(which);
      return;
    }
    c.items.forEach(function (item, i) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "sug";
      b.id = which + "-opt-" + i;
      b.setAttribute("role", "option");
      b.setAttribute("aria-selected", "false");
      b.dataset.i = String(i);

      var main = document.createElement("span");
      main.className = "sug-main";
      if (item.kind === "city") {
        var city = document.createElement("b");
        city.textContent = item.label;
        main.appendChild(city);
        var tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent = FLX.t("tag_city");
        main.appendChild(tag);
      } else {
        var code = document.createElement("b");
        code.className = "code";
        code.textContent = item.code;
        main.appendChild(code);
        var name = document.createElement("span");
        name.textContent = item.label;
        main.appendChild(name);
      }

      var sub = document.createElement("span");
      sub.className = "sug-sub";
      sub.textContent = item.kind === "city"
        ? FLX.t("all_airports",
                { codes: (item.airports || []).join(", ") })
        : (item.airport || item.cc || "");

      b.appendChild(main);
      b.appendChild(sub);
      c.list.appendChild(b);
    });
    c.list.hidden = false;
    c.input.setAttribute("aria-expanded", "true");
  }

  function fetchSuggest(which, query) {
    var c = combos[which];
    var seq = ++c.seq;
    var url = "/api/suggest?limit=12&q=" + encodeURIComponent(query || "");
    fetch(url, { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (seq !== c.seq) { return; }
        c.items = (res && res.items) || [];
        c.active = -1;
        renderList(which);
      })
      .catch(function () {
        if (seq !== c.seq) { return; }
        closeList(which);
      });
  }

  function scheduleSuggest(which) {
    var c = combos[which];
    clearTimeout(c.timer);
    c.timer = setTimeout(function () {
      fetchSuggest(which, c.input.value.trim());
    }, 170);
  }

  function choose(which, item) {
    var sel = state[which];
    sel.codes = (item.airports || [item.code]).slice(0, MAX_AIRPORTS);
    sel.name = item.label || (item.kind === "airport" ? item.code : "");
    var input = $("#" + which);
    input.value = selText(sel);
    renderChips(which);
    clearNearbyNote(which);
    closeList(which);
    input.blur();
    $("#form-msg").textContent = "";

    if (bothSelected()) {
      var over = overlapCodes(state.from.codes, state.to.codes);
      if (over.length) {
        $("#form-msg").textContent =
          FLX.t("err_overlap", { codes: over.join(", ") });
        return;
      }
      start(true);
    }
  }

  function setupCombo(which) {
    var input = $("#" + which);
    var list = $("#" + which + "-list");
    var c = {
      input: input,
      list: list,
      items: [],
      active: -1,
      seq: 0,
      timer: null
    };
    combos[which] = c;

    input.addEventListener("input", function () {
      if (state[which].codes.length &&
          input.value !== selText(state[which])) {
        clearSelection(which, false);
      }
      c.active = -1;
      scheduleSuggest(which);
    });

    input.addEventListener("focus", function () {
      clearTimeout(c.blurTimer);
      if (!input.value.trim()) {
        fetchSuggest(which, "");
      }
    });

    input.addEventListener("blur", function () {
      c.blurTimer = setTimeout(function () { closeList(which); }, 160);
    });

    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!c.items.length) {
          fetchSuggest(which, input.value.trim());
          return;
        }
        e.preventDefault();
        var n = c.items.length;
        if (e.key === "ArrowDown") {
          c.active = (c.active + 1) % n;
        } else {
          c.active = (c.active - 1 + n) % n;
        }
        markActive(which);
      } else if (e.key === "Enter") {
        if (c.active >= 0 && c.items[c.active]) {
          e.preventDefault();
          choose(which, c.items[c.active]);
        }
      } else if (e.key === "Escape") {
        closeList(which);
      }
    });

    /* prevent default on mousedown so the input doesn't blur and close the list */
    list.addEventListener("mousedown", function (e) { e.preventDefault(); });
    list.addEventListener("click", function (e) {
      var btn = e.target.closest(".sug");
      if (!btn) { return; }
      var item = c.items[parseInt(btn.dataset.i, 10)];
      if (item) { choose(which, item); }
    });
  }

  /* ---------- calendar cells ---------- */
  function buildMonth(key) {
    var parts = monthParts(key), y = parts[0], m = parts[1];
    var sec = document.createElement("section");
    sec.className = "month loading";
    sec.dataset.month = key;

    var head = document.createElement("div");
    head.className = "mhead";
    var h2 = document.createElement("h2");
    h2.textContent = FLX.monthsHead()[m - 1] + " ";
    var yr = document.createElement("span");
    yr.textContent = String(y);
    h2.appendChild(yr);
    var stat = document.createElement("div");
    stat.className = "mstat";
    stat.textContent = FLX.t("stat_loading");
    head.appendChild(h2);
    head.appendChild(stat);
    sec.appendChild(head);

    var wdHead = document.createElement("div");
    wdHead.className = "cal-head";
    FLX.wd().forEach(function (w) {
      var s = document.createElement("span");
      s.textContent = w;
      wdHead.appendChild(s);
    });
    sec.appendChild(wdHead);

    var grid = document.createElement("div");
    grid.className = "cal";
    var firstOff = (new Date(y, m - 1, 1).getDay() + 6) % 7;
    var dayCount = new Date(y, m, 0).getDate();
    var i;
    for (i = 0; i < firstOff; i++) {
      var b = document.createElement("span");
      b.className = "blank";
      grid.appendChild(b);
    }

    var tIso = todayIso();
    for (var d = 1; d <= dayCount; d++) {
      var ds = y + "-" + pad(m) + "-" + pad(d);
      var a = document.createElement("a");
      a.className = "day" + (ds < tIso ? " past" : "") +
        (ds === tIso ? " today" : "");
      a.dataset.date = ds;
      a.setAttribute("role", "gridcell");
      var dn = document.createElement("span");
      dn.className = "dn";
      dn.textContent = String(d);
      var pr = document.createElement("span");
      pr.className = "pr";
      a.appendChild(dn);
      a.appendChild(pr);
      if (ds < tIso) { a.setAttribute("aria-disabled", "true"); }
      grid.appendChild(a);
      state.cells[ds] = {
        el: a,
        priceEl: pr,
        price: null,
        past: ds < tIso
      };
    }
    var total = firstOff + dayCount;
    var tail = (7 - (total % 7)) % 7;
    for (i = 0; i < tail; i++) {
      var b2 = document.createElement("span");
      b2.className = "blank";
      grid.appendChild(b2);
    }
    sec.appendChild(grid);
    return sec;
  }

  function dayUrl(ds, p) {
    var q = new URLSearchParams({
      from: state.from.codes.join(","),
      to: state.to.codes.join(","),
      direct: state.direct ? "1" : "0",
      date: ds,
      price: String(p),
      cur: state.cur
    });
    if (state.from.name) { q.set("from_name", state.from.name); }
    if (state.to.name) { q.set("to_name", state.to.name); }
    if (state.from.near) { q.set("from_near", "1"); }
    if (state.to.near) { q.set("to_near", "1"); }
    return "/gun?" + q.toString();
  }

  function setPrice(cell, ds, p) {
    cell.price = p;
    cell.el.classList.add("priced");
    cell.el.classList.remove("none");
    cell.priceEl.textContent = priceCell(p);
    cell.el.href = dayUrl(ds, p);
    cell.el.title = fullDate(ds) + " · " + priceCell(p);
  }

  function setEmpty(cell, ds) {
    cell.price = null;
    cell.el.removeAttribute("href");
    cell.el.classList.add("none");
    cell.el.classList.remove("priced");
    cell.el.style.background = "";
    cell.priceEl.textContent = "—";
    cell.el.title = fullDate(ds) + " · " + FLX.t("no_price");
  }

  function recolor() {
    var lo = null, hi = null, ds, c;
    for (ds in state.cells) {
      if (!Object.prototype.hasOwnProperty.call(state.cells, ds)) continue;
      c = state.cells[ds];
      if (c.price == null) continue;
      if (lo === null || c.price < lo) { lo = c.price; }
      if (hi === null || c.price > hi) { hi = c.price; }
    }
    state.lo = lo;
    state.hi = hi;

    // The heat colour uses the same global scale; the outline goes to each
    // month's own cheapest price. All days tied at the cheapest get outlined.
    var monthMin = {};
    for (ds in state.cells) {
      if (!Object.prototype.hasOwnProperty.call(state.cells, ds)) continue;
      c = state.cells[ds];
      if (c.price == null) continue;
      var mk = ds.slice(0, 7);
      if (monthMin[mk] == null || c.price < monthMin[mk]) {
        monthMin[mk] = c.price;
      }
    }

    for (ds in state.cells) {
      if (!Object.prototype.hasOwnProperty.call(state.cells, ds)) continue;
      c = state.cells[ds];
      if (c.price == null) {
        c.el.classList.remove("best");
        c.el.style.background = "";
        continue;
      }
      var f = (hi === null || hi <= lo) ? 0.5 :
        (c.price - lo) / (hi - lo);
      f = Math.max(0, Math.min(1, f));
      var hue = Math.round(140 - 140 * f);
      c.el.style.background = "hsl(" + hue + ",72%,58%)";
      var mk2 = ds.slice(0, 7);
      if (monthMin[mk2] != null &&
          Math.abs(c.price - monthMin[mk2]) < 0.01) {
        c.el.classList.add("best");
      } else {
        c.el.classList.remove("best");
      }
    }
  }

  function applyMonth(key, sec, res) {
    var parts = monthParts(key), y = parts[0], m = parts[1];
    var dayCount = new Date(y, m, 0).getDate();
    var days = res.days || {};
    if (res.airports) {
      renderNearbyNote("from", res.airports.from || []);
      renderNearbyNote("to", res.airports.to || []);
    }
    var priced = 0;
    for (var d = 1; d <= dayCount; d++) {
      var ds = y + "-" + pad(m) + "-" + pad(d);
      var cell = state.cells[ds];
      if (!cell) continue;
      var p = days[ds];
      if (typeof p === "number") {
        setPrice(cell, ds, p);
        priced += 1;
      } else if (!cell.past) {
        setEmpty(cell, ds);
      }
    }
    var stat = sec.querySelector(".mstat");
    var prefix = res.stale ? FLX.t("stat_stale") : "";
    stat.textContent = (priced ? FLX.t("stat_priced", { n: priced }) :
                        FLX.t("stat_noprice")) + " · " + prefix +
      ageText(res.age || 0);
    if (res.note) { stat.title = res.note; }
    recolor();
  }

  function loadMonth(key, sec) {
    if (sec.dataset.state === "loading" || sec.dataset.state === "done") {
      return;
    }
    sec.dataset.state = "loading";
    sec.classList.add("loading");
    var gen = state.gen;
    var q = new URLSearchParams({
      from: state.from.codes.join(","),
      to: state.to.codes.join(","),
      direct: state.direct ? "1" : "0",
      month: key,
      cur: state.cur
    });
    if (state.from.near) { q.set("from_near", "1"); }
    if (state.to.near) { q.set("to_near", "1"); }
    fetch("/api/month?" + q.toString(), { cache: "no-store" })
      .then(function (r) {
        if (r.status === 401) { location.href = "/login"; }
        return r.json();
      })
      .then(function (res) {
        if (gen !== state.gen) { return; }
        if (!res.ok) { throw new Error(res.error || FLX.t("err_unknown")); }
        sec.dataset.state = "done";
        sec.classList.remove("loading");
        applyMonth(key, sec, res);
      })
      .catch(function (err) {
        if (gen !== state.gen) { return; }
        sec.dataset.state = "error";
        sec.classList.remove("loading");
        var stat = sec.querySelector(".mstat");
        stat.textContent = FLX.t("stat_failed");
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "retry";
        btn.textContent = FLX.t("retry");
        btn.addEventListener("click", function () {
          btn.remove();
          sec.dataset.state = "";
          stat.textContent = FLX.t("stat_loading");
          sec.classList.add("loading");
          loadMonth(key, sec);
        });
        stat.appendChild(btn);
        stat.title = String(err && err.message ? err.message : err);
        if (String(err && err.message || "").indexOf("Ayni havalimani") >= 0) {
          $("#form-msg").textContent =
            FLX.t("err_near_overlap", { msg: err.message });
        }
      });
  }

  function renderWelcome() {
    var host = $("#months");
    host.innerHTML = "";
    var box = document.createElement("div");
    box.className = "welcome";

    var icon = document.createElement("div");
    icon.className = "wicon";
    icon.textContent = "✈";
    box.appendChild(icon);

    var h2 = document.createElement("h2");
    h2.textContent = FLX.t("wel_title");
    box.appendChild(h2);

    var p1 = document.createElement("p");
    p1.textContent = FLX.t("wel_p1");
    box.appendChild(p1);

    var p2 = document.createElement("p");
    p2.className = "dimtext";
    p2.textContent = FLX.t("wel_p2");
    box.appendChild(p2);

    host.appendChild(box);
  }

  function buildMonths() {
    var host = $("#months");
    host.innerHTML = "";
    state.cells = {};
    state.lo = state.hi = null;
    if (state.io) { state.io.disconnect(); state.io = null; }

    if (!bothSelected()) {
      renderWelcome();
      return;
    }

    var keys = monthList();
    var sections = keys.map(function (key) {
      var sec = buildMonth(key);
      host.appendChild(sec);
      return sec;
    });

    if ("IntersectionObserver" in window) {
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          if (!en.isIntersecting) { return; }
          io.unobserve(en.target);
          loadMonth(en.target.dataset.month, en.target);
        });
      }, { rootMargin: "400px 0px" });
      state.io = io;
      sections.forEach(function (sec) { io.observe(sec); });
    } else {
      sections.forEach(function (sec) {
        loadMonth(sec.dataset.month, sec);
      });
    }

    // Load the first few months right away (the top of the screen).
    sections.slice(0, EAGER_MONTHS).forEach(function (sec) {
      loadMonth(sec.dataset.month, sec);
    });
  }

  /* ---------- form ---------- */
  function readInput(which) {
    var sel = state[which];
    var input = $("#" + which);
    var value = (input.value || "").trim();
    if (sel.codes.length && value === selText(sel)) {
      return true;
    }
    var codes = parseCodes(value);
    if (!codes) { return false; }
    sel.codes = codes;
    sel.name = "";
    input.value = codes.join(", ");
    renderChips(which);
    clearNearbyNote(which);
    return true;
  }

  function updateUrl() {
    var q = new URLSearchParams({
      from: state.from.codes.join(","),
      to: state.to.codes.join(","),
      direct: state.direct ? "1" : "0",
      cur: state.cur
    });
    if (state.from.name) { q.set("from_name", state.from.name); }
    if (state.to.name) { q.set("to_name", state.to.name); }
    if (state.from.near) { q.set("from_near", "1"); }
    if (state.to.near) { q.set("to_near", "1"); }
    history.replaceState(null, "", "?" + q.toString());
  }

  function start(updateUrlFlag) {
    var msg = $("#form-msg");
    var okFrom = readInput("from");
    var okTo = readInput("to");

    if (!okFrom || !okTo) {
      msg.textContent = FLX.t("err_pick");
      return;
    }
    var over = overlapCodes(state.from.codes, state.to.codes);
    if (over.length) {
      msg.textContent = FLX.t("err_overlap", { codes: over.join(", ") });
      return;
    }

    msg.textContent = "";
    state.direct = $("#direct").checked;
    state.cur = $("#cur").value;
    state.from.near = nearbyChecked("from");
    state.to.near = nearbyChecked("to");

    if (updateUrlFlag !== false) { updateUrl(); }

    $("#route-summary").textContent =
      selText(state.from) + " → " + selText(state.to) + " · " +
      (state.direct ? FLX.t("sum_direct") : FLX.t("sum_any")) +
      ((state.from.near || state.to.near)
        ? " · " + FLX.t("sum_near") : "") +
      " · " + FLX.t("sum_cur", { cur: state.cur });

    state.gen += 1;
    buildMonths();
  }

  function swap() {
    var a = state.from;
    state.from = state.to;
    state.to = a;
    $("#from").value = selText(state.from);
    $("#to").value = selText(state.to);
    $("#from-nearby").checked = state.from.near;
    $("#to-nearby").checked = state.to.near;
    clearNearbyNote("from");
    clearNearbyNote("to");
    renderChips("from");
    renderChips("to");
    if (bothSelected()) { start(true); }
  }

  function init() {
    document.title = FLX.t("title");
    var qs = new URLSearchParams(location.search);

    state.direct = qs.has("direct") ? qs.get("direct") !== "0" : true;
    $("#direct").checked = state.direct;

    state.from.near = qs.get("from_near") === "1";
    state.to.near = qs.get("to_near") === "1";
    $("#from-nearby").checked = state.from.near;
    $("#to-nearby").checked = state.to.near;

    var cur = (qs.get("cur") || "EUR").toUpperCase();
    if (!Object.prototype.hasOwnProperty.call(SYMBOLS, cur)) { cur = "EUR"; }
    state.cur = cur;
    $("#cur").value = cur;

    var f = parseCodes(qs.get("from") || "");
    var t = parseCodes(qs.get("to") || "");
    if (f) {
      state.from.codes = f;
      state.from.name = (qs.get("from_name") || "").trim().slice(0, 60);
    }
    if (t) {
      state.to.codes = t;
      state.to.name = (qs.get("to_name") || "").trim().slice(0, 60);
    }
    $("#from").value = selText(state.from);
    $("#to").value = selText(state.to);
    renderChips("from");
    renderChips("to");

    setupCombo("from");
    setupCombo("to");

    $("#route-form").addEventListener("submit", function (e) {
      e.preventDefault();
      start(true);
    });
    $("#direct").addEventListener("change", function () {
      if (bothSelected()) { start(true); }
    });
    ["from", "to"].forEach(function (which) {
      var cb = $("#" + which + "-nearby");
      if (!cb) { return; }
      cb.addEventListener("change", function () {
        state[which].near = cb.checked;
        clearNearbyNote(which);
        if (bothSelected()) { start(true); }
        else { updateUrl(); }
      });
    });
    $("#cur").addEventListener("change", function () {
      state.cur = $("#cur").value;
      if (bothSelected()) { start(true); }
    });
    $("#swap").addEventListener("click", swap);

    if (bothSelected()) {
      var over = overlapCodes(state.from.codes, state.to.codes);
      if (over.length) {
        $("#form-msg").textContent =
          FLX.t("err_overlap", { codes: over.join(", ") });
        buildMonths();
      } else {
        start(false);
      }
    } else {
      buildMonths();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
}());