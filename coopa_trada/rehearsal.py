"""Rehearsal: a pretend Friday-to-Monday cycle on a fake clock and fake prices.

    python -m coopa_trada rehearse

What happens (all invented, all inside this process):

* Friday: the run starts at 7:15 AM MT (as auto-start would), the picks are
  confirmed at 1:40 PM MT, three options are bought inside the buying window,
  none reaches the take-profit, and all three are held over the weekend.
* Monday: the run starts at 7:15 AM MT and watches them from the open. One hits
  the take-profit, one falls 10 points below its peak profit (the 10-point
  rule), and one's sell order does not fill, so it is canceled and replaced at
  the lower bid 60 seconds later.

It runs the real engine, exit rules and Schwab order code, but against
:class:`PretendSchwab`, an in-memory stand-in: no Schwab login is read, no
``.env`` value is used, nothing is sent anywhere (network connections are
refused while it runs), and the database lives in a temporary folder that is
deleted afterwards. Alerts are printed, not sent. Because the exit repricing only
applies to orders at Schwab (practice sells fill instantly), the engine runs its
real-order code path here, with every setting treated as confirmed in memory
only; ``config/rules.example.yaml`` is never changed.
"""

from __future__ import annotations

import logging
import os
import socket
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import clock
from .alerts import Alerter, contract_text
from .calendar_svc import CalendarService
from .contracts import ResolvedContract, osi_symbol
from .exits import Quote
from .models import AssetClass, Right, Signal
from .rules import SETTING_LABELS, load_rules
from .schwab_token import TokenState, TokenStatus
from .store import Store

__all__ = ["rehearse", "PretendSchwab", "FRIDAY", "MONDAY"]

UTC = timezone.utc
FRIDAY = date(2026, 10, 23)
MONDAY = date(2026, 10, 26)
EXPIRATION = date(2026, 11, 20)
ACCOUNT = "rehearsal-account"


def mt(day: date, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    """A Mountain-time moment, as used in the timeline."""
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=clock.DISPLAY_TZ)


@dataclass(frozen=True)
class Pick:
    underlying: str
    right: Right
    strike: float
    asset_class: AssetClass
    delta: float
    # (time, mid) points; the price moves in a straight line between them.
    path: tuple

    @property
    def osi(self) -> str:
        return osi_symbol(self.underlying, EXPIRATION, self.right, self.strike)

    @property
    def name(self) -> str:
        return f"{self.underlying} {self.strike:g} {'call' if self.right is Right.CALL else 'put'}"


PICKS = (
    # Take-profit: bought at $1.00, opens Monday at +66% (the take-profit is +60%).
    Pick("AMD", Right.CALL, 260, AssetClass.SINGLE_NAME, 0.55, (
        (mt(FRIDAY, 13, 40), 0.99), (mt(FRIDAY, 14, 0), 1.04),
        (mt(MONDAY, 7, 29), 1.04), (mt(MONDAY, 7, 30), 1.66), (mt(MONDAY, 11), 1.70))),
    # 10-point rule: bought at $1.20, peaks at +40% Monday morning, then slides.
    Pick("SNOW", Right.CALL, 230, AssetClass.SINGLE_NAME, 0.62, (
        (mt(FRIDAY, 13, 40), 1.19), (mt(FRIDAY, 14, 0), 1.22),
        (mt(MONDAY, 7, 29), 1.22), (mt(MONDAY, 7, 30), 1.40), (mt(MONDAY, 8, 30), 1.68),
        (mt(MONDAY, 9, 30), 1.50))),
    # Unfilled sell: bought at $0.50, peaks at +25%, slides, and the first sell sits unfilled.
    Pick("IBIT", Right.PUT, 44, AssetClass.ETF, -0.48, (
        (mt(FRIDAY, 13, 40), 0.49), (mt(FRIDAY, 14, 0), 0.51),
        (mt(MONDAY, 7, 29), 0.51), (mt(MONDAY, 7, 30), 0.55), (mt(MONDAY, 9), 0.625),
        (mt(MONDAY, 9, 40), 0.575), (mt(MONDAY, 9, 43), 0.545), (mt(MONDAY, 11), 0.54))),
)
STUCK = {"IBIT"}  # its first sell order is not filled by the pretend market


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start.astimezone(UTC)

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=max(1.0, float(seconds)))


