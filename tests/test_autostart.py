"""The day's run starts by itself on trading days."""
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from coopa_trada.autostart import AutoStarter

MT = ZoneInfo("America/Denver")


def mt(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=MT)


class Runner:
    def __init__(self):
        self.status = "idle"
        self.last_started = None
        self.starts = []

    def start(self, *, live=False, date_str=None):
        self.starts.append((live, date_str))
        self.status = "running"
        self.last_started = datetime.now(timezone.utc).isoformat()


@pytest.fixture
def paths(tmp_path):
    cfg = tmp_path / "rules.yaml"
    cfg.write_text(open("config/rules.example.yaml").read())
    return SimpleNamespace(config_path=str(cfg))


def test_starts_once_at_seven_mountain_on_a_trading_day(paths):
    runner = Runner()
    auto = AutoStarter(paths, runner, env={})
    assert not auto.check(mt(2026, 9, 2, 6, 59))
    assert auto.check(mt(2026, 9, 2, 7, 0))
    assert runner.starts == [(False, "2026-09-02")]           # practice by default
    runner.status = "idle"                                     # run ended (or crashed) later
    assert not auto.check(mt(2026, 9, 2, 12, 0))               # not again the same day
    assert auto.check(mt(2026, 9, 3, 7, 5))                    # next trading day


def test_no_start_on_weekends_holidays_or_after_the_close(paths):
    runner = Runner()
    auto = AutoStarter(paths, runner, env={})
    assert not auto.check(mt(2026, 9, 5, 8))       # Saturday
    assert not auto.check(mt(2026, 9, 7, 8))       # Labor Day
    assert not auto.check(mt(2026, 9, 4, 14, 5))   # after the 2:00 PM MT close
    assert runner.starts == []


def test_a_run_started_or_stopped_by_hand_today_is_left_alone(paths):
    runner = Runner()
    runner.last_started = datetime(2026, 9, 2, 13, 0, tzinfo=timezone.utc).isoformat()  # 7 AM MT today
    assert not AutoStarter(paths, runner, env={}).check(mt(2026, 9, 2, 9))
    runner.status = "running"
    runner.last_started = None
    assert not AutoStarter(paths, runner, env={}).check(mt(2026, 9, 2, 9))
    assert runner.starts == []


def test_app_restarted_mid_day_starts_the_run_again(paths):
    runner = Runner()
    assert AutoStarter(paths, runner, env={}).check(mt(2026, 9, 2, 11, 30))


@pytest.mark.parametrize("env, live", [
    ({"LIVE_TRADING": "1", "AUTO_START_LIVE": "1"}, True),
    ({"LIVE_TRADING": "1"}, False),
    ({"AUTO_START_LIVE": "1"}, False),
])
def test_live_needs_both_opt_ins(paths, env, live):
    runner = Runner()
    AutoStarter(paths, runner, env=env).check(mt(2026, 9, 2, 8))
    assert runner.starts == [(live, "2026-09-02")]


def test_setting_off_or_another_time(paths):
    text = open(paths.config_path).read()
    open(paths.config_path, "w").write(text.replace('auto_start_time: "07:00"', 'auto_start_time: "13:15"'))
    runner = Runner()
    auto = AutoStarter(paths, runner, env={})
    assert not auto.check(mt(2026, 9, 2, 13, 14)) and auto.check(mt(2026, 9, 2, 13, 15))
    open(paths.config_path, "w").write(text.replace("auto_start_daily: true", "auto_start_daily: false"))
    runner = Runner()
    assert not AutoStarter(paths, runner, env={}).check(mt(2026, 9, 3, 8)) and runner.starts == []


def test_bad_time_is_rejected(tmp_path):
    from coopa_trada.rules import load_rules
    p = tmp_path / "r.yaml"
    p.write_text(open("config/rules.example.yaml").read().replace('"07:00"', '"7 am"'))
    with pytest.raises(ValueError, match="auto_start_time"):
        load_rules(p)


def test_watchdog_runs_the_auto_start(tmp_path, paths):
    from coopa_trada.alerts import Alerter
    from coopa_trada.watchdog import Watchdog
    calls = []
    auto = SimpleNamespace(check=lambda now: calls.append(now))
    watch = Watchdog(SimpleNamespace(config_path=paths.config_path, db_path=str(tmp_path / "t.db")),
                     Runner(), Alerter([]), now=lambda: mt(2026, 9, 2, 7), auto_start=auto)
    watch.tick()
    assert calls == [mt(2026, 9, 2, 7)]
