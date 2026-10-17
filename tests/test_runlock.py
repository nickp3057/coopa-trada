"""One copy per database / Schwab account, crash detection, and clearing."""
import json
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone

import pytest

from coopa_trada import runlock
from coopa_trada.__main__ import main as cli_main
from coopa_trada.broker import SchwabBroker
from coopa_trada.runlock import (ProcessLock, RunLocked, StaleLock, account_lock_path, clear_stale_lock,
                                 database_lock_path, inspect_lock)
from coopa_trada.store import Store
from test_trading_regressions import DAY, Client, engine


def _lock(path, mode="LIVE"):
    return ProcessLock(path, purpose="database", mode=mode)


def _in_child(path, body):
    """Run ``body`` in another Python process that has taken the lock."""
    script = textwrap.dedent(f"""
        import os, sys, time
        from coopa_trada.runlock import ProcessLock
        lock = ProcessLock({str(path)!r}, purpose="database", mode=sys.argv[1]).acquire()
        print("locked", flush=True)
    """) + textwrap.dedent(body)
    return script


def _spawn(path, body, mode="LIVE"):
    child = subprocess.Popen([sys.executable, "-c", _in_child(path, body), mode],
                             stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "locked"
    return child


def test_second_holder_in_same_process_is_refused(tmp_path):
    path = tmp_path / "db.lock"
    with _lock(path):
        with pytest.raises(RunLocked, match="already running"):
            _lock(path).acquire()
    with _lock(path):  # free again after a clean release
        pass
    assert inspect_lock(path)["state"] == "free"


def test_inspecting_a_held_lock_does_not_release_it(tmp_path):
    path = tmp_path / "db.lock"
    with _lock(path):
        for _ in range(3):
            assert inspect_lock(path)["state"] == "held"
        with pytest.raises(RunLocked):
            _lock(path).acquire()


def test_other_process_is_refused_while_running(tmp_path):
    path = tmp_path / "db.lock"
    child = _spawn(path, "time.sleep(30)")
    try:
        with pytest.raises(RunLocked, match=f"process {child.pid}"):
            _lock(path).acquire()
        assert inspect_lock(path)["state"] == "held"
        with pytest.raises(RunLocked, match="running copy holds"):
            clear_stale_lock(path)
    finally:
        child.kill()
        child.wait()


def test_crashed_live_run_leaves_a_stale_lock_that_must_be_cleared(tmp_path):
    path = tmp_path / "db.lock"
    child = _spawn(path, "os._exit(1)")  # crash: no clean release
    child.wait()
    state = inspect_lock(path)
    assert state["state"] == "stale"
    assert state["holder"]["pid"] == child.pid
    with pytest.raises(StaleLock, match="did not shut down cleanly"):
        _lock(path).acquire()
    with pytest.raises(StaleLock):  # still blocked: refusing does not clear it
        _lock(path).acquire()
    assert clear_stale_lock(path)["cleared"] is True
    with _lock(path):
        pass


def test_crashed_simulation_is_cleared_automatically(tmp_path):
    path = tmp_path / "db.lock"
    _spawn(path, "os._exit(1)", mode="DRY-RUN").wait()
    lock = _lock(path, mode="DRY-RUN").acquire()
    try:
        assert lock.cleared_stale["mode"] == "DRY-RUN"
    finally:
        lock.release()


def test_unreadable_record_counts_as_stale_live(tmp_path):
    path = tmp_path / "db.lock"
    path.write_bytes(b" {garbage")
    with pytest.raises(StaleLock):
        _lock(path, mode="DRY-RUN").acquire()


def test_lock_from_another_computer_needs_force(tmp_path):
    path = tmp_path / "db.lock"
    record = {"pid": 1, "host": "someone-elses-mac", "purpose": "database", "mode": "LIVE",
              "started_at": datetime.now(timezone.utc).isoformat()}
    path.write_bytes(b" " + json.dumps(record).encode())
    with pytest.raises(RunLocked, match="another computer"):
        clear_stale_lock(path)
    assert inspect_lock(path)["state"] == "stale"
    assert clear_stale_lock(path, force_other_host=True)["cleared"] is True


# -- engine: CLI and web both go through Engine.run --------------------------------
@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "test.db") as s:
        yield s


