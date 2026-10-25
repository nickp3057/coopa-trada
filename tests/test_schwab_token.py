"""Schwab login status, the start gate, and the web reconnect flow (all mocked)."""
import json
import os
import stat
from collections import namedtuple
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from coopa_trada import schwab_login
from coopa_trada.broker import DryRunBroker
from coopa_trada.calendar_svc import CalendarService
from coopa_trada.config import Mode, SchwabCredentials
from coopa_trada.contracts import SyntheticResolver
from coopa_trada.engine import Engine
from coopa_trada.exits import Quote
from coopa_trada.models import AssetClass, Right, Signal
from coopa_trada.rules import load_rules
from coopa_trada.schwab_login import LoginError, LoginRejected, begin_login, finish_login
from coopa_trada.schwab_token import TokenState, read_token_status, start_block_reason
from coopa_trada.store import Store

UTC = timezone.utc
NOW = datetime(2026, 9, 2, 19, 50, tzinfo=UTC)  # 15:50 New York
DAY = date(2026, 9, 2)
CLOSE = datetime(2026, 9, 2, 20, 0, tzinfo=UTC)


def write_token(path, created, **extra):
    """The schwab-py on-disk format, with fake secrets."""
    body = {"creation_timestamp": created.timestamp() if isinstance(created, datetime) else created,
            "token": {"access_token": "fake", "refresh_token": "fake", "expires_at": 0}}
    body.update(extra)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


# -- classification ------------------------------------------------------------
def test_missing_file_is_not_set_up(tmp_path):
    status = read_token_status(tmp_path / "none.json", NOW)
    assert status.state is TokenState.NOT_SET_UP
    assert status.label == "Not set up"
    assert "Reconnect Schwab" in status.message


@pytest.mark.parametrize("content", [
    "not json{",
    "[]",
    json.dumps({"creation_timestamp": NOW.timestamp()}),                       # no token object
    json.dumps({"creation_timestamp": NOW.timestamp(), "token": {}}),         # no refresh token
    json.dumps({"token": {"refresh_token": "fake"}}),                         # legacy: no timestamp
    json.dumps({"creation_timestamp": "yesterday", "token": {"refresh_token": "fake"}}),
    json.dumps({"creation_timestamp": True, "token": {"refresh_token": "fake"}}),
    json.dumps({"creation_timestamp": 1e20, "token": {"refresh_token": "fake"}}),
])
def test_damaged_or_unknown_expiry_is_not_set_up(tmp_path, content):
    path = tmp_path / "t.json"
    path.write_text(content, encoding="utf-8")
    assert read_token_status(path, NOW).state is TokenState.NOT_SET_UP


def test_login_time_in_the_future_is_not_trusted(tmp_path):
    path = write_token(tmp_path / "t.json", NOW + timedelta(hours=1))
    assert read_token_status(path, NOW).state is TokenState.NOT_SET_UP


@pytest.mark.parametrize("age, state", [
    (timedelta(0), TokenState.CONNECTED),
    (timedelta(days=5), TokenState.CONNECTED),                   # exactly 2 days left
    (timedelta(days=5, seconds=1), TokenState.EXPIRING_SOON),
    (timedelta(days=6, hours=23), TokenState.EXPIRING_SOON),
    (timedelta(days=7), TokenState.EXPIRED),
    (timedelta(days=30), TokenState.EXPIRED),
])
def test_states_by_age(tmp_path, age, state):
    status = read_token_status(write_token(tmp_path / "t.json", NOW - age), NOW)
    assert status.state is state
    assert status.expires_at == NOW - age + timedelta(days=7)
    assert status.seconds_remaining == pytest.approx((timedelta(days=7) - age).total_seconds())
    assert status.to_dict()["warning"] is (state is not TokenState.CONNECTED)


def test_start_gate_requires_login_through_the_close(tmp_path):
    needed = CLOSE + timedelta(minutes=30)
    fresh = read_token_status(write_token(tmp_path / "a.json", NOW), NOW)
    assert start_block_reason(fresh, needed) is None
    # Expiring soon but lasts past today's close: allowed (the banner warns).
    soon = read_token_status(write_token(tmp_path / "b.json", NOW - timedelta(days=6)), NOW)
    assert soon.state is TokenState.EXPIRING_SOON
    assert start_block_reason(soon, needed) is None
    # Still valid now, but lapses during the session: blocked.
    lapsing = read_token_status(write_token(tmp_path / "c.json", NOW - timedelta(days=7) + timedelta(minutes=5)), NOW)
    assert "before this run finishes" in start_block_reason(lapsing, needed)
    expired = read_token_status(write_token(tmp_path / "d.json", NOW - timedelta(days=8)), NOW)
    assert "Schwab is expired" in start_block_reason(expired, needed)
    missing = read_token_status(tmp_path / "none.json", NOW)
    assert "Schwab is not set up" in start_block_reason(missing, needed)


# -- engine gate -------------------------------------------------------------
def _signal():
    return Signal("test", "AMD", Right.CALL, 100, date(2026, 9, 4), 1, AssetClass.SINGLE_NAME, NOW, "test")


