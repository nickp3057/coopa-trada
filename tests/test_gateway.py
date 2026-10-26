"""The login website: each person reaches only their own bot."""
import base64
import hashlib
import hmac
import struct

import httpx
import pytest

pytest.importorskip("flask")

from coopa_trada import gateway as gw

T = 1_800_000_000.0  # a fixed clock


class Bots:
    """Stands in for the per-person bots on 127.0.0.1."""

    def __init__(self):
        self.calls = []

    def request(self, method, url, headers=None, content=None):
        self.calls.append((method, url, headers, content))
        if url.startswith("http://127.0.0.1:8779"):
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"bot": url.split("/")[2]},
                              headers={"Set-Cookie": "x=1", "X-Thing": "kept"})


@pytest.fixture
def setup(tmp_path):
    config = gw.GatewayConfig(tmp_path / "gateway.yaml", "k" * 64)
    for name, port in (("cooper", 8771), ("nick", 8772)):
        config.users[name] = gw.User(name, gw.hash_password(f"{name}-long-password"), gw.new_totp_secret(),
                                     f"http://127.0.0.1:{port}")
    config.save()
    clock = [T]
    bots = Bots()
    app = gw.create_gateway(gw.GatewayConfig.load(config.path), clock=lambda: clock[0], http_client=bots)
    return app.test_client(), config, clock, bots


