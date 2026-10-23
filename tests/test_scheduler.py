"""Daily auto-start: on trading days, before the open, only with a login that
lasts through the next trading day's exit window. Fake clock throughout; no run
is really started (the runner is a fake) and no notification leaves the process."""
import json
from datetime import datetime, time, timedelta, timezone

import pytest
import yaml

from coopa_trada.alerts import Alerter
from coopa_trada.rules import SETTING_LABELS
from coopa_trada.scheduler import LIVE_PHRASE, AutoStartConfig, DailyScheduler
from coopa_trada.web.config import WebPaths
from coopa_trada.web.runner import RunnerBusy

UTC = timezone.utc
# Wed 2026-09-02. Mountain daylight time is UTC-6: 7:15 AM MT = 13:15 UTC; the open is 13:30 UTC.
AT_715 = datetime(2026, 9, 2, 13, 15, tzinfo=UTC)


class FakeNotifier:
    name = "fake"

    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


class FakeRunner:
    def __init__(self, status="idle", busy=False):
        self.status = status
        self.busy = busy
        self.starts = []

    def start(self, *, live=False, date_str=None):
        if self.busy:
            raise RunnerBusy("a run is already in progress")
        self.starts.append({"live": live, "date": date_str})
        self.status = "running"


def _token(path, created):
    path.write_text(json.dumps({"creation_timestamp": created.timestamp(),
                                "token": {"access_token": "f", "refresh_token": "f"}}))


def _confirmed_rules(tmp_path):
    data = yaml.safe_load(open("config/rules.example.yaml"))
    data["setting_confirmations"] = {k: {"confirmed_by_cooper": True} for k in SETTING_LABELS}
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(data))
    return str(path)


@pytest.fixture
def make(tmp_path):
    token = tmp_path / "token.json"
    _token(token, AT_715 - timedelta(days=1))  # fresh: lasts a week

    def build(env=None, *, runner=None, config_path="config/rules.example.yaml", start=AT_715):
        env = {"AUTO_START": "1", "SCHWAB_TOKEN_PATH": str(token), **(env or {})}
        notifier = FakeNotifier()
        clock = [start]
        sched = DailyScheduler(WebPaths(config_path=config_path, db_path=str(tmp_path / "t.db")),
                               runner or FakeRunner(), Alerter([notifier], now=lambda: clock[0]),
                               now=lambda: clock[0], env=env)
        sched.clock, sched.notifier, sched.token = clock, notifier, token
        return sched

    return build


def test_off_unless_auto_start_is_set(make):
    sched = make({"AUTO_START": ""})
    sched.tick()
    assert sched.runner.starts == [] and sched.notifier.sent == []
    assert "off" in sched.config.describe()


def test_starts_a_practice_run_at_715_mt_once_per_day(make):
    sched = make(start=AT_715 - timedelta(minutes=1))
    sched.tick()
    assert sched.runner.starts == []  # 7:14 AM MT: too early
    sched.clock[0] = AT_715
    sched.tick()
    assert sched.runner.starts == [{"live": False, "date": "2026-09-02"}]
    sched.runner.status = "idle"  # the run ended (or a person stopped it)
    sched.clock[0] += timedelta(hours=2)
    sched.tick()
    assert len(sched.runner.starts) == 1  # never twice in a day
    assert sched.notifier.sent == []
    assert sched.last_attempt == {"day": "2026-09-02", "ok": True, "result": "started a practice run"}


def test_start_time_is_configurable_in_mountain_time(make):
    sched = make({"AUTO_START_TIME": "06:30"}, start=datetime(2026, 9, 2, 12, 29, tzinfo=UTC))
    sched.tick()
    assert sched.runner.starts == []
    sched.clock[0] = datetime(2026, 9, 2, 12, 30, tzinfo=UTC)  # 6:30 AM MT
    sched.tick()
    assert len(sched.runner.starts) == 1


def test_winter_time_is_still_mountain_time(make):
    # Mon 2026-12-07: Mountain standard time is UTC-7, so 7:15 AM MT = 14:15 UTC.
    winter = datetime(2026, 12, 7, 14, 14, tzinfo=UTC)
    sched = make(start=winter)
    _token(sched.token, winter - timedelta(days=1))
    sched.tick()
    assert sched.runner.starts == []
    sched.clock[0] = winter + timedelta(minutes=1)
    sched.tick()
    assert len(sched.runner.starts) == 1


@pytest.mark.parametrize("when", [
    datetime(2026, 9, 5, 13, 15, tzinfo=UTC),   # Saturday
    datetime(2026, 9, 7, 13, 15, tzinfo=UTC),   # Labor Day
])
def test_no_start_and_no_alert_on_non_trading_days(make, when):
    sched = make(start=when)
    sched.tick()
    assert sched.runner.starts == [] and sched.notifier.sent == []


def test_app_started_after_the_close_does_nothing_that_day(make):
    sched = make(start=datetime(2026, 9, 2, 20, 30, tzinfo=UTC))  # 2:30 PM MT
    sched.tick()
    assert sched.runner.starts == []
    assert [a.kind for a in sched.notifier.sent] == ["daily_summary"]  # no start, no refusal


def test_app_started_after_the_open_still_starts_and_says_late(make):
    sched = make(start=datetime(2026, 9, 2, 15, 0, tzinfo=UTC))  # 9 AM MT
    sched.tick()
    assert len(sched.runner.starts) == 1 and "late" in sched.last_attempt["result"]


