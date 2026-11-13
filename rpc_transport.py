# -*- coding: utf-8 -*-
"""GetShoppingResults RPC transport — pure RPC instead of the HTML page.

Live-verified on 2026-11-10: data comes back without the
x-goog-batchexecute-bgr signature. Per date, ~40 KB of JSON instead of
~2.3 MB of HTML (~50x less).

Format (decoded from the fli source):
  POST /_/FlightsFrontendUi/data/travel.frontend.flights.FlightsFrontendService/
       GetShoppingResults?hl=en&curr=EUR
  body: f.req=[null,"<JSON-proto>"]
  JSON-proto (positional): [[], main, sort, all_results, 0, 1]
  main[13]=segments; segment[3]=stops enum (0=any,1=nonstop,2=max1),
  segment[6]=date, segment[14]=classifier (3=outbound).

Response: the first wrb.fr chunk's 3rd element of the )]}\' envelope is a
JSON string; its payload is identical to the ds:1 proto (rows [3][0],
airlines [7][1][1]) -> collectors.google_flights.parse_payload handles it.

Privacy: no cookies; every request is a fresh single stdlib HTTP call.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, PARENT)

from collectors.google_flights import parse_payload   # noqa: E402

RPC_URL = ("https://www.google.com/_/FlightsFrontendUi/data/"
           "travel.frontend.flights.FlightsFrontendService/"
           "GetShoppingResults")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")

# pbj stops enum: 0=ANY, 1=NON_STOP, 2=ONE_OR_FEWER
STOPS_ENUM = {"any": 0, "0": 1, "1": 2}


def build_proto(origin, dest, date, mode):
    """One-way query proto (positional pbj): [[], main, sort, 1, 0, 1]."""
    main = build_main_fields(origin, dest, date, mode)
    return [[], main, 2, 1, 0, 1]   # sort=2 CHEAPEST, all_results=1


def fetch_rpc(origin, dest, date, currency="EUR", mode="0"):
    """Fires the RPC, returns the raw text. Error -> RuntimeError."""
    proto = build_proto(origin, dest, date, mode)
    freq = json.dumps([None, json.dumps(proto, separators=(",", ":"))],
                      separators=(",", ":"))
    url = RPC_URL + "?" + urllib.parse.urlencode(
        {"hl": "en", "curr": currency})
    body = ("f.req=" + urllib.parse.quote(freq)).encode()
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Origin": "https://www.google.com",
        "Referer": "https://www.google.com/travel/flights",
    })
    with urllib.request.urlopen(req, timeout=45) as resp:
        txt = resp.read().decode("utf-8", errors="replace")
    if len(txt) < 20:
        raise RuntimeError("RPC response too short (%d bytes)" % len(txt))
    return txt


def parse_rpc_response(txt):
    """Extracts the payload from the wrb.fr chunk -> flight list (ds:1)."""
    m = re.search(r'\["wrb\.fr",null,"((?:[^"\\]|\\.)*)"', txt)
    if not m:
        # error envelope: [["er",...]] or empty
        if '"er"' in txt[:80] or "]}'" not in txt[:5]:
            raise RuntimeError("RPC error envelope: %s" % txt[:120])
        raise RuntimeError("wrb.fr not found")
    try:
        inner = json.loads('"%s"' % m.group(1))
    except ValueError:
        raise RuntimeError("wrb.fr inner JSON string is malformed")
    if not inner:
        raise RuntimeError("RPC payload empty (error row?)")
    try:
        payload = json.loads(inner)
    except ValueError:
        raise RuntimeError("RPC inner JSON is malformed: %s" % inner[:100])
    # error row check: ["wrb.fr", null, null, null, null, [code]]
    m2 = re.search(r'\["wrb\.fr",null,null,[^\]]*\[(-?\d+)\]', txt)
    if m2 and int(m2.group(1)) > 0:
        raise RuntimeError("RPC rejection, gRPC code %s" % m2.group(1))
    return parse_payload(payload)


def fetch_itins(origin, dest, date, currency="EUR", mode="0"):
    """RPC -> flight list. (scan_one uses this directly.)"""
    txt = fetch_rpc(origin, dest, date, currency=currency, mode=mode)
    return parse_rpc_response(txt)


# ---------------- GetCalendarGraph (1 request = whole range) ----------------
# Live-verified on 2026-11-10: 142 days in a SINGLE request (0.2 s, ~21 KB).
# fli's "bgr required/closed since 2026-08" measurement does not apply to
# this transport. The request layout comes from fli DateSearchFilters.format():
#   inner = [null, main, [from_date, to_date]]
# Response row: ["YYYY-MM-DD", null, [[null, price], token], 1]

CAL_URL = ("https://www.google.com/_/FlightsFrontendUi/data/"
           "travel.frontend.flights.FlightsFrontendService/"
           "GetCalendarGraph")


def build_calendar_proto(origin, dest, from_d, to_d, mode="0"):
    """Calendar graph request proto: [null, main, [from, to]]."""
    main = build_main_fields(origin, dest, from_d, mode)
    return [None, main, [from_d, to_d]]


def build_main_fields(origin, dest, date, mode):
    """main (positional pbj, 18 elements) — shared by ShoppingResults
    and CalendarGraph."""
    seg = [
        [[[origin, 0]]],
        [[[dest, 0]]],
        None,
        STOPS_ENUM.get(str(mode), 0),
        None, None,
        "%s" % date,
        None, None, None, None, None, None, None,
        3,                      # classifier outbound
    ]
    return [None, None, 2, None, [], 1, [1, 0, 0, 0], None, None, None,
            None, None, None, [seg], None, None, None, 1]


def fetch_calendar(origin, dest, from_d, to_d, currency="EUR", mode="0"):
    """Fires the calendar graph RPC, returns the raw text."""
    proto = build_calendar_proto(origin, dest, from_d, to_d, mode)
    freq = json.dumps([None, json.dumps(proto, separators=(",", ":"))],
                      separators=(",", ":"))
    url = CAL_URL + "?" + urllib.parse.urlencode(
        {"hl": "en", "curr": currency})
    body = ("f.req=" + urllib.parse.quote(freq)).encode()
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Origin": "https://www.google.com",
        "Referer": "https://www.google.com/travel/flights",
    })
    with urllib.request.urlopen(req, timeout=45) as resp:
        txt = resp.read().decode("utf-8", errors="replace")
    if len(txt) < 20:
        raise RuntimeError("calendar response too short (%d bytes)" % len(txt))
    return txt


def parse_calendar_response(txt):
    """Decodes the calendar response into [(date, price|None), ...].

    2026-11-11: Google changed the envelope/layout. Errors now arrive like:
      ["wrb.fr", null, null, null, null,
       [<code>, null, [["type.googleapis.com/.../ErrorResponse", ...]]]]
    The old regex caught a number from the trace id instead of the error
    code. The envelope is now decoded directly as JSON."""
    body = txt.lstrip()
    if body.startswith(")]}'"):
        body = body[4:].lstrip()
    try:
        envelope = json.loads(body)
    except ValueError:
        raise RuntimeError("calendar response is not JSON: %s" % txt[:120])
    raw = None
    for item in (envelope if isinstance(envelope, list) else [envelope]):
        if not (isinstance(item, list) and item and item[0] == "wrb.fr"):
            continue
        err = item[5] if len(item) > 5 else None
        if (item[2] is None and isinstance(err, list) and err
                and isinstance(err[0], int) and err[0] > 0):
            raise RuntimeError("calendar RPC rejection (gRPC code %d)" % err[0])
        if isinstance(item[2], str):
            raw = item[2]
            break
    if raw is None:
        raise RuntimeError("no data in calendar response: %s" % txt[:120])
    payload = json.loads(raw)
    out = []
    date_re = re.compile(r"^\d{4}-\d\d-\d\d$")

    def walk(node):
        if isinstance(node, list):
            if node and isinstance(node[0], str) and \
                    date_re.match(node[0]):
                price = None
                try:
                    price = float(node[2][0][1])
                except (IndexError, TypeError, ValueError):
                    pass
                out.append((node[0], price))
                return
            for ch in node:
                walk(ch)

    walk(payload)
    if not out:
        raise RuntimeError("no date rows in the calendar response")
    return out


def fetch_calendar_prices(origin, dest, from_d, to_d,
                          currency="EUR", mode="0"):
    """Calendar graph -> {date: price|None} dict."""
    txt = fetch_calendar(origin, dest, from_d, to_d,
                         currency=currency, mode=mode)
    return dict(parse_calendar_response(txt))