def code(config, name, at=T):
    return gw.totp(config.users[name].totp_secret, int(at // 30))


def login(client, config, name, password=None, code_=None):
    return client.post("/login", data={"username": name, "password": password or f"{name}-long-password",
                                       "code": code_ or code(config, name)})


def test_totp_matches_the_rfc_6238_test_vector():
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert gw.totp(secret, 59 // 30) == "287082"          # RFC 6238, T = 59 s (last 6 digits)
    assert gw.totp(secret, 1111111109 // 30) == "081804"


def test_logged_out_visitors_see_only_the_login_page(setup):
    client, *_ = setup
    rv = client.get("/settings")
    assert rv.status_code == 303 and rv.headers["Location"].startswith("/login?next=/settings")
    assert client.get("/api/rules").status_code == 401
    page = client.get("/login")
    assert b"6-digit code" in page.data and page.headers["X-Frame-Options"] == "DENY"


def test_each_person_reaches_only_their_own_bot(setup):
    client, config, _, bots = setup
    assert login(client, config, "cooper").status_code == 303
    assert client.get("/api/rules").get_json() == {"bot": "127.0.0.1:8771"}
    method, url, headers, _ = bots.calls[-1]
    assert headers["X-Coopa-User"] == "cooper" and "Cookie" not in headers
    other = setup[0].application.test_client()
    login(other, config, "nick")
    assert other.get("/api/rules").get_json() == {"bot": "127.0.0.1:8772"}


def test_a_spoofed_user_header_is_replaced(setup):
    client, config, _, bots = setup
    login(client, config, "cooper")
    client.get("/", headers={"X-Coopa-User": "nick", "X-Forwarded-Host": "evil.example"})
    _, _, headers, _ = bots.calls[-1]
    assert headers["X-Coopa-User"] == "cooper" and headers["X-Forwarded-Host"] != "evil.example"


def test_requests_bodies_and_query_strings_are_forwarded(setup):
    client, config, _, bots = setup
    login(client, config, "cooper")
    rv = client.post("/api/rules?x=1", json={"total_budget": 300})
    method, url, _, content = bots.calls[-1]
    assert (method, url) == ("POST", "http://127.0.0.1:8771/api/rules?x=1") and b"300" in content
    assert rv.headers["X-Thing"] == "kept" and "x=1" not in rv.headers.get("Set-Cookie", "")


@pytest.mark.parametrize("password, code_", [("nope-nope-nope", None), (None, "000000")])
def test_wrong_password_or_code_is_refused(setup, password, code_):
    client, config, *_ = setup
    rv = login(client, config, "cooper", password, code_)
    assert rv.status_code == 401 and b"Wrong name, password or code" in rv.data
    assert client.get("/api/rules").status_code == 401


def test_a_code_works_only_once_and_old_codes_expire(setup):
    client, config, clock, _ = setup
    c = code(config, "cooper")
    assert login(client, config, "cooper", code_=c).status_code == 303
    again = setup[0].application.test_client()
    assert login(again, config, "cooper", code_=c).status_code == 401      # replayed
    clock[0] = T + 90
    assert login(again, config, "cooper", code_=code(config, "cooper", T + 60)).status_code == 303
    assert login(again, config, "cooper", code_=code(config, "cooper", T - 60)).status_code == 401


def test_five_wrong_tries_lock_the_name_for_15_minutes(setup):
    client, config, clock, _ = setup
    for _ in range(5):
        login(client, config, "cooper", code_="111111")
    rv = login(client, config, "cooper")
    assert rv.status_code == 429 and b"15 minutes" in rv.data
    clock[0] = T + 15 * 60 + 1
    assert login(client, config, "cooper", code_=code(config, "cooper", clock[0])).status_code == 303


def test_sessions_expire_and_a_password_reset_logs_everyone_out(setup, tmp_path):
    client, config, clock, _ = setup
    login(client, config, "cooper")
    assert client.get("/api/rules").status_code == 200
    stored = gw.GatewayConfig.load(config.path)
    stored.users["cooper"].session_version += 1
    app = gw.create_gateway(stored, clock=lambda: clock[0], http_client=Bots())
    cookie = client.get_cookie(gw.COOKIE)
    fresh = app.test_client()
    fresh.set_cookie(gw.COOKIE, cookie.value)
    assert fresh.get("/api/rules").status_code == 401


def test_cookie_is_httponly_strict_and_secure_behind_https(setup):
    client, config, *_ = setup
    rv = client.post("/login", headers={"X-Forwarded-Proto": "https"},
                     data={"username": "cooper", "password": "cooper-long-password", "code": code(config, "cooper")})
    cookie = rv.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" in cookie
    assert rv.headers["Strict-Transport-Security"]


def test_next_cannot_send_people_to_another_site(setup):
    client, config, *_ = setup
    rv = client.post("/login", data={"username": "cooper", "password": "cooper-long-password",
                                     "code": code(config, "cooper"), "next": "//evil.example/x"})
    assert rv.headers["Location"] == "/"


def test_a_stopped_bot_gets_a_plain_message(setup):
    client, config, *_ = setup
    config.users["cooper"].backend = "http://127.0.0.1:8779"
    app = gw.create_gateway(config, clock=lambda: T, http_client=Bots())
    c = app.test_client()
    login(c, config, "cooper")
    rv = c.get("/")
    assert rv.status_code == 502 and b"systemctl restart coopa-trada@cooper" in rv.data


def test_users_file_keeps_no_passwords_and_is_private(setup):
    _, config, *_ = setup
    text = config.path.read_text()
    assert "cooper-long-password" not in text and "scrypt" in text
    assert (config.path.stat().st_mode & 0o777) == 0o600


def test_the_bot_shows_who_is_logged_in_only_from_the_local_gateway(tmp_path):
    from coopa_trada.web import create_app
    from coopa_trada.web.config import WebPaths
    bot = create_app(WebPaths(config_path="config/rules.example.yaml", db_path=str(tmp_path / "t.db"))).test_client()
    local = bot.get("/", headers={"X-Coopa-User": "cooper"}, environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert b"cooper &middot; Log out" in local.data
    far = bot.get("/", headers={"X-Coopa-User": "cooper"}, environ_base={"REMOTE_ADDR": "203.0.113.5"})
    assert b"Log out" not in far.data


def test_the_bots_stylesheets_and_scripts_are_forwarded(setup):
    client, config, _, bots = setup
    login(client, config, "cooper")
    for path in ("/static/app.css", "/static/dashboard.js"):
        assert client.get(path).status_code == 200
        assert bots.calls[-1][1] == "http://127.0.0.1:8771" + path


# -- sign-up links ----------------------------------------------------------------------
import time

def invite(config_path, name="cooper", port=8771, hours=24, site="https://coopa.example"):
    import io
    from contextlib import redirect_stdout
    out = io.StringIO()
    with redirect_stdout(out):
        gw.main(["--config", str(config_path), "invite", name, "--port", str(port), "--hours", str(hours),
                 "--site", site])
    link = next(line.strip() for line in out.getvalue().splitlines() if "/setup/" in line)
    return link.split("/setup/")[1]


@pytest.fixture
def fresh(tmp_path):
    """A gateway with only Nick; Cooper gets a sign-up link."""
    config = gw.GatewayConfig(tmp_path / "gateway.yaml", "k" * 64)
    config.users["nick"] = gw.User("nick", gw.hash_password("nick-long-password"), gw.new_totp_secret(),
                                   "http://127.0.0.1:8772")
    config.save()
    bots = Bots()
    clock = [time.time()]  # the invite command stamps links with the real time
    app = gw.create_gateway(gw.GatewayConfig.load(config.path), clock=lambda: clock[0], http_client=bots)
    return app.test_client(), config.path, clock, bots


def sign_up(client, token, secret, *, password="cooper-long-password", password2=None, code_=None):
    return client.post(f"/setup/{token}", data={
        "password": password, "password2": password2 or password,
        "code": code_ or gw.totp(secret, int(time.time() // 30))})


def test_an_invite_link_creates_the_login_and_logs_them_in(fresh):
    client, path, clock, bots = fresh
    token = invite(path)  # made at the server after the website started: no restart needed
    stored = gw.GatewayConfig.load(path)
    assert token not in path.read_text()                       # only a hash is kept
    page = client.get(f"/setup/{token}")
    assert page.status_code == 200 and b"Welcome, cooper" in page.data and b"<svg" in page.data
    assert page.headers["Cache-Control"] == "no-store"
    rv = sign_up(client, token, stored.invites["cooper"].totp_secret)
    assert rv.status_code == 303 and rv.headers["Location"] == "/"
    client.get("/api/rules")
    assert bots.calls[-1][1] == "http://127.0.0.1:8771/api/rules"   # Cooper's own bot
    after = gw.GatewayConfig.load(path)
    assert "cooper" in after.users and not after.invites
    assert "cooper-long-password" not in path.read_text()


def test_the_link_works_once_and_expires(fresh):
    client, path, clock, _ = fresh
    token = invite(path, hours=1)
    secret = gw.GatewayConfig.load(path).invites["cooper"].totp_secret
    assert sign_up(client, token, secret).status_code == 303
    assert client.get(f"/setup/{token}").status_code == 404
    other = invite(path, name="sam", port=8773, hours=1)
    clock[0] = time.time() + 3601
    assert client.get(f"/setup/{other}").status_code == 404
    assert client.get("/setup/made-up-token").status_code == 404


@pytest.mark.parametrize("kwargs, fragment", [
    ({"password": "short"}, b"at least 12"),
    ({"password2": "something-else-entirely"}, b"match."),
    ({"code_": "000000"}, b"That code doesn"),
])
def test_bad_sign_up_answers_are_refused_and_nothing_is_created(fresh, kwargs, fragment):
    client, path, *_ = fresh
    token = invite(path)
    secret = gw.GatewayConfig.load(path).invites["cooper"].totp_secret
    rv = sign_up(client, token, secret, **kwargs)
    assert rv.status_code == 400 and fragment in rv.data
    assert "cooper" not in gw.GatewayConfig.load(path).users


def test_an_existing_login_cannot_be_invited_over(fresh):
    _, path, *_ = fresh
    with pytest.raises(SystemExit, match="already has a login"):
        invite(path, name="nick", port=8772)


def test_sign_up_guessing_is_throttled(fresh):
    client, path, *_ = fresh
    token = invite(path)
    secret = gw.GatewayConfig.load(path).invites["cooper"].totp_secret
    for _ in range(5):
        sign_up(client, token, secret, code_="111111")
    assert sign_up(client, token, secret).status_code == 429


# -- 6-digit codes are optional per person ----------------------------------------------
def test_signing_up_without_codes_makes_a_password_only_login(fresh):
    client, path, *_ = fresh
    token = invite(path)
    assert b"optional" in client.get(f"/setup/{token}").data
    rv = client.post(f"/setup/{token}", data={"password": "cooper-long-password",
                                              "password2": "cooper-long-password", "code": ""})
    assert rv.status_code == 303
    assert gw.GatewayConfig.load(path).users["cooper"].totp_secret == ""
    other = client.application.test_client()
    assert other.post("/login", data={"username": "cooper", "password": "cooper-long-password",
                                      "code": ""}).status_code == 303
    assert other.post("/login", data={"username": "cooper", "password": "wrong-long-password",
                                      "code": ""}).status_code == 401


def test_a_person_with_codes_still_needs_one(fresh):
    client, *_ = fresh
    rv = client.post("/login", data={"username": "nick", "password": "nick-long-password", "code": ""})
    assert rv.status_code == 401 and client.get("/api/rules").status_code == 401


def test_codes_can_be_left_off_or_removed_at_the_server(tmp_path, monkeypatch):
    path = tmp_path / "gateway.yaml"
    monkeypatch.setattr(gw, "_ask_password", lambda name: f"{name}-long-password")
    monkeypatch.setattr(gw, "_enroll_2fa", lambda user: None)
    gw.main(["--config", str(path), "add-user", "cooper", "--port", "8771", "--no-2fa"])
    gw.main(["--config", str(path), "add-user", "nick", "--port", "8772"])
    users = gw.GatewayConfig.load(path).users
    assert users["cooper"].totp_secret == "" and users["nick"].totp_secret
    gw.main(["--config", str(path), "remove-2fa", "nick"])
    nick = gw.GatewayConfig.load(path).users["nick"]
    assert nick.totp_secret == "" and nick.session_version == 2
