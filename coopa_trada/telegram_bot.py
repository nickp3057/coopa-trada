"""Telegram intake: Cooper forwards the pick screenshot to the bot, the bot reads
it and answers with the parsed preview and Confirm / Reject buttons.

Flow (Telegram Bot API, long polling, so no public web address is needed):

1. A photo (or an image sent as a file) arrives from a chat listed in
   ``TELEGRAM_ALLOWED_CHAT_IDS``. Messages from any other chat are ignored
   (their chat ID is logged locally, which is how Nick finds Cooper's ID).
2. The picture goes through the configured reader (Claude, or Apple on a Mac),
   then the same strict preview as the dashboard. The list's dates come from
   its title ("FOMC Picks for 9/2, 9/4 Expiration"), and the trade date must be
   today; anything else is refused with a plain answer.
3. The bot replies with every pick, the full dates and the buttons. Nothing is
   saved or bought yet. The preview can also be confirmed or rejected on the
   dashboard. Previews expire after an hour.
4. Confirm saves the list exactly like a dashboard confirmation; a run in
   progress buys it on its next tick (inside the buying window).

``_api`` and ``_download`` are the only functions that leave the process; the
tests replace both. The bot token is part of every request URL, so it is never
logged and exception text from the HTTP layer is never shown.
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import clock
from .logs import get_logger

__all__ = ["TelegramIntake", "TelegramPoller", "Pending", "from_env", "parse_allowed_ids"]

log = get_logger("coopa_trada.telegram", "TELEGRAM")
API = "https://api.telegram.org"
PREVIEW_LIFETIME = timedelta(hours=1)
_MAX_PHOTO_BYTES = 3_700_000
_IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg"}
_TITLE_RE = re.compile(r"for\s+(\d{1,2})/(\d{1,2})\s*,\s*(\d{1,2})/(\d{1,2})\s+expiration", re.IGNORECASE)
_MD_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})\s*$")


class TelegramError(RuntimeError):
    """A Telegram request failed. The message never contains the token."""


def _api(token: str, method: str, payload: dict, timeout: float = 35.0) -> dict:
    import httpx
    try:
        resp = httpx.post(f"{API}/bot{token}/{method}", json=payload, timeout=timeout)
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 - never echo the URL (it holds the token)
        raise TelegramError(f"{method}: {type(exc).__name__}") from None
    if not body.get("ok"):
        raise TelegramError(f"{method}: {body.get('description', 'not ok')}"[:200])
    return body.get("result")


def _download(token: str, file_path: str) -> bytes:
    import httpx
    try:
        resp = httpx.get(f"{API}/file/bot{token}/{file_path}", timeout=60.0)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise TelegramError(f"download: {type(exc).__name__}") from None
    if len(resp.content) > _MAX_PHOTO_BYTES:
        raise TelegramError("download: picture too large")
    return resp.content


def parse_allowed_ids(raw: str | None) -> frozenset[int]:
    ids = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if re.fullmatch(r"-?\d{1,20}", part):
            ids.add(int(part))
    return frozenset(ids)


@dataclass
class Pending:
    id: str
    chat_id: int
    message_id: int | None
    csv: str
    trade_date: str
    expiration: str
    rows: list
    created_at: datetime
    state: str = "pending"      # pending | confirmed | rejected | expired
    result: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"id": self.id, "trade_date": self.trade_date, "expiration": self.expiration,
                "rows": self.rows, "created_at": self.created_at.isoformat(), "state": self.state}


def _fmt_day(d: date) -> str:
    return f"{d:%a %b} {d.day}, {d.year}"


def _list_dates(read, today: date) -> tuple[date, date]:
    """The list's trade date and expiration, from the reader's answer or the title."""
    from .models import _resolve_year
    if read.trade_date and read.expiration:
        a, b = _MD_RE.match(read.trade_date), _MD_RE.match(read.expiration)
        if a and b:
            parts = [int(g) for g in (*a.groups(), *b.groups())]
        else:
            parts = None
    else:
        parts = None
    if parts is None:
        m = next((_TITLE_RE.search(line) for line in [read.title or "", *read.csv.splitlines()[:3]]
                  if _TITLE_RE.search(line)), None)
        if m is None:
            raise ValueError("I could not read the two dates in the title (\"FOMC Picks for M/D, M/D "
                             "Expiration\"). Nothing was saved. Use the dashboard to enter the dates.")
        parts = [int(g) for g in m.groups()]
    try:
        trade = _resolve_year(parts[0], parts[1], today)
        expiration = _resolve_year(parts[2], parts[3], today)
    except ValueError:
        raise ValueError("The dates in the title are not real dates. Nothing was saved.") from None
    if expiration < trade:
        expiration = expiration.replace(year=expiration.year + 1)
    return trade, expiration


class TelegramIntake:
    def __init__(self, token: str, allowed_chat_ids, *,
                 read_image: Callable | None = None,
                 confirm: Callable[[str, str], dict] | None = None,
                 run_status: Callable[[], dict] | None = None,
                 now: Callable[[], datetime] = clock.now_utc):
        self.token = token
        self.allowed = frozenset(allowed_chat_ids)
        self.read_image = read_image
        self.confirm = confirm
        self.run_status = run_status or (lambda: {})
        self.now = now
        self.offset: int | None = None
        self.pending: dict[str, Pending] = {}
        self.ignored_chats: set[int] = set()
        self._lock = threading.Lock()

    # -- Telegram --------------------------------------------------------
    def poll_once(self, timeout: int = 25) -> int:
        payload = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if self.offset is not None:
            payload["offset"] = self.offset
        updates = _api(self.token, "getUpdates", payload, timeout=timeout + 10) or []
        for update in updates:
            self.offset = max(self.offset or 0, int(update["update_id"]) + 1)
            try:
                self.handle(update)
            except Exception as exc:  # noqa: BLE001 - one bad update must not stop the bot
                log.error("telegram update failed: %s", type(exc).__name__)
        return len(updates)

    def send(self, chat_id: int, text: str, buttons_for: str | None = None) -> dict | None:
        payload = {"chat_id": chat_id, "text": text[:4000]}
        if buttons_for:
            payload["reply_markup"] = {"inline_keyboard": [[
                {"text": "✅ Confirm", "callback_data": f"ok:{buttons_for}"},
                {"text": "❌ Reject", "callback_data": f"no:{buttons_for}"},
            ]]}
        return _api(self.token, "sendMessage", payload)

    # -- updates -----------------------------------------------------------
    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            return self._handle_button(update["callback_query"])
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None:
            return
        if chat_id not in self.allowed:
            if chat_id not in self.ignored_chats:
                self.ignored_chats.add(chat_id)
                log.warning("ignored a Telegram message from chat %s (not in TELEGRAM_ALLOWED_CHAT_IDS)", chat_id)
            return
        image = self._image_of(message)
        if image is None:
            self.send(chat_id, self._help())
            return
        file_id, suffix = image
        self.send(chat_id, "Reading the picks…")
        try:
            preview = self._read(file_id, suffix)
        except ValueError as exc:
            self.send(chat_id, f"Could not use that picture: {exc}")
            return
        except TelegramError as exc:
            log.error("telegram download failed: %s", exc)
            self.send(chat_id, "Could not download that picture from Telegram. Please send it again.")
            return
        pid = secrets.token_urlsafe(9)
        item = Pending(pid, chat_id, None, preview["csv"], preview["date"], preview["expiration"],
                       preview["rows"], self.now())
        with self._lock:
            self._expire()
            self.pending[pid] = item
        sent = self.send(chat_id, self._preview_text(item), buttons_for=pid)
        item.message_id = (sent or {}).get("message_id")

    def _image_of(self, message: dict):
        photos = message.get("photo") or []
        if photos:
            fitting = [p for p in photos if (p.get("file_size") or 0) <= _MAX_PHOTO_BYTES] or photos[:1]
            best = max(fitting, key=lambda p: (p.get("width", 0) * p.get("height", 0), p.get("file_size") or 0))
            return best["file_id"], ".jpg"
        doc = message.get("document") or {}
        if doc.get("mime_type") in _IMAGE_TYPES:
            return doc["file_id"], _IMAGE_TYPES[doc["mime_type"]]
        return None

    def _read(self, file_id: str, suffix: str) -> dict:
        from .imports import preview_csv, read_image
        info = _api(self.token, "getFile", {"file_id": file_id})
        data = _download(self.token, info["file_path"])
        with tempfile.TemporaryDirectory(prefix="coopa-telegram-") as tmp:
            path = Path(tmp) / ("picks" + suffix)
            path.write_bytes(data)
            read = (self.read_image or read_image)(path)
        today = clock.to_market(self.now()).date()
        trade, expiration = _list_dates(read, today)
        if trade != today:
            raise ValueError(f"this list is for {_fmt_day(trade)}, not today ({_fmt_day(today)}). "
                             f"Nothing was saved. Use the dashboard for another date.")
        try:
            return preview_csv(read.csv, trade.isoformat(), expiration.isoformat())
        except ValueError as exc:
            raise ValueError(f"{exc}\n\nWhat I read:\n{read.csv}\nFix it on the dashboard "
                             f"(paste the text, correct it, preview, confirm).") from None

    def _preview_text(self, item: Pending) -> str:
        trade, exp = date.fromisoformat(item.trade_date), date.fromisoformat(item.expiration)
        lines = [f"Picks for {_fmt_day(trade)}, expiring {_fmt_day(exp)} ({len(item.rows)}):"]
        for r in item.rows:
            lines.append(f"• {r['symbol']} {r['option']}  last {r['last_price']:g}  "
                         f"TVT {_num(r['tvt_delta'])}  Schwab {_num(r['schwab_delta'])}")
        lines.append("")
        lines.append("Check every row against the screenshot. Last prices are only references; "
                     "orders use live prices. Nothing is bought until you press Confirm "
                     "(here or on the dashboard). This preview expires in 1 hour.")
        return "\n".join(lines)

    def _handle_button(self, query: dict) -> None:
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id not in self.allowed:
            return  # not even an answer: unknown chats get nothing back
        action, _, pid = str(query.get("data", "")).partition(":")
        outcome = self.resolve(pid, confirm=action == "ok", chat_id=chat_id, via="Telegram")
        _api(self.token, "answerCallbackQuery", {"callback_query_id": query["id"], "text": outcome["short"]})

    # -- shared with the dashboard -------------------------------------------
    def resolve(self, pid: str, *, confirm: bool, chat_id: int | None = None, via: str = "dashboard") -> dict:
        """Confirm or reject a preview, from a Telegram button or the dashboard."""
        with self._lock:
            self._expire()
            item = self.pending.get(pid)
            if item is None or (chat_id is not None and item.chat_id != chat_id):
                return {"ok": False, "short": "Preview not found",
                        "message": "That preview is no longer available. Send the screenshot again."}
            if item.state != "pending":
                return {"ok": False, "short": f"Already {item.state}",
                        "message": f"That preview was already {item.state}."}
            if confirm and date.fromisoformat(item.trade_date) != clock.to_market(self.now()).date():
                item.state = "expired"
                return self._finish(item, False, "This list is not for today any more. Nothing was saved.")
            item.state = "confirmed" if confirm else "rejected"
        if not confirm:
            return self._finish(item, True, f"Rejected ({via}). Nothing was saved or bought.")
        try:
            item.result = self.confirm(item.csv, item.trade_date)
        except Exception as exc:  # noqa: BLE001
            item.state = "pending"
            log.error("telegram confirm failed: %s", type(exc).__name__)
            return {"ok": False, "short": "Could not save", "message": "Could not save the picks. Try again."}
        when = clock.fmt_display(self.now(), with_day=False)
        return self._finish(item, True, f"Confirmed at {when} ({via}). {item.result.get('message', '')}")

    def _finish(self, item: Pending, ok: bool, text: str) -> dict:
        if item.message_id is not None:
            try:  # replace the buttons with the outcome
                _api(self.token, "editMessageText", {"chat_id": item.chat_id, "message_id": item.message_id,
                                                     "text": (self._preview_text(item) + "\n\n" + text)[:4000]})
            except TelegramError as exc:
                log.error("telegram edit failed: %s", exc)
        return {"ok": ok, "short": text.split(".")[0][:180], "message": text}

    def _expire(self) -> None:
        now = self.now()
        for key, item in list(self.pending.items()):
            if now - item.created_at > PREVIEW_LIFETIME:
                if item.state == "pending":
                    item.state = "expired"
                if now - item.created_at > PREVIEW_LIFETIME * 24:
                    del self.pending[key]

    def pending_previews(self) -> list[dict]:
        with self._lock:
            self._expire()
            return [p.public() for p in self.pending.values() if p.state == "pending"]

    def _help(self) -> str:
        status = self.run_status() or {}
        running = status.get("status") == "running"
        return ("Send me a screenshot of today's pick list and I will read it and show you every "
                "pick to confirm. " + ("A run is active " + f"({status.get('mode', '')})." if running
                                       else "No run is active right now; start one on the dashboard."))


def _num(value) -> str:
    return "—" if value is None else f"{value:g}"


class TelegramPoller:
    """Background thread: long-polls Telegram and hands each update to the intake."""

    def __init__(self, intake: TelegramIntake, *, sleep: Callable[[float], bool] | None = None):
        self.intake = intake
        self._stop = threading.Event()
        self._sleep = sleep or (lambda s: self._stop.wait(s))
        self._thread: threading.Thread | None = None

    def start(self) -> "TelegramPoller":
        self._thread = threading.Thread(target=self._loop, name="telegram", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                self.intake.poll_once()
                backoff = 5.0
            except Exception as exc:  # noqa: BLE001 - keep polling; Telegram outages happen
                log.error("telegram polling failed: %s", exc if isinstance(exc, TelegramError) else type(exc).__name__)
                if self._sleep(backoff):
                    return
                backoff = min(backoff * 2, 300.0)


def from_env(env=None, **kwargs) -> TelegramIntake | None:
    """The intake when TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS are set, else None."""
    e = os.environ if env is None else env
    token = (e.get("TELEGRAM_BOT_TOKEN") or "").strip()
    allowed = parse_allowed_ids(e.get("TELEGRAM_ALLOWED_CHAT_IDS"))
    if not token or not allowed:
        return None
    return TelegramIntake(token, allowed, **kwargs)
