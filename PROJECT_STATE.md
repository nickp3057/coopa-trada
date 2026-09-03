# Project state — Coopa Trada

_Last updated: 2026-09-03_

## What it is

A Python program that places and manages **single-leg option orders** through the
Charles Schwab Trader API, driven by Cooper's "FOMC Picks" signal lists.
Dry-run by default; live trading needs `--live` **and** `LIVE_TRADING=1`.

## Status

- **74 tests passing** (`pytest -q`). No known failures.
- Git: branch `master`, 2 commits, no remote.
  - `95d768c` initial skeleton + real signal parser + notation-error surfacing
  - `beba7c0` sizing locked to 1 contract per pick
- Not yet run against a live Schwab token — dry-run / synthetic paths only.

## Modules (`coopa_trada/`)

| file | role |
|---|---|
| `__main__.py` | CLI: `run` \| `reconcile` \| `resolve` |
| `config.py` | env loading, DRY-RUN vs LIVE resolution |
| `clock.py` | tz helpers; naive-datetime guard |
| `calendar_svc.py` | `exchange_calendars` XNYS wrapper (real opens/closes, half days) |
| `models.py` | `Signal`, `SignalSource`, `CsvSignalSource` (real parser), asset-class lookup |
| `contracts.py` | `Signal` → resolved OSI contract; `ContractResolver` + offline `SyntheticResolver` |
| `rules.py` | YAML → `Rules`; `PositionSizer` protocol + `FixedQuantitySizer` |
| `exits.py` | `ExitStrategy` protocol + `StopLoss` / `TakeProfit` / `TrailingPeak` / `HardTimeExit` + `CompositeExit` |
| `engine.py` | scheduler / reconcile / entry pass / exit loop |
| `broker.py` | `Broker` protocol; `SchwabBroker`, `DryRunBroker` |
| `store.py` | SQLite trade log (signals, orders, fills, exit_events) |
| `idempotency.py` | `sha256(signal_id|INTENT|YYYY-MM-DD)`, UNIQUE in DB |
| `ratelimit.py` | token bucket ~120 req/min |
| `logs.py` | logging + loud MODE banner |

## Settled decisions

- **Signal format** — Cooper's list is parsed for real: expiration from the
  title line, `SYMBOL | OPTION | LAST PRICE | TVT-DELTA | SCHWAB DELTA` table.
  `OPTION` = `{strike}{C|P}` (permanent format). A non-matching row is surfaced
  loudly — per-row `WARNING`, batch-level `ERROR`, `[E_SIGNAL_NOTATION]` count in
  `resolve` output — then skipped without crashing the batch.
- **Asset class** — explicit hand-edited `config/asset_classes.yaml` lookup, no
  heuristic. Unknown ticker → skip + warn (`UnknownAssetClass`).
- **Position sizing** — always **1 contract per pick**. `max_dollars_per_position`
  is a pure skip gate: if 1 contract costs more than the cap, skip the ticker;
  otherwise buy 1, never more. `FixedQuantitySizer`. No contract-count or
  per-ticker quantity scheme.
- **Delta fields** — `signal_delta_tvt` / `signal_delta_schwab` recorded, not
  filtered on.

## Open / pending Cooper

- **Dollar-cap figure** — `max_dollars_per_position: 1500` in
  `config/rules.example.yaml` is a placeholder; confirm the real number before
  going live.
- **"Sell if it drops 10%"** — ambiguous between 10% off session peak
  (`TrailingPeak`) and 10% off entry (`StopLoss`). Both wired via
  `cooper_drop_from_peak_pct` / `cooper_drop_from_entry_pct` (optional keys),
  both left active — `CompositeExit` fires whichever triggers first. Also
  unconfirmed whether 10% *replaces* the existing 25% / 40% / 60% exits.
- **Delta filtering** — unknown whether Cooper pre-filters upstream or we must.

## Out of scope

Multi-leg / spreads · strike-or-expiration selection heuristics · non-CSV signal
sources · multi-expiration-per-list · delta-based filtering logic · backtesting
harness.

## Next steps

1. Get the real dollar cap and the trailing-stop interpretation from Cooper.
2. One-time interactive Schwab token login; smoke-test `resolve` and
   `reconcile` against the live API.
3. Dry-run a full session end-to-end on a real trading day before enabling
   `LIVE_TRADING=1`.