class SchwabBackedResolver(SyntheticResolver):
    """Stands in for any component that would call Schwab (e.g. real quotes)."""
    requires_schwab_token = True


def _engine(store, *, mode=Mode.DRY_RUN, resolver=None, token_status=None):
    rules = load_rules("config/rules.example.yaml")
    broker = DryRunBroker(store)
    broker.mode = mode
    return Engine(source=SimpleNamespace(fetch=lambda day: [_signal()]),
                  resolver=resolver or SyntheticResolver(base_price=1), broker=broker, store=store,
                  rules=rules, cal=CalendarService(), mode=mode,
                  quote_source=lambda _: Quote(1, 1.02, 1.01), now=lambda: NOW,
                  sleep=lambda _: None, token_status=token_status)


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "t.db") as s:
        yield s


def _orders(store):
    return store.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]


def test_simulation_with_fake_data_needs_no_token(store):
    _engine(store).run(DAY, once=True)
    assert _orders(store) >= 1


def test_dry_run_with_a_schwab_component_and_no_token_refuses(store, tmp_path):
    eng = _engine(store, resolver=SchwabBackedResolver(),
                  token_status=lambda now: read_token_status(tmp_path / "none.json", now))
    with pytest.raises(ValueError, match="Schwab is not set up"):
        eng.run(DAY, once=True)
    assert _orders(store) == 0


def test_schwab_component_without_any_token_check_refuses(store):
    with pytest.raises(ValueError, match="no Schwab login check"):
        _engine(store, resolver=SchwabBackedResolver()).run(DAY, once=True)
    with pytest.raises(ValueError, match="no Schwab login check"):
        _engine(store, mode=Mode.LIVE).run(DAY)
    assert _orders(store) == 0


@pytest.mark.parametrize("age, fragment", [
    (timedelta(days=8), "Schwab is expired"),
    (timedelta(days=7) - timedelta(minutes=5), "before this run finishes"),
])
def test_live_refuses_expired_or_lapsing_login_before_reconcile(store, tmp_path, age, fragment):
    path = write_token(tmp_path / "t.json", NOW - age)
    eng = _engine(store, mode=Mode.LIVE, token_status=lambda now: read_token_status(path, now))
    eng.reconcile = lambda: pytest.fail("reconcile would contact Schwab")
    with pytest.raises(ValueError, match=fragment):
        eng.run(DAY)
    assert _orders(store) == 0


def test_schwab_client_checks_token_before_loading_it(tmp_path, monkeypatch):
    from schwab import auth
    from coopa_trada.wiring import TokenMissing, schwab_client
    monkeypatch.setattr(auth, "client_from_token_file", lambda **_: pytest.fail("token file loaded"))
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(write_token(tmp_path / "t.json", datetime.now(UTC) - timedelta(days=9))))
    with pytest.raises(TokenMissing, match="expired"):
        schwab_client()
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(tmp_path / "none.json"))
    with pytest.raises(TokenMissing, match="not set up"):
        schwab_client()


# -- login flow (schwab-py mocked) ------------------------------------------------
Context = namedtuple("Context", "callback_url authorization_url state")


class FakeAuth:
    def __init__(self, reject=False):
        self.reject = reject
        self.received = None

    def get_auth_context(self, api_key, callback_url):
        return Context(callback_url, f"https://api.schwabapi.com/v1/oauth/authorize?client_id={api_key}", "s1")

    def client_from_received_url(self, api_key, app_secret, ctx, received_url, writer):
        if self.reject:
            raise RuntimeError("invalid_grant")
        self.received = (api_key, app_secret, ctx, received_url)
        writer({"creation_timestamp": int(datetime.now(UTC).timestamp()),
                "token": {"access_token": "fake", "refresh_token": "fake"}})
        return object()


def _creds(tmp_path, **overrides):
    values = dict(api_key="key", app_secret="secret", callback_url="https://127.0.0.1:8182",
                  token_path=tmp_path / "token.json")
    values.update(overrides)
    return SchwabCredentials(**values)


@pytest.fixture
def fake_auth(monkeypatch):
    auth = FakeAuth()
    monkeypatch.setattr(schwab_login, "_auth", lambda: auth)
    return auth


def test_login_needs_app_credentials(tmp_path, fake_auth):
    with pytest.raises(LoginError, match="SCHWAB_API_KEY, SCHWAB_APP_SECRET"):
        begin_login(_creds(tmp_path, api_key="", app_secret=""))


def test_login_writes_a_private_token_file(tmp_path, fake_auth):
    creds = _creds(tmp_path)
    pending = begin_login(creds)
    assert pending.authorization_url.startswith("https://api.schwabapi.com/")
    finish_login(creds, pending, "  https://127.0.0.1:8182/?code=abc&session=x  ")
    assert fake_auth.received[3] == "https://127.0.0.1:8182/?code=abc&session=x"
    assert fake_auth.received[2] is pending.auth_context
    assert stat.S_IMODE(os.stat(creds.token_path).st_mode) == 0o600
    assert read_token_status(creds.token_path).state is TokenState.CONNECTED
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".schwab-token-")]


