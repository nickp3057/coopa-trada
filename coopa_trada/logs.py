"""Logging setup and the startup mode banner.

Every log line is tagged with the run mode so a captured log can never be
mistaken for the other mode. ``banner`` is deliberately loud and multi-line.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path

from .config import Mode

__all__ = ["setup_logging", "add_file_log", "logs_dir", "log_days", "read_log", "banner", "get_logger"]

# Daily log files are kept this long, then deleted.
KEEP_LOG_DAYS = 365

_CONFIGURED = False


def setup_logging(level: int | str = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        logging.getLogger().setLevel(level)
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s"))
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    _CONFIGURED = True


def logs_dir(db_path) -> Path:
    """``logs/`` next to the database (one bot's data folder)."""
    return Path(db_path).resolve().parent / "logs"


class DailyFileHandler(logging.Handler):
    """Every log line, in ``<folder>/<YYYY-MM-DD>.log`` by the Mountain-time date.

    One file per day keeps "what happened on Tuesday" one click away (the
    Activity page). Files older than ``KEEP_LOG_DAYS`` are deleted.
    """

    def __init__(self, folder: Path):
        super().__init__()
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._day = None
        self._stream = None
        self.setFormatter(_MountainFormatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "werkzeug" and record.levelno < logging.WARNING:
            return  # one line per page request; POSTs are logged by coopa_trada.web.actions
        try:
            from . import clock
            day = datetime.fromtimestamp(record.created, clock.DISPLAY_TZ).date()
            msg = self.format(record)
            with self.lock:
                if day != self._day:
                    if self._stream:
                        self._stream.close()
                    self._stream = open(self.folder / f"{day.isoformat()}.log", "a", encoding="utf-8")
                    self._day = day
                    self._prune(day)
                self._stream.write(msg + "\n")
                self._stream.flush()
        except Exception:  # noqa: BLE001 - logging must never break the app
            self.handleError(record)

    def _prune(self, today) -> None:
        for path in self.folder.glob("*.log"):
            try:
                if (today - date.fromisoformat(path.stem)).days > KEEP_LOG_DAYS:
                    path.unlink()
            except ValueError:
                continue

    def close(self) -> None:
        with self.lock:
            if self._stream:
                self._stream.close()
                self._stream = None
        super().close()


class _MountainFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        from . import clock
        return datetime.fromtimestamp(record.created, clock.DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S MT")


def add_file_log(db_path) -> Path:
    """Also write every log line to the bot's daily log files. Returns the folder."""
    folder = logs_dir(db_path)
    root = logging.getLogger()
    if not any(isinstance(h, DailyFileHandler) and h.folder == folder for h in root.handlers):
        root.addHandler(DailyFileHandler(folder))
    return folder


def log_days(db_path) -> list[str]:
    """Dates that have a log file, newest first."""
    folder = logs_dir(db_path)
    days = []
    for path in folder.glob("*.log"):
        try:
            days.append(date.fromisoformat(path.stem).isoformat())
        except ValueError:
            continue
    return sorted(days, reverse=True)


_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) MT (\w+)\s+(\S+) \| (.*)$")


def read_log(db_path, day: str, *, level: str = "", text: str = "", after: int = 0, limit: int = 5000) -> dict:
    """One day's log lines, parsed. ``after``: skip that many lines (for live tailing)."""
    date.fromisoformat(day)  # refuses anything that is not a date (no paths)
    path = logs_dir(db_path) / f"{day}.log"
    if not path.is_file():
        return {"day": day, "lines": [], "total": 0}
    order = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
    floor = order.get(level.upper(), 0)
    needle = text.lower()
    raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    rows = []
    current = None
    for line in raw:
        m = _LINE.match(line)
        if m:
            current = {"at": m.group(1)[11:], "level": m.group(2), "source": m.group(3), "message": m.group(4)}
            rows.append(current)
        elif current is not None:  # a traceback or other continuation line
            current["message"] += "\n" + line
    total = len(rows)
    rows = rows[after:]
    rows = [r for r in rows if order.get(r["level"], 1) >= floor
            and (not needle or needle in (r["message"] + " " + r["source"]).lower())]
    return {"day": day, "lines": rows[-limit:], "total": total}


def banner(mode: Mode) -> str:
    """A multi-line startup banner. Return it *and* expect callers to print it."""
    bar = "=" * 66
    lines = [
        "",
        bar,
        "   ####   COOPA TRADA   ####".ljust(66),
        f"   MODE = {mode.value}".ljust(66),
        bar,
    ]
    if mode is Mode.DRY_RUN:
        lines += [
            "   No orders will be sent to Schwab.",
            "   Fills are simulated: buys at the ask, sells at the bid.",
        ]
    else:
        lines += [
            "   *** LIVE TRADING ENABLED ***",
            "   Real orders will be placed against a real account.",
            "   (Live was asked for and LIVE_TRADING=1 is set.)",
        ]
    lines += [bar, ""]
    return "\n".join(lines)


class _ModeAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"[{self.extra['mode']}] {msg}", kwargs


def get_logger(name: str, mode: Mode | str) -> logging.LoggerAdapter:
    """A logger whose every record is prefixed with ``[DRY-RUN]`` / ``[LIVE]``."""
    label = mode.value if isinstance(mode, Mode) else str(mode)
    return _ModeAdapter(logging.getLogger(name), {"mode": label})
