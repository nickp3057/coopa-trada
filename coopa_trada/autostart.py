"""Start the day's run automatically, so nobody has to press Start.

Checked by the watchdog every minute. On each trading day, once the Mountain
time reaches ``auto_start_time`` (and before the close), it starts the run if
none is active and none was started today, so a run a person started or
stopped earlier the same day is left alone. One attempt per day: a refused
start is alerted like any other refused run and not retried.

The automatic run is a practice (DRY-RUN) run unless the server environment
has both ``LIVE_TRADING=1`` and ``AUTO_START_LIVE=1``. That second, separate
opt-in replaces the typed confirmation phrase for unattended live trading; every
other live check (today's date, Schwab login covering the hold, reconcile,
locks) still runs inside ``Engine.run``.
"""

from __future__ import annotations

import os
from datetime import date, datetime

from . import clock
from .config import _truthy
from .logs import get_logger

__all__ = ["AutoStarter"]

log = get_logger("coopa_trada.autostart", "APP")


class AutoStarter:
    def __init__(self, paths, runner, *, env=None, cal=None):
        self.paths = paths
        self.runner = runner
        self.env = os.environ if env is None else env
        self._cal = cal
        self.last_day: date | None = None

    @property
    def cal(self):
        if self._cal is None:
            from .calendar_svc import CalendarService
            self._cal = CalendarService()
        return self._cal

    def live(self) -> bool:
        return _truthy(self.env.get("LIVE_TRADING")) and _truthy(self.env.get("AUTO_START_LIVE"))

    def check(self, now: datetime) -> bool:
        """Start today's run if it is due. Returns True when a run was started."""
        from .rules import load_rules
        try:
            rules = load_rules(self.paths.config_path)
        except (ValueError, TypeError, OSError):
            return False  # the Settings page and the run itself report a broken file
        if not rules.auto_start_daily:
            return False
        today = clock.to_market(now).date()
        if self.last_day == today or not self.cal.is_trading_day(today):
            return False
        due = datetime.combine(clock.to_display(now).date(), rules.auto_start_time, tzinfo=clock.DISPLAY_TZ)
        if now < due or now >= self.cal.session_close(today):
            return False
        if self.runner.status == "running":
            return False
        started = getattr(self.runner, "last_started", None)
        if started and clock.to_market(clock.parse_iso(started)).date() == today:
            self.last_day = today  # already run (or stopped by a person) today
            return False
        self.last_day = today
        live = self.live()
        log.info("starting today's %s run automatically", "LIVE" if live else "practice")
        try:
            self.runner.start(live=live, date_str=today.isoformat())
        except Exception as exc:  # noqa: BLE001 - e.g. a run started a moment earlier
            log.warning("automatic start skipped: %s", exc)
            return False
        return True
