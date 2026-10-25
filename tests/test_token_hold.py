"""The Schwab login must outlast the hold: entries are sold on the NEXT trading day."""
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from coopa_trada.broker import DryRunBroker
from coopa_trada.calendar_svc import CalendarService
from coopa_trada.config import Mode, SchwabCredentials
from coopa_trada.contracts import SyntheticResolver
from coopa_trada.engine import Engine
from coopa_trada.exits import Quote
from coopa_trada.models import AssetClass, Right, Signal
from coopa_trada.rules import load_rules
from coopa_trada.schwab_token import (START_MARGIN, hold_needed_until, next_run_day,
                                      read_token_status, start_block_reason)
from coopa_trada.store import Store
from coopa_trada.wiring import token_status

NY = ZoneInfo("America/New_York")
CAL = CalendarService()
RULES = load_rules("config/rules.example.yaml")


def ny(*args):
    return datetime(*args, tzinfo=NY)


def token_expiring(path, expires_at):
    """A schwab-py token file whose 7-day login lapses at ``expires_at``."""
    created = expires_at - timedelta(days=7)
    path.write_text(json.dumps({"creation_timestamp": created.timestamp(),
                                "token": {"access_token": "fake", "refresh_token": "fake"}}))
    return path


def gate(tmp_path, day, now, expires_at):
    status = read_token_status(token_expiring(tmp_path / "t.json", expires_at), now)
    return start_block_reason(status, hold_needed_until(CAL, RULES, day))


def test_hold_ends_after_the_next_trading_days_exit_window():
    # Example rules: the latest window (ETF) ends 1 minute before the close.
    assert hold_needed_until(CAL, RULES, date(2026, 10, 14)) == ny(2026, 10, 15, 15, 59) + START_MARGIN


def test_friday_entry_needs_the_login_through_monday(tmp_path):
    fri, now = date(2026, 10, 16), ny(2026, 10, 16, 15, 30)
    assert hold_needed_until(CAL, RULES, fri) == ny(2026, 10, 19, 16, 29)
    # Lasting through Friday's session (the old rule) or the weekend is not enough.
    assert "before this run finishes" in gate(tmp_path, fri, now, ny(2026, 10, 17, 10, 0))
    assert "Mon Oct 19, 2:29 PM MT" in gate(tmp_path, fri, now, ny(2026, 10, 19, 16, 0))
    assert gate(tmp_path, fri, now, ny(2026, 10, 19, 17, 0)) is None


def test_pre_holiday_entry_skips_the_holiday(tmp_path):
    # Fri Jul 3 2026 is the observed Independence Day holiday.
    thu, now = date(2026, 7, 2), ny(2026, 7, 2, 15, 30)
    assert CAL.next_session(thu) == date(2026, 7, 6)
    assert gate(tmp_path, thu, now, ny(2026, 7, 3, 12, 0)) is not None
    assert gate(tmp_path, thu, now, ny(2026, 7, 6, 16, 0)) is not None
    assert gate(tmp_path, thu, now, ny(2026, 7, 6, 17, 0)) is None


def test_entry_before_a_half_day_uses_that_days_early_close(tmp_path):
    # Thanksgiving: Wed entry, Thu closed, Fri closes at 1 PM. The exit windows
    # move with the early close (half-day fix), so the latest one (ETF) ends
    # 1 minute before 1 PM.
    wed, now = date(2026, 11, 25), ny(2026, 11, 25, 15, 30)
    assert hold_needed_until(CAL, RULES, wed) == ny(2026, 11, 27, 12, 59) + START_MARGIN
    assert gate(tmp_path, wed, now, ny(2026, 11, 27, 13, 15)) is not None
    assert gate(tmp_path, wed, now, ny(2026, 11, 27, 14, 0)) is None


def test_login_expiring_overnight_is_refused(tmp_path):
    tue, now = date(2026, 10, 13), ny(2026, 10, 13, 15, 30)
    overnight = ny(2026, 10, 13, 23, 0)
    # It would have passed the old same-day rule (close + 30 minutes).
    status = read_token_status(token_expiring(tmp_path / "t.json", overnight), now)
    assert start_block_reason(status, CAL.session_close(tue) + START_MARGIN) is None
    assert "Tue Oct 13, 9:00 PM MT" in gate(tmp_path, tue, now, overnight)


