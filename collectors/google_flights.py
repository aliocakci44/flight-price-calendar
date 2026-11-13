# -*- coding: utf-8 -*-
"""Google Flights collector — stdlib-only, Python 3.8+, phone-friendly.

Three parts in a single file:
1) tfs protobuf encoder (flights.proto layout, hand-rolled varint encoding)
2) urllib fetch (no TLS impersonation; tested: Google serves data to a
   normal client too, no consent wall appeared)
3) ds:1 script regex + pure json parsing (selectolax/lxml NOT required)

Privacy: NO cookies/sessions are stored; every query is a fresh, single
stdlib HTTP request. Google returns fare-cached data; no per-query signal
reaches the airline.
"""
import base64
import json
import re
import statistics
import urllib.parse
import urllib.request

FLIGHTS_URL = "https://www.google.com/travel/flights/search"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")


# ---------- 1) protobuf encoder ----------
def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field, wire):
    return _varint((field << 3) | wire)


def _s(field, val):
    b = val.encode("utf-8") if isinstance(val, str) else val
    return _tag(field, 2) + _varint(len(b)) + b


def _i(field, val):
    return _tag(field, 0) + _varint(val)


def encode_tfs(dep, origin, dest, ret=None, seat=1, trip=None,
               passengers=(1,), max_stops=None):
    """flights.proto Info -> base64 tfs.
    seat: 1=economy 2=premium-economy 3=business 4=first
    trip: 1=round-trip 2=one-way (None: decided by ret)
    Verified: byte-for-byte identical to the fast_flights v3.1 output."""
    legs = [dict(dep=dep, origin=origin, dest=dest)]
    if ret:
        legs.append(dict(dep=ret, origin=dest, dest=origin))
    if trip is None:
        trip = 1 if ret else 2

    msg = b""
    for leg in legs:
        fd = _s(2, leg["dep"])
        fd += _s(13, _s(2, leg["origin"]))
        fd += _s(14, _s(2, leg["dest"]))
        if max_stops is not None:
            fd += _i(5, max_stops)
        msg += _s(3, fd)

    packed = b"".join(_varint(p) for p in passengers)
    msg += _s(8, packed)      # passengers: proto3 packed repeated enum
    msg += _i(9, seat)        # seat
    msg += _i(19, trip)       # trip
    return base64.b64encode(msg).decode("ascii")


# ---------- 2) fetch ----------
def fetch(dep, origin, dest, ret=None, currency="EUR", max_stops=None):
    tfs = encode_tfs(dep, origin, dest, ret=ret, max_stops=max_stops)
    params = {"tfs": tfs, "hl": "en", "curr": currency}
    url = FLIGHTS_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------- 3) parser ----------
def _at(seq, i):
    """Safe index access (for missing/None fields)."""
    try:
        return seq[i]
    except (IndexError, KeyError, TypeError):
        return None


def _hhmm(v):
    if not isinstance(v, (list, tuple)) or len(v) < 2:
        return None
    try:
        return "%02d:%02d" % (int(v[0]), int(v[1]))
    except (TypeError, ValueError):
        return None


def _ymd(v):
    if not isinstance(v, (list, tuple)) or len(v) < 3:
        return None
    try:
        return "%04d-%02d-%02d" % (int(v[0]), int(v[1]), int(v[2]))
    except (TypeError, ValueError):
        return None


def _dt(date_s, time_s):
    if date_s and time_s:
        return date_s + " " + time_s
    return date_s or time_s or None


def _parse_segments(flight, airlines_map):
    segs = []
    for seg in (_at(flight, 2) or []):
        if not isinstance(seg, (list, tuple)):
            continue
        fn = _at(seg, 22)
        flight_no = None
        airline = None
        if isinstance(fn, (list, tuple)) and len(fn) >= 2 and fn[1]:
            code = fn[0] or ""
            flight_no = ("%s%s" % (code, fn[1])).strip() or None
            if fn[0]:
                airline = airlines_map.get(fn[0], fn[0])
        if not airline and isinstance(fn, (list, tuple)) and len(fn) >= 4:
            airline = fn[3]
        segs.append({
            "from": _at(seg, 3),
            "to": _at(seg, 6),
            "from_name": _at(seg, 4),
            "to_name": _at(seg, 5),
            "duration": _at(seg, 11),
            "dep_time": _hhmm(_at(seg, 8)),
            "arr_time": _hhmm(_at(seg, 10)),
            "dep_date": _ymd(_at(seg, 20)),
            "arr_date": _ymd(_at(seg, 21)),
            "flight_no": flight_no,
            "airline": airline,
            "aircraft": _at(seg, 17),
        })
    return segs


def _parse_layovers(flight, segs):
    lay = []
    for l in (_at(flight, 13) or []):
        if not isinstance(l, (list, tuple)) or len(l) < 2:
            continue
        lay.append({"code": _at(l, 1), "minutes": _at(l, 0),
                    "name": _at(l, 4)})
    if not lay and len(segs) > 1:
        # In some responses flight[13] is empty; derive it from segments.
        for s in segs[1:]:
            lay.append({"code": s["from"], "minutes": None,
                        "name": s["from_name"]})
    return lay


def parse_flights(html_text):
    m = re.search(r'<script[^>]*class="ds:1"[^>]*>(.*?)</script>', html_text,
                  re.DOTALL)
    if not m:
        raise RuntimeError("ds:1 script not found (consent/bot wall?)")
    js = m.group(1)
    data = js.split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        return []
    payload = json.loads(data)
    return parse_payload(payload)


