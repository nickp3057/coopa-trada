from datetime import date

import pytest

from coopa_trada.calendar_svc import CalendarService, NotATradingDay
from coopa_trada.clock import MARKET_TZ
from coopa_trada.models import AssetClass
from coopa_trada.rules import load_rules

CAL = CalendarService()


def test_regular_session_closes_at_16_00_et():
    close = CAL.session_close(date(2026, 9, 2)).astimezone(MARKET_TZ)
    assert (close.hour, close.minute) == (16, 0)


def test_known_half_day_closes_before_16_00():
    # Friday after US Thanksgiving 2026 -> early close at 13:00 ET.
    half = date(2026, 11, 27)
    assert CAL.is_trading_day(half)
    close = CAL.session_close(half).astimezone(MARKET_TZ)
    assert (close.hour, close.minute) == (13, 0)
    assert close.hour != 16


def test_weekend_and_holiday_are_not_trading_days():
    assert not CAL.is_trading_day(date(2026, 9, 5))   # Saturday
    assert not CAL.is_trading_day(date(2026, 12, 25))  # Christmas
    with pytest.raises(NotATradingDay):
        CAL.session_close(date(2026, 9, 5))


def test_entry_at_is_relative_to_the_real_close():
    day = date(2026, 11, 27)  # half day
    entry = CAL.entry_at(day, 20).astimezone(MARKET_TZ)
    assert (entry.hour, entry.minute) == (12, 40)  # 20 min before the 13:00 close


def test_exit_window_resolves_time_and_minutes_endpoints(tmp_path):
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        "\n".join(
            [
                "entry_minutes_before_close: 20",
                "exit_window:",
                '  SINGLE_NAME: ["15:35", 2]',
                "  ETF: [18, 1]",
                "trailing_stop_pct: 0.25",
                "hard_stop_loss_pct: 0.40",
                "take_profit_pct: 0.60",
                "max_concurrent_positions: 5",
                "max_dollars_per_position: 1500",
                "poll_interval_seconds: 30",
            ]
        ),
        encoding="utf-8",
    )
    rules = load_rules(rules_path)
    day = date(2026, 9, 2)  # regular 16:00 close

    start, end = CAL.exit_window(day, AssetClass.SINGLE_NAME, rules)
    start = start.astimezone(MARKET_TZ)
    end = end.astimezone(MARKET_TZ)
    assert (start.hour, start.minute) == (15, 35)   # wall-clock time endpoint
    assert (end.hour, end.minute) == (15, 58)       # 2 minutes before the close

    e_start, e_end = CAL.exit_window(day, AssetClass.ETF, rules)
    assert (e_start.astimezone(MARKET_TZ).hour, e_start.astimezone(MARKET_TZ).minute) == (15, 42)
    assert (e_end.astimezone(MARKET_TZ).hour, e_end.astimezone(MARKET_TZ).minute) == (15, 59)