class Timeline:
    def __init__(self, clock_: FakeClock):
        self.clock = clock_
        self.entries: list[tuple[datetime, str]] = []
        self.last_order_at: int | None = None  # where the latest order line went

    def add(self, text: str, at: datetime | None = None) -> None:
        self.entries.append((at or self.clock(), text))

    def add_before_last_order(self, text: str) -> None:
        """A decision goes above the order it led to (same time, so sorting keeps this order)."""
        i = len(self.entries) if self.last_order_at is None else self.last_order_at
        self.entries.insert(i, (self.clock(), text))
        self.last_order_at = None

    def lines(self) -> list[str]:
        out, last_day = [], None
        for at, text in sorted(self.entries, key=lambda e: e[0]):
            local = clock.to_display(at)
            if local.date() != last_day:
                out.append("")
                out.append(f"{local:%A %B} {local.day}")
                last_day = local.date()
            out.append(f"  {local.strftime('%I:%M:%S %p').lstrip('0')} MT  {text}")
        return out


class Market:
    """Fake prices: each pick's mid follows its path; bid/ask are a cent either side."""

    def __init__(self, clock_: FakeClock):
        self.clock = clock_
        self.by_osi = {p.osi: p for p in PICKS}

    def mid(self, pick: Pick) -> float:
        now = self.clock()
        points = [(t.astimezone(UTC), m) for t, m in pick.path]
        if now <= points[0][0]:
            return points[0][1]
        for (t0, m0), (t1, m1) in zip(points, points[1:]):
            if now <= t1:
                return m0 + (m1 - m0) * (now - t0) / (t1 - t0)
        return points[-1][1]

    def quote(self, osi: str) -> Quote:
        mid = round(self.mid(self.by_osi[osi]), 4)
        return Quote(bid=round(mid - 0.01, 2), ask=round(mid + 0.01, 2), mid=mid)

    __call__ = quote


class Resolver:
    """Resolves a pick to its contract at the pretend market's current prices."""

    def __init__(self, market: Market):
        self.market = market

    def resolve(self, signal: Signal) -> ResolvedContract:
        pick = next(p for p in PICKS if p.underlying == signal.underlying)
        q = self.market.quote(pick.osi)
        return ResolvedContract(pick.osi, pick.underlying, pick.right, pick.strike, EXPIRATION,
                                q.bid, q.ask, q.mid, 5000, True, self.market.clock(), pick.delta)


def _response(payload=None, status=200, **headers) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers,
                          request=httpx.Request("GET", "https://pretend-schwab.invalid"))


class PretendSchwab:
    """The few schwab-py client calls the engine makes, answered from memory.

    Buys fill at once at their limit price. Sells fill at once too, except the
    first sell for a symbol in ``stuck``, which stays WORKING until canceled.
    """

    def __init__(self, clock_: FakeClock, timeline: Timeline, stuck=frozenset()):
        self.clock = clock_
        self.timeline = timeline
        self.stuck = set(stuck)
        self.orders: list[dict] = []

    def _name(self, osi: str) -> str:
        pick = next((p for p in PICKS if p.osi == osi), None)
        return pick.name if pick else contract_text(osi)

    def place_order(self, account, spec):
        built = spec.build()
        leg = built["orderLegCollection"][0]
        order = {"id": len(self.orders) + 1, "spec": built, "status": "WORKING", "filled": 0,
                 "price": float(built["price"]), "symbol": leg["instrument"]["symbol"],
                 "instruction": leg["instruction"], "qty": int(leg["quantity"]), "fill_time": None}
        self.orders.append(order)
        name = self._name(order["symbol"])
        side = "Buy" if order["instruction"] == "BUY_TO_OPEN" else "Sell"
        self.timeline.last_order_at = len(self.timeline.entries)
        self.timeline.add(f"{side} order sent to (pretend) Schwab: {name}, limit ${order['price']:.2f}")
        root = order["symbol"].split()[0]
        if side == "Sell" and root in self.stuck:
            self.stuck.discard(root)
            self.timeline.add(f"  ...nobody buys {name} at ${order['price']:.2f}: the order sits unfilled")
        else:
            self._fill(order)
        return _response({}, 201, Location=f"https://pretend-schwab.invalid/orders/{order['id']}")

    def _fill(self, order) -> None:
        order.update(status="FILLED", filled=order["qty"], fill_time=self.clock())
        what = "Bought" if order["instruction"] == "BUY_TO_OPEN" else "Sold"
        self.timeline.add(f"  {what} {self._name(order['symbol'])} at ${order['price']:.2f}")

    def get_order(self, oid, account):
        o = self.orders[int(oid) - 1]
        leg = {**o["spec"]["orderLegCollection"][0], "legId": 1}
        execs = ([{"activityType": "EXECUTION", "executionLegs": [
            {"legId": 1, "quantity": o["filled"], "price": o["price"], "time": o["fill_time"].isoformat()}]}]
            if o["filled"] else [])
        return _response({"orderId": o["id"], "status": o["status"], "filledQuantity": o["filled"],
                          "orderLegCollection": [leg], "orderActivityCollection": execs,
                          "orderType": o["spec"]["orderType"], "price": o["price"]})

    def cancel_order(self, oid, account):
        o = self.orders[int(oid) - 1]
        if o["status"] == "WORKING":
            o["status"] = "CANCELED"
            self.timeline.add(f"  Unfilled sell for {self._name(o['symbol'])} canceled (Schwab confirms CANCELED)")
        return _response({}, 200)

    def get_account(self, account, fields=None):
        held: dict[str, int] = {}
        for o in self.orders:
            sign = 1 if o["instruction"] == "BUY_TO_OPEN" else -1
            held[o["symbol"]] = held.get(o["symbol"], 0) + sign * o["filled"]
        positions = [{"instrument": {"symbol": s}, "longQuantity": q, "shortQuantity": 0}
                     for s, q in held.items() if q]
        return _response({"securitiesAccount": {"positions": positions}})

    def get_orders_for_account(self, account, **kwargs):
        return _response([{"orderId": o["id"], "status": o["status"]} for o in self.orders
                          if o["status"] == "WORKING"])


