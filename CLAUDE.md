# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Windows desktop app (Tkinter) that scans the Nifty 500 with Upstox market data. It predicts which stocks will close
above their 20-day high within 7 sessions, tracks a breakout → retest → buy-in trade for each predicted breakout, and
grades its own calls. Optional Firebase accounts sync each user's data. README.md is the user-facing spec: scoring
table, buy-in rules, emails, accounts, privacy.

## Hard constraints

- **Identity: this is a personal project of sandeepbhuvan@gmail.com only.** Never associate it with the owner's work
  email or any work account in any manner: commits, git config, code, docs, Firebase, Gemini, Upstox, GitHub, emails.
  The repo's local `git config user.email` must stay `sandeepbhuvan@gmail.com` (the machine's global one is the work
  address). Any API key or account the app needs is created by the owner under that Gmail account.

- **Python standard library only.** No pip dependencies: Tkinter, urllib, smtplib, ctypes, zlib. Firebase is called
  over REST, not the SDK. The tray icon and screenshot capture are raw Win32 through ctypes. Keep it that way.
- On this machine `python` is a broken WindowsApps alias. Use `py` (or `pyw` for no console). Python 3.14.
- The Upstox token, SMTP password and Gemini API key live in memory only. They are never written to disk or sent to
  Firebase. They can be prefilled from `UPSTOX_ACCESS_TOKEN`, `HHLL_SMTP_PASSWORD` and `GEMINI_API_KEY`.