@pytest.mark.parametrize("url, fragment", [
    ("https://evil.example/?code=abc", "not the address"),
    ("https://127.0.0.1:9999/?code=abc", "not the address"),
    ("http://127.0.0.1:8182/?code=abc", "not the address"),
    ("https://127.0.0.1:8182/?error=access_denied", "no login code"),
    ("", "not the address"),
])
def test_login_rejects_wrong_addresses_without_calling_schwab(tmp_path, fake_auth, url, fragment):
    creds = _creds(tmp_path)
    with pytest.raises(LoginError, match=fragment) as info:
        finish_login(creds, begin_login(creds), url)
    assert not isinstance(info.value, LoginRejected)
    assert fake_auth.received is None
    assert not creds.token_path.exists()


def test_login_requires_a_fresh_pending_attempt(tmp_path, fake_auth, monkeypatch):
    creds = _creds(tmp_path)
    with pytest.raises(LoginError, match="No Schwab login is in progress"):
        finish_login(creds, None, "https://127.0.0.1:8182/?code=abc")
    pending = begin_login(creds)
    monkeypatch.setattr(schwab_login.time, "monotonic", lambda: pending.started_monotonic + 601)
    with pytest.raises(LoginError, match="more than 10 minutes"):
        finish_login(creds, pending, "https://127.0.0.1:8182/?code=abc")


def test_schwab_refusal_is_plain_english_and_leaves_old_token(tmp_path, monkeypatch):
    auth = FakeAuth(reject=True)
    monkeypatch.setattr(schwab_login, "_auth", lambda: auth)
    creds = _creds(tmp_path)
    old = write_token(creds.token_path, NOW).read_text()
    with pytest.raises(LoginRejected, match="did not accept"):
        finish_login(creds, begin_login(creds), "https://127.0.0.1:8182/?code=abc")
    assert creds.token_path.read_text() == old


# -- web endpoints -------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch, fake_auth):
    pytest.importorskip("flask")
    from coopa_trada.web import create_app
    from coopa_trada.web.config import WebPaths
    from coopa_trada.web.runner import EngineRunner
    for name, value in (("SCHWAB_API_KEY", "key"), ("SCHWAB_APP_SECRET", "secret"),
                        ("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182"),
                        ("SCHWAB_TOKEN_PATH", str(tmp_path / "token.json"))):
        monkeypatch.setenv(name, value)
    cfg = tmp_path / "rules.yaml"
    cfg.write_text(open("config/rules.example.yaml").read(), encoding="utf-8")
    paths = WebPaths(config_path=str(cfg), db_path=str(tmp_path / "t.db"),
                     signals_path="data/signals.example.csv")
    app = create_app(paths, EngineRunner(paths))
    app.testing = True
    return app.test_client()


def test_token_endpoint_reports_each_state(client, tmp_path):
    body = client.get("/api/token").get_json()
    assert (body["state"], body["label"], body["warning"]) == ("not_set_up", "Not set up", True)
    write_token(tmp_path / "token.json", datetime.now(UTC) - timedelta(days=6))
    body = client.get("/api/token").get_json()
    assert (body["label"], body["warning"]) == ("Expiring soon", True)
    assert 0 < body["seconds_remaining"] < 2 * 86400
    write_token(tmp_path / "token.json", datetime.now(UTC))
    body = client.get("/api/token").get_json()
    assert (body["label"], body["warning"], body["app_configured"]) == ("Connected", False, True)


def test_reconnect_round_trip(client, tmp_path):
    begin = client.post("/api/schwab/reconnect/begin", json={}).get_json()
    assert begin["authorization_url"].startswith("https://api.schwabapi.com/")
    assert begin["callback_url"] == "https://127.0.0.1:8182"
    bad = client.post("/api/schwab/reconnect/finish", json={"redirect_url": "nope"})
    assert bad.status_code == 400
    assert client.get("/api/token").get_json()["login_in_progress"] is True  # a typo keeps the attempt
    done = client.post("/api/schwab/reconnect/finish",
                       json={"redirect_url": "https://127.0.0.1:8182/?code=abc"})
    assert done.status_code == 200
    assert done.get_json()["label"] == "Connected"
    again = client.post("/api/schwab/reconnect/finish",
                        json={"redirect_url": "https://127.0.0.1:8182/?code=abc"})
    assert "No Schwab login is in progress" in again.get_json()["error"]


def test_reconnect_refused_while_a_run_is_in_progress(client):
    from flask import current_app
    with client.application.app_context():
        current_app.config["COOPA_RUNNER"].status = "running"
    assert client.post("/api/schwab/reconnect/begin", json={}).status_code == 409
    assert client.post("/api/schwab/reconnect/finish", json={"redirect_url": "x"}).status_code == 409


def test_live_start_blocked_when_schwab_not_set_up(client, tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "1")
    rv = client.post("/api/run/start", json={"live": True, "confirm": "ENABLE LIVE TRADING"})
    assert rv.status_code == 409
    assert "Schwab is not set up" in rv.get_json()["error"]
    assert client.get("/api/run/status").get_json()["status"] == "idle"
