"""Background engine runner for the web UI.

One run at a time. The daily Start/Stop control is DRY-RUN only -- LIVE needs
the separate guarded control *and* ``LIVE_TRADING=1`` in the environment, both
enforced in :mod:`coopa_trada.web.views` and again by ``resolve_mode`` here.

Stop is cooperative: a ``threading.Event`` is consulted through the engine's
injected ``sleep`` hook, so the exit loop unwinds at the next poll boundary
(within ``poll_interval_seconds``). Open positions are left as they are -- the
runner never force-liquidates.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable

from ..calendar_svc import CalendarService
from ..config import Mode, resolve_mode
from ..wiring import build_engine, parse_trading_date
from .config import WebPaths

__all__ = ["EngineRunner", "RunnerBusy", "RunStopped"]


class RunnerBusy(RuntimeError):
    """start() called while a run is already in progress."""


class RunStopped(Exception):
    """Internal: raised through the sleep hook to unwind engine.run on stop."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EngineRunner:
    def __init__(
        self,
        paths: WebPaths,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self._paths = paths
        self._now = now  # test hook: pins the engine clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.status = "idle"  # "idle" | "running"
        self.mode = Mode.DRY_RUN.value
        self.last_started: str | None = None
        self.last_finished: str | None = None
        self.last_error: str | None = None
        self.last_result: str | None = None

    # -- introspection ------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "mode": self.mode,
            "last_started": self.last_started,
            "last_finished": self.last_finished,
            "last_error": self.last_error,
            "last_result": self.last_result,
            "signals_path": self._paths.signals_path,
            "stop_requested": self._stop.is_set(),
        }

    # -- control ----------------------------------------------------
    def start(self, *, live: bool = False, date_str: str | None = None) -> None:
        with self._lock:
            if self.status == "running":
                raise RunnerBusy("a run is already in progress")
            self._stop.clear()
            self.status = "running"
            self.last_started = _now_iso()
            self.last_finished = None
            self.last_error = None
            self.last_result = None
            self._thread = threading.Thread(
                target=self._run, args=(bool(live), date_str, replace(self._paths)), daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- worker ---------------------------------------------------
    def _sleep(self, seconds: float) -> None:
        """Engine sleep hook: wake immediately on stop and unwind the run."""
        if self._stop.wait(timeout=max(0.0, seconds)):
            raise RunStopped()

    def _run(self, live: bool, date_str: str | None, paths: WebPaths) -> None:
        store = None
        try:
            mode = resolve_mode(live)  # DRY-RUN unless live AND LIVE_TRADING=1
            self.mode = mode.value
            day = parse_trading_date(date_str)
            if not CalendarService().is_trading_day(day):
                raise RuntimeError(f"{day.isoformat()} is not an XNYS trading day")

            engine, store = build_engine(
                mode=mode,
                signals_path=paths.signals_path,
                config_path=paths.config_path,
                db_path=paths.db_path,
                sleep=self._sleep,
                now=self._now,
            )
            engine.run(day)
            self.last_result = "run complete"
        except RunStopped:
            self.last_result = "stopped by user"
        except Exception as exc:  # surfaced in the UI, not a crash
            self.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:  # pragma: no cover - best effort
                    pass
            self.status = "idle"
            self.last_finished = _now_iso()
            self._stop.clear()