def test_expired_login_refuses_and_alerts_once(make):
    sched = make()
    _token(sched.token, AT_715 - timedelta(days=8))
    sched.tick()
    sched.clock[0] += timedelta(minutes=5)
    sched.tick()
    assert sched.runner.starts == []
    assert [a.title for a in sched.notifier.sent] == ["Coopa Trada did not start today"]
    assert "expired" in sched.notifier.sent[0].message.lower()
    assert sched.last_attempt["ok"] is False


def test_missing_login_refuses_and_alerts(make):
    sched = make()
    sched.token.unlink()
    sched.tick()
    assert sched.runner.starts == [] and "not set up" in sched.notifier.sent[0].message.lower()


def test_login_must_last_through_the_next_trading_days_exit_window(make):
    # Fri 2026-09-11: the next trading day is Mon 09-14, so Friday's run needs
    # Schwab until Monday afternoon. A login from Mon 09-07 morning ends Mon 09-14 morning.
    friday = datetime(2026, 9, 11, 13, 15, tzinfo=UTC)
    sched = make(start=friday)
    _token(sched.token, datetime(2026, 9, 7, 13, 0, tzinfo=UTC))
    sched.tick()
    assert sched.runner.starts == []
    assert "before this run finishes" in sched.notifier.sent[0].message
    # The same login on Wednesday covers Thursday's window: it starts.
    sched2 = make(start=datetime(2026, 9, 9, 13, 15, tzinfo=UTC))
    _token(sched2.token, datetime(2026, 9, 7, 13, 0, tzinfo=UTC))
    sched2.tick()
    assert len(sched2.runner.starts) == 1


def test_a_run_already_going_is_left_alone(make):
    sched = make(runner=FakeRunner(status="running"))
    sched.tick()
    assert sched.runner.starts == [] and sched.notifier.sent == []
    busy = make(runner=FakeRunner(busy=True))
    busy.tick()
    assert busy.notifier.sent == [] and busy.last_attempt["result"] == "already running"


# -- live ------------------------------------------------------------------------
def test_live_needs_live_trading_on(make, tmp_path):
    sched = make({"AUTO_START_LIVE": LIVE_PHRASE}, config_path=_confirmed_rules(tmp_path))
    sched.tick()
    assert sched.runner.starts == []
    assert "LIVE_TRADING is not on" in sched.notifier.sent[0].message


def test_live_needs_every_setting_confirmed(make):
    sched = make({"AUTO_START_LIVE": LIVE_PHRASE, "LIVE_TRADING": "1"})  # example rules: unconfirmed
    sched.tick()
    assert sched.runner.starts == []
    assert "unconfirmed settings" in sched.notifier.sent[0].message


def test_live_still_needs_the_login(make, tmp_path):
    sched = make({"AUTO_START_LIVE": LIVE_PHRASE, "LIVE_TRADING": "1"},
                 config_path=_confirmed_rules(tmp_path))
    _token(sched.token, AT_715 - timedelta(days=8))
    sched.tick()
    assert sched.runner.starts == [] and "expired" in sched.notifier.sent[0].message.lower()


def test_live_starts_when_every_gate_passes(make, tmp_path):
    sched = make({"AUTO_START_LIVE": LIVE_PHRASE, "LIVE_TRADING": "1"},
                 config_path=_confirmed_rules(tmp_path))
    sched.tick()
    assert sched.runner.starts == [{"live": True, "date": "2026-09-02"}]
    assert "LIVE" in sched.config.describe()


def test_live_trading_alone_does_not_make_auto_start_live(make):
    sched = make({"LIVE_TRADING": "1"})
    sched.tick()
    assert sched.runner.starts == [{"live": False, "date": "2026-09-02"}]


# -- settings --------------------------------------------------------------------
def test_config_parsing():
    assert AutoStartConfig.from_env({}) == AutoStartConfig(False, time(7, 15), False, None)
    cfg = AutoStartConfig.from_env({"AUTO_START": "yes", "AUTO_START_TIME": "06:45"})
    assert cfg.enabled and cfg.at == time(6, 45) and cfg.problem is None
    assert AutoStartConfig.from_env({"AUTO_START_TIME": "7.15am"}).problem
    assert AutoStartConfig.from_env({"AUTO_START_TIME": "25:00"}).problem
    sloppy = AutoStartConfig.from_env({"AUTO_START_LIVE": "yes"})
    assert sloppy.live is False and "exactly" in sloppy.problem


def test_bad_settings_alert_instead_of_starting(make):
    sched = make({"AUTO_START_TIME": "quarter past seven"})
    sched.tick()
    sched.tick()
    assert sched.runner.starts == []
    assert len(sched.notifier.sent) == 1 and "AUTO_START_TIME" in sched.notifier.sent[0].message


def test_unreadable_rules_refuse(make, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("nope: [")
    sched = make(config_path=str(bad))
    sched.tick()
    assert sched.runner.starts == [] and "rules file" in sched.notifier.sent[0].message


def test_alert_text_carries_no_account_details(make):
    sched = make()
    _token(sched.token, AT_715 - timedelta(days=8))
    sched.tick()
    msg = sched.notifier.sent[0].message
    assert "$" not in msg and "token.json" not in msg


def test_status_api_shows_the_schedule(make, tmp_path):
    from coopa_trada.web import create_app
    sched = make()
    app = create_app(paths=sched.paths, alerter=sched.alerter)
    app.config["COOPA_SCHEDULER"] = sched
    body = app.test_client().get("/api/run/status").get_json()
    assert body["schedule"]["auto_start"].startswith("practice run at 7:15 AM MT")
