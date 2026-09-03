"""HTTP surface of the local web UI (coopa_trada.web)."""

import time
from datetime import date, datetime, timezone

import pytest

pytest.importorskip("flask")

from coopa_trada.idempotency import Intent, make_key
from coopa_trada.models import AssetClass, Right, Signal
from coopa_trada.rules import load_rules
from coopa_trada.store import Store
from coopa_trada.web import create_app
from coopa_trada.web.config import WebPaths
from coopa_trada.web.runner import EngineRunner

UTC = timezone.utc

RULES_YAML = "\n".join(
    [
        "entry_minutes_before_close: 20",
        "exit_window:",
        '  SINGLE_NAME: ["15:35", 2]',
        "  ETF: [18, 1]",
        "trailing_stop_pct: 0.25",
        "hard_stop_loss_pct: 0.40",
        "take_profit_pct: 0.60",
        "max_concurrent_positions: 5",
        "max_dollars_per_position: 1500",
        "poll_interval_seconds: 30",
    ]
)


def _seed_round_trip(store, sid, underlying, osi, entry_px, exit_px):
    store.record_signal(
        Signal(
            id=sid, underlying=underlying, right=Right.CALL, strike=100.0,
            expiration=date(2026, 9, 18), quantity=1, asset_class=AssetClass.SINGLE_NAME,
            received_at=datetime(2026, 9, 2, 17, 30, tzinfo=UTC), raw="x",
        )
    )
    day = date(2026, 9, 2)
    eid = store.record_order(
        signal_id=sid, intent=Intent.ENTRY, idempotency_key=make_key(sid, Intent.ENTRY, day),
        mode="DRY-RUN", osi_symbol=osi, side="BUY_TO_OPEN", quantity=1,
        limit_price=entry_px, status="FILLED", request={},
    )
    store.record_fill(order_id=eid, quantity=1, price=entry_px, mode="DRY-RUN")
    xid = store.record_order(
        signal_id=sid, intent=Intent.EXIT, idempotency_key=make_key(sid, Intent.EXIT, day),
        mode="DRY-RUN", osi_symbol=osi, side="SELL_TO_CLOSE", quantity=1,
        limit_price=exit_px, status="FILLED", request={},
    )
    store.record_fill(order_id=xid, quantity=1, price=exit_px, mode="DRY-RUN")
    store.record_exit_event(
        position_key=f"{sid}:{osi}", order_id=xid, strategy="HardTimeExit",
        reason="window ended",
    )


@pytest.fixture
def paths(tmp_path):
    cfg = tmp_path / "rules.yaml"
    cfg.write_text(RULES_YAML, encoding="utf-8")
    return WebPaths(
        config_path=str(cfg),
        db_path=str(tmp_path / "trada.db"),
        signals_path="data/signals.example.csv",
    )


@pytest.fixture
def client(paths):
    runner = EngineRunner(paths, now=lambda: datetime(2026, 9, 2, 20, 30, tzinfo=UTC))
    return create_app(paths, runner).test_client()


# -- rules -------------------------------------------------------------
def test_get_rules_returns_current_values(client):
    body = client.get("/api/rules").get_json()
    assert body["trailing_stop_pct"] == 0.25
    assert body["exit_window"]["SINGLE_NAME"] == ["15:35", 2]
    assert body["warning"] is None
    assert body["ambiguous_keys"] == [
        "cooper_drop_from_peak_pct",
        "cooper_drop_from_entry_pct",
    ]


def test_post_rules_rejects_out_of_range(client, paths):
    good = client.get("/api/rules").get_json()
    before = open(paths.config_path).read()
    rv = client.post("/api/rules", json={**good, "trailing_stop_pct": 1.5})
    assert rv.status_code == 400
    assert "trailing_stop_pct" in rv.get_json()["error"]
    assert open(paths.config_path).read() == before  # unchanged


def test_post_rules_rejects_bad_exit_window_time(client):
    good = client.get("/api/rules").get_json()
    rv = client.post(
        "/api/rules",
        json={**good, "exit_window": {"SINGLE_NAME": ["99:99", 2], "ETF": [18, 1]}},
    )
    assert rv.status_code == 400


