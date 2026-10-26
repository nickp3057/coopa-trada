"""The login website in front of everyone's bot.

One server can run a separate copy of Coopa Trada per person (its own Schwab
login, settings, trade log and Telegram bot; see docs/SERVER_SETUP.md). This
small web app is the only thing the internet reaches (through Caddy, which adds
HTTPS). It asks for a user name, a password and (for anyone who set them up)
a 6-digit code from an authenticator app, then forwards that person, and only that person, to their
own copy. Cooper logging in reaches Cooper's bot; Nick logging in reaches
Nick's. Nothing in one copy can reach the other.

    python -m coopa_trada.gateway add-user cooper --port 8771   (at the server)
    python -m coopa_trada.gateway invite cooper --port 8771     (a sign-up link)
    python -m coopa_trada.gateway serve

An invite is a one-time sign-up link (valid 24 hours by default) for a name
and bot chosen at the server: the person opens it, picks a password and can
set up the 6-digit codes in the browser (optional). Only a hash of the link is stored.

Security: passwords are stored as salted hashes (never the password); the
6-digit codes are standard TOTP (RFC 6238), each usable once (a person
without codes, totp_secret "", logs in with the password alone); 5 wrong tries for
a name or from one address lock it for 15 minutes; the session cookie is
signed, HttpOnly, SameSite=Strict, Secure behind HTTPS, and lasts 12 hours.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import html
import os
import secrets
import struct
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

import yaml

__all__ = ["GatewayConfig", "create_gateway", "totp", "verify_totp", "main"]

DEFAULT_CONFIG = Path("~/coopa-data/gateway.yaml").expanduser()
COOKIE = "coopa_session"
SESSION_SECONDS = 12 * 3600
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
MIN_PASSWORD = 12
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade", "host", "content-length", "content-encoding", "cookie",
        "authorization"}


# -- TOTP (RFC 6238: SHA-1, 30-second steps, 6 digits) ---------------------------------
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def totp(secret: str, step: int) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def verify_totp(secret: str, code: str, now: float, *, after_step: int = -1) -> int | None:
    """The time step ``code`` belongs to (now, or 30 s either side), or None.
    Steps at or before ``after_step`` are refused, so a code works only once."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != 6:
        return None
    current = int(now // 30)
    for step in (current - 1, current, current + 1):
        if step > after_step and hmac.compare_digest(totp(secret, step), code):
            return step
    return None


def otpauth_uri(name: str, secret: str) -> str:
    label = quote(f"Coopa Trada:{name}")
    return f"otpauth://totp/{label}?secret={secret}&issuer=Coopa%20Trada&digits=6&period=30"


# -- users file ---------------------------------------------------------------
@dataclass
class User:
    name: str
    password_hash: str
    totp_secret: str      # "" = this person chose no 6-digit codes
    backend: str
    session_version: int = 1


@dataclass
class Invite:
    name: str
    backend: str
    token_hash: str       # sha256 of the link's secret part; the link itself is never stored
    expires: float        # unix time
    totp_secret: str      # shown on the sign-up page so the person can scan it


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class GatewayConfig:
    path: Path
    secret_key: str
    users: dict[str, User] = field(default_factory=dict)
    invites: dict[str, Invite] = field(default_factory=dict)

    def invite_for(self, token: str, now: float) -> Invite | None:
        wanted = _token_hash(token or "")
        for invite in self.invites.values():
            if hmac.compare_digest(invite.token_hash, wanted) and invite.expires > now:
                return invite
        return None

    @classmethod
    def load(cls, path=DEFAULT_CONFIG, *, create: bool = False) -> "GatewayConfig":
        path = Path(path).expanduser()
        if not path.exists():
            if not create:
                raise SystemExit(f"No gateway users file at {path}. Add a user first: "
                                 f"python -m coopa_trada.gateway add-user NAME --port PORT")
            return cls(path, secrets.token_hex(32))
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        users = {name: User(name, u["password_hash"], u["totp_secret"], u["backend"],
                            int(u.get("session_version", 1)))
                 for name, u in (raw.get("users") or {}).items()}
        invites = {name: Invite(name, i["backend"], i["token_hash"], float(i["expires"]), i["totp_secret"])
                   for name, i in (raw.get("invites") or {}).items()}
        return cls(path, raw["secret_key"], users, invites)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {"secret_key": self.secret_key,
                "users": {u.name: {"password_hash": u.password_hash, "totp_secret": u.totp_secret,
                                   "backend": u.backend, "session_version": u.session_version}
                          for u in self.users.values()},
                "invites": {i.name: {"backend": i.backend, "token_hash": i.token_hash, "expires": i.expires,
                                     "totp_secret": i.totp_secret} for i in self.invites.values()}}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".yaml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("# Coopa Trada login users. Holds password hashes and 2FA secrets:\n"
                             "# keep it private (mode 600). Edit with python -m coopa_trada.gateway.\n")
                yaml.safe_dump(body, handle, sort_keys=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def hash_password(password: str) -> str:
    from werkzeug.security import generate_password_hash
    return generate_password_hash(password, method="scrypt")


def check_password(stored: str, password: str) -> bool:
    from werkzeug.security import check_password_hash
    return check_password_hash(stored, password)


# -- login throttling -----------------------------------------------------------
class Throttle:
    def __init__(self, clock=time.time):
        self.clock = clock
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def locked(self, *keys: str) -> bool:
        now = self.clock()
        with self._lock:
            return any(len([t for t in self._fails.get(k, []) if now - t < LOCK_SECONDS]) >= MAX_FAILURES
                       for k in keys)

    def fail(self, *keys: str) -> None:
        now = self.clock()
        with self._lock:
            for k in keys:
                self._fails[k] = [t for t in self._fails.get(k, []) if now - t < LOCK_SECONDS] + [now]

    def clear(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._fails.pop(k, None)


# -- the website -----------------------------------------------------------------
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Coopa Trada · Log in</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--text:#1d232b;--muted:#5d6875;--line:#d5dae1;--accent:#1f5fbf;--bad:#b3261e}}
@media (prefers-color-scheme: dark){{:root{{--bg:#14171b;--card:#1d2127;--text:#e7eaee;--muted:#9aa4b0;--line:#323942;--accent:#6aa2f7;--bad:#f2b8b5}}}}
body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,sans-serif;display:flex;
min-height:100vh;align-items:center;justify-content:center;padding:16px;box-sizing:border-box}}
form{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:24px;width:100%;max-width:340px}}
h1{{font-size:20px;margin:0 0 16px}} label{{display:block;margin:12px 0 4px;color:var(--muted);font-size:14px}}
input{{width:100%;box-sizing:border-box;padding:10px;border:1px solid var(--line);border-radius:6px;
background:var(--bg);color:var(--text);font-size:16px}}
button{{margin-top:18px;width:100%;padding:11px;border:0;border-radius:6px;background:var(--accent);color:#fff;font-size:16px}}
p.err{{color:var(--bad);margin:12px 0 0}}
</style></head><body>
<form method="post" action="/login">
<h1>Coopa Trada</h1>
<label for="u">Name</label><input id="u" name="username" autocomplete="username" autocapitalize="none" required value="{username}">
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<label for="c">6-digit code <span style="font-size:12px">(leave empty if you didn't set up codes)</span></label>
<input id="c" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9 ]{{6,7}}">
<input type="hidden" name="next" value="{next}">
<button type="submit">Log in</button>{error}
</form></body></html>"""


def _mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


_SETUP_PAGE = _PAGE.split("<form")[0].replace("Log in</title>", "Sign up</title>") + """
<form method="post" action="/setup/{token}" autocomplete="off">
<h1>Welcome, {name}</h1>
<p style="color:var(--muted);font-size:14px">Set up your Coopa Trada login. This link works once.</p>
<label for="p">Choose a password (at least {min} characters)</label>
<input id="p" name="password" type="password" autocomplete="new-password" required minlength="{min}">
<label for="p2">Type it again</label>
<input id="p2" name="password2" type="password" autocomplete="new-password" required minlength="{min}">
<details style="margin-top:14px"><summary style="cursor:pointer">Also ask for a 6-digit code from my phone
(optional, safer)</summary>
<label>Install an authenticator app on your phone (Google Authenticator, Microsoft Authenticator,
1Password&hellip;), tap +, and scan this:</label>
<div style="background:#fff;padding:8px;border-radius:6px;width:max-content;margin:6px 0">{qr}</div>
<p style="font-size:13px;color:var(--muted);word-break:break-all">On this phone? Tap
<a href="{uri}">add to authenticator</a>, or choose "enter a setup key" and type: <b>{key}</b></p>
<label for="c">Then type the 6-digit code the app shows</label>
<input id="c" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9 ]{{6,7}}">
</details>
<button type="submit">Create my login</button>{error}
</form></body></html>"""


def create_gateway(config: GatewayConfig, *, clock=time.time, http_client=None):
    import httpx
    from flask import Flask, make_response, redirect, request
    from itsdangerous import BadSignature, URLSafeTimedSerializer

    # No static folder of its own: /static/... must reach the person's bot (its CSS and JS).
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 9 * 1024 * 1024
    signer = URLSafeTimedSerializer(config.secret_key, salt="coopa-gateway-session")
    throttle = Throttle(clock)
    state = {"config": config, "mtime": _mtime(config.path)}
    save_lock = threading.Lock()

    def cfg() -> GatewayConfig:
        """The users file, re-read when it changes (an invite or reset made at the
        server takes effect without a restart)."""
        mtime = _mtime(config.path)
        if mtime is not None and mtime != state["mtime"]:
            try:
                state["config"] = GatewayConfig.load(config.path)
                state["mtime"] = mtime
            except Exception:  # noqa: BLE001 - half-written file: keep the last good copy
                pass
        return state["config"]
    last_step: dict[str, int] = {}
    client = http_client or httpx.Client(timeout=httpx.Timeout(150.0, connect=5.0), follow_redirects=False)

    def https() -> bool:
        return request.is_secure or request.headers.get("X-Forwarded-Proto", "") == "https"

    def current_user() -> User | None:
        token = request.cookies.get(COOKIE)
        if not token:
            return None
        try:
            data = signer.loads(token, max_age=SESSION_SECONDS)
        except (BadSignature, TypeError):
            return None
        user = cfg().users.get(data.get("u"))
        if user is None or data.get("v") != user.session_version:
            return None
        return user

    def page(username="", error="", nxt="/", status=200):
        body = _PAGE.format(username=html.escape(username), next=html.escape(nxt),
                            error=f'<p class="err" role="alert">{html.escape(error)}</p>' if error else "")
        return make_response(body, status)

    def safe_next(value: str) -> str:
        parts = urlsplit(value or "/")
        return value if value.startswith("/") and not value.startswith("//") and not parts.netloc else "/"

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        if https():
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    @app.get("/login")
    def login_form():
        return page(nxt=safe_next(request.args.get("next", "/")))

    @app.post("/login")
    def login():
        name = (request.form.get("username") or "").strip().lower()
        # The last address is the one Caddy (the closest proxy) saw; earlier ones can be made up.
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[-1].strip()
        nxt = safe_next(request.form.get("next", "/"))
        keys = (f"user:{name}", f"ip:{ip}")
        if throttle.locked(*keys):
            return page(name, "Too many wrong tries. Wait 15 minutes, then try again.", nxt, 429)
        user = cfg().users.get(name)
        password_ok = user is not None and check_password(user.password_hash, request.form.get("password", ""))
        step = None
        if password_ok and user.totp_secret:
            step = verify_totp(user.totp_secret, request.form.get("code", ""), clock(),
                               after_step=last_step.get(name, -1))
        if not password_ok or (user.totp_secret and step is None):
            throttle.fail(*keys)
            return page(name, "Wrong name, password or code.", nxt, 401)
        if step is not None:
            last_step[name] = step
        throttle.clear(*keys)
        return start_session(redirect(nxt, 303), user)

    def start_session(resp, user: User):
        resp.set_cookie(COOKIE, signer.dumps({"u": user.name, "v": user.session_version}),
                        max_age=SESSION_SECONDS, httponly=True, samesite="Strict", secure=https(), path="/")
        return resp

    def setup_page(invite: Invite, token: str, error: str = "", status: int = 200):
        uri = otpauth_uri(invite.name, invite.totp_secret)
        try:
            import segno
            qr = segno.make(uri, error="m").svg_inline(scale=5, dark="#000", light="#fff")
        except ImportError:
            qr = ""
        body = _SETUP_PAGE.format(name=html.escape(invite.name), qr=qr, key=html.escape(invite.totp_secret),
                                  uri=html.escape(uri), token=html.escape(token), min=MIN_PASSWORD,
                                  error=f'<p class="err" role="alert">{html.escape(error)}</p>' if error else "")
        resp = make_response(body, status)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/setup/<token>")
    def setup_form(token):
        invite = cfg().invite_for(token, clock())
        if invite is None:
            return make_response("This sign-up link is used up or has expired. Ask Nick for a new one.", 404)
        return setup_page(invite, token)

    @app.post("/setup/<token>")
    def setup(token):
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[-1].strip()
        if throttle.locked(f"ip:{ip}"):
            return make_response("Too many wrong tries. Wait 15 minutes, then try again.", 429)
        invite = cfg().invite_for(token, clock())
        if invite is None:
            return make_response("This sign-up link is used up or has expired. Ask Nick for a new one.", 404)
        password = request.form.get("password", "")
        if len(password) < MIN_PASSWORD:
            return setup_page(invite, token, f"Choose a password of at least {MIN_PASSWORD} characters.", 400)
        if password != request.form.get("password2", ""):
            return setup_page(invite, token, "The two passwords don't match.", 400)
        typed = request.form.get("code", "").strip()
        step = verify_totp(invite.totp_secret, typed, clock()) if typed else None
        if typed and step is None:
            throttle.fail(f"ip:{ip}")
            return setup_page(invite, token, "That code doesn't match. Check the app shows Coopa Trada, "
                                             "and that the phone's clock is set automatically.", 400)
        with save_lock:
            current = GatewayConfig.load(config.path)
            if current.invite_for(token, clock()) is None or invite.name in current.users:
                return make_response("This sign-up link is used up. Log in instead.", 409)
            # No code typed: they chose no 6-digit codes (they can be added later with reset-2fa).
            user = User(invite.name, hash_password(password), invite.totp_secret if typed else "", invite.backend)
            current.users[user.name] = user
            del current.invites[invite.name]
            current.save()
        if step is not None:
            last_step[user.name] = step
        cfg()
        return start_session(redirect("/", 303), user)

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        resp = redirect("/login", 303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    @app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def forward(path):
        user = current_user()
        if user is None:
            if request.path.startswith("/api/"):
                return {"error": "You are logged out. Reload the page to log in again."}, 401
            return redirect("/login?next=" + quote(request.full_path.rstrip("?")), 303)
        target = user.backend.rstrip("/") + request.full_path.rstrip("?")
        fwd = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
        fwd.update({"X-Forwarded-Host": request.host, "X-Forwarded-Proto": "https" if https() else "http",
                    "X-Forwarded-For": request.remote_addr or "", "X-Coopa-User": user.name})
        try:
            upstream = client.request(request.method, target, headers=fwd, content=request.get_data())
        except httpx.HTTPError:
            return make_response(f"Your bot ({html.escape(user.name)}) is not answering. On the server: "
                                 f"sudo systemctl restart coopa-trada@{html.escape(user.name)}", 502)
        resp = make_response(upstream.content, upstream.status_code)
        for key, value in upstream.headers.multi_items():
            if key.lower() == "content-type":
                resp.headers["Content-Type"] = value  # replace Flask's default, never two
            elif key.lower() not in _HOP and key.lower() != "set-cookie":
                resp.headers.add(key, value)
        return resp

    return app


# -- command line ------------------------------------------------------------------
def _saved_site(config_path: Path) -> str | None:
    """The website address setup-https.sh saved next to the users file."""
    try:
        return (Path(config_path).parent / "site").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _ask_password(name: str) -> str:
    while True:
        first = getpass.getpass(f"New password for {name} (at least {MIN_PASSWORD} characters): ")
        if len(first) < MIN_PASSWORD:
            print("Too short.")
            continue
        if getpass.getpass("Type it again: ") != first:
            print("They don't match.")
            continue
        return first


def _enroll_2fa(user: User) -> None:
    uri = otpauth_uri(user.name, user.totp_secret)
    print(f"\n{user.name}: open an authenticator app (Google Authenticator, Microsoft "
          f"Authenticator, 1Password...), tap +, and scan this code:\n")
    try:
        import segno
        segno.make(uri, error="m").terminal(compact=True)
    except ImportError:
        pass
    print(f"\nOr choose 'enter a setup key' and type: {user.totp_secret}\n")
    while True:
        code = input("Type the 6-digit code the app shows now (or press Enter to skip the check): ").strip()
        if not code or verify_totp(user.totp_secret, code, time.time()) is not None:
            return
        print("That code doesn't match. Check the phone's clock is automatic, and try the next code.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="coopa_trada.gateway", description="Coopa Trada login website")
    parser.add_argument("--config", default=os.environ.get("COOPA_GATEWAY_CONFIG", str(DEFAULT_CONFIG)))
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the login website")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8780)
    add = sub.add_parser("add-user", help="add a person and the port of their bot")
    add.add_argument("name")
    add.add_argument("--port", type=int, required=True, help="the port of this person's bot, e.g. 8771")
    add.add_argument("--no-2fa", action="store_true", help="password only, no 6-digit codes")
    inv = sub.add_parser("invite", help="make a one-time sign-up link for a person")
    inv.add_argument("name")
    inv.add_argument("--port", type=int, required=True, help="the port of this person's bot, e.g. 8771")
    inv.add_argument("--hours", type=float, default=24, help="how long the link works (default 24)")
    inv.add_argument("--site", help="the website address (default: from setup-https.sh)")
    for cmd in ("reset-password", "reset-2fa", "remove-2fa", "remove-user"):
        sub.add_parser(cmd).add_argument("name")
    sub.add_parser("list")
    args = parser.parse_args(argv)

    if args.command == "serve":
        config = GatewayConfig.load(args.config)
        if not config.users:
            raise SystemExit("No users yet: python -m coopa_trada.gateway add-user NAME --port PORT")
        from werkzeug.serving import run_simple
        print(f"Coopa Trada login on http://{args.host}:{args.port} for: {', '.join(config.users)}", flush=True)
        run_simple(args.host, args.port, create_gateway(config), threaded=True)
        return 0

    config = GatewayConfig.load(args.config, create=args.command in ("add-user", "invite"))
    name = getattr(args, "name", "").strip().lower()
    if args.command == "list":
        for user in config.users.values():
            print(f"{user.name}: bot at {user.backend}, 6-digit codes {'on' if user.totp_secret else 'off'}")
        for invite in config.invites.values():
            state = "expired" if invite.expires < time.time() else "waiting"
            print(f"{invite.name}: sign-up link {state} (bot at {invite.backend})")
        return 0
    if args.command == "invite":
        if not name.isidentifier():
            raise SystemExit("Use a simple name: letters, digits and _ only")
        if name in config.users:
            raise SystemExit(f"{name} already has a login (use reset-password or reset-2fa)")
        token = secrets.token_urlsafe(32)
        config.invites[name] = Invite(name, f"http://127.0.0.1:{args.port}", _token_hash(token),
                                      time.time() + args.hours * 3600, new_totp_secret())
        config.save()
        site = (args.site or _saved_site(config.path) or "https://YOUR-SITE").rstrip("/")
        print(f"\nSign-up link for {name} (works once, for {args.hours:g} hours):\n\n  {site}/setup/{token}\n\n"
              "Send it to them privately (a text or DM). Anyone with the link can create this login.")
        if site == "https://YOUR-SITE":
            print("Replace https://YOUR-SITE with your website address.")
        return 0
    if args.command == "add-user":
        if not name.isidentifier():
            raise SystemExit("Use a simple name: letters, digits and _ only")
        if name in config.users:
            raise SystemExit(f"{name} already exists (use reset-password or reset-2fa)")
        user = User(name, hash_password(_ask_password(name)), "" if args.no_2fa else new_totp_secret(),
                    f"http://127.0.0.1:{args.port}")
        if user.totp_secret:
            _enroll_2fa(user)
        config.users[name] = user
    else:
        user = config.users.get(name)
        if user is None:
            raise SystemExit(f"No user {name}")
        if args.command == "remove-user":
            del config.users[name]
        elif args.command == "reset-password":
            user.password_hash = hash_password(_ask_password(name))
        elif args.command == "remove-2fa":
            user.totp_secret = ""
        else:
            user.totp_secret = new_totp_secret()
            _enroll_2fa(user)
        if args.command != "remove-user":
            user.session_version += 1  # logs out every browser that was logged in
    config.save()
    print(f"Saved {config.path}. Restart the login website: sudo systemctl restart coopa-gateway")
    return 0


if __name__ == "__main__":
    sys.exit(main())
