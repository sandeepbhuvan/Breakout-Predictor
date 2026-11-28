"""
Nifty 500 Breakout Predictor - desktop app (Upstox real-time).

Standard library only (Tkinter + urllib). Run:  py breakout_predictor_app.py

Goal: flag stocks LIKELY to break out within the next 7 sessions, before it happens.

How a "setup" is scored (0-100), using completed daily candles:
  Breakout level (R)   = highest high of the last 20 sessions
  Proximity      (20)  = live price is 0..MAX_DIST % below R (already above R = too late, skipped)
  Contraction    (15)  = 5-day ATR shrinking vs 20-day ATR
  Squeeze        (15)  = Bollinger bandwidth near its 40-day low
  Higher lows    (15)  = last 5 sessions' low above the prior 10 sessions' low
  Accumulation   (15)  = up-day volume beats down-day volume (15 sessions)
  Trend          (10)  = close > 50 SMA and 20 SMA > 50 SMA
  Resistance tests(10) = R has been touched (within 1.5%) several times

A prediction "hits" if any daily CLOSE above R happens within 7 sessions (counting the day it
was made). Every prediction is logged and graded automatically, and "Run backtest" measures
the hit rate of the same rules over the past year so you can judge the threshold.

Daily: ~500 throttled candle calls once (cached). Every minute in market hours: 3 batched quote
calls. Email goes out ONLY when new stocks qualify (each stock once while its prediction is open).

Secrets (Upstox token, SMTP password) are never written to disk; they can be prefilled from the
environment variables UPSTOX_ACCESS_TOKEN and HHLL_SMTP_PASSWORD.
"""

import bisect
import csv
import gzip
import io
import json
import os
import queue
import re
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

IST = timezone(timedelta(hours=5, minutes=30))
APP_DIR = Path.home() / ".breakout_predictor"
API = "https://api.upstox.com/v2"
UPSTOX_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
NIFTY_500_CSV_URLS = [
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "https://raw.githubusercontent.com/kprohith/nse-stock-analysis/master/ind_nifty500list.csv",
]
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

APP_USER_AGENT = "BreakoutPredictor/1.0 (Python; Upstox API client)"

MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)
HISTORY_CALENDAR_DAYS = 365   # ~245 sessions: enough for indicators + a backtest
MIN_BARS = 60                 # first index at which every indicator is defined
HORIZON = 7                   # prediction window in sessions
QUOTE_BATCH = 200             # Upstox allows up to 500 keys per quote call
HISTORY_REQ_PER_SEC = 6       # stays under Upstox's 500 requests/minute limit

SCORE_VERSION = 2  # bump when the score changes meaning; resets min_score to the new default

DEFAULT_CONFIG = {
    "score_version": SCORE_VERSION,
    "close_to_tray": True,
    "min_score": 70,
    "max_dist_pct": 3.0,
    "min_price": 20.0,
    "nifty_csv_path": "",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 465,
    "smtp_user": "",
    "email_to": "",
    "max_emails_per_day": 20,
    "wma200_filter": False,
    "target_pct": 10.0,
    "pnl_mode": "pct",
    "trade_amount": 100000,
}
WEEKLY_CALENDAR_DAYS = 1950   # ~5.3 years: 200 completed weeks before every backtest day
WMA_WEEKS = 200


# =========================================================================== utils

def now_ist():
    return datetime.now(IST)


def today_str(dt=None):
    return (dt or now_ist()).strftime("%Y-%m-%d")


def is_market_open(dt):
    return dt.weekday() < 5 and MARKET_OPEN <= (dt.hour, dt.minute) <= MARKET_CLOSE


def next_open(dt):
    """Next weekday 09:15 IST at or after dt (NSE holidays not modelled)."""
    cand = dt.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    if cand <= dt:
        cand += timedelta(days=1)
    while cand.weekday() >= 5:
        cand += timedelta(days=1)
    return cand


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    saved = read_json(APP_DIR / "config.json", {}) or {}
    cfg.update(saved)
    if saved and saved.get("score_version") != SCORE_VERSION:
        # The score was rebuilt, so an old threshold means something else now: start from the new default.
        cfg["min_score"] = DEFAULT_CONFIG["min_score"]
        cfg["score_version"] = SCORE_VERSION
        save_config(cfg)
        cfg["_score_migrated"] = True
    return cfg


def save_config(cfg):
    write_json(APP_DIR / "config.json", {k: cfg[k] for k in DEFAULT_CONFIG if k in cfg})


def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# =========================================================================== indicators / scoring

def rolling_mean(x, w):
    out, s = [None] * len(x), 0.0
    for i, val in enumerate(x):
        s += val
        if i >= w:
            s -= x[i - w]
        if i >= w - 1:
            out[i] = s / w
    return out


def rolling_std(x, w):
    out, s, ss = [None] * len(x), 0.0, 0.0
    for i, val in enumerate(x):
        s += val
        ss += val * val
        if i >= w:
            s -= x[i - w]
            ss -= x[i - w] ** 2
        if i >= w - 1:
            m = s / w
            out[i] = max(ss / w - m * m, 0.0) ** 0.5
    return out


def compute_series(stock):
    """Precompute rolling indicators once per stock. Arrays are oldest-first."""
    h, l, c = stock["h"], stock["l"], stock["c"]
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    sma20, std20 = rolling_mean(c, 20), rolling_std(c, 20)
    bw = [None if m is None or not m else 4 * s / m for m, s in zip(sma20, std20)]
    return {**stock, "sma20": sma20, "sma50": rolling_mean(c, 50), "sma100": rolling_mean(c, 100), "bw": bw,
            "atr5": rolling_mean(tr, 5), "atr20": rolling_mean(tr, 20)}


RS_LOOKBACK = 60  # sessions for relative strength


def ret60(S, t):
    """60-session return at bar t, or None without enough history."""
    c = S["c"]
    return c[t] / c[t - RS_LOOKBACK] - 1 if t >= RS_LOOKBACK and c[t - RS_LOOKBACK] else None


def rs_percentile(sorted_returns, r):
    """Where a 60-session return ranks among every stock's on the same day (0 = weakest, 1 = strongest)."""
    if r is None or not sorted_returns:
        return None
    return bisect.bisect_left(sorted_returns, r) / len(sorted_returns)


def setup_components(S, t, rs):
    """Score parts that don't depend on the distance to R (max 80), using bars up to and including index t.

    Score v2 (see README): relative strength 25, 5-day momentum 20, trend 15, near 1-year high 10,
    accumulation 10, plus proximity 20 from proximity(). rs = rs_percentile() of this stock's 60-session return
    against all stocks on the same day (None counts as 0)."""
    h, c, v = S["h"], S["c"], S["v"]
    R = max(h[t - 19:t + 1])
    rs = rs or 0.0
    mom5 = (c[t] / c[t - 5] - 1) * 100 if c[t - 5] else 0.0
    sma50, sma100 = S["sma50"][t], S["sma100"][t]
    trend = 0.5 * bool(sma50 and c[t] > sma50 and S["sma20"][t] > sma50) + 0.5 * bool(sma100 and c[t] > sma100)
    near_high = c[t] / max(h[max(0, t - 249):t + 1])
    up = sum(v[i] for i in range(t - 14, t + 1) if c[i] > c[i - 1])
    dn = sum(v[i] for i in range(t - 14, t + 1) if c[i] < c[i - 1])
    accum = clamp((up / max(dn, 1) - 0.8) / 1.6)

    base = (25 * rs + 20 * clamp(mom5 / 8) + 15 * trend + 10 * clamp((near_high - 0.85) / 0.15) + 10 * accum)
    reasons = []
    if rs >= 0.8:
        reasons.append(f"stronger than {rs * 100:.0f}% of stocks")
    if mom5 >= 4:
        reasons.append(f"up {mom5:.0f}% in 5 days")
    if trend == 1:
        reasons.append("uptrend")
    if near_high >= 0.97:
        reasons.append("near 1-year high")
    if accum >= 0.5:
        reasons.append("accumulation")
    return {"R": R, "base": base, "reasons": reasons}


def returns_by_date(series):
    """{date: sorted 60-session returns of every stock that day}, for rs_percentile in the backtest."""
    out = {}
    for S in series.values():
        for t in range(RS_LOOKBACK, len(S["c"])):
            r = ret60(S, t)
            if r is not None:
                out.setdefault(S["d"][t], []).append(r)
    for v in out.values():
        v.sort()
    return out


def proximity(R, price, max_dist):
    """(score 0-20, distance %) or None if price is already above R or too far below."""
    if price <= 0:
        return None
    dist = (R - price) / price * 100
    if dist < 0 or dist > max_dist:
        return None
    return 20 * (1 - dist / max_dist), dist


# --------------------------------------------------------------------------- breakout -> retest -> buy-in

RETEST_TOL = 0.015     # a retest touch: the day's low comes within 1.5% above the old ceiling...
HOLD_BELOW = 0.01      # ...and the close stays no more than 1% below it
FAIL_BELOW = 0.03      # a close 3%+ below the ceiling = failed breakout
RETEST_WINDOW = 10     # sessions after the breakout to wait for a retest
ENTRY_WINDOW = 5       # sessions after the latest retest touch to wait for the bounce
MAX_HOLD = 30          # sessions to hold a buy-in before calling it expired
STOP_BUFFER = 0.01     # stop sits 1% below the retest day's low

TRADE_CLOSED = ("win", "loss", "expired", "failed", "no_retest", "no_entry")
TRADE_LABELS = {"waiting_retest": "Waiting for retest", "retesting": "Retesting", "entered": "In trade",
                "win": "✓ Target hit", "loss": "✗ Stopped out", "expired": "Expired",
                "failed": "Failed breakout", "no_retest": "No retest", "no_entry": "No bounce"}


def simulate_exit(S, entry_idx, entry, stop, target, n):
    """Exits from the session after entry. Same-day stop and target counts as the stop (conservative).
    Gaps fill at the open. Returns (status, exit_idx, exit_price) or None if still open at bar n."""
    h, l, c = S["h"], S["l"], S["c"]
    o = S.get("o")
    for i in range(entry_idx + 1, n):
        if l[i] <= stop:
            return "loss", i, min(o[i], stop) if o else stop
        if h[i] >= target:
            return "win", i, max(o[i], target) if o else target
        if i - entry_idx >= MAX_HOLD:
            return "expired", i, c[i]
    return None


def simulate_trade(S, b, R, target_pct, n=None):
    """Follow a breakout (close > R at index b) through retest, bounce entry and exit, using daily bars < n."""
    n = len(S["c"]) if n is None else n
    h, l, c, d = S["h"], S["l"], S["c"], S["d"]
    o = S.get("o")
    st = {"breakout_date": d[b], "R": R, "breakout_close": c[b], "status": "waiting_retest"}
    retest = None
    for i in range(b + 1, n):
        if c[i] < R * (1 - FAIL_BELOW):
            st.update(status="failed", closed_date=d[i])
            return st
        if retest is not None and h[i] > h[retest]:
            trigger = h[retest]
            entry = max(o[i], trigger) if o else trigger
            stop = l[retest] * (1 - STOP_BUFFER)
            target = entry * (1 + target_pct / 100)
            st.update(status="entered", entry_date=d[i], entry_price=round(entry, 2), stop=round(stop, 2),
                      target=round(target, 2))
            ex = simulate_exit(S, i, entry, stop, target, n)
            if ex:
                status, j, price = ex
                st.update(status=status, closed_date=d[j], exit_price=round(price, 2),
                          return_pct=round((price - entry) / entry * 100, 2),
                          r_multiple=round((price - entry) / max(entry - stop, 1e-9), 2))
            return st
        if l[i] <= R * (1 + RETEST_TOL) and c[i] >= R * (1 - HOLD_BELOW):
            retest = i
            st.update(status="retesting", retest_date=d[i], retest_low=l[i], retest_high=h[i],
                      buy_above=round(h[i], 2), stop=round(l[i] * (1 - STOP_BUFFER), 2))
            continue
        if retest is None and i - b >= RETEST_WINDOW:
            st.update(status="no_retest", closed_date=d[i])
            return st
        if retest is not None and i - retest >= ENTRY_WINDOW:
            st.update(status="no_entry", closed_date=d[i])
            return st
    return st


def backtest(history, cfg, stop_event=None, weekly=None):
    """Walk forward through stored history with the exact live rules.

    With weekly data it also splits score >= threshold setups by the 200-week MA, so you can see whether the
    filter actually helps before switching it on."""
    thr, max_dist, min_price = float(cfg["min_score"]), float(cfg["max_dist_pct"]), float(cfg["min_price"])
    use_filter = bool(cfg.get("wma200_filter")) and weekly is not None
    buckets = {b: [0, 0] for b in ("<50", "50-60", "60-70", "70-80", "80+")}
    near, picked = [0, 0], [0, 0]
    split = {"above": [0, 0], "below": [0, 0], "no data": [0, 0]}
    target_pct = float(cfg.get("target_pct") or 10)
    trades, baseline_trades = [], []
    all_series = {k: compute_series(st_) for k, st_ in history.items() if len(st_["c"]) >= MIN_BARS + HORIZON + 1}
    rs_by_date = returns_by_date(all_series)
    for key_, S in all_series.items():
        if stop_event and stop_event.is_set():
            break
        n = len(S["c"])
        c = S["c"]
        w = weekly.get(key_) if weekly else None
        seen_breakouts = set()
        for t in range(max(MIN_BARS - 1, RS_LOOKBACK), n - HORIZON):
            if c[t] < min_price:
                continue
            comp = setup_components(S, t, rs_percentile(rs_by_date.get(S["d"][t]), ret60(S, t)))
            prox = proximity(comp["R"], c[t], max_dist)
            if not prox:
                continue
            score = comp["base"] + prox[0]
            hit = any(c[j] > comp["R"] for j in range(t + 1, t + 1 + HORIZON))
            bucket = "<50" if score < 50 else "80+" if score >= 80 else f"{int(score // 10) * 10}-{int(score // 10) * 10 + 10}"
            aggs = [buckets[bucket], near]
            if score >= thr:
                side = "no data"
                if weekly is not None:
                    wma = wma200_at(w, S["d"][t])
                    side = "no data" if wma is None else "above" if c[t] > wma else "below"
                    aggs.append(split[side])
                if not use_filter or side == "above":
                    aggs.append(picked)
                    b = next((j for j in range(t + 1, t + 1 + HORIZON) if c[j] > comp["R"]), None)
                    if b is not None and b not in seen_breakouts:
                        seen_breakouts.add(b)
                        trades.append(simulate_trade(S, b, comp["R"], target_pct, n))
                        stop = S["l"][b] * (1 - STOP_BUFFER)  # comparison: buy the breakout close, no retest
                        ex = simulate_exit(S, b, c[b], stop, c[b] * (1 + target_pct / 100), n)
                        if ex:
                            baseline_trades.append({"status": ex[0], "return_pct": (ex[2] - c[b]) / c[b] * 100})
            for agg in aggs:
                agg[0] += 1
                agg[1] += hit
    rate = lambda a: f"{a[1] / a[0] * 100:.1f}% of {a[0]:,}" if a[0] else "n/a"  # noqa: E731
    lines = [f"Backtest: close above 20-day high within {HORIZON} sessions",
             f"  All stocks within {max_dist}% of R (baseline): {rate(near)}",
             f"  Your setups (score >= {thr:g}{', above 200W MA' if use_filter else ''}): {rate(picked)}",
             "  By score bucket:"]
    lines += [f"    {k:>6}: {rate(v)}" for k, v in buckets.items()]
    if weekly is not None:
        lines += [f"  Setups (score >= {thr:g}) split by the 200-week MA:",
                  f"    above 200W MA: {rate(split['above'])}",
                  f"    below 200W MA: {rate(split['below'])}",
                  f"    under 200 weeks of data (newer listings): {rate(split['no data'])}",
                  "  If 'above' clearly beats 'below', the 200-week filter is worth switching on."]
    lines.append("  (Overlapping days on the same stock are counted separately.)")
    lines.append("  Judge the threshold by the LIFT over the baseline, not the absolute %.")

    summary = trade_summary(trades)
    base = trade_summary(baseline_trades)
    retested = sum(1 for t in trades if t.get("retest_date"))
    lines += ["",
              f"Buy-ins: breakout, retest, then a move above the retest day's high "
              f"(target +{target_pct:g}%, stop 1% below the retest low, max {MAX_HOLD} sessions)",
              f"  Predicted breakouts: {len(trades):,}  |  retested: "
              f"{retested / len(trades) * 100 if trades else 0:.0f}%  |  buy-ins: {summary['entries']:,} "
              f"(closed: {summary['closed']:,})",
              f"  {summary['text']}",
              f"  For comparison, buying at the breakout close (stop below breakout day's low): {base['text']}",
              "  No brokerage, taxes or slippage included."]
    if near[0] and picked[0]:
        write_json(APP_DIR / "backtest.json", {"baseline": near[1] / near[0] * 100,
                                               "setups": picked[1] / picked[0] * 100,
                                               "min_score": thr, "date": today_str(),
                                               "buyin_win": summary["win_rate"], "buyin_avg": summary["avg"],
                                               "buyin_closed": summary["closed"]})
    return "\n".join(lines)


