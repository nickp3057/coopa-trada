"""Regression coverage for order lifecycle, restart behavior and mode isolation."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from coopa_trada.broker import DryRunBroker, SchwabBroker
from coopa_trada.config import Mode
from coopa_trada.contracts import SyntheticResolver
from coopa_trada.engine import Engine
from coopa_trada.exits import Quote
from coopa_trada.models import AssetClass, Right, Signal
from coopa_trada.rules import SETTING_LABELS, FixedQuantitySizer, load_rules
from coopa_trada.schwab_token import TokenState, TokenStatus
from coopa_trada.store import Store
from coopa_trada.calendar_svc import CalendarService

DAY = date(2026, 9, 2)
NOW = datetime(2026, 9, 2, 19, 50, tzinfo=timezone.utc)


def signal():
    return Signal("test", "AMD", Right.CALL, 100, date(2026, 9, 4), 1,
                  AssetClass.SINGLE_NAME, NOW, "test")


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "test.db") as store:
        yield store


def response(payload=None, status=200, **headers):
    return httpx.Response(status, json=payload, headers=headers,
                          request=httpx.Request("GET", "https://example.test"))


class Client:
    def __init__(self):
        self.submitted = []
        self.polls = {}
        self.reject = False
        self.delay = False

    def place_order(self, account, spec):
        if self.reject:
            return response({"error": "rejected"}, 400)
        self.submitted.append(spec.build())
        return response({}, 201, Location=f"https://example.test/orders/{len(self.submitted)}")

    def get_order(self, oid, account):
        spec = self.submitted[int(oid) - 1]
        self.polls[oid] = self.polls.get(oid, 0) + 1
        filled = not self.delay or int(oid) != 1 or self.polls[oid] > 1
        leg = {**spec["orderLegCollection"][0], "legId": 1}
        return response({
            "orderId": int(oid), "status": "FILLED" if filled else "WORKING",
            "filledQuantity": 1 if filled else 0, "orderLegCollection": [leg],
            "orderActivityCollection": [{"activityType": "EXECUTION", "executionLegs": [
                {"legId": 1, "quantity": 1, "price": float(spec["price"]), "time": NOW.isoformat()}
            ]}] if filled else [],
        })

    def get_account(self, *_args, **_kwargs):
        return response({"securitiesAccount": {"positions": []}})

    def get_orders_for_account(self, *_args):
        return response([])


def confirmations(*unconfirmed):
    return {key: {"confirmed_by_cooper": key not in unconfirmed} for key in SETTING_LABELS}


def logged_in(now):
    """A fake Schwab login made at NOW, valid for 7 days."""
    return TokenStatus(TokenState.CONNECTED, "fake", "Logged in to Schwab.",
                       NOW, NOW + timedelta(days=7), now)


def engine(store, broker, now=lambda: NOW, sleep=lambda _: None):
    rules = replace(load_rules("config/rules.example.yaml"), setting_confirmations=confirmations(),
                    cooper_drop_from_entry_pct=None)
    return Engine(source=SimpleNamespace(fetch=lambda day: [signal()]),
                  resolver=SyntheticResolver(base_price=1), broker=broker, store=store,
                  rules=rules, cal=CalendarService(), mode=broker.mode,
                  quote_source=lambda _: Quote(1, 1.02, 1.01), now=now, sleep=sleep,
                  token_status=logged_in)


def test_actual_fills_are_recorded_once_with_actual_time_and_price(store):
    client = Client()
    broker = SchwabBroker(client, "account", store, None)
    sig = signal()
    store.record_signal(sig)
    contract = SyntheticResolver().resolve(sig)
    order = broker.place_entry(contract, 1, contract.ask, signal_id=sig.id, day=DAY)
    assert not store.open_positions("LIVE")
    broker.sync_orders()
    broker.sync_orders()
    fills = store.fills_for_order(order.order_row_id)
    assert len(fills) == 1
    assert fills[0]["price"] == contract.ask
    assert fills[0]["filled_at_utc"] == NOW.isoformat()
    assert store.open_positions("LIVE")[0]["quantity"] == 1


def test_dry_and_live_same_pick_have_distinct_orders_and_positions(store):
    client = Client()
    live = SchwabBroker(client, "account", store, None)
    sig = signal()
    store.record_signal(sig)
    contract = SyntheticResolver().resolve(sig)
    dry = DryRunBroker(store)
    simulated = dry.place_entry(contract, 1, contract.ask, signal_id=sig.id, day=DAY)
    actual = live.place_entry(contract, 1, contract.ask, signal_id=sig.id, day=DAY)
    assert not actual.deduplicated
    assert simulated.idempotency_key != actual.idempotency_key
    live.sync_orders()
    assert len(store.open_positions("DRY-RUN")) == 1
    assert len(store.open_positions("LIVE")) == 1
    assert all(p["mode"] == "LIVE" for p in engine(store, live).reconcile().local_open_positions)
    eng = engine(store, dry)
    eng.quote_source = lambda _: Quote(.01, .02, .015)
    eng._exit_tick(DAY)
    assert not store.open_positions("DRY-RUN")
    assert len(store.open_positions("LIVE")) == 1
    reports = {r["mode"]: r for r in store.position_report()}
    assert reports["LIVE"]["is_open"]
    assert not reports["DRY-RUN"]["is_open"]


def test_peak_survives_new_engine_and_database_connection(store):
    sig = signal()
    store.record_signal(sig)
    contract = replace(SyntheticResolver().resolve(sig), bid=.99, ask=1, mid=.995)
    broker = DryRunBroker(store)
    broker.place_entry(contract, 1, 1, signal_id=sig.id, day=DAY)
    eng = engine(store, broker)
    eng.quote_source = lambda _: Quote(1.49, 1.51, 1.5)
    assert eng._exit_tick(DAY) == 0
    db = store.conn.execute("PRAGMA database_list").fetchone()["file"]
    with Store.open(db) as fresh:
        restarted = engine(fresh, DryRunBroker(fresh))
        restarted.quote_source = lambda _: Quote(1.29, 1.31, 1.3)
        assert restarted._exit_tick(DAY) == 1
        assert not fresh.open_positions("DRY-RUN")
        assert fresh.conn.execute("SELECT strategy FROM exit_events").fetchone()[0] == "TrailingPeak"


def test_rejection_is_not_recorded_as_working_or_retried(store):
    client = Client()
    client.reject = True
    broker = SchwabBroker(client, "account", store, None)
    store.record_signal(signal())
    contract = SyntheticResolver().resolve(signal())
    with pytest.raises(httpx.HTTPStatusError):
        broker.place_entry(contract, 1, contract.ask, signal_id="test", day=DAY)
    assert store.conn.execute("SELECT status FROM orders").fetchone()[0] == "ERROR"
    with pytest.raises(RuntimeError, match="without broker ID"):
        broker.sync_orders()
    assert not store.open_positions("LIVE")


def test_delayed_entry_is_monitored_through_exit_fill(store):
    client = Client()
    client.delay = True
    live = SchwabBroker(client, "account", store, None)
    now = [NOW]
    def sleep(_seconds):
        now[0] += timedelta(seconds=_seconds)
        assert now[0].hour < 20, "engine failed to finish before close"
    eng = engine(store, live, now=lambda: now[0], sleep=sleep)
    # Keep price steady until the hard exit deadline.
    contract = eng.resolver.resolve(signal())
    eng.quote_source = lambda _: Quote(contract.bid, contract.ask, contract.mid)
    eng.run(DAY)
    assert len(client.submitted) == 2
    assert client.submitted[1]["orderLegCollection"][0]["instruction"] == "SELL_TO_CLOSE"
    assert not store.open_positions("LIVE")
    assert not store.orders_open("LIVE")


def test_cap_uses_ask_and_pending_entries_reserve_slots(store):
    assert FixedQuantitySizer(100).size(None, SimpleNamespace(mid=.99, ask=1.01), None) == 0
    client = Client()
    eng = engine(store, SchwabBroker(client, "account", store, None))
    eng.rules = replace(eng.rules, max_concurrent_positions=1)
    eng.source = SimpleNamespace(fetch=lambda day: [signal(), replace(signal(), id="second", underlying="QQQ")])
    eng._entry_pass(DAY)
    assert len(client.submitted) == 1


@pytest.mark.parametrize("key", list(SETTING_LABELS))
def test_each_unconfirmed_setting_blocks_live_before_any_broker_call(store, key):
    client = Client()
    eng = engine(store, SchwabBroker(client, "account", store, None))
    eng.rules = replace(eng.rules, setting_confirmations=confirmations(key))
    with pytest.raises(ValueError, match=f"unconfirmed settings: {SETTING_LABELS[key]}\\."):
        eng.run(DAY)
    assert not client.submitted
    assert not store.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]


def test_unconfirmed_settings_never_block_dry_run(store):
    eng = engine(store, DryRunBroker(store))
    eng.rules = replace(eng.rules, setting_confirmations=confirmations(*SETTING_LABELS))
    eng.run(DAY, once=True)
    assert store.conn.execute(
        "SELECT COUNT(*) FROM orders WHERE mode = 'DRY-RUN' AND intent = 'ENTRY'").fetchone()[0] == 1


def test_live_rules_and_historical_dates_are_blocked(store):
    eng = engine(store, SchwabBroker(Client(), "account", store, None))
    eng.rules = replace(eng.rules, setting_confirmations=confirmations(*SETTING_LABELS))
    with pytest.raises(ValueError, match="unconfirmed"):
        eng.run(DAY)
    eng.rules = replace(eng.rules, setting_confirmations=confirmations())
    with pytest.raises(ValueError, match="today"):
        eng.run(date(2026, 9, 1))
    with pytest.raises(ValueError, match="continuous monitoring"):
        eng.run(DAY, once=True)


def test_account_binding_prevents_switching_account(store):
    SchwabBroker(Client(), "first", store, None)
    with pytest.raises(ValueError, match="another Schwab account"):
        SchwabBroker(Client(), "second", store, None)


def test_same_contract_on_later_day_is_a_new_position(store):
    sig = signal()
    store.record_signal(sig)
    contract = SyntheticResolver().resolve(sig)
    dry = DryRunBroker(store)
    first = dry.place_entry(contract, 1, contract.ask, signal_id=sig.id, day=DAY)
    eng = engine(store, dry)
    eng.quote_source = lambda _: Quote(.01, .02, .015)
    eng._exit_tick(DAY)
    assert not store.open_positions("DRY-RUN")
    later = DAY + timedelta(days=1)
    second = dry.place_entry(contract, 1, contract.ask, signal_id=sig.id, day=later)
    assert not second.deduplicated
    assert first.order_row_id != second.order_row_id
    assert len(store.open_positions("DRY-RUN")) == 1
    report = store.position_report()
    assert len(report) == 2
    assert not report[0]["is_open"]
    assert report[1]["is_open"]


def test_pending_exit_is_not_duplicated_after_date_changes(store):
    client = Client()
    live = SchwabBroker(client, "account", store, None)
    eng = engine(store, live)
    eng._entry_pass(DAY)
    live.sync_orders()
    state, sid = eng._positions_state(DAY)[0]
    from coopa_trada.exits import ExitDecision
    decision = ExitDecision("test", "test", .01, NOW)
    first = live.place_exit(state, .01, decision, signal_id=sid, day=DAY)
    second = live.place_exit(state, .01, decision, signal_id=sid, day=DAY + timedelta(days=1))
    assert second.deduplicated
    assert first.order_row_id == second.order_row_id
    assert len(client.submitted) == 2  # one entry, one exit


def test_execution_snapshots_are_idempotent_and_reject_regression(store):
    client = Client()
    live = SchwabBroker(client, "account", store, None)
    eng = engine(store, live)
    eng._entry_pass(DAY)
    order = store.orders_open("LIVE")[0]
    payload = client.get_order("1", "account").json()
    store.sync_order(order["id"], payload)
    store.sync_order(order["id"], payload)
    assert len(store.fills_for_order(order["id"])) == 1
    bad = {**payload, "status": "WORKING", "filledQuantity": 0, "orderActivityCollection": []}
    with pytest.raises(ValueError, match="regressed"):
        store.sync_order(order["id"], bad)
    assert len(store.fills_for_order(order["id"])) == 1


def test_cancellation_racing_with_a_fill_is_still_exited(store):
    class RacingClient(Client):
        canceled = False
        def get_order(self, oid, account):
            payload = super().get_order(oid, account).json()
            if oid == "1" and not self.canceled:
                payload.update(status="WORKING", filledQuantity=0, orderActivityCollection=[])
            return response(payload)
        def cancel_order(self, oid, account):
            self.canceled = True  # The entry filled just before cancellation reached Schwab.
            return response({}, 200)
    client = RacingClient()
    now = [NOW.replace(minute=57)]
    def sleep(seconds):
        now[0] += timedelta(seconds=seconds)
        assert now[0] <= NOW.replace(hour=20, minute=0)
    eng = engine(store, SchwabBroker(client, "account", store, None), now=lambda: now[0], sleep=sleep)
    eng.run(DAY)
    assert client.canceled
    assert len(client.submitted) == 2
    assert not store.open_positions("LIVE")
