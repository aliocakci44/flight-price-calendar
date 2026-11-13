# Flight Price Calendar

A self-hosted, privacy-friendly flight price calendar powered by Google Flights data.
Pick any origin/destination (airports **or** whole cities — e.g. `IST + SAW` are scanned
together when you pick Istanbul), browse a multi-month cheapest-price calendar, and click
a day to see the cheapest itineraries with a deep link to Google Flights.

Fully implemented in Python **standard library only** — no `pip install` required,
Python 3.8+ is enough.

```
python webapp.py --port 8124
# open http://localhost:8124
```

## Features

- **Monthly calendar view** — cheapest price per day for up to ~11 months ahead,
  fetched in a single Google RPC per route/month chunk.
- **City = all airports** — selecting a city scans every airport in it and merges
  results per day (cheapest wins).
- **Day view** — cheapest itineraries for a chosen day, with direct links to
  Google Flights for booking.
- **"Nonstop only" toggle** and currency selector (EUR/USD/TRY/GBP/...);
  the cache is per-currency.
- **11 languages** (tr, en, de, fr, es, it, nl, pt, pl, ro, ru), selected client-side;
  the preference lives in `localStorage` and is never sent to the server.
- **No cookies for price data** — the calendar API is cookie-free. A session cookie
  exists only if you enable the (optional) login system.
- **Multi-user login (optional)** — invite-code registration, scrypt-hashed passwords,
  30-day `HttpOnly` session cookies, per-IP login-attempt throttling.
  With no users registered and no `FLEX_AUTH` set, the site runs open (local dev mode).
- **Self-protection** — daily request budget, per-IP rate limit, and a circuit breaker
  that stops hammering Google when it answers 429/403.

## How the data is fetched

Browsers cannot query Google Flights directly (no CORS on the RPC endpoints), so the
Python server acts as the proxy:

```
Browser  ──/api/*──▶  webapp.py  ──single stateless HTTPS request──▶  Google Flights RPC
                          │
                          ▼
                   SQLite cache (route, date, price, currency only)
```

- Every Google request is a fresh, cookie-less, stdlib HTTP request — nothing about
  you is attached.
- Monthly calendar: `GetCalendarGraph` (one request covers a whole date range).
- Day itineraries: `GetShoppingResults`.
- Calendar months are cached ~2 h, individual days ~20 min; stale cache is served
  as a fallback if Google refuses or the budget is exhausted.

This is an **unofficial** client of an undocumented Google endpoint — built for
personal use. Expect occasional breakage when Google changes its internal format.

## Requirements

- Python 3.8+ (standard library only; tested on Windows, Linux and Android/Termux)
- An internet connection (that's it)

## Quick start

```bash
git clone <repo-url>
cd <repo-name>
python webapp.py --port 8124
```

Open <http://localhost:8124>. The page loads empty; choose a route and the calendar
appears. Each person running this gets their own independent instance: their own
cache, their own limits, and Google requests leaving from their own IP.

## Command-line options

| Flag | Meaning |
|---|---|
| `--port N` | Port to listen on (default `8124`) |
| `--host ADDR` | Bind address (default `127.0.0.1`; use `0.0.0.0` to expose on your LAN) |
| `--invite` | Print a single-use invite code and exit |
| `--add-user NAME:PASSWORD` | Create a user directly (bypasses invite codes) |

The first time a user registers, the site locks itself behind the login page.
While no user exists and `FLEX_AUTH` is unset, the site is open (local development).

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `FLEX_BUDGET` | `300` | Max Google RPC requests per day per install (`0` = unlimited). When exhausted, cached routes are served stale; uncached routes get HTTP 503. |
| `FLEX_IP_LIMIT` | `20` | Calendar API requests per minute per client IP (`0` = off). |
| `FLEX_AUTH` | *(empty)* | Optional Basic Auth fallback, e.g. `name:password` (comma-separated for several users). |
| `FLEX_TS_SITE` / `FLEX_TS_SECRET` | *(empty)* | Cloudflare Turnstile widget keys for the login/register forms. Empty = widget hidden and verification skipped. |

Session data, invite codes and the daily usage counter live in `data/users.db`;
the price cache in `data/webcache.db`. Both are created on first run.
Neither file contains personal data beyond the usernames you create yourself.

## Public deployment (optional)

The server binds to localhost by default. If you want it reachable from the internet
without opening ports, a common pattern is a reverse tunnel, e.g.
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/):

```
Visitor ─▶ Cloudflare edge (bot protection) ─▶ cloudflared connector ─▶ your machine:8124
```

In that setup you can add Turnstile to the login forms via
`FLEX_TS_SITE` / `FLEX_TS_SECRET`, and Cloudflare's Bot Fight Mode gives you edge-level
protection on top. The per-IP limiter reads `X-Forwarded-For`, so it keeps working
behind the tunnel.

## Project layout

```
webapp.py                  HTTP server: routing, auth, cache, budgets, rate limits
rpc_transport.py           Google Flights RPC transport (request building + parsing)
collectors/google_flights.py   Payload decoding helpers (flight rows, TFS encoder)
web/                       Static UI (vanilla JS, no build step)
  index.html / app.js      Monthly calendar page
  gun.html / gun.js        Day page ("gün" = day in Turkish)
  login.html / register.html / auth.js   Auth pages (+ Turnstile support)
  i18n.js                  UI translations (11 languages)
  style.css                Shared styles (dark theme, responsive)
  places.json              Airport/city suggestions (OurAirports-derived data)
```

## Privacy notes

- The calendar/price API works without any cookie; nothing about visitors is stored
  server-side beyond aggregate per-day request counters.
- Language preference is `localStorage`-only, never transmitted.
- The optional session cookie is `HttpOnly`, `SameSite=Lax` (and `Secure` behind HTTPS),
  used solely for authentication; `logout` deletes it.
- Requests to Google are cookie-less single HTTP requests — no Google session,
  no tracking cookies, no NID.

## Disclaimer

This project queries an undocumented Google Flights endpoint and is not affiliated
with, endorsed by, or supported by Google. Use it for personal, low-volume purposes;
respect Google's terms of service. The authors provide no warranty — APIs may break
at any time.
