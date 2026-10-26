# How Coopa Trada trades

This is the plain-English rulebook: when the bot buys, what it buys, how much it
spends, and every reason it sells. The dollar examples use the settings it ships
with. Every choice is a setting on the **Settings** page; the table at the end
lists them all with their defaults.

All times are **Mountain time (MT)**. A normal market day opens at **7:30 AM MT**
and closes at **2:00 PM MT**.

---

## 1. When it buys

1. At **7:00 AM MT** on every trading day the day's run **starts by itself**
   (nobody has to press Start). It watches anything held from yesterday (section 4)
   and waits for the picks.
2. Cooper's picks arrive between **1:40 and 1:55 PM**. He forwards the screenshot
   to the Telegram bot, or uploads it on the dashboard.
3. The bot reads it and shows every pick. **Nothing is bought until someone presses
   Confirm** (in Telegram or on the dashboard).
4. The moment the list is confirmed, the run buys. There is no fixed buying time.

The buying window:

- **No buys before 1:30 PM.** This guards against a list confirmed hours early by
  mistake. A list confirmed at 11 AM waits until 1:30 PM.
- **No new buys from 1:57 PM** (3 minutes before the close). A list confirmed at
  1:58 PM is not bought; the bot logs it as "confirmed after the cutoff".
- **Half days** (for example the day after Thanksgiving, closing 11:00 AM MT): the
  whole window moves with the early close, so it runs 10:30–10:57 AM MT.

Other buying rules:

- The trade date on the list must be today. Telegram refuses any other date.
- Confirming a corrected list later the same day **replaces** the earlier one.
  Anything already bought stays bought; picks that were only on the old list are no
  longer bought.
- A buy order that hasn't filled by the end of that pick's exit window (1:58 PM MT for
  stocks, 1:59 PM MT for ETFs) is canceled.

## 2. What it buys, and in what order

- **One contract per pick** (setting: contracts per pick).
- The picks are **ranked by delta, highest first**, using **Schwab's live delta**
  at the moment of buying (the other choice is the TVT delta from the screenshot).
  Puts count by size (−0.70 ranks above 0.55). A pick with no delta goes last.
- It buys down the list while each pick still **fits the budget** (section 3). A pick
  that doesn't fit is skipped, and the bot still tries the cheaper ones after it.
- Every skipped pick is written in the log with the reason (too expensive, doesn't
  fit the budget, after the cutoff, option not trading, and so on).

## 3. How much it spends

- **Total budget: $300.** The most that everything open may cost **together**:
  today's buys, positions held from yesterday, and buy orders still waiting to fill.
  Costs are counted at the **ask × 100** (what one contract costs to buy).
- **Per-pick cap: $300.** A pick whose contract costs more than $300 is skipped.
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

The bot checks prices every 30 seconds. It judges on the **mid** (halfway between
the bid and the ask) and sells with a limit order at the **bid** (what buyers are
paying right now). Several rules watch at once; **whichever fires first wins**.

**What counts as the buy price for the rules.** The rules measure from the **mid at
the moment of buying**, not from the ask paid. Example: an option quoted $0.80 bid /
$1.00 ask is bought at $1.00; its mid is $0.90, so the rules start at $0.90. That way
a wide bid–ask gap doesn't look like an instant 10% loss that would trigger a sale
straight away. The trade log always shows the real prices paid and received. (The
other choice, "the price paid", measures from the $1.00.)

### Day of purchase

- **Take profit: +60%.** Bought at a $1.00 mid → sells when the mid reaches **$1.60**,
  the same day.
- **Stops also protect the position on the day it is bought** (hard stop, trailing
  stops, the 10-point rule). Switch this off to let only the take-profit sell on the
  day of purchase.
- If nothing fires by the end of the exit window, the position is **held
  overnight** and rolls to the next trading day.
- Exception: an option that **expires today** can't be held, so it is sold at the
  end of today's exit window.

### Next trading day

