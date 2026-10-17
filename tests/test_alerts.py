"""Alerts reach a person, scrubbed and de-duplicated. No real notifications are sent:
conftest.py replaces the osascript and ntfy transports for every test."""
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from coopa_trada import alerts
from coopa_trada.alerts import Alerter, NotifyError, from_env, scrub
from coopa_trada.broker import DryRunBroker, OrderNotPlaced, OrderOutcomeUnknown
from coopa_trada.calendar_svc import CalendarService
from coopa_trada.config import Mode
from coopa_trada.contracts import SyntheticResolver
from coopa_trada.engine import Engine
from coopa_trada.exits import Quote
from coopa_trada.idempotency import Intent
from coopa_trada.models import AssetClass, Right, Signal
from coopa_trada.rules import SETTING_LABELS, load_rules
from coopa_trada.store import Store

UTC = timezone.utc
NOW = datetime(2026, 9, 2, 19, 50, tzinfo=UTC)  # Wed 15:50 New York
DAY = date(2026, 9, 2)
GOOD_TOPIC = "coopa-" + "x" * 30


class FakeNotifier:
    name = "fake"

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, alert):
        if self.fail:
            raise NotifyError("down")
        self.sent.append(alert)


@pytest.fixture
def fake():
    return FakeNotifier()


# -- scrubbing and delivery -------------------------------------------------------
def test_scrub_removes_account_numbers_hashes_links_and_dollar_amounts():
    text = scrub("Account 12345678 balance: $1,234.56, cash 900 cap=300 ask 3.20 "
                 "https://api.schwabapi.com/trader/v1/accounts/ABCDEF0123456789AB/orders "
                 "hash 0123456789abcdef0123")
    for leaked in ("12345678", "1,234.56", "900", "300", "3.20", "schwabapi", "0123456789abcdef"):
        assert leaked not in text
    assert "AMD 100 call, expires 2026-12-18" == scrub(alerts.contract_text("AMD   261218C00100000"))


def test_alerts_are_scrubbed_and_repeated_at_most_every_few_hours(fake):
    clock = [NOW]
    alerter = Alerter([fake], now=lambda: clock[0])
    assert alerter.alert("x", "Title", "Account 87654321 has $5,000", key="k")
    assert "87654321" not in fake.sent[0].message and "5,000" not in fake.sent[0].message
    assert not alerter.alert("x", "Title", "again", key="k")
    clock[0] += timedelta(hours=7)
    assert alerter.alert("x", "Title", "again", key="k")
    alerter.clear("k")
    assert alerter.alert("x", "Title", "cleared", key="k")
    assert len(fake.sent) == 3


def test_a_broken_channel_does_not_stop_the_others(fake):
    alerter = Alerter([FakeNotifier(fail=True), fake])
    alerter.alert("x", "T", "M")
    assert len(fake.sent) == 1
    assert [c["ok"] for c in alerter.recent[0]["channels"]] == [False, True]


def test_mac_notification_passes_text_as_arguments_not_script(sent_alerts, monkeypatch):
    monkeypatch.setattr(alerts.sys, "platform", "darwin")
    alerts.MacNotifier().send(alerts.Alert("x", 'Ti"tle', 'say "hi" & quit', "k"))
    kind, args = sent_alerts[0]
    assert kind == "mac" and args[-2:] == ['Ti"tle', 'say "hi" & quit']
    assert all('"hi"' not in a for a in args[:-2])


def test_ntfy_is_optional_and_needs_an_unguessable_topic(monkeypatch):
    monkeypatch.setattr(alerts.sys, "platform", "darwin")
    names = lambda a: [n.name for n in a.notifiers]
    assert names(from_env({})) == ["Mac notification"]
    short = from_env({"NTFY_TOPIC": "cooper"})
    assert names(short) == ["Mac notification"]
    assert "24 to 64" in short.channels()[-1]["detail"]
    assert names(from_env({"NTFY_TOPIC": GOOD_TOPIC})) == ["Mac notification", "Phone (ntfy)"]
    assert "https" in from_env({"NTFY_TOPIC": GOOD_TOPIC, "NTFY_SERVER": "http://x"}).channels()[-1]["detail"]
    log_only = from_env({"NTFY_TOPIC": GOOD_TOPIC, "COOPA_TRADA_ALERTS_LOG_ONLY": "1"})
    assert names(log_only) == ["Log only"]


def test_ntfy_message_is_json_and_scrubbed(sent_alerts):
    alerter = Alerter([alerts.NtfyNotifier(GOOD_TOPIC)])
    alerter.alert("x", "Sell failed", "Account 12345678, balance $10,000.00")
    kind, url, body = sent_alerts[0]
    payload = json.loads(body)
    assert (kind, url, payload["topic"]) == ("ntfy", "https://ntfy.sh", GOOD_TOPIC)
    assert "12345678" not in body.decode() and "10,000" not in body.decode()


