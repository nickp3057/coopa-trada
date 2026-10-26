"""The "Real money" switch on the Dashboard.

Real orders need two things:

* ``LIVE_TRADING=1`` in this bot's ``.env`` on the server: "this bot may ever
  trade real money". Set once, at the server; the website never sets it.
* This switch, kept next to the bot's database: the everyday on/off. Turning it
  on takes the typed confirmation phrase; turning it off is one click. While it
  is on, the day's automatic run and the Start button trade for real.

The switch starts off, and stays off if its file is missing or unreadable.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .config import _truthy

__all__ = ["switch_path", "is_on", "set_switch", "live_allowed", "armed"]


def switch_path(db_path) -> Path:
    return Path(db_path).expanduser().resolve().parent / "live_switch.json"


def is_on(db_path) -> bool:
    try:
        return json.loads(switch_path(db_path).read_text(encoding="utf-8")).get("on") is True
    except (OSError, ValueError, AttributeError):
        return False


def set_switch(db_path, on: bool) -> None:
    path = switch_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".live_switch.", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"on": bool(on), "changed_at": datetime.now(timezone.utc).isoformat()}, handle)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def live_allowed(env=None) -> bool:
    """LIVE_TRADING=1 on the server: this bot may trade real money at all."""
    return _truthy((os.environ if env is None else env).get("LIVE_TRADING"))


def armed(db_path, env=None) -> bool:
    """Runs started now trade real money: allowed on the server and switched on."""
    return live_allowed(env) and is_on(db_path)
