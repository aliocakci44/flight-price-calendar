# -*- coding: utf-8 -*-
"""Route-selectable flight price calendar: monthly view + per-day tickets.

Features:
  - Empty landing page; airport AND city suggestions for From/To
    (/api/suggest, OurAirports-derived data -> web/places.json).
  - When a city is selected, all of its airports are scanned together
    (e.g. Istanbul = IST + SAW); results are merged per day by cheapest price.
  - "Nonstop only" checkbox.
  - Currency selector (EUR/USD/TRY/...); the cache is split by currency.
  - Authentication: multi-user session system (registration + invite codes,
    scrypt hashes, HttpOnly cookie, data/users.db) with optional Basic Auth
    (FLEX_AUTH) as a fallback. If no user is registered and FLEX_AUTH is
    empty, the site runs open (local development mode).
  - Monthly calendar in 3-column grid; clicking a day shows that day's
    cheapest ticket(s) with a Google Flights booking link.

Architecture: stdlib-only ThreadingHTTPServer + SQLite cache.
  GET /                         -> web/index.html
  GET /day?...                  -> web/day.html
  GET /api/suggest?q=&limit=    -> airport/city suggestions
  GET /api/month?from=&to=&direct=&month=&cur=
  GET /api/day?from=&to=&direct=&date=&price=&cur=

from/to accept comma-separated 3-letter IATA lists (e.g. IST,SAW).
Privacy: the cache holds only route/date/price/currency; every request to
Google goes out as a fresh single stdlib HTTP request (no cookies).
"""
import argparse
import base64
import hashlib
import hmac
import http.cookies
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, PARENT)
sys.path.insert(0, HERE)

import rpc_transport                                          # noqa: E402
from collectors.google_flights import encode_tfs              # noqa: E402

WEB_DIR = os.path.join(HERE, "web")
PLACES_PATH = os.path.join(WEB_DIR, "places.json")
DB_PATH = os.path.join(HERE, "data", "webcache.db")
HORIZON_DAYS = 330          # Google's booking horizon (~11 months)
MONTH_TTL = 2 * 3600
DAY_TTL = 20 * 60
FMT = "%Y-%m-%d %H:%M:%S"
MAX_AIRPORTS = 6            # per-side cap (to avoid request storms)

CURRENCIES = {"EUR", "USD", "GBP", "TRY", "CHF", "PLN", "CZK", "HUF",
              "RON", "BGN", "SEK", "NOK", "DKK", "CAD", "AUD", "JPY",
              "AED"}

# Basic Auth (optional): FLEX_AUTH="user:password[,user2:password2]"
AUTH = {}


def parse_auth(raw):
    """Reads a user/password list from an environment variable. Passwords
    must not contain commas; they may contain colons (first colon splits)."""
    users = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        user, pwd = part.split(":", 1)
        if user and pwd:
            users[user] = pwd
    return users


# ---------------- users / sessions / budget ----------------
USERS_DB = os.path.join(HERE, "data", "users.db")
SESSION_COOKIE = "flex_session"
SESSION_TTL = 30 * 24 * 3600     # 30 days
TS_SITE = os.environ.get("FLEX_TS_SITE", "")    # Cloudflare Turnstile
TS_SECRET = os.environ.get("FLEX_TS_SECRET", "")
BUDGET = int(os.environ.get("FLEX_BUDGET", "300") or 0)   # daily RPCs, 0=unlimited
IP_LIMIT = int(os.environ.get("FLEX_IP_LIMIT", "20") or 0)  # per /api minute, 0=off

USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY, pwhash TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
  code TEXT PRIMARY KEY, created TEXT NOT NULL, used_by TEXT
);
CREATE TABLE IF NOT EXISTS usage (
  day TEXT PRIMARY KEY, n INTEGER NOT NULL
);
"""


def init_users_db(path=USERS_DB):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(USERS_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def hash_password(pw):
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode("utf-8"), salt=salt,
                       n=16384, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$%s$%s" % (salt.hex(), h.hex())


def check_password(pw, stored):
    try:
        algo, n, r, p, salt, h = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(pw.encode("utf-8"), salt=bytes.fromhex(salt),
                              n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except (ValueError, TypeError):
        return False


def get_user(username):
    conn = sqlite3.connect(USERS_DB, timeout=10)
    try:
        row = conn.execute("SELECT username, pwhash FROM users "
                           "WHERE username=?", (username,)).fetchone()
    finally:
        conn.close()
    return row


def add_user(username, password):
    conn = sqlite3.connect(USERS_DB, timeout=10)
    try:
        conn.execute("INSERT INTO users (username, pwhash, created) "
                     "VALUES (?,?,?)",
                     (username, hash_password(password),
                      datetime.utcnow().strftime(FMT)))
        conn.commit()
    finally:
        conn.close()


def user_count(path=USERS_DB):
    conn = sqlite3.connect(path, timeout=10)
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()


def create_invite():
    code = secrets.token_urlsafe(9)
    conn = sqlite3.connect(USERS_DB, timeout=10)
    try:
        conn.execute("INSERT INTO invites (code, created) VALUES (?,?)",
                     (code, datetime.utcnow().strftime(FMT)))
        conn.commit()
    finally:
        conn.close()
    return code


def consume_invite(code):
    """Consumes the code once; returns True if it was valid."""
    conn = sqlite3.connect(USERS_DB, timeout=10)
    try:
        row = conn.execute("SELECT code FROM invites "
                           "WHERE code=? AND used_by IS NULL",
                           (code,)).fetchone()
        if not row:
            return False
        conn.execute("UPDATE invites SET used_by=? WHERE code=?",
                     ("__pending__", code))
        conn.commit()
        return True
    finally:
        conn.close()


def create_session(username):
    token = secrets.token_urlsafe(32)
    th = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = sqlite3.connect(USERS_DB, timeout=10)
    try:
        conn.execute("DELETE FROM sessions WHERE expires < ?",
                     (time.time(),))
        conn.execute("INSERT INTO sessions (token_hash, username, expires) "
                     "VALUES (?,?,?)", (th, username, time.time() + SESSION_TTL))
        conn.commit()
    finally:
        conn.close()
    return token


def session_user(token):
    if not token:
        return None
    th = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = sqlite3.connect(USERS_DB, timeout=10)
    try:
        row = conn.execute("SELECT username, expires FROM sessions "
                           "WHERE token_hash=?", (th,)).fetchone()
        if not row or row[1] < time.time():
            if row:
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (th,))
                conn.commit()
            return None
        return row[0]
    finally:
        conn.close()


def drop_session(token):
    if not token:
        return
    th = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = sqlite3.connect(USERS_DB, timeout=10)
    try:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (th,))
        conn.commit()
    finally:
        conn.close()


# Auth state (refreshed every 30 s): with no users and empty FLEX_AUTH the
# site is open (local development behaviour is preserved).
AUTH_STATE = {"users": None, "t": 0.0}


def auth_required():
    if AUTH:
        return True
    now = time.time()
    if AUTH_STATE["users"] is None or now - AUTH_STATE["t"] > 30:
        try:
            AUTH_STATE["users"] = user_count() > 0
        except sqlite3.Error:
            AUTH_STATE["users"] = True     # lock up if the DB is corrupt
        AUTH_STATE["t"] = now
    return AUTH_STATE["users"]


# Login attempt limit: 5 failed attempts per IP within 15 minutes
_LOGIN_FAILS = {}
_LOGIN_GUARD = threading.Lock()


def login_allowed(ip):
    now = time.time()
    with _LOGIN_GUARD:
        lst = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < 900]
        _LOGIN_FAILS[ip] = lst
        return len(lst) < 5


def login_fail(ip):
    with _LOGIN_GUARD:
        _LOGIN_FAILS.setdefault(ip, []).append(time.time())


# Cloudflare Turnstile server-side verification
def turnstile_ok(token, remoteip):
    if not TS_SECRET:
        return True     # no verification when the widget is not configured
    if not token:
        return False
    try:
        data = urllib.parse.urlencode({
            "secret": TS_SECRET, "response": token,
            "remoteip": remoteip or ""}).encode("utf-8")
        req = urllib.request.Request(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        return bool(out.get("success"))
    except Exception:
        return False


# Google request budget + circuit breaker
class BudgetExceeded(Exception):
    """Budget exhausted or circuit breaker open; stale cache fallback active."""


_CB_LOCK = threading.Lock()
_CB_UNTIL = [0.0]      # timestamp until which the circuit breaker stays open
_CB_FAILS = [0]


def google_gate():
    """Called before each RPC: budget/circuit check. Raises when blocked."""
    with _CB_LOCK:
        until = _CB_UNTIL[0]
    if time.time() < until:
        raise BudgetExceeded(
            "google circuit breaker open; retry in %d s"
            % int(until - time.time() + 1))
    if BUDGET:
        day = date.today().isoformat()
        conn = sqlite3.connect(USERS_DB, timeout=10)
        try:
            row = conn.execute("SELECT n FROM usage WHERE day=?",
                               (day,)).fetchone()
            n = row[0] if row else 0
        finally:
            conn.close()
        if n >= BUDGET:
            raise BudgetExceeded(
                "daily request budget exhausted (%d/%d); "
                "cache-only until tomorrow" % (n, BUDGET))


def google_charge():
    """Increments the daily counter per Google RPC attempt."""
    if not BUDGET:
        return
    day = date.today().isoformat()
    conn = sqlite3.connect(USERS_DB, timeout=10)
    try:
        conn.execute("INSERT INTO usage (day, n) VALUES (?,1) "
                     "ON CONFLICT(day) DO UPDATE SET n = n + 1", (day,))
        conn.commit()
    finally:
        conn.close()


def google_failure(exc):
    """Opens the circuit breaker on Google anger signals such as 429/403."""
    hard = isinstance(exc, urllib.error.HTTPError) or \
        " kod 8" in str(exc) or " kod 16" in str(exc) or \
        "429" in str(exc) or "403" in str(exc)
    if not hard:
        return
    with _CB_LOCK:
        _CB_FAILS[0] += 1
        cooldown = min(600 * (2 ** (_CB_FAILS[0] - 1)), 3600)
        _CB_UNTIL[0] = time.time() + cooldown
        sys.stderr.write("[web] circuit breaker: %d s (%s)\n"
                         % (cooldown, exc))


def google_success():
    with _CB_LOCK:
        _CB_FAILS[0] = 0
        _CB_UNTIL[0] = 0.0


# Per-IP per-minute request limit for /api/month + /api/day
_IP_REQS = {}
_IP_GUARD = threading.Lock()


def ip_allowed(ip):
    if not IP_LIMIT:
        return True
    now = time.time()
    with _IP_GUARD:
        lst = [t for t in _IP_REQS.get(ip, []) if now - t < 60]
        if len(lst) >= IP_LIMIT:
            _IP_REQS[ip] = lst
            return False
        lst.append(now)
        _IP_REQS[ip] = lst
        if len(_IP_REQS) > 4096:
            for k in [k for k, v in _IP_REQS.items() if not v]:
                _IP_REQS.pop(k, None)
        return True

# Popular points shown on an empty search
POPULAR = ["IST", "SAW", "STR", "FRA", "MUC", "BER", "DUS", "HAM",
           "CGN", "VIE", "ZRH", "AMS", "LHR", "CDG", "AYT", "ESB",
           "ADB", "JFK", "DXB"]

# At most 2 concurrent requests to Google
_GOOGLE_SEM = threading.Semaphore(2)
_KEY_LOCKS = {}
_KEY_GUARD = threading.Lock()

# Suggestion data (filled in by main()/ensure_places())
PLACES = []
PLACE_BY_CODE = {}
METRO_GROUPS = {}           # (country, metro name) -> [codes]
CODE_METRO = {}             # airport code -> (country, metro name)
_PLACES_LOCK = threading.Lock()

FALLBACK_PLACES = [
    ("IST", "İstanbul Airport", "İstanbul", "TR", "large_airport", "İstanbul"),
    ("SAW", "Istanbul Sabiha Gökçen International Airport", "İstanbul",
     "TR", "large_airport", "İstanbul"),
    ("STR", "Stuttgart Airport", "Stuttgart", "DE", "large_airport", None),
    ("FRA", "Frankfurt Main Airport", "Frankfurt am Main", "DE",
     "large_airport", None),
    ("MUC", "Munich Airport", "Munich", "DE", "large_airport", None),
    ("BER", "Berlin Brandenburg Airport", "Berlin", "DE",
     "large_airport", None),
    ("VIE", "Vienna International Airport", "Vienna", "AT",
     "large_airport", None),
    ("ZRH", "Zurich Airport", "Zurich", "CH", "large_airport", None),
    ("AMS", "Amsterdam Airport Schiphol", "Amsterdam", "NL",
     "large_airport", None),
    ("LHR", "London Heathrow Airport", "London", "GB", "large_airport",
     "London"),
    ("CDG", "Charles de Gaulle International Airport", "Paris", "FR",
     "large_airport", "Paris"),
    ("ESB", "Esenboğa International Airport", "Ankara", "TR",
     "large_airport", None),
    ("ADB", "Adnan Menderes International Airport", "İzmir", "TR",
     "large_airport", None),
    ("AYT", "Antalya International Airport", "Antalya", "TR",
     "large_airport", None),
]


class ApiError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status


# ---------------- cache ----------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS month_cache (
  route TEXT NOT NULL, mode TEXT NOT NULL, currency TEXT NOT NULL,
  month TEXT NOT NULL, payload TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY (route, mode, currency, month)
);
CREATE TABLE IF NOT EXISTS day_cache (
  route TEXT NOT NULL, mode TEXT NOT NULL, currency TEXT NOT NULL,
  d TEXT NOT NULL, payload TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY (route, mode, currency, d)
);
"""


