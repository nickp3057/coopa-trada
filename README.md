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

Image recognition runs locally using Apple Vision through a bundled Swift helper.
It currently requires **macOS and the Swift toolchain** (Xcode or Command Line
Tools). CSV works on other supported Python platforms. Recognition is designed
for the five-column screenshot format, and can misread other layouts; the editable
preview is required before selecting an uploaded list. The supplied screenshot
was checked against all eight rows, including decimal strikes and negative deltas.
Images are discarded after recognition; only confirmed CSV files are retained.

Editing text or dates invalidates the preview. Signed previews expire after an
hour or server restart. Selected uploads persist on disk, but selection itself
is per server session; reselect after restarting. A running job keeps its original
file paths, and selection changes are blocked until it stops.

The original CSV-path endpoint and CLI remain available for already-prepared CSVs.
The sample `data/signals.example.csv` matches the supplied screenshot exactly.

## Trading behavior

- Always buy **one contract per eligible pick**. The spending cap is a skip gate,
  calculated from the **ask × 100** (excluding commissions/fees). It never increases
  the quantity for cheap options. Pending entry orders reserve position slots.
- `OPTION` is `{strike}{C|P}`, e.g. `44P` or `322.5C`. Tickers must appear in
  `config/asset_classes.yaml`. CLI ingestion skips invalid notation/unknown tickers
  with warnings; upload preview rejects the entire list until corrected.
- LAST PRICE is reference information. Live orders use fresh broker quotes;
  simulation uses deterministic generated quotes, not historical market data.
- Entries are limit buys at the ask, scheduled relative to the exchange close.
  Exits are limit sells at the bid, evaluated during each asset class's configured
  exit window. An exit order being submitted does **not** guarantee it fills.
- Stop loss, take profit, trailing peak, and time-based exits are configurable.
  Observed peaks are stored per entry and survive polling and app restarts.
- Real session times use `exchange_calendars` (`XNYS`). Internal timestamps are
  timezone-aware UTC; market rules use New York and human output uses Denver.
- Broker order status is polled before each exit tick. Validated execution records
  provide actual fill prices/timestamps, with atomic snapshots preventing duplicate
  fills. A delayed entry is monitored even when there are no filled positions yet.
- Unfilled entries are canceled at their exit deadline, then checked again for fills
  racing with cancellation. Exit orders remain monitored until they settle or the
  market closes. Unsettled exposure at close produces an explicit error.
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

The example cap (`1500`) and exit settings are placeholders. Before live use,
record Cooper's actual cap and selling rules. In particular, clarify whether
“drops 10%” means from entry or from the observed high, and whether it replaces
any of the other exit rules. The example leaves both interpretations enabled for
simulation; live runs reject that ambiguity.

Delta columns are recorded but no delta filter is implemented. Live runs currently
require Cooper to confirm picks are filtered upstream:

```yaml
# Set only after confirming the actual cap and exit settings with Cooper.
live_rules_confirmed: true
delta_policy: already_filtered
```

The default is `live_rules_confirmed: false` and `delta_policy: unconfirmed`.
Saving settings in the dashboard resets confirmation, because it changes the
reviewed rules. If Cooper needs filtering inside this app, implement his exact
criteria before enabling live runs.

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
- No automatic adoption of untracked positions, automatic recovery of ambiguous
  submissions, or automatic repricing/replacement of unfilled exit limits.
- Image extraction supports this table layout on macOS; always review the output.
- Exit triggers are local polling rules, not broker-hosted protective orders.
  Interruptions, quote failures, and unfilled limits require operator attention.

See `PROJECT_STATE.md` for the current validation and remaining decisions.
