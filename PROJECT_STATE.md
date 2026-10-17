# Project state — Coopa Trada

_Last updated: 2026-09-14_

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
- Count pending buys toward the position cap; use ask × 100 for the dollar gate.
- Reject failed HTTP responses, bad quotes, historical live runs, and live --once.
- Add CSV/JPEG/PNG upload, local Apple Vision recognition, strict preview validation,
  signed confirmation, and immutable reviewed CSV files.
- Correct the sample CSV's SNOW, DDOG, PLTR and GDX rows to match the supplied image.
- Block live runs until the cap/exit rules are explicitly confirmed and the delta
  policy is recorded. Settings edits reset that confirmation.

## Validation

**118 automated tests pass.** Automated coverage includes the existing suite plus lifecycle, rejection, delayed
fill, restart, mode isolation, account binding, spending cap, upload validation,
preview confirmation, and original image recognition regression cases.

Actual Apple Vision recognition was run locally on the supplied screenshot. All
eight rows matched the corrected CSV. Safari was used to upload the original image,
enter explicit dates, preview all eight contracts, and confirm the list while the
runner remained idle. No credentials or real trading were used.

## Pending Cooper / integration

1. Actual per-position dollar cap.
2. Entry-price vs peak-price interpretation of “drops 10%,” and whether it replaces
   the other exit rules. Both may remain enabled only in simulation.
3. Whether Cooper filters delta upstream. In-app delta filtering remains unimplemented.
4. Read-only Schwab verification, then supervised order-lifecycle checks on a real
   account before considering unattended operation.

The app does not automatically reprice an unfilled exit or recover an ambiguous
submission. Stop means stop monitoring, not liquidate. Only one process should
operate each account. See README for setup and operational limits.