Every rule applies **from the market open (7:30 AM MT)**, and anything still open is
**sold at the end of that day's exit window** (1:58 PM MT for stocks, 1:59 PM MT for
ETFs), so nothing is held more than one night. A Friday buy rolls to Monday; holidays
are skipped. If a position misses its next-day sale (for example the server was
off), it is sold at the start of the following day's watching. (Setting: switch the
next-day sale off to keep holding until a rule fires; an option is still sold on its
expiration day.)

### The rules (bought at a $1.00 mid in every example)

| Rule | Sells when | Example |
|---|---|---|
| **Take profit** | mid is 60% above the buy price | mid reaches $1.60 |
| **10-point rule** (profit-point trail) | profit falls 10 percentage points below its best | best $2.00 (+100%) → sells at **$1.90** (+90%) |
| **Hard stop** | mid is 40% below the buy price | mid falls to $0.60 |
| **Trailing stop 25%** | mid falls 25% from its highest | highest $2.00 → sells at $1.50 |
| **10% from the high** (older reading of the 10% rule) | mid falls 10% from its highest | highest $2.00 → sells at $1.80 |
| **Time exit** (next trading day only) | end of the next day's exit window | 1:58 PM MT |

**The 10-point rule, more examples.** The trigger is *highest price − 10% of the buy
price*:

- Bought $1.00, highest $2.00 (+100%) → sells at **$1.90** (+90%).
- Bought $2.50, highest $3.00 (+20%) → sells at **$2.75** (+10%). (Not $2.70: it is
  10 points of *profit*, not 10% of the high.)
- Bought $1.00, never went up → sells at **$0.90**: before any gain it works like a
  −10% stop. (Setting: "only once in profit" waits until the price has been above
  the buy price at least once.)

The 10-point rule's trigger is never below the "10% from the high" trigger, so that
older rule only matters if the 10-point rule is switched to "only once in profit" or off.

### If a sell order doesn't fill

A real sell order still waiting after **60 seconds** is canceled and replaced at the
**current bid**, up to **5 times**. It only ever lowers the price and never uses a
market order. After the 5th try the order stays at its last price and you get a phone
alert.

Example: a sell order at $1.40 doesn't fill. A minute later the bid is $1.35. The bot
cancels, waits for Schwab to confirm the cancel, then places a new order at $1.35. If
the $1.40 order filled in the meantime, nothing more is sold. If it was 2 contracts
and 1 filled, the new order is for the 1 that's left.

## 5. What it never does

- Buy before a list is confirmed, or buy a list that isn't for today.
- Spend more than the budget, or more than the per-pick cap on one pick.
- Use market orders.
- Resend an order it is unsure about (it stops trading that option and asks a person).
- Trade real money unless the bot is allowed to in the server's `.env`
  (`LIVE_TRADING=1`) **and** the Dashboard's **Real money** switch is on. The switch
  starts off, turning it on takes the typed confirmation phrase, and turning it off is
  one click. While it is on, the 7:00 AM automatic run and the Start button trade for real.

---

## Every setting (Settings page)

| Setting | Default | Other choices |
|---|---|---|
| Start the day's run automatically | on, 7:00 AM MT | off, or another time |
| Earliest buy | 30 min before the close (1:30 PM MT) | any number of minutes |
| No new buys after | 3 min before the close (1:57 PM MT) | any number of minutes |
| Rank picks by | Schwab's live delta | TVT delta from the screenshot |
| Contracts per pick | 1 | 1–100 |
| Total budget | $300 | any amount |
| Per-pick cap | $300 | any amount |
| Delta filter | off (buy every pick) | a delta range to keep |
| Exit rules measure from | the mid at purchase | the price paid |
| Take profit | +60% | any % |
| Hard stop | −40% | any % |
| Trailing stop | 25% from the high | any % |
| 10-point rule | 10 points, from entry | other points, "only once in profit", or off |
| 10% from the high | on, 10% | another %, or off |
| Stops on the day of purchase | yes | no (only the take-profit sells that day) |
| Next-day watching starts | market open | a time, or minutes before the close |
| Sell everything at the end of the next day | yes | no (hold until a rule fires) |
| Unfilled sells | reprice after 60 s, up to 5 times | other numbers; 0 = off |
| Exit windows | stocks 1:35–1:58 PM MT, ETFs 1:42–1:59 PM MT | other times |