def init_db(path=DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        # one-time cleanup of legacy cache tables without a currency column
        for table in ("month_cache", "day_cache"):
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(%s)" % table)]
            if cols and "currency" not in cols:
                conn.execute("DROP TABLE %s" % table)
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def cache_get(table, keycol, route, mode, currency, key, path=DB_PATH):
    """(payload|None, age_seconds|None)"""
    conn = sqlite3.connect(path, timeout=10)
    try:
        row = conn.execute(
            "SELECT payload, fetched_at FROM %s WHERE route=? AND mode=? "
            "AND currency=? AND %s=?" % (table, keycol),
            (route, mode, currency, key)).fetchone()
    finally:
        conn.close()
    if not row:
        return None, None
    try:
        age = (datetime.utcnow()
               - datetime.strptime(row[1], FMT)).total_seconds()
    except ValueError:
        return None, None
    try:
        return json.loads(row[0]), max(0, int(age))
    except ValueError:
        return None, None


def cache_put(table, keycol, route, mode, currency, key, payload,
              path=DB_PATH):
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.execute(
            "INSERT INTO %s (route, mode, currency, %s, payload, fetched_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(route, mode, currency, %s) "
            "DO UPDATE SET payload=excluded.payload, "
            "fetched_at=excluded.fetched_at" % (table, keycol, keycol),
            (route, mode, currency, key,
             json.dumps(payload, ensure_ascii=False),
             datetime.utcnow().strftime(FMT)))
        conn.commit()
    finally:
        conn.close()


def key_lock(key):
    with _KEY_GUARD:
        lock = _KEY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _KEY_LOCKS[key] = lock
        return lock


# ---------------- suggestion data ----------------
def ensure_places():
    global PLACES, PLACE_BY_CODE, METRO_GROUPS, CODE_METRO
    if PLACES:
        return
    with _PLACES_LOCK:
        if PLACES:
            return
        data = []
        try:
            with open(PLACES_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = [{"code": c, "name": n, "city": city, "cc": cc,
                     "type": typ, "metro": metro}
                    for c, n, city, cc, typ, metro in FALLBACK_PLACES]
        PLACES = data
        PLACE_BY_CODE = {p["code"]: p for p in data if p.get("code")}
        groups = {}
        code_metro = {}
        for p in data:
            metro = p.get("metro")
            if metro:
                key = (p["cc"], metro)
                groups.setdefault(key, []).append(p["code"])
                code_metro[p["code"]] = key
        METRO_GROUPS = groups
        CODE_METRO = code_metro


_TR_MAP = {
    "ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
    "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c",
    "â": "a", "Â": "a", "î": "i", "Î": "i", "û": "u", "Û": "u",
}


def norm(s):
    s = (s or "").translate(str.maketrans(_TR_MAP)).lower()
    s = s.replace("\u0307", "")
    return re.sub(r"\s+", " ", s).strip()


def _city_item(label, cc, codes):
    codes = sorted(codes)
    return {"kind": "city", "label": label, "cc": cc,
            "codes": ", ".join(codes), "airports": codes,
            "airport": None,
            "sub": "All airports: " + ", ".join(codes)}


def _airport_item(p):
    # For metro airports (e.g. SAW) the municipality may be
    # "Pendik, Istanbul"; show the city/metro name to the user instead.
    return {"kind": "airport", "label": p.get("metro") or p.get("city")
            or p["code"],
            "code": p["code"], "airport": p["name"], "cc": p["cc"],
            "codes": p["code"], "airports": [p["code"]],
            "sub": "%s · %s" % (p["name"], p["cc"])}


def suggest_items(q, limit=10):
    ensure_places()
    qn = norm(q)
    if not qn:
        scored = []
        seen = set()
        for code in POPULAR:
            p = PLACE_BY_CODE.get(code)
            if not p:
                continue
            metro = p.get("metro")
            if metro:
                key = (p["cc"], metro)
                if key in seen:
                    continue
                seen.add(key)
                scored.append((0, _city_item(metro, p["cc"],
                                             METRO_GROUPS.get(key, [code]))))
            else:
                scored.append((0, _airport_item(p)))
        return [item for _, item in scored[:limit]]

    scored = []

    for (cc, metro), codes in METRO_GROUPS.items():
        nl = norm(metro)
        ncodes = [c.lower() for c in codes]
        score = None
        if nl == qn:
            score = 0
        elif qn in ncodes:
            score = 0.5
        elif nl.startswith(qn):
            score = 1
        elif any(c.startswith(qn) for c in ncodes):
            score = 1.5
        elif qn in nl:
            score = 2
        else:
            for c in codes:
                p = PLACE_BY_CODE.get(c)
                if p and any(w.startswith(qn)
                             for w in norm(p["name"]).split()):
                    score = 3
                    break
        if score is not None:
            scored.append((score, _city_item(metro, cc, codes)))

    for p in PLACES:
        code = (p.get("code") or "").lower()
        city = norm(p.get("city"))
        name = norm(p.get("name"))
        score = None
        if code == qn:
            score = 0
        elif code.startswith(qn):
            score = 1.5
        elif city == qn:
            score = 2
        elif city.startswith(qn):
            score = 2.4
        elif name.startswith(qn):
            score = 2.6
        elif any(w.startswith(qn) for w in city.split()):
            score = 2.9
        elif len(qn) >= 4 and qn in city:
            score = 3.2
        elif any(w.startswith(qn) for w in name.split()):
            score = 3.4
        elif len(qn) >= 4 and qn in name:
            score = 3.6
        elif qn == (p.get("cc") or "").lower():
            score = 4
        if score is not None:
            scored.append((score, _airport_item(p)))

    def _key(pair):
        score, item = pair
        if item["kind"] == "city":
            return (score, 0, -len(item["airports"]),
                    norm(item["label"]))
        p = PLACE_BY_CODE.get(item["code"], {})
        rank = 0 if p.get("type") == "large_airport" else 1
        return (score, 1, rank, norm(item["label"]), item["code"])

    scored.sort(key=_key)
    return [item for _, item in scored[:limit]]


# ---------------- helpers ----------------
def one(qs, name, default=None):
    vals = qs.get(name) or []
    return vals[0].strip() if vals and vals[0] is not None else default


def get_airports(qs, name, field):
    raw = (one(qs, name) or "").upper()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ApiError(400, "select an airport or city for %s" % field)
    if len(parts) > MAX_AIRPORTS:
        raise ApiError(400, "at most %d airports for %s"
                            % (field, MAX_AIRPORTS))
    out = []
    for code in parts:
        if not re.match(r"^[A-Z]{3}$", code):
            raise ApiError(400, "%s: invalid airport code (%s)"
                                % (field, code))
        if code not in out:
            out.append(code)
    return out


def get_mode(qs):
    return "0" if (one(qs, "direct") or "").lower() in ("1", "true", "on") \
        else "any"


def near_flag(qs, name):
    return (one(qs, name) or "").lower() in ("1", "true", "on", "yes")


def nearby_siblings(code):
    """Skyscanner logic: airport -> the other airports in the same
    city/metro set. No radius involved; the source is the metro groups
    in web/places.json."""
    ensure_places()
    key = CODE_METRO.get(code)
    if not key:
        return []
    return [c for c in sorted(METRO_GROUPS.get(key, [])) if c != code]


def expand_nearby(codes, enabled):
    if not enabled:
        return list(codes)
    out = list(codes)
    for code in codes:
        for sib in nearby_siblings(code):
            if sib not in out:
                out.append(sib)
    # The codes the user typed come first; if the cap is exceeded, group
    # siblings fall off the end (the request count stays capped by
    # MAX_AIRPORTS).
    return out[:MAX_AIRPORTS]


def get_currency(qs):
    cur = (one(qs, "cur") or one(qs, "currency") or "EUR").upper()
    if cur not in CURRENCIES:
        raise ApiError(400, "unsupported currency: %s" % cur)
    return cur


def parse_month(val):
    m = re.match(r"^(\d{4})-(\d{2})$", val or "")
    if not m:
        raise ApiError(400, "month must be in YYYY-MM format")
    y, mo = int(m.group(1)), int(m.group(2))
    if not 1 <= mo <= 12:
        raise ApiError(400, "invalid month")
    return y, mo


def month_bounds(y, mo):
    first = date(y, mo, 1)
    nxt = date(y + 1, 1, 1) if mo == 12 else date(y, mo + 1, 1)
    return first, nxt - timedelta(days=1)


def allowed_range():
    today = date.today()
    return today.replace(day=1), \
        (today + timedelta(days=HORIZON_DAYS)).replace(day=1)


def check_month(month):
    y, mo = parse_month(month)
    lo_month, hi_month = allowed_range()
    if date(y, mo, 1) < lo_month or date(y, mo, 1) > hi_month:
        raise ApiError(400, "month outside the allowed range")
    return y, mo


def parse_date(val):
    if not re.match(r"^\d{4}-\d\d-\d\d$", val or ""):
        raise ApiError(400, "date must be in YYYY-MM-DD format")
    try:
        d = date(int(val[:4]), int(val[5:7]), int(val[8:10]))
    except ValueError:
        raise ApiError(400, "invalid date")
    today = date.today()
    if d < today:
        raise ApiError(400, "date in the past")
    if d > today + timedelta(days=HORIZON_DAYS):
        raise ApiError(400, "date beyond the booking horizon")
    return d


def search_url(origin, dest, day, mode, currency):
    """Search (booking) link on Google Flights for that airport/date."""
    tfs = encode_tfs(day, origin, dest,
                     max_stops=0 if mode == "0" else None)
    return ("https://www.google.com/travel/flights/search?" +
            urllib.parse.urlencode({"tfs": tfs, "hl": "en",
                                    "curr": currency}))


def _retry(fn, attempts=3, pause=1.5):
    """Retries on transient errors. Budget exhaustion and Google 4xx
    (429/403) are propagated without retrying; repeating a 429 only
    makes the blocks grow."""
    last = None
    for i in range(attempts):
        if i:
            time.sleep(pause * i)
        try:
            return fn()
        except BudgetExceeded:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                google_failure(exc)
                raise
            last = exc
        except Exception as exc:
            last = exc
    if last is not None:
        google_failure(last)
    raise last


# ---------------- month data ----------------
def fetch_month(origin, dest, month, mode, currency):
    """Cheapest daily prices for one month. Returns {"YYYY-MM-DD": price}."""
    y, mo = check_month(month)
    first, last = month_bounds(y, mo)
    today = date.today()
    horizon = today + timedelta(days=HORIZON_DAYS)
    start, end = max(first, today), min(last, horizon)
    if start > end:
        return {}

    def _do():
        google_gate()
        google_charge()
        with _GOOGLE_SEM:
            try:
                res = rpc_transport.fetch_calendar_prices(
                    origin, dest, start.isoformat(), end.isoformat(),
                    currency=currency, mode=mode)
            except Exception as exc:
                google_failure(exc)
                raise
            google_success()
            return res

    prices = _retry(_do)
    days = {}
    s_iso, e_iso = start.isoformat(), end.isoformat()
    for d, p in prices.items():
        if isinstance(p, (int, float)) and s_iso <= d <= e_iso:
            days[d] = round(float(p), 2)
    return days


def pair_month(origin, dest, month, mode, currency):
    """Month data for a single airport pair (cached).
    Returns: (payload, age, cached, stale)."""
    route = origin + "-" + dest
    cached, age = cache_get("month_cache", "month", route, mode, currency,
                            month)
    if cached is not None and age is not None and age < MONTH_TTL:
        return cached, age, True, False

    lock = key_lock("m|%s|%s|%s|%s" % (route, mode, currency, month))
    with lock:
        cached, age = cache_get("month_cache", "month", route, mode,
                                currency, month)
        if cached is not None and age is not None and age < MONTH_TTL:
            return cached, age, True, False
        try:
            days = fetch_month(origin, dest, month, mode, currency)
        except Exception:
            if cached is not None:
                return cached, age, True, True
            raise
        payload = {"route": route, "month": month, "mode": mode,
                   "currency": currency, "days": days}
        cache_put("month_cache", "month", route, mode, currency, month,
                  payload)
        return payload, 0, False, False


def api_month(qs):
    origins = get_airports(qs, "from", "From")
    dests = get_airports(qs, "to", "To")
    near_from = near_flag(qs, "from_near")
    near_to = near_flag(qs, "to_near")
    origins = expand_nearby(origins, near_from)
    dests = expand_nearby(dests, near_to)
    overlap = [c for c in origins if c in dests]
    if overlap:
        raise ApiError(400, "The same airport cannot be on both sides: %s"
                            % ", ".join(overlap))
    month = one(qs, "month") or ""
    check_month(month)
    mode = get_mode(qs)
    currency = get_currency(qs)

    merged = {}
    ages = []
    cached_all = True
    stale_any = False
    failed = 0
    last_err = None
    pairs = [(o, d) for o in origins for d in dests]

    for origin, dest in pairs:
        try:
            payload, age, cached, stale = pair_month(
                origin, dest, month, mode, currency)
        except ApiError:
            raise
        except Exception as exc:
            failed += 1
            last_err = exc
            continue
        for d, price in (payload.get("days") or {}).items():
            if d not in merged or price < merged[d]:
                merged[d] = price
        ages.append(age)
        cached_all = cached_all and cached
        stale_any = stale_any or stale

    if failed == len(pairs):
        if isinstance(last_err, BudgetExceeded):
            raise ApiError(503, "temporarily rate-limited: %s" % last_err)
        raise ApiError(502, "could not fetch the calendar: %s" % last_err)

    out = {
        "route": " + ".join(origins) + " > " + " + ".join(dests),
        "month": month,
        "mode": mode,
        "currency": currency,
        "days": merged,
        "cached": cached_all,
        "stale": stale_any,
        "age": max(ages) if ages else 0,
        "airports": {"from": origins, "to": dests},
        "near": {"from": near_from, "to": near_to},
        "pairs": len(pairs),
        "partial": failed > 0,
    }
    if failed:
        out["note"] = "%d/%d airport pairs failed" % (failed,
                                                            len(pairs))
    out["ok"] = True
    return out


# ---------------- day / ticket data ----------------
def _ticket(it):
    segs = it.get("segments") or []
    price = it.get("price")
    if not isinstance(price, (int, float)):
        return None
    stops = max(0, len(segs) - 1)
    flight_nos = [s.get("flight_no") for s in segs if s.get("flight_no")]
    airlines = list(it.get("airlines") or [])
    if not airlines:
        airlines = [s.get("airline") for s in segs if s.get("airline")]
    via = []
    for s in segs[:-1]:
        if s.get("to") and s["to"] not in via:
            via.append(s["to"])
    return {
        "price": round(float(price), 2),
        "airlines": airlines,
        "flight_nos": flight_nos,
        "stops": stops,
        "via": via,
        "duration": it.get("duration"),
        "dep_time": it.get("dep_time"),
        "arr_time": it.get("arr_time"),
        "dep_date": it.get("dep_date"),
        "arr_date": it.get("arr_date"),
        "aircraft": ", ".join([s.get("aircraft") for s in segs
                               if s.get("aircraft")]) or None,
        "origin": (segs[0].get("from") if segs else None),
        "destination": (segs[-1].get("to") if segs else None),
    }


def fetch_day(origin, dest, day, mode, currency):
    """The cheapest ticket(s) of that day. Returns: the ticket payload."""
    def _do():
        google_gate()
        google_charge()
        with _GOOGLE_SEM:
            try:
                res = rpc_transport.fetch_itins(origin, dest, day,
                                                currency=currency, mode=mode)
            except Exception as exc:
                google_failure(exc)
                raise
            google_success()
            return res

    itins = _retry(_do)
    tickets = []
    for it in itins:
        if mode == "0" and (it.get("stops_n") or 0) > 0:
            continue
        t = _ticket(it)
        if t is None:
            continue
        t["origin"] = t["origin"] or origin
        t["destination"] = t["destination"] or dest
        tickets.append(t)
    if not tickets:
        return {"cheapest": None, "tickets": [], "offers": 0}
    cheapest = min(t["price"] for t in tickets)
    at_min = [t for t in tickets if abs(t["price"] - cheapest) < 0.01]
    at_min.sort(key=lambda t: (t.get("dep_time") or "99:99"))
    return {"cheapest": cheapest, "tickets": at_min[:12],
            "offers": len(tickets)}


def pair_day(origin, dest, day, mode, currency):
    """Day tickets for a single airport pair (cached)."""
    route = origin + "-" + dest
    cached, age = cache_get("day_cache", "d", route, mode, currency, day)
    if cached is not None and age is not None and age < DAY_TTL:
        return cached, age, True, False

    lock = key_lock("d|%s|%s|%s|%s" % (route, mode, currency, day))
    with lock:
        cached, age = cache_get("day_cache", "d", route, mode, currency, day)
        if cached is not None and age is not None and age < DAY_TTL:
            return cached, age, True, False
        try:
            payload = fetch_day(origin, dest, day, mode, currency)
        except Exception:
            if cached is not None:
                return cached, age, True, True
            raise
        payload.update({"route": route, "date": day, "mode": mode,
                        "currency": currency})
        cache_put("day_cache", "d", route, mode, currency, day, payload)
        return payload, 0, False, False


def api_day(qs):
    origins = get_airports(qs, "from", "From")
    dests = get_airports(qs, "to", "To")
    near_from = near_flag(qs, "from_near")
    near_to = near_flag(qs, "to_near")
    origins = expand_nearby(origins, near_from)
    dests = expand_nearby(dests, near_to)
    overlap = [c for c in origins if c in dests]
    if overlap:
        raise ApiError(400, "The same airport cannot be on both sides: %s"
                            % ", ".join(overlap))
    d = parse_date(one(qs, "date"))
    day = d.isoformat()
    mode = get_mode(qs)
    currency = get_currency(qs)

    try:
        expected = float(one(qs, "price"))
    except (TypeError, ValueError):
        expected = None

    pairs = [(o, dd) for o in origins for dd in dests]
    tickets = []
    ages = []
    offers = 0
    cached_all = True
    stale_any = False
    failed = 0
    last_err = None

    for origin, dest in pairs:
        try:
            payload, age, cached, stale = pair_day(
                origin, dest, day, mode, currency)
        except ApiError:
            raise
        except Exception as exc:
            failed += 1
            last_err = exc
            continue
        for t in payload.get("tickets") or []:
            t = dict(t)
            t["google_url"] = search_url(
                t.get("origin") or origin, t.get("destination") or dest,
                day, mode, currency)
            tickets.append(t)
        offers += payload.get("offers") or 0
        ages.append(age)
        cached_all = cached_all and cached
        stale_any = stale_any or stale

    if failed == len(pairs):
        if isinstance(last_err, BudgetExceeded):
            raise ApiError(503, "temporarily rate-limited: %s" % last_err)
        raise ApiError(502, "could not fetch tickets: %s" % last_err)

    if tickets:
        cheapest = min(t["price"] for t in tickets)
        at_min = [t for t in tickets if abs(t["price"] - cheapest) < 0.01]
        at_min.sort(key=lambda t: (t.get("dep_time") or "99:99",
                                   t.get("origin") or ""))
        at_min = at_min[:12]
    else:
        cheapest = None
        at_min = []

    out = {
        "route": " + ".join(origins) + " > " + " + ".join(dests),
        "date": day,
        "mode": mode,
        "currency": currency,
        "cheapest": cheapest,
        "offers": offers,
        "tickets": at_min,
        "search_url": search_url(origins[0], dests[0], day, mode,
                                 currency),
        "cached": cached_all,
        "stale": stale_any,
        "age": max(ages) if ages else 0,
        "airports": {"from": origins, "to": dests},
        "near": {"from": near_from, "to": near_to},
        "pairs": len(pairs),
        "partial": failed > 0,
        "expected": expected,
    }
    if failed:
        out["note"] = "%d/%d airport pairs failed" % (failed,
                                                            len(pairs))
    out["ok"] = True
    return out


def api_suggest(qs):
    q = one(qs, "q") or ""
    try:
        limit = int(one(qs, "limit") or 10)
    except ValueError:
        limit = 10
    limit = max(1, min(20, limit))
    return {"ok": True, "items": suggest_items(q, limit)}


# ---------------- HTTP ----------------
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/day": ("day.html", "text/html; charset=utf-8"),
    "/day.html": ("day.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/i18n.js": ("i18n.js", "application/javascript; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/day.js": ("day.js", "application/javascript; charset=utf-8"),
    "/auth.js": ("auth.js", "application/javascript; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "flexweb/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[web] " + (fmt % args) + "\n")

    # ---- helper readers ----
    def _client_ip(self):
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "?"

    def _session_token(self):
        try:
            jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return None
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _is_https(self):
        return (self.headers.get("X-Forwarded-Proto", "").lower() == "https")

    def _cookie_header(self, token, expire=False):
        if expire:
            value = "%s=; Path=/; HttpOnly; Max-Age=0; SameSite=Lax" \
                % SESSION_COOKIE
        else:
            value = "%s=%s; Path=/; HttpOnly; Max-Age=%d; SameSite=Lax" % (
                SESSION_COOKIE, token, SESSION_TTL)
            if self._is_https():
                value += "; Secure"
        return value

    def _redirect(self, location, set_cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        if set_cookie:
            self.send_header("Set-Cookie",
                             self._cookie_header(set_cookie))
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _authorized(self):
        """Session cookie or Basic Auth (FLEX_AUTH). If neither is present
        and no user is registered, the site is open (local development)."""
        if not auth_required():
            return True
        token = self._session_token()
        if token and session_user(token):
            return True
        header = self.headers.get("Authorization", "")
        if AUTH and header.startswith("Basic "):
            try:
                raw = base64.b64decode(header[6:].strip(),
                                       validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return False
            user, sep, pwd = raw.partition(":")
            if not sep:
                return False
            expected = AUTH.get(user)
            return expected is not None and hmac.compare_digest(expected,
                                                                pwd)
        return False

    def _unauthorized(self, path):
        if path.startswith("/api/"):
            self._json({"ok": False, "error": "unauthorized"}, 401)
        else:
            self._redirect("/login")

    def do_GET(self):
        try:
            self._dispatch_get()
        except ApiError as exc:
            self._json({"ok": False, "error": str(exc)}, exc.status)
        except Exception as exc:
            sys.stderr.write("[web] unexpected error: %s\n" % exc)
            self._json({"ok": False, "error": "server error"}, 500)

    def do_POST(self):
        try:
            self._dispatch_post()
        except ApiError as exc:
            self._json({"ok": False, "error": str(exc)}, exc.status)
        except Exception as exc:
            sys.stderr.write("[web] unexpected error: %s\n" % exc)
            self._json({"ok": False, "error": "server error"}, 500)

    def _dispatch_get(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path in ("/login", "/login.html"):
            self._page("login.html")
            return
        if path in ("/register", "/register.html"):
            self._page("register.html")
            return
        if path == "/logout":
            drop_session(self._session_token())
            self._redirect("/login")
            return
        # unprotected assets (so that the login page works)
        if path in ("/style.css", "/i18n.js", "/auth.js"):
            name, ctype = STATIC[path]
            self._file(os.path.join(WEB_DIR, name), ctype)
            return
        if not self._authorized():
            self._unauthorized(path)
            return
        if path == "/api/suggest":
            self._json(api_suggest(qs))
            return
        if path in ("/api/month", "/api/day"):
            if not ip_allowed(self._client_ip()):
                self._json({"ok": False,
                            "error": "too many requests; please wait a bit"}, 429)
                return
            if path == "/api/month":
                self._json(api_month(qs))
            else:
                self._json(api_day(qs))
            return
        if path in STATIC:
            name, ctype = STATIC[path]
            self._file(os.path.join(WEB_DIR, name), ctype)
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def _dispatch_post(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/login":
            self._handle_login()
            return
        if path == "/register":
            self._handle_register()
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def _form(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(length, 65536)).decode("utf-8", "replace")
        return urllib.parse.parse_qs(body)

    def _handle_login(self):
        form = self._form()
        user = (form.get("username") or [""])[0].strip()
        pwd = (form.get("password") or [""])[0]
        ip = self._client_ip()
        if not login_allowed(ip):
            self._redirect("/login?err=lock")
            return
        if not turnstile_ok((form.get("cf-turnstile-response") or [""])[0],
                            ip):
            self._redirect("/login?err=ts")
            return
        row = get_user(user)
        if row and check_password(pwd, row[1]):
            self._redirect("/", set_cookie=create_session(user))
            return
        login_fail(ip)
        self._redirect("/login?err=auth")

    def _handle_register(self):
        form = self._form()
        user = (form.get("username") or [""])[0].strip()
        pwd = (form.get("password") or [""])[0]
        pwd2 = (form.get("password2") or [""])[0]
        invite = (form.get("invite") or [""])[0].strip()
        ip = self._client_ip()

        def fail(code):
            login_fail(ip)
            self._redirect("/register?err=" + code)

        if not login_allowed(ip):
            self._redirect("/register?err=lock")
            return
        if not re.match(r"^[A-Za-z0-9_.-]{3,20}$", user):
            fail("fmt")
            return
        if len(pwd) < 8:
            fail("short")
            return
        if pwd != pwd2:
            fail("mismatch")
            return
        if not turnstile_ok((form.get("cf-turnstile-response") or [""])[0],
                            ip):
            fail("ts")
            return
        if not consume_invite(invite):
            fail("code")
            return
        if get_user(user):
            fail("user")
            return
        try:
            add_user(user, pwd)
        except sqlite3.IntegrityError:
            fail("user")
            return
        AUTH_STATE["users"] = True      # the site locks from the first user on
        AUTH_STATE["t"] = time.time()
        self._redirect("/", set_cookie=create_session(user))

    def _page(self, name):
        try:
            with open(os.path.join(WEB_DIR, name), encoding="utf-8") as fh:
                html = fh.read()
        except OSError:
            self._json({"ok": False, "error": "file not found"}, 404)
            return
        html = html.replace("__TS_SITE__", TS_SITE or "")
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self._json({"ok": False, "error": "file not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    global AUTH
    ap = argparse.ArgumentParser(
        description="Route-selectable flight price calendar")
    ap.add_argument("--port", type=int, default=8124)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--invite", action="store_true",
                    help="print a single-use invite code and exit")
    ap.add_argument("--add-user", metavar="NAME:PASSWORD",
                    help="create a user directly (no invite code needed)")
    args = ap.parse_args()

    AUTH = parse_auth(os.environ.get("FLEX_AUTH", ""))
    init_db()
    init_users_db()
    ensure_places()

    if args.invite:
        print("Invite code (single use): %s" % create_invite())
        return
    if args.add_user:
        name, sep, pwd = args.add_user.partition(":")
        if not sep or not re.match(r"^[A-Za-z0-9_.-]{3,20}$", name.strip()) \
                or len(pwd) < 8:
            print("ERROR: NAME:PASSWORD format; name 3-20 chars, password >= 8")
            return
        try:
            add_user(name.strip(), pwd)
        except sqlite3.IntegrityError:
            print("ERROR: username already exists")
            return
        print("User added: %s" % name.strip())
        return

    print("Budget: %s Google RPC/day | IP limit: %s req/min | "
          "Turnstile: %s"
          % (BUDGET if BUDGET else "unlimited",
             IP_LIMIT if IP_LIMIT else "off",
             "on" if TS_SECRET else "off"))
    if auth_required():
        print("Access: session/Basic Auth required "
              "(%d users, %d basic-auth)"
              % (user_count(), len(AUTH)))
    elif args.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: the site is exposed but no user exists and "
              "FLEX_AUTH is empty; create the first user with --invite.")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print("Flight calendar: http://%s:%d  (%d airports, %d metros)"
          % (args.host, args.port, len(PLACES), len(METRO_GROUPS)))
    print("(press Ctrl+C to exit)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
