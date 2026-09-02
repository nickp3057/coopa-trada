"""Logging setup and the startup mode banner.

Every log line is tagged with the run mode so a captured log can never be
mistaken for the other mode. ``banner`` is deliberately loud and multi-line.
"""

from __future__ import annotations

import logging
import sys

from .config import Mode

__all__ = ["setup_logging", "banner", "get_logger"]

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
            "   (--live AND LIVE_TRADING=1 are both set.)",
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