- `legacy/` (the original Apps Script, which contains a third party's email) and `firebase_config.json` are git-ignored
  and must stay out of commits.
- Upstox calls must send `APP_USER_AGENT`. Without it, Cloudflare returns 403 / error 1010.

## Commands

```powershell
pyw breakout_predictor_app.py                                   # run the app
py -m py_compile breakout_predictor_app.py                      # syntax check (there is no linter config)
py tools/make_screenshots.py                                    # regenerate docs/images from demo data
powershell -ExecutionPolicy Bypass -File install_shortcut.ps1   # desktop shortcut
py breakout_predictor_app.py --make-icon out.ico                # write the generated icon
```

The repo has no test suite. To test, write a throwaway script that does the following:
1. Import the module.
2. Point `bp.APP_DIR` at a scratch folder and set `bp.load_firebase_config = lambda: None`, so it never touches
   `~/.breakout_predictor` or the shared Firebase project.
3. Build synthetic candle dicts (`{"d","o","h","l","c","v"}` lists).
4. Call the pure functions, or drive `bp.App()` directly.

`tools/make_screenshots.py` shows this pattern, including filling every tab with demo rows. Synthetic
`event_generate("<Return>")` doesn't reach Tk dialogs; call the button commands instead.

## Architecture

Everything is in `breakout_predictor_app.py`, split into `# ====` sections in this order: utils → indicators/scoring →
trade simulation → Upstox → data → prediction log → Firebase → email → engine → icon → UI.

**Threads and the UI.** `Engine(threading.Thread)` runs in one of three modes: `live` (a scan every minute in market
hours), `dry` (one scan) or `backtest`. It never touches Tk. It pushes tuples onto `App.events`, and
`App._drain_events` (polled every 200 ms) applies them on the Tk thread. Event kinds: log, status, rows, stats,
progress, prices, notify, backtest, error, auth_error, stopped, cloud, tray. The `CloudAccount` push thread and
`Tray` report through the same queue. Any new background work should do the same.

**One rule set, three users.** `setup_components(S, t, rs)` / `compute_series` produce the score (v2, see
`SCORE_VERSION`) and the breakout level R (the 20-day high). `rs` is `rs_percentile` of the stock's 60-session return
against every stock on the same day: `returns_by_date` in the backtest, and the live quotes in `scan_once`, which
appends today's bar from the quote before scoring. Changing the score's meaning means bumping `SCORE_VERSION`: saved
`min_score` resets once and old synced settings are ignored. Research scripts that justified v2 aren't in the repo. They are used by:
- the live scan (with quotes as today's bar)
- the walk-forward `backtest()` (writes `backtest.json`, compared against a baseline of every stock near its high)
- prediction logging

Changing the scoring changes all three.

**Data flow.**
- `prepare` loads the universe (Nifty 500 CSV from niftyindices, with a GitHub fallback), mapped to Upstox instrument
  keys.
- `build_history` caches about a year of daily candles per day in `history_DATE.json`. `build_weekly` caches weekly
  closes per ISO week for the 200-week SMA filter, which uses completed weeks only.
- `grade_predictions` resolves old predictions: a hit is a close > R within `HORIZON` sessions.
- `build_trades` rebuilds every buy-in from completed candles with `simulate_trade` (constants `RETEST_*`, `ENTRY_*`,
  `MAX_HOLD`, `STOP_BUFFER`; each record stores its `stop_buffer`, and an entered trade keeps its own; a day that touches both stop and target counts as the stop; gaps fill at the open). The
  rebuild is the official record.
- `check_trades_live` only sends intraday alerts. Its `alert_*` flags must survive rebuilds.

**News sentiment.**
- `NewsDesk` (used by `Engine.scan_once` when the app sets `engine.news`) fetches Google News RSS (`google_news`).
  It keeps headlines that name the company (`mentions`), then makes one `gemini_json` call per sector (key in the
  `x-goog-api-key` header).
- Results are cached in `APP_DIR/news_DATE.json`.
- The rows get `news` (the combined score), the optional `news_filter` drops rows at or below `NEG_NEWS`, and
  predictions store `news`.
- News is deliberately not part of the score: it can't be backtested.
- Tests point `GEMINI_API` at a local fake server.

**User data and merging.**
- `predictions.json` and `trades.json` live in `data_dir()`: `APP_DIR/users/<uid>/` when signed in, else `APP_DIR`.
- Trades carry app fields and user `my_*` fields (manual buy/sell).
- `merge_trades` takes app fields from the most advanced copy (`trade_rank`) and `my_*` fields from the newest
  `my_updated`.
- `build_trades` must preserve `alert_*` and `my_*` fields. Any new user-editable field needs the same treatment in
  `MY_FIELDS` and the merge.

**Firebase.**
- `CloudAccount` uses Identity Toolkit and Secure Token over REST, and Firestore documents through
  `fs_encode`/`fs_decode` with `:commit` batches.
- Collections: `users/{uid}/predictions`, `users/{uid}/trades`; settings are a field on `users/{uid}`.
- `BUILTIN_FIREBASE` is the owner's shared project, which every copy uses. A local `firebase_config.json` overrides
  it.
- `firestore.rules` grants each uid only its own subtree. A new collection outside `users/{uid}` needs a rules change
  that the owner republishes.
- Only `DEFAULT_CONFIG` keys are saved and synced as settings.

**UI.**
- `App(tk.Tk)` uses the clam theme with the dark palette `P`. Sizes are multiplied by `self.k`, the display scale
  (1.25 on the owner's 125% screen). Check layouts at that scale, because the sidebar and header are tight.
- Tabs: Live setups, Buy-ins, Track record.
- The `?` button and F1 open a guide built from `App.HELP`, which shows images from `docs/images`. If the UI changes,
  update `HELP`, the README "How to use" section and the screenshots together.
- `Tray` puts a notification-area icon on its own Win32 message loop. With `close_to_tray`, the window's X hides the
  app; tray → Exit quits. A second launch finds the running copy through the tray window class and opens it.
- New setups must never raise or focus the window: `_notify` only chimes and updates the title.

## Workflow notes

- The owner asks for a push to GitHub (`main`) after each tested feature. Commit through a Bash heredoc
  (`git commit -F - <<'EOF'`), because PowerShell 5.1 here-strings break with git.
- Screenshots must be captured with PrintWindow from the app's own window, as `make_screenshots.py` does. Never use
  screen grabs: an earlier one caught the user's browser.
