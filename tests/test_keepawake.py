"""Keep the Mac awake while a run is active or a real position/order is open.
conftest.py replaces caffeinate with FakeCaffeinate."""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from conftest import REAL_SPAWN, FakeCaffeinate
from coopa_trada import keepawake
from coopa_trada.alerts import Alerter
from coopa_trada.idempotency import Intent
from coopa_trada.keepawake import KeepAwake
from coopa_trada.models import AssetClass, Right, Signal
from coopa_trada.store import Store

NOW = datetime(2026, 9, 2, 19, 50, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh():
    FakeCaffeinate.started.clear()


def test_holds_one_caffeinate_and_releases_it():
    awake = KeepAwake(platform="darwin")
    awake.set(True, "a run is active")
    awake.set(True, "a run is active")
    assert len(FakeCaffeinate.started) == 1 and awake.status()["active"]
    proc = FakeCaffeinate.started[0]
    awake.set(False)
    assert proc.returncode is not None
    assert awake.status() == {"active": False, "wanted": False, "reason": "", "error": None}


def test_restarts_caffeinate_if_it_dies():
    awake = KeepAwake(platform="darwin")
    awake.set(True, "x")
    FakeCaffeinate.started[0].returncode = 1
    awake.set(True, "x")
    assert len(FakeCaffeinate.started) == 2 and awake.active


def test_caffeinate_is_tied_to_this_process(monkeypatch):
    import os
    calls = []
    monkeypatch.setattr(keepawake.subprocess, "Popen", lambda args, **kw: calls.append(args))
    REAL_SPAWN(os.getpid())
    assert calls == [["/usr/bin/caffeinate", "-i", "-s", "-w", str(os.getpid())]]


def test_off_macos_it_reports_instead_of_holding():
    awake = KeepAwake(platform="win32")
    awake.set(True, "x")
    assert FakeCaffeinate.started == [] and "only built for macOS" in awake.status()["error"]


def test_on_a_linux_server_there_is_nothing_to_hold_and_no_warning():
    awake = KeepAwake(platform="linux")
    awake.set(True, "x")
    assert FakeCaffeinate.started == [] and awake.status()["error"] is None


# -- when it is wanted ---------------------------------------------------------
def _watchdog(tmp_path, runner_status="idle"):
    from coopa_trada.watchdog import Watchdog
    from coopa_trada.web.config import WebPaths
    awake = KeepAwake(platform="darwin")
    paths = WebPaths(config_path="config/rules.example.yaml", db_path=str(tmp_path / "w.db"))
    runner = SimpleNamespace(status=runner_status)
    return Watchdog(paths, runner, Alerter(), keep_awake=awake, now=lambda: NOW), runner, awake


def _position(db, mode, status="FILLED", fill=True):
    with Store.open(db) as s:
        s.record_signal(Signal("s", "AMD", Right.CALL, 100, date(2026, 9, 4), 1, AssetClass.SINGLE_NAME, NOW, "t"))
        oid = s.record_order(signal_id="s", intent=Intent.ENTRY, idempotency_key=f"{mode}:k", mode=mode,
                             osi_symbol="AMD   260904C00100000", side="BUY_TO_OPEN", quantity=1,
                             limit_price=1.0, status=status, request={})
        if fill:
            s.record_fill(order_id=oid, quantity=1, price=1.0, mode=mode)


def test_awake_while_a_run_is_active_then_released(tmp_path):
    dog, runner, awake = _watchdog(tmp_path, "running")
    dog._check_keep_awake(NOW)
    assert awake.status()["active"] and awake.reason == "a run is active"
    runner.status = "idle"
    dog._check_keep_awake(NOW)
    assert not awake.status()["active"]


def test_awake_while_a_real_position_or_order_is_open(tmp_path):
    dog, _, awake = _watchdog(tmp_path)
    _position(tmp_path / "w.db", "LIVE")
    dog._check_keep_awake(NOW)
    assert awake.active and awake.reason == "1 real position open"


def test_awake_while_a_real_order_is_working(tmp_path):
    dog, _, awake = _watchdog(tmp_path)
    _position(tmp_path / "w.db", "LIVE", status="WORKING", fill=False)
    dog._check_keep_awake(NOW)
    assert awake.active and awake.reason == "1 real order open"


def test_leftover_practice_positions_let_the_mac_sleep(tmp_path):
    dog, _, awake = _watchdog(tmp_path)
    _position(tmp_path / "w.db", "DRY-RUN")
    dog._check_keep_awake(NOW)
    assert not awake.active and FakeCaffeinate.started == []


def test_stopping_the_watchdog_releases_it(tmp_path):
    dog, _, awake = _watchdog(tmp_path, "running")
    dog.tick()
    assert awake.active
    dog.stop()
    assert not awake.active


def test_runner_pokes_the_watchdog_on_start_and_finish(tmp_path):
    from coopa_trada.web.config import WebPaths
    from coopa_trada.web.runner import EngineRunner
    runner = EngineRunner(WebPaths(config_path=str(tmp_path / "missing.yaml"), db_path=str(tmp_path / "t.db")),
                          alerter=Alerter())
    pokes = []
    runner.on_change = lambda: pokes.append(runner.status)
    runner.start(date_str="2026-09-02")
    runner._thread.join(10)
    assert pokes == ["running", "idle"]


def test_dashboard_shows_keeping_mac_awake(tmp_path):
    from coopa_trada.web import create_app
    from coopa_trada.web.config import WebPaths
    app = create_app(WebPaths(config_path="config/rules.example.yaml", db_path=str(tmp_path / "t.db")))
    client = app.test_client()
    assert client.get("/api/run/status").get_json()["keep_awake"]["active"] is False
    app.config["COOPA_KEEPAWAKE"].platform = "darwin"
    app.config["COOPA_KEEPAWAKE"].set(True, "a run is active")
    status = client.get("/api/run/status").get_json()["keep_awake"]
    assert (status["active"], status["reason"]) == (True, "a run is active")
    assert "Keeping Mac awake" in client.get("/static/dashboard.js").get_data(as_text=True)
