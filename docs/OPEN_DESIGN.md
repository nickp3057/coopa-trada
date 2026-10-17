# Open design questions

Three decisions that are not built yet. Each section explains the problem in
plain language, lays out the options, recommends a default, and ends with what
Cooper would need to answer.

Sources: the installed schwab-py 1.5.1 package
(`.venv/lib/python3.14/site-packages/schwab/`), mainly `orders/options.py`,
`orders/common.py`, `orders/generic.py` and `client/base.py`. Schwab's own
developer site was not consulted from this machine (no network calls to Schwab),
so anything about what Schwab *accepts*, as opposed to what schwab-py can *send*,
is marked **unverified** and needs a supervised test on a real account.

## A few terms

- **Option contract**: here, the right to buy (call) or sell (put) 100 shares at
  a set price until a set date. The bot only *buys* options and later *sells
  them back*. With a bought option, **the most you can lose is what you paid for it**.
- **Bid / ask**: the bid is the best price someone will pay you right now; the
  ask is the cheapest price someone will sell to you. The gap between them is the
  **spread**. Option spreads are often wide, 5–20% of the price.
- **Limit order**: "sell at this price or better." It may never fill.
- **Market order**: "sell now at whatever price." It always fills, but on a
  wide spread the price can be much worse than you expect.
- **Stop order**: an order that sits waiting at the broker and turns into a
  market (or limit) order once the price falls to a trigger level.
- **DAY order**: expires at the end of today's session. Everything the bot places
  today is a DAY order (`schwab.orders.options.__base_builder` sets `Duration.DAY`).

---

## (a) Repricing an exit order that does not fill

### The problem

When an exit rule fires, the bot places a sell **limit order at the current bid**.
Prices move. If the bid drops a few cents before the order reaches the market, the
order sits above every buyer and does not fill. Today the bot leaves it there: it
keeps checking the order, but it never lowers the price. If nobody buys before the
close, the DAY order expires and the position is still open. You then own an
option overnight that the strategy meant to sell today. At the close, the bot stops
with "Market closed with unsettled orders or positions".

### Options

