"""Settings → Schwab app: each bot saves its own Schwab details; the live checklist."""
import os

import pytest

pytest.importorskip("flask")

from coopa_trada.web import create_app
from coopa_trada.web.config import WebPaths

KEY, SECRET = "AbCdEfGh12345678AbCdEfGh12345678", "SeCrEt1234567890"


@pytest.fixture
def client(tmp_path, monkeypatch):
    env = tmp_path / "nick" / ".env"
    env.parent.mkdir()
    env.write_text("# my bot\nLIVE_TRADING=0\nSCHWAB_API_KEY=\nNTFY_TOPIC=keep-me\n")
    monkeypatch.setenv("COOPA_TRADA_ENV_FILE", str(env))
    for name in ("SCHWAB_API_KEY", "SCHWAB_APP_SECRET", "SCHWAB_ACCOUNT_ID", "SCHWAB_CALLBACK_URL"):
        monkeypatch.delenv(name, raising=False)
    app = create_app(WebPaths(config_path="config/rules.example.yaml", db_path=str(tmp_path / "t.db")))
    return app.test_client(), env


def test_saving_writes_this_bots_file_and_never_echoes_the_secret(client):
    c, env = client
    assert c.get("/api/schwab/keys").get_json()["api_key_set"] is False
    rv = c.post("/api/schwab/keys", json={"api_key": KEY, "app_secret": SECRET, "account_id": "1234-5678",
                                          "callback_url": "https://127.0.0.1:8182"})
    body = rv.get_json()
    assert rv.status_code == 200 and body["api_key_set"] and body["secret_set"]
    assert SECRET not in rv.get_data(as_text=True) and KEY not in rv.get_data(as_text=True)
    assert body["api_key_hint"].endswith("5678") and "Reconnect Schwab" in body["message"]
    text = env.read_text()
    assert f"SCHWAB_API_KEY={KEY}" in text and f"SCHWAB_APP_SECRET={SECRET}" in text
    assert "SCHWAB_ACCOUNT_ID=12345678" in text
    assert "NTFY_TOPIC=keep-me" in text and "# my bot" in text and text.count("SCHWAB_API_KEY=") == 1
    assert (env.stat().st_mode & 0o777) == 0o600
    assert os.environ["SCHWAB_API_KEY"] == KEY  # in effect without a restart


def test_empty_boxes_keep_what_is_saved(client):
    c, env = client
    c.post("/api/schwab/keys", json={"api_key": KEY, "app_secret": SECRET})
    c.post("/api/schwab/keys", json={"api_key": "", "app_secret": "", "account_id": "87654321"})
    text = env.read_text()
    assert f"SCHWAB_APP_SECRET={SECRET}" in text and "SCHWAB_ACCOUNT_ID=87654321" in text


@pytest.mark.parametrize("payload, fragment", [
    ({"api_key": "has space in it 123"}, "App Key"),
    ({"app_secret": "bad\nLIVE_TRADING=1"}, "Secret"),
    ({"callback_url": "http://127.0.0.1:8182"}, "https"),
    ({"account_id": "abc"}, "digits"),
    ({}, "Nothing to save"),
])
def test_bad_values_are_refused_and_nothing_is_written(client, payload, fragment):
    c, env = client
    before = env.read_text()
    rv = c.post("/api/schwab/keys", json=payload)
    assert rv.status_code == 400 and fragment in rv.get_json()["error"]
    assert env.read_text() == before


def test_live_trading_cannot_be_switched_on_from_the_page(client):
    c, env = client
    c.post("/api/schwab/keys", json={"api_key": KEY, "LIVE_TRADING": "1"})
    assert "LIVE_TRADING=0" in env.read_text() and "LIVE_TRADING=1" not in env.read_text()


def test_live_checklist_says_what_is_missing(client):
    c, _ = client
    checks = {x["label"]: x for x in c.get("/api/run/status").get_json()["live_checks"]}
    switch = checks["Real trading is switched on for this bot (LIVE_TRADING=1)"]
    assert switch["ok"] is False and "LIVE_TRADING=1" in switch["fix"]
    assert checks["Your Schwab app key and secret are saved"]["ok"] is False
    assert checks["No run is going right now"]["ok"] is True
    c.post("/api/schwab/keys", json={"api_key": KEY, "app_secret": SECRET})
    checks = {x["label"]: x for x in c.get("/api/run/status").get_json()["live_checks"]}
    assert checks["Your Schwab app key and secret are saved"]["ok"] is True
