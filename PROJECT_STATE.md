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

## Schwab login status and weekly reconnect

- `coopa_trada/schwab_token.py` reads schwab-py's token file
  (`{"creation_timestamp", "token": {...}}`) and classifies it as Connected,
  Expiring soon (< 2 days left), Expired (7 days after login), or Not set up
  (missing, damaged, legacy format without a login time, or a login time in the future).
- Any run that touches Schwab (LIVE, or any component marked
  `requires_schwab_token`) refuses to start unless the login lasts until 30 minutes
  after that day's close. Simulation with synthetic data needs no token.
  `wiring.schwab_client` checks before schwab-py even loads the file.
- Dashboard: status pill, countdown, warning banner under 2 days, and a
  Reconnect Schwab button (paste-the-redirect-address flow; see below). Reconnect
  is refused while a run is in progress. All of it is tested against a mocked schwab-py.

### Would schwab-py's manual login work on a headless cloud server?

Yes for the token exchange, with one practical catch. From the schwab-py 1.5.1 source
in `.venv/lib/python3.14/site-packages/schwab/auth.py`:

- `client_from_manual_flow` never opens a browser or starts a server. It builds
  the login URL locally (`get_auth_context`), prints it, reads the redirect address
  with `input('Redirect URL> ')`, and exchanges it with `client_from_received_url`.
  The person can log in from any browser on any machine. The redirect to
  `https://127.0.0.1:8182` fails to load on their machine, but the address bar
  still holds the one-time code, and that is what gets pasted.
- The catch: `input()` needs someone at an interactive terminal on the server
  (for example an SSH session), every 7 days. This app's Reconnect page does the
  same two calls through a web form instead, but the UI binds to 127.0.0.1 only,
  so on a cloud server it would have to be reached through an SSH tunnel or put
  behind real authentication first. Neither is built.
- `client_from_login_flow` (the browser-assisted flow) would **not** work headless:
  it calls `webbrowser.get(...).open(...)` on the server and waits for the redirect on
  its own 127.0.0.1 callback server (it rejects any other callback host), so the
  browser must run on the same machine.
- `easy_client` falls back to `client_from_login_flow` outside notebooks, so it
  is unsuitable for headless use too.

## One process per account

- `coopa_trada/runlock.py`: `Engine.run` takes an OS file lock on `<db>.lock` next
  to the database, plus `~/.coopa_trada/locks/account-<sha256>.lock` whenever the
  broker is bound to a Schwab account (`COOPA_TRADA_LOCK_DIR` overrides the folder).
  The CLI and web runner both go through `Engine.run`, so neither can skip it.
- Locks use `flock` (macOS/Linux) or `msvcrt.locking` (Windows, untested). The OS frees
  them when a process dies. The file records the holder, and a clean exit erases
  the record, so a free lock with a record is a stale lock left by a crash.
- A stale lock from a LIVE run blocks every new run until a person clears it with
  `python -m coopa_trada unlock` or the dashboard's Clear stale lock button, after
  checking the Reconcile page. A stale record from a DRY-RUN is cleared automatically.
  A lock recorded by another computer needs `--force-other-host`.

## Unclear order submissions

- A live submit that fails without a definite answer is never resent.
  `SchwabBroker._recover` looks for it among orders entered around the submit
  time (exact contract, side, quantity, LIMIT price; ignoring orders already
  tracked locally). It looks up to 3 times, 5 seconds apart, because orders can
  take a moment to appear.
  - **Found exactly one:** adopted (broker ID recorded, normal polling resumes).
    Logged as a resolved incident.
  - **Connection never opened** (`ConnectError`/`ConnectTimeout`, so no request left)
    and nothing found: `NOT_PLACED`. Not resent; shown for a person to acknowledge.
  - **Anything else:** read timeouts, 5xx, a 2xx without an order ID, a failed
    lookup, or two matching orders. Marked `UNKNOWN` and recorded in
    `order_incidents`. New orders (entries and exits) for that contract are halted
    until a person resolves it on the Reconcile page ("It IS in Schwab" plus the
    order number, or "It is NOT in Schwab"). The dashboard shows a banner.
- A definite 4xx rejection keeps the existing strict path (`ERROR`, re-raised,
  polling blocked until someone reconciles manually).
- A recovered failure no longer aborts the run: other picks and positions keep
  being managed. An unresolved unknown order still keeps the day from finishing
  cleanly (it raises at the close), and if the order later shows up at Schwab,
  the next start's reconcile reports it as an untracked divergence and refuses to trade.
