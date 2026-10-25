"""Uncertain order submissions: look up, never resend; halt the contract if still unknown."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from coopa_trada.broker import OrderNotPlaced, OrderOutcomeUnknown, SchwabBroker
from coopa_trada.contracts import SyntheticResolver
from coopa_trada.exits import ExitDecision, Quote
from coopa_trada.store import Store
from test_trading_regressions import DAY, NOW, Client, engine, response, signal


class FlakyClient(Client):
    """Fake Schwab whose place_order can fail in the ways a network can.

    ``reaches_schwab``: whether the order is recorded at "Schwab" despite the error.
    ``visible_after``: lookups before a recorded order shows up in the order list.
    """

    def __init__(self, error=None, *, reaches_schwab=False, visible_after=0, no_location=False):
        super().__init__()
        self.error = error
        self.reaches_schwab = reaches_schwab
        self.visible_after = visible_after
        self.no_location = no_location
        self.place_calls = 0
        self.lookups = 0
        self.lookup_error = None
        self.extra_orders = []

    def place_order(self, account, spec):
        self.place_calls += 1
        if self.error is None and not self.no_location:
            return super().place_order(account, spec)
        if self.error is None or self.reaches_schwab:
            self.submitted.append(spec.build())
        if self.no_location:
            return response({}, 201)
        raise self.error

    def get_orders_for_account(self, account, **kwargs):
        assert kwargs["from_entered_datetime"] < kwargs["to_entered_datetime"]
        self.lookups += 1
        if self.lookup_error is not None:
            raise self.lookup_error
        visible = self.lookups > self.visible_after
        orders = [self._as_listed(i + 1, spec) for i, spec in enumerate(self.submitted)] if visible else []
        return response(orders + self.extra_orders)

    @staticmethod
    def _as_listed(order_id, spec):
        leg = {**spec["orderLegCollection"][0], "legId": 1}
        return {**spec, "orderId": order_id, "status": "WORKING", "filledQuantity": 0,
                "orderLegCollection": [leg], "orderActivityCollection": [], "enteredTime": NOW.isoformat()}


def entry_spec(osi_symbol, price):
    from schwab.orders.options import option_buy_to_open_limit
    return option_buy_to_open_limit(osi_symbol, 1, f"{price:.2f}").build()


def timeout():
    return httpx.ReadTimeout("read timed out", request=httpx.Request("POST", "https://example.test"))


def connect_error():
    return httpx.ConnectError("connection refused", request=httpx.Request("POST", "https://example.test"))


def server_error():
    return httpx.HTTPStatusError("503", request=httpx.Request("POST", "https://example.test"),
                                 response=httpx.Response(503))


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "test.db") as s:
        yield s


def broker_for(client, store, sleeps=None):
    return SchwabBroker(client, "account", store, None,
                        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)))


def place(broker, store, sig=None):
    sig = sig or signal()
    store.record_signal(sig)
    contract = SyntheticResolver().resolve(sig)
    return broker.place_entry(contract, 1, contract.ask, signal_id=sig.id, day=DAY), contract


def only_order(store):
    return store.conn.execute("SELECT * FROM orders").fetchone()


# -- confirmed placed --------------------------------------------------------
@pytest.mark.parametrize("make_client", [
    lambda: FlakyClient(timeout(), reaches_schwab=True),
    lambda: FlakyClient(server_error(), reaches_schwab=True),
    lambda: FlakyClient(no_location=True),  # 201 without an order ID
])
def test_confirmed_placed_is_adopted_not_resent(store, make_client):
    client = make_client()
    result, contract = place(broker_for(client, store), store)
    assert client.place_calls == 1
    assert result.broker_order_id == "1"
    row = only_order(store)
    assert (row["status"], row["broker_order_id"]) == ("WORKING", "1")
    assert store.halted_symbols("LIVE") == set()
    assert store.open_incidents() == []  # resolved automatically
    incident = store.conn.execute("SELECT * FROM order_incidents").fetchone()
    assert incident["outcome"] == "PLACED" and "order 1" in incident["resolution"]


def test_order_that_appears_late_is_still_found(store):
    sleeps = []
    client = FlakyClient(timeout(), reaches_schwab=True, visible_after=2)
    result, _ = place(broker_for(client, store, sleeps), store)
    assert result.broker_order_id == "1"
    assert client.lookups == 3 and sleeps == [5.0, 5.0]
    assert client.place_calls == 1


def test_already_tracked_order_is_not_mistaken_for_the_lost_one(store):
    first = FlakyClient()
    broker = broker_for(first, store)
    place(broker, store)  # a normal, tracked order with broker ID 1
    first.error, first.reaches_schwab = timeout(), False
    later = replace(signal(), id="again")
    with pytest.raises(OrderOutcomeUnknown):
        place(broker, store, later)
    assert first.place_calls == 2


# -- confirmed not placed ------------------------------------------------------
def test_connection_never_opened_is_confirmed_not_placed(store):
    client = FlakyClient(connect_error())
    with pytest.raises(OrderNotPlaced, match="not resent"):
        place(broker_for(client, store), store)
    assert client.place_calls == 1 and client.submitted == []
    assert only_order(store)["status"] == "NOT_PLACED"
    assert store.orders_open("LIVE") == []
    assert store.halted_symbols("LIVE") == set()
    [incident] = store.open_incidents()
    assert incident["outcome"] == "NOT_PLACED"  # still shown to a person


# -- still unknown ------------------------------------------------------------
@pytest.mark.parametrize("make_client", [
    lambda: FlakyClient(timeout()),
    lambda: FlakyClient(server_error()),
    lambda: FlakyClient(RuntimeError("unexpected")),
])
def test_unknown_outcome_halts_the_contract_and_is_never_resent(store, make_client):
    client = make_client()
    broker = broker_for(client, store)
    with pytest.raises(OrderOutcomeUnknown, match="halted"):
        place(broker, store)
    assert client.place_calls == 1 and client.lookups == 3
    row = only_order(store)
    assert row["status"] == "UNKNOWN"
    assert store.halted_symbols("LIVE") == {row["osi_symbol"]}
    [incident] = store.open_incidents()
    assert incident["outcome"] == "UNKNOWN"
    broker.sync_orders()  # an unknown order does not block polling of others
    again, _ = place(broker, store)  # same pick again: deduplicated, not resent
    assert again.deduplicated and client.place_calls == 1


def test_connect_error_with_failed_lookup_stays_unknown(store):
    client = FlakyClient(connect_error())
    client.lookup_error = RuntimeError("lookup failed")
    with pytest.raises(OrderOutcomeUnknown):
        place(broker_for(client, store), store)
    assert only_order(store)["status"] == "UNKNOWN"


def test_two_matching_orders_are_ambiguous(store):
    client = FlakyClient(timeout(), reaches_schwab=True)
    broker = broker_for(client, store)
    store.record_signal(signal())
    contract = SyntheticResolver().resolve(signal())
    client.extra_orders = [FlakyClient._as_listed(99, entry_spec(contract.osi_symbol, contract.ask))]
    with pytest.raises(OrderOutcomeUnknown):
        broker.place_entry(contract, 1, contract.ask, signal_id="test", day=DAY)
    detail = store.conn.execute("SELECT detail_json FROM order_incidents").fetchone()[0]
    assert '"ambiguous": true' in detail


def test_definite_rejection_keeps_strict_error_path(store):
    client = FlakyClient(httpx.HTTPStatusError("400", request=httpx.Request("POST", "https://x"),
                                               response=httpx.Response(400)))
    with pytest.raises(httpx.HTTPStatusError):
        place(broker_for(client, store), store)
    assert only_order(store)["status"] == "ERROR"
    assert client.lookups == 0


# -- engine behaviour ------------------------------------------------------------
def test_entry_pass_continues_after_unknown_and_skips_halted_contract(store):
    client = FlakyClient(timeout())
    eng = engine(store, broker_for(client, store))
    second = replace(signal(), id="second", underlying="QQQ")
    eng.source = SimpleNamespace(fetch=lambda day: [signal(), second])
    # The unknown order reserves its cost in the total budget (it may be live at
    # Schwab); give room for both so the test isolates "the pass continues".
    eng.rules = replace(eng.rules, total_budget=10_000)
    original = client.place_order

    def fail_first_only(account, spec):
        if client.place_calls == 0:
            return original(account, spec)
        client.error = None
        return Client.place_order(client, account, spec)
    client.place_order = fail_first_only
    eng._entry_pass(DAY)
    assert store.conn.execute("SELECT COUNT(*) FROM orders WHERE status='UNKNOWN'").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM orders WHERE status='WORKING'").fetchone()[0] == 1

    # A new pick for the same halted contract is skipped, not submitted.
    calls = client.place_calls
    eng.source = SimpleNamespace(fetch=lambda day: [replace(signal(), id="new-pick")])
    eng._entry_pass(DAY + timedelta(days=1))
    skip = store.conn.execute("SELECT category FROM skip_events WHERE category='halted'").fetchall()
    assert skip and client.place_calls == calls


def test_halted_position_gets_no_exit_order(store):
    client = FlakyClient()
    broker = broker_for(client, store)
    eng = engine(store, broker)
    eng._entry_pass(DAY)
    broker.sync_orders()  # fills the entry
    state, sid = eng._positions_state(DAY)[0]
    client.error = timeout()
    with pytest.raises(OrderOutcomeUnknown):
        broker.place_exit(state, .5, ExitDecision("t", "t", .5, NOW), signal_id=sid, day=DAY)
    calls = client.place_calls
    eng.quote_source = lambda _: Quote(.01, .02, .015)  # would trigger every stop
    assert eng._exit_tick(DAY) == 0
    assert client.place_calls == calls


# -- human resolution -----------------------------------------------------------
def test_person_confirms_placed_with_order_number(store):
    client = FlakyClient(timeout())
    broker = broker_for(client, store)
    with pytest.raises(OrderOutcomeUnknown):
        place(broker, store)
    [incident] = store.open_incidents()
    with pytest.raises(ValueError, match="note"):
        store.resolve_incident(incident["id"], placed=True, note=" ", broker_order_id="1")
    with pytest.raises(ValueError, match="order number"):
        store.resolve_incident(incident["id"], placed=True, note="checked", broker_order_id="abc")
    # The person found it at Schwab: the fake now knows the order as ID 1.
    client.submitted.append(entry_spec(only_order(store)["osi_symbol"], only_order(store)["limit_price"]))
    store.resolve_incident(incident["id"], placed=True, note="Schwab Orders tab", broker_order_id="1")
    assert store.halted_symbols("LIVE") == set() and store.open_incidents() == []
    assert (only_order(store)["status"], only_order(store)["broker_order_id"]) == ("WORKING", "1")
    broker.sync_orders()  # back under normal polling: the fake reports it filled
    assert only_order(store)["status"] == "FILLED"


def test_person_confirms_not_placed(store):
    client = FlakyClient(timeout())
    with pytest.raises(OrderOutcomeUnknown):
        place(broker_for(client, store), store)
    [incident] = store.open_incidents()
    store.resolve_incident(incident["id"], placed=False, note="not in Schwab Orders")
    assert only_order(store)["status"] == "NOT_PLACED"
    assert store.halted_symbols("LIVE") == set()
    with pytest.raises(ValueError, match="No open incident"):
        store.resolve_incident(incident["id"], placed=False, note="again")


def test_attention_endpoints(tmp_path):
    pytest.importorskip("flask")
    from coopa_trada.web import create_app
    from coopa_trada.web.config import WebPaths
    from coopa_trada.web.runner import EngineRunner
    paths = WebPaths(config_path="config/rules.example.yaml", db_path=str(tmp_path / "web.db"),
                     signals_path="data/signals.example.csv")
    with Store.open(paths.db_path) as store:
        with pytest.raises(OrderOutcomeUnknown):
            place(broker_for(FlakyClient(timeout()), store), store)
    web = create_app(paths, EngineRunner(paths)).test_client()
    [item] = web.get("/api/attention").get_json()["incidents"]
    assert item["outcome"] == "UNKNOWN" and item["side"] == "BUY_TO_OPEN"
    assert web.post(f"/api/attention/{item['id']}/resolve", json={"note": "x"}).status_code == 400
    rv = web.post(f"/api/attention/{item['id']}/resolve", json={"placed": False, "note": "not in Schwab"})
    assert rv.status_code == 200 and rv.get_json()["incidents"] == []
    page = web.get("/reconcile").get_data(as_text=True)
    assert "Orders that need your attention" in page
