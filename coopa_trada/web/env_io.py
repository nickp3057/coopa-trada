"""The Schwab app details in this bot's own settings file (``.env``).

Each person's bot has its own ``.env`` (on a server: ``COOPA_TRADA_ENV_FILE``,
one per person), so a person saving their Schwab App Key here can never touch
anyone else's. The secret is never sent back to the browser: the page only
learns whether it is set. ``LIVE_TRADING`` is deliberately not editable here;
switching real trading on stays a step done on the server itself.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

__all__ = ["EnvValidationError", "env_path", "schwab_keys_status", "save_schwab_keys"]

KEYS = ("SCHWAB_API_KEY", "SCHWAB_APP_SECRET", "SCHWAB_CALLBACK_URL", "SCHWAB_ACCOUNT_ID")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{8,200}$")


class EnvValidationError(ValueError):
    """A proposed value would be unsafe or unusable."""


def env_path() -> Path:
    return Path(os.environ.get("COOPA_TRADA_ENV_FILE") or ".env").expanduser()


def _mask(value: str, keep: int) -> str:
    return "•" * 6 + value[-keep:] if value else ""


def schwab_keys_status() -> dict:
    env = os.environ
    key, secret = env.get("SCHWAB_API_KEY", ""), env.get("SCHWAB_APP_SECRET", "")
    account = env.get("SCHWAB_ACCOUNT_ID", "")
    return {"api_key_set": bool(key), "api_key_hint": _mask(key, 4),
            "secret_set": bool(secret),
            "callback_url": env.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182"),
            "account_id_set": bool(account), "account_hint": _mask(account, 3),
            "file": str(env_path())}


def _validate(payload: dict) -> dict:
    out = {}
    for field, name in (("api_key", "SCHWAB_API_KEY"), ("app_secret", "SCHWAB_APP_SECRET")):
        value = (payload.get(field) or "").strip()
        if value:  # blank keeps what is saved
            if not _TOKEN_RE.match(value):
                raise EnvValidationError(f"That {'App Key' if field == 'api_key' else 'Secret'} doesn't look "
                                         "right: copy it again from the Schwab Developer Portal (no spaces).")
            out[name] = value
    callback = (payload.get("callback_url") or "").strip()
    if callback:
        parts = urlsplit(callback)
        if parts.scheme != "https" or not parts.netloc or any(c.isspace() for c in callback):
            raise EnvValidationError("The callback URL must be the https address registered on your Schwab "
                                     "app, character for character (normally https://127.0.0.1:8182).")
        out["SCHWAB_CALLBACK_URL"] = callback
    account = (payload.get("account_id") or "").replace(" ", "").replace("-", "")
    if account:
        if not account.isdigit() or not 4 <= len(account) <= 20:
            raise EnvValidationError("The account number is digits only.")
        out["SCHWAB_ACCOUNT_ID"] = account
    if payload.get("clear_account_id") is True:
        out["SCHWAB_ACCOUNT_ID"] = ""
    return out


def save_schwab_keys(payload: dict) -> dict:
    """Update the given keys in this bot's .env (others kept) and in this process."""
    updates = _validate(payload if isinstance(payload, dict) else {})
    if not updates:
        raise EnvValidationError("Nothing to save: fill in at least one box.")
    path = env_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    done = set()
    for i, line in enumerate(lines):
        name = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and name in updates:
            lines[i] = f"{name}={updates[name]}"
            done.add(name)
    lines += [f"{name}={value}" for name, value in updates.items() if name not in done]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    os.environ.update(updates)  # takes effect now; the file keeps it across restarts
    return schwab_keys_status()
