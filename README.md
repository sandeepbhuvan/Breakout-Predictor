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
   within 7 sessions. The **Track record** tab lists every prediction with its result, the date it resolved, how
   many sessions it took and the price move, filterable by hits, misses and open.

### Optional 200-week moving-average filter

Tick **Only stocks above 200-week MA** to drop setups trading below their 200-week simple moving average (a
long-term trend filter). It needs ~5 years of weekly candles, downloaded once a week. Stocks listed for under
200 weeks have no 200-week MA and are excluded while the filter is on. **Run backtest** always reports setups split
into above / below the 200-week MA, so you can check whether the filter helps before turning it on.

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

Paste your Upstox access token in the app. The read-only **Analytics token** (Upstox developer console → My Apps →
Analytics) is valid for a year and covers everything this app uses; regular trading-app access tokens expire daily.

### Email alerts (optional)

Click **Email alerts** and enter SMTP details. For Gmail use `smtp.gmail.com`, port `465`, and an
[App Password](https://support.google.com/accounts/answer/185833).

## Accounts and cloud sync (optional, Firebase)

Without an account the app stores everything on the PC. With a Firebase project, users can create an account in
the app (**Sign in** in the header) and their predictions and settings sync to Firestore, so they follow them to any
PC. Each user can only read their own data (see `firestore.rules`). Upstox tokens and SMTP passwords are never
uploaded.

One-time setup (free Spark plan is plenty):

1. Go to <https://console.firebase.google.com>, **Add project**, any name (Google Analytics not needed).
2. **Build → Authentication → Get started → Sign-in method → Email/Password → Enable**.
3. **Build → Firestore Database → Create database**, location `asia-south1 (Mumbai)`, start in **production mode**.
4. In Firestore, open the **Rules** tab, replace the contents with [`firestore.rules`](firestore.rules), **Publish**.
5. **Project settings (gear) → General**: copy the **Project ID** and **Web API key**. (If no Web API key shows,
   add a Web app with the `</>` button; you don't need its hosting or SDK snippet.)
6. Copy `firebase_config.example.json` to `firebase_config.json` next to the app and fill in both values.
   `firebase_config.json` is git-ignored. The Web API key only identifies the project; the security rules are what
   protect the data.
7. Restart the app and click **Sign in → Create account**.

## Privacy

The Upstox token and SMTP password are kept in memory only and are never written to disk. They can be prefilled
from the environment variables `UPSTOX_ACCESS_TOKEN` and `HHLL_SMTP_PASSWORD`. Settings, cached history and the
prediction log live in `~/.breakout_predictor` (per account under `users/<uid>` when signed in). When signed in,
`session.json` there holds the Firebase sign-in session so you stay signed in, like a browser cookie; **Sign out**
deletes it.
