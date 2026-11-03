# Simulator, Activity page and logs

## Simulator (Simulator tab)

Replays a pick list on any trading days and times you choose, for debugging. It
runs this bot's **real** buying and selling code with **this bot's settings**,
against a pretend Schwab on a fake clock, in a throwaway database. Nothing is
sent to Schwab and the trade log is not touched. Each run happens in its own
process, so it can never disturb the real bot.

1. **Picks**: start from one of this bot's confirmed lists, a saved seed, or a
   made-up example, then edit freely. "Save as seed" keeps an edited list.
2. **When**: buy date, when the list is confirmed (default 1:40 PM MT), when the
   simulation starts, and when it ends (default 10:00 AM MT the next trading day;
   up to 6 trading days). A list replayed on another date keeps its days to expiry.
3. **Prices**:
   - **Made-up**: a shape per pick (rise, drop, spike then fade, gap up then fade,
     gap down, flat, random walk). Use this to check that each rule fires when it should.
   - **Real stock prices**: the stock's real one-minute prices from Schwab (Schwab
     keeps about 48 days), with the option priced by Black-Scholes. The volatility
     is set so the option costs the list's LAST PRICE at the confirm time, then held
     constant. Needs the Schwab login. **An estimate**: Schwab has no option price
     history, and real volatility moves (especially overnight).
   - **Recorded Schwab quotes**: real option quotes the bot saved while it ran
     (every quote it reads goes into the `quote_log` table). Only covers contracts it
     watched with real Schwab prices: live runs, or practice with "real Schwab prices" on.
4. Press **Simulate**, then play it back: play/pause, speed, a scrubber, one tick at
   a time (arrow keys; space plays/pauses). Each bought option gets a chart (price,
   take-profit in green, the nearest stop in red, buy and sell dots), its current
   price, high, and every rule's trigger price with how far away it is. The event
   feed lists every decision, order, fill, skip and alert; click one to jump there.
   "Engine log lines" adds the bot's raw log. Download the log or the raw data.

Fills: a buy fills when the ask is at or below its limit, a sell when the bid is at
or above it. "First sell order doesn't fill" exercises the cancel-and-reprice path
(the bot only lowers the price, so that needs a falling bid).

Runs are kept in `simulations/` next to the database (newest 50).
Command line: `python -m coopa_trada simulate REQUEST.json OUT_FOLDER --config ... --db ...`.

## Activity tab

- **Watching now**: every open position, its latest price, its high, and the price
  at which each rule would sell it (the stop that would fire first is highlighted),
  plus the next-day sell-by time. Refreshes every 10 seconds.
- **Everything that happened**: the bot's full log for any day, filterable and
  searchable, following live. Download any day.

## Logs

Every log line is written to `logs/YYYY-MM-DD.log` (Mountain-time date) next to the
database and kept for a year. That includes runs, picks, every decision and its
reason, orders, fills, skips, alerts, every button pressed (who and the result; never
the request body, which can hold keys), every settings change as old → new, and
every simulation started and finished. On the server the same lines also go to the
journal (`journalctl -u coopa-trada@NAME`).