# -- engine ---------------------------------------------------------------------
def _signal(now):
    return Signal("test", "AMD", Right.CALL, 100, date(2026, 12, 18), 1, AssetClass.SINGLE_NAME,
                  now.astimezone(timezone.utc), "test")


def _engine(store, now, token_status):
    clock = [now]

    def sleep(seconds):  # time passes only while the engine waits
        clock[0] += timedelta(seconds=seconds)

    broker = DryRunBroker(store)
    broker.mode = Mode.LIVE
    return Engine(source=SimpleNamespace(fetch=lambda day: [_signal(now)]),
                  resolver=SyntheticResolver(base_price=1), broker=broker, store=store,
                  rules=RULES, cal=CAL, mode=Mode.LIVE,
                  quote_source=lambda _: Quote(1, 1.02, 1.01), now=lambda: clock[0],
                  sleep=sleep, token_status=token_status)


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "t.db") as s:
        yield s


def test_live_run_refuses_a_login_expiring_overnight(store, tmp_path):
    now = ny(2026, 10, 13, 15, 30)
    path = token_expiring(tmp_path / "t.json", ny(2026, 10, 13, 23, 0))
    eng = _engine(store, now, lambda n: read_token_status(path, n))
    eng.reconcile = lambda: pytest.fail("reconcile would contact Schwab")
    with pytest.raises(ValueError, match="sold during the next trading day's exit window"):
        eng.run(date(2026, 10, 13))
    assert store.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_login_is_checked_again_at_entry_time(store, tmp_path):
    # Started in the morning with a good login; by entry time the file is gone.
    now = ny(2026, 10, 13, 9, 0)
    good = token_expiring(tmp_path / "good.json", ny(2026, 10, 18, 12, 0))
    calls = []

    def status(n):
        calls.append(n)
        return read_token_status(good if len(calls) == 1 else tmp_path / "gone.json", n)

    eng = _engine(store, now, status)
    eng.reconcile = lambda: SimpleNamespace(divergences=[])
    with pytest.raises(ValueError, match="Schwab is not set up"):
        eng.run(date(2026, 10, 13))
    assert len(calls) == 2
    assert store.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


# -- dashboard ------------------------------------------------------------------
def _creds(path):
    return SchwabCredentials("k", "s", "https://127.0.0.1:8182", path)


def test_next_run_day_rolls_over_after_the_close():
    assert next_run_day(CAL, ny(2026, 10, 16, 15, 0)) == date(2026, 10, 16)
    assert next_run_day(CAL, ny(2026, 10, 16, 16, 30)) == date(2026, 10, 19)
    assert next_run_day(CAL, ny(2026, 10, 17, 12, 0)) == date(2026, 10, 19)


def test_dashboard_warns_when_the_login_will_not_cover_the_next_run(tmp_path):
    path = token_expiring(tmp_path / "t.json", ny(2026, 10, 18, 12, 0))  # Sunday noon
    friday = token_status(_creds(path), ny(2026, 10, 16, 12, 0), "config/rules.example.yaml")
    # More than 2 days left, so it is not "expiring soon", but it still cannot cover Monday.
    assert friday["state"] == "connected"
    assert (friday["covers_next_run"], friday["warning"]) == (False, True)
    assert "Mon Oct 19, 2:29 PM MT" in friday["hold_message"]

    path = token_expiring(tmp_path / "t.json", ny(2026, 10, 20, 16, 0))
    ok = token_status(_creds(path), ny(2026, 10, 16, 12, 0), "config/rules.example.yaml")
    assert (ok["covers_next_run"], ok["warning"], ok["hold_message"]) == (True, False, None)
    # After Friday's close the next run is Monday, sold Tuesday: no longer covered.
    later = token_status(_creds(path), ny(2026, 10, 16, 17, 0), "config/rules.example.yaml")
    assert later["next_run_day"] == "2026-10-19"
    assert later["covers_next_run"] is False