def parse_payload(payload):
    """Converts the GetShoppingResults response payload into a flight list.
    Both ds:1 (HTML) and RPC (wrb.fr) carry the same proto, so both go
    through this function: rows payload[3][0], airlines payload[7][1][1]."""
    try:
        am = payload[7][1][1]
        airlines_map = dict(am) if am else {}
    except (TypeError, IndexError, KeyError):
        # this block comes back as None for same-day/flight-less queries
        airlines_map = {}
    out = []
    try:
        rows = payload[3][0]
    except (TypeError, IndexError, KeyError):
        rows = None
    if rows is None:
        return out
    for k in rows:
        flight = k[0]
        price = _at(_at(k, 1), 0)
        price = _at(price, 1)
        if not isinstance(price, (int, float)):
            continue
        codes = flight[1] or []
        segs = _parse_segments(flight, airlines_map)
        layovers = _parse_layovers(flight, segs)
        first = segs[0] if segs else {}
        last = segs[-1] if segs else {}
        duration = _at(flight, 9)
        if duration is None and segs:
            duration = sum(s.get("duration") or 0 for s in segs) or None
        out.append({
            "price": price,
            "airlines": [airlines_map.get(c, c) for c in codes],
            "type": flight[0],
            "segments": segs,
            "dep_date": _ymd(_at(flight, 4)) or first.get("dep_date"),
            "dep_time": _hhmm(_at(flight, 5)) or first.get("dep_time"),
            "arr_date": _ymd(_at(flight, 7)) or last.get("arr_date"),
            "arr_time": _hhmm(_at(flight, 8)) or last.get("arr_time"),
            "duration": duration,
            "stops_n": max(0, len(segs) - 1),
            "layovers": layovers,
        })
    return out


def _layover_text(layovers):
    """Machine-readable text like 'BAH:345; DOH:120' (the email side formats it)."""
    parts = []
    for l in layovers or []:
        code = l.get("code") or "?"
        mins = l.get("minutes")
        if isinstance(mins, (int, float)) and mins > 0:
            parts.append("%s:%d" % (code, int(mins)))
        else:
            parts.append(code)
    return "; ".join(parts)


def summarize(itins):
    prices = [i["price"] for i in itins
              if isinstance(i.get("price"), (int, float))]
    empty = {"cheapest": None, "median": None, "offers": 0,
             "carriers": None, "stops": None, "best_airline": None,
             "best_dep": None, "best_arr": None, "best_duration": None,
             "best_stops": None, "best_segments": None, "best_layover": None,
             "best_flight_no": None, "best_aircraft": None}
    if not prices:
        return empty
    best = min(itins, key=lambda i: i["price"])
    segs = best.get("segments") or []
    stops = max(0, len(segs) - 1)
    carriers = sorted({a for i in itins for a in (i.get("airlines") or [])})
    airlines = [a for a in (best.get("airlines") or []) if a]
    if not airlines:
        airlines = [s["airline"] for s in segs if s.get("airline")]

    def _uniq(vals):
        seen, out = set(), []
        for v in vals:
            if v and v not in seen:
                seen.add(v)
                out.append(v)
        return out

    flight_nos = _uniq([s.get("flight_no") for s in segs])
    aircraft = _uniq([s.get("aircraft") for s in segs])
    return {
        "cheapest": min(prices),
        "median": round(statistics.median(prices), 2),
        "offers": len(prices),
        "carriers": "+".join(carriers)[:120] or None,
        "stops": str(stops),
        "best_airline": "+".join(airlines)[:120] or None,
        "best_dep": _dt(best.get("dep_date"), best.get("dep_time")),
        "best_arr": _dt(best.get("arr_date"), best.get("arr_time")),
        "best_duration": best.get("duration"),
        "best_stops": stops,
        "best_segments": len(segs),
        "best_layover": _layover_text(best.get("layovers")) or None,
        "best_flight_no": ", ".join(flight_nos)[:120] or None,
        "best_aircraft": ", ".join(aircraft)[:120] or None,
    }


# ---------- collector interface ----------
class GoogleFlightsCollector(object):
    name = "google_flights"

    def __init__(self, cfg, env):
        self.currency = cfg.get("currency", "EUR")
        cc = cfg.get("collectors", {}).get("google_flights", {})
        self.max_stops = cc.get("max_stops")

    def fetch(self, route):
        dep = route["departure"]
        ret = route.get("return")
        o, d = route["origin"], route["destination"]
        # Mode: 0 = nonstop only, null = connections allowed, 1 = at most 1
        # connection. Per-route override; falls back to the global value.
        max_stops = route.get("max_stops", self.max_stops)
        html_text = fetch(dep, o, d, ret=ret, currency=self.currency,
                          max_stops=max_stops)
        itins = parse_flights(html_text)
        if max_stops is not None:
            # Defensive filter in case anything leaks past the Google filter
            itins = [i for i in itins
                     if (i.get("stops_n") or 0) <= max_stops]
        itins = [i for i in itins
                 if isinstance(i.get("price"), (int, float))]
        s = summarize(itins)
        scan = {
            "ts": None, "route": route["id"], "collector": self.name,
            "ok": 1, "currency": self.currency,
            "cheapest": s["cheapest"], "median": s["median"],
            "offers": s["offers"], "carriers": s["carriers"],
            "stops": s["stops"],
            # for route_latest / detailed email:
            "airline": s["best_airline"],
            "dep": s["best_dep"],
            "arr": s["best_arr"],
            "duration": s["best_duration"],
            "stops_n": s["best_stops"],
            "segments": s["best_segments"],
            "layover": s["best_layover"],
            "flight_no": s["best_flight_no"],
            "aircraft": s["best_aircraft"],
        }
        return scan
