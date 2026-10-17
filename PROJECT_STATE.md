# Project state — Coopa Trada

_Last updated: 2026-10-16_

Local Python/Flask app for Cooper's FOMC Picks, packaged as a double-click macOS
app. CSV and macOS image upload feed an editable preview, explicit dates, and a
separate confirmation step. Uploading or confirming a list never starts trading.
Simulation remains the default. No real Schwab account, credentials or live
orders have been used yet.

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

**210 automated tests pass** (`.venv/bin/python -m pytest`; `.venv` built from
`requirements.txt` plus pytest). Coverage includes lifecycle, rejection, delayed
fill, restart, mode isolation, account binding, spending cap at the ask, the live
block per unconfirmed setting, confirmation persistence and reset, delta filter
validation, token states and the start gate, the mocked reconnect flow, process
locks (with real subprocesses for concurrent and crashed holders), uncertain-order
recovery, the desktop launcher, and upload/preview/image recognition.

`packaging/build_macos.sh` built `dist/Coopa Trada.app` (arm64, 73 MB), and
`packaging/verify_app.py --app` passed against it: dry run by default, the typed
phrase, `LIVE_TRADING=1` from the `.env` next to the app, unconfirmed settings and
a missing Schwab login each refused a live start, a dry run completed, and Quit
exited cleanly. The bundled picture reader read all eight rows of a rendered
sample picks table correctly. A real double-click launch (browser opening) and
Gatekeeper on another Mac were not tested.

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
4. The questions at the end of each section of `docs/OPEN_DESIGN.md`
   (exit repricing, broker-side stops, leftovers after a crash).
5. Read-only Schwab verification, then supervised order-lifecycle checks on a real
   account before considering unattended operation. The reconnect flow and order
   recovery have only run against mocks.

Not built yet (see `docs/OPEN_DESIGN.md`): repricing an unfilled exit, a broker-side
protective stop, special handling for positions left over after a crash. Stop means
stop monitoring, not liquidate. The app keeps the Mac from idle sleeping while it
has something to watch, but closing the lid still sleeps it. The app is not code-signed or notarized, so Cooper has to
use Open Anyway on first launch. See README for setup and operational limits.

## Schwab login status and weekly reconnect

- `coopa_trada/schwab_token.py` reads schwab-py's token file
  (`{"creation_timestamp", "token": {...}}`) and classifies it as Connected,
  Expiring soon (< 2 days left), Expired (7 days after login), or Not set up
  (missing, damaged, legacy format without a login time, or a login time in the future).
- Any run that touches Schwab (LIVE, or any component marked
  `requires_schwab_token`) refuses to start unless the login lasts through the hold:
  until 30 minutes after the latest exit-window end on the **next** trading day
  (`CalendarService.next_day_exit_end`, weekends and holidays skipped; a window that
  can't resolve on a half day counts as lasting to that day's close). The check runs
  at start and again at entry time. Simulation with synthetic data needs no token.
  Note: the engine itself still manages exits in the *same* session (the exit windows
  resolve on the entry date). The longer gate is deliberately conservative.
  `wiring.schwab_client` checks before schwab-py even loads the file.
- Dashboard: status pill, countdown, warning banner under 2 days *or* whenever the
  login won't cover the next run's hold (red, since that run would be refused), and a
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

## Double-click app (macOS)

- `coopa_trada/desktop.py`: the folder containing `Coopa Trada.app` is the home
  folder (`.env`, `config/`, `trada.db`, `schwab_token.json`, `logs/`). The first run
  copies the example config and `env.example.txt` but never creates `.env`. Only the
  `.env` next to the app is loaded. A double-click while the app is running reopens
  the page. A macOS-translocated copy (run straight from Downloads) refuses with
  move-the-app instructions. Quit is a button on every page and is refused mid-run.
- `packaging/`: PyInstaller spec (one-folder `.app`, no Dock icon), a build script
  that precompiles the Apple Vision helper with `swiftc` (so Cooper needs no Xcode
  tools), and `verify_app.py`, which re-checks the live gates against the built app
  or the source launcher (the latter also runs in pytest).
- PyInstaller rather than py2app or Briefcase: it builds both macOS and Windows from
  the same spec, it already handles this stack (pandas, Flask, schwab-py), and
  one-folder builds start quickly. py2app is macOS-only. Briefcase targets native
  GUI toolkits and adds little for a browser-based UI.
- Off macOS, picture upload is disabled with a plain message and CSV upload still works.
- `docs/WINDOWS_BUILD.md`: what a Windows build would need (not built).
- `COOPER_README.md`: install, first Schwab login, daily workflow, weekly reconnect,
  what each status means.

## Alerts

- `coopa_trada/alerts.py`: an `Alerter` sends each alert to every channel, scrubbed
  (`scrub` removes links, `$` amounts, numbers after money words, 16+ character hex
  IDs and any run of 5+ digits) and de-duplicated by key (repeats at most every 6 h;
  12 h for login warnings). A failing channel is logged and never raises into trading.
  - `MacNotifier`: `osascript` with the text passed as arguments (never as script).
    Always on when running on macOS.
  - `NtfyNotifier`: optional JSON POST to `https://ntfy.sh` (or `NTFY_SERVER`, https
    only) when `NTFY_TOPIC` is set. Topics under 24 characters are refused.
  - `COOPA_TRADA_ALERTS_LOG_ONLY=1` swaps every channel for the log; it is set only by
    `packaging/verify_app.py`. Settings shows when it is on.
- Fired by the engine: entry/exit order outcome unknown or not placed; exit order
  REJECTED/CANCELED/EXPIRED/NOT_PLACED/ERROR with the position still open; exit order
  working 3+ minutes without filling; any run failure (refused before the entry pass,
  including the entry-time login re-check, or an error after it). The web runner and
  CLI alert for failures before the engine exists. A person pressing Stop never alerts.
- Fired by `coopa_trada/watchdog.py` (a background thread in the desktop app and
  `python -m coopa_trada.web`, every 60 s): login expiring within 2 days, expired, or
  not covering the next run's hold; a stale lock from a LIVE run; LIVE positions open
  while no run is active; and, on the first check after startup, unresolved order incidents.
- Settings → Alerts lists the channels, has **Send test alert**, and shows recent alerts.
- Tests replace `alerts._osascript` and `alerts._post` for every test (`tests/conftest.py`).
  No real notification or ntfy request is made by the suite.

## Keeping the Mac awake

- `coopa_trada/keepawake.py`: holds `caffeinate -i -s -w <app pid>` while wanted
  (idle sleep blocked, plus system sleep on AC power; it ends with the app even after
  a crash). Off macOS it reports an error and holds nothing.
- The watchdog wants it while a run is active (any mode), or while any LIVE position
  or LIVE open order exists. It releases it when neither is true. The runner pokes the
  watchdog when a run starts or ends, so it takes effect at once. CLI `run` holds it
  for the duration of the command.
- Simulated positions left over from a stopped practice run do not keep the Mac awake.
- The Dashboard shows "Keeping Mac awake: <reason>" (or the error). Closing the lid
  still sleeps the Mac without an external display; COOPER_README explains why the Mac
  must stay plugged in with the lid open.

## Schwab setup checklist

`docs/SCHWAB_SETUP.md`: what the Developer Portal and `.env` must contain for the
Reconnect flow (callback URL character-for-character identical, app Ready For Use,
key/secret), which login decides the account (`SCHWAB_ACCOUNT_ID` recommended), a
plain-language first-login walkthrough, a failure table, and a list of nine points
the installed schwab-py/Authlib source can't confirm.
