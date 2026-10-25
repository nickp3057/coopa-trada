"""Task 1: Cooper's profit-point trailing exit, with worked dollar examples.

Rule: sell when the profit % drops 10 percentage points below its peak profit %.
Trigger price = peak - 0.10 x entry.
"""
from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from coopa_trada.broker import DryRunBroker
from coopa_trada.calendar_svc import CalendarService
from coopa_trada.contracts import SyntheticResolver
from coopa_trada.engine import Engine
from coopa_trada.exits import PositionState, ProfitPointTrail, Quote
from coopa_trada.models import AssetClass, Right, Signal
from coopa_trada.rules import load_rules
from coopa_trada.store import Store

NOW = datetime(2026, 9, 2, 19, 50, tzinfo=timezone.utc)  # 3:50 PM ET, inside the exit window
DAY = date(2026, 9, 2)


def pos(entry, peak):
    return PositionState("X", entry, NOW, 1, peak, AssetClass.SINGLE_NAME, NOW.replace(hour=23))


def q(mid):
    return Quote(round(mid - .01, 2), round(mid + .01, 2), mid)


def test_cooper_example_one_dollar_entry_two_dollar_peak_sells_at_one_ninety():
    rule = ProfitPointTrail(10)
    assert rule.trigger(pos(1.00, 2.00)) == pytest.approx(1.90)
    assert rule.check(pos(1.00, 2.00), q(1.91), NOW) is None        # +91%: 9 points off the peak
    decision = rule.check(pos(1.00, 2.00), q(1.90), NOW)             # +90%: exactly 10 points
    assert decision is not None and decision.strategy == "ProfitPointTrail"
    assert decision.price == 1.89                                    # sells at the bid
    assert "10.0 points below peak profit +100.0%" in decision.reason


def test_drop_is_in_points_of_entry_not_percent_of_peak():
    # Entry $2.50, peak $3.00 (+20%). 10 points -> +10% -> $2.75 (not 10% of $3.00 = $2.70).
    rule = ProfitPointTrail(10)
    assert rule.trigger(pos(2.50, 3.00)) == pytest.approx(2.75)
    assert rule.check(pos(2.50, 3.00), q(2.76), NOW) is None
    assert rule.check(pos(2.50, 3.00), q(2.75), NOW) is not None


def test_from_entry_acts_as_a_ten_percent_stop_before_any_gain():
    # Entry $1.00, never above $1.00: peak profit 0% -> sells at -10% = $0.90.
    rule = ProfitPointTrail(10, "from_entry")
    assert rule.check(pos(1.00, 1.00), q(0.91), NOW) is None
    assert rule.check(pos(1.00, 1.00), q(0.90), NOW) is not None


def test_in_profit_arming_waits_for_a_gain():
    rule = ProfitPointTrail(10, "in_profit")
    assert rule.trigger(pos(1.00, 1.00)) is None
    assert rule.check(pos(1.00, 1.00), q(0.50), NOW) is None         # never in profit: not armed
    # Once the price reached $1.20 (+20%) it arms: sells at +10% = $1.10.
    assert rule.trigger(pos(1.00, 1.20)) == pytest.approx(1.10)
    assert rule.check(pos(1.00, 1.20), q(1.10), NOW) is not None
    # A tiny gain arms it too; the trigger can then sit below entry ($1.01 peak -> $0.91).
    assert rule.trigger(pos(1.00, 1.01)) == pytest.approx(0.91)


def test_invalid_settings_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        ProfitPointTrail(0)
    with pytest.raises(ValueError):
        ProfitPointTrail(10, "sometimes")
    base = open("config/rules.example.yaml").read()
    for bad in ("profit_trail_points: 150", "profit_trail_arm: whenever"):
        p = tmp_path / "r.yaml"
        key = bad.split(":")[0]
        p.write_text("\n".join(l for l in base.splitlines() if not l.startswith(key + ":")) + "\n" + bad)
        with pytest.raises(ValueError, match=key):
            load_rules(p)


def test_rules_default_to_ten_points_from_entry_and_blank_turns_it_off(tmp_path):
    rules = load_rules("config/rules.example.yaml")
    assert (rules.profit_trail_points, rules.profit_trail_arm) == (10, "from_entry")
    p = tmp_path / "r.yaml"
    p.write_text(open("config/rules.example.yaml").read().replace("profit_trail_points: 10", "profit_trail_points:"))
    assert load_rules(p).profit_trail_points is None


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "t.db") as s:
        yield s


def _engine(store, quotes, **rule_changes):
    sig = Signal("s", "AMD", Right.CALL, 100, date(2026, 9, 4), 1, AssetClass.SINGLE_NAME, NOW, "x")
    rules = replace(load_rules("config/rules.example.yaml"), **rule_changes)
    broker = DryRunBroker(store)
    feed = iter(quotes)
    eng = Engine(source=SimpleNamespace(fetch=lambda day: [sig]), resolver=SyntheticResolver(), broker=broker,
                 store=store, rules=rules, cal=CalendarService(), mode=broker.mode,
                 quote_source=lambda _: next(feed), now=lambda: NOW, sleep=lambda _: None)
    store.record_signal(sig)
    contract = replace(SyntheticResolver().resolve(sig), bid=0.99, ask=1.00, mid=0.995)
    broker.place_entry(contract, 1, 1.00, signal_id=sig.id, day=DAY)  # bought at $1.00
    return eng


