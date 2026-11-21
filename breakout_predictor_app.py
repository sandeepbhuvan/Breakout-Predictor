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

import csv
import gzip
import io
import json
import os
import queue
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

DEFAULT_CONFIG = {
    "min_score": 65,
    "max_dist_pct": 3.0,
    "min_price": 20.0,
    "nifty_csv_path": "",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 465,
    "smtp_user": "",
    "email_to": "",
    "max_emails_per_day": 20,
}


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
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(read_json(APP_DIR / "config.json", {}) or {})
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
    return {**stock, "sma20": sma20, "sma50": rolling_mean(c, 50), "bw": bw,
            "atr5": rolling_mean(tr, 5), "atr20": rolling_mean(tr, 20)}


def setup_components(S, t):
    """Price-independent part of the score (max 80), using bars up to and including index t."""
    h, l, c, v = S["h"], S["l"], S["c"], S["v"]
    R = max(h[t - 19:t + 1])

    atr_ratio = S["atr5"][t] / S["atr20"][t] if S["atr20"][t] else 1.0
    contraction = clamp((1.0 - atr_ratio) / 0.4)

    window = [x for x in S["bw"][t - 39:t + 1] if x is not None]
    squeeze = 1.0 - sum(1 for x in window if x < S["bw"][t]) / len(window) if window and S["bw"][t] else 0.0

    rising = 1.0 if min(l[t - 4:t + 1]) > min(l[t - 14:t - 4]) else 0.0

    up = sum(v[i] for i in range(t - 14, t + 1) if c[i] > c[i - 1])
    dn = sum(v[i] for i in range(t - 14, t + 1) if c[i] < c[i - 1])
    accum = clamp((up / max(dn, 1) - 0.8) / 0.8)

    trend = 0.5 * (c[t] > S["sma50"][t]) + 0.5 * (S["sma20"][t] > S["sma50"][t])

    tests = sum(1 for i in range(t - 19, t + 1) if h[i] >= R * 0.985)
    tests_score = clamp((tests - 1) / 3)

    base = 15 * contraction + 15 * squeeze + 15 * rising + 15 * accum + 10 * trend + 10 * tests_score
    reasons = []
    if contraction >= 0.5:
        reasons.append("range contracting")
    if squeeze >= 0.75:
        reasons.append("BB squeeze")
    if rising:
        reasons.append("higher lows")
    if accum >= 0.5:
        reasons.append("accumulation")
    if trend == 1:
        reasons.append("uptrend")
    if tests >= 3:
        reasons.append(f"R tested {tests}x")
    return {"R": R, "base": base, "reasons": reasons}


def proximity(R, price, max_dist):
    """(score 0-20, distance %) or None if price is already above R or too far below."""
    if price <= 0:
        return None
    dist = (R - price) / price * 100
    if dist < 0 or dist > max_dist:
        return None
    return 20 * (1 - dist / max_dist), dist


def backtest(history, cfg, stop_event=None):
    """Walk forward through stored history with the exact live rules."""
    thr, max_dist, min_price = float(cfg["min_score"]), float(cfg["max_dist_pct"]), float(cfg["min_price"])
    buckets = {b: [0, 0] for b in ("<50", "50-60", "60-70", "70-80", "80+")}
    near = [0, 0]
    picked = [0, 0]
    for stock in history.values():
        if stop_event and stop_event.is_set():
            break
        n = len(stock["c"])
        if n < MIN_BARS + HORIZON + 1:
            continue
        S = compute_series(stock)
        c = S["c"]
        for t in range(MIN_BARS - 1, n - HORIZON):
            if c[t] < min_price:
                continue
            comp = setup_components(S, t)
            prox = proximity(comp["R"], c[t], max_dist)
            if not prox:
                continue
            score = comp["base"] + prox[0]
            hit = any(c[j] > comp["R"] for j in range(t + 1, t + 1 + HORIZON))
            key = "<50" if score < 50 else "80+" if score >= 80 else f"{int(score // 10) * 10}-{int(score // 10) * 10 + 10}"
            for agg in (buckets[key], near) + ((picked,) if score >= thr else ()):
                agg[0] += 1
                agg[1] += hit
    rate = lambda a: f"{a[1] / a[0] * 100:.1f}% of {a[0]:,}" if a[0] else "n/a"  # noqa: E731
    lines = [f"Backtest: close above 20-day high within {HORIZON} sessions",
             f"  All stocks within {max_dist}% of R (baseline): {rate(near)}",
             f"  Your setups (score >= {thr:g}):            {rate(picked)}",
             "  By score bucket:"]
    lines += [f"    {k:>6}: {rate(v)}" for k, v in buckets.items()]
    lines.append("  (Overlapping days on the same stock are counted separately.)")
    lines.append("  Judge the threshold by the LIFT over the baseline, not the absolute %.")
    if near[0] and picked[0]:
        write_json(APP_DIR / "backtest.json", {"baseline": near[1] / near[0] * 100,
                                               "setups": picked[1] / picked[0] * 100,
                                               "min_score": thr, "date": today_str()})
    return "\n".join(lines)


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

    def daily_candles(self, key, to_date, from_date, limiter):
        path = f"/historical-candle/{urllib.parse.quote(key, safe='')}/day/{to_date}/{from_date}"
        return (self.get(path, limiter=limiter).get("data") or {}).get("candles") or []


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