def trade_summary(trades):
    closed = [t for t in trades if t.get("status") in ("win", "loss", "expired")]
    entries = sum(1 for t in trades if t.get("status") in ("entered", "win", "loss", "expired"))
    if not closed:
        return {"entries": entries, "closed": 0, "win_rate": None, "avg": None, "text": "no closed trades yet"}
    wins = sum(t["status"] == "win" for t in closed)
    losses = sum(t["status"] == "loss" for t in closed)
    avg = sum(t["return_pct"] for t in closed) / len(closed)
    return {"entries": entries, "closed": len(closed), "win_rate": wins / len(closed) * 100, "avg": avg,
            "text": f"target hit {wins / len(closed) * 100:.0f}%  |  stopped {losses / len(closed) * 100:.0f}%  |  "
                    f"expired {(len(closed) - wins - losses) / len(closed) * 100:.0f}%  |  "
                    f"avg return per trade {avg:+.2f}%  ({len(closed):,} trades)"}


# =========================================================================== Upstox

class AuthError(Exception):
    pass


class BlockedError(Exception):
    pass


class RateLimiter:
    def __init__(self, per_second):
        self.interval = 1.0 / per_second
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        with self.lock:
            t = time.monotonic()
            if t < self.next_at:
                time.sleep(self.next_at - t)
                t = self.next_at
            self.next_at = t + self.interval


class UpstoxClient:
    def __init__(self, token):
        # Upstox's firewall rejects Python's default "Python-urllib" agent (Cloudflare 1010), so identify the app.
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
                        "User-Agent": APP_USER_AGENT}

    def get(self, path, params=None, limiter=None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(4):
            if limiter:
                limiter.wait()
            try:
                return json.loads(http_get(url, self.headers))
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise AuthError("Upstox rejected the token (401). It may have expired.")
                if e.code == 403:
                    body = e.read()[:400].decode("utf-8", "replace")
                    if "1010" in body or "cloudflare" in body.lower():
                        raise BlockedError("Upstox's firewall blocked this request (Cloudflare 1010).")
                    raise RuntimeError(f"Upstox 403 on {path}: {body[:200]}")
                if e.code == 429 or e.code >= 500:
                    time.sleep(2 * (attempt + 1))
                    continue
                body = e.read()[:600].decode("utf-8", "replace")
                raise RuntimeError(f"Upstox {e.code} on {path}: {body}")
            except urllib.error.URLError:
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"Upstox request kept failing: {path}")

    def quotes(self, keys):
        out = {}
        for i in range(0, len(keys), QUOTE_BATCH):
            data = self.get("/market-quote/quotes", {"instrument_key": ",".join(keys[i:i + QUOTE_BATCH])}).get("data") or {}
            for q in data.values():
                if q and q.get("instrument_token"):
                    out[q["instrument_token"]] = q
        return out

    def candles(self, key, interval, to_date, from_date, limiter):
        path = f"/historical-candle/{urllib.parse.quote(key, safe='')}/{interval}/{to_date}/{from_date}"
        return (self.get(path, limiter=limiter).get("data") or {}).get("candles") or []

    def daily_candles(self, key, to_date, from_date, limiter):
        return self.candles(key, "day", to_date, from_date, limiter)


# =========================================================================== data

def load_universe(cfg, log):
    cache_path = APP_DIR / "universe.json"
    cached = read_json(cache_path)
    if cached and cached.get("date") == today_str() and len(cached.get("items", [])) >= 450:
        return cached["items"]

    csv_text = None
    if cfg.get("nifty_csv_path"):
        csv_text = Path(cfg["nifty_csv_path"]).read_text(encoding="utf-8-sig")
        log(f"Loaded Nifty 500 list from {cfg['nifty_csv_path']}")
    else:
        for url in NIFTY_500_CSV_URLS:
            host = urllib.parse.urlparse(url).netloc
            try:
                csv_text = http_get(url, {"User-Agent": BROWSER_UA}, timeout=20).decode("utf-8-sig")
                log(f"Downloaded Nifty 500 list from {host}")
                break
            except Exception as e:  # noqa: BLE001 - try the next source
                log(f"Nifty 500 source failed ({host}): {e}")
    if not csv_text:
        raise RuntimeError("Could not load the Nifty 500 list. Set a local CSV path in settings.")

    base = []
    for r in csv.DictReader(io.StringIO(csv_text)):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        if r.get("Symbol"):
            base.append({"symbol": r["Symbol"], "isin": r.get("ISIN Code", ""), "industry": r.get("Industry", "")})

    log("Downloading Upstox NSE instrument master...")
    master = json.loads(gzip.decompress(http_get(UPSTOX_MASTER_URL, timeout=60)))
    by_isin, by_symbol = {}, {}
    for inst in master:
        if inst.get("segment") == "NSE_EQ" and inst.get("instrument_type") == "EQ":
            if inst.get("isin"):
                by_isin[inst["isin"].strip()] = inst["instrument_key"]
            if inst.get("trading_symbol"):
                by_symbol[inst["trading_symbol"].strip()] = inst["instrument_key"]

    items = [{**b, "key": by_isin.get(b["isin"]) or by_symbol.get(b["symbol"])} for b in base]
    items = [i for i in items if i["key"]]
    if len(items) < 450:
        raise RuntimeError(f"Only matched {len(items)} Nifty 500 stocks to Upstox keys; aborting.")
    write_json(cache_path, {"date": today_str(), "items": items})
    log(f"Universe ready: {len(items)} stocks.")
    return items


def download_all(universe, fetch_one, log, progress, stop_event):
    """Run fetch_one(item, limiter) for every stock, throttled. Returns ({key: result}, errors)."""
    limiter = RateLimiter(HISTORY_REQ_PER_SEC)
    out, done, auth_failures, errors = {}, 0, 0, 0
    lock = threading.Lock()
    blocked = threading.Event()

    def fetch(item):
        nonlocal done, auth_failures, errors
        if stop_event.is_set() or blocked.is_set():
            return
        try:
            result = fetch_one(item, limiter)
            if result is not None:
                with lock:
                    out[item["key"]] = result
        except AuthError:
            with lock:
                auth_failures += 1
        except BlockedError:
            blocked.set()
        except Exception as e:  # noqa: BLE001 - one bad symbol shouldn't stop the build
            with lock:
                errors += 1
                if errors <= 5:
                    log(f"History failed for {item['symbol']}: {e}")
        finally:
            with lock:
                done += 1
                progress(done, len(universe))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fetch, universe))

    if stop_event.is_set():
        raise InterruptedError("Stopped")
    if blocked.is_set():
        raise BlockedError("Upstox's firewall blocked the history download (Cloudflare 1010). "
                           "Try again later or from another network.")
    if auth_failures and not out:
        raise AuthError("Upstox rejected the token (401). It may have expired.")
    return out, errors


def _save_replacing(path, data, pattern):
    write_json(path, data)
    for old in APP_DIR.glob(pattern):
        if old != path:
            old.unlink(missing_ok=True)


def build_history(client, universe, log, progress, stop_event):
    """~1 year of completed daily candles per stock (today excluded), oldest-first. Cached per day."""
    path = APP_DIR / f"history_{today_str()}.json"
    cached = read_json(path)
    if cached:
        return cached

    log(f"Downloading daily history for {len(universe)} stocks (~{len(universe) // HISTORY_REQ_PER_SEC + 5}s)...")
    to_date = today_str()
    from_date = (now_ist() - timedelta(days=HISTORY_CALENDAR_DAYS)).strftime("%Y-%m-%d")

    def fetch_one(item, limiter):
        candles = client.daily_candles(item["key"], to_date, from_date, limiter)
        # Upstox returns newest-first: [timestamp, open, high, low, close, volume, oi]
        bars = sorted((c for c in candles if not str(c[0]).startswith(to_date)), key=lambda c: str(c[0]))
        if len(bars) < MIN_BARS:
            return None
        return {"symbol": item["symbol"], "industry": item["industry"],
                "d": [str(b[0])[:10] for b in bars], "o": [float(b[1]) for b in bars],
                "h": [float(b[2]) for b in bars], "l": [float(b[3]) for b in bars],
                "c": [float(b[4]) for b in bars], "v": [float(b[5]) for b in bars]}

    history, errors = download_all(universe, fetch_one, log, progress, stop_event)
    if len(history) < 400:
        raise RuntimeError(f"Only built history for {len(history)} stocks ({errors} errors); not saving.")
    _save_replacing(path, history, "history_*.json")
    log(f"History saved for {len(history)} stocks.")
    return history


def build_weekly(client, universe, log, progress, stop_event):
    """~5 years of weekly closes per stock for the 200-week moving average. Cached per ISO week."""
    year, week, _ = now_ist().isocalendar()
    path = APP_DIR / f"weekly_{year}-W{week:02d}.json"
    cached = read_json(path)
    if cached:
        return cached

    log(f"Downloading weekly history for the 200-week MA ({len(universe)} stocks, ~"
        f"{len(universe) // HISTORY_REQ_PER_SEC + 5}s, once a week)...")
    to_date = today_str()
    from_date = (now_ist() - timedelta(days=WEEKLY_CALENDAR_DAYS)).strftime("%Y-%m-%d")

    def fetch_one(item, limiter):
        bars = sorted(client.candles(item["key"], "week", to_date, from_date, limiter), key=lambda c: str(c[0]))
        return {"d": [str(b[0])[:10] for b in bars], "c": [float(b[4]) for b in bars]} if bars else None

    weekly, errors = download_all(universe, fetch_one, log, progress, stop_event)
    if len(weekly) < 400:
        raise RuntimeError(f"Only got weekly history for {len(weekly)} stocks ({errors} errors); not saving.")
    _save_replacing(path, weekly, "weekly_*.json")
    enough = sum(1 for w in weekly.values() if len(w["c"]) > WMA_WEEKS)
    log(f"Weekly history saved. {enough} of {len(weekly)} stocks have 200+ weeks of data "
        "(newer listings can't have a 200-week MA).")
    return weekly


def wma200_at(w, date_str):
    """200-week simple MA of weekly closes from weeks that finished before date_str's week. None if too short."""
    if not w:
        return None
    if "ps" not in w:
        ps = [0.0]
        for c in w["c"]:
            ps.append(ps[-1] + c)
        w["ps"] = ps
    cutoff = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    idx = bisect.bisect_right(w["d"], cutoff)
    if idx < WMA_WEEKS:
        return None
    return (w["ps"][idx] - w["ps"][idx - WMA_WEEKS]) / WMA_WEEKS


# =========================================================================== prediction log

def data_dir():
    """Where the current user's predictions live: per account when signed in, else the shared local folder."""
    return ACCOUNT.data_dir() if ACCOUNT else APP_DIR


def load_predictions():
    return read_json(data_dir() / "predictions.json", []) or []


def save_predictions(preds):
    write_json(data_dir() / "predictions.json", preds)
    if ACCOUNT and ACCOUNT.signed_in:
        ACCOUNT.queue_push(preds)


def pred_id(p):
    return f"{p['date']}_{p['symbol']}"


def load_trades():
    return read_json(data_dir() / "trades.json", []) or []


def save_trades(trades):
    write_json(data_dir() / "trades.json", trades)
    if ACCOUNT and ACCOUNT.signed_in:
        ACCOUNT.queue_push(trades, "trades")


def trade_rank(t):
    s = t.get("status")
    return 3 if s in TRADE_CLOSED else 2 if s == "entered" else 1 if s == "retesting" else 0


MY_FIELDS = ("my_buy_price", "my_buy_time", "my_sell_price", "my_sell_time", "my_updated")


def merge_trades(*lists):
    """App-tracked fields come from the most advanced copy; your own buy/sell fields from the most recent edit."""
    out, mine = {}, {}
    for lst in lists:
        for t in lst or []:
            tid = t["id"]
            if tid not in out or trade_rank(t) > trade_rank(out[tid]):
                out[tid] = dict(t)
            if "my_updated" in t and t["my_updated"] >= mine.get(tid, {}).get("my_updated", ""):
                mine[tid] = {k: t[k] for k in MY_FIELDS if k in t}
    for tid, fields in mine.items():
        for k in MY_FIELDS:
            out[tid].pop(k, None)
        out[tid].update(fields)
    return sorted(out.values(), key=lambda t: t.get("breakout_date", ""))


def pct(a, b):
    return (b - a) / a * 100 if a else None


def app_pnl(t, price_now):
    """(pct, realised?) for the app's recommended buy-in, or (None, False)."""
    if t.get("status") in ("win", "loss", "expired") and t.get("return_pct") is not None:
        return t["return_pct"], True
    if t.get("status") == "entered" and t.get("entry_price") and price_now:
        return pct(t["entry_price"], price_now), False
    return None, False


def my_pnl(t, price_now):
    """(pct, realised?) for your own buy, against your sell price if set, else the latest price."""
    if not t.get("my_buy_price"):
        return None, False
    if t.get("my_sell_price"):
        return pct(t["my_buy_price"], t["my_sell_price"]), True
    return (pct(t["my_buy_price"], price_now), False) if price_now else (None, False)


