"""Trade log: "Clear practice log" deletes practice rows only, never real ones."""
from datetime import date, datetime, timezone

import pytest

from coopa_trada.idempotency import Intent
from coopa_trada.models import AssetClass, Right, Signal
from coopa_trada.runlock import ProcessLock, database_lock_path
from coopa_trada.store import Store
from coopa_trada.web import create_app
from coopa_trada.web.config import WebPaths

DAY = date(2026, 9, 2)


def _round_trip(store, sid, mode):
    store.record_signal(Signal(sid, "AMD", Right.CALL, 100, date(2026, 9, 18), 1, AssetClass.SINGLE_NAME,
                               datetime(2026, 9, 2, 19, 40, tzinfo=timezone.utc), "x"))
    osi = "AMD   260918C00100000"
    entry = store.record_order(signal_id=sid, intent=Intent.ENTRY, idempotency_key=f"e:{mode}:{sid}", mode=mode,
                               osi_symbol=osi, side="BUY_TO_OPEN", quantity=1, limit_price=1.0, status="FILLED",
                               request={}, trading_day=DAY)
    store.record_fill(order_id=entry, quantity=1, price=1.0, mode=mode)
    store.update_peak(entry, 1.4)
    sell = store.record_order(signal_id=sid, intent=Intent.EXIT, idempotency_key=f"x:{mode}:{sid}", mode=mode,
                              osi_symbol=osi, side="SELL_TO_CLOSE", quantity=1, limit_price=1.3, status="FILLED",
                              request={}, entry_order_id=entry, trading_day=DAY)
    store.record_fill(order_id=sell, quantity=1, price=1.3, mode=mode)
    store.record_exit_event(position_key=f"{sid}:{osi}", order_id=sell, strategy="TakeProfit", reason="r")
    store.record_skip(signal_id=sid, day=DAY, mode=mode, category="budget", reason="r", detail={})
    store.record_incident(order_id=entry, outcome="PLACED", error="slow")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("COOPA_TRADA_LOCK_DIR", str(tmp_path / "locks"))
    db = tmp_path / "trada.db"
    with Store.open(db) as s:
        _round_trip(s, "p1", "DRY-RUN")
        _round_trip(s, "p2", "DRY-RUN")
        _round_trip(s, "real", "LIVE")
    paths = WebPaths(config_path="config/rules.example.yaml", db_path=str(db),
                     signals_path="data/signals.example.csv")
    return create_app(paths).test_client(), db


def _modes(client):
    return sorted(p["mode"] for p in client.get("/api/trades").get_json()["positions"])


def test_clears_practice_trades_and_keeps_real_ones(setup):
    client, db = setup
    assert _modes(client) == ["DRY-RUN", "DRY-RUN", "LIVE"]
    rv = client.post("/api/trades/clear", json={"confirm": True})
    assert rv.status_code == 200 and rv.get_json()["message"].startswith("Cleared 2 practice positions.")
    assert _modes(client) == ["LIVE"]
    with Store.open(db) as s:
        for table in ("orders", "fills", "skip_events", "order_incidents"):
            assert {r[0] for r in s.conn.execute(f"SELECT DISTINCT mode FROM {table}")} == {"LIVE"}, table
        assert s.conn.execute("SELECT COUNT(*) FROM exit_events").fetchone()[0] == 1
        assert s.conn.execute("SELECT COUNT(*) FROM position_peaks").fetchone()[0] == 1


def test_needs_confirmation(setup):
    client, _ = setup
    assert client.post("/api/trades/clear", json={}).status_code == 400
    assert _modes(client) == ["DRY-RUN", "DRY-RUN", "LIVE"]


def test_refused_while_a_run_is_going(setup):
    client, db = setup
    client.application.config["COOPA_RUNNER"].status = "running"
    assert client.post("/api/trades/clear", json={"confirm": True}).status_code == 409
    client.application.config["COOPA_RUNNER"].status = "idle"
    with ProcessLock(database_lock_path(db), purpose="database", mode="DRY-RUN"):  # e.g. a run from the CLI
        rv = client.post("/api/trades/clear", json={"confirm": True})
    assert rv.status_code == 409 and "Stop it first" in rv.get_json()["error"]
    assert _modes(client) == ["DRY-RUN", "DRY-RUN", "LIVE"]


def test_page_has_the_button(setup):
    client, _ = setup
    assert b'id="btn-clear"' in client.get("/trades").data
