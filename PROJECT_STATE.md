# Project state — Coopa Trada

_Last updated: 2026-10-26_

Python/Flask app that trades Cooper's daily FOMC option picks through Schwab. It runs
as a double-click macOS app or as a service on a Linux server
(docs/SERVER_SETUP.md). Picks arrive as screenshots by Telegram (or dashboard upload),
are read by Claude (or Apple Vision on a Mac), previewed, and bought on confirmation.
Every trading choice is an ordinary setting with a working default; nothing waits
for sign-off. Simulation remains the default and **no live order has ever been
placed**. The only real Schwab request so far is still Nick's login (2026-10-16).

The plain-English rulebook, with dollar examples and every setting, is
**`docs/HOW_IT_TRADES.md`**.

## Session summary (2026-10-25)

On `claude/gracious-einstein-a30t1n`, one commit each (373 tests passing):

- **Cooper confirmation checks removed** (`b45ef1d`). Settings are plain settings;
  the dashboard's live panel lists them in plain words. Live runs still need
  `LIVE_TRADING=1`, the typed phrase (or `AUTO_START_LIVE=1`), a valid rules file
  and a Schwab login covering the hold. Old `setting_confirmations` blocks are ignored.
- **Spread fix** (`b66e116`): `exit_reference_price` (default `mid_at_entry`) makes
  the exit rules measure from the mid at purchase, so a wide bid–ask gap is not an
  instant loss. Price triggers count an exact hit (float tolerance).
- **Automatic daily start** (`50f1c12`): 7:00 AM MT on trading days; practice unless
  `LIVE_TRADING=1` and `AUTO_START_LIVE=1`; a run started or stopped by hand that day is
  left alone; one attempt per day.
- **Server-ready** (`efcc10f`): `DASHBOARD_PASSWORD` (HTTP login, required off
  127.0.0.1), Tailscale-proxy-safe origin check, quiet keep-awake on Linux, calendar
  expiry warning 60 days ahead, `packaging/server/` (systemd unit, install script
  tested in a scratch copy), `docs/SERVER_SETUP.md`.
- **Last open questions as settings** (`1197daf`): `next_day_time_exit` (sell at the
  end of the next day; default yes) and `contracts_per_pick` (default 1).
- Docs: HOW_IT_TRADES has a full settings table instead of questions; COOPER_README
  and README describe the automatic start and server use.

**Multi-person website** (`fa02222`): one bot per person (`coopa-trada@NAME`, own
data folder, `.env`, Schwab login, settings, trade log, Telegram bot) behind
`coopa_trada.gateway` (password + TOTP 2FA, lockout, 12-hour signed session), HTTPS by
Caddy on a free DuckDNS name. Verified end to end with two bots and curl.

Hosting: a 1 GB Linux server (AWS Lightsail, DigitalOcean, Hetzner), about $5–12 a
month, on 24/7, reached privately over Tailscale.

## Session summary (2026-10-22): Cooper's rules

One branch and one commit per task, stacked in order (each branch contains the
ones before it); `claude/gracious-einstein-a30t1n` was fast-forwarded to the last one.
Nothing was pushed. **249 tests at the start, 360 at the end, all passing.**

| Task | Branch | Commit | Status |
|---|---|---|---|
| 0 Read-only Schwab check | `task-0-readonly-check` | `370542c` | Partial: tool built and tested; **not run against Schwab** (this cloud container has no `schwab_token.json` or `.env`) |
| 1 Profit-point trailing exit | `task-1-profit-trail` | `0cdc0d4` | Done |
| 2 Hold logic | `task-2-hold-logic` | `8187f5c` | Done |
| 3 Sizing (budget, delta rank) | `task-3-sizing` | `52b8c42` | Done |
| 4 Exit repricing | `task-4-exit-repricing` | `fb09d43` | Done |
| 5 Entry timing, half-day fix, MT | `task-5-entry-timing` | `fb5024a` | Done |
| 6 Claude picture reader | `task-6-claude-reader` | `d3b46c6` | Done (API mocked; never called for real) |
| 7 Telegram intake | `task-7-telegram` | `60e8cdb` | Done (Bot API mocked; never called for real) |
| 8 Rule sheet + this file | `task-8-docs` | (this commit) | Done |

