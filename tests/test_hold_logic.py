"""Task 2: take-profit sells the same day; otherwise hold and roll to the next trading day."""
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from coopa_trada.broker import DryRunBroker
from coopa_trada.calendar_svc import CalendarService
from coopa_trada.contracts import SyntheticResolver
from coopa_trada.engine import Engine
from coopa_trada.exits import Quote
from coopa_trada.models import AssetClass, Right, Signal
from coopa_trada.rules import load_rules
from coopa_trada.store import Store

NY = ZoneInfo("America/New_York")
WED, THU, FRI = date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)


def ny(day, h, m=0):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=NY)


def q(mid):
    return Quote(round(mid - .01, 2), round(mid + .01, 2), mid)


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "t.db") as s:
        yield s


def buy(store, day, expiration=date(2026, 9, 18)):
    """A $1.00 entry bought on ``day``, as a dry-run fill."""
    sig = Signal("s1", "AMD", Right.CALL, 100, expiration, 1, AssetClass.SINGLE_NAME, ny(day, 9), "x")
    store.record_signal(sig)
    contract = replace(SyntheticResolver().resolve(sig), bid=0.99, ask=1.00, mid=0.995)
    DryRunBroker(store).place_entry(contract, 1, 1.00, signal_id=sig.id, day=day)


def engine(store, start, price, **rule_changes):
    """Engine on a clock that advances with each sleep; ``price(now)`` gives the mid."""
    clock = [start]

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)
        assert clock[0].astimezone(NY).hour < 17, "run did not finish by the close"

    rules = replace(load_rules("config/rules.example.yaml"), **rule_changes)
    return Engine(source=SimpleNamespace(fetch=lambda day: []), resolver=SyntheticResolver(),
                  broker=DryRunBroker(store), store=store, rules=rules, cal=CalendarService(),
                  mode=DryRunBroker.mode, quote_source=lambda _: q(price(clock[0])),
                  now=lambda: clock[0], sleep=sleep), clock


def exits(store):
    return [(r["strategy"]) for r in store.conn.execute("SELECT strategy FROM exit_events ORDER BY id")]


def test_take_profit_on_entry_day_sells_that_day(store):
    buy(store, WED)
    eng, _ = engine(store, ny(WED, 15, 50), lambda now: 1.65)  # +65% >= 60% take-profit
    eng.run(WED)
    assert exits(store) == ["TakeProfit"] and not store.open_positions()


def test_without_take_profit_the_position_is_held_overnight_and_the_run_ends_cleanly(store):
    buy(store, WED)
    eng, clock = engine(store, ny(WED, 15, 50), lambda now: 1.20)  # +20%: nothing fires
    eng.run(WED)  # returns normally instead of "Market closed with unsettled positions"
    assert exits(store) == [] and len(store.open_positions()) == 1
    assert clock[0] >= ny(WED, 15, 58)  # watched through the end of the exit window


@pytest.mark.parametrize("stops_today, expected", [(True, ["StopLoss"]), (False, [])])
def test_entry_day_stops_follow_the_setting(store, stops_today, expected):
    buy(store, WED)
    # $1.00 -> $0.55: -45%. The hard stop (-40%) would fire.
    eng, _ = engine(store, ny(WED, 15, 50), lambda now: 0.55, stops_on_entry_day=stops_today,
                    profit_trail_points=None, cooper_drop_from_peak_pct=None)
    eng.run(WED)
    assert exits(store) == expected
    assert len(store.open_positions()) == (0 if expected else 1)


def test_entry_day_ignores_stops_but_not_take_profit_when_stops_are_off(store):
    buy(store, WED)
    eng, _ = engine(store, ny(WED, 15, 50), lambda now: 0.50 if now < ny(WED, 15, 53) else 1.70,
                    stops_on_entry_day=False)
    eng.run(WED)
    assert exits(store) == ["TakeProfit"]


def test_an_option_expiring_on_its_entry_day_is_never_held(store):
    buy(store, WED, expiration=WED)
    eng, _ = engine(store, ny(WED, 15, 50), lambda now: 1.20)
    eng.run(WED)
    assert exits(store) == ["HardTimeExit"] and not store.open_positions()


