"""Daily schedule: start each trading day's run automatically before the open,
and send one end-of-day summary after the exit windows close.

Runs as a background thread in the desktop app and ``python -m coopa_trada.web``
(next to the watchdog). Once a minute it checks the clock; on a trading day,
at ``AUTO_START_TIME`` (Mountain time, default 07:15, before the 07:30 MT open)
it starts that day's run through the same :class:`EngineRunner` the Start button
uses, so held positions are watched from the open and picks confirmed later in
the day are bought inside the buying window.

Off unless ``AUTO_START=1``. It never starts a run when:

* the Schwab login does not last through the next trading day's exit window
  (the same check as the Start button and the engine; checked for practice
  runs too, so a lapsing login is found before it matters), or
* a live start was asked for (``AUTO_START_LIVE`` set to the exact phrase the
  Start button asks for) but ``LIVE_TRADING`` is not on, or a setting is still
  unconfirmed.

Each refusal is an alert (Mac + ntfy) saying why. A live run still passes every
gate in the engine (confirmed settings, login, reconcile, run locks, today's
date); this module adds gates, it never removes one. Practice (DRY-RUN) is the
default; ``AUTO_START_LIVE`` is the only way to ask for a live start.

One attempt per trading day per process: a refused start is not retried (a
person starts it from the Dashboard after fixing the cause). After a restart
(crash or reboot) it tries again, so the day's positions are watched again.

End-of-day summary (on unless ``DAILY_SUMMARY=0``): once the latest exit
window of the day has ended and no run is active (or 30 minutes after the
close, whichever comes first), one alert with counts only (see summary). It is
sent once per trading day; a marker file next to the database keeps a restart
from sending it twice.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import clock
from .alerts import Alerter
from .logs import get_logger

__all__ = ["AutoStartConfig", "DailyScheduler", "LIVE_PHRASE", "DEFAULT_START"]

log = get_logger("coopa_trada.scheduler", "APP")

DEFAULT_START = time(7, 15)
# Sent at the latest this long after the close, even if a run is still active.
SUMMARY_LATEST_AFTER_CLOSE = timedelta(minutes=30)
# The phrase the Dashboard's live Start asks for (web.views.LIVE_CONFIRM_PHRASE).
LIVE_PHRASE = "ENABLE LIVE TRADING"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AutoStartConfig:
    enabled: bool = False
    at: time = DEFAULT_START          # Mountain time
    live: bool = False
    problem: str | None = None        # a setting that could not be read
    summary: bool = True              # end-of-day summary alert

    @classmethod
    def from_env(cls, env: dict | None = None) -> "AutoStartConfig":
        e = os.environ if env is None else env
        enabled = _truthy(e.get("AUTO_START"))
        raw = (e.get("AUTO_START_TIME") or "").strip()
        at, problem = DEFAULT_START, None
        if raw:
            try:
                hh, mm = raw.split(":")
                at = time(int(hh), int(mm))
            except ValueError:
                problem = (f"AUTO_START_TIME must be a 24-hour Mountain time like 07:15, "
                           f"not {raw!r}.")
        live_raw = (e.get("AUTO_START_LIVE") or "").strip()
        live = live_raw == LIVE_PHRASE
        if live_raw and not live and problem is None:
            problem = (f'AUTO_START_LIVE must be exactly "{LIVE_PHRASE}" (or blank for practice '
                       f"runs).")
        summary = (e.get("DAILY_SUMMARY") or "1").strip().lower() not in {"0", "false", "no", "off"}
        return cls(enabled=enabled, at=at, live=live, problem=problem, summary=summary)

    def describe(self) -> str:
        if not self.enabled:
            return "off (set AUTO_START=1 to start each trading day automatically)"
        hm = datetime.combine(date(2000, 1, 3), self.at).strftime("%I:%M %p").lstrip("0")
        return f"{'LIVE' if self.live else 'practice'} run at {hm} MT on trading days"


class DailyScheduler:
    def __init__(self, paths, runner, alerter: Alerter, *, config: AutoStartConfig | None = None,
                 cal=None, interval: float = 60.0,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 env: dict | None = None):
        self.paths = paths
        self.runner = runner
        self.alerter = alerter
        self.config = config if config is not None else AutoStartConfig.from_env(env)
        self._env = env
        self._cal = cal
        self.interval = interval
        self._now = now
        self._attempted: date | None = None
        self.last_attempt: dict | None = None   # for the Dashboard / logs
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- loop (same shape as the watchdog) ------------------------------------
    def start(self) -> "DailyScheduler":
        log.info("auto-start: %s", self.config.describe())
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
        return {"auto_start": self.config.describe(), "last_attempt": self.last_attempt}

    # -- one check -------------------------------------------------------------
    def tick(self) -> None:
        now = self._now()
        for step in (self._auto_start, self._daily_summary):
            try:
                step(now)
            except Exception:  # one failing step must not stop the other
                log.exception("scheduler step %s failed", step.__name__)

    def start_time(self, day: date) -> datetime:
        """The auto-start moment on ``day``, as a tz-aware datetime."""
        return datetime.combine(day, self.config.at, tzinfo=clock.DISPLAY_TZ)

    def _auto_start(self, now: datetime) -> None:
        if not self.config.enabled:
            return
        day = clock.to_market(now).date()
        if self._attempted == day:
            return
        if self.config.problem:
            self._attempted = day
            self._refuse(day, f"Auto-start is not set up correctly. {self.config.problem}")
            return
        if not self.cal.is_trading_day(day) or now < self.start_time(day):
            return
        self._attempted = day
        if now >= self.cal.session_close(day):
            return  # the app started after the close: nothing left to run today
        if self.runner.status == "running":
            self._record(day, "already running", ok=True)
            return
        reason = self._block_reason(day, now)
        if reason is not None:
            self._refuse(day, reason)
            return
        from .web.runner import RunnerBusy
        try:
            self.runner.start(live=self.config.live, date_str=day.isoformat())
        except RunnerBusy:
            self._record(day, "already running", ok=True)
            return
        late = now >= self.cal.session_open(day)
        self._record(day, f"started a {'LIVE' if self.config.live else 'practice'} run"
                          f"{' (late: after the open)' if late else ''}", ok=True)
        # Refusals inside the run (reconcile, locks, ...) alert from the runner/engine.

    def _block_reason(self, day: date, now: datetime) -> str | None:
        from .config import credentials_from_env
        from .rules import load_rules
        from .schwab_token import hold_needed_until, read_token_status, start_block_reason

        try:
            rules = load_rules(self.paths.config_path)
        except Exception as exc:
            return f"The rules file could not be read: {exc}"
        if self.config.live:
            env = os.environ if self._env is None else self._env
            if not _truthy(env.get("LIVE_TRADING")):
                return ("A live auto-start is set (AUTO_START_LIVE) but LIVE_TRADING is not on, "
                        "so nothing was started. Turn both on, or clear AUTO_START_LIVE for "
                        "practice runs.")
            blocked = rules.live_block_reason()
            if blocked is not None:
                return blocked
        creds = credentials_from_env(self._env)
        try:
            needed = hold_needed_until(self.cal, rules, day)
        except Exception as exc:  # e.g. beyond the bundled market calendar
            return f"Could not work out how long today's run needs Schwab: {exc}"
        return start_block_reason(read_token_status(creds.token_path, now), needed)

    def _record(self, day: date, what: str, *, ok: bool) -> None:
        self.last_attempt = {"day": day.isoformat(), "ok": ok, "result": what}
        (log.info if ok else log.warning)("auto-start %s: %s", day.isoformat(), what)

    def _refuse(self, day: date, reason: str) -> None:
        self._record(day, reason, ok=False)
        self.alerter.alert("autostart_refused", "Coopa Trada did not start today",
                           f"Today's automatic start was skipped. {reason} Start the run from the "
                           f"Dashboard once this is fixed.", key=f"autostart:{day.isoformat()}")

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
                           autostart=self.last_attempt, token=token, locks=locks,
                           autostart_enabled=self.config.enabled and not self.config.problem)
        finally:
            store.close()

    def _creds(self):
        from .config import credentials_from_env
        return credentials_from_env(self._env)