class PicksArrive:
    """The confirmed list appears at 1:40 PM MT on Friday (as a Telegram confirm would)."""

    def __init__(self, clock_: FakeClock, timeline: Timeline):
        self.clock = clock_
        self.timeline = timeline
        self.announced = False

    def fetch(self, day=None):
        if day != FRIDAY or self.clock() < mt(FRIDAY, 13, 40):
            return []
        if not self.announced:
            self.announced = True
            self.timeline.add(f"Cooper confirms today's picks: {', '.join(p.name for p in PICKS)}. "
                              f"They are bought at once (inside the window), highest delta first.",
                              at=mt(FRIDAY, 13, 40))
        received = mt(FRIDAY, 13, 40)
        return [Signal(f"{p.underlying}-{p.strike:g}{p.right.value[0]}-{EXPIRATION:%Y%m%d}", p.underlying,
                       p.right, p.strike, EXPIRATION, 1, p.asset_class, received, "rehearsal")
                for p in PICKS]


class PrintedAlerts:
    name = "rehearsal (printed, not sent)"

    def __init__(self, timeline: Timeline):
        self.timeline = timeline

    def send(self, alert) -> None:
        self.timeline.add(f"ALERT (not sent): {alert.title}: {alert.message}")


_STRATEGY = {
    "TakeProfit": "take-profit reached",
    "ProfitPointTrail": "10-point rule",
    "TrailingPeak": "dropped from its peak",
    "StopLoss": "stop-loss",
    "HardTimeExit": "end of the selling window",
    "Reprice": "repriced",
}


@contextmanager
def no_network():
    """Refuse every outgoing connection while the rehearsal runs."""
    real = socket.socket.connect

    def refuse(self, *args, **kwargs):
        raise ConnectionRefusedError("the rehearsal never uses the network")

    socket.socket.connect = refuse
    try:
        yield
    finally:
        socket.socket.connect = real


def rehearse(*, config_path: str | Path = "config/rules.example.yaml", workdir: str | Path | None = None,
             verbose: bool = False) -> list[str]:
    """Run the pretend weekend; return the timeline as printable lines."""
    previous_disable = logging.root.manager.disable
    if not verbose:
        logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="coopa-rehearsal-") as tmp, no_network():
        old_lock_dir = os.environ.get("COOPA_TRADA_LOCK_DIR")
        os.environ["COOPA_TRADA_LOCK_DIR"] = str(Path(tmp) / "locks")
        try:
            return _rehearse(Path(workdir or tmp), config_path)
        finally:
            if old_lock_dir is None:
                os.environ.pop("COOPA_TRADA_LOCK_DIR", None)
            else:
                os.environ["COOPA_TRADA_LOCK_DIR"] = old_lock_dir
            logging.disable(previous_disable)


