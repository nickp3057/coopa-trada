"""End-of-day summary: one alert after the exit windows close, counts only,
no account numbers or dollar amounts. Fake clock; nothing leaves the process."""
import json
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

import pytest

from coopa_trada.alerts import Alerter
from coopa_trada.idempotency import Intent
from coopa_trada.models import AssetClass, Right, Signal
from coopa_trada.picks import save_confirmed_list
from coopa_trada.scheduler import DailyScheduler
from coopa_trada.store import Store
from coopa_trada.summary import collect
from coopa_trada.web.config import WebPaths

UTC = timezone.utc
DAY = date(2026, 9, 2)  # Wednesday
# The example rules' latest exit window ends 1 minute before the 4 PM ET close:
# 15:59 ET = 19:59 UTC = 1:59 PM MT.
WINDOW_END = datetime(2026, 9, 2, 19, 59, tzinfo=UTC)
CLOSE = datetime(2026, 9, 2, 20, 0, tzinfo=UTC)
LIST = ("Trade date: 2026-09-02; Expiration: 2026-12-18\n"
        "SYMBOL,OPTION,LAST PRICE,TVT-DELTA,SCHWAB DELTA\n"
        "IBIT,44P,0.71,66.4,-0.554\nTSLL,9C,0.48,73.2,0.679\nAMD,460P,12.5,70.1,-0.61\n"
        "SNOW,322.5C,18.4,79.8,0.507\n")


class FakeNotifier:
    name = "fake"

    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


class FakeRunner:
    def __init__(self, status="idle", last_error=None, last_started=None):
        self.status = status
        self.last_error = last_error
        self.last_started = last_started

    def start(self, **kw):  # auto-start is off in these tests
        raise AssertionError("not expected")

    def snapshot(self):
        return {"status": self.status, "last_error": self.last_error, "last_started": self.last_started}


def _signal(store, sid):
    store.record_signal(Signal(sid, sid.split("-")[0], Right.CALL, 100, date(2026, 12, 18), 1,
                               AssetClass.SINGLE_NAME, WINDOW_END, "t"))


def _entry(store, sid, mode="LIVE", *, day=DAY, status="FILLED", fill=True):
    _signal(store, sid)
    oid = store.record_order(signal_id=sid, intent=Intent.ENTRY, idempotency_key=f"{mode}:{sid}:{day}",
                             mode=mode, osi_symbol=f"{sid[:4]:<6}261218C00100000", side="BUY_TO_OPEN",
                             quantity=1, limit_price=1.23, status=status, request={}, trading_day=day)
    if fill:
        store.record_fill(order_id=oid, quantity=1, price=1.23, mode=mode)
    return oid


def _sell(store, sid, entry_id, mode="LIVE"):
    oid = store.record_order(signal_id=sid, intent=Intent.EXIT, idempotency_key=f"x:{mode}:{sid}", mode=mode,
                             osi_symbol=f"{sid[:4]:<6}261218C00100000", side="SELL_TO_CLOSE", quantity=1,
                             limit_price=1.5, status="FILLED", request={}, entry_order_id=entry_id,
                             trading_day=DAY)
    store.record_fill(order_id=oid, quantity=1, price=1.5, mode=mode)


def _skip(store, sid, mode="LIVE", category="budget"):
    _signal(store, sid)
    store.record_skip(signal_id=sid, day=DAY, mode=mode, category=category,
                      reason="does not fit the total budget: costs $1,250.00", detail={})


@pytest.fixture
def setup(tmp_path):
    db = tmp_path / "t.db"
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"creation_timestamp": (WINDOW_END - timedelta(days=1)).timestamp(),
                                 "token": {"access_token": "f", "refresh_token": "f"}}))

    def build(runner=None, env=None, start=WINDOW_END, config_path="config/rules.example.yaml"):
        notifier = FakeNotifier()
        clock = [start]
        sched = DailyScheduler(WebPaths(config_path=str(config_path), db_path=str(db)),
                               runner or FakeRunner(), Alerter([notifier], now=lambda: clock[0]),
                               now=lambda: clock[0],
                               env={"SCHWAB_TOKEN_PATH": str(token), **(env or {})})
        sched.clock, sched.notifier = clock, notifier
        return sched

    build.db = db
    return build


def _bought_two_skipped_one(db):
    with Store.open(db) as s:
        a = _entry(s, "IBIT-44P-20261218")
        _entry(s, "TSLL-9C-20261218")
        _sell(s, "IBIT-44P-20261218", a)  # take-profit the same day; TSLL is held overnight
        _skip(s, "AMD-460P-20261218")