1. **Leave it (today's behaviour).** Simple, never sells cheaper than the bid
   when the rule fired. Risk: the exit may not happen at all.
2. **Follow the bid down.** Every check (every 30 seconds by default), if the
   order has not filled, replace it at the *current* bid. schwab-py has
   `Client.replace_order(account_hash, order_id, order_spec)`
   (`client/base.py`), which cancels the old order and creates a new one.
   Risk: in a falling market you sell lower each time. Also, the old order can
   fill in the instant before it is replaced. The bot must then notice the fill
   and not sell twice.
3. **Step down in fixed amounts**, for example 1–2 cents or a few percent below
   the last price each round, stopping at a floor. More predictable, but a floor
   means the position might still not sell.
4. **Start at the middle of the spread, then walk to the bid.** Can get a better
   price when the spread is wide, but it takes longer.
5. **Switch to a market order near the deadline.** Guarantees the exit. On
   a thin option the fill can be far below the bid, sometimes near zero.

### Recommended default

**Option 2 with limits:** replace the unfilled exit at the current bid each
check, at most a fixed number of times (say 10). For the final 2 minutes before
the exit deadline, keep following the bid. **Never use market orders.** Every
replacement goes through the same "never resend blindly" recovery as new
orders. A fill that races a replacement is detected from Schwab's order status
before the next action. This keeps the "sell at what buyers are paying" intent,
makes a missed exit much less likely, and has no unlimited downside.

### Questions for Cooper

1. If the bot has to choose between "sell for a worse price" and "maybe still own
   it tomorrow", which do you prefer?
2. Is there a lowest price (in dollars, or a % below where the exit fired) you
   would never sell under?
3. Is a market order ever acceptable in the last minutes, or never?

---

## (b) A protective stop at Schwab as an overnight backstop

### The problem

The bot only protects a position while it is running. If the computer sleeps,
loses power or crashes after buying, nothing watches the position. A **stop order
sitting at Schwab** would keep working even with the computer off.

### What schwab-py and the Schwab API support for options

From the schwab-py source:

- The ready-made option helpers in `orders/options.py` cover only **market**
  and **limit** orders (`option_buy_to_open_limit`, `option_sell_to_close_limit`,
  `option_sell_to_close_market`, and similar), all as DAY orders.
- The generic `OrderBuilder` (`orders/generic.py`) can send any order type
  listed in `orders/common.py` `OrderType`: `STOP`, `STOP_LIMIT`, `TRAILING_STOP`,
  `TRAILING_STOP_LIMIT`, and others. It has `set_stop_price`, `set_stop_type`,
  `set_stop_price_link_basis/type`, `set_stop_price_offset` (for trailing stops),
  and durations including `GOOD_TILL_CANCEL`.
- `orders/common.py` provides `one_cancels_other` (OCO: two orders, when one fills
  the other is canceled) and `first_triggers_second` (TRIGGER: when the first fills,
  the second is placed automatically).

So schwab-py **can send** a stop or trailing-stop sell-to-close for an option, as
GOOD_TILL_CANCEL, and could attach it to the entry with `first_triggers_second`.
Whether Schwab **accepts** stop orders on options through the API, which
durations it allows for them, and what price it triggers on is **unverified**.
schwab-py offers no option-stop helper, which hints that this path is less
travelled. Schwab's public order-entry documentation would settle it, as would
one small supervised test.

### The catch

A resting stop at Schwab and the bot's own exit compete for the same contracts.
At most brokers, once a sell order covers your whole position, a second sell for
the same contracts is rejected (unverified for Schwab's API, but typical). So
before every bot exit, the bot would have to **cancel the stop, confirm the
cancel, then sell**. That is two extra steps, a race if the stop triggers during
the cancel, and a gap with no protection between cancel and sale. An OCO pairing
of "stop" and "bot exit" does not fit either: the bot's exit price is not known
in advance.

Also, the bot buys options, so **the most a position can lose is the premium
paid** (at most the spending cap, $300 by default, times up to 5 positions). A
stop does not prevent a loss; it only limits how much of that premium is lost.
On wide option spreads, stops also tend to trigger on a brief dip and sell at
a poor price.

### Recommendation: **No, not for now**

The strategy enters about 20 minutes before the close and exits the same
afternoon, so overnight holds should only happen after a failure. The extra
cancel-then-sell steps add new ways to fail during every normal exit, in exchange
for limiting a loss that is already capped. Better first steps:

1. Repricing (section a), so exits do not get stranded.
2. A loud alert (dashboard now; text or email later) when positions are still
   open at the close or the bot stops unexpectedly.
3. Section (c), so the next morning's start handles leftovers deliberately.

Revisit if Cooper wants to hold positions overnight on purpose, or if a
supervised test shows Schwab accepts option stops and lets a working stop be
replaced by a sell in one step.

### Questions for Cooper

1. Do you ever want to hold a position overnight on purpose?
2. If the computer dies after buying, is losing up to the full premium on those
   positions acceptable, given it is at most the cap per pick?
3. If you want a backstop anyway: at what loss (% below the entry price) should it
   sell, and is a sale at a bad price on a brief dip acceptable?

---

## (c) Starting up the morning after a crash, with positions still open

### What the bot does today

From `coopa_trada/engine.py` and the safety changes in this branch:

1. **Stale lock (new).** A LIVE run that crashed leaves a stale lock. Every run is
   refused until a person clears it on the dashboard or with
   `python -m coopa_trada unlock`. The message tells them to check Reconcile first.
2. **Unknown orders (new).** Any order whose outcome was unknown keeps its contract
   halted until resolved on the Reconcile page.
3. **Reconcile.** Before trading, the bot compares its records with Schwab. If they
   differ, for example a position Schwab shows that the bot does not know, or an
   option that expired overnight, it **refuses to start** and lists the differences.
4. **If everything matches,** the bot waits until about 20 minutes before the close,
   buys today's picks, and then manages **all** open positions, including
   yesterday's leftovers. It uses yesterday's entry price and the highest price it
   saw, with today's exit window. A leftover therefore sits unmanaged from the
   open until about 3:35 pm and is then sold by the normal rules, or at today's
   deadline at the latest.
5. Yesterday's leftover also takes one of today's position slots (maximum 5).

### Options

1. **Keep today's behaviour.** Simple, but a leftover rides most of the day unwatched.
2. **Sell leftovers at the open.** Gets out quickly, but spreads are widest in
   the first minutes, so the price is often poor.
3. **Sell leftovers once spreads settle** (for example from 10:00 am), using
   the repricing from (a). The bot would need a morning monitoring mode it does
   not have yet; today it only works in the late afternoon.
4. **Ask a person.** Show leftovers on the dashboard with "Sell now", "Manage
   with today's rules" and "I'll handle it in Schwab (stop tracking)". Do nothing
   until someone chooses.

### Recommended default

**Option 4 with option 1 as the fallback.** On startup, list leftovers
prominently with those three choices. If nobody chooses, manage them with today's
rules (today's behaviour), so a leftover never lasts past today's close
unattended. The stale-lock and reconcile checks stay as hard stops, because a
crash is exactly when the bot's records are most likely to be wrong.

### Questions for Cooper

1. If a position is left over from yesterday, should the bot sell it as early as
   possible, or treat it like any other position and sell it in the afternoon window?
2. Is selling at the open (worse prices) acceptable for leftovers, or should it
   wait until mid-morning?
3. Should a leftover count against the 5-position limit for today's new picks?
