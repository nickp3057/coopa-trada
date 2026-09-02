"""Exit logic proven against synthetic price series, before any live quote."""

from datetime import datetime, timedelta, timezone

import pytest

from coopa_trada.exits import (
    CompositeExit,
    HardTimeExit,
    PositionState,
    Quote,
    StopLoss,
    TakeProfit,
    TrailingPeak,
    update_peak,
)
from coopa_trada.models import AssetClass

UTC = timezone.utc
T0 = datetime(2026, 9, 2, 19, 40, tzinfo=UTC)
HARD_EXIT = datetime(2026, 9, 2, 19, 58, tzinfo=UTC)


def _pos(entry=1.00, peak=1.00):
    return PositionState(
        osi_symbol="AAPL  260918C00250000",
        entry_price=entry,
        entry_at=T0,
        quantity=3,
        session_peak_mid=peak,
        asset_class=AssetClass.SINGLE_NAME,
        hard_exit_at=HARD_EXIT,
    )


def _q(mid, spread=0.02):
    return Quote(bid=round(mid - spread / 2, 4), ask=round(mid + spread / 2, 4), mid=mid)


def _series(pos, strategy, mids, *, start=T0, step_s=30):
    """Feed a price series tick by tick; return (decision, tick_index) or (None, n)."""
    now = start
    for i, mid in enumerate(mids):
        q = _q(mid)
        update_peak(pos, q)
        d = strategy.check(pos, q, now)
        if d is not None:
            return d, i
        now += timedelta(seconds=step_s)
    return None, len(mids)


# --- 1. steady climb then retrace -> TrailingPeak --------------------------
def test_trailing_peak_fires_after_pct_retrace_from_peak():
    pos = _pos(entry=1.00, peak=1.00)
    strat = TrailingPeak(0.25)
    climb = [1.00, 1.20, 1.60, 2.00, 2.40]  # peak -> 2.40
    retrace = [2.20, 1.95, 1.79]            # 1.79 <= 2.40 * 0.75 = 1.80
    decision, idx = _series(pos, strat, climb + retrace)
    assert decision is not None
    assert decision.strategy == "TrailingPeak"
    assert pos.session_peak_mid == 2.40
    assert idx == len(climb + retrace) - 1
    assert decision.price == _q(1.79).bid  # sells at the bid


def test_trailing_peak_quiet_while_still_near_highs():
    pos = _pos(peak=1.00)
    decision, _ = _series(pos, TrailingPeak(0.25), [1.0, 1.5, 2.0, 1.7, 1.9, 1.6])
    assert decision is None  # 1.60 > 2.00 * 0.75


# --- 2. gap-down straight through the stop in one tick -> StopLoss ---------
def test_stop_loss_fires_on_gap_through_stop_and_reports_gapped_price():
    pos = _pos(entry=1.00)
    strat = StopLoss(0.40)  # stop level = 0.60
    # one tick from 0.95 to 0.42 -- blows past 0.60 entirely
    decision, idx = _series(pos, strat, [0.95, 0.42])
    assert decision is not None
    assert decision.strategy == "StopLoss"
    assert idx == 1
    # decision still fires; price is the gapped bid, well below the 0.60 stop
    assert decision.price == _q(0.42).bid
    assert decision.price < 0.60


# --- 3. spike hits take-profit within a single poll interval -> TakeProfit -
def test_take_profit_fires_within_one_interval_on_spike():
    pos = _pos(entry=1.00)
    strat = TakeProfit(0.60)  # target = 1.60
    decision, idx = _series(pos, strat, [1.05, 1.95])  # single step spike over target
    assert decision is not None
    assert decision.strategy == "TakeProfit"
    assert idx == 1
    assert decision.at == T0 + timedelta(seconds=30)


# --- 4. clock passes hard_exit_at with price fine -> HardTimeExit ----------
def test_hard_time_exit_fires_purely_on_the_clock():
    pos = _pos(entry=1.00)
    strat = HardTimeExit()
    healthy = _q(1.05)
    assert strat.check(pos, healthy, HARD_EXIT - timedelta(seconds=1)) is None
    d = strat.check(pos, healthy, HARD_EXIT)
    assert d is not None and d.strategy == "HardTimeExit"
    assert strat.check(pos, healthy, HARD_EXIT + timedelta(minutes=5)) is not None


# --- 5. CompositeExit ordering when two fire on the same tick --------------
def test_composite_returns_first_matching_strategy_in_order():
    pos = _pos(entry=1.00, peak=2.00)
    q = _q(0.50)  # below stop (0.60) AND a >25% retrace from peak 2.00
    now = T0

    stop_first = CompositeExit([StopLoss(0.40), TrailingPeak(0.25)])
    trail_first = CompositeExit([TrailingPeak(0.25), StopLoss(0.40)])

    assert stop_first.check(pos, q, now).strategy == "StopLoss"
    assert trail_first.check(_pos(entry=1.00, peak=2.00), q, now).strategy == "TrailingPeak"


def test_composite_none_when_nothing_triggers():
    pos = _pos(entry=1.00, peak=1.10)
    comp = CompositeExit([StopLoss(0.40), TakeProfit(0.60), TrailingPeak(0.25), HardTimeExit()])
    assert comp.check(pos, _q(1.05), T0) is None


def test_update_peak_is_monotonic():
    pos = _pos(peak=1.0)
    for mid, expected in [(1.2, 1.2), (1.1, 1.2), (1.5, 1.5), (0.9, 1.5)]:
        assert update_peak(pos, _q(mid)) == expected


@pytest.mark.parametrize("bad", [0, 1, -0.1, 1.5])
def test_pct_strategies_validate_range(bad):
    with pytest.raises(ValueError):
        StopLoss(bad)
    with pytest.raises(ValueError):
        TrailingPeak(bad)


def test_position_state_rejects_naive_datetimes():
    with pytest.raises(Exception):
        PositionState(
            osi_symbol="X", entry_price=1.0, entry_at=datetime(2026, 9, 2, 12, 0),
            quantity=1, session_peak_mid=1.0, asset_class=AssetClass.ETF,
            hard_exit_at=HARD_EXIT,
        )
