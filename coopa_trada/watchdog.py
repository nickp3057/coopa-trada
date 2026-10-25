"""Background checks for the app, so problems reach a person who isn't looking.

Runs once a minute inside the desktop app (and ``python -m coopa_trada.web``),
and straight away when a run starts or ends:

* keeps the Mac awake while a run is active or a LIVE position or order is
  open (see keepawake), and releases it when neither is true;
* the Schwab login is expiring (under 2 days), expired, or won't last through
  the next run's hold;
* crash recovery needs a decision: a stale lock from a LIVE run, LIVE positions
  open with no run managing them, and (on the first check after the app
  starts) orders still waiting for someone on the Reconcile page.

Apart from the keep-awake child process, it only reads files and the database
and calls the alerter. It never trades, never contacts Schwab and never changes
a lock.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from .alerts import Alerter
from .logs import get_logger

__all__ = ["Watchdog", "TOKEN_ALERT_REPEAT"]

log = get_logger("coopa_trada.watchdog", "APP")

# Repeat a still-true login warning twice a day, not every 6 hours.
TOKEN_ALERT_REPEAT = timedelta(hours=12)
# Warn this many days before the bundled market calendar ends.
CALENDAR_WARNING_DAYS = 60


class Watchdog:
    def __init__(self, paths, runner, alerter: Alerter, *, keep_awake=None, interval: float = 60.0,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), auto_start=None):
        self.paths = paths
        self.runner = runner
        self.alerter = alerter
        self.keep_awake = keep_awake
        # autostart.AutoStarter (or None): starts the day's run when it is due.
        self.auto_start = auto_start
        self.interval = interval
        self._now = now
        self._first = True
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # -- loop ----------------------------------------------------------------
    def start(self) -> "Watchdog":
        self._thread = threading.Thread(target=self._loop, name="coopa-watchdog", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self.keep_awake is not None:
            self.keep_awake.release()

    def poke(self) -> None:
        """Check again now instead of at the next interval."""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._wake.wait(self.interval)
            self._wake.clear()

    def tick(self) -> None:
        now = self._now()
        checks = [self._check_auto_start, self._check_keep_awake, self._check_token,
                  self._check_stale_locks, self._check_leftovers, self._check_calendar]
        if self._first:
            checks.append(self._check_incidents)
        for check in checks:
            try:
                check(now)
            except Exception:  # one failing check must not stop the others
                log.exception("watchdog check %s failed", check.__name__)
        self._first = False

    # -- checks ----------------------------------------------------------------
    def _check_calendar(self, now: datetime) -> None:
        """The bundled market calendar ends on a fixed date; after it no run can start."""
        from .calendar_svc import CalendarService
        last = CalendarService().cal.last_session.date()
        days = (last - now.date()).days
        if days <= CALENDAR_WARNING_DAYS:
            self.alerter.alert(
                "calendar", "Market calendar running out",
                f"The bundled market calendar ends {last:%b} {last.day}, {last.year} ({max(days, 0)} days). "
                f"After that no run can start. Update the app (pip install -U exchange-calendars, "
                f"or a new build) before then.", key="calendar", repeat_after=timedelta(days=1))

    def _check_auto_start(self, now: datetime) -> None:
        if self.auto_start is not None:
            self.auto_start.check(now)

    def _check_keep_awake(self, now: datetime) -> None:
        if self.keep_awake is None:
            return
        if self.runner is not None and self.runner.status == "running":
            self.keep_awake.set(True, "a run is active")
            return
        # Only real positions and orders count: simulated ones left by a stopped
        # practice run have nothing to protect.
        positions, orders = self._with_store(
            lambda s: (len(s.open_positions("LIVE")), len(s.orders_open("LIVE"))))
        if positions or orders:
            parts = []
            if positions:
                parts.append(f"{positions} real position{'s' if positions != 1 else ''} open")
            if orders:
                parts.append(f"{orders} real order{'s' if orders != 1 else ''} open")
            self.keep_awake.set(True, " and ".join(parts))
        else:
            self.keep_awake.set(False)

    def _check_token(self, now: datetime) -> None:
        from .wiring import token_status
        status = token_status(now=now, config_path=self.paths.config_path)
        key = "token"
        if status["state"] == "not_set_up":
            self.alerter.clear(key)  # nothing to renew; the dashboard explains setup
        elif status["state"] == "expired":
            self.alerter.alert("token_expiring", "Schwab login expired",
                               "Runs that use Schwab will not start. Open Coopa Trada and click "
                               "Reconnect Schwab.", key=key, repeat_after=TOKEN_ALERT_REPEAT)
        elif status["state"] == "expiring_soon":
            hours = max(0, int((status["seconds_remaining"] or 0) // 3600))
            self.alerter.alert("token_expiring", "Schwab login expires soon",
                               f"The Schwab login runs out in about {hours} hours. Open Coopa Trada "
                               f"and click Reconnect Schwab before the next run.",
                               key=key, repeat_after=TOKEN_ALERT_REPEAT)
        elif not status.get("covers_next_run", True):
            self.alerter.alert("token_expiring", "Reconnect Schwab before the next run",
                               status.get("hold_message") or "The login won't last through the next run.",
                               key=key, repeat_after=TOKEN_ALERT_REPEAT)
        else:
            self.alerter.clear(key)

    def _check_stale_locks(self, now: datetime) -> None:
        from .runlock import known_locks
        for lock in known_locks(self.paths.db_path):
            key = f"stale:{lock['path']}"
            holder = lock.get("holder") or {}
            if lock["state"] != "stale" or holder.get("mode") == "DRY-RUN":
                self.alerter.clear(key)  # dry-run leftovers clear themselves
                continue
            self.alerter.alert("crash_recovery", "A real-money run did not shut down cleanly",
                               "New runs are blocked until you decide. Check Reconcile and your "
                               "Schwab positions, then click Clear stale lock on the Dashboard.", key=key)

    def _with_store(self, fn):
        from .store import Store
        store = Store.open(self.paths.db_path)
        try:
            return fn(store)
        finally:
            store.close()

    def _check_leftovers(self, now: datetime) -> None:
        if self.runner is not None and self.runner.status == "running":
            return
        open_live = self._with_store(lambda s: s.open_positions("LIVE"))
        if not open_live:
            self.alerter.clear("leftovers")
            return
        n = len(open_live)
        self.alerter.alert("crash_recovery", "Positions open with nothing watching them",
                           f"{n} real position{'s are' if n != 1 else ' is'} open but no run is "
                           f"managing {'them' if n != 1 else 'it'}. Positions held overnight on "
                           f"purpose still need the next trading day's run started (before the "
                           f"open) so their exits are watched. Otherwise check Schwab, then start "
                           f"a run or sell there.", key="leftovers")

    def _check_incidents(self, now: datetime) -> None:
        incidents = self._with_store(lambda s: s.open_incidents())
        if incidents:
            n = len(incidents)
            self.alerter.alert("crash_recovery", "Orders still need checking",
                               f"{n} order{'s' if n != 1 else ''} from an earlier run still "
                               f"need{'' if n != 1 else 's'} you to check Schwab and answer on the "
                               f"Reconcile page. Trading in those options stays halted until then.",
                               key="incidents-at-start")
