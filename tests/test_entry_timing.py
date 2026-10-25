"""Task 5: buy when picks are confirmed (inside a buying window), half-day windows,
and Mountain-time display."""
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from coopa_trada import clock as clk
from coopa_trada.broker import DryRunBroker
from coopa_trada.calendar_svc import CalendarService
from coopa_trada.contracts import SyntheticResolver
from coopa_trada.engine import Engine
from coopa_trada.exits import Quote
from coopa_trada.models import AssetClass, Right, Signal
from coopa_trada.picks import PickListSource, latest_list, save_confirmed_list
from coopa_trada.rules import load_rules
from coopa_trada.store import Store

NY = ZoneInfo("America/New_York")
CAL = CalendarService()
RULES = load_rules("config/rules.example.yaml")
DAY, HALF = date(2026, 9, 2), date(2026, 11, 27)
LIST = "Trade date: {d}; Expiration: 2026-12-18\nSYMBOL,OPTION,LAST PRICE,TVT-DELTA,SCHWAB DELTA\n{rows}"


def ny(day, h, m=0):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=NY)


def hm(dt):
    dt = dt.astimezone(NY)
    return dt.hour, dt.minute


# -- calendar --------------------------------------------------------------------
def test_buy_window_normal_day_is_130_to_157_mountain():
    earliest, cutoff = CAL.buy_window(DAY, RULES)
    assert (hm(earliest), hm(cutoff)) == ((15, 30), (15, 57))
    assert clk.fmt_display(earliest, with_day=False) == "1:30 PM MT"
    assert clk.fmt_display(cutoff, with_day=False) == "1:57 PM MT"


def test_buy_window_on_a_half_day_follows_the_early_close():
    earliest, cutoff = CAL.buy_window(HALF, RULES)  # closes 1:00 PM ET = 11:00 AM MT
    assert (hm(earliest), hm(cutoff)) == ((12, 30), (12, 57))


@pytest.mark.parametrize("day", [HALF, date(2026, 12, 24)])
def test_half_day_exit_windows_resolve_inside_the_session(day):
    # The old bug: the 15:35 single-name start fell after a 1 PM close and raised.
    start, end = CAL.exit_window(day, AssetClass.SINGLE_NAME, RULES)
    assert (hm(start), hm(end)) == ((12, 35), (12, 58))
    e_start, e_end = CAL.exit_window(day, AssetClass.ETF, RULES)
    assert (hm(e_start), hm(e_end)) == ((12, 42), (12, 59))
    # Regular days are unchanged.
    assert hm(CAL.exit_window(DAY, AssetClass.SINGLE_NAME, RULES)[0]) == (15, 35)


def test_wall_clock_times_never_fall_before_the_open_on_a_half_day():
    assert CAL.resolve_time(HALF, time(10, 0)) == CAL.session_open(HALF)  # 10:00 - 3 h -> clamped to 9:30


# -- engine ---------------------------------------------------------------------
@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "t.db") as s:
        yield s


def sig(day, symbol="AMD"):
    return Signal(f"{symbol}-100C", symbol, Right.CALL, 100, date(2026, 12, 18), 1, AssetClass.SINGLE_NAME,
                  ny(day, 9), "x", signal_delta_schwab=0.5)


def engine(store, day, start, *, confirmed_at, **rules):
    """Picks become visible ("confirmed") at ``confirmed_at``; the clock moves with sleep."""
    clock = [start]

    def fetch(d):
        return [sig(day)] if clock[0] >= confirmed_at else []

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)
        assert clock[0] < ny(day, 17), "run did not end"

    eng = Engine(source=SimpleNamespace(fetch=fetch), resolver=SyntheticResolver(base_price=.5),
                 broker=DryRunBroker(store), store=store, rules=replace(RULES, **rules), cal=CAL,
                 mode=DryRunBroker.mode, quote_source=lambda _: Quote(.5, .52, .51),
                 now=lambda: clock[0], sleep=sleep)
    placed = []
    real = eng.broker.place_entry
    eng.broker.place_entry = lambda *a, **k: placed.append(clock[0]) or real(*a, **k)
    return eng, clock, placed


def test_buys_as_soon_as_picks_are_confirmed(store):
    eng, clock, placed = engine(store, DAY, ny(DAY, 9), confirmed_at=ny(DAY, 15, 47))  # 1:47 PM MT
    eng.run(DAY)
    assert placed == [ny(DAY, 15, 47)]


def test_picks_confirmed_early_wait_for_the_buying_window(store):
    eng, clock, placed = engine(store, DAY, ny(DAY, 9), confirmed_at=ny(DAY, 10))
    eng.run(DAY)
    assert placed == [ny(DAY, 15, 30)]  # not at 10 AM


def test_picks_confirmed_at_or_after_the_cutoff_are_skipped_and_logged(store):
    eng, clock, placed = engine(store, DAY, ny(DAY, 15), confirmed_at=ny(DAY, 15, 57))
    eng.run(DAY)
    assert placed == []
    row = store.conn.execute("SELECT category, reason FROM skip_events").fetchone()
    assert row["category"] == "cutoff" and "1:57 PM MT" in row["reason"]


