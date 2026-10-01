#!/usr/bin/env python3
"""Daily market data fetcher (runs in GitHub Actions, open internet, stdlib only).

Writes data/market.json (schema 1):
  series.{spx,dji,ixic,sox,vix,brent,ust10y}.points  [[YYYY-MM-DD, close], ...]  (Yahoo chart API)
  fg.points                                          [[YYYY-MM-DD, int], ...]     (CNN Fear & Greed)
  fedwatch                                           cumulative hike/hold/cut odds for the next
                                                     two FOMC meetings, derived from 30-day Fed
                                                     Funds futures (ZQ) + NY Fed EFFR; or null
  errors                                             human-readable strings, one per failed source

Every source is independent; a failure is recorded in `errors` and the run continues.
Exit code 0 if at least one series was fetched, else 1 (and market.json is NOT overwritten,
so a previously committed good file is preserved).

Optional config.json (next to this script):
  {"fomc_meetings": ["YYYY-MM-DD", ...], "target_range": [lo, hi] | null}
"""
import argparse
import calendar
import datetime as dt
import json
import math
import os
import sys
import urllib.request
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))

YAHOO = {
    "spx": "^GSPC", "dji": "^DJI", "ixic": "^IXIC", "sox": "^SOX",
    "vix": "^VIX", "brent": "BZ=F", "ust10y": "^TNX",
}

# FOMC decision dates (last day of each meeting). The Fed had not published 2027 dates when this
# was written; add them to config.json ("fomc_meetings") once available. Do NOT guess them.
DEFAULT_FOMC_MEETINGS = ["2026-10-28", "2026-12-09"]

EFFR_URL = "https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json"
CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
MONTH_CODES = "FGHJKMNQUVXZ"  # Jan..Dec
FEDWATCH_METHOD = "30-day fed funds futures (ZQ), simplified CME method"


# ---------------------------------------------------------------- HTTP
def http_json(url, headers=None):
    h = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- Yahoo
def parse_yahoo_chart(j, now_ts=None, drop_running=True):
    """Yahoo chart JSON -> ascending [(YYYY-MM-DD, close)], dated in the exchange time zone.

    Null closes are skipped. A still-running regular session is dropped (same logic as
    update_dash.fetch_yahoo): if `now` is inside the regular session and the last point is dated
    on that session's day, it is an intraday quote, not a close.
    """
    res = j["chart"]["result"][0]
    meta = res["meta"]
    tz = ZoneInfo(meta.get("exchangeTimezoneName") or "America/New_York")
    closes = res["indicators"]["quote"][0]["close"]
    out = []
    for ts, c in zip(res["timestamp"], closes):
        if c is None:
            continue
        out.append((dt.datetime.fromtimestamp(ts, tz).date().isoformat(), round(float(c), 4)))
    if drop_running and meta.get("currentTradingPeriod"):
        reg = meta["currentTradingPeriod"]["regular"]
        now = now_ts if now_ts is not None else dt.datetime.now(dt.timezone.utc).timestamp()
        if reg["start"] <= now < reg["end"] and out:
            # Date the running session by its END: futures sessions open the previous evening
            # (e.g. Brent opens 18:00 ET on day D-1 for trade date D), so dating by start would
            # wrongly drop day D-1's already-settled bar.
            today = dt.datetime.fromtimestamp(reg["end"], tz).date().isoformat()
            if out[-1][0] == today:
                out.pop()
    return out


def yahoo_chart(symbol, drop_running=True):
    """Fetch + parse, retrying once on query2 if query1 fails."""
    q = urllib.request.quote(symbol)
    last = None
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{q}?range=3mo&interval=1d"
        try:
            pts = parse_yahoo_chart(http_json(url), drop_running=drop_running)
            if not pts:
                raise ValueError("no data points")
            return pts
        except Exception as e:
            last = e
    raise last


# ---------------------------------------------------------------- CNN Fear & Greed
def parse_fg(j):
    """CNN graphdata JSON -> ascending [(YYYY-MM-DD, int)] in America/New_York dates."""
    rows = []
    for p in j["fear_and_greed_historical"]["data"]:
        d = dt.datetime.fromtimestamp(p["x"] / 1000, ET).date().isoformat()
        rows.append((d, int(round(p["y"]))))
    cur = j["fear_and_greed"]
    d = dt.datetime.fromisoformat(cur["timestamp"].replace("Z", "+00:00")).astimezone(ET).date().isoformat()
    rows = [r for r in rows if r[0] != d] + [(d, int(round(cur["score"])))]
    dedup = {}
    for d, v in rows:
        dedup[d] = v
    return sorted(dedup.items())


CNN_HEADERS = {  # CNN answers HTTP 418 to non-browser clients
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.cnn.com/markets/fear-and-greed",
    "Origin": "https://www.cnn.com",
}


def fetch_fg():
    j = http_json(CNN_URL, CNN_HEADERS)
    return parse_fg(j)


# ---------------------------------------------------------------- FedWatch
def contract_symbol(year, month):
    return f"ZQ{MONTH_CODES[month - 1]}{year % 100:02d}.CBT"


def next_month(year, month):
    return (year + 1, 1) if month == 12 else (year, month + 1)


