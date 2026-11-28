"""Regenerate the guide screenshots in docs/images from demo data (Windows only).

    py tools/make_screenshots.py

Runs the app against a throwaway data folder with made-up stocks, so no account, token or real data appears.
Each image is captured from the app's own window (PrintWindow), never from the screen, so other windows can't
leak into a screenshot. Crops come from the widgets' own positions.
"""
import ctypes
import json
import os
import struct
import sys
import tempfile
import zlib
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
sys.path.insert(0, str(ROOT))
for var in ("UPSTOX_ACCESS_TOKEN", "HHLL_SMTP_PASSWORD"):
    os.environ.pop(var, None)

import breakout_predictor_app as bp  # noqa: E402

bp.APP_DIR = Path(tempfile.mkdtemp(prefix="bp_shots_"))
bp.load_firebase_config = lambda: None  # signed-out demo: never touches the cloud
bp.today_str = lambda: "2026-11-20"

# ------------------------------------------------------------------ capture (PrintWindow -> PNG, stdlib only)
user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
for fn, args, res in [(user32.GetDC, [wintypes.HWND], wintypes.HDC),
                      (user32.ReleaseDC, [wintypes.HWND, wintypes.HDC], ctypes.c_int),
                      (user32.GetClientRect, [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
                      (user32.PrintWindow, [wintypes.HWND, wintypes.HDC, wintypes.UINT], wintypes.BOOL),
                      (gdi32.CreateCompatibleDC, [wintypes.HDC], wintypes.HDC),
                      (gdi32.CreateCompatibleBitmap, [wintypes.HDC, ctypes.c_int, ctypes.c_int], wintypes.HBITMAP),
                      (gdi32.SelectObject, [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ),
                      (gdi32.GetDIBits, [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                         ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT], ctypes.c_int),
                      (gdi32.DeleteObject, [wintypes.HGDIOBJ], wintypes.BOOL),
                      (gdi32.DeleteDC, [wintypes.HDC], wintypes.BOOL)]:
    fn.argtypes, fn.restype = args, res


def grab(win):
    """Client area of a Tk window as (width, height, BGRA bytes)."""
    win.update()
    hwnd = int(win.wm_frame(), 16)
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    w, h = r.right, r.bottom
    hdc = user32.GetDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    user32.PrintWindow(hwnd, mdc, 3)  # PW_CLIENTONLY | PW_RENDERFULLCONTENT
    header = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    bi = ctypes.create_string_buffer(header + b"\0" * 16)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, bi, 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)
    return w, h, buf.raw


def save_png(shot, path, box=None):
    w, h, raw = shot
    x0, y0, x1, y1 = box or (0, 0, w, h)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    rows = []
    for y in range(y0, y1):
        line = raw[(y * w + x0) * 4:(y * w + x1) * 4]
        rgb = bytearray(len(line) // 4 * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = line[2::4], line[1::4], line[0::4]
        rows.append(b"\0" + bytes(rgb))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", x1 - x0, y1 - y0, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))
    path.write_bytes(png)
    print(f"  {path.name:18} {x1 - x0}x{y1 - y0}")


def box(win, *widgets, pad=10):
    """Bounding box of widgets in the window's client coordinates, with a margin."""
    win.update_idletasks()
    bx, by = win.winfo_rootx(), win.winfo_rooty()
    xs = [w.winfo_rootx() - bx for w in widgets]
    ys = [w.winfo_rooty() - by for w in widgets]
    xe = [w.winfo_rootx() - bx + w.winfo_width() for w in widgets]
    ye = [w.winfo_rooty() - by + w.winfo_height() for w in widgets]
    return min(xs) - pad, min(ys) - pad, max(xe) + pad, max(ye) + pad


# ------------------------------------------------------------------ demo data (made-up companies)
LIVE = [
    ("ACMEPOWER", "Power", 412.35, 418.90, 1.56, 88, ["stronger than 94% of stocks", "up 7% in 5 days", "uptrend"]),
    ("NOVACHEM", "Chemicals", 1284.10, 1302.00, 1.37, 84, ["stronger than 88% of stocks", "near 1-year high"]),
    ("ORBITFIN", "Financial Services", 96.80, 98.75, 1.97, 79, ["up 5% in 5 days", "uptrend", "accumulation"]),
    ("TERRAINFRA", "Construction", 2210.00, 2264.50, 2.41, 76, ["stronger than 81% of stocks", "uptrend"]),
    ("HELIXPHARMA", "Healthcare", 731.45, 748.00, 2.21, 73, ["near 1-year high", "accumulation"]),
    ("ZENITHAUTO", "Automobile", 5122.00, 5260.00, 2.62, 71, ["uptrend", "up 4% in 5 days"]),
]
TRADES = [
    {"id": "t1", "symbol": "QUARTZTECH", "breakout_date": "2026-10-20", "R": 1872.0, "retest_date": "2026-10-23",
     "entry_date": "2026-10-24", "entry_price": 1890.0, "stop": 1846.0, "target": 2079.0, "status": "win",
     "return_pct": 10.0, "exit_date": "2026-11-04", "my_buy_price": 1881.0, "my_buy_time": "2026-10-24 09:40",
     "my_sell_price": 2100.0, "my_sell_time": "2026-11-04 13:10"},
    {"id": "t2", "symbol": "LUMENRETAIL", "breakout_date": "2026-10-28", "R": 624.9, "retest_date": "2026-10-30",
     "entry_date": "2026-11-02", "entry_price": 631.0, "stop": 612.4, "target": 694.1, "status": "loss",
     "return_pct": -2.95},
    {"id": "t3", "symbol": "NOVACHEM", "breakout_date": "2026-11-12", "R": 1210.0, "retest_date": "2026-11-17",
     "entry_date": "2026-11-18", "entry_price": 1224.0, "stop": 1188.0, "target": 1346.4, "status": "entered",
     "last_close": 1284.1, "last_date": "2026-11-20", "my_buy_price": 1219.5, "my_buy_time": "2026-11-17 14:55"},
    {"id": "t4", "symbol": "CEDARFOODS", "breakout_date": "2026-11-18", "R": 251.2, "retest_date": "2026-11-20",
     "buy_above": 256.4, "stop": 247.5, "status": "retesting"},
    {"id": "t5", "symbol": "SOLARISGAS", "breakout_date": "2026-11-20", "R": 980.0, "status": "waiting_retest"},
    {"id": "t6", "symbol": "PIXELMEDIA", "breakout_date": "2026-11-03", "R": 410.0, "status": "no_retest"},
    {"id": "t7", "symbol": "ARCADIAMET", "breakout_date": "2026-10-14", "R": 318.0, "retest_date": "2026-10-16",
     "entry_date": "2026-10-17", "entry_price": 322.5, "stop": 311.6, "target": 354.75, "status": "win",
     "return_pct": 10.0, "exit_date": "2026-10-29"},
]
PREDS = [
    {"date": "2026-11-20", "time": "10:42", "symbol": "ACMEPOWER", "score": 88, "price": 412.35, "R": 418.9, "status": "open"},
    {"date": "2026-11-20", "time": "10:42", "symbol": "ORBITFIN", "score": 79, "price": 96.8, "R": 98.75, "status": "open"},
    {"date": "2026-11-19", "time": "11:05", "symbol": "SOLARISGAS", "score": 81, "price": 962.0, "R": 975.0, "status": "hit",
     "resolved": "2026-11-20", "sessions": 2, "resolved_close": 991.2},
    {"date": "2026-11-17", "time": "09:58", "symbol": "CEDARFOODS", "score": 77, "price": 246.1, "R": 249.0, "status": "hit",
     "resolved": "2026-11-18", "sessions": 2, "resolved_close": 252.4},
    {"date": "2026-11-10", "time": "13:20", "symbol": "NOVACHEM", "score": 83, "price": 1188.0, "R": 1205.0, "status": "hit",
     "resolved": "2026-11-12", "sessions": 3, "resolved_close": 1214.6},
    {"date": "2026-11-05", "time": "10:15", "symbol": "IRISTEXTILE", "score": 69, "price": 152.4, "R": 157.0, "status": "miss",
     "resolved": "2026-11-13", "sessions": 7, "resolved_close": 149.9},
    {"date": "2026-10-30", "time": "14:02", "symbol": "PIXELMEDIA", "score": 72, "price": 401.0, "R": 405.5, "status": "hit",
     "resolved": "2026-11-03", "sessions": 3, "resolved_close": 412.3},
    {"date": "2026-10-24", "time": "11:47", "symbol": "LUMENRETAIL", "score": 75, "price": 611.0, "R": 620.0, "status": "hit",
     "resolved": "2026-10-28", "sessions": 3, "resolved_close": 627.8},
    {"date": "2026-10-16", "time": "09:31", "symbol": "VERTEXLOG", "score": 66, "price": 88.4, "R": 91.2, "status": "miss",
     "resolved": "2026-10-27", "sessions": 7, "resolved_close": 86.1},
]
bp.write_json(bp.APP_DIR / "trades.json", TRADES)
bp.write_json(bp.APP_DIR / "predictions.json", PREDS)
bp.write_json(bp.APP_DIR / "backtest.json", {"baseline": 38.0, "setups": 52.5, "buyin_win": 46.0,
                                              "buyin_avg": 2.4, "buyin_closed": 61})
LOG = ["10:41:58  Universe: 495 Nifty 500 stocks",
       "10:42:01  History up to date (cached today)",
       "10:42:06  Scan 10:42 · 3 quote calls · 6 setups (2 new)",
       "10:42:06  Email sent: 2 new likely breakouts",
       "10:43:05  Scan 10:43 · 6 setups (0 new)"]

# ------------------------------------------------------------------ shoot
OUT.mkdir(parents=True, exist_ok=True)
app = bp.App()
# Must stay fully on screen: PrintWindow leaves off-screen pixels blank.
app.geometry("1880x1000+0+0")
NEWS = {"ACMEPOWER": 0.62, "NOVACHEM": 0.18, "ORBITFIN": 0.0, "TERRAINFRA": 0.41, "HELIXPHARMA": -0.08,
        "ZENITHAUTO": 0.27}
# made-up headlines for the made-up stocks, so the news window has something to show
bp.write_json(bp.APP_DIR / f"news_{bp.today_str()}.json", {
    "stocks": {"ACMEPOWER": {"score": 0.62, "stock_score": 0.8, "sector": "Power", "time": "10:42",
                             "summary": "New capacity orders and a strong quarter point to higher earnings.",
                             "headlines": [{"title": t, "source": src, "link": "", "date": d} for t, src, d in [
                                 ("Acme Power wins 1.2 GW solar order from state utility", "Demo Business Daily",
                                  "2026-11-20"),
                                 ("Acme Power Q2 profit rises 28% on higher plant load", "Demo Markets", "2026-11-18"),
                                 ("Brokerage raises Acme Power target price on order book", "Demo Money",
                                  "2026-11-17")]]}},
    "sectors": {"Power": {"score": 0.2, "summary": "Steady demand growth; power stocks near highs.",
                          "headlines": [{"title": t, "source": src, "link": "", "date": d} for t, src, d in [
                              ("India's peak power demand hits record in November", "Demo Energy News", "2026-11-19"),
                              ("Power stocks extend rally as demand outlook improves", "Demo Markets",
                               "2026-11-18")]]}}})
app.cfg["news_enabled"] = True
app.gemini_key_var.set("demo")
app._update_news_btn()
app._show_rows([{"symbol": s, "industry": ind, "ltp": ltp, "R": r, "dist": d, "score": sc, "reasons": why,
                 "news": NEWS.get(s)} for s, ind, ltp, r, d, sc, why in LIVE], {"ACMEPOWER", "ORBITFIN"},
               {"NOVACHEM": "Nov 18", "TERRAINFRA": "Nov 19", "HELIXPHARMA": "Nov 19", "ZENITHAUTO": "Nov 20"})
for line in LOG:
    app._log(line.split("  ", 1)[1])
app._set_pill("live")
app._main_btn_running()
app.status_var.set("Live · next scan in 54s · 6 setups · market closes 15:30")


def card_of(widget):
    """The bordered card (outer frame) a widget sits in."""
    w = widget
    while w is not None and not (isinstance(w, bp.tk.Frame) and str(w.cget("highlightthickness")) == "1"):
        w = w.master
    return w


def step1():
    shot = grab(app)
    save_png(shot, OUT / "overview.png")
    side_cards = card_of(app.main_btn).master.winfo_children()
    for name, card in zip(("connection", "rules"), side_cards):
        save_png(shot, OUT / f"{name}.png", box(app, card))
    save_png(shot, OUT / "stats.png", box(app, card_of(app.stat["now"][0]).master))
    save_png(shot, OUT / "live.png", box(app, card_of(app.tab_btns["live"])))
    save_png(shot, OUT / "activity.png", box(app, card_of(app.copy_btn)))
    save_png(shot, OUT / "header.png", box(app, app.sync_label, app.account_btn, app.news_btn, app.email_hint,
                                           app.pill, app.help_btn, pad=14))
    app._main_btn_idle()  # the guide explains the buttons as they look before starting
    save_png(grab(app), OUT / "actions.png", box(app, side_cards[2]))
    app._main_btn_running()
    app._show_tab("buyins")
    app.after(500, step2)


def step2():
    shot = grab(app)
    save_png(shot, OUT / "buyins.png", box(app, card_of(app.tab_btns["live"])))
    app.buyins.selection_set("t3")
    app.open_my_trade_dialog()
    app.after(600, step3)


def step3():
    dlg = app.winfo_children()[-1]
    save_png(grab(dlg), OUT / "mytrade.png")
    dlg.destroy()
    app._show_tab("track")
    app.after(500, step4)


def step4():
    save_png(grab(app), OUT / "track.png", box(app, card_of(app.tab_btns["live"])))
    app._show_tab("live")
    app.tree.selection_set(app.tree.get_children()[0])
    app.open_news_window()
    app.after(600, step4b)


def step4b():
    win = app.winfo_children()[-1]
    save_png(grab(win), OUT / "news.png")
    win.destroy()
    app.gemini_key_var.set("")
    app.open_news_dialog()
    app.after(600, step4c)


def step4c():
    dlg = app.winfo_children()[-1]
    save_png(grab(dlg), OUT / "news_settings.png")
    dlg.destroy()
    app.open_help()
    app.after(800, step5)


def step5():
    loaded = len(app.help_win.images)
    expected = sum(1 for kind, _ in app.HELP if kind == "img")
    print(f"help window: {loaded}/{expected} images loaded")
    save_png(grab(app.help_win), Path(tempfile.gettempdir()) / "bp_help_window.png")
    app.destroy()


app.after(1500, step1)
app.mainloop()
print("data folder used:", bp.APP_DIR)
