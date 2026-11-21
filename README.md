# Breakout Predictor

A Windows desktop app that scans the **Nifty 500** with live **Upstox** data and flags stocks that are
*likely* to break out above their 20-day high within the next **7 trading sessions**, before it happens.
It emails you only when new setups appear.

Pure Python standard library (Tkinter + urllib). Nothing to `pip install`.

## How it works

1. **Once a day** it downloads about a year of daily candles for every Nifty 500 stock from Upstox (throttled,
   cached locally).
2. **Every minute during market hours** (09:15–15:30 IST, Mon–Fri) it makes 3 batched Upstox quote calls and
   scores each stock that sits 0–3% below its 20-day high.
3. Stocks scoring at or above your threshold are shown in the table and, if email is set up, sent in one email.
   Each stock is alerted once while its prediction is open.
4. Every prediction is logged and graded automatically: a **hit** is a daily close above the breakout level
   within 7 sessions.

### Setup score (0–100)

| Points | Signal | Meaning |
| --- | --- | --- |
| 20 | Proximity | Price is close to (but still below) the 20-day high |
| 15 | Contraction | 5-day ATR shrinking vs 20-day ATR |
| 15 | Squeeze | Bollinger bandwidth near its 40-day low |
| 15 | Higher lows | Recent lows rising into resistance |
| 15 | Accumulation | More volume on up days than down days |
| 10 | Trend | Close above 50-day SMA, 20-day SMA above 50-day SMA |
| 10 | Resistance tests | The level has been touched several times |

**Run backtest** replays these exact rules over the past year and compares them with a baseline (every stock
near its 20-day high). Judge the threshold by the *lift* over that baseline, not the raw hit rate. These are
probabilistic setups, not certainties, and nothing here is investment advice.

## Run

```powershell
pyw breakout_predictor_app.py
```

Or create a desktop shortcut:

```powershell
powershell -ExecutionPolicy Bypass -File install_shortcut.ps1
```

Paste your Upstox access token (a read-only Analytics token works) in the app. Upstox tokens expire daily.

### Email alerts (optional)

Click **Email alerts** and enter SMTP details. For Gmail use `smtp.gmail.com`, port `465`, and an
[App Password](https://support.google.com/accounts/answer/185833).

## Privacy

The Upstox token and SMTP password are kept in memory only and are never written to disk. They can be prefilled
from the environment variables `UPSTOX_ACCESS_TOKEN` and `HHLL_SMTP_PASSWORD`. Settings, cached history and the
prediction log live in `~/.breakout_predictor`.