def compute_fedwatch(effr, meetings, prices, today, target_range=None, asof=None):
    """Pure function. Cumulative odds (vs the CURRENT range) for up to the next two meetings.

    effr         current effective fed funds rate, percent (e.g. 3.88)
    meetings     iterable of decision dates (YYYY-MM-DD strings or dates)
    prices       {(year, month): futures price or None}; ZQ price = 100 - expected monthly avg rate
    today        date; only meetings strictly after it are used (first two)
    target_range optional [lo, hi]; default lower = floor(effr*4)/4, hi = lo + 0.25
    asof         date string for the output (default: today)
    Returns the fedwatch dict, or None if effr is missing/zero or no meeting could be computed.
    """
    if not effr:
        return None
    effr = float(effr)
    if target_range:
        lo, hi = float(target_range[0]), float(target_range[1])
    else:
        lo = math.floor(effr * 4 + 1e-9) / 4
        hi = lo + 0.25
    ms = sorted(dt.date.fromisoformat(m) if isinstance(m, str) else m for m in meetings)
    ms = [m for m in ms if m > today][:2]

    out = []
    r_start = effr
    dist = {0: 1.0}  # expanding tree: P(cumulative number of 25bp steps since today)
    for m in ms:
        n = calendar.monthrange(m.year, m.month)[1]
        d = m.day
        p_m = prices.get((m.year, m.month))
        if n - d >= 7 and p_m is not None:
            avg = 100 - p_m
            r_end = (avg * n - r_start * d) / (n - d)
        else:
            p_next = prices.get(next_month(m.year, m.month))
            if p_next is None:
                break  # needed contract missing: stop the chain
            r_end = 100 - p_next
        # This meeting's expected move in 25bp steps, split between the two nearest whole steps
        # and applied to every branch of the tree (CME FedWatch-style).
        move = (r_end - r_start) * 100 / 25
        lo_step = math.floor(move)
        frac = move - lo_step
        nd = {}
        for s, p in dist.items():
            nd[s + lo_step] = nd.get(s + lo_step, 0.0) + p * (1 - frac)
            nd[s + lo_step + 1] = nd.get(s + lo_step + 1, 0.0) + p * frac
        dist = nd
        hike = round(sum(p for s, p in dist.items() if s > 0) * 100, 1) + 0.0
        cut = round(sum(p for s, p in dist.items() if s < 0) * 100, 1) + 0.0
        hold = round(100 - hike - cut, 1) + 0.0
        out.append({"date": m.isoformat(), "hike": hike, "hold": hold, "cut": cut,
                    "implied_rate": round(r_end, 4)})
        r_start = r_end
    if not out:
        return None
    return {
        "asof": asof or today.isoformat(),
        "method": FEDWATCH_METHOD,
        "effr": round(effr, 4),
        "range": [lo, hi],
        "meetings": out,
    }


def fetch_effr():
    j = http_json(EFFR_URL)
    return float(j["refRates"][0]["percentRate"])


def fetch_fedwatch(cfg, now_utc, errors):
    """Thin wrapper: fetch EFFR + needed ZQ contracts, then compute_fedwatch. None on failure."""
    today = now_utc.date()
    try:
        effr = fetch_effr()
    except Exception as e:
        errors.append(f"fedwatch: EFFR fetch failed ({type(e).__name__}: {str(e)[:120]})")
        return None
    if not effr:
        errors.append("fedwatch: EFFR missing or zero")
        return None
    meetings = cfg.get("fomc_meetings") or DEFAULT_FOMC_MEETINGS
    upcoming = sorted(m for m in (dt.date.fromisoformat(x) for x in meetings) if m > today)[:2]
    if not upcoming:
        errors.append("fedwatch: no upcoming FOMC meetings in config (add new dates to config.json)")
        return None
    months = set()
    for m in upcoming:
        if calendar.monthrange(m.year, m.month)[1] - m.day >= 7:
            months.add((m.year, m.month))  # same-month contract is only used when >= 7 days remain
        months.add(next_month(m.year, m.month))
    prices = {}
    for ym in sorted(months):
        sym = contract_symbol(*ym)
        try:
            pts = yahoo_chart(sym, drop_running=False)
            prices[ym] = pts[-1][1]
        except Exception as e:
            errors.append(f"fedwatch: {sym} failed ({type(e).__name__}: {str(e)[:120]})")
    res = compute_fedwatch(effr, meetings, prices, today, cfg.get("target_range"), today.isoformat())
    if res is None:
        errors.append("fedwatch: no meetings computed (missing futures contracts)")
    elif len(res["meetings"]) < len(upcoming):
        errors.append(f"fedwatch: only {len(res['meetings'])} of {len(upcoming)} meetings computed (missing contract)")
    return res


# ---------------------------------------------------------------- main
def load_config(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def build_market(cfg, now_utc):
    errors = []
    series = {}
    for key, sym in YAHOO.items():
        try:
            series[key] = {"symbol": sym, "points": [[d, v] for d, v in yahoo_chart(sym)]}
        except Exception as e:
            errors.append(f"{key} ({sym}): {type(e).__name__}: {str(e)[:150]}")
    fg = None
    try:
        fg = {"points": [[d, v] for d, v in fetch_fg()]}
    except Exception as e:
        errors.append(f"fg: {type(e).__name__}: {str(e)[:150]}")
    try:
        fedwatch = fetch_fedwatch(cfg, now_utc, errors)
    except Exception as e:  # defensive: never let fedwatch kill the run
        errors.append(f"fedwatch: {type(e).__name__}: {str(e)[:150]}")
        fedwatch = None
    market = {
        "schema": 1,
        "fetched_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "series": series,
        "fg": fg if fg is not None else {"points": []},
        "fedwatch": fedwatch,
        "errors": errors,
    }
    return market


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "data", "market.json"))
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    market = build_market(cfg, dt.datetime.now(dt.timezone.utc))
    for e in market["errors"]:
        print("ERROR:", e, file=sys.stderr)
    if not market["series"]:
        print("no series fetched; leaving existing market.json untouched", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(market, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, a.out)
    print(f"wrote {a.out}: {len(market['series'])}/{len(YAHOO)} series, fg={'ok' if market['fg']['points'] else 'missing'}, "
          f"fedwatch={'ok' if market['fedwatch'] else 'null'}, {len(market['errors'])} errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
