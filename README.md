# Coopa Trada

Skeleton of a Python program that places and manages **single-leg option orders**
through the Charles Schwab Trader API from external trade signals.

Design stance:

- **The signal format is Cooper's "FOMC Picks" list.** `CsvSignalSource` parses it
  behind a clean interface (`SignalSource`): a title line carrying the shared
  expiration, then a `SYMBOL | OPTION | LAST PRICE | TVT-DELTA | SCHWAB DELTA`
  table. A row whose `OPTION` cell isn't `{strike}{C|P}`, or whose ticker isn't in
  `config/asset_classes.yaml`, is skipped with a warning — never guessed.
- **Exit logic is the heart of the program.** `coopa_trada/exits.py` is pure — no I/O,
  no clock, no network — and is proven against synthetic price series in
  `tests/test_exits.py` before any live quote can reach it.
- **Dry run is the default.** Live trading requires two independent opt-ins:
  `--live` on the CLI **and** `LIVE_TRADING=1` in the environment. Either alone stays
  DRY-RUN.
- **No naive datetimes, ever.** Everything tz-aware; stored as ISO-8601 UTC. Market
  logic uses `America/New_York`; human output uses `America/Denver`.
- **No hardcoded 9:30 / 16:00.** Session opens/closes (half days, holidays) come from
  `exchange_calendars` (`XNYS`).
- **Every order carries an idempotency key.** `sha256(signal_id|INTENT|YYYY-MM-DD)`,
  UNIQUE in the DB. A re-run collides instead of double-ordering.
- **Reconcile at startup.** Schwab is treated as the source of truth; local rows that
  diverge are flagged, not trusted. In dry-run, reconcile is a logged no-op.
- **The trade log answers everything.** Every broker call persists its full request and
  response as JSON. `tests/test_store.py` reconstructs a round trip from the DB alone.

## Layout

```
coopa_trada/
  __main__.py     argparse: run | reconcile | resolve
  config.py       env loading + DRY-RUN vs LIVE resolution
  logs.py         logging + loud MODE banner
  clock.py        tz helpers; naive-datetime guard
  calendar_svc.py exchange_calendars XNYS wrapper
  idempotency.py  make_key(signal_id, intent, date)
  models.py       Signal, SignalSource, CsvSignalSource (+ config/asset_classes.yaml lookup)
  contracts.py    Signal -> resolved OSI contract (+ offline SyntheticResolver)
  store.py        SQLite trade log
  rules.py        YAML -> Rules
  exits.py        ExitStrategy protocol + 4 strategies + CompositeExit
  broker.py       Broker protocol; SchwabBroker, DryRunBroker
  ratelimit.py    token bucket ~120 req/min
  engine.py       scheduler / reconcile / entry / exit loop
```

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

cp .env.example .env                              # leave creds blank for dry-run
cp config/rules.example.yaml config/rules.yaml
pytest -q                                         # all green
```

## One-time token login

The program assumes an OAuth token file has already been minted by a one-time
interactive login (a browser round trip you run once on a machine you control).

```bash
python - <<'PY'
from schwab.auth import client_from_login_flow
client_from_login_flow(
    api_key="<SCHWAB_API_KEY>",
    app_secret="<SCHWAB_APP_SECRET>",
    callback_url="https://127.0.0.1:8182",
    token_path="./schwab_token.json",
)
PY
```

Point `SCHWAB_TOKEN_PATH` at the resulting file. `schwab-py` refreshes it in place.
(Exact helper names are confirmed against the installed `schwab-py` version.)

## Run modes

### Dry run (default) — no network, no token needed

```bash
python -m coopa_trada run --once \
  --date 2026-09-02 \
  --signals data/signals.example.csv \
  --db ./trada.db
```

Prints the `MODE = DRY-RUN` banner, creates `trada.db`, resolves contracts with the
offline `SyntheticResolver`, places a simulated entry **at the ask**, runs one exit
tick, and returns.

Inspect the log:

```bash
sqlite3 ./trada.db \
  "SELECT o.osi_symbol, o.side, f.price, f.filled_at_utc
     FROM orders o JOIN fills f ON f.order_id = o.id;"
```

Re-run the exact command → idempotency-key collision, **no duplicate order rows**.

### resolve — read-only, needs a token

```bash
python -m coopa_trada resolve --signals data/signals.example.csv
```

With no token it fails loudly with a clear "no Schwab token" message (not a
traceback). With a token it prints each signal's OSI symbol + bid/ask/mid/OI, or the
`ContractUnderspecified` / `ContractNotTradable` reason.

### reconcile

```bash
python -m coopa_trada reconcile
```

With a real `.env` + token: prints live Schwab positions/orders vs the local trade
log, flagging every divergence. In dry-run it logs `simulated` and does nothing.

### Live

```bash
LIVE_TRADING=1 python -m coopa_trada run --live --date 2026-09-02
```

`run --live` **without** `LIVE_TRADING=1` still logs `MODE = DRY-RUN`.

## Pending confirmation from Cooper

Both original questions are now **resolved**. Only a residual on each remains.

1. **Position sizing — resolved: always 1 contract per pick; the dollar cap is a
   pure skip gate.** Cooper's rule is a flat "buy 1 per pick," never more.
   `max_dollars_per_position` is a spending gate, not a sizing formula: if 1
   contract (`mid * 100`) would cost more than it, `FixedQuantitySizer` returns
   `0` and the ticker is **skipped entirely**; otherwise it returns `1`, however
   cheap the contract. Only sizer; no contract-count or per-ticker quantity
   scheme. *Residual:* the cap figure in `rules.example.yaml` (1500) is still a
   placeholder — confirm the real number with Cooper before going live.

2. **Contract notation — resolved: `{strike}{C|P}` is permanent.** It will not
   change, so there is no new parser branch. A row that fails the pattern is
   surfaced loudly, not silently dropped: a per-row `WARNING`, a batch-level
   `ERROR` summary from `CsvSignalSource.fetch`, and a `[E_SIGNAL_NOTATION]` line
   plus skipped-row count in `resolve` output (`UnrecognizedContractNotation.code`).
   The regex (`^(\d+\.?\d*)([CP])$`, case-insensitive) and the skip-and-continue
   behavior are unchanged.

Still unconfirmed:

- **"Sell if it drops 10% between the times I said."** Could mean 10% off the
  session peak (`TrailingPeak`) or 10% off entry (`StopLoss`). `rules.example.yaml`
  wires both — `cooper_drop_from_peak_pct` and `cooper_drop_from_entry_pct` — and
  leaves both active; `CompositeExit` fires whichever triggers first. It's also
  unconfirmed whether this 10% *replaces* the existing 25% / 40% / 60% exits, so
  all of those keys coexist for now. These two keys look redundant on purpose.
- **Delta filtering.** `TVT-DELTA` / `SCHWAB DELTA` are captured on the `Signal`
  (`signal_delta_tvt`, `signal_delta_schwab`) but not filtered on anywhere —
  unknown whether Cooper pre-filters upstream or we're meant to.

## Out of scope (stubbed)

Delta-based filtering logic · multi-expiration-per-list handling · any
`SignalSource` beyond CSV · strike/expiration selection heuristics (raise
`ContractUnderspecified`) · multi-leg / spreads · backtesting harness.

Position sizing is settled (`FixedQuantitySizer`: always 1 contract per pick,
`max_dollars_per_position` a pure skip gate); there is no contract-count or
per-ticker quantity sizer, planned or otherwise.
