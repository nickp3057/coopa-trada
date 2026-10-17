"""Alerts: actively tell a person when something needs them.

The dashboard only helps if someone is looking at it. Anything that needs a
human (an order whose outcome is unknown, an exit that failed or is not
filling, a crash left something to decide, the Schwab login running out, a run
refused or stopped with an error) also goes out as an alert:

* a macOS desktop notification, always (when running on a Mac), and
* optionally a push to a phone through ntfy (https://ntfy.sh), when
  ``NTFY_TOPIC`` is set in ``.env``.

Anyone who knows an ntfy topic name can read its messages, so the topic must be
long and random (see COOPER_README.md), and every message is passed through
:func:`scrub` first: no account numbers, account hashes, links or dollar
amounts ever leave this computer in an alert.

Sending never raises into trading code: a failed channel is logged and the rest
still go out. The same alert (by ``key``) is repeated at most every
``repeat_after`` so a stuck condition polled every 30 seconds does not flood the
phone. ``_osascript`` and ``_post`` are the only two functions that leave the
process; the tests replace both.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from .logs import get_logger

__all__ = ["Alert", "Alerter", "MacNotifier", "NtfyNotifier", "NotifyError", "Stopped",
           "contract_text", "from_env", "report_failure", "scrub"]

log = get_logger("coopa_trada.alerts", "ALERT")

REPEAT_AFTER = timedelta(hours=6)
NTFY_DEFAULT_SERVER = "https://ntfy.sh"
# ntfy allows up to 64 characters; short topics are guessable.
_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{24,64}$")
_MAX_MESSAGE = 900


class NotifyError(RuntimeError):
    """One channel could not deliver an alert."""


class Stopped(Exception):
    """Base for "a person stopped the run": unwinds a run without an alert."""


# -- what may leave the computer --------------------------------------------------
_SCRUBS = [
    (re.compile(r"https?://\S+"), "[link]"),
    (re.compile(r"\$\s*-?[\d,]*\d(?:\.\d+)?"), "[amount]"),
    (re.compile(r"(?i)\b(balance|cash|buying power|equity|value|cap|ask|bid|mid|price|cost|"
                r"amount|limit)(\s*[=:]\s*|\s+of\s+|\s+)-?[\d,]*\d(?:\.\d+)?"), r"\1\2[amount]"),
    (re.compile(r"\b[0-9A-Fa-f]{16,}\b"), "[id]"),
    (re.compile(r"\d{5,}"), "[number]"),
]


def scrub(text: str) -> str:
    """Remove account numbers, hashes, links and dollar amounts from alert text."""
    text = str(text)
    for pattern, replacement in _SCRUBS:
        text = pattern.sub(replacement, text)
    return text


def contract_text(osi_symbol: str) -> str:
    """``AMD 100 call, expires 2026-12-18`` from an OSI symbol, without long digit runs."""
    s = osi_symbol or ""
    m = re.fullmatch(r"(.{1,6}?)\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})", s)
    if not m:
        return s.split()[0] if s.split() else "an option"
    root, yy, mm, dd, right, strike = m.groups()
    strike_text = f"{int(strike) / 1000:g}"
    return f"{root.strip()} {strike_text} {'call' if right == 'C' else 'put'}, expires 20{yy}-{mm}-{dd}"


@dataclass(frozen=True)
class Alert:
    kind: str
    title: str
    message: str
    key: str
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {"kind": self.kind, "title": self.title, "message": self.message, "at": self.at.isoformat()}


class Notifier(Protocol):
    name: str

    def send(self, alert: Alert) -> None: ...


# -- channels --------------------------------------------------------------
def _osascript(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["/usr/bin/osascript", *args], capture_output=True, text=True, timeout=15)


def _post(url: str, body: bytes, headers: dict) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


class MacNotifier:
    """macOS Notification Center via ``osascript``. Text goes in as arguments, never as script."""

    name = "Mac notification"
    _SCRIPT = ["-e", "on run argv",
               "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"',
               "-e", "end run"]

    @staticmethod
    def available() -> bool:
        return sys.platform == "darwin"

    def send(self, alert: Alert) -> None:
        if not self.available():
            raise NotifyError("desktop notifications only work on macOS")
        result = _osascript([*self._SCRIPT, alert.title, alert.message])
        if result.returncode != 0:
            raise NotifyError(f"osascript failed: {(result.stderr or '').strip()[:200]}")


class NtfyNotifier:
    """A push to the ntfy app on a phone subscribed to ``topic``."""

    name = "Phone (ntfy)"

    def __init__(self, topic: str, server: str = NTFY_DEFAULT_SERVER):
        problem = ntfy_config_problem(topic, server)
        if problem:
            raise ValueError(problem)
        self.topic = topic
        self.server = server.rstrip("/")

    def send(self, alert: Alert) -> None:
        body = json.dumps({"topic": self.topic, "title": alert.title, "message": alert.message,
                           "priority": 4, "tags": ["warning"]}).encode("utf-8")
        try:
            status = _post(self.server, body, {"Content-Type": "application/json"})
        except OSError as exc:
            raise NotifyError(f"could not reach the ntfy server: {type(exc).__name__}") from exc
        if not 200 <= status < 300:
            raise NotifyError(f"the ntfy server answered HTTP {status}")


def ntfy_config_problem(topic: str, server: str = NTFY_DEFAULT_SERVER) -> str | None:
    if not _TOPIC_RE.match(topic or ""):
        return ("NTFY_TOPIC must be 24 to 64 letters, digits, - or _, so nobody can guess it. "
                "Generate one as described in COOPER_README.md.")
    if not (server or "").startswith("https://"):
        return "NTFY_SERVER must start with https://"
    return None


# -- dispatcher ------------------------------------------------------------
class Alerter:
    """Sends each alert to every channel, scrubbed and de-duplicated by key."""

    def __init__(self, notifiers=(), *, problems=(), repeat_after: timedelta = REPEAT_AFTER,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.notifiers = list(notifiers)
        self.problems = list(problems)  # channels that are configured wrongly, for Settings
        self.repeat_after = repeat_after
        self._now = now
        self._lock = threading.Lock()
        self._last_sent: dict[str, datetime] = {}
        self.recent: deque[dict] = deque(maxlen=30)

    def alert(self, kind: str, title: str, message: str, *, key: str | None = None,
              repeat_after: timedelta | None = None) -> bool:
        """Send unless the same key went out within ``repeat_after``. Never raises."""
        key = key or f"{kind}:{message}"
        now = self._now()
        with self._lock:
            last = self._last_sent.get(key)
            if last is not None and now - last < (repeat_after or self.repeat_after):
                return False
            self._last_sent[key] = now
        item = Alert(kind, scrub(title)[:120], scrub(message)[:_MAX_MESSAGE], key, now)
        log.warning("ALERT %s: %s %s", kind, item.title, item.message)
        results = self._deliver(item)
        self.recent.appendleft({**item.to_dict(), "channels": results})
        return True

    def clear(self, key: str) -> None:
        """Forget ``key`` so the next occurrence alerts straight away (the condition went away)."""
        with self._lock:
            self._last_sent.pop(key, None)

    def test(self) -> list[dict]:
        """A test alert on every channel, bypassing de-duplication."""
        item = Alert("test", "Coopa Trada test alert",
                     "If you can read this, alerts reach you here. Nothing needs doing.", "test",
                     self._now())
        results = self._deliver(item)
        results += [{"channel": name, "ok": False, "detail": why} for name, why in self.problems]
        if not results:
            results.append({"channel": "none", "ok": False, "detail": "No alert channels are available."})
        self.recent.appendleft({**item.to_dict(), "channels": results})
        return results

    def _deliver(self, item: Alert) -> list[dict]:
        results = []
        for notifier in self.notifiers:
            try:
                notifier.send(item)
                results.append({"channel": notifier.name, "ok": True, "detail": "sent"})
            except Exception as exc:  # one broken channel must not stop the others
                log.error("alert channel %s failed: %s", notifier.name, exc)
                results.append({"channel": notifier.name, "ok": False, "detail": scrub(str(exc))})
        return results

    def channels(self) -> list[dict]:
        out = [{"channel": n.name, "configured": True, "detail": "on"} for n in self.notifiers]
        out += [{"channel": name, "configured": False, "detail": why} for name, why in self.problems]
        return out


class LogOnlyNotifier:
    """Stands in for every channel when ``COOPA_TRADA_ALERTS_LOG_ONLY=1`` (automated checks only)."""

    name = "Log only"

    def send(self, alert: Alert) -> None:
        log.warning("log-only alert (not delivered): %s", alert.title)


LOG_ONLY_ENV = "COOPA_TRADA_ALERTS_LOG_ONLY"


def from_env(env: dict | None = None) -> Alerter:
    """Mac notifications always (on a Mac); ntfy when ``NTFY_TOPIC`` is set and valid."""
    e = os.environ if env is None else env
    if (e.get(LOG_ONLY_ENV) or "").strip() == "1":
        # For packaging/verify_app.py and tests that start the real app in a child process.
        return Alerter([LogOnlyNotifier()], problems=[
            ("All channels", f"Switched off by {LOG_ONLY_ENV}=1: alerts are only written to the log.")])
    notifiers, problems = [], []
    if MacNotifier.available():
        notifiers.append(MacNotifier())
    else:
        problems.append((MacNotifier.name, "Only available on macOS."))
    topic = (e.get("NTFY_TOPIC") or "").strip()
    server = (e.get("NTFY_SERVER") or NTFY_DEFAULT_SERVER).strip()
    if not topic:
        problems.append((NtfyNotifier.name, "Off. Set NTFY_TOPIC in .env to get alerts on your phone."))
    elif ntfy_config_problem(topic, server):
        problems.append((NtfyNotifier.name, ntfy_config_problem(topic, server)))
    else:
        notifiers.append(NtfyNotifier(topic, server))
    return Alerter(notifiers, problems=problems)


def report_failure(alerter: Alerter | None, exc: BaseException, *, refused: bool, mode: str = "") -> None:
    """Alert once for a run that was refused or stopped with an error."""
    if alerter is None or isinstance(exc, Stopped) or getattr(exc, "_coopa_alerted", False):
        return
    practice = "Practice run: " if mode == "DRY-RUN" else ""
    if refused:
        alerter.alert("run_refused", "Coopa Trada: run refused",
                      f"{practice}The run did not start trading. {exc}", key=f"refused:{exc}")
    else:
        alerter.alert("error", "Coopa Trada stopped with an error",
                      f"{practice}{type(exc).__name__}: {exc} Open the Dashboard and check Schwab.",
                      key=f"error:{exc}")
    try:
        exc._coopa_alerted = True  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - builtin exceptions accept attributes
        pass
