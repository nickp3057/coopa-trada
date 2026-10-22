# How Coopa Trada trades

This is the plain-English rulebook: when the bot buys, what it buys, how much it
spends, and every reason it sells. The dollar examples use the settings it ships
with. Every setting can be changed on the **Settings** page. The ones marked
**(unconfirmed)** still need your yes or no, and real-money trading stays switched
off until every one of them is confirmed.

All times are **Mountain time (MT)**. A normal market day closes at **2:00 PM MT**.

---

## 1. When it buys

1. Before your picks arrive, Nick (or you) starts **the day's run** on the
   dashboard. Starting it before the market opens (7:30 AM MT) is best, because the
   same run also watches anything held from yesterday (section 4).
2. Your picks arrive between **1:40 and 1:55 PM**. You forward the screenshot to the
   Telegram bot, or upload it on the dashboard.
3. The bot reads it and shows every pick. **Nothing is bought until you press
   Confirm.**
4. The moment you confirm, the run buys. There is no fixed buying time.

The buying window **(unconfirmed)**:

- **No buys before 1:30 PM.** This guards against a list confirmed hours early by
  mistake. A list confirmed at 11 AM waits until 1:30 PM.
- **No new buys from 1:57 PM** (3 minutes before the close). A list confirmed at
  1:58 PM is not bought; the bot logs it as "confirmed after the cutoff".
- **Half days** (for example the day after Thanksgiving, closing 11:00 AM MT): the
  whole window moves with the early close, so it runs 10:30–10:57 AM MT.

Other buying rules:

- The trade date on the list must be today. Telegram refuses any other date.
- If you confirm a corrected list later the same day, it **replaces** the earlier
  one. Anything already bought stays bought; picks that were only on the old list
  are no longer bought.
- If a buy order hasn't filled by the end of that pick's exit window (1:58 PM MT for
  stocks, 1:59 PM MT for ETFs), it is canceled.

## 2. What it buys, and in what order

- **One contract per pick**, always (never 2 or 3 of the same option).
- The picks are **ranked by delta, highest first**. By default it uses
  **Schwab's live delta** at the moment of buying **(unconfirmed)**; the other
  choice is the TVT delta from your screenshot. Puts count by size (−0.70 ranks
  above 0.55). A pick with no delta goes to the end of the line.
- It buys down the list while each pick still **fits the budget** (section 3).
  A pick that doesn't fit is skipped, and the bot still tries the cheaper ones
  after it.
- Every skipped pick is written in the log with the reason (too expensive, doesn't
  fit the budget, after the cutoff, option not trading, and so on).

## 3. How much it spends

- **Total budget: $300** **(unconfirmed)**. This is the most that everything open
  may cost **together**: today's buys, positions held from yesterday, and buy
  orders still waiting to fill. Costs are counted at the **ask × 100** (what one
  contract costs to buy).
- **Per-pick cap: $300.** A single contract that costs more than $300 is skipped.
- There is **no limit on how many positions** it holds; the budget is the only limit.

**Worked example.** Budget $300, nothing held from yesterday. Four picks arrive:

| Pick | Live delta | Ask | Cost (ask × 100) |
|---|---|---|---|
| AAA | 0.30 | $1.00 | $100 |
| BBB | −0.70 | $1.50 | $150 |
| CCC | 0.55 | $2.00 | $200 |
| DDD | 0.45 | $3.20 | $320 |

1. DDD costs $320, over the $300 per-pick cap: **skipped**.
2. BBB ranks first (|−0.70|): costs $150, fits → **bought**. $150 left.
3. CCC ranks next (0.55): costs $200, only $150 left → **skipped**.
4. AAA (0.30): costs $100, fits → **bought**. $50 left.

Spent: $250. **If yesterday's position were still open** and had cost $250, only
$50 would be left today, so neither BBB nor AAA would fit and nothing would be bought.

## 4. Every reason it sells

The bot checks prices every 30 seconds. It decides on the **midpoint** between
the bid and the ask (the "mid"), and sells with a limit order at the **bid** (what
buyers are paying right now). Several rules watch at once; **whichever fires first
wins**.

### Day of purchase

- **Take profit: +60%.** Bought at $1.00 → sells when the mid reaches **$1.60**.
  If this happens the same day, it sells the same day.
- **Stops (unconfirmed: "stops on the day of entry", currently yes).** The stop-type
  rules below also protect the position on the day it is bought. If you answer
  no, only the take-profit can sell on the day of purchase.
- If no rule fires by the end of the exit window, the position is **held
  overnight** and rolls to the next trading day.
- Exception: an option that **expires today** can't be held, so it is sold at the
  end of today's exit window.

### Next trading day

Every rule applies, **from the market open (7:30 AM MT)** **(unconfirmed)**, and
anything still open is **sold at the end of that day's exit window** (1:58 PM MT
for stocks, 1:59 PM MT for ETFs). A Friday buy rolls to Monday; holidays are
skipped. If a position somehow misses its next-day sale (for example the computer
was off), it is sold at the start of the following day's monitoring.