def build_history(client, universe, log, progress, stop_event):
    """~1 year of completed daily candles per stock (today excluded), oldest-first. Cached per day."""
    path = APP_DIR / f"history_{today_str()}.json"
    cached = read_json(path)
    if cached:
        return cached

    log(f"Downloading daily history for {len(universe)} stocks (~{len(universe) // HISTORY_REQ_PER_SEC + 5}s)...")
    to_date = today_str()
    from_date = (now_ist() - timedelta(days=HISTORY_CALENDAR_DAYS)).strftime("%Y-%m-%d")
    limiter = RateLimiter(HISTORY_REQ_PER_SEC)
    history, done, auth_failures, errors = {}, 0, 0, 0
    lock = threading.Lock()
    blocked = threading.Event()

    def fetch(item):
        nonlocal done, auth_failures, errors
        if stop_event.is_set() or blocked.is_set():
            return
        try:
            candles = client.daily_candles(item["key"], to_date, from_date, limiter)
            # Upstox returns newest-first: [timestamp, open, high, low, close, volume, oi]
            bars = sorted((c for c in candles if not str(c[0]).startswith(to_date)), key=lambda c: str(c[0]))
            if len(bars) >= MIN_BARS:
                with lock:
                    history[item["key"]] = {
                        "symbol": item["symbol"], "industry": item["industry"],
                        "d": [str(b[0])[:10] for b in bars],
                        "h": [float(b[2]) for b in bars], "l": [float(b[3]) for b in bars],
                        "c": [float(b[4]) for b in bars], "v": [float(b[5]) for b in bars],
                    }
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
    if auth_failures and not history:
        raise AuthError("Upstox rejected the token (401). It may have expired.")
    if len(history) < 400:
        raise RuntimeError(f"Only built history for {len(history)} stocks ({errors} errors); not saving.")
    write_json(path, history)
    for old in APP_DIR.glob("history_*.json"):
        if old != path:
            old.unlink(missing_ok=True)
    log(f"History saved for {len(history)} stocks.")
    return history


# =========================================================================== prediction log

def load_predictions():
    return read_json(APP_DIR / "predictions.json", []) or []


