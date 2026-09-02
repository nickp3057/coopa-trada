from datetime import date, datetime, timezone

import pytest

from coopa_trada.broker import DryRunBroker
from coopa_trada.contracts import ResolvedContract
from coopa_trada.exits import ExitDecision, PositionState, Quote
from coopa_trada.models import AssetClass, Right, Signal
from coopa_trada.store import Store

UTC = timezone.utc
DAY = date(2026, 9, 2)
OSI = "AAPL  260918C00250000"


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "t.db")
    s.record_signal(
        Signal(
            id="sig-1", underlying="AAPL", right=Right.CALL, strike=250.0,
            expiration=date(2026, 9, 18), quantity=3, asset_class=AssetClass.SINGLE_NAME,
            received_at=datetime(2026, 9, 2, 17, 30, tzinfo=UTC), raw="x",
        )
    )
    yield s
    s.close()


def _contract(bid=1.20, ask=1.30):
    return ResolvedContract(
        osi_symbol=OSI, underlying="AAPL", right=Right.CALL, strike=250.0,
        expiration=date(2026, 9, 18), bid=bid, ask=ask, mid=round((bid + ask) / 2, 4),
        open_interest=1000, tradable=True, resolved_at=datetime(2026, 9, 2, 17, 30, tzinfo=UTC),
    )


def _position(qty=3, entry=1.30):
    return PositionState(
        osi_symbol=OSI, entry_price=entry, entry_at=datetime(2026, 9, 2, 19, 40, tzinfo=UTC),
        quantity=qty, session_peak_mid=entry, asset_class=AssetClass.SINGLE_NAME,
        hard_exit_at=datetime(2026, 9, 2, 19, 58, tzinfo=UTC),
    )


def test_entry_fills_at_ask_and_writes_orders_and_fills(store):
    broker = DryRunBroker(store)
    res = broker.place_entry(_contract(bid=1.20, ask=1.30), 3, 1.30, signal_id="sig-1", day=DAY)

    assert res.status == "FILLED"
    assert res.broker_order_id == f"DRY-{res.order_row_id}"
    assert res.broker_order_id.startswith("DRY-")

    order = store.get_order(res.order_row_id)
    assert order["side"] == "BUY_TO_OPEN"
    assert order["intent"] == "ENTRY"
    assert order["mode"] == "DRY-RUN"
    assert order["request_json"] and order["response_json"]  # full request + response persisted

    (fill,) = store.fills_for_order(res.order_row_id)
    assert fill["price"] == 1.30  # buys fill at the ask, never mid
    assert fill["quantity"] == 3


def test_exit_fills_at_bid_from_quote_source(store):
    # need an open position first
    broker = DryRunBroker(store, quote_source=lambda osi: Quote(bid=1.55, ask=1.65, mid=1.60))
    broker.place_entry(_contract(ask=1.30), 3, 1.30, signal_id="sig-1", day=DAY)

    decision = ExitDecision(
        strategy="TakeProfit", reason="mid >= target", price=1.55,
        at=datetime(2026, 9, 2, 19, 50, tzinfo=UTC), meta={},
    )
    res = broker.place_exit(_position(), 1.55, decision, signal_id="sig-1", day=DAY)
    assert res.status == "FILLED"
    (fill,) = store.fills_for_order(res.order_row_id)
    assert fill["price"] == 1.55  # sells fill at the bid, never mid

    order = store.get_order(res.order_row_id)
    assert order["side"] == "SELL_TO_CLOSE"
    assert order["intent"] == "EXIT"


def test_idempotency_dedupe_no_duplicate_rows(store):
    broker = DryRunBroker(store)
    c = _contract(ask=1.30)
    first = broker.place_entry(c, 3, 1.30, signal_id="sig-1", day=DAY)
    second = broker.place_entry(c, 3, 1.30, signal_id="sig-1", day=DAY)

    assert second.deduplicated is True
    assert second.order_row_id == first.order_row_id
    assert len(store.conn.execute("SELECT * FROM orders").fetchall()) == 1
    assert len(store.fills_for_order(first.order_row_id)) == 1  # no second fill


def test_dry_run_rows_mirror_the_live_schema(store):
    """Same tables, same columns populated as the live path would."""
    broker = DryRunBroker(store, quote_source=lambda osi: Quote(bid=1.55, ask=1.65, mid=1.60))
    res = broker.place_entry(_contract(ask=1.30), 3, 1.30, signal_id="sig-1", day=DAY)
    order = dict(store.get_order(res.order_row_id))
    for col in (
        "signal_id", "intent", "idempotency_key", "mode", "osi_symbol", "side",
        "quantity", "limit_price", "status", "broker_order_id", "created_at_utc",
        "request_json", "response_json",
    ):
        assert order[col] is not None, f"{col} not populated"


def test_get_positions_reflects_local_open_state(store):
    broker = DryRunBroker(store)
    broker.place_entry(_contract(ask=1.30), 3, 1.30, signal_id="sig-1", day=DAY)
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0]["instrument"]["symbol"] == OSI
    assert positions[0]["longQuantity"] == 3
