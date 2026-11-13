/* Day page: the cheapest ticket(s) of the selected date + booking link. */
(function () {
  "use strict";

  var MAX_AIRPORTS = 6;

  document.title = FLX.t("tickets_title");

  var SYMBOLS = {
    EUR: "€", USD: "$", GBP: "£", TRY: "₺", CHF: "CHF",
    PLN: "zł", CZK: "Kč", HUF: "Ft", RON: "lei", BGN: "лв",
    SEK: "kr", NOK: "kr", DKK: "kr", CAD: "C$", AUD: "A$",
    JPY: "¥", AED: "AED"
  };

  function $(sel) { return document.querySelector(sel); }
  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function fullDate(ds) {
    return FLX.fullDate(ds);
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text != null) { n.textContent = text; }
    return n;
  }

  function fmtPrice(p) {
    var n = Math.round(p);
    return n.toLocaleString(FLX.locale()) + " " + (SYMBOLS[cur] || cur);
  }

  function fmtDur(mins) {
    if (typeof mins !== "number" || mins <= 0) { return null; }
    var h = Math.floor(mins / 60), m = mins % 60;
    return h ? FLX.t("dur_hm", { h: h, m: pad(m) })
             : FLX.t("dur_m", { m: m });
  }

  function stopsText(n) {
    if (n === 0) { return FLX.t("stops_0"); }
    if (n == null) { return "—"; }
    if (n === 1) { return FLX.t("stops_1"); }
    return FLX.t("stops_n", { n: n });
  }

  function parseCodes(value) {
    var raw = (value || "").toUpperCase().replace(/\s+/g, "");
    if (!raw) { return null; }
    var parts = raw.split(",");
    var out = [];
    for (var i = 0; i < parts.length; i++) {
      if (!/^[A-Z]{3}$/.test(parts[i])) { return null; }
      if (out.indexOf(parts[i]) < 0) { out.push(parts[i]); }
    }
    if (!out.length || out.length > MAX_AIRPORTS) { return null; }
    return out;
  }

  var qs = new URLSearchParams(location.search);
  var from = parseCodes(qs.get("from") || "") || [];
  var to = parseCodes(qs.get("to") || "") || [];
  var fromName = (qs.get("from_name") || "").trim().slice(0, 60);
  var toName = (qs.get("to_name") || "").trim().slice(0, 60);
  var cur = (qs.get("cur") || "EUR").toUpperCase();
  if (!Object.prototype.hasOwnProperty.call(SYMBOLS, cur)) { cur = "EUR"; }
  var direct = qs.get("direct") !== "0";
  var fromNear = qs.get("from_near") === "1";
  var toNear = qs.get("to_near") === "1";
  var date = qs.get("date") || "";
  var expected = parseFloat(qs.get("price"));

  function overlap() {
    return from.filter(function (c) { return to.indexOf(c) >= 0; });
  }

  function paramsOk() {
    return from.length > 0 && to.length > 0 && !overlap().length &&
      /^\d{4}-\d\d-\d\d$/.test(date);
  }

  function fromLabel() { return fromName || from.join(", "); }
  function toLabel() { return toName || to.join(", "); }
  function routeCodes() {
    return from.join(", ") + " → " + to.join(", ");
  }
  function multiAirport() {
    return from.length > 1 || to.length > 1;
  }

  function fallbackUrl() {
    var q = "Flights from " + from.join(",") + " to " + to.join(",") +
      " on " + date;
    return "https://www.google.com/travel/flights?q=" +
      encodeURIComponent(q) + "&curr=" + encodeURIComponent(cur) +
      "&hl=" + encodeURIComponent(FLX.hl());
  }

  function buyLink(href, text) {
    var a = el("a", "buy", text);
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    return a;
  }

  function ticketCard(t, searchUrl, showRoute) {
    var card = el("div", "ticket");
    var main = el("div", "tk-main");

    if (showRoute && t.origin && t.destination) {
      main.appendChild(el("div", "tk-route",
                          t.origin + " → " + t.destination));
    }

    var air = el("div", "tk-air");
    var names = (t.airlines && t.airlines.length)
      ? t.airlines.join(" + ") : FLX.t("airline_unknown");
    air.appendChild(el("b", null, names));
    if (t.flight_nos && t.flight_nos.length) {
      air.appendChild(el("span", null, t.flight_nos.join(" · ")));
    }
    main.appendChild(air);

    var times = el("div", "tk-times");
    times.appendChild(document.createTextNode(t.dep_time || "—"));
    times.appendChild(el("span", "arrow", "→"));
    times.appendChild(document.createTextNode(t.arr_time || "—"));
    if (t.arr_date && t.dep_date && t.arr_date !== t.dep_date) {
      times.appendChild(el("span", "plus", FLX.t("plus1")));
    }
    main.appendChild(times);

    var meta = [stopsText(t.stops)];
    var dur = fmtDur(t.duration);
    if (dur) { meta.push(dur); }
    if (t.aircraft) { meta.push(t.aircraft); }
    main.appendChild(el("div", "tk-meta", meta.join(" · ")));

    if (t.via && t.via.length) {
      main.appendChild(el("div", "tk-via",
        FLX.t("via", { airports: t.via.join(", ") })));
    }

    var buy = el("div", "tk-buy");
    buy.appendChild(el("div", "tk-price", fmtPrice(t.price)));
    buy.appendChild(buyLink(t.google_url || searchUrl,
                            FLX.t("buy")));

    card.appendChild(main);
    card.appendChild(buy);
    return card;
  }

  function showFallback(message) {
    var note = $("#note");
    note.textContent = message;
    var app = $("#app");
    app.innerHTML = "";
    app.appendChild(el("div", "emptybox", FLX.t("search_hint")));
    app.appendChild(buyLink(fallbackUrl(), FLX.t("search")));
  }

  function render(res) {
    var app = $("#app");
    var note = $("#note");
    var tickets = res.tickets || [];
    var searchUrl = res.search_url || fallbackUrl();
    var showRoute = multiAirport() || (res.pairs || 1) > 1;

    app.innerHTML = "";
    note.textContent = "";

    if (res.cheapest == null || !tickets.length) {
      note.textContent = FLX.t("no_flights");
      app.appendChild(el("div", "emptybox", FLX.t("search_hint")));
      app.appendChild(buyLink(searchUrl, FLX.t("search")));
      return;
    }

    if (expected != null && isFinite(expected) &&
        Math.abs(expected - res.cheapest) >= 0.5) {
      note.appendChild(document.createTextNode(
        FLX.t("price_diff", { a: fmtPrice(expected),
                              b: fmtPrice(res.cheapest) })));
    } else {
      note.appendChild(el("b", null, fmtPrice(res.cheapest)));
      note.appendChild(document.createTextNode(
        " · " + FLX.t("cheapest_of",
                      { n: res.offers || tickets.length })));
      if (tickets.length > 1) {
        note.appendChild(document.createTextNode(
          " " + FLX.t("same_price", { n: tickets.length })));
      }
      note.appendChild(document.createTextNode("."));
    }

    if (res.partial && res.note) {
      note.appendChild(document.createTextNode(" "));
      note.appendChild(el("span", "warn", res.note + "."));
    }
    if (res.stale) {
      note.appendChild(document.createTextNode(" "));
      note.appendChild(el("span", "warn", FLX.t("stale_warn")));
    }
    if (res.age > 90) {
      note.appendChild(document.createTextNode(
        " " + FLX.t("fetched_min", { n: Math.round(res.age / 60) })));
    }

    $("#title").textContent = fromLabel() + " → " + toLabel();
    $("#sub").textContent = fullDate(date) + " · " +
      (direct ? FLX.t("sum_direct") : FLX.t("sum_any")) +
      ((fromNear || toNear) ? " · " + FLX.t("sum_near") : "") +
      (multiAirport() ? " · " + routeCodes() : "") +
      " · " + fmtPrice(res.cheapest);

    tickets.forEach(function (t) {
      app.appendChild(ticketCard(t, searchUrl, showRoute));
    });
  }

  function init() {
    if (!paramsOk()) {
      $("#note").textContent = FLX.t("err_invalid");
      $("#app").innerHTML = '<div class="emptybox"><a href="/">' +
        FLX.t("back") + '</a></div>';
      return;
    }

    var back = new URLSearchParams({
      from: from.join(","),
      to: to.join(","),
      direct: direct ? "1" : "0",
      cur: cur
    });
    if (fromName) { back.set("from_name", fromName); }
    if (toName) { back.set("to_name", toName); }
    if (fromNear) { back.set("from_near", "1"); }
    if (toNear) { back.set("to_near", "1"); }
    $("#back").href = "/?" + back.toString();

    $("#title").textContent = fromLabel() + " → " + toLabel();
    $("#sub").textContent = fullDate(date) + " · " +
      (direct ? FLX.t("sum_direct") : FLX.t("sum_any")) +
      ((fromNear || toNear) ? " · " + FLX.t("sum_near") : "") +
      (multiAirport() ? " · " + routeCodes() : "");
    document.title = fromLabel() + " → " + toLabel() + " · " +
      fullDate(date);

    var q = new URLSearchParams({
      from: from.join(","),
      to: to.join(","),
      direct: direct ? "1" : "0",
      date: date,
      cur: cur
    });
    if (isFinite(expected)) { q.set("price", String(expected)); }
    if (fromNear) { q.set("from_near", "1"); }
    if (toNear) { q.set("to_near", "1"); }

    fetch("/api/day?" + q.toString(), { cache: "no-store" })
      .then(function (r) {
        if (r.status === 401) { location.href = "/login"; }
        return r.json();
      })
      .then(function (res) {
        if (!res.ok) { throw new Error(res.error || FLX.t("err_unknown")); }
        render(res);
      })
      .catch(function (err) {
        showFallback(FLX.t("err_day", {
          msg: (err && err.message ? err.message : err)
        }));
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
}());