def test_half_day_buys_relative_to_the_early_close(store):
    eng, clock, placed = engine(store, HALF, ny(HALF, 9), confirmed_at=ny(HALF, 12, 55))
    eng.run(HALF)
    assert placed == [ny(HALF, 12, 55)]
    with Store.open(":memory:") as other:
        eng, clock, placed = engine(other, HALF, ny(HALF, 9), confirmed_at=ny(HALF, 12, 57))
        eng.run(HALF)
        assert placed == []


def test_cutoff_setting_moves_the_window(store):
    eng, clock, placed = engine(store, DAY, ny(DAY, 15, 50), confirmed_at=ny(DAY, 15, 55),
                                no_new_buys_minutes_before_close=10)
    eng.run(DAY)
    assert placed == []  # 3:55 ET is after a 3:50 cutoff


def test_status_line_shows_mountain_times(store):
    eng, clock, placed = engine(store, DAY, ny(DAY, 9), confirmed_at=ny(DAY, 23))
    notes = []
    real_sleep = eng._sleep
    eng._sleep = lambda s: notes.append(eng.status_note) or real_sleep(s)
    eng.run(DAY)
    assert "waiting for today's picks: buys from 1:30 PM MT until 1:57 PM MT" in notes[0]


def test_past_dates_replay_without_the_window(store):
    eng, clock, placed = engine(store, DAY, ny(DAY, 20), confirmed_at=ny(DAY, 0))  # after the close
    eng.run(DAY)
    assert len(placed) == 1


def test_confirming_during_a_run_triggers_the_buy(store, tmp_path):
    folder = tmp_path / "imports"
    clock = [ny(DAY, 15, 35)]
    eng = Engine(source=PickListSource(folder), resolver=SyntheticResolver(base_price=.5),
                 broker=DryRunBroker(store), store=store, rules=RULES, cal=CAL, mode=DryRunBroker.mode,
                 quote_source=lambda _: Quote(.5, .52, .51), now=lambda: clock[0], sleep=lambda s: tick(s))
    written = []

    def tick(seconds):
        clock[0] += timedelta(seconds=seconds)
        if clock[0] >= ny(DAY, 15, 45) and not written:  # Cooper confirms at 1:45 PM MT
            written.append(save_confirmed_list(tmp_path / "t.db", LIST.format(
                d=DAY, rows="IBIT,44P,0.71,66.4,-0.554\n"), str(DAY)))
    eng.run(DAY)
    buys = store.conn.execute("SELECT osi_symbol, trading_day FROM orders WHERE intent='ENTRY'").fetchall()
    assert [(b["osi_symbol"][:4], b["trading_day"]) for b in buys] == [("IBIT", str(DAY))]


# -- confirmed lists --------------------------------------------------------------
def test_latest_confirmed_list_replaces_earlier_ones_and_days_are_separate(tmp_path):
    db = tmp_path / "t.db"
    first = save_confirmed_list(db, LIST.format(d=DAY, rows="IBIT,44P,0.71,66.4,-0.554\n"), str(DAY))
    second = save_confirmed_list(db, LIST.format(d=DAY, rows="SNOW,322.5C,18.4,79.8,0.507\n"), str(DAY))
    other = save_confirmed_list(db, LIST.format(d=date(2026, 9, 3), rows="IBIT,45P,0.71,66.4,-0.554\n"),
                                "2026-09-03")
    assert first != second and latest_list(tmp_path / "imports", DAY) == second
    source = PickListSource(tmp_path / "imports")
    assert [s.underlying for s in source.fetch(DAY)] == ["SNOW"]
    assert [s.strike for s in source.fetch(date(2026, 9, 3))] == [45]
    assert source.fetch(date(2026, 9, 4)) == []
    assert not list((tmp_path / "imports").glob("*.part")) and other.exists()


def test_without_a_confirmed_list_the_fallback_is_used(tmp_path):
    fallback = SimpleNamespace(fetch=lambda day: ["sample"])
    assert PickListSource(tmp_path / "none", fallback).fetch(DAY) == ["sample"]


@pytest.mark.parametrize("line, key", [
    ("no_new_buys_minutes_before_close: 40", "earliest_buy_minutes_before_close"),
    ("no_new_buys_minutes_before_close: -1", "no_new_buys_minutes_before_close"),
])
def test_buy_window_settings_validate(tmp_path, line, key):
    base = open("config/rules.example.yaml").read()
    name = line.split(":")[0]
    p = tmp_path / "r.yaml"
    p.write_text("\n".join(l for l in base.splitlines() if not l.startswith(name + ":")) + "\n" + line)
    with pytest.raises(ValueError, match=key):
        load_rules(p)
    old = tmp_path / "old.yaml"
    old.write_text(base + "\nentry_minutes_before_close: 20\n")  # legacy key is ignored
    assert load_rules(old).no_new_buys_minutes_before_close == 3