def test_engine_sells_on_the_profit_point_trail_when_it_fires_first(store):
    # $1.00 entry. Rises to $1.50 (+50%, under the 60% take-profit), falls to $1.40 (+40%):
    # 10 points off the peak. The 25% trailing stop ($1.125) and the old 10%-of-peak
    # reading ($1.35) have not fired yet, so the profit-point trail wins.
    eng = _engine(store, [q(1.30), q(1.50), q(1.45), q(1.40)])
    assert [eng._exit_tick(DAY) for _ in range(4)] == [0, 0, 0, 1]
    event = store.conn.execute("SELECT strategy, peak_mid FROM exit_events").fetchone()
    assert (event["strategy"], event["peak_mid"]) == ("ProfitPointTrail", 1.50)
    sold = store.position_report()[0]
    assert sold["exit_price"] == 1.39 and sold["realized_pnl"] == pytest.approx(39.0)  # (1.39-1.00) x 100


def test_engine_other_exits_still_win_when_they_fire_first(store):
    # $1.00 -> $1.60 hits the 60% take-profit before any 10-point retrace.
    eng = _engine(store, [q(1.30), q(1.60)])
    assert [eng._exit_tick(DAY) for _ in range(2)] == [0, 1]
    assert store.conn.execute("SELECT strategy FROM exit_events").fetchone()[0] == "TakeProfit"


def test_engine_without_the_rule_holds_through_a_ten_point_retrace(store):
    eng = _engine(store, [q(1.50), q(1.40)], profit_trail_points=None, cooper_drop_from_peak_pct=None)
    assert [eng._exit_tick(DAY) for _ in range(2)] == [0, 0]


def test_settings_page_saves_the_rule(tmp_path):
    from coopa_trada.web import create_app
    from coopa_trada.web.config import WebPaths
    cfg = tmp_path / "rules.yaml"
    cfg.write_text(open("config/rules.example.yaml").read())
    client = create_app(WebPaths(config_path=str(cfg), db_path=str(tmp_path / "t.db"))).test_client()
    good = client.get("/api/rules").get_json()
    assert (good["profit_trail_points"], good["profit_trail_arm"]) == (10, "from_entry")
    body = client.post("/api/rules", json={**good, "profit_trail_arm": "in_profit"}).get_json()
    assert body["profit_trail_arm"] == "in_profit"
    assert load_rules(cfg).profit_trail_arm == "in_profit"
    assert client.post("/api/rules", json={**good, "profit_trail_arm": "never"}).status_code == 400


# -- what the exit rules measure from ------------------------------------------------
def _wide(store, reference):
    """Bought at a $1.00 ask while the bid was $0.80 (mid $0.90): a 20% spread."""
    sig = Signal("w", "AMD", Right.CALL, 100, date(2026, 9, 4), 1, AssetClass.SINGLE_NAME, NOW, "x")
    rules = replace(load_rules("config/rules.example.yaml"), exit_reference_price=reference)
    broker = DryRunBroker(store)
    quotes = []
    eng = Engine(source=SimpleNamespace(fetch=lambda day: []), resolver=SyntheticResolver(), broker=broker,
                 store=store, rules=rules, cal=CalendarService(), mode=broker.mode,
                 quote_source=lambda _: quotes.pop(0), now=lambda: NOW, sleep=lambda _: None)
    store.record_signal(sig)
    contract = replace(SyntheticResolver().resolve(sig), bid=0.80, ask=1.00, mid=0.90)
    broker.place_entry(contract, 1, 1.00, signal_id=sig.id, day=DAY)
    return eng, quotes


def test_mid_at_entry_keeps_a_wide_spread_from_selling_at_once(store):
    eng, quotes = _wide(store, "mid_at_entry")
    quotes += [Quote(0.80, 1.00, 0.90)]                  # unchanged market
    assert eng._exit_tick(DAY) == 0
    state, _ = eng._positions_state(DAY)[0]
    assert state.entry_price == 0.90                    # rules start from the mid
    quotes += [Quote(1.34, 1.54, 1.44)]                  # +60% from the $0.90 mid
    assert eng._exit_tick(DAY) == 1
    row = store.position_report()[0]
    assert (row["entry_price"], row["exit_price"]) == (1.00, 1.34)   # real prices in the log
    assert row["realized_pnl"] == pytest.approx(34.0)


def test_fill_reference_reproduces_the_instant_sell(store):
    eng, quotes = _wide(store, "fill")
    quotes += [Quote(0.80, 1.00, 0.90)]                  # -10% from the $1.00 paid
    assert eng._exit_tick(DAY) == 1
    assert store.conn.execute("SELECT strategy FROM exit_events").fetchone()[0] in (
        "ProfitPointTrail", "TrailingPeak")


def test_ten_point_rule_from_the_mid_before_any_gain(store):
    eng, quotes = _wide(store, "mid_at_entry")
    quotes += [Quote(0.72, 0.92, 0.82)]                  # $0.90 - 10% of $0.90 = $0.81: not yet
    assert eng._exit_tick(DAY) == 0
    quotes += [Quote(0.71, 0.91, 0.81)]
    assert eng._exit_tick(DAY) == 1


def test_older_orders_without_a_recorded_mid_use_the_price_paid(store):
    eng, quotes = _wide(store, "mid_at_entry")
    store.conn.execute("UPDATE orders SET entry_mid = NULL")
    state, _ = eng._positions_state(DAY)[0]
    assert state.entry_price == 1.00


def test_exit_reference_setting_validates(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(open("config/rules.example.yaml").read().replace(
        "exit_reference_price: mid_at_entry", "exit_reference_price: last"))
    with pytest.raises(ValueError, match="exit_reference_price"):
        load_rules(p)
