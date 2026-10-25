"""Confirmed pick lists: the hand-off between "a person confirmed these picks"
(dashboard or Telegram) and a run that buys them.

A confirmed list is an immutable reviewed CSV in the ``imports/`` folder next to
the database, named ``<trade date>-<UTC timestamp>-<id>.csv``. Previews never
write there; only a confirmation does. A running engine reads the folder on
every tick (:class:`PickListSource`), so confirming a list while today's run is
active makes it buy straight away (inside the buying window).

The most recently confirmed list for a trade date is that day's list: confirming
a corrected list replaces an earlier one. Picks already bought stay bought;
picks only on the replaced list are no longer bought.
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from .models import CsvSignalSource, SignalSource

__all__ = ["imports_dir", "save_confirmed_list", "latest_list", "PickListSource"]


def imports_dir(db_path) -> Path:
    return Path(db_path).resolve().parent / "imports"


def save_confirmed_list(db_path, csv_text: str, trade_date: str) -> Path:
    """Write a confirmed, already-previewed list. Returns its path."""
    day = date.fromisoformat(trade_date)
    folder = imports_dir(db_path)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    path = folder / f"{day.isoformat()}-{stamp}-{uuid4().hex[:8]}.csv"
    # Written under a temporary name and renamed, so a run never reads half a file.
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(csv_text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def latest_list(folder, day: date) -> Path | None:
    folder = Path(folder)
    if not folder.is_dir():
        return None
    files = [p for p in folder.glob(f"{day.isoformat()}-*.csv") if p.is_file()]
    return max(files, key=lambda p: (p.stat().st_mtime_ns, p.name), default=None)


class PickListSource(SignalSource):
    """Today's latest confirmed list, or ``fallback`` when none was confirmed."""

    def __init__(self, folder, fallback: SignalSource | None = None):
        self.folder = Path(folder)
        self.fallback = fallback
        self.path: Path | None = None  # the list the last fetch read

    def fetch(self, day: date | None = None):
        if day is not None:
            path = latest_list(self.folder, day)
            if path is not None:
                self.path = path
                return CsvSignalSource(path).fetch(day)
        self.path = None
        return self.fallback.fetch(day) if self.fallback is not None else []