def test_next_day_every_exit_applies_from_the_open(store):
    buy(store, WED)
    # Thursday morning the price drops 45%: the stop fires at the open, long before
    # the afternoon window. (Profit-point trail would fire first at -10%; turn it off
    # here to show the plain hard stop applies.)
    eng, clock = engine(store, ny(THU, 9, 0), lambda now: 0.55 if now >= ny(THU, 10) else 1.0,
                        profit_trail_points=None, cooper_drop_from_peak_pct=None)
    placed = []
    real_place_exit = eng.broker.place_exit
    eng.broker.place_exit = lambda *a, **k: placed.append(clock[0]) or real_place_exit(*a, **k)
    eng.run(THU)
    assert exits(store) == ["StopLoss"]
    assert placed == [ny(THU, 10)]  # sold the moment the stop was hit, in the morning


def test_next_day_with_no_trigger_sells_at_the_window_end(store):
    buy(store, WED)
    eng, clock = engine(store, ny(THU, 9, 0), lambda now: 1.05)
    eng.run(THU)
    assert exits(store) == ["HardTimeExit"] and not store.open_positions()
    assert ny(THU, 15, 58) <= clock[0] < ny(THU, 16)


def test_next_day_start_setting_delays_monitoring(store):
    buy(store, WED)
    seen = []

    def price(now):
        seen.append(now)
        return 0.55  # would trip the stops as soon as they are evaluated

    eng, _ = engine(store, ny(THU, 9, 0), price, held_exit_start=time(14, 0))
    eng.run(THU)
    first = store.conn.execute("SELECT created_at_utc FROM exit_events").fetchone()
    assert exits(store)[0] in ("ProfitPointTrail", "TrailingPeak", "StopLoss")
    # No sell before 2:00 PM New York, although quotes were read all morning.
    plan = eng._exit_plan(THU, AssetClass.SINGLE_NAME, WED, date(2026, 9, 18))
    assert plan.kind == "held" and plan.start == ny(THU, 14)
    assert min(seen) < ny(THU, 10)


def test_overdue_position_is_sold_from_the_start_of_monitoring(store):
    buy(store, WED)
    eng, _ = engine(store, ny(FRI, 9, 0), lambda now: 1.05)  # missed Thursday entirely
    plan = eng._exit_plan(FRI, AssetClass.SINGLE_NAME, WED, date(2026, 9, 18))
    assert plan.kind == "overdue" and plan.hard_exit_at == plan.start == ny(FRI, 9, 30)
    eng.run(FRI)
    assert exits(store) == ["HardTimeExit"]


def test_friday_entry_rolls_to_monday(store):
    eng, _ = engine(store, ny(FRI, 9), lambda now: 1.0)
    assert eng._exit_plan(date(2026, 9, 8), AssetClass.SINGLE_NAME, FRI, None).kind == "held"  # Mon 9/7 is Labor Day


@pytest.mark.parametrize("line", ["stops_on_entry_day: maybe", "held_exit_start: 25:00",
                                  "held_exit_start: 14:00", "held_exit_start: noonish"])
def test_bad_hold_settings_are_rejected(tmp_path, line):
    base = open("config/rules.example.yaml").read()
    key = line.split(":")[0]
    p = tmp_path / "r.yaml"
    p.write_text("\n".join(l for l in base.splitlines() if not l.startswith(key + ":")) + "\n" + line)
    with pytest.raises(ValueError, match=key):
        load_rules(p)


def test_quoted_time_and_minutes_are_accepted(tmp_path):
    base = open("config/rules.example.yaml").read().replace("held_exit_start: open", 'held_exit_start: "10:15"')
    p = tmp_path / "r.yaml"
    p.write_text(base)
    assert load_rules(p).held_exit_start == time(10, 15)
    p.write_text(base.replace('"10:15"', "60"))
    assert load_rules(p).held_exit_start == 60


def test_hold_settings_default_to_stops_on_and_monitoring_from_the_open():
    rules = load_rules("config/rules.example.yaml")
    assert (rules.stops_on_entry_day, rules.held_exit_start) == (True, "open")
