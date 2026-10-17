"""Exchange-calendar wrapper. No hardcoded 9:30 or 16:00 anywhere.

Session opens/closes — including half days and holidays — come from
``exchange_calendars.get_calendar("XNYS")``. Everything returned is a
tz-aware :class:`datetime` in ``MARKET_TZ``.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import exchange_calendars as xcals
import pandas as pd

from .clock import MARKET_TZ

__all__ = ["CalendarService", "NotATradingDay"]


class NotATradingDay(ValueError):
    """Raised when a session-specific query is made for a non-session date."""


class CalendarService:
    def __init__(self, name: str = "XNYS"):
        self.name = name
        self.cal = xcals.get_calendar(name)

    # -- basics -----------------------------------------------------------
    def _session_ts(self, d: date) -> pd.Timestamp:
        ts = pd.Timestamp(d)
        return ts.normalize()

    def is_trading_day(self, d: date) -> bool:
        return bool(self.cal.is_session(self._session_ts(d)))

    def _require_session(self, d: date) -> pd.Timestamp:
        ts = self._session_ts(d)
        if not self.cal.is_session(ts):
            raise NotATradingDay(f"{d.isoformat()} is not an {self.name} session")
        return ts

    def session_open(self, d: date) -> datetime:
        ts = self._require_session(d)
        return self.cal.session_open(ts).tz_convert(MARKET_TZ).to_pydatetime()

    def session_close(self, d: date) -> datetime:
        ts = self._require_session(d)
        return self.cal.session_close(ts).tz_convert(MARKET_TZ).to_pydatetime()

    # -- derived windows ------------------------------------------------
    def entry_at(self, d: date, minutes_before_close: int) -> datetime:
        """The instant entries are placed: ``minutes_before_close`` before the close."""
        return self.session_close(d) - timedelta(minutes=int(minutes_before_close))

    def _resolve_endpoint(self, d: date, endpoint: time | int, close: datetime) -> datetime:
        if isinstance(endpoint, time):
            return datetime.combine(d, endpoint, tzinfo=MARKET_TZ)
        if isinstance(endpoint, bool):  # bool is an int subclass; reject it explicitly
            raise TypeError("exit-window endpoint must be a time or int, not bool")
        if isinstance(endpoint, int):
            return close - timedelta(minutes=endpoint)
        raise TypeError(f"exit-window endpoint must be time or int, got {type(endpoint).__name__}")

    def exit_window(self, d: date, asset_class, rules) -> tuple[datetime, datetime]:
        """(start, end) for the exit-management loop for ``asset_class`` on ``d``.

        Each endpoint in ``rules.exit_window[asset_class]`` is either a wall-clock
        ``time`` (resolved on the session date) or an ``int`` number of minutes
        before the session close.
        """
        close = self.session_close(d)
        try:
            start_ep, end_ep = rules.exit_window[asset_class]
        except KeyError:
            raise KeyError(f"rules.exit_window has no entry for {asset_class}") from None
        start = self._resolve_endpoint(d, start_ep, close)
        end = self._resolve_endpoint(d, end_ep, close)
        if end < start:
            raise ValueError(f"exit window for {asset_class} ends before it starts")
        return start, end

    # -- holding period ---------------------------------------------------
    def next_session(self, d: date) -> date:
        """The first trading day strictly after ``d`` (skips weekends and holidays)."""
        ts = self._session_ts(d + timedelta(days=1))
        try:
            return self.cal.date_to_session(ts, direction="next").date()
        except Exception as exc:  # past the end of the bundled calendar
            raise NotATradingDay(f"no {self.name} session is known after {d.isoformat()}") from exc

    def next_day_exit_end(self, d: date, rules) -> datetime:
        """The latest exit-window end, over every asset class, on the trading day after ``d``.

        Positions entered on ``d`` can still be managed until then. A window that
        does not resolve on that day (e.g. a wall-clock start after a half day's
        close) counts as lasting until that day's close.
        """
        nxt = self.next_session(d)
        close = self.session_close(nxt)
        ends = []
        for asset_class in getattr(rules, "exit_window", None) or {}:
            try:
                ends.append(self.exit_window(nxt, asset_class, rules)[1])
            except (ValueError, TypeError, KeyError):
                ends.append(close)
        return max(ends, default=close)