def save_predictions(preds):
    write_json(APP_DIR / "predictions.json", preds)


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
        hit = next((d for d, c in window if c > p["R"]), None)
        if hit:
            p["status"], p["resolved"] = "hit", hit
        elif len(window) >= HORIZON:
            p["status"], p["resolved"] = "miss", window[-1][0]
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
                self.status("Running backtest...")
                report = backtest(self.history, self.cfg, self.stop_event)
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
        universe = load_universe(self.cfg, self.log)
        self.history = build_history(self.client, universe, self.log,
                                     lambda d, t: self.events.put(("progress", d, t)), self.stop_event)
        preds = grade_predictions(load_predictions(), self.history)
        save_predictions(preds)
        self.events.put(("stats", prediction_stats(preds)))

        self.setups = {}
        for key, stock in self.history.items():
            S = compute_series(stock)
            t = len(S["c"]) - 1
            comp = setup_components(S, t)
            comp.update(symbol=stock["symbol"], industry=stock["industry"], prev_close=S["c"][t])
            self.setups[key] = comp
        self.prepared_for = today_str()
        self.log(f"Setups computed for {len(self.setups)} stocks from completed sessions.")

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
        thr, max_dist, min_price = float(self.cfg["min_score"]), float(self.cfg["max_dist_pct"]), float(self.cfg["min_price"])

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
            if ltp < min_price:
                continue
            prox = proximity(s["R"], ltp, max_dist)
            if not prox:
                continue
            score = s["base"] + prox[0]
            if score >= thr:
                rows.append({"key": key, "symbol": s["symbol"], "industry": s["industry"], "ltp": ltp,
                             "R": s["R"], "dist": prox[1], "score": score, "reasons": s["reasons"]})
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
        if not live or not fresh:
            return

        for r in fresh:
            preds.append({"date": today_str(t), "time": f"{t:%H:%M}", "symbol": r["symbol"], "R": r["R"],
                          "price": r["ltp"], "score": round(r["score"], 1), "status": "open"})
        save_predictions(preds)
        stats = prediction_stats(preds)
        self.events.put(("stats", stats))
        self.log(f"New predictions: {', '.join(r['symbol'] for r in fresh)}")

        state_path = APP_DIR / f"emails_{today_str(t)}.json"
        state = read_json(state_path, {"sent": 0})
        if not self.email_enabled():
            self.log("Email not configured; shown here only.")
        elif state["sent"] >= int(self.cfg["max_emails_per_day"]):
            self.log("Daily email cap reached; shown here only.")
        else:
            subject = f"Nifty 500: {len(fresh)} likely breakout(s) within {HORIZON} days - {t:%d %b %H:%M}"
            try:
                send_email(self.cfg, self.smtp_password, subject, alert_html(fresh, self.cfg, t, stats))
                state["sent"] += 1
                write_json(state_path, state)
                self.log(f"Email sent to {self.cfg['email_to']}")
            except Exception as e:  # noqa: BLE001
                self.log(f"Email failed: {e}")
        self.events.put(("notify", len(fresh)))


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
        self.smtp_pw_var = tk.StringVar(value=os.environ.get("HHLL_SMTP_PASSWORD", ""))
        self._style()
        self._build()
        self._refresh_stats()
        self._set_pill("idle")
        self.after(200, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

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

        self.pill = tk.Frame(header, bg=P["surface2"], padx=14, pady=7)
        self.pill.pack(side="right")
        ttk.Button(header, text="✉  Email alerts", style="Ghost.TButton",
                   command=self.open_email_dialog).pack(side="right", padx=(0, 12))
        self.email_hint = ttk.Label(header, style="Status.TLabel")
        self.email_hint.pack(side="right", padx=(0, 12))
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
        self._field(c, "Nifty 500 CSV path (optional)", "nifty_csv_path")

        outer, c = self._card(side, "Prediction rules")
        outer.pack(fill="x", pady=(0, 14))
        grid = ttk.Frame(c, style="Card.TFrame")
        grid.pack(fill="x")
        for i, (k, label) in enumerate([("min_score", "Min setup score"), ("max_dist_pct", "Max % below 20D high"),
                                        ("min_price", "Min price (₹)")]):
            ttk.Label(grid, text=label, style="Card.TLabel").grid(row=i, column=0, sticky="w", pady=5)
            self.vars[k] = tk.StringVar(value=str(self.cfg[k]))
            ttk.Entry(grid, textvariable=self.vars[k], width=8, justify="right").grid(row=i, column=1, sticky="e", pady=5)
        grid.columnconfigure(0, weight=1)
        ttk.Label(c, text=f"A hit = a daily close above the 20-day high\nwithin {HORIZON} sessions of the prediction.",
                  style="Hint.TLabel", justify="left").pack(anchor="w", pady=(10, 0))

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
        for i, (key, label) in enumerate([("now", "LIKELY BREAKOUTS"), ("today", "NEW TODAY"),
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

        outer, c = self._card(main, "Predictions", "highlighted rows are new  ·  sorted by score", padding=(18, 14))
        outer.grid(row=2, column=0, sticky="nsew")
        tbl = ttk.Frame(c, style="Card.TFrame")
        tbl.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tbl, columns=[col[0] for col in self.COLUMNS], show="headings", selectmode="browse")
        for key, title, width, anchor in self.COLUMNS:
            self.tree.heading(key, text=title, anchor=anchor)
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
        self.email_hint.configure(text=f"Emailing {to}" if ready else
                                  "Email off · alerts show here only" if not to else
                                  "Email needs the SMTP password")

    def _refresh_stats(self):
        preds = load_predictions()
        today = today_str()
        hit = sum(p["status"] == "hit" for p in preds)
        miss = sum(p["status"] == "miss" for p in preds)
        opened = sum(p["status"] == "open" for p in preds)
        self.stat["now"][0].configure(text="—" if self.row_count is None else str(self.row_count))
        self.stat["now"][1].configure(text="from the latest scan")
        self.stat["today"][0].configure(text=str(sum(p["date"] == today for p in preds)))
        self.stat["today"][1].configure(text=f"{opened} predictions still open")
        self.stat["hit"][0].configure(text=f"{hit / (hit + miss) * 100:.0f}%" if hit + miss else "—",
                                      foreground=P["text"])
        self.stat["hit"][1].configure(text=f"{hit} hit  ·  {miss} missed" if hit + miss else "graded after 7 sessions")
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
        except ValueError:
            messagebox.showerror("Settings", "Score, distance, price, port and max emails must be numbers.")
            return None
        if cfg["max_dist_pct"] <= 0:
            messagebox.showerror("Settings", "Max % below 20-day high must be greater than 0.")
            return None
        self.cfg = cfg
        save_config(cfg)
        return cfg

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
                    self.status_var.set(f"Downloading daily history… {ev[1]}/{ev[2]}")
                elif kind == "rows":
                    self._show_rows(*ev[1:])
                elif kind == "backtest":
                    self._refresh_stats()
                    messagebox.showinfo("Backtest", ev[1])
                elif kind == "notify":
                    self._notify(ev[1])
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
        if rows:
            self.empty_label.place_forget()
        else:
            self.empty_label.configure(text="No stocks qualify right now.\nTry lowering the min score or widening the distance.")
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        self._refresh_stats()

    def _notify(self, count):
        self.bell()
        if self.state() == "iconic":
            self.deiconify()
        self.attributes("-topmost", True)
        self.after(1500, lambda: self.attributes("-topmost", False))
        self.title(f"({count} new) {self.TITLE}")
        self.after(10000, lambda: self.title(self.TITLE))

    def _on_close(self):
        if self.engine and self.engine.is_alive():
            self.engine.stop()
        self.destroy()


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--make-icon":
        write_icon(sys.argv[2])
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
