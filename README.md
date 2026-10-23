# Coopa Trada

A local Python app that reads Cooper's **FOMC Picks** lists and places/manages
single-leg option orders through Charles Schwab. It follows the listed contracts;
it does not generate investment recommendations. Simulation is the default.

## Set up and open the dashboard

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,web]'
cp .env.example .env
cp config/rules.example.yaml config/rules.yaml
pytest
python -m coopa_trada.web
```

Open http://127.0.0.1:8770. Keep the app local; it has no user authentication.
Leave credentials blank for simulation. The `.[web]` extra installs Flask.

### Upload, review, then select picks

1. Choose a CSV, JPEG, or PNG and click **Read file** (8 MB maximum).
2. Check the extracted text against the original. Correct any reading errors.
3. Enter the **full trade date and expiration**, including the year. The image's
   month/day title alone is not sufficient to confirm those dates.
4. Click **Preview picks**. Every row must parse; unknown tickers, invalid
   contracts, nonfinite numbers, duplicate picks, and missing prices are rejected.
5. Check the table, then **Confirm and select these picks**. This saves a reviewed
   CSV beside the database in `imports/`; it does not start trading.
6. Start a dry run separately. The run date is filled from the reviewed list.

Image recognition uses Apple Vision through a bundled Swift helper on a Mac, or
the Claude API anywhere when `ANTHROPIC_API_KEY` is set (the default off the Mac;
the picture is then sent to Anthropic). CSV works everywhere. Recognition is designed
for the five-column screenshot format, and can misread other layouts; the editable
preview is required before selecting an uploaded list. The supplied screenshot
was checked against all eight rows, including decimal strikes and negative deltas.
Images are discarded after recognition; only confirmed CSV files are retained.
Confirmed lists live in `imports/` next to the database; the latest one for a trade
date is that day's list, and a running run picks it up on its next tick.

Editing text or dates invalidates the preview. Signed previews expire after an
hour or server restart. Selected uploads persist on disk, but selection itself
is per server session; reselect after restarting. A running job keeps its original
file paths, and selection changes are blocked until it stops.

The original CSV-path endpoint and CLI remain available for already-prepared CSVs.
The sample `data/signals.example.csv` matches the supplied screenshot exactly.

## Daily auto-start

Optional (`AUTO_START=1` in `.env`; `coopa_trada/scheduler.py`). On XNYS trading days
at `AUTO_START_TIME` (Mountain time, default 07:15, before the 07:30 MT open) the app
starts the day's run through the same runner as the Start button. It refuses, with an
alert, when the Schwab login won't last through the next trading day's exit window,
when the rules file can't be read, or (live only) when `LIVE_TRADING` is off or a
setting is unconfirmed. Practice is the default; a live auto-start also needs
`AUTO_START_LIVE="ENABLE LIVE TRADING"`, and the engine still applies every live gate.
One attempt per day per process; a restart tries again.

## Trading behavior

Plain-English rules with dollar examples: **docs/HOW_IT_TRADES.md**.

- **Buying is triggered by confirming the day's picks** (dashboard or Telegram), inside
  a buying window counted back from the real close (default 1:30–1:57 PM MT; half days
  move with the early close). Picks confirmed after the cutoff are skipped and logged.
- Always buy **one contract per eligible pick**. Picks are ranked by |delta| (Schwab
  live by default) and bought while each fits the **total budget** ($300 for all open
  positions and working buys together, at ask × 100). The **per-pick cap** ($300) is a
  skip gate. There is no position-count limit.
- **Hold logic:** a take-profit sells the same day; otherwise the position is held to
  the next trading day, where every exit applies (from the open by default) and the
  time exit sells at that day's exit-window end.
- **Exits:** take profit, hard stop, trailing stop, Cooper's 10-point profit trail
  (profit % 10 points below its peak), the older 10%-from-peak reading, and the
  next-day time exit. First trigger wins. An unfilled real sell is repriced to the
  current bid after 60 s, up to 5 times (never a market order).
- `OPTION` is `{strike}{C|P}`, e.g. `44P` or `322.5C`. Tickers must appear in
  `config/asset_classes.yaml`. CLI ingestion skips invalid notation/unknown tickers
  with warnings; upload preview rejects the entire list until corrected.
- LAST PRICE is reference information. Live orders use fresh broker quotes;
  simulation uses deterministic generated quotes, not historical market data.
- Entries are limit buys at the ask. Exits are limit sells at the bid, evaluated during each asset class's configured
  exit window. An exit order being submitted does **not** guarantee it fills.
- Stop loss, take profit, trailing peak, and time-based exits are configurable.
  Observed peaks are stored per entry and survive polling and app restarts.
- Real session times use `exchange_calendars` (`XNYS`). Internal timestamps are
  timezone-aware UTC; market rules use New York; the UI, alerts and messages use
  Mountain time (MT).
- Broker order status is polled before each exit tick. Validated execution records
  provide actual fill prices/timestamps, with atomic snapshots preventing duplicate
  fills. A delayed entry is monitored even when there are no filled positions yet.
- Unfilled entries are canceled at their exit deadline, then checked again for fills
  racing with cancellation. Exit orders remain monitored until they settle or the
  market closes. Open orders, or positions not deliberately held overnight, at the
  close produce an explicit error.
- Startup reconciliation compares broker and local positions, quantities, and open
  orders. Divergences block a run; they are not silently adopted or overwritten.
- Simulated/live orders have separate idempotency keys and mode-filtered position
  queries. A live database is bound to one account. Legacy live databases without
  a recorded account binding require manual reconciliation/migration.
- A submitted order's uncertain outcome is never blindly retried. An unresolved
  request without a broker ID blocks further synchronization until investigated.

The dashboard's **Stop** stops monitoring; it does not liquidate positions or
cancel all outstanding broker orders. Treat it as stopping the program, not as an
emergency sell button.

## Cooper's decisions are still required

Every Cooper-facing setting has a marker under `setting_confirmations` in
`config/rules.yaml` (spending cap, 10% stop basis, delta filter, profit-point trail,
entry-day stops, next-day exit start, total budget, delta ranking, exit repricing,
buying window). **Any unconfirmed marker blocks live runs; dry runs are never
blocked.** `Engine.run` enforces this, so it applies to the CLI and the dashboard
alike. Settings shows an Unconfirmed badge and a Confirm checkbox for each, and the
dashboard's live panel lists whatever is still unconfirmed. Changing a confirmed
setting's value resets its marker unless the same save re-confirms it. The questions
for Cooper are at the end of `docs/HOW_IT_TRADES.md`.

## Picture reader and Telegram

- Screenshots are read by Apple Vision on a Mac (local) or by the Claude API
  (`ANTHROPIC_API_KEY`, the default elsewhere); `COOPA_IMAGE_READER=auto|claude|apple`.
  Either way the editable preview and an explicit confirmation come before anything trades.
- Optional Telegram intake (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS`): forward
  the screenshot, get the parsed preview with Confirm / Reject. See
  `docs/TELEGRAM_SETUP.md`.