### The rules (bought at $1.00 in every example)

| Rule | Sells when | Example |
|---|---|---|
| **Take profit** | mid is 60% above the buy price | mid reaches $1.60 |
| **Your 10-point rule** (profit-point trail) | profit falls 10 percentage points below its best | best $2.00 (+100%) → sells at **$1.90** (+90%) |
| **Hard stop** | mid is 40% below the buy price | mid falls to $0.60 |
| **Trailing stop 25%** | mid falls 25% from its highest | highest $2.00 → sells at $1.50 |
| **Old "10% from the high"** (from before your answer) | mid falls 10% from its highest | highest $2.00 → sells at $1.80 |
| **Time exit** (next trading day only) | end of the next day's exit window | 1:58 PM MT |

**Your 10-point rule, more examples.** The trigger is *highest price − 10% of
what you paid*:

- Bought $1.00, highest $2.00 (+100%) → sells at **$1.90** (+90%).
- Bought $2.50, highest $3.00 (+20%) → sells at **$2.75** (+10%). (Not $2.70: it is
  10 points of *profit*, not 10% of the high.)
- Bought $1.00, never went up → **"from entry"** (current setting,
  **unconfirmed**): sells at **$0.90**, so before any gain it works like a −10%
  stop. With **"only once in profit"** it doesn't act until the price has been above
  $1.00 at least once.

Because your 10-point rule is always at or above the old "10% from the high"
trigger, the old rule only matters if you switch the 10-point rule to "only once in
profit" (then the old rule would still act as a −10% stop). See question 3.

**An important side effect of "from entry" plus "stops on the day of entry":** the
bot buys at the **ask** but judges by the **mid**, which is lower by half the
bid–ask gap. On an option quoted $0.80 bid / $1.00 ask, it pays $1.00 and the mid
is already $0.90, which is −10%. So the 10-point rule would sell it within 30
seconds, at the $0.80 bid, for a $20 loss per contract. For a gap of about 20% of the
price or more, that happens to almost every buy. Narrow gaps (most liquid options)
are not affected. Questions 1 and 2 decide this.

### If a sell order doesn't fill (unconfirmed)

A sell order still waiting after **60 seconds** is canceled and replaced at the
**current bid**, up to **5 times**. Only ever lower, never a market order. After the
5th try it stays at its last price and you get an alert on your Mac and phone.

Example: sell order at $1.40 doesn't fill. A minute later the bid is $1.35: the bot
cancels, waits for Schwab to confirm the cancel, then places a new order at $1.35.
If the $1.40 order filled in the meantime, nothing more is sold. If half of it
filled (2 contracts, 1 filled), the new order is for the 1 that's left.

## 5. What it never does

- Buy before you confirm, or buy a list that isn't for today.
- Spend more than the budget, or more than $300 on one contract.
- Use market orders.
- Resend an order it is unsure about (it stops trading that option and asks a
  person).
- Trade real money unless Nick has switched real trading on, **every** setting
  below is confirmed, and someone types the confirmation phrase.

---

## Settings still waiting for your answer

Reply with eleven yes/no answers, for example **"1 yes, 2 yes, 3 no, …"**. A "no" means
we change it to the alternative in brackets.

1. **10-point rule from entry:** should your 10-point rule work from the moment of
   purchase, acting like a −10% stop before any gain? (No = only once the position
   has been in profit.)
2. **Stops on the day of purchase:** should the stop-type rules (10-point rule,
   −40% hard stop, 25% trailing stop) also be able to sell on the day of purchase?
   (No = only the +60% take-profit can sell that day.)
3. **Old "10% from the high" rule:** keep it switched on alongside your 10-point
   rule? (No = turn it off; your 10-point rule replaces it.)
4. **Total budget $300:** should everything open together, yesterday's positions
   included, be limited to $300 at the ask? (No = tell us the amount.)
5. **Per-pick cap $300:** skip any single contract costing more than $300?
   (No = tell us the amount.)
6. **Rank by Schwab's live delta:** rank picks using Schwab's delta at buying time?
   (No = use the TVT delta from your screenshot.)
7. **Next-day watching from the open:** on the day after purchase, should the stops
   and take-profit work from the 7:30 AM MT open? (No = only during the afternoon
   exit window, from 1:35 PM MT.)
8. **Next-day final sale:** sell anything still open at the end of the next day's
   exit window (about 1:58 PM MT), so nothing is held more than one night?
   (No = keep holding until a rule fires.)
9. **Sell-order repricing:** if a sell doesn't fill in 60 seconds, move it to the
   current bid, up to 5 times, then alert you? (No = tell us a different number of
   tries.)
10. **Buying window 1:30–1:57 PM MT:** buy only between 1:30 and 1:57 PM MT, earlier
    on half days? (No = tell us the times.)

11. **No delta filter:** buy every pick on your list regardless of its delta
    (your list is treated as already screened)? (No = tell us the delta range to keep.)

One more, with no setting yet: should it ever buy **more than one contract** of a
pick when the budget allows? Today the answer is never.