def _orders(store):
    return store.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]


def test_engine_refuses_when_database_is_in_use(store, tmp_path):
    eng = engine(store, SchwabBroker(Client(), "account", store, None))
    with _lock(database_lock_path(tmp_path / "test.db")):
        with pytest.raises(RunLocked, match="this database"):
            eng.run(DAY)
    assert _orders(store) == 0


def test_engine_refuses_second_database_on_the_same_account(tmp_path):
    with Store.open(tmp_path / "other.db") as other:
        eng = engine(other, SchwabBroker(Client(), "account", other, None))
        with ProcessLock(account_lock_path("account"), purpose="Schwab account", mode="LIVE"):
            with pytest.raises(RunLocked, match="this Schwab account"):
                eng.run(DAY)
        assert _orders(other) == 0


def test_engine_releases_locks_when_a_run_fails(store, tmp_path):
    eng = engine(store, SchwabBroker(Client(), "account", store, None))
    with pytest.raises(ValueError, match="today"):
        eng.run(datetime(2026, 9, 1).date())
    assert inspect_lock(database_lock_path(tmp_path / "test.db"))["state"] == "free"
    assert inspect_lock(account_lock_path("account"))["state"] == "free"


def test_cli_reports_lock_in_plain_words_and_unlock_clears(tmp_path, capsys):
    db = tmp_path / "cli.db"
    path = database_lock_path(db)
    with _lock(path):
        rc = cli_main(["run", "--once", "--date", "2026-09-02", "--db", str(db),
                       "--config", "config/rules.example.yaml"])
    assert rc == 2
    assert "already running" in capsys.readouterr().err

    _spawn(path, "os._exit(1)").wait()
    assert cli_main(["unlock", "--db", str(db)]) == 0
    assert "Cleared the stale lock" in capsys.readouterr().out
    assert inspect_lock(path)["state"] == "free"


def test_web_lists_and_clears_only_known_stale_locks(tmp_path):
    pytest.importorskip("flask")
    from coopa_trada.web import create_app
    from coopa_trada.web.config import WebPaths
    from coopa_trada.web.runner import EngineRunner
    paths = WebPaths(config_path="config/rules.example.yaml", db_path=str(tmp_path / "web.db"),
                     signals_path="data/signals.example.csv")
    client = create_app(paths, EngineRunner(paths)).test_client()
    assert client.get("/api/lock").get_json()["stale"] == []
    path = database_lock_path(paths.db_path)
    _spawn(path, "os._exit(1)").wait()
    stale = client.get("/api/lock").get_json()["stale"]
    assert [lock["path"] for lock in stale] == [str(path)]
    assert client.post("/api/lock/clear", json={"path": str(tmp_path / "x.lock")}).status_code == 400
    assert client.post("/api/lock/clear", json={"path": str(path)}).get_json()["cleared"] is True
    assert client.get("/api/lock").get_json()["locks"] == []


def test_web_run_surfaces_lock_error(tmp_path):
    pytest.importorskip("flask")
    from coopa_trada.web import create_app
    from coopa_trada.web.config import WebPaths
    from coopa_trada.web.runner import EngineRunner
    paths = WebPaths(config_path="config/rules.example.yaml", db_path=str(tmp_path / "web.db"),
                     signals_path="data/signals.example.csv")
    runner = EngineRunner(paths, now=lambda: datetime(2026, 9, 2, 20, 30, tzinfo=timezone.utc))
    client = create_app(paths, runner).test_client()
    with _lock(database_lock_path(paths.db_path)):
        assert client.post("/api/run/start", json={"date": "2026-09-02"}).status_code == 202
        for _ in range(100):
            status = client.get("/api/run/status").get_json()
            if status["status"] == "idle":
                break
            time.sleep(0.05)
    assert "already running" in status["last_error"]
    store = Store.open(paths.db_path)
    try:
        assert _orders(store) == 0
    finally:
        store.close()
