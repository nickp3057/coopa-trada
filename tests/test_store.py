from datetime import date, datetime, timezone

import pytest

from coopa_trada.clock import parse_iso
from coopa_trada.idempotency import Intent, make_key
from coopa_trada.models import AssetClass, Right, Signal
from coopa_trada.store import Store, position_key

UTC = timezone.utc


def _signal(sid="sig-1"):
    return Signal(
        id=sid, underlying="AAPL", right=Right.CALL, strike=250.0,
        expiration=date(2026, 9, 18), quantity=3, asset_class=AssetClass.SINGLE_NAME,
        received_at=datetime(2026, 9, 2, 17, 30, tzinfo=UTC), raw="raw payload",
    )


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "t.db")
    yield s
    s.close()


def test_ddl_is_idempotent(tmp_path):
    Store.open(tmp_path / "t.db").close()
    Store.open(tmp_path / "t.db").close()  # second open must not raise


def test_record_signal_is_idempotent_on_id(store):
    store.record_signal(_signal())
    store.record_signal(_signal())
    rows = store.conn.execute("SELECT * FROM signals").fetchall()
    assert len(rows) == 1


def test_idempotency_key_unique_constraint(store):
    store.record_signal(_signal())
    key = make_key("sig-1", Intent.ENTRY, date(2026, 9, 2))
    oid1 = store.record_order(
        signal_id="sig-1", intent=Intent.ENTRY, idempotency_key=key, mode="DRY-RUN",
        osi_symbol="AAPL  260918C00250000", side="BUY_TO_OPEN", quantity=3,
        limit_price=1.20, status="PENDING", request={"a": 1},
    )
    oid2 = store.record_order(
        signal_id="sig-1", intent=Intent.ENTRY, idempotency_key=key, mode="DRY-RUN",
        osi_symbol="AAPL  260918C00250000", side="BUY_TO_OPEN", quantity=3,
        limit_price=1.20, status="PENDING", request={"a": 2},
    )
    assert oid1 == oid2
    assert len(store.conn.execute("SELECT * FROM orders").fetchall()) == 1
    assert store.find_order_by_idempotency_key(key)["id"] == oid1


def test_round_trip_reconstructed_from_db_only(store):
    """The trade log alone answers: what did we buy, at what price, when, and
    what did we sell it for."""
    store.record_signal(_signal())
    day = date(2026, 9, 2)
    osi = "AAPL  260918C00250000"

    entry_key = make_key("sig-1", Intent.ENTRY, day)
    entry_id = store.record_order(
        signal_id="sig-1", intent=Intent.ENTRY, idempotency_key=entry_key, mode="DRY-RUN",
        osi_symbol=osi, side="BUY_TO_OPEN", quantity=3, limit_price=1.25,
        status="PENDING", request={"kind": "buy_to_open_limit", "limit": 1.25},
    )
    store.update_order_status(entry_id, "FILLED", broker_order_id="DRY-1", response={"ok": True})
    store.record_fill(
        order_id=entry_id, quantity=3, price=1.25, mode="DRY-RUN",
        filled_at=datetime(2026, 9, 2, 19, 40, tzinfo=UTC), raw={"fill": "ask"},
    )

    exit_key = make_key("sig-1", Intent.EXIT, day)
    exit_id = store.record_order(
        signal_id="sig-1", intent=Intent.EXIT, idempotency_key=exit_key, mode="DRY-RUN",
        osi_symbol=osi, side="SELL_TO_CLOSE", quantity=3, limit_price=1.90,
        status="PENDING", request={"kind": "sell_to_close_limit", "limit": 1.90},
    )
    store.update_order_status(exit_id, "FILLED", broker_order_id="DRY-2", response={"ok": True})
    store.record_fill(
        order_id=exit_id, quantity=3, price=1.90, mode="DRY-RUN",
        filled_at=datetime(2026, 9, 2, 19, 55, tzinfo=UTC), raw={"fill": "bid"},
    )

    # --- reconstruct using nothing but the DB ---
    fresh = Store(store.conn)
    rows = fresh.conn.execute(
        """
        SELECT o.intent, o.osi_symbol, o.side, f.quantity, f.price, f.filled_at_utc
        FROM orders o JOIN fills f ON f.order_id = o.id
        ORDER BY f.id
        """
    ).fetchall()
    assert len(rows) == 2
    buy, sell = rows
    assert (buy["intent"], buy["side"], buy["price"]) == ("ENTRY", "BUY_TO_OPEN", 1.25)
    assert (sell["intent"], sell["side"], sell["price"]) == ("EXIT", "SELL_TO_CLOSE", 1.90)
    assert parse_iso(buy["filled_at_utc"]) < parse_iso(sell["filled_at_utc"])
    realized = (sell["price"] - buy["price"]) * buy["quantity"] * 100
    assert realized == pytest.approx(195.0)


def test_open_positions_excludes_closed_round_trips(store):
    store.record_signal(_signal("s1"))
    store.record_signal(_signal("s2"))
    day = date(2026, 9, 2)
    osi = "AAPL  260918C00250000"

    for sid in ("s1", "s2"):
        oid = store.record_order(
            signal_id=sid, intent=Intent.ENTRY, idempotency_key=make_key(sid, Intent.ENTRY, day),
            mode="DRY-RUN", osi_symbol=osi, side="BUY_TO_OPEN", quantity=1, limit_price=1.0,
            status="FILLED", request={},
        )
        store.record_fill(order_id=oid, quantity=1, price=1.0, mode="DRY-RUN")

    # close s2 only
    xid = store.record_order(
        signal_id="s2", intent=Intent.EXIT, idempotency_key=make_key("s2", Intent.EXIT, day),
        mode="DRY-RUN", osi_symbol=osi, side="SELL_TO_CLOSE", quantity=1, limit_price=1.5,
        status="FILLED", request={},
    )
    store.record_fill(order_id=xid, quantity=1, price=1.5, mode="DRY-RUN")

    open_pos = store.open_positions()
    assert [r["signal_id"] for r in open_pos] == ["s1"]
    assert open_pos[0]["position_key"] == position_key("s1", osi)
    assert open_pos[0]["entry_price"] == 1.0


def test_orders_open_filters_closed_statuses(store):
    store.record_signal(_signal())
    day = date(2026, 9, 2)
    store.record_order(
        signal_id="sig-1", intent=Intent.ENTRY, idempotency_key=make_key("sig-1", Intent.ENTRY, day),
        mode="DRY-RUN", osi_symbol="X", side="BUY_TO_OPEN", quantity=1, limit_price=1.0,
        status="WORKING", request={},
    )
    store.record_order(
        signal_id="sig-1", intent=Intent.EXIT, idempotency_key=make_key("sig-1", Intent.EXIT, day),
        mode="DRY-RUN", osi_symbol="X", side="SELL_TO_CLOSE", quantity=1, limit_price=1.0,
        status="FILLED", request={},
    )
    assert [r["status"] for r in store.orders_open()] == ["WORKING"]
