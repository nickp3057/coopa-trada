"""Weekly Schwab reconnect, driven from the web UI.

This is schwab-py's manual login flow (``client_from_manual_flow``) split into
its two public halves so a web page can stand in for the terminal prompt:

1. :func:`begin_login` -> ``schwab.auth.get_auth_context``: builds the Schwab
   login URL locally (no network).
2. The person logs in on Schwab's site and clicks Allow. Schwab redirects to
   the callback URL (``https://127.0.0.1:8182`` by default); that page fails
   to load, but its address contains the one-time code.
3. :func:`finish_login` -> ``schwab.auth.client_from_received_url``: trades
   the pasted address for a token and writes the token file.

The browser-assisted ``client_from_login_flow`` is not used: it starts a
second web server in a separate process and needs a local browser, which is
fragile in a packaged app and impossible on a headless server.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import SchwabCredentials

__all__ = ["LoginError", "LoginRejected", "PendingLogin", "begin_login", "finish_login", "LOGIN_TTL_SECONDS"]

# A started login must be finished within this window.
LOGIN_TTL_SECONDS = 10 * 60


class LoginError(ValueError):
    """Plain-English reason a reconnect could not complete."""


class LoginRejected(LoginError):
    """Schwab was asked and refused; the pending login's code is used up."""


@dataclass(frozen=True)
class PendingLogin:
    auth_context: object
    started_monotonic: float

    @property
    def authorization_url(self) -> str:
        return self.auth_context.authorization_url

    def expired(self, now_monotonic: float | None = None) -> bool:
        now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
        return now_monotonic - self.started_monotonic > LOGIN_TTL_SECONDS


def _auth():
    from schwab import auth
    return auth


def _require_app_credentials(creds: SchwabCredentials) -> None:
    missing = [name for name, value in (("SCHWAB_API_KEY", creds.api_key),
                                        ("SCHWAB_APP_SECRET", creds.app_secret),
                                        ("SCHWAB_CALLBACK_URL", creds.callback_url)) if not value]
    if missing:
        raise LoginError("Schwab app details are missing from the .env file next to the app: "
                         + ", ".join(missing) + ".")


def begin_login(creds: SchwabCredentials) -> PendingLogin:
    _require_app_credentials(creds)
    context = _auth().get_auth_context(creds.api_key, creds.callback_url)
    return PendingLogin(context, time.monotonic())


def _same_callback(received: str, callback: str) -> bool:
    got, want = urlsplit(received), urlsplit(callback)
    return (got.scheme, got.hostname, got.port, got.path.rstrip("/")) == \
        (want.scheme, want.hostname, want.port, want.path.rstrip("/"))


def _atomic_token_writer(token_path: Path):
    def write(token, *args, **kwargs):
        token_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=token_path.parent, prefix=".schwab-token-", suffix=".json")
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(token, handle)
            os.replace(temporary, token_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return write


def finish_login(creds: SchwabCredentials, pending: PendingLogin | None, received_url: str) -> None:
    _require_app_credentials(creds)
    if pending is None:
        raise LoginError("No Schwab login is in progress. Click Reconnect Schwab to start again.")
    if pending.expired():
        raise LoginError("That login attempt is more than 10 minutes old. Click Reconnect Schwab to start again.")
    received_url = (received_url or "").strip()
    if not _same_callback(received_url, creds.callback_url):
        raise LoginError("That is not the address Schwab sent you to. Copy the whole address from the "
                         f"browser's address bar after clicking Allow; it starts with {creds.callback_url}")
    query = parse_qs(urlsplit(received_url).query)
    if "code" not in query:
        raise LoginError("The pasted address has no login code in it. Make sure you clicked Allow on "
                         "Schwab's page, then copy the whole address.")
    try:
        _auth().client_from_received_url(
            creds.api_key, creds.app_secret, pending.auth_context, received_url,
            _atomic_token_writer(Path(creds.token_path)))
    except Exception as exc:  # authlib / HTTP errors carry no plain-English message
        raise LoginRejected("Schwab did not accept that login. Its codes only last a short time, so click "
                         f"Reconnect Schwab and paste the new address straight away. (Details: {exc})") from exc