def pnl_amount(pct_value, entry, amount):
    """Rupee P&L of putting `amount` into whole shares at `entry` and moving pct_value %. None if unknown."""
    if pct_value is None or not entry or not amount:
        return None
    qty = int(amount // entry)
    return qty * entry * pct_value / 100 if qty else None


def build_trades(preds, history, existing, target_pct):
    """One buy-in record per prediction that broke out. Closed records are kept as they are; open ones are
    recomputed from the latest daily bars. Live-alert flags on open records are preserved."""
    by_symbol = {s["symbol"]: s for s in history.values()}
    old = {t["id"]: t for t in existing}
    out = []
    for p in preds:
        if p.get("status") != "hit" or not p.get("resolved"):
            continue
        tid = pred_id(p)
        prev = old.get(tid)
        s = by_symbol.get(p["symbol"])
        if prev and prev.get("status") in TRADE_CLOSED:
            t = dict(prev)
        elif not s or p["resolved"] not in s["d"]:
            if prev:
                out.append(prev)
            continue
        else:
            t = simulate_trade(s, s["d"].index(p["resolved"]), p["R"], target_pct)
            t.update(id=tid, symbol=p["symbol"], predicted=p["date"])
            if prev:
                for k, v in prev.items():
                    if k.startswith("alert_") or k in MY_FIELDS:
                        t[k] = v
        if s:
            t["last_close"], t["last_date"] = s["c"][-1], s["d"][-1]
        out.append(t)
    known = {t["id"] for t in out}
    out += [t for t in existing if t["id"] not in known]
    return sorted(out, key=lambda t: t.get("breakout_date", ""))


def merge_predictions(*lists):
    """Union by (date, symbol). A graded copy (hit/miss) wins over an open one."""
    rank = {"open": 0, "hit": 1, "miss": 1}
    out = {}
    for lst in lists:
        for p in lst or []:
            k = pred_id(p)
            if k not in out or rank.get(p.get("status"), 0) > rank.get(out[k].get("status"), 0):
                out[k] = p
    return sorted(out.values(), key=lambda p: (p["date"], p.get("time", "")))


def grade_predictions(preds, history):
    """A prediction made on day D hits if any close > R on D or the next 6 sessions."""
    by_symbol = {s["symbol"]: s for s in history.values()}
    for p in preds:
        if p["status"] != "open":
            continue
        s = by_symbol.get(p["symbol"])
        if not s:
            continue
        window = [(d, c) for d, c in zip(s["d"], s["c"]) if d >= p["date"]][:HORIZON]
        hit = next(((i, d, c) for i, (d, c) in enumerate(window) if c > p["R"]), None)
        if hit:
            p["status"], p["resolved"], p["sessions"], p["resolved_close"] = "hit", hit[1], hit[0] + 1, hit[2]
        elif len(window) >= HORIZON:
            p["status"], p["resolved"], p["sessions"], p["resolved_close"] = "miss", window[-1][0], HORIZON, window[-1][1]
    return preds


def prediction_stats(preds):
    hit = sum(p["status"] == "hit" for p in preds)
    miss = sum(p["status"] == "miss" for p in preds)
    opened = sum(p["status"] == "open" for p in preds)
    rate = f"{hit / (hit + miss) * 100:.0f}%" if hit + miss else "n/a"
    text = f"Tracked predictions: {hit} hit, {miss} missed, {opened} open  |  hit rate {rate}"
    bt = read_json(APP_DIR / "backtest.json")
    if bt:
        text += (f"  |  backtest (score >= {bt['min_score']:g}): {bt['setups']:.0f}% vs "
                 f"{bt['baseline']:.0f}% baseline for any stock near its 20-day high")
    else:
        text += "  |  run the backtest to see the baseline"
    return text


def trade_date(q):
    """IST date of a quote's last trade. Accepts epoch ms, epoch seconds or an ISO string."""
    raw = q.get("last_trade_time") or q.get("timestamp")
    if not raw:
        return None
    try:
        x = float(raw)
        return datetime.fromtimestamp(x / 1000 if x > 1e11 else x, IST).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        s = str(raw)
        return s[:10] if len(s) >= 10 and s[4] == "-" else None


# =========================================================================== Firebase account + sync

FIREBASE_CONFIG_PATH = Path(__file__).with_name("firebase_config.json")
ACCOUNT = None  # CloudAccount, created by the UI

AUTH_MESSAGES = {
    "EMAIL_EXISTS": "An account with this email already exists. Sign in instead.",
    "INVALID_LOGIN_CREDENTIALS": "Wrong email or password.",
    "INVALID_PASSWORD": "Wrong email or password.",
    "EMAIL_NOT_FOUND": "Wrong email or password.",
    "INVALID_EMAIL": "That email address doesn't look valid.",
    "MISSING_PASSWORD": "Enter a password.",
    "USER_DISABLED": "This account has been disabled.",
    "TOO_MANY_ATTEMPTS_TRY_LATER": "Too many attempts. Wait a few minutes and try again.",
    "OPERATION_NOT_ALLOWED": "Email/password sign-in isn't enabled in the Firebase project yet.",
    "CONFIGURATION_NOT_FOUND": "Firebase Authentication isn't set up in the Firebase project yet.",
    "TOKEN_EXPIRED": "Your session expired. Sign in again.",
    "INVALID_REFRESH_TOKEN": "Your session expired. Sign in again.",
}


class CloudError(Exception):
    def __init__(self, message, code=0):
        super().__init__(message)
        self.code = code


def http_json(method, url, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": APP_USER_AGENT}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            err = json.loads(raw).get("error", {})
        except ValueError:
            err = {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise CloudError(msg or f"HTTP {e.code}", e.code) from None
    except urllib.error.URLError as e:
        raise CloudError(f"Can't reach Firebase: {e.reason}") from None


def fs_encode(v):
    """Python value -> Firestore REST typed value."""
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [fs_encode(x) for x in v]}}
    if isinstance(v, dict):
        return {"mapValue": {"fields": {str(k): fs_encode(x) for k, x in v.items()}}}
    raise TypeError(f"Can't store {type(v).__name__} in Firestore")


def fs_decode(v):
    """Firestore REST typed value -> Python value."""
    (kind, val), = v.items()
    if kind == "integerValue":
        return int(val)
    if kind == "doubleValue":
        return float(val)
    if kind == "arrayValue":
        return [fs_decode(x) for x in val.get("values", [])]
    if kind == "mapValue":
        return {k: fs_decode(x) for k, x in val.get("fields", {}).items()}
    if kind == "nullValue":
        return None
    return val  # stringValue, booleanValue, timestampValue


# Shared Firebase project every copy of the app uses for accounts and sync. A Firebase Web API key only identifies
# the project (it is meant to ship inside apps); firestore.rules is what keeps each user's data private.
# A firebase_config.json next to the app overrides this, e.g. to point a copy at a different project.
BUILTIN_FIREBASE = {"apiKey": "AIzaSyCmua-RymNtjeZRtlsfCpW3hjpDRndQXu8", "projectId": "breakout-predictor"}


def load_firebase_config():
    cfg = read_json(FIREBASE_CONFIG_PATH, {}) or {}
    if cfg.get("apiKey") and cfg.get("projectId"):
        return cfg
    return dict(BUILTIN_FIREBASE)


class CloudAccount:
    """Firebase Auth (email/password) + Firestore storage at users/{uid} and users/{uid}/predictions/{date_symbol}.

    The session (refresh token) is kept in ~/.breakout_predictor/session.json so you stay signed in, the same way a
    browser keeps a login cookie. Upstox tokens and SMTP passwords are never sent to Firebase."""

    def __init__(self, notify=None):
        self.fb = load_firebase_config()
        self.notify = notify or (lambda *a: None)   # notify(kind, payload)
        self.uid = self.email = self.id_token = self.refresh_token = None
        self.expires_at = 0.0
        self.lock = threading.RLock()
        self.synced = {}                              # pred_id -> json of what the cloud has
        self.push_q = queue.Queue()
        threading.Thread(target=self._pusher, daemon=True).start()

    # ---- state
    @property
    def configured(self):
        return self.fb is not None

    @property
    def signed_in(self):
        return self.uid is not None

    def data_dir(self):
        return APP_DIR / "users" / self.uid if self.uid else APP_DIR

    # ---- auth
    def _auth(self, endpoint, body):
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:{endpoint}?key={self.fb['apiKey']}"
        try:
            return http_json("POST", url, body)
        except CloudError as e:
            key = str(e).split(" ")[0].split(":")[0]
            if key == "WEAK_PASSWORD":
                raise CloudError("Password must be at least 6 characters.", e.code) from None
            raise CloudError(AUTH_MESSAGES.get(key, str(e)), e.code) from None

    def _set_session(self, uid, email, id_token, refresh_token, expires_in):
        with self.lock:
            if uid != self.uid:
                self.synced = {}
            self.uid, self.email, self.id_token, self.refresh_token = uid, email, id_token, refresh_token
            self.expires_at = time.time() + int(expires_in or 3600) - 120
        write_json(APP_DIR / "session.json", {"uid": uid, "email": email, "refresh_token": refresh_token})

    def sign_up(self, email, password):
        r = self._auth("signUp", {"email": email, "password": password, "returnSecureToken": True})
        self._set_session(r["localId"], r["email"], r["idToken"], r["refreshToken"], r.get("expiresIn"))
        try:
            self._auth("sendOobCode", {"requestType": "VERIFY_EMAIL", "idToken": r["idToken"]})
        except CloudError:
            pass  # verification email is a courtesy, not required

    def sign_in(self, email, password):
        r = self._auth("signInWithPassword", {"email": email, "password": password, "returnSecureToken": True})
        self._set_session(r["localId"], r["email"], r["idToken"], r["refreshToken"], r.get("expiresIn"))

    def reset_password(self, email):
        self._auth("sendOobCode", {"requestType": "PASSWORD_RESET", "email": email})

    def restore(self):
        """Resume the saved session. Returns True if signed in."""
        s = read_json(APP_DIR / "session.json")
        if not (self.configured and s and s.get("refresh_token")):
            return False
        self.refresh_token, self.email = s["refresh_token"], s.get("email")
        try:
            self._refresh()
        except CloudError as e:
            if e.code in (400, 401, 403):
                self.sign_out()
            raise
        return True

    def _refresh(self):
        url = f"https://securetoken.googleapis.com/v1/token?key={self.fb['apiKey']}"
        try:
            r = http_json("POST", url, {"grant_type": "refresh_token", "refresh_token": self.refresh_token})
        except CloudError as e:
            raise CloudError(AUTH_MESSAGES.get(str(e).split(" ")[0], str(e)), e.code) from None
        self._set_session(r["user_id"], self.email, r["id_token"], r["refresh_token"], r.get("expires_in"))

    def sign_out(self):
        with self.lock:
            self.uid = self.email = self.id_token = self.refresh_token = None
            self.synced = {}
        try:
            (APP_DIR / "session.json").unlink()
        except OSError:
            pass

    def _token(self):
        with self.lock:
            if time.time() >= self.expires_at:
                self._refresh()
            return self.id_token

    # ---- Firestore
    def _root(self):
        return f"projects/{self.fb['projectId']}/databases/(default)/documents"

    def _fs(self, method, path, body=None):
        url = f"https://firestore.googleapis.com/v1/{path}"
        for attempt in (1, 2):
            try:
                return http_json(method, url, body, {"Authorization": f"Bearer {self._token()}"})
            except CloudError as e:
                if e.code == 401 and attempt == 1:
                    with self.lock:
                        self.expires_at = 0
                    continue
                if e.code == 403:
                    raise CloudError("Firestore refused access. Check the security rules in the Firebase console "
                                     "(see firestore.rules).", 403) from None
                if e.code == 404 and "database (default) does not exist" in str(e).lower():
                    raise CloudError("No Firestore database in this Firebase project yet. Create one in the "
                                     "Firebase console.", 404) from None
                raise

    ID_FN = {"predictions": pred_id, "trades": lambda t: t["id"]}

    def pull_collection(self, coll):
        out, token = [], ""
        while True:
            path = f"{self._root()}/users/{self.uid}/{coll}?pageSize=300"
            r = self._fs("GET", path + (f"&pageToken={urllib.parse.quote(token)}" if token else ""))
            for doc in r.get("documents", []):
                out.append({k: fs_decode(v) for k, v in doc.get("fields", {}).items()})
            token = r.get("nextPageToken")
            if not token:
                return out

    def pull_predictions(self):
        return self.pull_collection("predictions")

    def _commit(self, items, coll="predictions"):
        idf = self.ID_FN[coll]
        writes = [{"update": {"name": f"{self._root()}/users/{self.uid}/{coll}/{idf(x)}",
                              "fields": {k: fs_encode(v) for k, v in x.items()}}} for x in items]
        for i in range(0, len(writes), 400):
            self._fs("POST", f"{self._root()}:commit", {"writes": writes[i:i + 400]})

    def pull_settings(self):
        try:
            doc = self._fs("GET", f"{self._root()}/users/{self.uid}")
        except CloudError as e:
            if e.code == 404:
                return None
            raise
        s = doc.get("fields", {}).get("settings")
        return fs_decode(s) if s else None

    def push_settings(self, cfg):
        settings = {k: cfg[k] for k in DEFAULT_CONFIG if k in cfg}
        self._fs("PATCH", f"{self._root()}/users/{self.uid}?updateMask.fieldPaths=settings&updateMask.fieldPaths=email",
                 {"fields": {"settings": fs_encode(settings), "email": fs_encode(self.email or "")}})

    def _sync_collection(self, coll, merge_fn, extra=None):
        idf = self.ID_FN[coll]
        remote = self.pull_collection(coll)
        local = read_json(self.data_dir() / f"{coll}.json", []) or []
        merged = merge_fn(local, extra or [], remote)
        write_json(self.data_dir() / f"{coll}.json", merged)
        remote_by_id = {idf(x): json.dumps(x, sort_keys=True) for x in remote}
        with self.lock:
            self.synced[coll] = dict(remote_by_id)
        changed = [x for x in merged if remote_by_id.get(idf(x)) != json.dumps(x, sort_keys=True)]
        if changed:
            self._commit(changed, coll)
            with self.lock:
                for x in changed:
                    self.synced[coll][idf(x)] = json.dumps(x, sort_keys=True)
        return merged

    def sync(self, extra=None):
        """Two-way merge of predictions and buy-ins. Returns the merged predictions (also saved locally)."""
        merged = self._sync_collection("predictions", merge_predictions, extra)
        self._sync_collection("trades", merge_trades)
        self.notify("synced", len(merged))
        return merged

    def queue_push(self, items, coll="predictions"):
        idf = self.ID_FN[coll]
        with self.lock:
            done = self.synced.setdefault(coll, {})
            changed = [x for x in items if done.get(idf(x)) != json.dumps(x, sort_keys=True)]
        if changed:
            self.push_q.put((coll, changed))

    def _pusher(self):
        while True:
            coll, batch = self.push_q.get()
            if not self.signed_in:
                continue
            try:
                self._commit(batch, coll)
                idf = self.ID_FN[coll]
                with self.lock:
                    for x in batch:
                        self.synced.setdefault(coll, {})[idf(x)] = json.dumps(x, sort_keys=True)
                self.notify("synced", None)
            except Exception as e:  # noqa: BLE001 - stays dirty; retried on the next save or sync
                self.notify("sync_error", str(e))


# =========================================================================== email

def send_email(cfg, password, subject, html):
    recipients = [r.strip() for r in cfg["email_to"].split(",") if r.strip()]
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, cfg["smtp_user"], ", ".join(recipients)
    port, ctx = int(cfg["smtp_port"]), ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(cfg["smtp_host"], port, context=ctx, timeout=30) as s:
            s.login(cfg["smtp_user"], password)
            s.sendmail(cfg["smtp_user"], recipients, msg.as_string())
    else:
        with smtplib.SMTP(cfg["smtp_host"], port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(cfg["smtp_user"], password)
            s.sendmail(cfg["smtp_user"], recipients, msg.as_string())


def alert_html(rows, cfg, when, stats):
    td = "padding:6px 8px;border-bottom:1px solid #ddd;"
    body = "".join(
        f"<tr><td style='{td}font-weight:bold'>{r['symbol']}</td><td style='{td}color:#555'>{r['industry']}</td>"
        f"<td style='{td}'>&#8377;{r['ltp']:.2f}</td><td style='{td}font-weight:bold'>&#8377;{r['R']:.2f}</td>"
        f"<td style='{td}'>{r['dist']:.2f}%</td><td style='{td}font-weight:bold'>{r['score']:.0f}</td>"
        f"<td style='{td}color:#555'>{', '.join(r['reasons'])}</td></tr>" for r in rows)
    head = "".join(f"<th style='padding:6px 8px;border-bottom:2px solid #ccc;text-align:left'>{h}</th>" for h in
                   ["Stock", "Sector", "LTP", "Breakout above", "Distance", "Score", "Why"])
    return (f"<div style='font-family:Arial,sans-serif;color:#222'>"
            f"<h2 style='margin-bottom:4px'>Nifty 500: likely breakouts in the next {HORIZON} sessions</h2>"
            f"<p style='color:#666;margin-top:0'>{when.strftime('%d %b %Y, %I:%M %p')} IST | Upstox | "
            f"score &ge; {cfg['min_score']}, within {cfg['max_dist_pct']}% of the 20-day high | new setups only</p>"
            f"<table style='border-collapse:collapse;font-size:14px'><thead><tr style='background:#f4f6f8'>{head}"
            f"</tr></thead><tbody>{body}</tbody></table>"
            f"<p style='color:#666'>A prediction counts as a hit if the stock CLOSES above the breakout level "
            f"within {HORIZON} sessions. {stats}. These are probabilistic setups, not certainties.</p></div>")


def trade_alert_html(events, when, cfg):
    td = "padding:6px 8px;border-bottom:1px solid #ddd;"
    color = {"BUY-IN": "#1a5fb4", "TARGET HIT": "#0f7b3e", "STOPPED OUT": "#c0392b"}
    money = lambda v: f"&#8377;{v:,.2f}" if v else "&mdash;"  # noqa: E731
    body = "".join(
        f"<tr><td style='{td}font-weight:bold;color:{color[e['kind']]}'>{e['kind']}</td>"
        f"<td style='{td}font-weight:bold'>{e['symbol']}</td><td style='{td}'>{money(e['price'])}</td>"
        f"<td style='{td}'>{money(e.get('entry'))}</td><td style='{td}'>{money(e['R'])}</td>"
        f"<td style='{td}'>{money(e.get('stop'))}</td><td style='{td}'>{money(e.get('target'))}</td></tr>"
        for e in events)
    head = "".join(f"<th style='padding:6px 8px;border-bottom:2px solid #ccc;text-align:left'>{h}</th>" for h in
                   ["Signal", "Stock", "Price now", "Entry", "Breakout level", "Stop", "Target"])
    return (f"<div style='font-family:Arial,sans-serif;color:#222'>"
            f"<h2 style='margin-bottom:4px'>Breakout retest signals</h2>"
            f"<p style='color:#666;margin-top:0'>{when.strftime('%d %b %Y, %I:%M %p')} IST | Upstox live</p>"
            f"<table style='border-collapse:collapse;font-size:14px'><thead><tr style='background:#f4f6f8'>{head}"
            f"</tr></thead><tbody>{body}</tbody></table>"
            f"<p style='color:#666'><b>BUY-IN</b>: a predicted breakout came back to retest its old ceiling and has now "
            f"moved above the retest day's high. Stop is 1% below the retest low; target is "
            f"+{float(cfg.get('target_pct') or 10):g}% from entry. Probabilistic signals, not investment advice.</p></div>")


# =========================================================================== engine

class Engine(threading.Thread):
    """Background worker. Talks to the UI only through `events`."""

    def __init__(self, cfg, token, smtp_password, events, mode):
        super().__init__(daemon=True)
        self.cfg, self.smtp_password, self.events, self.mode = cfg, smtp_password, events, mode
        self.client = UpstoxClient(token)
        self.stop_event = threading.Event()
        self.prepared_for = None
        self.history = self.setups = None
        self.logged_sample = False
        self.universe = None
        self.weekly = None
        self.key_by_symbol = {}
        self.preview_for = None

    def log(self, msg):
        self.events.put(("log", f"[{now_ist():%H:%M:%S}] {msg}"))

    def status(self, msg):
        self.events.put(("status", msg))

    def stop(self):
        self.stop_event.set()

    def email_enabled(self):
        c = self.cfg
        return bool(c["smtp_host"] and c["smtp_user"] and c["email_to"] and self.smtp_password)

    def run(self):
        try:
            if self.mode == "dry":
                self.scan_once(live=False)
            elif self.mode == "backtest":
                self.prepare()
                weekly = self.weekly
                if weekly is None:
                    try:
                        weekly = build_weekly(self.client, self.universe, self.log,
                                              lambda d, t: self.events.put(("progress", d, t)), self.stop_event)
                    except (AuthError, InterruptedError):
                        raise
                    except Exception as e:  # noqa: BLE001 - backtest still runs without the 200W split
                        self.log(f"Weekly history unavailable, skipping the 200-week MA split: {e}")
                self.status("Running backtest...")
                report = backtest(self.history, self.cfg, self.stop_event, weekly)
                self.log(report)
                self.events.put(("backtest", report))
                self.events.put(("stats", prediction_stats(load_predictions())))
                self.status("Backtest done.")
            else:
                self.loop()
        except AuthError as e:
            self.log(str(e))
            self.events.put(("auth_error", str(e)))
        except InterruptedError:
            pass
        except Exception as e:  # noqa: BLE001 - surface anything unexpected in the UI
            self.log(f"Error: {e}")
            self.events.put(("error", str(e)))
        finally:
            self.events.put(("stopped", self.mode))

    def prepare(self):
        """Load history, grade old predictions, precompute today's setup components."""
        if self.prepared_for == today_str():
            return
        universe = self.universe = load_universe(self.cfg, self.log)
        progress = lambda d, t: self.events.put(("progress", d, t))  # noqa: E731
        self.history = build_history(self.client, universe, self.log, progress, self.stop_event)
        self.weekly = None
        if self.cfg.get("wma200_filter"):
            self.weekly = build_weekly(self.client, universe, self.log, progress, self.stop_event)
        preds = grade_predictions(load_predictions(), self.history)
        save_predictions(preds)
        trades = build_trades(preds, self.history, load_trades(), float(self.cfg.get("target_pct") or 10))
        save_trades(trades)
        self.key_by_symbol = {s["symbol"]: k for k, s in self.history.items()}
        self.events.put(("stats", prediction_stats(preds)))

        # Scores are computed each scan from the completed bars plus today's live bar (see scan_once).
        self.setups = {}
        for key, stock in self.history.items():
            if len(stock["c"]) < max(MIN_BARS, RS_LOOKBACK + 1):
                continue
            info = {"symbol": stock["symbol"], "industry": stock["industry"], "stock": stock}
            if self.weekly is not None:
                info["wma200"] = wma200_at(self.weekly.get(key), today_str())
            self.setups[key] = info
        self.prepared_for = today_str()
        self.log(f"Ready to score {len(self.setups)} stocks.")

    def loop(self):
        self.log("Live predictor started.")
        while not self.stop_event.is_set():
            t = now_ist()
            if t.weekday() < 5 and (8, 45) <= (t.hour, t.minute) <= MARKET_CLOSE:
                self.prepare()
            if is_market_open(t):
                try:
                    self.scan_once(live=True)
                except AuthError:
                    raise
                except Exception as e:  # noqa: BLE001 - keep going next minute
                    self.log(f"Scan failed, retrying next minute: {e}")
                wait = 60 - now_ist().second + 1
            else:
                if self.preview_for != today_str(t):
                    # Market closed: show setups for the next session from the last close (nothing emailed/logged).
                    self.log("Market is closed. Showing setups for the next session from the last close.")
                    try:
                        self.scan_once(live=False)
                    except AuthError:
                        raise
                    except Exception as e:  # noqa: BLE001 - keep the live loop alive
                        self.log(f"Preview scan failed: {e}")
                    self.preview_for = today_str(t)
                self.status(f"Market closed · showing setups for the next session from the last close · "
                            f"live checks start {next_open(t):%a %d %b, %H:%M} IST")
                wait = 30
            self.stop_event.wait(wait)
        self.log("Live predictor stopped.")

    def scan_once(self, live):
        self.prepare()
        t = now_ist()
        quotes = self.client.quotes(list(self.setups.keys()))
        prices = {}
        for tr in load_trades():
            k = self.key_by_symbol.get(tr["symbol"])
            if k and (quotes.get(k) or {}).get("last_price"):
                prices[tr["symbol"]] = float(quotes[k]["last_price"])
        self.events.put(("prices", prices))
        thr, max_dist, min_price = float(self.cfg["min_score"]), float(self.cfg["max_dist_pct"]), float(self.cfg["min_price"])

        # Today's bar from the quote (replacing a stored bar for today, if any), so the live score is computed
        # exactly like the backtest's: on the session's bar, with relative strength ranked across all stocks.
        live_bars, live_ret = {}, []
        for key, s in self.setups.items():
            q = quotes.get(key)
            if not q or not q.get("last_price"):
                continue
            stock, ltp = s["stock"], float(q["last_price"])
            ohlc = q.get("ohlc") or {}
            same_day = stock["d"][-1] == (trade_date(q) or today_str(t))
            keep = len(stock["c"]) - (1 if same_day else 0)
            bar = {"d": trade_date(q) or today_str(t), "o": float(ohlc.get("open") or ltp),
                   "h": max(float(ohlc.get("high") or ltp), ltp), "l": min(float(ohlc.get("low") or ltp), ltp),
                   "c": ltp, "v": float(q.get("volume") or 0)}
            live_bars[key] = (keep, bar)
            base_close = stock["c"][keep - RS_LOOKBACK] if keep >= RS_LOOKBACK else None
            if base_close:
                live_ret.append(ltp / base_close - 1)
        live_ret.sort()

        rows, stale = [], 0
        for key, s in self.setups.items():
            q = quotes.get(key)
            if not q or not q.get("last_price"):
                continue
            if not self.logged_sample:
                self.logged_sample = True
                self.log(f"Sample quote time fields: last_trade_time={q.get('last_trade_time')!r}, "
                         f"timestamp={q.get('timestamp')!r} -> trade date {trade_date(q)}")
            td = trade_date(q)
            if td and td != today_str(t):
                stale += 1
            ltp = float(q["last_price"])
            if ltp < min_price or key not in live_bars:
                continue
            keep, bar = live_bars[key]
            stock = s["stock"]
            if max(stock["h"][keep - 19:keep] + [bar["h"]]) > ltp * (1 + max_dist / 100):
                continue  # too far below the 20-day high: skip the full scoring
            S = compute_series({**stock, **{f: stock[f][:keep] + [bar[f]] for f in ("d", "o", "h", "l", "c", "v")}})
            tt = len(S["c"]) - 1
            comp = setup_components(S, tt, rs_percentile(live_ret, ret60(S, tt)))
            prox = proximity(comp["R"], ltp, max_dist)
            if not prox:
                continue
            s = {**s, **comp}
            reasons = s["reasons"]
            if self.weekly is not None:
                if not s.get("wma200") or ltp <= s["wma200"]:
                    continue  # 200-week MA filter: skip stocks below it or without 200 weeks of data
                reasons = reasons + ["above 200W MA"]
            score = s["base"] + prox[0]
            if score >= thr:
                rows.append({"key": key, "symbol": s["symbol"], "industry": s["industry"], "ltp": ltp,
                             "R": s["R"], "dist": prox[1], "score": score, "reasons": reasons})
        rows.sort(key=lambda r: r["score"], reverse=True)

        if live and stale > len(self.setups) / 2:
            self.status(f"{t:%H:%M:%S} - quotes are not from today (holiday?). Skipping.")
            return

        preds = load_predictions()
        open_symbols = {p["symbol"] for p in preds if p["status"] == "open"}
        fresh = [r for r in rows if r["symbol"] not in open_symbols]
        self.events.put(("rows", rows, {r["symbol"] for r in fresh},
                         {p["symbol"]: p["date"] for p in preds if p["status"] == "open"}))
        self.status(f"{t:%H:%M:%S} IST - {len(rows)} likely breakouts, {len(fresh)} new"
                    + ("" if live else " (dry run: nothing logged or emailed)"))
        if live:
            self.check_trades_live(quotes, t)
        if not live or not fresh:
            return

        for r in fresh:
            preds.append({"date": today_str(t), "time": f"{t:%H:%M}", "symbol": r["symbol"], "R": r["R"],
                          "price": r["ltp"], "score": round(r["score"], 1), "status": "open", "v": SCORE_VERSION})
        save_predictions(preds)
        stats = prediction_stats(preds)
        self.events.put(("stats", stats))
        self.log(f"New predictions: {', '.join(r['symbol'] for r in fresh)}")

        subject = f"Nifty 500: {len(fresh)} likely breakout(s) within {HORIZON} days - {t:%d %b %H:%M}"
        self.send_alert(subject, alert_html(fresh, self.cfg, t, stats), t)
        self.events.put(("notify", len(fresh)))

    def send_alert(self, subject, html, t):
        state_path = APP_DIR / f"emails_{today_str(t)}.json"
        state = read_json(state_path, {"sent": 0})
        if not self.email_enabled():
            self.log("Email not configured; shown here only.")
        elif state["sent"] >= int(self.cfg["max_emails_per_day"]):
            self.log("Daily email cap reached; shown here only.")
        else:
            try:
                send_email(self.cfg, self.smtp_password, subject, html)
                state["sent"] += 1
                write_json(state_path, state)
                self.log(f"Email sent to {self.cfg['email_to']}: {subject}")
            except Exception as e:  # noqa: BLE001
                self.log(f"Email failed: {e}")

    def check_trades_live(self, quotes, t):
        """Live buy-in triggers (retesting -> price above the retest day's high) and exits (target / stop).
        The daily rebuild each morning stays the record; these are the real-time alerts, sent once each."""
        trades = load_trades()
        events, today = [], today_str(t)
        for tr in trades:
            key = self.key_by_symbol.get(tr["symbol"])
            q = quotes.get(key) if key else None
            if not q or not q.get("last_price"):
                continue
            ltp = float(q["last_price"])
            if tr["status"] == "retesting" and tr.get("buy_above") and ltp > tr["buy_above"] \
                    and tr.get("alert_buy") != today:
                tr["alert_buy"], tr["alert_buy_price"], tr["alert_buy_time"] = today, ltp, f"{t:%H:%M}"
                target = ltp * (1 + float(self.cfg.get("target_pct") or 10) / 100)
                events.append({"kind": "BUY-IN", "symbol": tr["symbol"], "price": ltp, "R": tr["R"],
                               "stop": tr.get("stop"), "target": round(target, 2)})
            elif tr["status"] == "entered" and not tr.get("alert_exit"):
                if tr.get("stop") and ltp <= tr["stop"]:
                    tr["alert_exit"] = f"stop {today} {t:%H:%M}"
                    events.append({"kind": "STOPPED OUT", "symbol": tr["symbol"], "price": ltp, "R": tr["R"],
                                   "stop": tr["stop"], "target": tr.get("target"), "entry": tr.get("entry_price")})
                elif tr.get("target") and ltp >= tr["target"]:
                    tr["alert_exit"] = f"target {today} {t:%H:%M}"
                    events.append({"kind": "TARGET HIT", "symbol": tr["symbol"], "price": ltp, "R": tr["R"],
                                   "stop": tr.get("stop"), "target": tr["target"], "entry": tr.get("entry_price")})
        if not events:
            return
        save_trades(trades)
        self.events.put(("stats", None))
        self.log("Buy-in alerts: " + ", ".join(f"{e['kind']} {e['symbol']} @ ₹{e['price']:,.2f}" for e in events))
        kinds = sorted({e["kind"] for e in events})
        subject = (f"{' / '.join(kinds)}: " + ", ".join(e["symbol"] for e in events[:5])
                   + (" …" if len(events) > 5 else "") + f" - {t:%d %b %H:%M}")
        self.send_alert(subject, trade_alert_html(events, t, self.cfg), t)
        self.events.put(("notify", len(events)))


# =========================================================================== icon

def _png_bytes(n, rgba):
    import struct
    import zlib
    raw = b"".join(b"\x00" + bytes(rgba[y * n * 4:(y + 1) * n * 4]) for y in range(n))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _icon_rgba(n):
    """Rounded blue tile, dashed amber resistance line, white price line breaking through it."""
    out = bytearray(n * n * 4)
    rad = n * 0.22
    half = n / 2
    pts = [(0.17, 0.72), (0.34, 0.55), (0.48, 0.63), (0.66, 0.42), (0.83, 0.20)]
    pts = [(x * n, y * n) for x, y in pts]
    lw = max(1.3, n * 0.08)
    res_y, res_w, dash = 0.38 * n, max(1.0, n * 0.035), n * 0.09
    top, bot = (96, 165, 250), (30, 64, 175)

    def seg_dist(px, py, a, b):
        ax, ay = a
        bx, by = b
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5

    for y in range(n):
        for x in range(n):
            cx, cy = x + 0.5, y + 0.5
            qx, qy = abs(cx - half) - (half - rad), abs(cy - half) - (half - rad)
            sd = (max(qx, 0) ** 2 + max(qy, 0) ** 2) ** 0.5 + min(max(qx, qy), 0) - rad
            alpha = clamp(0.5 - sd)
            if alpha <= 0:
                continue
            g = cy / n
            col = [top[i] + (bot[i] - top[i]) * g for i in range(3)]
            if 0.12 * n < cx < 0.88 * n and int(cx / dash) % 2 == 0:
                a = clamp(res_w / 2 - abs(cy - res_y) + 0.5)
                col = [c + (v - c) * a * 0.95 for c, v in zip(col, (251, 191, 36))]
            d = min(seg_dist(cx, cy, pts[i], pts[i + 1]) for i in range(len(pts) - 1))
            d = min(d, ((cx - pts[-1][0]) ** 2 + (cy - pts[-1][1]) ** 2) ** 0.5 - lw * 0.45)
            a = clamp(lw / 2 - d + 0.5)
            col = [c + (255 - c) * a for c in col]
            i = (y * n + x) * 4
            out[i:i + 4] = bytes([int(col[0]), int(col[1]), int(col[2]), int(alpha * 255)])
    return out


def write_icon(path):
    import struct
    sizes = [256, 48, 32, 16]
    pngs = [_png_bytes(s, _icon_rgba(s)) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, data in zip(sizes, pngs):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(header + entries + b"".join(pngs))


# =========================================================================== UI

P = {"bg": "#0d1117", "surface": "#161b26", "surface2": "#1f2635", "border": "#272f40", "row_alt": "#1a2030",
     "text": "#e6e9ef", "muted": "#8b93a7", "accent": "#4f8cff", "accent_hover": "#6c9fff",
     "green": "#34d399", "amber": "#fbbf24", "red": "#f87171", "new_row": "#2a2616", "selected": "#24324d"}
FONT = "Segoe UI"


TRAY_CLASS = "BreakoutPredictorTray"


class Tray(threading.Thread):
    """Windows notification-area (system tray) icon with an Open / Close-to-tray / Exit menu.

    Runs its own Win32 message loop on a daemon thread and reports clicks through on_event("open" | "exit" |
    "toggle"), which the app reads on the Tk thread. Plain ctypes, no extra packages.
    """
    WM_TRAY = 0x0400 + 20
    CMD_OPEN, CMD_TOGGLE, CMD_EXIT = 1, 2, 3

    def __init__(self, icon_path, tooltip, on_event, is_checked):
        super().__init__(daemon=True)
        self.icon_path, self.tooltip, self.on_event, self.is_checked = str(icon_path), tooltip, on_event, is_checked
        self.hwnd = None
        self.ready = threading.Event()
        self.ok = False

    # ---- Win32 plumbing
    def _api(self):
        import ctypes
        from ctypes import wintypes as wt
        u, s, k = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32
        LRESULT = ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wt.UINT), ("lpfnWndProc", self.WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                        ("lpszClassName", wt.LPCWSTR)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                        ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                        ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                        ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON)]

        u.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u.DefWindowProcW.restype = LRESULT
        u.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
        u.CreateWindowExW.restype = wt.HWND
        u.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
        u.LoadImageW.restype = wt.HANDLE
        u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        u.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID]
        u.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
        u.CreatePopupMenu.restype = wt.HMENU
        u.DestroyMenu.argtypes = [wt.HMENU]
        u.SetForegroundWindow.argtypes = [wt.HWND]
        u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
        u.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
        u.SetMenuDefaultItem.argtypes = [wt.HMENU, wt.UINT, wt.UINT]
        s.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        k.GetModuleHandleW.restype = wt.HMODULE
        return ctypes, wt, u, s, k, WNDCLASSW, NOTIFYICONDATAW

    def run(self):
        try:
            self._run()
        except Exception:  # noqa: BLE001 - no tray: the app falls back to a normal close
            self.ok = False
            self.ready.set()

    def _run(self):
        ctypes, wt, u, s, k, WNDCLASSW, NOTIFYICONDATAW = self._api()
        hinst = k.GetModuleHandleW(None)
        taskbar_created = u.RegisterWindowMessageW("TaskbarCreated")

        def nid(flags):
            d = NOTIFYICONDATAW()
            d.cbSize, d.hWnd, d.uID, d.uFlags = ctypes.sizeof(NOTIFYICONDATAW), self.hwnd, 1, flags
            d.uCallbackMessage, d.hIcon, d.szTip = self.WM_TRAY, self.hicon, self.tooltip[:127]
            return d

        def add():
            return s.Shell_NotifyIconW(0, ctypes.byref(nid(0x1 | 0x2 | 0x4)))  # NIM_ADD: message | icon | tip

        def menu():
            m = u.CreatePopupMenu()
            u.AppendMenuW(m, 0, self.CMD_OPEN, "Open Breakout Predictor")
            u.AppendMenuW(m, 0x8 if self.is_checked() else 0, self.CMD_TOGGLE, "Keep running in tray when closed")
            u.AppendMenuW(m, 0x800, 0, None)  # separator
            u.AppendMenuW(m, 0, self.CMD_EXIT, "Exit")
            u.SetMenuDefaultItem(m, self.CMD_OPEN, 0)
            pt = wt.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            u.SetForegroundWindow(self.hwnd)  # lets the menu close when you click elsewhere
            cmd = u.TrackPopupMenu(m, 0x100 | 0x2, pt.x, pt.y, 0, self.hwnd, None)  # RETURNCMD | RIGHTBUTTON
            u.PostMessageW(self.hwnd, 0, 0, 0)
            u.DestroyMenu(m)
            if cmd:
                self.on_event({self.CMD_OPEN: "open", self.CMD_TOGGLE: "toggle", self.CMD_EXIT: "exit"}[cmd])

        def proc(hwnd, msg, wparam, lparam):
            if msg == self.WM_TRAY:
                if lparam in (0x0202, 0x0203):  # left click / double click
                    self.on_event("open")
                elif lparam == 0x0205:  # right button up
                    menu()
                return 0
            if msg == taskbar_created:  # Explorer restarted: put the icon back
                add()
                return 0
            if msg == 0x0002:  # WM_DESTROY
                s.Shell_NotifyIconW(2, ctypes.byref(nid(0)))  # NIM_DELETE
                u.PostQuitMessage(0)
                return 0
            return u.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = self.WNDPROC(proc)  # keep a reference, or Windows calls freed memory
        wc = WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst, lpszClassName=TRAY_CLASS)
        u.RegisterClassW(ctypes.byref(wc))
        self.hwnd = u.CreateWindowExW(0, TRAY_CLASS, "Breakout Predictor", 0, 0, 0, 0, 0, None, None, hinst, None)
        self.hicon = u.LoadImageW(None, self.icon_path, 1, 0, 0, 0x10 | 0x40)  # IMAGE_ICON, FROMFILE | DEFAULTSIZE
        self.ok = bool(self.hwnd) and bool(add())
        self.ready.set()
        msg = wt.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

    # ---- called from the Tk thread
    def stop(self):
        if self.hwnd:
            import ctypes
            ctypes.windll.user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE -> WM_DESTROY on the tray thread

    @staticmethod
    def show_running_instance():
        """If the app is already running (maybe hidden in the tray), open that window and return True."""
        try:
            import ctypes
            u = ctypes.windll.user32
            u.FindWindowW.restype = ctypes.c_void_p
            hwnd = u.FindWindowW(TRAY_CLASS, None)
            if hwnd:
                u.AllowSetForegroundWindow(-1)
                u.PostMessageW(ctypes.c_void_p(hwnd), Tray.WM_TRAY, 0, 0x0202)
                return True
        except Exception:  # noqa: BLE001 - not Windows
            pass
        return False


