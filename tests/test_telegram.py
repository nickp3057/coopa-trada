"""Task 7: Telegram intake (Bot API mocked; nothing leaves the test)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coopa_trada import telegram_bot as tg
from coopa_trada.vision import ReadResult

COOPER, STRANGER = 111222333, 999
NOW = datetime(2026, 9, 2, 19, 45, tzinfo=timezone.utc)  # Wed Sep 2, 1:45 PM MT
TABLE = ("FOMC Picks for 9/2, 9/4 Expiration\nSYMBOL,OPTION,LAST PRICE,TVT-DELTA,SCHWAB DELTA\n"
         "IBIT,44P,0.71,66.4,-0.554\nSNOW,322.5C,18.4,79.8,0.507\n")


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.updates = []
        self.next_message = 500

    def api(self, token, method, payload, timeout=35.0):
        assert token == "TOKEN"
        self.calls.append((method, payload))
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return out
        if method == "getFile":
            return {"file_path": "photos/file_1.jpg"}
        if method == "sendMessage":
            self.next_message += 1
            return {"message_id": self.next_message}
        return True

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]


@pytest.fixture
def fake(monkeypatch):
    f = FakeTelegram()
    monkeypatch.setattr(tg, "_api", f.api)
    monkeypatch.setattr(tg, "_download", lambda token, path: b"\xff\xd8jpeg")
    return f


@pytest.fixture
def app(tmp_path, monkeypatch, fake):
    pytest.importorskip("flask")
    from coopa_trada.web import create_app
    from coopa_trada.web.config import WebPaths
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", f"{COOPER}, 444")
    app = create_app(WebPaths(config_path="config/rules.example.yaml", db_path=str(tmp_path / "t.db")))
    bot = app.config["COOPA_TELEGRAM"]
    bot.now = lambda: NOW
    bot.read_image = lambda path: ReadResult(TABLE, "FOMC Picks for 9/2, 9/4 Expiration", "9/2", "9/4")
    return app


def photo(chat, update_id=1):
    return {"update_id": update_id, "message": {"message_id": 7, "chat": {"id": chat},
            "photo": [{"file_id": "small", "width": 90, "height": 60, "file_size": 1000},
                      {"file_id": "big", "width": 1280, "height": 860, "file_size": 90000}]}}


def press(chat, data, message_id=501):
    return {"update_id": 9, "callback_query": {"id": "cb1", "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat}}}}


def imports(app):
    folder = Path(app.config["COOPA_PATHS"].db_path).parent / "imports"
    return sorted(folder.glob("*.csv")) if folder.exists() else []


def test_screenshot_gets_a_preview_with_buttons_and_nothing_is_saved(app, fake):
    bot = app.config["COOPA_TELEGRAM"]
    fake.updates = [photo(COOPER)]
    assert bot.poll_once() == 1
    assert bot.offset == 2
    assert ("getFile", {"file_id": "big"}) in fake.calls          # the largest photo
    reading, preview = fake.sent()
    assert reading["text"] == "Reading the picks…"
    assert "Picks for Wed Sep 2, 2026, expiring Fri Sep 4, 2026 (2):" in preview["text"]
    assert "• IBIT 44P  last 0.71  TVT 66.4  Schwab -0.554" in preview["text"]
    buttons = preview["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["✅ Confirm", "❌ Reject"]
    assert imports(app) == []                                     # reading never confirms


def test_confirm_button_saves_the_list_like_the_dashboard(app, fake):
    bot = app.config["COOPA_TELEGRAM"]
    fake.updates = [photo(COOPER)]
    bot.poll_once()
    pid = fake.sent()[1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    fake.updates = [press(COOPER, pid)]
    bot.poll_once()
    (saved,) = imports(app)
    assert saved.name.startswith("2026-09-02-") and saved.read_text().startswith("Trade date: 2026-09-02;")
    edit = fake.sent("editMessageText")[0]
    assert "Confirmed at 1:45 PM MT (Telegram)" in edit["text"] and "No run is active" in edit["text"]
    assert "reply_markup" not in edit                                # buttons removed
    assert fake.sent("answerCallbackQuery")[0]["callback_query_id"] == "cb1"
    fake.updates = [press(COOPER, pid)]                              # a second press does nothing
    bot.poll_once()
    assert len(imports(app)) == 1 and fake.sent("answerCallbackQuery")[1]["text"] == "Already confirmed"


def test_reject_saves_nothing(app, fake):
    bot = app.config["COOPA_TELEGRAM"]
    fake.updates = [photo(COOPER)]
    bot.poll_once()
    pid = fake.sent()[1]["reply_markup"]["inline_keyboard"][0][1]["callback_data"]
    assert pid.startswith("no:")
    fake.updates = [press(COOPER, pid)]
    bot.poll_once()
    assert imports(app) == [] and "Rejected (Telegram)" in fake.sent("editMessageText")[0]["text"]


def test_other_chats_are_ignored_and_cannot_press_buttons(app, fake):
    bot = app.config["COOPA_TELEGRAM"]
    fake.updates = [photo(STRANGER)]
    bot.poll_once()
    assert fake.sent() == [] and not any(m == "getFile" for m, _ in fake.calls)
    assert bot.ignored_chats == {STRANGER}
    fake.updates = [photo(COOPER)]
    bot.poll_once()
    pid = fake.sent()[1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    fake.updates = [press(STRANGER, pid)]
    bot.poll_once()
    assert imports(app) == [] and fake.sent("answerCallbackQuery") == []
    # Another allowed chat cannot confirm Cooper's preview either.
    fake.updates = [press(444, pid)]
    bot.poll_once()
    assert imports(app) == [] and fake.sent("answerCallbackQuery")[0]["text"] == "Preview not found"


def test_dashboard_confirm_works_and_updates_the_telegram_message(app, fake):
    bot = app.config["COOPA_TELEGRAM"]
    fake.updates = [photo(COOPER)]
    bot.poll_once()
    client = app.test_client()
    pending = client.get("/api/telegram").get_json()["pending"]
    assert len(pending) == 1 and pending[0]["trade_date"] == "2026-09-02"
    rv = client.post(f"/api/telegram/{pending[0]['id']}/confirm")
    assert rv.status_code == 200 and "(dashboard)" in rv.get_json()["message"]
    assert len(imports(app)) == 1 and "(dashboard)" in fake.sent("editMessageText")[0]["text"]
    assert client.get("/api/telegram").get_json()["pending"] == []
    assert client.post(f"/api/telegram/{pending[0]['id']}/confirm").status_code == 409


@pytest.mark.parametrize("read, fragment", [
    (ReadResult(TABLE.replace("9/2,", "9/1,"), "FOMC Picks for 9/1, 9/4 Expiration", "9/1", "9/4"),
     "this list is for Tue Sep 1, 2026, not today (Wed Sep 2, 2026)"),
    (ReadResult(TABLE.split("\n", 1)[1], None, None, None), "could not read the two dates"),
    (ReadResult(TABLE.replace("IBIT", "ZZZZ"), None, "9/2", "9/4"), "unknown ticker"),
])
def test_unusable_pictures_get_a_plain_answer_and_nothing_is_saved(app, fake, read, fragment):
    bot = app.config["COOPA_TELEGRAM"]
    bot.read_image = lambda path: read
    fake.updates = [photo(COOPER)]
    bot.poll_once()
    answer = fake.sent()[-1]["text"]
    assert answer.startswith("Could not use that picture:") and fragment in answer
    assert "reply_markup" not in fake.sent()[-1] and imports(app) == []


def test_previews_expire_after_an_hour(app, fake):
    bot = app.config["COOPA_TELEGRAM"]
    fake.updates = [photo(COOPER)]
    bot.poll_once()
    pid = fake.sent()[1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.now = lambda: NOW + timedelta(minutes=61)
    fake.updates = [press(COOPER, pid)]
    bot.poll_once()
    assert imports(app) == [] and fake.sent("answerCallbackQuery")[0]["text"] == "Already expired"


def test_text_messages_get_help_and_images_sent_as_files_work(app, fake):
    bot = app.config["COOPA_TELEGRAM"]
    fake.updates = [{"update_id": 3, "message": {"chat": {"id": COOPER}, "text": "/start"}}]
    bot.poll_once()
    assert "Send me a screenshot" in fake.sent()[-1]["text"]
    fake.updates = [{"update_id": 4, "message": {"chat": {"id": COOPER},
                     "document": {"file_id": "doc", "mime_type": "image/png"}}}]
    bot.poll_once()
    assert ("getFile", {"file_id": "doc"}) in fake.calls and "reply_markup" in fake.sent()[-1]


def test_one_bad_update_does_not_stop_the_rest(app, fake):
    bot = app.config["COOPA_TELEGRAM"]
    fake.updates = [{"update_id": 5, "message": {"chat": {"id": COOPER}, "photo": [{}]}}, photo(COOPER, 6)]
    assert bot.poll_once() == 2 and bot.offset == 7
    assert "reply_markup" in fake.sent()[-1]


def test_configuration_needs_both_settings():
    assert tg.from_env({"TELEGRAM_BOT_TOKEN": "x"}) is None
    assert tg.from_env({"TELEGRAM_ALLOWED_CHAT_IDS": "1"}) is None
    assert tg.parse_allowed_ids(" 123, -100456 ;abc, ") == {123, -100456}
    assert tg.from_env({"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_CHAT_IDS": "1,2"}).allowed == {1, 2}


def test_dashboard_says_when_telegram_is_off(tmp_path):
    pytest.importorskip("flask")
    from coopa_trada.web import create_app
    from coopa_trada.web.config import WebPaths
    client = create_app(WebPaths(db_path=str(tmp_path / "t.db"))).test_client()
    assert client.get("/api/telegram").get_json()["configured"] is False


def test_errors_never_contain_the_token(monkeypatch):
    import httpx

    def boom(url, **kwargs):
        raise httpx.ConnectError(f"cannot reach {url}")
    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(tg.TelegramError) as info:
        tg._api("123456:SECRET-TOKEN", "getUpdates", {})
    assert "SECRET" not in str(info.value) and "123456" not in str(info.value)


def test_poller_keeps_going_after_errors():
    calls = []

    class Intake:
        def poll_once(self):
            calls.append(1)
            raise tg.TelegramError("getUpdates: Bad Gateway")

    waits = []
    poller = tg.TelegramPoller(Intake(), sleep=lambda s: waits.append(s) or len(waits) >= 3)
    poller._loop()
    assert len(calls) == 3 and waits == [5.0, 10.0, 20.0]