What changed, briefly:

- **Read-only check:** `python -m coopa_trada readonly-check [--symbol SPY]` calls only
  `get_account_numbers`, `get_account` and `get_option_chain` through a wrapper that
  refuses every other method (order methods included) and prints counts and yes/no
  answers, never account numbers, hashes or amounts.
- **10-point rule:** `ProfitPointTrail` sells when profit % falls `profit_trail_points`
  (10) points below peak profit % (trigger = peak − 0.10 × entry; $1 → $2 peak → $1.90).
  `profit_trail_arm`: `from_entry` (default) or `in_profit`.
- **Hold logic:** entry day = only take-profit (plus stops when `stops_on_entry_day`,
  default yes); otherwise held overnight and the run ends cleanly. Next trading day:
  every exit from `held_exit_start` (default the open) and the time exit at that
  day's window end. Options expiring on their entry day are never held; overdue
  positions are sold at the start of monitoring. Orders record their `trading_day`.
- **Sizing:** `total_budget` ($300) over all open positions and working buys (held
  ones included), counted at max(limit, fill) × 100, rounded up to the cent;
  `max_concurrent_positions` removed; picks ranked by |delta| (`rank_delta_source`:
  `schwab_live` default, or `tvt`), bought while each fits; every skip recorded.
- **Exit repricing:** a real sell unfilled after 60 s is canceled; only after Schwab
  reports CANCELED is a LIMIT order sent at the current bid for what is still held,
  at most 5 times, then an alert (Mac + ntfy). Only ever lowers the price.
- **Entry timing:** buys when picks are confirmed, inside 1:30–1:57 PM MT
  (`earliest_buy_minutes_before_close` 30, `no_new_buys_minutes_before_close` 3,
  relative to the real close, so half days move). Confirmed lists are files in
  `imports/`; the latest per trade date wins; a running run picks them up each tick.
- **Half-day bug fixed:** wall-clock window times shift earlier on early-close days.
- **Mountain time** everywhere a person reads a time (UI, login messages, alerts).
- **Claude reader** (`coopa_trada/vision.py`): Claude Opus 5.5, strict JSON schema,
  server-side refusal fallback; default off the Mac; same preview + confirm.
- **Telegram** (`coopa_trada/telegram_bot.py`, `docs/TELEGRAM_SETUP.md`): forward
  screenshot → preview with Confirm/Reject (also on the dashboard); allowed chat IDs only.

## Decisions made (conservative option on each ambiguity)

1. **Task 0 could not run here**: no Schwab login in the cloud container. Built the
   read-only command instead; Nick runs it on his Mac.
2. Exit rules keep judging on the **mid**; the 10-point rule fires **at** exactly 10
   points (inclusive).
3. **The old 10%-from-peak exit stays on** (never weaken a safety check). It is
   redundant with the 10-point rule in `from_entry` mode; Cooper is asked whether to
   remove it.
4. **One overnight hold at most**: on the next trading day the time exit sells at the
   window end (matches the existing login-hold check). Overdue positions are sold at
   the start of monitoring.
5. **Next-day exits start at the open** (protection all day) rather than the
   afternoon window; setting `held_exit_start`.
6. An option **expiring on its entry day is never held** overnight.
7. **Budget counting**: the higher of limit and average fill, × 100, rounded up to
   the cent; working, unknown and errored buy orders reserve their full cost.
8. **Budget skips continue down the list** (a cheaper, lower-delta pick can still
   fit); the total stays capped at the budget either way.
9. **Still one contract per pick**, never more (question for Cooper).
10. **Default ranking delta: Schwab live** (fresh from the broker, not OCR-dependent);
    picks without a delta rank last. Simulation uses the list's SCHWAB DELTA column.
11. **Repricing = cancel, confirm, new order** (not `replace_order`), so a fill during
    the cancel can never cause a second sale; quantity = what Schwab's executions show
    is still held. Only lowers the price; a cancel Schwab never confirms is retried
    after another interval; real orders only (simulated sells fill at once). The
    3-minute "not filling" alert counts from the first sell order.
