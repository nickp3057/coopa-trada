# Project state — Coopa Trada

_Last updated: 2026-10-16_

Local Python/Flask app for Cooper's FOMC Picks. CSV and macOS image upload now feed
an editable preview, explicit dates, and a separate confirmation step. Uploading
or confirming a list never starts trading. Simulation remains the default.

## Implemented in this revision

- Poll live order status and import validated executions atomically, including
  delayed fills and actual execution prices/timestamps.
- Keep monitoring pending entries; cancel them at the exit deadline, accounting
  for a fill racing with cancellation. Report unsettled exposure at market close.
- Persist price peaks across ticks/restarts; enforce exit-window start times.
- Separate simulation/live positions and idempotency keys. Bind live DBs to an
  account; block unverified legacy live DBs and reconciliation divergences.
- Count pending buys toward the position cap. The spending cap is checked against
  ask × 100 (the entry limit price), not mid.
- Reject failed HTTP responses, bad quotes, historical live runs, and live --once.
- Add CSV/JPEG/PNG upload, local Apple Vision recognition, strict preview validation,
  signed confirmation, and immutable reviewed CSV files.
- Correct the sample CSV's SNOW, DDOG, PLTR and GDX rows to match the supplied image.
- Three Cooper-pending settings (spending cap, 10% stop basis, delta filter) each
  have a `setting_confirmations` marker in `rules.yaml`. Any unconfirmed marker
  blocks live runs in `Engine.run` (CLI and web alike); dry runs are never blocked.
  Settings shows an Unconfirmed badge and a Confirm checkbox per setting; the
  dashboard's live panel lists the unconfirmed settings and their current values.
  Changing a confirmed value resets its marker unless the same save re-confirms it.
- Optional delta filter (`delta_filter`): off by default; Schwab (0–1) or TVT (0–100)
  delta, inclusive |delta| band. Editable in Settings with the same validation as YAML.

## Validation

**135 automated tests pass** (`.venv/bin/python -m pytest`, `.venv` built from
`requirements.txt` plus pytest). Coverage includes lifecycle, rejection, delayed
fill, restart, mode isolation, account binding, spending cap at the ask, the live
block per unconfirmed setting, dry runs ignoring it, confirmation persistence and
reset, delta filter validation through Settings, upload validation, preview
confirmation, and image recognition regression cases.

Actual Apple Vision recognition was run locally on the supplied screenshot. All
eight rows matched the corrected CSV. Safari was used to upload the original image,
enter explicit dates, preview all eight contracts, and confirm the list while the
runner remained idle. No credentials or real trading were used.

## Pending Cooper / integration

Defaults below are in place and adjustable; each blocks live runs until confirmed.

1. Actual per-position dollar cap (default $300, checked at the ask).
2. Entry-price vs peak-price interpretation of “drops 10%,” and whether it replaces
   the other exit rules (default: 10% from session peak, alongside the standard exits).
3. Whether Cooper filters delta upstream (default: yes, so the in-app filter is off).
4. Read-only Schwab verification, then supervised order-lifecycle checks on a real
   account before considering unattended operation.

The app does not automatically reprice an unfilled exit or recover an ambiguous
submission. Stop means stop monitoring, not liquidate. Only one process should
operate each account. See README for setup and operational limits.
