"""The login website in front of everyone's bot.

One server can run a separate copy of Coopa Trada per person (its own Schwab
login, settings, trade log and Telegram bot; see docs/SERVER_SETUP.md). This
small web app is the only thing the internet reaches (through Caddy, which adds
HTTPS). It asks for a user name, a password and a 6-digit code from an
authenticator app, then forwards that person, and only that person, to their
own copy. Cooper logging in reaches Cooper's bot; Nick logging in reaches
Nick's. Nothing in one copy can reach the other.

    python -m coopa_trada.gateway add-user cooper --port 8771
    python -m coopa_trada.gateway serve

Security: passwords are stored as salted hashes (never the password); the
6-digit codes are standard TOTP (RFC 6238), each usable once; 5 wrong tries for
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
    totp_secret: str
    backend: str
    session_version: int = 1


@dataclass
class GatewayConfig:
    path: Path
    secret_key: str
    users: dict[str, User] = field(default_factory=dict)

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
        return cls(path, raw["secret_key"], users)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {"secret_key": self.secret_key,
                "users": {u.name: {"password_hash": u.password_hash, "totp_secret": u.totp_secret,
                                   "backend": u.backend, "session_version": u.session_version}
                          for u in self.users.values()}}
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
<label for="c">6-digit code from your authenticator app</label>
<input id="c" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9 ]{{6,7}}" required>
<input type="hidden" name="next" value="{next}">
<button type="submit">Log in</button>{error}
</form></body></html>"""


def create_gateway(config: GatewayConfig, *, clock=time.time, http_client=None):
    import httpx
    from flask import Flask, make_response, redirect, request
    from itsdangerous import BadSignature, URLSafeTimedSerializer

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 9 * 1024 * 1024
    signer = URLSafeTimedSerializer(config.secret_key, salt="coopa-gateway-session")
    throttle = Throttle(clock)
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
        user = config.users.get(data.get("u"))
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
        user = config.users.get(name)
        password_ok = user is not None and check_password(user.password_hash, request.form.get("password", ""))
        step = (verify_totp(user.totp_secret, request.form.get("code", ""), clock(),
                            after_step=last_step.get(name, -1)) if password_ok else None)
        if step is None:
            throttle.fail(*keys)
            return page(name, "Wrong name, password or code.", nxt, 401)
        last_step[name] = step
        throttle.clear(*keys)
        resp = redirect(nxt, 303)
        resp.set_cookie(COOKIE, signer.dumps({"u": user.name, "v": user.session_version}),
                        max_age=SESSION_SECONDS, httponly=True, samesite="Strict", secure=https(), path="/")
        return resp

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
    for cmd in ("reset-password", "reset-2fa", "remove-user"):
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

    config = GatewayConfig.load(args.config, create=args.command == "add-user")
    name = getattr(args, "name", "").strip().lower()
    if args.command == "list":
        for user in config.users.values():
            print(f"{user.name}: bot at {user.backend}")
        return 0
    if args.command == "add-user":
        if not name.isidentifier():
            raise SystemExit("Use a simple name: letters, digits and _ only")
        if name in config.users:
            raise SystemExit(f"{name} already exists (use reset-password or reset-2fa)")
        user = User(name, hash_password(_ask_password(name)), new_totp_secret(), f"http://127.0.0.1:{args.port}")
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
