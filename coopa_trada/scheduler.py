"""End-of-day summary: one alert after the day's exit windows close.

Runs as a background thread in the desktop app and ``python -m coopa_trada.web``
(next to the watchdog, which does the automatic daily start; see
:mod:`coopa_trada.autostart`). Once a minute it checks the clock.

On unless ``DAILY_SUMMARY=0``: once the latest exit window of the day has ended
and no run is active (or 30 minutes after the close, whichever comes first),
one alert with counts only (see :mod:`coopa_trada.summary`). It is sent once per
trading day; a marker file next to the database keeps a restart from sending
it twice.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import clock
from .alerts import Alerter
from .logs import get_logger

__all__ = ["SummaryConfig", "DailyScheduler"]

log = get_logger("coopa_trada.scheduler", "APP")

# Sent at the latest this long after the close, even if a run is still active.
SUMMARY_LATEST_AFTER_CLOSE = timedelta(minutes=30)


@dataclass(frozen=True)
class SummaryConfig:
    summary: bool = True              # end-of-day summary alert

    @classmethod
    def from_env(cls, env: dict | None = None) -> "SummaryConfig":
        e = os.environ if env is None else env
        summary = (e.get("DAILY_SUMMARY") or "1").strip().lower() not in {"0", "false", "no", "off"}
        return cls(summary=summary)

    def describe(self) -> str:
        return "on" if self.summary else "off (DAILY_SUMMARY=0)"


class DailyScheduler:
    def __init__(self, paths, runner, alerter: Alerter, *, config: SummaryConfig | None = None,
                 cal=None, interval: float = 60.0,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 env: dict | None = None):
        self.paths = paths
        self.runner = runner
        self.alerter = alerter
        self.config = config if config is not None else SummaryConfig.from_env(env)
        self._env = env
        self._cal = cal
        self.interval = interval
        self._now = now
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- loop (same shape as the watchdog) ------------------------------------
    def start(self) -> "DailyScheduler":
        log.info("end-of-day summary: %s", self.config.describe())
        self._thread = threading.Thread(target=self._loop, name="coopa-scheduler", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # the schedule must keep running
                log.exception("scheduler tick failed")
            self._stop.wait(self.interval)

    @property
    def cal(self):
        if self._cal is None:
            from .calendar_svc import CalendarService
            self._cal = CalendarService()
        return self._cal

    def snapshot(self) -> dict:
        return {"daily_summary": self.config.describe()}

    # -- one check -------------------------------------------------------------
    def tick(self) -> None:
        now = self._now()
        self._daily_summary(now)

    # -- end-of-day summary ------------------------------------------------------
    def _summary_marker(self) -> Path:
        return Path(str(self.paths.db_path) + ".summary-sent")

    def _summary_sent_for(self) -> str:
        try:
            return self._summary_marker().read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def summary_due_at(self, day: date) -> datetime:
        """The end of the day's latest exit window (the close if the rules can't be read)."""
        from .rules import load_rules
        close = self.cal.session_close(day)
        try:
            rules = load_rules(self.paths.config_path)
        except Exception:
            return close
        ends = []
        for asset_class in rules.exit_window:
            try:
                ends.append(self.cal.exit_window(day, asset_class, rules)[1])
            except (ValueError, TypeError, KeyError):
                ends.append(close)
        return max(ends, default=close)

    def _daily_summary(self, now: datetime) -> None:
        if not self.config.summary:
            return
        day = clock.to_market(now).date()
        if not self.cal.is_trading_day(day) or self._summary_sent_for() == day.isoformat():
            return
        close = self.cal.session_close(day)
        if now < self.summary_due_at(day):
            return
        if self.runner.status == "running" and now < close + SUMMARY_LATEST_AFTER_CLOSE:
            return  # still selling; wait for it (but not forever)
        summary = self.build_summary(day, now)
        # Marked first: a failed channel is logged by the alerter, never re-sent in a loop.
        self._summary_marker().write_text(day.isoformat(), encoding="utf-8")
        self.alerter.alert("daily_summary", summary.title, summary.message,
                           key=f"summary:{day.isoformat()}", repeat_after=timedelta(hours=20))

    def build_summary(self, day: date, now: datetime):
        from .picks import imports_dir
        from .runlock import known_locks
        from .store import Store
        from .summary import collect
        from .wiring import token_status

        try:
            token = token_status(creds=self._creds(), now=now, config_path=self.paths.config_path)
        except Exception:
            token = None
        try:
            locks = known_locks(self.paths.db_path)
        except Exception:
            locks = []
        snap = self.runner.snapshot() if hasattr(self.runner, "snapshot") else None
        store = Store.open(self.paths.db_path)
        try:
            return collect(store, day, picks_folder=imports_dir(self.paths.db_path), runner=snap,
                           token=token, locks=locks, autostart_enabled=self._autostart_on())
        finally:
            store.close()

    def _autostart_on(self) -> bool:
        from .rules import load_rules
        try:
            return load_rules(self.paths.config_path).auto_start_daily
        except Exception:
            return False

    def _creds(self):
        from .config import credentials_from_env
        return credentials_from_env(self._env)
