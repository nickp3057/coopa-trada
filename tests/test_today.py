"""The Dashboard's "Today's picks" card: is a list loaded, and each pick's status."""
from datetime import date, datetime, timedelta, timezone

import pytest

from coopa_trada.calendar_svc import CalendarService
from coopa_trada.idempotency import Intent
from coopa_trada.picks import PickListSource, imports_dir, save_confirmed_list
from coopa_trada.rules import load_rules
from coopa_trada.store import Store
from coopa_trada.today import picks_today

UTC = timezone.utc
DAY = date(2026, 9, 2)  # Wednesday; the example buying window is 3:30-3:57 PM ET
IN_WINDOW = datetime(2026, 9, 2, 19, 40, tzinfo=UTC)
LIST = ("Trade date: 2026-09-02; Expiration: 2026-12-18\n"
        "SYMBOL,OPTION,LAST PRICE,TVT-DELTA,SCHWAB DELTA\n"
        "IBIT,44P,0.71,66.4,-0.554\nTSLL,9C,0.48,73.2,0.679\nAMD,460P,12.5,70.1,-0.61\n"
        "SNOW,322.5C,18.4,79.8,0.507\n")
CAL, RULES = CalendarService(), load_rules("config/rules.example.yaml")


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "t.db") as s:
        yield s


def today(store, now=IN_WINDOW, day=DAY):
    return picks_today(store, imports_dir(store_path(store)), day, now=now, cal=CAL, rules=RULES)


def store_path(store):
    return store.conn.execute("PRAGMA database_list").fetchone()["file"]


def _ids(store):
    return {s.underlying: s for s in PickListSource(imports_dir(store_path(store))).fetch(DAY)}


def _buy(store, signal, *, mode="DRY-RUN", status="FILLED", price=1.20, day=DAY, osi=None):
    store.record_signal(signal)
    oid = store.record_order(signal_id=signal.id, intent=Intent.ENTRY, idempotency_key=f"{mode}:{signal.id}:{day}",
                             mode=mode, osi_symbol=osi or f"{signal.underlying:<6}261218P00044000",
                             side="BUY_TO_OPEN", quantity=1, limit_price=price, status=status, request={},
                             trading_day=day)
    if status == "FILLED":
        store.record_fill(order_id=oid, quantity=1, price=price, mode=mode)
    return oid


def test_nothing_loaded_yet(store):
    body = today(store)
    assert body["trading_day"] and not body["list_loaded"] and body["picks"] == []
    assert body["phase"] == "open"


def test_a_loaded_list_shows_every_pick_and_its_status(store):
    save_confirmed_list(store_path(store), LIST, "2026-09-02")
    sig = _ids(store)
    entry = _buy(store, sig["IBIT"], price=1.20)                       # bought, still open
    snow = _buy(store, sig["SNOW"], price=2.00)                        # bought and sold
    xo = store.record_order(signal_id=sig["SNOW"].id, intent=Intent.EXIT, idempotency_key="x", mode="DRY-RUN",
                            osi_symbol=f"{'SNOW':<6}261218P00044000", side="SELL_TO_CLOSE", quantity=1,
                            limit_price=2.5, status="FILLED", request={}, entry_order_id=snow, trading_day=DAY)
    store.record_fill(order_id=xo, quantity=1, price=2.5, mode="DRY-RUN")
    store.record_signal(sig["AMD"])
    store.record_skip(signal_id=sig["AMD"].id, day=DAY, mode="DRY-RUN", category="budget",
                      reason="does not fit the total budget", detail={})
    body = today(store)
    assert body["list_loaded"] and body["confirmed_at"]
    status = {p["name"]: p for p in body["picks"]}
    assert list(status) == ["IBIT 44 put", "TSLL 9 call", "AMD 460 put", "SNOW 322.5 call"]
    assert status["IBIT 44 put"]["state"] == "bought" and "$1.20" in status["IBIT 44 put"]["text"]
    assert status["SNOW 322.5 call"]["state"] == "sold" and status["SNOW 322.5 call"]["pnl"] == 50.0
    assert status["AMD 460 put"]["text"] == "Skipped: does not fit the total budget"
    assert status["TSLL 9 call"]["state"] == "waiting" and status["TSLL 9 call"]["mode"] is None
    assert entry


def test_waiting_picks_say_where_the_buying_window_is(store):
    save_confirmed_list(store_path(store), LIST, "2026-09-02")
    early = today(store, now=datetime(2026, 9, 2, 14, 0, tzinfo=UTC))
    assert early["phase"] == "before" and early["picks"][0]["text"] == "Waiting for the buying window"
    late = today(store, now=datetime(2026, 9, 2, 20, 30, tzinfo=UTC))
    assert late["picks"][0]["text"] == "Not bought (the buying window is over)"


def test_an_unfilled_or_unclear_buy_order(store):
    save_confirmed_list(store_path(store), LIST, "2026-09-02")
    sig = _ids(store)
    _buy(store, sig["IBIT"], mode="LIVE", status="WORKING")
    _buy(store, sig["TSLL"], mode="LIVE", status="UNKNOWN")
    _buy(store, sig["AMD"], mode="LIVE", status="CANCELED")
    status = {p["name"]: p for p in today(store)["picks"]}
    assert status["IBIT 44 put"]["state"] == "buying" and status["IBIT 44 put"]["mode"] == "LIVE"
    assert status["TSLL 9 call"]["state"] == "problem"
    assert status["AMD 460 put"]["text"] == "Buy order did not fill"


def test_positions_held_from_earlier_days_are_listed(store):
    yesterday = DAY - timedelta(days=1)
    save_confirmed_list(store_path(store), LIST.replace("2026-09-02", "2026-09-01"), "2026-09-01")
    old = {s.underlying: s for s in PickListSource(imports_dir(store_path(store))).fetch(yesterday)}
    _buy(store, old["IBIT"], day=yesterday, price=0.70)
    body = today(store)
    assert not body["list_loaded"]                                   # yesterday's list is not today's
    assert body["held"] == [{"name": "IBIT 44 put", "mode": "DRY-RUN", "quantity": 1,
                             "entry_price": 0.70, "bought_on": "2026-09-01"}]


def test_a_weekend_is_not_a_trading_day(store):
    body = today(store, day=date(2026, 9, 5), now=datetime(2026, 9, 5, 16, 0, tzinfo=UTC))
    assert body["trading_day"] is False and body["phase"] is None


def test_the_dashboard_endpoint(tmp_path):
    from coopa_trada.web import create_app
    from coopa_trada.web.config import WebPaths

    paths = WebPaths(config_path="config/rules.example.yaml", db_path=str(tmp_path / "trada.db"),
                     signals_path="data/signals.example.csv")
    client = create_app(paths).test_client()
    body = client.get("/api/today").get_json()
    assert body["list_loaded"] is False and body["run_active"] is False and "picks" in body
    assert b'id="today-card"' in client.get("/").data