12. **Earliest-buy guard added** (30 min before the close = 1:30 PM MT) so a list
    confirmed hours early cannot buy at the open. New setting.
13. The cutoff is **strict**: a buy needs now < 1:57 PM MT.
14. **The latest confirmed list for a day replaces earlier ones** (fixing a typo by
    re-confirming must not also buy the wrong picks).
15. Practice runs for past dates or after the close **replay** without the window.
16. **Half-day fix**: wall-clock endpoints move earlier by the early-close difference
    and never before the open (15:35 → 12:35 on a 1 PM close).
17. The Schwab login re-check before each buy still **refuses and stops** (as before).
18. **Claude reader** uses `claude-opus-5-5` with refusal fallbacks; Apple Vision stays
    the default on a Mac; the reader only transcribes (cells as text, never guessed).
19. **Telegram**: long polling (no open ports); list trade date must be today; only
    allowed chat IDs; buttons only work from the chat that got the preview; previews
    live in memory for 1 hour; ignored chat IDs are shown on the dashboard to help setup.
20. Confirming picks **during a run is now allowed** (it is the trigger). The old test
    that refused it was changed accordingly.
21. The watchdog's "positions open with nothing watching" alert **stays on** for
    deliberate overnight holds, reworded to say the next day's run must be started.
22. **Not pushed**: the session's hard rules say never push, which overrides the
    cloud session's default instruction. Everything is committed locally only.

Existing tests changed because Cooper's rules changed the expected behavior (none
removed, none loosened on safety): the time-exit smoke test now holds overnight and
sells next day; the delayed-entry test reaches take-profit; the slot-reservation
test now checks that a working buy reserves budget; the cancel-race test starts at
3:56 PM ET; the half-day hold ends at the real 12:59 window end; login messages
are in MT; the settings tests cover every confirmation marker; confirming during a
run is accepted; the unknown-order test gets budget for both picks.

## Still untested

- Every real Schwab call except the login: run `python -m coopa_trada readonly-check`
  on the Mac first, then a dry-run day on real prices.
- Real Claude API and Telegram Bot API calls (both only mocked).
- Real exit repricing at Schwab: what Schwab returns for a cancel of a filled order,
  `PENDING_CANCEL` timing, and partial fills (all assumed from schwab-py, tested
  against a fake).
- The packaged macOS app was **not rebuilt** (Linux container); `verify_app.py`
  passed against the source. The new `anthropic` dependency must be in the next build.
- Real ntfy delivery, notifications from the packaged app, Gatekeeper, Windows.

## Known issues / risks

- Every real Schwab call except the login is still untested (quotes, chains, orders,
  cancels). Run `python -m coopa_trada readonly-check`, then a practice day on real
  quotes, then a supervised live day with one cheap contract before unattended use.
- Real Claude API and Telegram Bot API calls have only run against mocks.
- The macOS app has not been rebuilt since these changes (it needs `anthropic`).
- Telegram previews live in memory: a restart loses pending previews (resend).
- The bundled market calendar ends at the session of 2027-10-15; the watchdog alerts
  60 days before. Update `exchange-calendars` then.

## Next steps

1. Set up the server (docs/SERVER_SETUP.md) or keep the Mac; fill in `.env`.
2. `python -m coopa_trada readonly-check`; Telegram bot (docs/TELEGRAM_SETUP.md).
3. A few practice days on real quotes (automatic start, Telegram confirm).
4. Supervised live checks with one cheap contract (buy, sell, a forced reprice), then
   `LIVE_TRADING=1` (and `AUTO_START_LIVE=1` for unattended days).

## Validation

**373 automated tests pass** (249 before 2026-10-22). `packaging/verify_app.py --source`
passes inside the suite; a headless-browser check loaded Settings and the Dashboard
without JavaScript errors; `packaging/server/install.sh` was run in a scratch copy
(with `sudo` stubbed) and the installed copy served the dashboard.

## Earlier sessions (reference)

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
  Since 2026-10-22 positions are held to the next trading day by design (Cooper's
  hold rule), so this gate matches what the engine does.
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