def test_ntfy_http_error_is_reported(monkeypatch):
    monkeypatch.setattr(alerts, "_post", lambda *a: 500)
    with pytest.raises(NotifyError, match="HTTP 500"):
        alerts.NtfyNotifier(GOOD_TOPIC).send(alerts.Alert("x", "t", "m", "k"))


# -- engine events -------------------------------------------------------------
def _signal():
    return Signal("sig1", "AMD", Right.CALL, 100, date(2026, 9, 4), 1, AssetClass.SINGLE_NAME, NOW, "test")


def _engine(store, fake, *, mode=Mode.DRY_RUN, confirmed=True, source=None, clock=None):
    rules = load_rules("config/rules.example.yaml")
    if confirmed:
        rules = replace(rules, setting_confirmations={k: {"confirmed_by_cooper": True} for k in SETTING_LABELS})
    broker = DryRunBroker(store)
    broker.mode = mode
    clock = clock or [NOW]
    return Engine(source=source or SimpleNamespace(fetch=lambda day: [_signal()]),
                  resolver=SyntheticResolver(base_price=1), broker=broker, store=store, rules=rules,
                  cal=CalendarService(), mode=mode, quote_source=lambda _: Quote(1.55, 1.59, 1.57),  # flat at the entry ask
                  now=lambda: clock[0], sleep=lambda _: None, alerter=Alerter([fake]))


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "t.db") as s:
        yield s


@pytest.mark.parametrize("exc, kind", [
    (OrderOutcomeUnknown("Cannot tell whether Schwab received BUY_TO_OPEN AMD   260904C00100000"), "order_unknown"),
    (OrderNotPlaced("not placed"), "order_not_placed"),
])
def test_unclear_or_unplaced_entry_alerts(store, fake, exc, kind):
    eng = _engine(store, fake)

    def boom(*a, **k):
        raise exc

    eng.broker.place_entry = boom
    eng.run(DAY, once=True)
    assert [a.kind for a in fake.sent] == [kind]
    assert "AMD 100 call" in fake.sent[0].message and "not resent" in fake.sent[0].message.lower()


def _open_position_with_exit(store, fake, status, *, age):
    clock = [NOW]
    eng = _engine(store, fake, clock=clock)
    eng.run(DAY, once=True)  # buys; no exit triggers yet at 15:50
    pos = store.open_positions("DRY-RUN")[0]
    store.record_order(signal_id=pos["signal_id"], intent=Intent.EXIT, idempotency_key="exit-test",
                       mode="DRY-RUN", osi_symbol=pos["osi_symbol"], side="SELL_TO_CLOSE", quantity=1,
                       limit_price=1.0, status=status, request={}, created_at=NOW,
                       entry_order_id=pos["order_id"])
    clock[0] = NOW + age
    eng._exit_tick(DAY)
    return eng


def test_failed_exit_alerts(store, fake):
    _open_position_with_exit(store, fake, "REJECTED", age=timedelta(seconds=30))
    assert [a.kind for a in fake.sent] == ["exit_failed"]
    assert "REJECTED" in fake.sent[0].message and "still open" in fake.sent[0].message


def test_exit_not_filling_alerts_after_a_few_minutes(store, fake):
    eng = _open_position_with_exit(store, fake, "WORKING", age=timedelta(minutes=1))
    assert fake.sent == []
    eng._now = lambda: NOW + timedelta(minutes=4)
    eng._exit_tick(DAY)
    assert [a.kind for a in fake.sent] == ["exit_unfilled"]


def test_refused_run_alerts(store, fake):
    with pytest.raises(ValueError, match="unconfirmed"):
        _engine(store, fake, mode=Mode.LIVE, confirmed=False).run(DAY)
    assert [a.kind for a in fake.sent] == ["run_refused"]
    assert fake.sent[0].title == "Coopa Trada: run refused"


def test_error_during_trading_alerts_once(store, fake):
    def fetch(day):
        raise RuntimeError("signal file vanished")
    with pytest.raises(RuntimeError):
        _engine(store, fake, source=SimpleNamespace(fetch=fetch)).run(DAY, once=True)
    assert [a.kind for a in fake.sent] == ["error"]
    assert fake.sent[0].message.startswith("Practice run: RuntimeError: signal file vanished")


def test_stopping_a_run_does_not_alert(store, fake):
    class Stop(alerts.Stopped):
        pass

    def fetch(day):
        raise Stop()
    with pytest.raises(Stop):
        _engine(store, fake, source=SimpleNamespace(fetch=fetch)).run(DAY, once=True)
    assert fake.sent == []