def test_one_summary_after_the_exit_window_with_counts_only(setup, tmp_path):
    save_confirmed_list(setup.db, LIST, "2026-09-02")
    _bought_two_skipped_one(setup.db)
    rules = tmp_path / "rules.yaml"  # automatic daily start switched off in Settings
    rules.write_text(Path("config/rules.example.yaml").read_text()
                     .replace("auto_start_daily: true", "auto_start_daily: false"))
    sched = setup(start=WINDOW_END - timedelta(minutes=1), config_path=rules)
    sched.tick()
    assert sched.notifier.sent == []  # window still open
    sched.clock[0] = WINDOW_END
    sched.tick()
    sched.tick()
    assert len(sched.notifier.sent) == 1
    alert = sched.notifier.sent[0]
    assert alert.title == "Coopa Trada: Wed Sep 2 summary"
    assert alert.message.splitlines()[0] == (
        "Real trading: 4 picks received, 2 bought, 1 skipped. 1 position held overnight.")
    # Not auto-started, so the held position needs tomorrow's run started by a person.
    assert "start tomorrow's run before the open" in alert.message
    for leak in ("$", "1.23", "1,250", "token", "Practice"):
        assert leak not in alert.message


def test_not_sent_twice_even_after_a_restart(setup):
    setup().tick()
    again = setup()  # a new process the same afternoon
    again.tick()
    assert again.notifier.sent == []


def test_waits_for_a_run_still_selling_but_not_forever(setup):
    runner = FakeRunner(status="running")
    sched = setup(runner=runner)
    sched.tick()
    assert sched.notifier.sent == []
    sched.clock[0] = CLOSE + timedelta(minutes=30)
    sched.tick()
    assert len(sched.notifier.sent) == 1


def test_quiet_day(setup):
    sched = setup()
    sched.tick()
    assert sched.notifier.sent[0].message == ("0 picks received. Nothing bought, nothing held.\n"
                                              "Open issues: none.")


def test_practice_activity_is_labelled(setup):
    with Store.open(setup.db) as s:
        _entry(s, "QQQ-707C-20261218", mode="DRY-RUN")
    sched = setup()
    sched.tick()
    first = sched.notifier.sent[0].message.splitlines()[0]
    assert first == "Practice: 1 pick received, 1 bought, 0 skipped. 1 position held overnight."
    assert "start tomorrow's run" not in sched.notifier.sent[0].message  # only for real positions


def test_open_issues_are_listed(setup):
    with Store.open(setup.db) as s:
        oid = _entry(s, "PLTR-180C-20261218", status="UNKNOWN", fill=False)
        s.record_incident(order_id=oid, outcome="UNKNOWN", error="read timeout")
        _entry(s, "GDX-95C-20261218", status="WORKING", fill=False)
        _entry(s, "DDOG-222.5C-20261218", status="CANCELED", fill=False)
    runner = FakeRunner(last_error="RuntimeError: boom", last_started="2026-09-02T19:40:00+00:00")
    sched = setup(runner=runner)
    sched.tick()
    msg = sched.notifier.sent[0].message
    assert "3 picks received, 0 bought, 0 skipped, 1 not filled" in msg
    assert "1 order to check on the Reconcile page" in msg
    assert "2 orders still working" in msg  # the unknown one and the working one
    assert "Today's run stopped with an error" in msg
    assert "boom" not in msg


def test_login_that_wont_cover_the_next_run_is_an_issue(setup, tmp_path):
    (tmp_path / "token.json").write_text(json.dumps({
        "creation_timestamp": (WINDOW_END - timedelta(days=6, hours=20)).timestamp(),
        "token": {"access_token": "f", "refresh_token": "f"}}))
    sched = setup()
    sched.tick()
    assert "Reconnect Schwab before the next run" in sched.notifier.sent[0].message


def test_skips_and_weekends(setup):
    sched = setup(start=datetime(2026, 9, 5, 21, 0, tzinfo=UTC))  # Saturday
    sched.tick()
    assert sched.notifier.sent == []


def test_half_day_summary_comes_after_the_early_close_window(setup):
    # Fri 2026-11-27 closes at 1 PM ET: the ETF window ends 12:59 ET = 17:59 UTC.
    sched = setup(start=datetime(2026, 11, 27, 17, 58, tzinfo=UTC))
    sched.tick()
    assert sched.notifier.sent == []
    sched.clock[0] = datetime(2026, 11, 27, 17, 59, tzinfo=UTC)
    sched.tick()
    assert len(sched.notifier.sent) == 1


def test_can_be_switched_off(setup):
    sched = setup(env={"DAILY_SUMMARY": "0"})
    sched.tick()
    assert sched.notifier.sent == []


def test_counts_ignore_other_days(setup):
    with Store.open(setup.db) as s:
        _entry(s, "IBIT-44P-20261218", day=date(2026, 9, 1))  # bought yesterday, still held
    summary = collect(Store.open(setup.db), DAY, picks_folder=setup.db.parent / "imports")
    live = summary.counts[0]
    assert (live.received, live.bought, live.held) == (0, 0, 1)
