"""Running on a server: dashboard password, network binding, calendar warning."""
import base64
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("flask")

from coopa_trada.alerts import Alerter
from coopa_trada.web import create_app
from coopa_trada.web.config import WebPaths


class Notifier:
    name = "fake"

    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


def _client(tmp_path):
    return create_app(WebPaths(config_path="config/rules.example.yaml", db_path=str(tmp_path / "t.db"))).test_client()


def _basic(password, user="cooper"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_without_a_password_the_local_dashboard_is_open(tmp_path):
    assert _client(tmp_path).get("/api/mode").status_code == 200


def test_with_a_password_every_page_needs_it(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "correct horse battery")
    client = _client(tmp_path)
    for url in ("/", "/settings", "/api/rules", "/static/app.js"):
        rv = client.get(url)
        assert rv.status_code == 401 and "Basic" in rv.headers["WWW-Authenticate"]
    assert client.get("/api/mode", headers=_basic("wrong")).status_code == 401
    assert client.post("/api/run/start", json={}, headers=_basic("wrong")).status_code == 401
    assert client.get("/api/mode", headers=_basic("correct horse battery")).status_code == 200


def test_listening_on_a_network_address_needs_a_long_password(monkeypatch):
    from coopa_trada.web.__main__ import main
    with pytest.raises(SystemExit):
        main(["--host", "0.0.0.0"])
    monkeypatch.setenv("DASHBOARD_PASSWORD", "short")
    with pytest.raises(SystemExit):
        main(["--host", "100.64.1.2"])


def test_calendar_warning_two_months_before_it_ends(tmp_path):
    from coopa_trada.calendar_svc import CalendarService
    from coopa_trada.watchdog import Watchdog
    last = CalendarService().cal.last_session
    paths = SimpleNamespace(config_path="config/rules.example.yaml", db_path=str(tmp_path / "w.db"))
    runner = SimpleNamespace(status="idle")
    notifier = Notifier()
    early = datetime(last.year - 1, 1, 5, tzinfo=timezone.utc)
    Watchdog(paths, runner, Alerter([notifier]), now=lambda: early)._check_calendar(early)
    assert notifier.sent == []
    late = datetime(last.year, last.month, 1, tzinfo=timezone.utc)
    Watchdog(paths, runner, Alerter([notifier]), now=lambda: late)._check_calendar(late)
    assert [a.kind for a in notifier.sent] == ["calendar"]


def test_a_local_proxy_may_forward_the_browser_host_but_others_may_not(tmp_path):
    client = _client(tmp_path)
    headers = {"Origin": "https://coopa.tailnet.ts.net", "X-Forwarded-Host": "coopa.tailnet.ts.net"}
    ok = client.post("/api/run/stop", json={}, headers=headers, environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert ok.status_code == 202
    far = client.post("/api/run/stop", json={}, headers=headers, environ_base={"REMOTE_ADDR": "203.0.113.9"})
    assert far.status_code == 403
    assert client.post("/api/run/stop", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