def test_post_rules_valid_persists_and_reloads(client, paths):
    good = client.get("/api/rules").get_json()
    rv = client.post("/api/rules", json={**good, "take_profit_pct": 0.55})
    assert rv.status_code == 200
    assert rv.get_json()["take_profit_pct"] == 0.55
    reloaded = load_rules(paths.config_path)
    assert reloaded.take_profit_pct == 0.55
    assert reloaded.trailing_stop_pct == 0.25


# -- trade log --------------------------------------------------------
def test_trades_view_reports_seeded_round_trips(client, paths):
    store = Store.open(paths.db_path)
    _seed_round_trip(store, "s1", "AMD", "AMD   260918C00100000", 2.00, 2.50)
    _seed_round_trip(store, "s2", "QQQ", "QQQ   260918C00100000", 3.00, 2.40)
    store.close()

    body = client.get("/api/trades").get_json()
    assert body["summary"]["closed"] == 2
    assert body["summary"]["open"] == 0
    by_sym = {p["underlying"]: p for p in body["positions"]}
    assert by_sym["AMD"]["realized_pnl"] == pytest.approx(50.0)
    assert by_sym["QQQ"]["realized_pnl"] == pytest.approx(-60.0)
    assert by_sym["AMD"]["exit_strategy"] == "HardTimeExit"
    assert body["summary"]["realized_pnl"] == pytest.approx(-10.0)


# -- run control ----------------------------------------------------
def test_dry_run_start_completes_and_populates_trade_log(client):
    rv = client.post("/api/run/start", json={"date": "2026-09-02"})
    assert rv.status_code == 202

    for _ in range(100):
        status = client.get("/api/run/status").get_json()
        if status["status"] == "idle":
            break
        time.sleep(0.05)
    assert status["status"] == "idle"
    assert status["last_error"] is None
    assert status["mode"] == "DRY-RUN"

    trades = client.get("/api/trades").get_json()
    assert trades["summary"]["total"] >= 1


def test_live_start_blocked_without_confirm_phrase(client):
    rv = client.post("/api/run/start", json={"live": True})
    assert rv.status_code == 409
    assert "ENABLE LIVE TRADING" in rv.get_json()["error"]


def test_live_start_blocked_without_env_optin(client, monkeypatch):
    monkeypatch.delenv("LIVE_TRADING", raising=False)
    rv = client.post(
        "/api/run/start", json={"live": True, "confirm": "ENABLE LIVE TRADING"}
    )
    assert rv.status_code == 409
    assert "LIVE_TRADING" in rv.get_json()["error"]


def test_double_start_is_rejected_while_running(client, monkeypatch):
    # Make the run hang so the second start races a live worker.
    import coopa_trada.web.runner as runner_mod

    monkeypatch.setattr(runner_mod, "build_engine", _hanging_build_engine)
    r1 = client.post("/api/run/start", json={"date": "2026-09-02"})
    assert r1.status_code == 202
    r2 = client.post("/api/run/start", json={"date": "2026-09-02"})
    assert r2.status_code == 409
    client.post("/api/run/stop", json={})


def _hanging_build_engine(**kwargs):
    class _Hang:
        def run(self, *a, **k):
            import time as _t

            _t.sleep(2)

    class _Store:
        def close(self):
            pass

    return _Hang(), _Store()


# -- reconcile ----------------------------------------------------
def test_reconcile_dry_run_is_simulated_noop(client):
    body = client.post("/api/reconcile", json={}).get_json()
    assert body["simulated"] is True
    assert "nothing to reconcile" in body["lines"][0].lower()


# -- token ------------------------------------------------------
def test_token_status_absent(client, monkeypatch):
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", "/nonexistent/schwab_token.json")
    body = client.get("/api/token").get_json()
    assert body["present"] is False
    assert "reconnect_hint" in body


# -- signals picker --------------------------------------------
def test_signals_picker_accepts_valid_csv(client, paths):
    body = client.post(
        "/api/signals", json={"path": "data/signals.example.csv"}
    ).get_json()
    assert body["signal_count"] >= 1
    assert paths.signals_path.endswith("signals.example.csv")


def test_signals_picker_rejects_missing_file(client):
    rv = client.post("/api/signals", json={"path": "/nope/does-not-exist.csv"})
    assert rv.status_code == 400
