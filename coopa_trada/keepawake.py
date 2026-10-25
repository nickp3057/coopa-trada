"""Keep the Mac from sleeping while the bot has something to look after.

A sleeping Mac stops polling prices and order status, so exits wouldn't happen.
While wanted, this holds a ``caffeinate -i -s -w <this process>`` child:

* ``-i`` prevents idle system sleep (on battery too),
* ``-s`` also prevents system sleep while on AC power,
* ``-w`` ties it to this process, so it ends even if the app crashes.

The display may still turn off; that is fine. Closing the lid still sleeps the
Mac (unless an external display is attached), which no app can prevent, hence
the "plugged in, lid open" rule in COOPER_README.md.

The watchdog decides when it is wanted (see ``Watchdog._check_keep_awake``).
``_spawn`` is the only function that starts a process; the tests replace it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

from .logs import get_logger

__all__ = ["KeepAwake"]

log = get_logger("coopa_trada.keepawake", "APP")


def _spawn(pid: int) -> subprocess.Popen:
    return subprocess.Popen(["/usr/bin/caffeinate", "-i", "-s", "-w", str(pid)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class KeepAwake:
    def __init__(self, *, platform: str | None = None):
        self.platform = platform or sys.platform
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self.wanted = False
        self.reason = ""
        self.error: str | None = None

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def set(self, wanted: bool, reason: str = "") -> None:
        """Hold or release the assertion. Never raises; a failure is kept in ``error``."""
        with self._lock:
            self.wanted, self.reason = bool(wanted), reason if wanted else ""
            if wanted:
                self._hold()
            else:
                self._release()

    def release(self) -> None:
        self.set(False)

    def _hold(self) -> None:
        if self.active:
            return
        if self._proc is not None:  # caffeinate ended by itself (killed?): start another
            log.warning("caffeinate exited with %s; restarting it", self._proc.returncode)
            self._proc = None
        if self.platform.startswith("linux"):
            self.error = None  # a server does not sleep; nothing to hold
            return
        if self.platform != "darwin":
            self.error = "Keeping the computer awake is only built for macOS."
            return
        try:
            self._proc = _spawn(os.getpid())
            self.error = None
            log.info("keeping the Mac awake: %s", self.reason)
        except OSError as exc:
            self.error = f"Could not start caffeinate: {exc}"
            log.error(self.error)

    def _release(self) -> None:
        proc, self._proc = self._proc, None
        self.error = None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.info("no longer keeping the Mac awake")

    def status(self) -> dict:
        with self._lock:
            return {"active": self.active, "wanted": self.wanted, "reason": self.reason,
                    "error": self.error}
