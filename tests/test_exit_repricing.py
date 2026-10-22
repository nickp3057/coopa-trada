"""Task 4: an unfilled sell is canceled and replaced at the current bid after 60 s,
at most 5 times, never as a market order, and safely when a fill races the cancel."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from coopa_trada.alerts import Alerter
from coopa_trada.broker import SchwabBroker
from coopa_trada.calendar_svc import CalendarService
from coopa_trada.contracts import SyntheticResolver
from coopa_trada.engine import Engine
from coopa_trada.exits import Quote
from coopa_trada.models import AssetClass, Right, Signal
from coopa_trada.rules import load_rules
from coopa_trada.store import Store

T0 = datetime(2026, 9, 2, 19, 45, tzinfo=timezone.utc)  # 3:45 PM ET
DAY = date(2026, 9, 2)


def response(payload=None, status=200, **headers):
    return httpx.Response(status, json=payload, headers=headers,
                          request=httpx.Request("GET", "https://example.test"))


class Schwab:
    """A fake Schwab that keeps order state. ``on_cancel`` decides what a cancel does:
    "cancel", "fill_first" (fills just before the cancel lands), "partial" (one
    contract fills, the rest is canceled), "pending" (cancel not done yet),
    "refuse" (Schwab says no: the order already filled), "timeout"."""

    def __init__(self):
        self.orders = []
        self.on_cancel = "cancel"
        self.cancels = []

    def place_order(self, account, spec):
        built = spec.build()
        self.orders.append({"spec": built, "status": "WORKING", "filled": 0, "price": float(built["price"])})
        return response({}, 201, Location=f"https://example.test/orders/{len(self.orders)}")

    def fill(self, oid, qty=None):
        o = self.orders[int(oid) - 1]
        o["filled"] = qty if qty is not None else int(o["spec"]["orderLegCollection"][0]["quantity"])
        if o["filled"] == int(o["spec"]["orderLegCollection"][0]["quantity"]):
            o["status"] = "FILLED"

    def get_order(self, oid, account):
        o = self.orders[int(oid) - 1]
        leg = {**o["spec"]["orderLegCollection"][0], "legId": 1}
        execs = [{"activityType": "EXECUTION", "executionLegs": [
            {"legId": 1, "quantity": o["filled"], "price": o["price"], "time": T0.isoformat()}]}] if o["filled"] else []
        return response({"orderId": int(oid), "status": o["status"], "filledQuantity": o["filled"],
                         "orderLegCollection": [leg], "orderActivityCollection": execs,
                         "orderType": o["spec"]["orderType"], "price": o["price"]})

    def cancel_order(self, oid, account):
        self.cancels.append(oid)
        o = self.orders[int(oid) - 1]
        if self.on_cancel == "timeout":
            raise httpx.ReadTimeout("read timed out")
        if self.on_cancel == "refuse":
            self.fill(oid)
            return response({"error": "order cannot be canceled"}, 400)
        if self.on_cancel == "fill_first":
            self.fill(oid)
        elif self.on_cancel == "partial":
            self.fill(oid, 1)
            o["status"] = "CANCELED"
        elif self.on_cancel == "pending":
            o["status"] = "PENDING_CANCEL"
        else:
            o["status"] = "CANCELED"
        return response({}, 200)

    def sells(self):
        return [o for o in self.orders if o["spec"]["orderLegCollection"][0]["instruction"] == "SELL_TO_CLOSE"]


class Notifier:
    name = "fake"

    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "t.db") as s:
        yield s


def setup(store, *, quantity=1, max_attempts=5):
    """A filled real $1.00 entry, quantity contracts; the engine clock and bid are controllable."""
    schwab, notifier = Schwab(), Notifier()
    broker = SchwabBroker(schwab, "acct", store, None)
    clock, bid = [T0], [1.70]
    rules = replace(load_rules("config/rules.example.yaml"), exit_reprice_max_attempts=max_attempts)
    sig = Signal("s1", "AMD", Right.CALL, 100, date(2026, 9, 4), 1, AssetClass.SINGLE_NAME, T0, "x")
    eng = Engine(source=SimpleNamespace(fetch=lambda day: []), resolver=SyntheticResolver(), broker=broker,
                 store=store, rules=rules, cal=CalendarService(), mode=broker.mode,
                 quote_source=lambda _: Quote(bid[0], round(bid[0] + .02, 2), round(bid[0] + .01, 4)),
                 now=lambda: clock[0], sleep=lambda _: None, alerter=Alerter([notifier]))
    store.record_signal(sig)
    contract = replace(SyntheticResolver().resolve(sig), bid=0.99, ask=1.00, mid=0.995)
    broker.place_entry(contract, quantity, 1.00, signal_id=sig.id, day=DAY)
    schwab.fill(1)
    broker.sync_orders()
    return eng, schwab, notifier, clock, bid


def tick(eng, clock, seconds, bid=None, bids=None):
    clock[0] += timedelta(seconds=seconds)
    if bid is not None:
        bids[0] = bid
    return eng._exit_tick(DAY)


def exits(store):
    return store.conn.execute("SELECT * FROM orders WHERE intent='EXIT' ORDER BY id").fetchall()


def open_sells(schwab):
    return [o for o in schwab.sells() if o["status"] in ("WORKING", "PENDING_CANCEL")]


def test_unfilled_sell_is_replaced_at_the_current_bid_after_60_seconds(store):
    eng, schwab, _, clock, bid = setup(store)
    assert tick(eng, clock, 0) == 1                      # +70%: take-profit sells at the 1.70 bid
    assert tick(eng, clock, 30, 1.65, bid) == 0          # 30 s: too early to touch it
    assert schwab.cancels == []
    assert tick(eng, clock, 31, 1.64, bid) == 1          # 61 s: canceled, replaced at 1.64
    first, second = exits(store)
    assert first["status"] == "CANCELED" and first["cancel_requested_at_utc"]
    assert (second["status"], second["limit_price"], second["reprice_attempt"], second["replaces_order_id"]) == \
        ("WORKING", 1.64, 1, first["id"])
    assert [o["price"] for o in schwab.sells()] == [1.70, 1.64]
    assert len(open_sells(schwab)) == 1                  # never two sells working at once
    reasons = [r["strategy"] for r in store.conn.execute("SELECT strategy FROM exit_events ORDER BY id")]
    assert reasons == ["TakeProfit", "Reprice"]
    # The trade log keeps the original reason and joins the sale across orders.
    schwab.fill(3)
    eng.broker.sync_orders()
    row = store.position_report()[0]
    assert (row["exit_strategy"], row["exit_price"], row["exit_reprices"], row["is_open"]) == \
        ("TakeProfit", 1.64, 1, False)
    assert row["realized_pnl"] == pytest.approx(64.0)


def test_at_most_five_replacements_then_an_alert_and_never_a_market_order(store):
    eng, schwab, notifier, clock, bid = setup(store)
    tick(eng, clock, 0)
    prices = [1.60, 1.50, 1.40, 1.30, 1.20, 1.10, 1.00]
    for p in prices:
        tick(eng, clock, 61, p, bid)
    sells = schwab.sells()
    assert [o["price"] for o in sells] == [1.70, 1.60, 1.50, 1.40, 1.30, 1.20]  # original + 5
    assert all(o["spec"]["orderType"] == "LIMIT" for o in schwab.orders)
    assert len(open_sells(schwab)) == 1
    kinds = [a.kind for a in notifier.sent]
    assert "exit_reprice_exhausted" in kinds
    exhausted = next(a for a in notifier.sent if a.kind == "exit_reprice_exhausted")
    assert "after 5 price changes" in exhausted.message and "$" not in exhausted.message


def test_fill_arriving_while_the_cancel_is_in_flight_is_not_sold_again(store):
    eng, schwab, _, clock, bid = setup(store)
    tick(eng, clock, 0)
    schwab.on_cancel = "fill_first"
    assert tick(eng, clock, 61, 1.60, bid) == 0
    assert len(schwab.sells()) == 1 and schwab.sells()[0]["status"] == "FILLED"
    assert not store.open_positions("LIVE")
    assert tick(eng, clock, 61, 1.50, bid) == 0 and len(schwab.sells()) == 1


def test_cancel_refused_because_it_already_filled(store):
    eng, schwab, notifier, clock, bid = setup(store)
    tick(eng, clock, 0)
    schwab.on_cancel = "refuse"
    assert tick(eng, clock, 61, 1.60, bid) == 0
    assert exits(store)[0]["status"] == "FILLED" and exits(store)[0]["cancel_requested_at_utc"] is None
    assert len(schwab.sells()) == 1 and not store.open_positions("LIVE")
    assert not [a for a in notifier.sent if a.kind == "exit_failed"]


def test_partial_fill_before_the_cancel_replaces_only_the_rest(store):
    eng, schwab, _, clock, bid = setup(store, quantity=2)
    tick(eng, clock, 0)
    schwab.on_cancel = "partial"
    assert tick(eng, clock, 61, 1.60, bid) == 1
    first, second = schwab.sells()
    assert first["filled"] == 1 and first["status"] == "CANCELED"
    assert int(second["spec"]["orderLegCollection"][0]["quantity"]) == 1
    assert store.open_positions("LIVE")[0]["quantity"] == 1


def test_pending_cancel_waits_for_schwab_before_replacing(store):
    eng, schwab, _, clock, bid = setup(store)
    tick(eng, clock, 0)
    schwab.on_cancel = "pending"
    assert tick(eng, clock, 61, 1.60, bid) == 0
    assert len(schwab.sells()) == 1                      # no replacement while it might still fill
    assert tick(eng, clock, 10, 1.60, bid) == 0 and len(schwab.cancels) == 1
    schwab.orders[1]["status"] = "CANCELED"              # Schwab finishes the cancel
    assert tick(eng, clock, 10, 1.58, bid) == 1
    assert [o["price"] for o in schwab.sells()] == [1.70, 1.58]


def test_cancel_that_times_out_never_leads_to_a_second_sell_until_schwab_confirms(store):
    eng, schwab, _, clock, bid = setup(store)
    tick(eng, clock, 0)
    schwab.on_cancel = "timeout"
    assert tick(eng, clock, 61, 1.60, bid) == 0           # the cancel may or may not have landed
    assert len(schwab.sells()) == 1
    assert exits(store)[0]["cancel_requested_at_utc"]     # marked as possibly in flight
    assert tick(eng, clock, 30, 1.60, bid) == 0 and len(schwab.cancels) == 1
    schwab.on_cancel = "cancel"
    assert tick(eng, clock, 31, 1.60, bid) == 0           # mark expired: asks again next time
    assert tick(eng, clock, 1, 1.60, bid) == 1
    assert len(open_sells(schwab)) == 1


def test_restart_after_cancel_confirmed_places_the_replacement(store, tmp_path):
    eng, schwab, _, clock, bid = setup(store)
    tick(eng, clock, 0)
    # Simulate a crash right after Schwab confirmed the cancel: mark set, order CANCELED.
    row = exits(store)[0]
    store.set_cancel_requested(row["id"], clock[0])
    schwab.orders[1]["status"] = "CANCELED"
    eng.broker.sync_orders()
    assert tick(eng, clock, 5, 1.61, bid) == 1
    assert [o["price"] for o in schwab.sells()] == [1.70, 1.61]


def test_a_canceled_order_the_bot_did_not_cancel_is_never_replaced(store):
    eng, schwab, notifier, clock, bid = setup(store)
    tick(eng, clock, 0)
    schwab.orders[1]["status"] = "CANCELED"   # canceled by someone in Schwab, not the bot
    assert tick(eng, clock, 61, 1.60, bid) == 0
    assert len(schwab.sells()) == 1
    assert [a.kind for a in notifier.sent] == ["exit_failed"]


def test_only_ever_lowers_the_price(store):
    eng, schwab, _, clock, bid = setup(store)
    tick(eng, clock, 0)
    assert tick(eng, clock, 61, 1.70, bid) == 0 and tick(eng, clock, 61, 1.80, bid) == 0
    assert schwab.cancels == []


def test_repricing_off_leaves_the_order_alone(store):
    eng, schwab, _, clock, bid = setup(store, max_attempts=0)
    tick(eng, clock, 0)
    assert tick(eng, clock, 120, 1.50, bid) == 0 and schwab.cancels == []


def test_unfilled_alert_counts_from_the_first_sell_order(store):
    eng, schwab, notifier, clock, bid = setup(store)
    tick(eng, clock, 0)
    for p in (1.60, 1.50, 1.40):
        tick(eng, clock, 61, p, bid)
    assert "exit_unfilled" in [a.kind for a in notifier.sent]   # 3+ minutes since the first sell


def test_loop_wakes_up_when_a_reprice_is_due(store):
    eng, schwab, _, clock, bid = setup(store)
    tick(eng, clock, 0)
    clock[0] += timedelta(seconds=45)
    assert eng._next_sleep() == pytest.approx(15)


def test_reprice_settings_validate(tmp_path):
    base = open("config/rules.example.yaml").read()
    rules = load_rules("config/rules.example.yaml")
    assert (rules.exit_reprice_after_seconds, rules.exit_reprice_max_attempts) == (60, 5)
    for bad, key in (("exit_reprice_after_seconds: 5", "exit_reprice_after_seconds"),
                     ("exit_reprice_max_attempts: -1", "exit_reprice_max_attempts")):
        p = tmp_path / "r.yaml"
        p.write_text("\n".join(l for l in base.splitlines() if not l.startswith(key + ":")) + "\n" + bad)
        with pytest.raises(ValueError, match=key):
            load_rules(p)