def windows_setup():
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Local.BreakoutPredictor")
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - not Windows, or older Windows
        pass


class App(tk.Tk):
    TITLE = "Breakout Predictor"
    COLUMNS = [("symbol", "STOCK", 110, "w"), ("industry", "SECTOR", 170, "w"), ("ltp", "LTP", 90, "e"),
               ("R", "BREAKOUT LEVEL", 135, "e"), ("dist", "DISTANCE", 85, "e"), ("score", "SCORE", 120, "w"),
               ("why", "WHY", 300, "w"), ("since", "PREDICTED", 95, "e")]

    def __init__(self):
        windows_setup()
        super().__init__()
        self.title(self.TITLE)
        self.k = max(1.0, self.winfo_fpixels("1i") / 96)  # display scaling (1.25 at 125%)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(int(1380 * self.k), int(sw * 0.94)), min(int(880 * self.k), int(sh * 0.88))
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 3)}")
        self.minsize(min(int(1100 * self.k), w), min(int(680 * self.k), h))
        self.configure(bg=P["bg"])
        icon = APP_DIR / "icon.ico"
        try:
            if not icon.exists():
                write_icon(icon)
            self.iconbitmap(default=str(icon))
        except Exception:  # noqa: BLE001 - icon is cosmetic
            pass
        self.cfg = load_config()
        self.events = queue.Queue()
        self.engine = None
        self.mode = None
        self.vars = {}
        self.row_count = None
        self.live_prices = {}
        self.smtp_pw_var = tk.StringVar(value=os.environ.get("HHLL_SMTP_PASSWORD", ""))
        global ACCOUNT
        ACCOUNT = CloudAccount(notify=lambda kind, payload: self.events.put(("cloud", kind, payload)))
        self._style()
        self._build()
        self._refresh_stats()
        self._set_pill("idle")
        self._update_account_ui()
        self.after(200, self._drain_events)
        if self.cfg.pop("_score_migrated", False):
            self._log(f"The setup score was rebuilt (v{SCORE_VERSION}): relative strength and momentum replace squeeze "
                      f"and resistance tests. Min setup score reset to {DEFAULT_CONFIG['min_score']:g}.")
        if ACCOUNT.configured and (APP_DIR / "session.json").exists():
            self.sync_label.configure(text="Signing in…")
            def resume():
                if ACCOUNT.restore():
                    ACCOUNT.sync()
                else:
                    raise CloudError("Saved sign-in not found. Sign in again.")
            self._cloud_task(resume, "restored")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<F1>", lambda e: self.open_help())
        self.tray = None
        self.tray_hint_shown = False
        if os.name == "nt":
            tray = Tray(icon, self.TITLE, lambda what: self.events.put(("tray", what)),
                        lambda: bool(self.cfg.get("close_to_tray", True)))
            tray.start()
            if tray.ready.wait(3) and tray.ok:
                self.tray = tray

    # ------------------------------------------------------------------ styling
    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=P["bg"], foreground=P["text"], font=(FONT, 10),
                    bordercolor=P["border"], focuscolor=P["accent"], troughcolor=P["surface2"])
        s.configure("Bg.TFrame", background=P["bg"])
        s.configure("Card.TFrame", background=P["surface"])
        s.configure("Card.TLabel", background=P["surface"], foreground=P["text"])
        s.configure("Field.TLabel", background=P["surface"], foreground=P["muted"], font=(FONT, 9))
        s.configure("Hint.TLabel", background=P["surface"], foreground=P["muted"], font=(FONT, 9))
        s.configure("CardTitle.TLabel", background=P["surface"], foreground=P["text"], font=(FONT, 11, "bold"))
        s.configure("H1.TLabel", background=P["bg"], foreground=P["text"], font=(FONT, 20, "bold"))
        s.configure("Sub.TLabel", background=P["bg"], foreground=P["muted"], font=(FONT, 10))
        s.configure("Status.TLabel", background=P["bg"], foreground=P["muted"], font=(FONT, 9))
        s.configure("StatValue.TLabel", background=P["surface"], foreground=P["text"], font=(FONT, 22, "bold"))
        s.configure("StatLabel.TLabel", background=P["surface"], foreground=P["muted"], font=(FONT, 9, "bold"))
        s.configure("StatHint.TLabel", background=P["surface"], foreground=P["muted"], font=(FONT, 9))

        s.configure("TEntry", fieldbackground=P["surface2"], foreground=P["text"], insertcolor=P["text"],
                    bordercolor=P["border"], lightcolor=P["surface2"], darkcolor=P["surface2"], padding=7)
        s.map("TEntry", bordercolor=[("focus", P["accent"])], lightcolor=[("focus", P["accent"])],
              darkcolor=[("focus", P["accent"])])

        for name, bg, hover, fg, bold in [("Accent", P["accent"], P["accent_hover"], "#ffffff", True),
                                          ("Ghost", P["surface2"], P["border"], P["text"], False),
                                          ("Danger", P["surface2"], "#3a2226", P["red"], False)]:
            s.configure(f"{name}.TButton", background=bg, foreground=fg, bordercolor=bg, lightcolor=bg,
                        darkcolor=bg, padding=(14, 9), font=(FONT, 10, "bold" if bold else "normal"),
                        focuscolor=bg, anchor="center")
            s.map(f"{name}.TButton",
                  background=[("disabled", P["surface2"]), ("pressed", bg), ("active", hover)],
                  foreground=[("disabled", "#4b5366")],
                  bordercolor=[("disabled", P["surface2"]), ("active", hover)],
                  lightcolor=[("disabled", P["surface2"]), ("active", hover)],
                  darkcolor=[("disabled", P["surface2"]), ("active", hover)])

        for name, bg, fg, hover in [("Seg", P["surface"], P["muted"], P["surface2"]),
                                    ("SegOn", P["surface2"], P["text"], P["surface2"])]:
            s.configure(f"{name}.TButton", background=bg, foreground=fg, bordercolor=P["border"] if name == "SegOn" else bg,
                        lightcolor=bg, darkcolor=bg, padding=(12, 5), font=(FONT, 9, "bold"), focuscolor=bg)
            s.map(f"{name}.TButton", background=[("active", hover)], lightcolor=[("active", hover)],
                  darkcolor=[("active", hover)], foreground=[("active", P["text"])])
        s.configure("Card.TCheckbutton", background=P["surface"], foreground=P["text"], focuscolor=P["surface"],
                    indicatorbackground=P["surface2"], indicatorforeground=P["accent"], indicatormargin=(0, 0, 8, 0))
        s.map("Card.TCheckbutton", background=[("active", P["surface"])],
              indicatorbackground=[("selected", P["accent"]), ("active", P["surface2"])],
              indicatorforeground=[("selected", "#ffffff")])

        s.configure("Treeview", background=P["surface"], fieldbackground=P["surface"], foreground=P["text"],
                    rowheight=36, borderwidth=0, font=(FONT, 10))
        s.map("Treeview", background=[("selected", P["selected"])], foreground=[("selected", P["text"])])
        s.configure("Treeview.Heading", background=P["surface"], foreground=P["muted"], font=(FONT, 8, "bold"),
                    borderwidth=0, relief="flat", padding=(10, 10), bordercolor=P["surface"],
                    lightcolor=P["surface"], darkcolor=P["surface"])
        s.map("Treeview.Heading", background=[("active", P["surface"])])
        s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        s.configure("Thin.Horizontal.TProgressbar", troughcolor=P["bg"], background=P["accent"],
                    bordercolor=P["bg"], lightcolor=P["accent"], darkcolor=P["accent"], thickness=3)
        s.configure("Vertical.TScrollbar", background=P["surface2"], troughcolor=P["surface"],
                    bordercolor=P["surface"], arrowcolor=P["muted"], lightcolor=P["surface2"],
                    darkcolor=P["surface2"], gripcount=0)
        s.map("Vertical.TScrollbar", background=[("active", P["border"])])

    def _card(self, parent, title=None, subtitle=None, padding=18):
        outer = tk.Frame(parent, bg=P["surface"], highlightthickness=1,
                         highlightbackground=P["border"], highlightcolor=P["border"])
        inner = ttk.Frame(outer, style="Card.TFrame", padding=padding)
        inner.pack(fill="both", expand=True)
        if title:
            head = ttk.Frame(inner, style="Card.TFrame")
            head.pack(fill="x", pady=(0, 12))
            ttk.Label(head, text=title, style="CardTitle.TLabel").pack(side="left")
            if subtitle:
                ttk.Label(head, text=subtitle, style="Hint.TLabel").pack(side="right")
        return outer, inner

    def _field(self, parent, label, key=None, var=None, show=None, width=30):
        ttk.Label(parent, text=label, style="Field.TLabel").pack(anchor="w", pady=(0, 4))
        if var is None:
            var = self.vars[key] = tk.StringVar(value=str(self.cfg[key]))
        ttk.Entry(parent, textvariable=var, show=show or "", width=width).pack(fill="x", pady=(0, 12))
        return var

    # ------------------------------------------------------------------ layout
    def _build(self):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(2, weight=1)

        header = ttk.Frame(self, style="Bg.TFrame", padding=(28, 22, 28, 10))
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        titles = ttk.Frame(header, style="Bg.TFrame")
        titles.pack(side="left")
        ttk.Label(titles, text="Breakout Predictor", style="H1.TLabel").pack(anchor="w")
        ttk.Label(titles, text=f"Nifty 500  ·  setups likely to close above their 20-day high within "
                               f"{HORIZON} sessions  ·  Upstox live", style="Sub.TLabel").pack(anchor="w")

        self.help_btn = ttk.Button(header, text="?", style="Ghost.TButton", width=2, command=self.open_help)
        self.help_btn.pack(side="right", padx=(10, 0))
        self.pill = tk.Frame(header, bg=P["surface2"], padx=14, pady=7)
        self.pill.pack(side="right")
        self.email_hint = ttk.Button(header, style="Ghost.TButton", command=self.open_email_dialog)
        self.email_hint.pack(side="right", padx=(0, 10))
        self.account_btn = ttk.Button(header, style="Ghost.TButton", command=self.open_account_dialog)
        self.account_btn.pack(side="right", padx=(0, 10))
        self.sync_label = ttk.Label(header, style="Status.TLabel")
        self.sync_label.pack(side="right", padx=(0, 12))
        self.pill_dot = tk.Canvas(self.pill, width=10, height=10, bg=P["surface2"], highlightthickness=0)
        self.pill_dot.pack(side="left", padx=(0, 8))
        self.pill_text = tk.Label(self.pill, bg=P["surface2"], fg=P["text"], font=(FONT, 9, "bold"))
        self.pill_text.pack(side="left")

        self.progress = ttk.Progressbar(self, style="Thin.Horizontal.TProgressbar", mode="determinate")
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", padx=28)

        self._build_sidebar()
        self._build_main()
        self._update_email_hint()

    def _build_sidebar(self):
        side = ttk.Frame(self, style="Bg.TFrame", padding=(28, 16, 10, 24))
        side.grid(row=2, column=0, sticky="ns")

        outer, c = self._card(side, "Connection")
        outer.pack(fill="x", pady=(0, 14))
        self.token_var = self._field(c, "Upstox access token",
                                     var=tk.StringVar(value=os.environ.get("UPSTOX_ACCESS_TOKEN", "")), show="•")

        outer, c = self._card(side, "Prediction rules")
        outer.pack(fill="x", pady=(0, 14))
        grid = ttk.Frame(c, style="Card.TFrame")
        grid.pack(fill="x")
        for i, (k, label) in enumerate([("min_score", "Min setup score"), ("max_dist_pct", "Max % below 20D high"),
                                        ("min_price", "Min price (₹)"), ("target_pct", "Buy-in profit target %")]):
            ttk.Label(grid, text=label, style="Card.TLabel").grid(row=i, column=0, sticky="w", pady=5)
            self.vars[k] = tk.StringVar(value=str(self.cfg[k]))
            ttk.Entry(grid, textvariable=self.vars[k], width=8, justify="right").grid(row=i, column=1, sticky="e", pady=5)
        grid.columnconfigure(0, weight=1)
        self.wma_var = tk.BooleanVar(value=bool(self.cfg.get("wma200_filter")))
        ttk.Checkbutton(c, text="Only stocks above 200-week MA", variable=self.wma_var,
                        style="Card.TCheckbutton").pack(anchor="w", pady=(10, 0))

        outer, c = self._card(side, "Actions")
        outer.pack(fill="x")
        self.main_btn = ttk.Button(c, style="Accent.TButton")
        self.main_btn.pack(fill="x", pady=(0, 8))
        self.buttons = {}
        for name, text, cmd in [("dry", "Scan now  (no email)", self.dry_run),
                                ("backtest", "Run backtest", self.run_backtest)]:
            self.buttons[name] = ttk.Button(c, text=text, style="Ghost.TButton", command=cmd)
            self.buttons[name].pack(fill="x", pady=(0, 8))
        self._main_btn_idle()

    def _build_main(self):
        main = ttk.Frame(self, style="Bg.TFrame", padding=(10, 16, 28, 24))
        main.grid(row=2, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        stats = ttk.Frame(main, style="Bg.TFrame")
        stats.grid(row=0, column=0, sticky="ew")
        self.stat = {}
        for i, (key, label) in enumerate([("now", "LIKELY BREAKOUTS"), ("buyin", "BUY-IN WIN RATE"),
                                          ("hit", "TRACKED HIT RATE"), ("lift", "BACKTEST LIFT")]):
            stats.columnconfigure(i, weight=1, uniform="stat")
            outer, c = self._card(stats, padding=(18, 14))
            outer.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 7, 0 if i == 3 else 7))
            ttk.Label(c, text=label, style="StatLabel.TLabel").pack(anchor="w")
            value = ttk.Label(c, text="—", style="StatValue.TLabel")
            value.pack(anchor="w", pady=(4, 0))
            hint = ttk.Label(c, text="", style="StatHint.TLabel")
            hint.pack(anchor="w")
            self.stat[key] = (value, hint)

        self.status_var = tk.StringVar(value="Paste your Upstox token, then Scan now or Start live predictor.")
        ttk.Label(main, textvariable=self.status_var, style="Status.TLabel").grid(row=1, column=0, sticky="w", pady=(12, 8))

        outer, c = self._card(main, padding=(18, 14))
        outer.grid(row=2, column=0, sticky="nsew")
        head = ttk.Frame(c, style="Card.TFrame")
        head.pack(fill="x", pady=(0, 10))
        self.tab_btns = {}
        for name, text in [("live", "Live setups"), ("buyins", "Buy-ins"), ("track", "Track record")]:
            self.tab_btns[name] = ttk.Button(head, text=text, style="Seg.TButton", width=-4,
                                             command=lambda n=name: self._show_tab(n))
            self.tab_btns[name].pack(side="left", padx=(0, 6))
        self.tab_hint = ttk.Label(head, style="Hint.TLabel")
        self.tab_hint.pack(side="right")

        holder = ttk.Frame(c, style="Card.TFrame")
        holder.pack(fill="both", expand=True)
        self.tabs = {n: ttk.Frame(holder, style="Card.TFrame") for n in ("live", "buyins", "track")}
        self._build_track(self.tabs["track"])
        self._build_buyins(self.tabs["buyins"])
        tbl = self.tabs["live"]
        self.tree = ttk.Treeview(tbl, columns=[col[0] for col in self.COLUMNS], show="headings", selectmode="browse")
        for key, title, width, anchor in self.COLUMNS:
            self.tree.heading(key, text=title, anchor=anchor, command=lambda k=key: self._sort_click(self.tree, k))
            self.tree.column(key, width=int(width * self.k), anchor=anchor, stretch=key in ("why", "industry"))
        self.tree.tag_configure("alt", background=P["row_alt"])
        self.tree.tag_configure("new", background=P["new_row"])
        sb = ttk.Scrollbar(tbl, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.empty_label = tk.Label(tbl, text="No predictions yet.\nRun a scan to see stocks coiling under resistance.",
                                    bg=P["surface"], fg=P["muted"], font=(FONT, 10), justify="center")
        self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        self._show_tab("live")

        outer, c = self._card(main, padding=(18, 12))
        outer.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        head = ttk.Frame(c, style="Card.TFrame")
        head.pack(fill="x", pady=(0, 8))
        ttk.Label(head, text="Activity", style="CardTitle.TLabel").pack(side="left")
        self.copy_btn = ttk.Button(head, text="⧉  Copy", style="Ghost.TButton", command=self._copy_log)
        self.copy_btn.pack(side="right")
        self.wrap_var = tk.BooleanVar(value=True)
        self.wrap_btn = ttk.Button(head, text="↵  Wrap: on", style="Ghost.TButton", command=self._toggle_wrap)
        self.wrap_btn.pack(side="right", padx=(0, 8))
        ttk.Button(head, text="Clear", style="Ghost.TButton", command=self._clear_log).pack(side="right", padx=(0, 8))

        body = ttk.Frame(c, style="Card.TFrame")
        body.pack(fill="x")
        self.log_box = tk.Text(body, height=7, state="disabled", font=("Consolas", 9), bg=P["surface"],
                               fg=P["muted"], relief="flat", borderwidth=0, highlightthickness=0, wrap="word",
                               selectbackground=P["selected"], selectforeground=P["text"], cursor="xterm")
        ysb = ttk.Scrollbar(body, orient="vertical", command=self.log_box.yview)
        self.log_xsb = ttk.Scrollbar(c, orient="horizontal", command=self.log_box.xview)
        self.log_box.configure(yscrollcommand=ysb.set, xscrollcommand=self.log_xsb.set)
        ysb.pack(side="right", fill="y")
        self.log_box.pack(side="left", fill="x", expand=True)
        # Read-only but still selectable / Ctrl+C-able.
        self.log_box.bind("<Control-a>", lambda e: (self.log_box.tag_add("sel", "1.0", "end"), "break")[1])

    TRACK_COLUMNS = [("date", "PREDICTED", 145, "w"), ("symbol", "STOCK", 110, "w"), ("score", "SCORE", 65, "e"),
                     ("entry", "PRICE THEN", 100, "e"), ("R", "BREAKOUT LEVEL", 125, "e"), ("status", "RESULT", 95, "w"),
                     ("resolved", "RESOLVED ON", 105, "e"), ("sessions", "SESSIONS", 80, "e"), ("move", "MOVE", 85, "e")]

    def _build_track(self, parent):
        bar = ttk.Frame(parent, style="Card.TFrame")
        bar.pack(fill="x", pady=(0, 8))
        self.track_filter = "all"
        self.filter_btns = {}
        for name, text in [("all", "All"), ("hit", "Hits"), ("miss", "Misses"), ("open", "Open")]:
            self.filter_btns[name] = ttk.Button(bar, text=text, style="Seg.TButton", width=-4,
                                                command=lambda n=name: self._set_track_filter(n))
            self.filter_btns[name].pack(side="left", padx=(0, 6))
        self.track_summary = ttk.Label(bar, style="Hint.TLabel")
        self.track_summary.pack(side="right")

        box = ttk.Frame(parent, style="Card.TFrame")
        box.pack(fill="both", expand=True)
        self.track = ttk.Treeview(box, columns=[col[0] for col in self.TRACK_COLUMNS], show="headings", selectmode="browse")
        for key, title, width, anchor in self.TRACK_COLUMNS:
            self.track.heading(key, text=title, anchor=anchor,
                               command=lambda k=key: self._sort_click(self.track, k))
            self.track.column(key, width=int(width * self.k), anchor=anchor, stretch=key == "symbol")
        self.track.tag_configure("hit", foreground=P["green"])
        self.track.tag_configure("miss", foreground=P["red"])
        self.track.tag_configure("open", foreground=P["muted"])
        self.track.tag_configure("pos", foreground=P["green"])
        self.track.tag_configure("neg", foreground=P["red"])
        sb = ttk.Scrollbar(box, orient="vertical", command=self.track.yview)
        self.track.configure(yscrollcommand=sb.set)
        self.track.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.track_empty = tk.Label(box, bg=P["surface"], fg=P["muted"], font=(FONT, 10), justify="center",
                                    text="No live predictions recorded yet.\nThey are logged while the live "
                                         "predictor runs during market hours,\nthen graded after up to 7 sessions.")
        self._set_track_filter("all")

    BUYIN_COLUMNS = [("breakout", "BREAKOUT", 90, "w"), ("symbol", "STOCK", 100, "w"), ("R", "LEVEL", 85, "e"),
                     ("entry", "APP BUY-IN", 135, "e"), ("stop", "STOP", 80, "e"), ("target", "TARGET", 85, "e"),
                     ("status", "STATUS", 150, "w"), ("app_pnl", "APP P&L", 90, "e"), ("my_buy", "MY BUY", 135, "e"),
                     ("my_pnl", "MY P&L", 90, "e")]
    BUYIN_FILTERS = {"all": None, "active": ("waiting_retest", "retesting", "entered"), "win": ("win",),
                     "loss": ("loss", "expired"), "none": ("failed", "no_retest", "no_entry")}

    def _build_buyins(self, parent):
        bar = ttk.Frame(parent, style="Card.TFrame")
        bar.pack(fill="x", pady=(0, 8))
        self.buyin_filter = "all"
        self.buyin_btns = {}
        for name, text in [("all", "All"), ("active", "Active"), ("win", "Target hit"), ("loss", "Stopped / expired"),
                           ("none", "No entry")]:
            self.buyin_btns[name] = ttk.Button(bar, text=text, style="Seg.TButton", width=-4,
                                               command=lambda n=name: self._set_buyin_filter(n))
            self.buyin_btns[name].pack(side="left", padx=(0, 6))
        # The summary sits in the tab header row (where the other tabs show their hint) to leave room here.
        self.buyin_summary = ttk.Label(self.tab_hint.master, style="Hint.TLabel")
        ttk.Button(bar, text="✎  My buy / sell", style="Ghost.TButton",
                   command=self.open_my_trade_dialog).pack(side="right")
        self.pnl_mode = self.cfg.get("pnl_mode", "pct")
        self.amount_var = tk.StringVar(value=f"{float(self.cfg.get('trade_amount') or 100000):,.0f}")
        self.amount_entry = ttk.Entry(bar, textvariable=self.amount_var, width=10, justify="right")
        self.amount_entry.pack(side="right", padx=(0, 16))
        self.amount_entry.bind("<Return>", lambda e: self._set_trade_amount())
        self.amount_entry.bind("<FocusOut>", lambda e: self._set_trade_amount())
        self.amount_label = ttk.Label(bar, text="per trade ₹", style="Hint.TLabel")
        self.amount_label.pack(side="right", padx=(0, 6))
        self.pnl_btns = {}
        for name, text in [("amt", "₹"), ("pct", "%")]:
            self.pnl_btns[name] = ttk.Button(bar, text=text, style="Seg.TButton", width=3,
                                             command=lambda n=name: self._set_pnl_mode(n))
            self.pnl_btns[name].pack(side="right", padx=(0, 4))
        ttk.Label(bar, text="P&L in", style="Hint.TLabel").pack(side="right", padx=(0, 6))
        box = ttk.Frame(parent, style="Card.TFrame")
        box.pack(fill="both", expand=True)
        self.buyins = ttk.Treeview(box, columns=[col[0] for col in self.BUYIN_COLUMNS], show="headings",
                                   selectmode="browse")
        for key, title, width, anchor in self.BUYIN_COLUMNS:
            self.buyins.heading(key, text=title, anchor=anchor,
                                command=lambda k=key: self._sort_click(self.buyins, k))
            self.buyins.column(key, width=int(width * self.k), anchor=anchor, stretch=key == "status")
        for tag, color in [("win", P["green"]), ("loss", P["red"]), ("expired", P["amber"]), ("entered", P["accent"]),
                           ("retesting", P["amber"]), ("waiting_retest", P["text"])]:
            self.buyins.tag_configure(tag, foreground=color)
        for tag in ("failed", "no_retest", "no_entry"):
            self.buyins.tag_configure(tag, foreground=P["muted"])
        # Rows with a P&L are coloured by its sign instead (Tk can't colour single cells).
        self.buyins.tag_configure("pos", foreground=P["green"])
        self.buyins.tag_configure("neg", foreground=P["red"])
        sb = ttk.Scrollbar(box, orient="vertical", command=self.buyins.yview)
        self.buyins.configure(yscrollcommand=sb.set)
        self.buyins.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.buyins.bind("<Double-1>", lambda e: self.open_my_trade_dialog())
        self.buyin_empty = tk.Label(box, bg=P["surface"], fg=P["muted"], font=(FONT, 10), justify="center",
                                    text="No buy-ins yet.\nWhen a predicted stock breaks out it appears here, "
                                         "then moves to Retesting\nand In trade once it bounces above the "
                                         "retest day's high.")
        self._set_buyin_filter("all")

    def _set_pnl_mode(self, mode):
        self.pnl_mode = self.cfg["pnl_mode"] = mode
        save_config(self.cfg)
        self._refresh_buyins()

    def _set_trade_amount(self):
        try:
            amount = float(self.amount_var.get().replace(",", "").replace("₹", "").strip())
            if amount <= 0:
                raise ValueError
        except ValueError:
            amount = float(self.cfg.get("trade_amount") or 100000)
        self.amount_var.set(f"{amount:,.0f}")
        if amount != self.cfg.get("trade_amount"):
            self.cfg["trade_amount"] = amount
            save_config(self.cfg)
            self._refresh_buyins()

    # ------------------------------------------------------------------ column sorting
    TEXT_COLUMNS = {"symbol", "industry", "why", "status"}  # first click sorts these A→Z, the rest high→low

    def _columns_of(self, tree):
        for name, cols in (("tree", self.COLUMNS), ("buyins", self.BUYIN_COLUMNS), ("track", self.TRACK_COLUMNS)):
            if getattr(self, name, None) is tree:
                return cols
        return []

    def _sort_click(self, tree, col):
        if not hasattr(self, "sort_state"):
            self.sort_state = {}
        cur = self.sort_state.get(str(tree))
        desc = (not cur[1]) if cur and cur[0] == col else col not in self.TEXT_COLUMNS
        self.sort_state[str(tree)] = (col, desc)
        self._apply_sort(tree)

    @staticmethod
    def _sort_value(text):
        """Sort key from a cell's display text: dates, then numbers (₹, %, commas, • ignored), then text."""
        s = str(text).strip()
        if s in ("", "—", "-"):
            return None
        if s == "new":  # live 'PREDICTED' column: predicted this scan
            return (0, float("inf"))
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return (0, datetime.strptime(s, fmt).timestamp())
            except ValueError:
                pass
        try:
            d = datetime.strptime(f"{now_ist().year} {s}", "%Y %b %d")
            return (0, d.timestamp())
        except ValueError:
            pass
        m = re.match(r"^([+\-−]?)₹?([\d,]+(?:\.\d+)?)", s)
        if m:
            value = float(m.group(2).replace(",", ""))
            return (0, -value if m.group(1) in ("-", "−") else value)
        return (1, s.lower())

    def _apply_sort(self, tree):
        state = getattr(self, "sort_state", {}).get(str(tree))
        for key, title, _, _ in self._columns_of(tree):
            arrow = ("  ▼" if state[1] else "  ▲") if state and state[0] == key else ""
            tree.heading(key, text=title + arrow)
        if not state:
            return
        col, desc = state
        keyed = [(self._sort_value(tree.set(iid, col)), iid) for iid in tree.get_children("")]
        present = sorted([k for k in keyed if k[0] is not None], key=lambda k: k[0], reverse=desc)
        missing = [k for k in keyed if k[0] is None]  # blanks always go last
        for i, (_, iid) in enumerate(present + missing):
            tree.move(iid, "", i)
        if tree is self.tree:  # re-stripe the live table, keeping 'new' highlights
            for i, iid in enumerate(tree.get_children("")):
                if "new" not in tree.item(iid, "tags"):
                    tree.item(iid, tags=("alt",) if i % 2 else ())

    def _set_buyin_filter(self, name):
        self.buyin_filter = name
        for n, b in self.buyin_btns.items():
            b.configure(style="SegOn.TButton" if n == name else "Seg.TButton")
        self._refresh_buyins()

    def _price_now(self, t):
        return self.live_prices.get(t["symbol"]) or t.get("last_close")

    def _refresh_buyins(self):
        trades = load_trades()
        ts = trade_summary(trades)
        amount = float(self.cfg.get("trade_amount") or 100000)
        in_rupees = self.pnl_mode == "amt"
        for n, b in self.pnl_btns.items():
            b.configure(style="SegOn.TButton" if n == self.pnl_mode else "Seg.TButton")
        self.amount_entry.configure(state="normal" if in_rupees else "disabled")
        active = sum(1 for t in trades if t.get("status") in self.BUYIN_FILTERS["active"])
        rupees = lambda v: ("+" if v >= 0 else "−") + f"₹{abs(v):,.0f}"  # noqa: E731
        if in_rupees:
            booked = opened = mine_total = 0.0
            n_mine = 0
            for t in trades:
                val, realised = app_pnl(t, self._price_now(t))
                amt = pnl_amount(val, t.get("entry_price"), amount)
                if amt is not None:
                    if realised:
                        booked += amt
                    else:
                        opened += amt
                mine_amt = pnl_amount(my_pnl(t, self._price_now(t))[0], t.get("my_buy_price"), amount)
                if mine_amt is not None:
                    mine_total += mine_amt
                    n_mine += 1
            summary = (f"{active} active  ·  {ts['closed']} closed  ·  app "
                       + (f"{rupees(booked)} booked" if ts["closed"] else "nothing booked yet")
                       + f", {rupees(opened)} open" + (f"  ·  mine {rupees(mine_total)}" if n_mine else ""))
        else:
            mine = [p for p in (my_pnl(t, self._price_now(t))[0] for t in trades) if p is not None]
            summary = (f"{active} active  ·  {ts['closed']} closed"
                       + (f"  ·  app avg {ts['avg']:+.1f}%" if ts["closed"] else "")
                       + (f"  ·  my trades {len(mine)}, avg {sum(mine) / len(mine):+.1f}%" if mine else ""))
        self.buyin_summary.configure(text=summary)
        self.buyins.delete(*self.buyins.get_children())
        allowed = self.BUYIN_FILTERS[self.buyin_filter]
        shown = [t for t in trades if allowed is None or t.get("status") in allowed]
        shown.sort(key=lambda t: t.get("breakout_date", ""), reverse=True)
        money = lambda v: f"₹{v:,.2f}" if v else "—"  # noqa: E731

        def pnl_text(p, entry):
            val, realised = p
            if in_rupees:
                val = pnl_amount(val, entry, amount)
                text = None if val is None else rupees(val)
            else:
                text = None if val is None else f"{val:+.2f}%"
            return "—" if text is None else text + ("" if realised else " •")

        for t in shown:
            st = t.get("status")
            label = TRADE_LABELS.get(st, st)
            if st == "retesting" and t.get("buy_above"):
                label = f"Buy above {money(t['buy_above'])}"
                if t.get("alert_buy"):
                    label = f"Buy triggered {money(t.get('alert_buy_price'))}"
            entry = (f"{money(t['entry_price'])} · {t['entry_date'][5:]}" if t.get("entry_price")
                     else "—")
            my_buy = "—"
            if t.get("my_buy_price"):
                my_buy = f"{money(t['my_buy_price'])} · {str(t.get('my_buy_time', ''))[5:10]}"
            price_now = self._price_now(t)
            app_val = app_pnl(t, price_now)[0]
            sign = app_val if app_val is not None else my_pnl(t, price_now)[0]
            tag = st if sign is None or sign == 0 else ("pos" if sign > 0 else "neg")
            self.buyins.insert("", "end", iid=t["id"], tags=(tag,), values=(
                t.get("breakout_date", "—"), t["symbol"], money(t.get("R")), entry, money(t.get("stop")),
                money(t.get("target")), label, pnl_text(app_pnl(t, price_now), t.get("entry_price")), my_buy,
                pnl_text(my_pnl(t, price_now), t.get("my_buy_price"))))
        self._apply_sort(self.buyins)
        if shown:
            self.buyin_empty.place_forget()
        else:
            self.buyin_empty.place(relx=0.5, rely=0.5, anchor="center")

    def open_my_trade_dialog(self):
        sel = self.buyins.selection()
        if not sel:
            messagebox.showinfo("My buy / sell", "Select a stock in the Buy-ins table first (or double-click it).")
            return
        trades = load_trades()
        tr = next((t for t in trades if t["id"] == sel[0]), None)
        if not tr:
            return
        now = now_ist()
        win = tk.Toplevel(self)
        win.title(f"My trade · {tr['symbol']}")
        win.configure(bg=P["bg"])
        win.transient(self)
        win.resizable(False, False)
        outer, c = self._card(win, f"My trade · {tr['symbol']}")
        outer.pack(fill="both", expand=True, padx=20, pady=20)
        app_hint = (f"App buy-in: ₹{tr['entry_price']:,.2f} on {tr['entry_date']}" if tr.get("entry_price") else
                    f"App buy-in: not triggered yet ({TRADE_LABELS.get(tr.get('status'), '')})")
        ttk.Label(c, text=f"Breakout level ₹{tr['R']:,.2f}  ·  {app_hint}", style="Hint.TLabel").pack(
            anchor="w", pady=(0, 12))

        def split(ts):
            ts = str(ts or "")
            return (ts[:10], ts[11:16]) if ts else ("", "")

        bd, bt = split(tr.get("my_buy_time"))
        sd, st_ = split(tr.get("my_sell_time"))
        v = {"buy": tk.StringVar(value=str(tr.get("my_buy_price") or "")),
             "buy_d": tk.StringVar(value=bd or f"{now:%Y-%m-%d}"), "buy_t": tk.StringVar(value=bt or f"{now:%H:%M}"),
             "sell": tk.StringVar(value=str(tr.get("my_sell_price") or "")),
             "sell_d": tk.StringVar(value=sd), "sell_t": tk.StringVar(value=st_)}
        grid = ttk.Frame(c, style="Card.TFrame")
        grid.pack(fill="x")
        for col, text in enumerate(["", "Price (₹)", "Date (YYYY-MM-DD)", "Time (HH:MM)"]):
            ttk.Label(grid, text=text, style="Field.TLabel").grid(row=0, column=col, sticky="w", padx=(0, 10), pady=(0, 4))
        for row, (label, keys) in enumerate([("I bought at", ("buy", "buy_d", "buy_t")),
                                             ("I sold at (optional)", ("sell", "sell_d", "sell_t"))], start=1):
            ttk.Label(grid, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
            for col, (k, w) in enumerate(zip(keys, (12, 14, 8)), start=1):
                ttk.Entry(grid, textvariable=v[k], width=w).grid(row=row, column=col, sticky="w", padx=(0, 10), pady=4)
        error = tk.Label(c, bg=P["surface"], fg=P["red"], font=(FONT, 9), justify="left")
        error.pack(anchor="w", pady=(8, 0))
        ttk.Label(c, text="Leave 'sold at' empty while you still hold it; profit then uses the latest price.",
                  style="Hint.TLabel").pack(anchor="w", pady=(4, 12))

        def parse(price_s, d_s, t_s, required):
            price_s, d_s, t_s = price_s.strip().replace(",", ""), d_s.strip(), t_s.strip() or "00:00"
            if not price_s:
                if required:
                    raise ValueError("Enter the price you bought at.")
                return None, None
            price = float(price_s)
            if price <= 0:
                raise ValueError("Prices must be above 0.")
            stamp = datetime.strptime(f"{d_s} {t_s}", "%Y-%m-%d %H:%M")
            return round(price, 2), f"{stamp:%Y-%m-%d %H:%M}"

        def save(clear=False):
            try:
                if clear:
                    buy = sell = (None, None)
                else:
                    buy = parse(v["buy"].get(), v["buy_d"].get(), v["buy_t"].get(), True)
                    sell = parse(v["sell"].get(), v["sell_d"].get() or v["buy_d"].get(), v["sell_t"].get(), False)
                    if sell[1] and sell[1] < buy[1]:
                        raise ValueError("The sell time is before the buy time.")
            except ValueError as e:
                msg = str(e)
                error.configure(text=msg if not msg.startswith(("could not", "time data", "unconverted"))
                                else "Check the numbers and the date/time format (YYYY-MM-DD, HH:MM).")
                return
            for t in trades:
                if t["id"] == tr["id"]:
                    t["my_buy_price"], t["my_buy_time"] = buy
                    t["my_sell_price"], t["my_sell_time"] = sell
                    for k in ("my_buy_price", "my_buy_time", "my_sell_price", "my_sell_time"):
                        if t[k] is None:
                            del t[k]
                    t["my_updated"] = now_ist().strftime("%Y-%m-%d %H:%M:%S")
            save_trades(trades)
            win.destroy()
            self._refresh_buyins()

        row = ttk.Frame(c, style="Card.TFrame")
        row.pack(fill="x")
        if tr.get("my_buy_price"):
            ttk.Button(row, text="Remove my trade", style="Danger.TButton",
                       command=lambda: save(clear=True)).pack(side="left")
        ttk.Button(row, text="Save", style="Accent.TButton", command=save).pack(side="right")
        win.bind("<Return>", lambda e: save())
        win.grab_set()

    def _show_tab(self, name):
        for n, frame in self.tabs.items():
            frame.pack_forget()
            self.tab_btns[n].configure(style="SegOn.TButton" if n == name else "Seg.TButton")
        self.tabs[name].pack(fill="both", expand=True)
        if name == "buyins":
            self.tab_hint.pack_forget()
            self.buyin_summary.pack(side="right")
        else:
            self.buyin_summary.pack_forget()
            self.tab_hint.pack(side="right")
        self.tab_hint.configure(text={
            "live": "highlighted rows are new  ·  sorted by score",
            "buyins": "predicted breakout → retest → buy above the retest day's high  ·  stop 1% under "
                      "the retest low",
            "track": f"hit = a close above the breakout level within {HORIZON} sessions  ·  newest first"}[name])
        if name == "track":
            self._refresh_track()
        if name == "buyins":
            self._refresh_buyins()

    def _set_track_filter(self, name):
        self.track_filter = name
        for n, b in self.filter_btns.items():
            b.configure(style="SegOn.TButton" if n == name else "Seg.TButton")
        self._refresh_track()

    def _refresh_track(self):
        preds = load_predictions()
        hit = sum(p["status"] == "hit" for p in preds)
        miss = sum(p["status"] == "miss" for p in preds)
        opened = sum(p["status"] == "open" for p in preds)
        self.track_summary.configure(text=f"{hit} hits  ·  {miss} misses  ·  {opened} open")
        self.track.delete(*self.track.get_children())
        shown = [p for p in preds if self.track_filter == "all" or p["status"] == self.track_filter]
        shown.sort(key=lambda p: (p["date"], p.get("time", "")), reverse=True)
        label = {"hit": "✓ Hit", "miss": "✗ Missed", "open": "• Open"}
        for p in shown:
            move, tag = "—", p["status"]
            if p.get("resolved_close") and p.get("price"):
                change = (p["resolved_close"] - p["price"]) / p["price"] * 100
                move = f"{change:+.2f}%"
                tag = "pos" if change > 0 else "neg" if change < 0 else tag  # colour by the move's sign
            self.track.insert("", "end", tags=(tag,), values=(
                f"{p['date']} {p.get('time', '')}".strip(), p["symbol"], f"{p.get('score', 0):.0f}",
                f"₹{p['price']:,.2f}" if p.get("price") else "—", f"₹{p['R']:,.2f}",
                label.get(p["status"], p["status"]), p.get("resolved", "—"),
                str(p["sessions"]) if p.get("sessions") else "—", move))
        self._apply_sort(self.track)
        if shown:
            self.track_empty.place_forget()
        else:
            self.track_empty.place(relx=0.5, rely=0.5, anchor="center")

    # ------------------------------------------------------------------ helpers
    def _set_pill(self, state):
        color, text = {"idle": (P["muted"], "IDLE"), "live": (P["green"], "LIVE"),
                       "working": (P["amber"], "WORKING"), "error": (P["red"], "ERROR")}[state]
        self.pill_dot.delete("all")
        self.pill_dot.create_oval(1, 1, 9, 9, fill=color, outline=color)
        self.pill_text.configure(text=text, fg=color)

    def _main_btn_idle(self):
        self.main_btn.configure(text="▶  Start live predictor", style="Accent.TButton", command=self.start)
        for b in self.buttons.values():
            b.configure(state="normal")

    def _main_btn_running(self):
        self.main_btn.configure(text="■  Stop", style="Danger.TButton", command=self.stop)
        for b in self.buttons.values():
            b.configure(state="disabled")

    def _update_email_hint(self):
        to = self.cfg.get("email_to")
        ready = to and self.cfg.get("smtp_user") and self.smtp_pw_var.get()
        self.email_hint.configure(text="✉  Email: on" if ready else
                                  "✉  Email: off" if not to else "✉  Email: needs password")

    def _refresh_stats(self):
        preds = load_predictions()
        today = today_str()
        hit = sum(p["status"] == "hit" for p in preds)
        miss = sum(p["status"] == "miss" for p in preds)
        opened = sum(p["status"] == "open" for p in preds)
        self.stat["now"][0].configure(text="—" if self.row_count is None else str(self.row_count))
        self.stat["now"][1].configure(text=f"{sum(p['date'] == today for p in preds)} new today  ·  {opened} open")
        ts = trade_summary(load_trades())
        if ts["closed"]:
            self.stat["buyin"][0].configure(text=f"{ts['win_rate']:.0f}%",
                                            foreground=P["green"] if ts["avg"] > 0 else P["red"])
            self.stat["buyin"][1].configure(text=f"{ts['closed']} closed  ·  avg {ts['avg']:+.1f}% per trade")
        else:
            active = sum(1 for t in load_trades() if t.get("status") in ("waiting_retest", "retesting", "entered"))
            self.stat["buyin"][0].configure(text="—", foreground=P["text"])
            self.stat["buyin"][1].configure(text=f"{active} active  ·  no closed trades yet")
        if hasattr(self, "buyins"):
            self._refresh_buyins()
        self.stat["hit"][0].configure(text=f"{hit / (hit + miss) * 100:.0f}%" if hit + miss else "—",
                                      foreground=P["text"])
        self.stat["hit"][1].configure(text=(f"{hit} hit  ·  {miss} missed  ·  {opened} pending" if hit + miss
                                            else f"graded after {HORIZON} sessions"))
        if hasattr(self, "track"):
            self._refresh_track()
        bt = read_json(APP_DIR / "backtest.json")
        if bt:
            lift = bt["setups"] - bt["baseline"]
            self.stat["lift"][0].configure(text=f"{lift:+.1f} pts", foreground=P["green"] if lift > 0 else P["red"])
            self.stat["lift"][1].configure(text=f"{bt['setups']:.0f}% vs {bt['baseline']:.0f}% baseline")
        else:
            self.stat["lift"][0].configure(text="—", foreground=P["text"])
            self.stat["lift"][1].configure(text="run a backtest to measure")

    # ------------------------------------------------------------------ email dialog
    def open_email_dialog(self):
        win = tk.Toplevel(self)
        win.title("Email alerts")
        win.configure(bg=P["bg"])
        win.transient(self)
        win.resizable(False, False)
        outer, c = self._card(win, "Email alerts", "Gmail: use an App Password")
        outer.pack(fill="both", expand=True, padx=20, pady=20)
        local = {}
        for k, label in [("smtp_host", "SMTP host"), ("smtp_port", "Port (465 = SSL, 587 = STARTTLS)"),
                         ("smtp_user", "From / login"), ("email_to", "Send to (comma separated)"),
                         ("max_emails_per_day", "Max emails per day")]:
            local[k] = self.vars.get(k) or tk.StringVar(value=str(self.cfg[k]))
            self.vars[k] = local[k]
            self._field(c, label, var=local[k], width=40)
        self._field(c, "Password (kept in memory only)", var=self.smtp_pw_var, show="•", width=40)
        row = ttk.Frame(c, style="Card.TFrame")
        row.pack(fill="x", pady=(4, 0))

        def save():
            if self._collect():
                self._update_email_hint()
                win.destroy()
        ttk.Button(row, text="Send test email", style="Ghost.TButton", command=self.test_email).pack(side="left")
        ttk.Button(row, text="Save", style="Accent.TButton", command=save).pack(side="right")
        win.grab_set()

    # ------------------------------------------------------------------ help
    HELP_DIR = Path(__file__).resolve().parent / "docs" / "images"
    HELP = [
        ("h1", "How to use Breakout Predictor"),
        ("p", "The app watches the Nifty 500 and lists stocks that look ready to close above their 20-day high "
              f"within {HORIZON} trading sessions. When one of them breaks out and then retests the old high, it "
              "gives you a buy-in price, a stop and a target, and keeps score of how its calls turned out."),
        ("img", "overview.png"),
        ("h2", "1.  Paste your Upstox token"),
        ("p", "Upstox developer console → My Apps → Analytics token (read-only, valid for a year). Paste it under "
              "Connection. It stays in memory only and is never saved or uploaded."),
        ("img", "connection.png"),
        ("h2", "2.  Set your rules"),
        ("p", "Min setup score: how strict the predictions are (70 keeps about the top quarter). Max % below 20D high: how close "
              "to the breakout level a stock must be. Min price: skips penny stocks. Buy-in profit target %: the "
              "target for buy-ins (10% by default). Tick the 200-week MA box to keep only long-term uptrends."),
        ("img", "rules.png"),
        ("h2", "3.  Run it"),
        ("p", "Start live predictor scans every minute while the market is open (09:15–15:30 IST, Mon–Fri) and "
              "emails new setups and buy-in signals. Scan now does a single scan without emails, handy for a quick "
              "look. Run backtest replays the rules over the past year so you can see whether they work. The first "
              "run of the day downloads about a year of candles, which takes a few minutes."),
        ("img", "actions.png"),
        ("h2", "4.  Read the scoreboard"),
        ("p", "LIKELY BREAKOUTS: setups right now. BUY-IN WIN RATE: share of closed buy-ins that hit the target. "
              f"TRACKED HIT RATE: share of past predictions that did break out within {HORIZON} sessions. "
              "BACKTEST LIFT: how many percentage points more often the picks broke out than every stock near its "
              "20-day high (positive means the score helps)."),
        ("img", "stats.png"),
        ("h2", "5.  Live setups"),
        ("p", "Each row is a predicted breakout. BREAKOUT LEVEL is the 20-day high to beat, DISTANCE is how far below "
              "it the price is, SCORE is 0–100 and WHY lists what pushed the score up. Highlighted rows are new in "
              "this scan. When the market is closed it shows a preview for the next session. Click any column heading "
              "to sort by it (click again to reverse); this works in all three tables."),
        ("img", "live.png"),
        ("h2", "6.  Buy-ins"),
        ("p", "Predicted stocks that broke out move here: Waiting for retest → Retesting (Buy above ₹X) → In trade → "
              "Target hit, Stopped out or Expired. APP BUY-IN, STOP and TARGET are the app's plan. Use the filters "
              "to see active trades, winners, or stocks that never gave an entry. P&L in % / ₹ switches the P&L columns "
              "between percent and rupees; ₹ assumes you put the 'per trade' amount (₹1,00,000 by default) into whole "
              "shares at each buy price. Rows with a P&L are green when it's up and red when it's down."),
        ("img", "buyins.png"),
        ("h2", "7.  Record your own trade"),
        ("p", "Select a row and click ✎ My buy / sell (or double-click it). Enter the price, date and time you "
              "bought at, and optionally where you sold. MY P&L then sits next to APP P&L. A • means the position "
              "is still open and is valued at the live price (or the last close)."),
        ("img", "mytrade.png"),
        ("h2", "8.  Track record"),
        ("p", "Every live prediction is graded automatically: a hit is a daily close above the breakout level within "
              f"{HORIZON} sessions, a miss is anything else. Filter by hits, misses or still open."),
        ("img", "track.png"),
        ("h2", "9.  Email alerts and your account"),
        ("p", "The Email button opens the SMTP settings (for Gmail: smtp.gmail.com, port 465 and an App Password). "
              "Sign in creates a free account so predictions, buy-ins, your own trades and settings follow you to "
              "any PC. ? opens this guide (or press F1)."),
        ("img", "header.png"),
        ("h2", "10.  Activity log"),
        ("p", "Shows what the app is doing: downloads, scans, alerts and errors. Copy puts the whole log on the "
              "clipboard, handy for reporting a problem. Wrap toggles line wrapping."),
        ("img", "activity.png"),
        ("h2", "Good to know"),
        ("p", "•  Leave the app running during market hours for live alerts; otherwise open it once a day and use "
              "Scan now.\n•  Closing the window hides the app in the system tray (by the clock) and the predictor "
              "keeps running. Click the tray icon to open it; right-click → Exit to quit. Untick \"Keep running in "
              "tray when closed\" in that menu to make the X quit instead. New setups never pop the window up."
              "\n•  Predictions are probabilities, not certainties. Judge them by the backtest lift and "
              "your track record.\n•  Brokerage, taxes and slippage aren't included in any P&L. Nothing here is "
              "investment advice."),
    ]

    def open_help(self):
        if getattr(self, "help_win", None) and self.help_win.winfo_exists():
            self.help_win.deiconify()
            self.help_win.lift()
            return
        win = self.help_win = tk.Toplevel(self)
        win.title("How to use · Breakout Predictor")
        win.configure(bg=P["bg"])
        w = min(int(980 * self.k), int(self.winfo_screenwidth() * 0.9))
        h = min(int(820 * self.k), int(self.winfo_screenheight() * 0.85))
        win.geometry(f"{w}x{h}+{self.winfo_rootx() + 40}+{self.winfo_rooty() + 30}")
        box = tk.Frame(win, bg=P["surface"], highlightthickness=1, highlightbackground=P["border"])
        box.pack(fill="both", expand=True, padx=16, pady=16)
        text = tk.Text(box, bg=P["surface"], fg=P["text"], relief="flat", borderwidth=0, highlightthickness=0,
                       wrap="word", padx=int(26 * self.k), pady=int(18 * self.k), cursor="arrow",
                       font=(FONT, 10), spacing3=int(4 * self.k))
        sb = ttk.Scrollbar(box, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.tag_configure("h1", font=(FONT, 18, "bold"), spacing3=int(10 * self.k))
        text.tag_configure("h2", font=(FONT, 12, "bold"), foreground=P["accent"],
                           spacing1=int(18 * self.k), spacing3=int(6 * self.k))
        text.tag_configure("p", foreground=P["text"])
        text.tag_configure("img", spacing1=int(8 * self.k), spacing3=int(8 * self.k))
        win.images = []  # keep references, or Tk shows blank images
        max_w = w - int(120 * self.k)
        for kind, value in self.HELP:
            if kind == "img":
                try:
                    img = tk.PhotoImage(file=str(self.HELP_DIR / value))
                except (tk.TclError, OSError):
                    continue  # screenshots ship in docs/images; the guide still reads fine without them
                factor = -(-img.width() // max_w)
                if factor > 1:
                    img = img.subsample(factor)
                win.images.append(img)
                text.image_create("end", image=img)
                text.insert("end", "\n", "img")
            else:
                text.insert("end", value + "\n", kind)
        text.configure(state="disabled")
        win.bind("<Escape>", lambda e: win.destroy())
        win.focus_set()

    # ------------------------------------------------------------------ actions
    def _collect(self):
        cfg = dict(self.cfg)
        for k, v in self.vars.items():
            cfg[k] = v.get().strip()
        try:
            cfg["min_score"] = float(cfg["min_score"])
            cfg["max_dist_pct"] = float(cfg["max_dist_pct"])
            cfg["min_price"] = float(cfg["min_price"] or 0)
            cfg["smtp_port"] = int(cfg["smtp_port"] or 465)
            cfg["max_emails_per_day"] = int(cfg["max_emails_per_day"] or 20)
            cfg["wma200_filter"] = bool(self.wma_var.get())
            cfg["target_pct"] = float(cfg.get("target_pct") or 10)
        except ValueError:
            messagebox.showerror("Settings", "Score, distance, price, port and max emails must be numbers.")
            return None
        if cfg["max_dist_pct"] <= 0:
            messagebox.showerror("Settings", "Max % below 20-day high must be greater than 0.")
            return None
        self.cfg = cfg
        save_config(cfg)
        if ACCOUNT and ACCOUNT.signed_in:
            self._cloud_task(lambda: ACCOUNT.push_settings(cfg), None)
        return cfg

    # ------------------------------------------------------------------ account
    def _cloud_task(self, fn, done_kind):
        """Run a Firebase call off the UI thread; report back through the event queue."""
        def work():
            try:
                fn()
                if done_kind:
                    self.events.put(("cloud", done_kind, None))
            except Exception as e:  # noqa: BLE001 - shown in the UI
                self.events.put(("cloud", "sync_error", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _update_account_ui(self, sync_text=None):
        if not ACCOUNT.configured:
            self.account_btn.configure(text="○  Account")
            self.sync_label.configure(text="Local only")
        elif ACCOUNT.signed_in:
            self.account_btn.configure(text=f"●  {ACCOUNT.email}")
            if sync_text is not None:
                self.sync_label.configure(text=sync_text)
        else:
            self.account_btn.configure(text="○  Sign in")
            self.sync_label.configure(text="Local only · sign in to sync")

    def _engine_busy(self):
        if self.engine and self.engine.is_alive():
            messagebox.showinfo("Account", "Stop the predictor before signing in or out.")
            return True
        return False

    def open_account_dialog(self):
        if not ACCOUNT.configured:
            messagebox.showinfo(
                "Account", "Accounts and cloud sync need a Firebase project.\n\n"
                f"Create {FIREBASE_CONFIG_PATH.name} next to the app (see firebase_config.example.json and the "
                "README), then restart the app.\n\nUntil then everything is stored on this PC only.")
            return
        if ACCOUNT.signed_in:
            return self._open_signed_in_dialog()
        if self._engine_busy():
            return

        win = tk.Toplevel(self)
        win.title("Sign in")
        win.configure(bg=P["bg"])
        win.transient(self)
        win.resizable(False, False)
        outer, c = self._card(win)
        outer.pack(fill="both", expand=True, padx=20, pady=20)
        mode = {"value": "signin"}

        seg = ttk.Frame(c, style="Card.TFrame")
        seg.pack(fill="x", pady=(0, 14))
        seg_btns = {}
        email_var, pw_var, pw2_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        ttk.Label(c, text="Email", style="Field.TLabel").pack(anchor="w", pady=(0, 4))
        email_entry = ttk.Entry(c, textvariable=email_var, width=36)
        email_entry.pack(fill="x", pady=(0, 12))
        ttk.Label(c, text="Password", style="Field.TLabel").pack(anchor="w", pady=(0, 4))
        ttk.Entry(c, textvariable=pw_var, show="•", width=36).pack(fill="x", pady=(0, 12))
        confirm = ttk.Frame(c, style="Card.TFrame")
        ttk.Label(confirm, text="Confirm password", style="Field.TLabel").pack(anchor="w", pady=(0, 4))
        ttk.Entry(confirm, textvariable=pw2_var, show="•", width=36).pack(fill="x", pady=(0, 12))
        error = tk.Label(c, bg=P["surface"], fg=P["red"], font=(FONT, 9), wraplength=int(300 * self.k), justify="left")
        error.pack(anchor="w")
        row = ttk.Frame(c, style="Card.TFrame")
        row.pack(fill="x", pady=(8, 0))
        forgot = ttk.Button(row, text="Forgot password?", style="Seg.TButton")
        forgot.pack(side="left")
        submit = ttk.Button(row, style="Accent.TButton")
        submit.pack(side="right")
        note = ttk.Label(c, style="Hint.TLabel", wraplength=int(300 * self.k), justify="left",
                         text="Your predictions, buy-ins and settings sync to your account, stored in the app "
                              "owner's Firebase project. Upstox tokens and email passwords stay on this PC.")
        note.pack(anchor="w", pady=(14, 0))

        def set_mode(m):
            mode["value"] = m
            for n, b in seg_btns.items():
                b.configure(style="SegOn.TButton" if n == m else "Seg.TButton")
            submit.configure(text="Sign in" if m == "signin" else "Create account")
            if m == "signup":
                confirm.pack(fill="x", before=error)
            else:
                confirm.pack_forget()
            error.configure(text="")

        for name, text in [("signin", "Sign in"), ("signup", "Create account")]:
            seg_btns[name] = ttk.Button(seg, text=text, style="Seg.TButton", width=-4, command=lambda n=name: set_mode(n))
            seg_btns[name].pack(side="left", padx=(0, 6))

        def finish(err):
            submit.configure(state="normal")
            if err:
                error.configure(text=err, fg=P["red"])
                return
            win.destroy()
            self._after_sign_in()

        def go():
            email, pw = email_var.get().strip(), pw_var.get()
            if not email or not pw:
                error.configure(text="Enter your email and password.", fg=P["red"])
                return
            if mode["value"] == "signup" and pw != pw2_var.get():
                error.configure(text="The passwords don't match.", fg=P["red"])
                return
            submit.configure(state="disabled")
            error.configure(text="Working…", fg=P["muted"])
            fn = ACCOUNT.sign_up if mode["value"] == "signup" else ACCOUNT.sign_in

            def work():
                try:
                    fn(email, pw)
                    self.after(0, finish, None)
                except Exception as e:  # noqa: BLE001
                    self.after(0, finish, str(e))
            threading.Thread(target=work, daemon=True).start()

        def forgot_pw():
            email = email_var.get().strip()
            if not email:
                error.configure(text="Enter your email first, then click Forgot password.", fg=P["red"])
                return

            def work():
                try:
                    ACCOUNT.reset_password(email)
                    self.after(0, lambda: error.configure(text=f"Password reset email sent to {email}.", fg=P["green"]))
                except Exception as e:  # noqa: BLE001
                    self.after(0, lambda: error.configure(text=str(e), fg=P["red"]))
            threading.Thread(target=work, daemon=True).start()

        submit.configure(command=go)
        forgot.configure(command=forgot_pw)
        win.bind("<Return>", lambda e: go())
        set_mode("signin")
        email_entry.focus_set()
        win.grab_set()

    def _after_sign_in(self):
        """Pull account data, optionally adopting predictions recorded on this PC before signing in."""
        self._update_account_ui("Syncing…")
        extra = None
        local_root = read_json(APP_DIR / "predictions.json", []) or []
        marker = ACCOUNT.data_dir() / "imported_local.flag"
        if local_root and not marker.exists():
            if messagebox.askyesno("Account", f"Add the {len(local_root)} prediction(s) recorded on this PC before "
                                              f"you signed in to {ACCOUNT.email}?"):
                extra = local_root
            write_json(marker, {"answered": True})

        def work():
            ACCOUNT.sync(extra)
            remote_cfg = ACCOUNT.pull_settings()
            if remote_cfg:
                self.events.put(("cloud", "settings", remote_cfg))
            else:
                ACCOUNT.push_settings(self.cfg)
        self._cloud_task(work, None)

    def _open_signed_in_dialog(self):
        win = tk.Toplevel(self)
        win.title("Account")
        win.configure(bg=P["bg"])
        win.transient(self)
        win.resizable(False, False)
        outer, c = self._card(win, "Account")
        outer.pack(fill="both", expand=True, padx=20, pady=20)
        ttk.Label(c, text="Signed in as", style="Field.TLabel").pack(anchor="w")
        ttk.Label(c, text=ACCOUNT.email, style="CardTitle.TLabel").pack(anchor="w", pady=(2, 10))
        ttk.Label(c, text=f"Status: {self.sync_label.cget('text')}", style="Hint.TLabel").pack(anchor="w", pady=(0, 14))
        row = ttk.Frame(c, style="Card.TFrame")
        row.pack(fill="x")

        def sync_now():
            self._update_account_ui("Syncing…")
            self._cloud_task(lambda: ACCOUNT.sync(), None)
            win.destroy()

        def sign_out():
            if self._engine_busy():
                return
            ACCOUNT.sign_out()
            win.destroy()
            self._update_account_ui()
            self._refresh_stats()
            self._log(f"[{now_ist():%H:%M:%S}] Signed out. Showing data stored on this PC.")
        ttk.Button(row, text="Sync now", style="Ghost.TButton", command=sync_now).pack(side="left")
        ttk.Button(row, text="Sign out", style="Danger.TButton", command=sign_out).pack(side="right")
        win.grab_set()

    def _apply_settings(self, remote):
        remote = dict(remote)
        if remote.get("score_version") != SCORE_VERSION:  # saved under the old score: its threshold doesn't apply
            remote.pop("min_score", None)
            remote.pop("score_version", None)
        for k, v in remote.items():
            if k in self.vars:
                self.vars[k].set(str(v))
        if "wma200_filter" in remote:
            self.wma_var.set(bool(remote["wma200_filter"]))
        self.cfg.update({k: v for k, v in remote.items() if k in DEFAULT_CONFIG})
        save_config(self.cfg)
        self._update_email_hint()

    def _launch(self, mode):
        if self.engine and self.engine.is_alive():
            return
        cfg = self._collect()
        if not cfg:
            return
        token = self.token_var.get().strip()
        if not token:
            messagebox.showerror("Upstox", "Paste your Upstox access token first.")
            return
        self.mode = mode
        self.engine = Engine(cfg, token, self.smtp_pw_var.get(), self.events, mode)
        self.engine.start()
        self._main_btn_running()
        self._set_pill("live" if mode == "live" else "working")
        self.status_var.set({"dry": "Scanning…", "backtest": "Preparing backtest…",
                             "live": "Starting live predictor…"}[mode])

    def start(self):
        self._launch("live")

    def dry_run(self):
        self._launch("dry")

    def run_backtest(self):
        self._launch("backtest")

    def stop(self):
        if self.engine:
            self.engine.stop()
            self.status_var.set("Stopping…")

    def test_email(self):
        cfg = self._collect()
        if not cfg:
            return
        pw = self.smtp_pw_var.get()
        if not (cfg["smtp_user"] and cfg["email_to"] and pw):
            messagebox.showerror("Email", "Fill SMTP login, password and recipients first.")
            return

        def work():
            try:
                send_email(cfg, pw, "Breakout predictor test email", "<p>Email alerts are working.</p>")
                self.events.put(("log", f"[{now_ist():%H:%M:%S}] Test email sent to {cfg['email_to']}"))
            except Exception as e:  # noqa: BLE001
                self.events.put(("log", f"[{now_ist():%H:%M:%S}] Test email failed: {e}"))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ events
    def _drain_events(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self._log(ev[1])
                elif kind == "status":
                    self.status_var.set(ev[1])
                elif kind == "stats":
                    self._refresh_stats()
                elif kind == "progress":
                    self.progress.configure(maximum=ev[2], value=ev[1])
                    self.status_var.set(f"Downloading price history… {ev[1]}/{ev[2]}")
                elif kind == "rows":
                    self._show_rows(*ev[1:])
                elif kind == "backtest":
                    self._refresh_stats()
                    messagebox.showinfo("Backtest", ev[1])
                elif kind == "cloud":
                    self._on_cloud(ev[1], ev[2])
                elif kind == "prices":
                    self.live_prices.update(ev[1])
                    self._refresh_buyins()
                elif kind == "notify":
                    self._notify(ev[1])
                elif kind == "tray":
                    if ev[1] == "open":
                        self._show_window()
                    elif ev[1] == "exit":
                        self._quit()
                        return
                    elif ev[1] == "toggle":
                        self.cfg["close_to_tray"] = not self.cfg.get("close_to_tray", True)
                        save_config(self.cfg)
                        self._log("Close button " + ("now hides the app in the tray (right-click the tray icon → "
                                                     "Exit to quit)." if self.cfg["close_to_tray"]
                                                     else "now quits the app."))
                elif kind == "auth_error":
                    self._set_pill("error")
                    messagebox.showerror("Upstox token", ev[1] + "\nPaste a fresh token and start again.")
                elif kind == "error":
                    self._set_pill("error")
                    self.status_var.set("Error: " + ev[1])
                elif kind == "stopped":
                    self._main_btn_idle()
                    self.progress.configure(value=0)
                    if self.pill_text.cget("text") != "ERROR":
                        self._set_pill("idle")
                    if ev[1] == "live" and not self.status_var.get().startswith("Error"):
                        self.status_var.set("Stopped.")
        except queue.Empty:
            pass
        self.after(200, self._drain_events)

    def _on_cloud(self, kind, payload):
        if kind == "synced":
            self._update_account_ui(f"☁ Synced {now_ist():%H:%M}")
            self._refresh_stats()
        elif kind == "restored":
            self._update_account_ui(f"☁ Synced {now_ist():%H:%M}")
            self._refresh_stats()
            self._log(f"[{now_ist():%H:%M:%S}] Signed in as {ACCOUNT.email}.")
        elif kind == "settings":
            self._apply_settings(payload)
            self._log(f"[{now_ist():%H:%M:%S}] Loaded your saved settings from your account.")
        elif kind == "sync_error":
            self._update_account_ui()
            if ACCOUNT.signed_in:
                self.sync_label.configure(text="⚠ Sync failed")
            self._log(f"[{now_ist():%H:%M:%S}] Cloud sync: {payload}")

    def _copy_log(self):
        text = self.log_box.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(text)
        self.copy_btn.configure(text="✓  Copied")
        self.after(1500, lambda: self.copy_btn.configure(text="⧉  Copy"))

    def _toggle_wrap(self):
        on = not self.wrap_var.get()
        self.wrap_var.set(on)
        self.log_box.configure(wrap="word" if on else "none")
        self.wrap_btn.configure(text=f"↵  Wrap: {'on' if on else 'off'}")
        if on:
            self.log_xsb.pack_forget()
        else:
            self.log_xsb.pack(fill="x", pady=(4, 0))

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _show_rows(self, rows, fresh, open_since):
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(rows):
            filled = int(round(clamp(r["score"] / 100) * 5))
            bar = "●" * filled + "○" * (5 - filled)
            tag = "new" if r["symbol"] in fresh else ("alt" if i % 2 else "")
            self.tree.insert("", "end", tags=(tag,) if tag else (), values=(
                r["symbol"], r["industry"], f"₹{r['ltp']:,.2f}", f"₹{r['R']:,.2f}", f"{r['dist']:.2f}%",
                f"{r['score']:.0f}   {bar}", "  ·  ".join(r["reasons"]),
                open_since.get(r["symbol"], "new")))
        self.row_count = len(rows)
        self._apply_sort(self.tree)
        if rows:
            self.empty_label.place_forget()
        else:
            self.empty_label.configure(text="No stocks qualify right now.\nTry lowering the min score or widening the distance.")
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        self._refresh_stats()

    def _notify(self, count):
        # Stay where the user left it: just a chime and a count in the title / taskbar, no stealing focus.
        self.bell()
        self.title(f"({count} new) {self.TITLE}")
        self.after(10000, lambda: self.title(self.TITLE))

    def _on_close(self):
        """The window's X: hide in the tray (the predictor keeps running) or quit, depending on the tray setting."""
        if self.tray and self.cfg.get("close_to_tray", True):
            self.withdraw()
            if not self.tray_hint_shown:
                self.tray_hint_shown = True
                self._log("Still running in the system tray. Click the tray icon to open; right-click → Exit to quit.")
            return
        self._quit()

    def _show_window(self):
        """Bring the window back, only ever because the user asked (tray click or launching the app again)."""
        self.deiconify()
        self.state("normal")
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))
        self.focus_force()

    def _quit(self):
        if self.engine and self.engine.is_alive():
            self.engine.stop()
        if self.tray:
            self.tray.stop()
        self.destroy()


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--make-icon":
        write_icon(sys.argv[2])
    elif Tray.show_running_instance():
        pass  # already running (perhaps hidden in the tray): that window was opened instead
    else:
        try:
            app = App()
            app.lift()
            app.attributes("-topmost", True)
            app.after(800, lambda: app.attributes("-topmost", False))
            app.focus_force()
            app.mainloop()
        except Exception:  # noqa: BLE001 - pythonw has no console, so leave a trace
            import traceback
            APP_DIR.mkdir(parents=True, exist_ok=True)
            (APP_DIR / "crash.log").write_text(traceback.format_exc(), encoding="utf-8")
            try:
                messagebox.showerror("Breakout Predictor", f"The app failed to start.\nDetails: {APP_DIR / 'crash.log'}")
            except Exception:  # noqa: BLE001
                pass
            raise