def _rehearse(folder: Path, config_path) -> list[str]:
    from .broker import SchwabBroker
    from .engine import Engine
    from .summary import collect

    fake = FakeClock(mt(FRIDAY, 7, 15))
    timeline = Timeline(fake)
    market = Market(fake)
    schwab = PretendSchwab(fake, timeline, STUCK)
    rules = load_rules(config_path)
    # In memory only: the rehearsal's copy of the rules counts as confirmed.
    rules = replace(rules, setting_confirmations={k: {"confirmed_by_cooper": True} for k in SETTING_LABELS})
    cal = CalendarService()
    login = mt(FRIDAY, 6, 0) - timedelta(days=1)

    def token(now):
        return TokenStatus(TokenState.CONNECTED, "(rehearsal)", "Logged in to Schwab.",
                           login, login + timedelta(days=7), now)

    store = Store.open(folder / "rehearsal.db")
    try:
        broker = SchwabBroker(schwab, ACCOUNT, store, None, sleep=fake.sleep)
        engine = Engine(source=PicksArrive(fake, timeline), resolver=Resolver(market), broker=broker,
                        store=store, rules=rules, cal=cal, mode=broker.mode, quote_source=market,
                        now=fake, token_status=token, sleep=fake.sleep,
                        alerter=Alerter([PrintedAlerts(timeline)], now=fake))
        record_exit = store.record_exit_event

        def narrate_exit(**kw):
            pick = next(p for p in PICKS if p.osi == kw["position_key"].split(":", 1)[1])
            why = _STRATEGY.get(kw["strategy"], kw["strategy"])
            timeline.add_before_last_order(f"Decision: sell {pick.name} ({why}). {_plain_reason(kw)}")
            return record_exit(**kw)

        store.record_exit_event = narrate_exit

        for day in (FRIDAY, MONDAY):
            fake.now = mt(day, 7, 15).astimezone(UTC)
            timeline.add(f"{day:%A}'s run starts (this is when auto-start would start it). "
                         f"Schwab login checked: lasts until {clock.fmt_display(login + timedelta(days=7))}.")
            earliest, cutoff = cal.buy_window(day, rules)
            timeline.add(f"Market opens. Buying window today: {clock.fmt_display(earliest, with_day=False)} "
                         f"to {clock.fmt_display(cutoff, with_day=False)}.", at=cal.session_open(day))
            engine.run(day)
            held = store.open_positions(broker.mode.value)
            if held:
                names = ", ".join(next(p.name for p in PICKS if p.osi == r["osi_symbol"]) for r in held)
                timeline.add(f"Selling window over. No take-profit today, so these are held to the "
                             f"next trading day: {names}. The run ends.")
            else:
                timeline.add("Everything is sold; no new picks today. The run ends.")
            summary = collect(store, day, picks_folder=folder / "imports",
                              autostart={"day": day.isoformat(), "ok": True}, autostart_enabled=True)
            text = summary.message.replace("\n", " / ").replace("Real trading:", "Pretend Schwab:")
            timeline.add(f"End-of-day summary (would go to the phone): {text}")
            if day == FRIDAY:
                for pick in PICKS:
                    q = market.quote(pick.osi)
                    timeline.add(f"  {pick.name} at the close: ${q.mid:.2f} (mid)", at=cal.session_close(day))

        lines = ["COOPA TRADA REHEARSAL: pretend Schwab, fake clock, fake prices.",
                 "Nothing here is real and nothing left this computer.", *timeline.lines(), ""]
        lines.append(_results(store, broker.mode.value))
        return lines
    finally:
        store.close()


def _plain_reason(kw) -> str:
    meta = (kw.get("detail") or {}).get("decision_meta") or {}
    if kw["strategy"] == "Reprice":
        return f"Its earlier sell did not fill within 60 seconds; new limit at the bid, ${kw['trigger_price']:.2f}."
    quote = (kw.get("detail") or {}).get("quote") or {}
    if kw["strategy"] == "ProfitPointTrail" and meta.get("entry_price"):
        entry = meta["entry_price"]
        peak_pct = (meta["peak"] / entry - 1) * 100
        now_pct = (quote.get("mid", entry) / entry - 1) * 100
        return (f"Profit peaked at {peak_pct:+.0f}% and is now {now_pct:+.1f}%, "
                f"{peak_pct - now_pct:.1f} points below. Sell at the bid, ${kw['trigger_price']:.2f}.")
    return f"Price (mid) ${quote.get('mid', 0):.2f}. Sell at the bid, ${kw['trigger_price']:.2f}."


def _results(store: Store, mode: str) -> str:
    rows = [r for r in store.position_report() if r.get("mode") in (None, mode)]
    lines = ["Results (pretend money, 1 contract = 100 options):"]
    total = 0.0
    for r in rows:
        pick = next((p for p in PICKS if p.osi == r.get("osi_symbol")), None)
        entry, exit_ = r.get("entry_price"), r.get("exit_price")
        if pick is None or entry is None or exit_ is None:
            continue
        pnl = (exit_ - entry) * 100
        total += pnl
        lines.append(f"  {pick.name:<14} bought ${entry:.2f}, sold ${exit_:.2f}: {pnl:+.2f}")
    lines.append(f"  Total: {total:+.2f}")
    return "\n".join(lines)