- `python -m coopa_trada readonly-check` exercises the saved Schwab login against
  read-only endpoints only and prints counts, never account numbers or amounts.

## Double-click app (macOS)

`packaging/build_macos.sh` compiles the Apple Vision helper with `swiftc`, builds
`dist/Coopa Trada.app` with PyInstaller, then runs `packaging/verify_app.py`. That
script launches the built app in a throwaway folder and checks that dry run is the
default and every live gate still holds. The app keeps `.env`, `config/`, the
database and the Schwab token in the folder that contains it. Cooper's guide is
`COOPER_README.md`; Windows notes are in `docs/WINDOWS_BUILD.md`.

## CLI simulation

```bash
python -m coopa_trada run --once \
  --date 2026-09-02 \
  --signals data/signals.example.csv \
  --config config/rules.example.yaml \
  --db /tmp/coopa-demo.db
```

No network or token is needed. `--once` runs one entry pass and exit tick. A full
simulation uses the real clock, not a historical replay. Re-running the same pick,
intent, date and mode reuses the existing order, rather than submitting again.

Input supports the original combined title:

```csv
FOMC Picks for 9/2, 9/4 Expiration
SYMBOL,OPTION,LAST PRICE,TVT-DELTA,SCHWAB DELTA
IBIT,44P,0.71,66.4,-0.554
```

Reviewed uploads use explicit dates, avoiding year inference:

```csv
Trade date: 2026-09-02; Expiration: 2026-09-04
SYMBOL,OPTION,LAST PRICE,TVT-DELTA,SCHWAB DELTA
IBIT,44P,0.71,66.4,-0.554
```

## Schwab connection and live mode

This version has been tested with simulated responses, **not a real Schwab
account**. No real orders were placed during development. Use a dedicated database
and one running process per account. Complete read-only integration checks and
supervised verification before unattended use.

Mint an OAuth token with the interactive helper, using your own app credentials:

```python
from schwab.auth import client_from_login_flow
client_from_login_flow(
    api_key="<SCHWAB_API_KEY>",
    app_secret="<SCHWAB_APP_SECRET>",
    callback_url="https://127.0.0.1:8182",
    token_path="./schwab_token.json",
)
```

Set `SCHWAB_TOKEN_PATH` and `SCHWAB_ACCOUNT_ID` in `.env`. Credentials never belong
in source control. `schwab-py` manages token refresh; the dashboard shows a local
file-age estimate, not proof the server will accept a token.

Read-only commands:

```bash
python -m coopa_trada resolve --signals data/signals.example.csv --date 2026-09-02
LIVE_TRADING=1 python -m coopa_trada reconcile --live --db ./live.db
```

Live trading needs **both** `--live` and `LIVE_TRADING=1`, plus confirmed rules.
The dashboard substitutes its typed confirmation phrase for the CLI flag. Live
runs must use today's market date; `--once` and after-close starts are rejected.
The app never enables the environment opt-in itself.

The broker adapter uses the documented [schwab-py order methods](https://schwab-py.readthedocs.io/en/latest/client.html)
and [single-leg limit order templates](https://schwab-py.readthedocs.io/en/latest/order-templates.html).

## Limits and remaining integration work

- No multi-leg spreads, strike-selection heuristics, or per-row expirations.
- No historical backtesting or live-market paper-trading feed.
- No automatic adoption of untracked positions or automatic recovery of ambiguous
  submissions. Unfilled entries are canceled, not repriced.
- Image extraction targets this table layout; always review the output.
- Exit triggers are local polling rules, not broker-hosted protective orders.
  Interruptions, quote failures, and unfilled limits require operator attention.

See `PROJECT_STATE.md` for the current validation and remaining decisions.