def test_runner_alerts_when_the_run_cannot_even_be_built(tmp_path, fake):
    from coopa_trada.web.config import WebPaths
    from coopa_trada.web.runner import EngineRunner
    runner = EngineRunner(WebPaths(config_path=str(tmp_path / "missing.yaml"), db_path=str(tmp_path / "t.db")),
                          alerter=Alerter([fake]))
    runner.start(date_str="2026-09-02")
    runner._thread.join(10)
    assert runner.last_error and [a.kind for a in fake.sent] == ["run_refused"]


# -- watchdog --------------------------------------------------------------------
def _token(path, created):
    path.write_text(json.dumps({"creation_timestamp": created.timestamp(),
                                "token": {"access_token": "f", "refresh_token": "f"}}))


@pytest.fixture
def watch(tmp_path, monkeypatch, fake):
    from coopa_trada.watchdog import Watchdog
    from coopa_trada.web.config import WebPaths
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(tmp_path / "token.json"))
    paths = WebPaths(config_path="config/rules.example.yaml", db_path=str(tmp_path / "w.db"))
    clock = [NOW]
    dog = Watchdog(paths, SimpleNamespace(status="idle"), Alerter([fake], now=lambda: clock[0]),
                   now=lambda: clock[0])
    dog.clock = clock
    return dog


def test_watchdog_alerts_on_a_login_expiring_within_two_days(watch, fake, tmp_path):
    watch.tick()
    assert fake.sent == []  # not set up: nothing to renew
    _token(tmp_path / "token.json", NOW - timedelta(days=5, hours=12))
    watch.tick()
    watch.tick()
    assert [a.title for a in fake.sent] == ["Schwab login expires soon"]
    assert "about 36 hours" in fake.sent[0].message
    watch.clock[0] += timedelta(hours=13)
    watch.tick()
    assert len(fake.sent) == 2


def test_watchdog_alerts_when_the_login_wont_cover_the_next_run(watch, fake, tmp_path):
    # 3 days left on a Wednesday is fine; on a Friday it does not reach Monday's selling.
    _token(tmp_path / "token.json", NOW - timedelta(days=4))
    watch.tick()
    assert fake.sent == []
    friday = datetime(2026, 9, 4, 19, 0, tzinfo=UTC)
    _token(tmp_path / "token.json", friday - timedelta(days=4))
    watch.clock[0] = friday
    watch.tick()
    assert [a.title for a in fake.sent] == ["Reconnect Schwab before the next run"]


def test_watchdog_alerts_on_crash_leftovers(watch, fake, tmp_path):
    from coopa_trada.runlock import database_lock_path
    lock = database_lock_path(tmp_path / "w.db")
    lock.write_text(json.dumps({"pid": 999999, "host": "here", "purpose": "database", "mode": "LIVE",
                                "started_at": NOW.isoformat()}))
    with Store.open(tmp_path / "w.db") as s:
        s.record_signal(replace(_signal(), id="s"))
        oid = s.record_order(signal_id="s", intent=Intent.ENTRY, idempotency_key="k", mode="LIVE",
                             osi_symbol="AMD   260904C00100000", side="BUY_TO_OPEN", quantity=1,
                             limit_price=1.0, status="FILLED", request={})
        s.record_fill(order_id=oid, quantity=1, price=1.0, mode="LIVE")
        s.record_incident(order_id=oid, outcome="UNKNOWN", error="timeout")
    watch.tick()
    kinds = sorted(a.title for a in fake.sent)
    assert kinds == ["A real-money run did not shut down cleanly", "Orders still need checking",
                     "Positions open with nothing watching them"]
    watch.tick()
    assert len(fake.sent) == 3  # de-duplicated; incidents only on the first check


# -- settings button ----------------------------------------------------------
def test_send_test_alert_button(tmp_path, monkeypatch, sent_alerts):
    monkeypatch.setattr(alerts.sys, "platform", "darwin")
    monkeypatch.setenv("NTFY_TOPIC", GOOD_TOPIC)
    from coopa_trada.web import create_app
    from coopa_trada.web.config import WebPaths
    app = create_app(WebPaths(config_path="config/rules.example.yaml", db_path=str(tmp_path / "t.db")))
    client = app.test_client()
    channels = client.get("/api/alerts").get_json()["channels"]
    assert [c["channel"] for c in channels] == ["Mac notification", "Phone (ntfy)"]
    body = client.post("/api/alerts/test", json={}).get_json()
    assert [(r["channel"], r["ok"]) for r in body["results"]] == [("Mac notification", True), ("Phone (ntfy)", True)]
    assert [s[0] for s in sent_alerts] == ["mac", "ntfy"]
    assert body["recent"][0]["title"] == "Coopa Trada test alert"
