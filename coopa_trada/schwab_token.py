"""Schwab login (token file) status, read-only.

schwab-py writes the token file as::

    {"creation_timestamp": <unix seconds at login>, "token": {..., "refresh_token": ...}}

``creation_timestamp`` is set once by the browser login and kept on every
automatic access-token refresh (see ``schwab.auth.TokenMetadata``). Schwab's
refresh token lasts 7 days from that login and can only be renewed by logging
in again, so the expiry is ``creation_timestamp + 7 days``.

Nothing here contacts Schwab or modifies the file.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

__all__ = [
    "REFRESH_TOKEN_LIFETIME",
    "EXPIRING_SOON",
    "START_MARGIN",
    "TokenState",
    "TokenStatus",
    "read_token_status",
    "start_block_reason",
]

REFRESH_TOKEN_LIFETIME = timedelta(days=7)
# Dashboard warning threshold.
EXPIRING_SOON = timedelta(days=2)
# A run must keep Schwab access this long past the session close.
START_MARGIN = timedelta(minutes=30)
# Tolerated clock difference for a creation time slightly in the future.
_CLOCK_SKEW = timedelta(minutes=5)


class TokenState(str, Enum):
    CONNECTED = "connected"
    EXPIRING_SOON = "expiring_soon"
    EXPIRED = "expired"
    NOT_SET_UP = "not_set_up"


LABELS = {
    TokenState.CONNECTED: "Connected",
    TokenState.EXPIRING_SOON: "Expiring soon",
    TokenState.EXPIRED: "Expired",
    TokenState.NOT_SET_UP: "Not set up",
}


@dataclass(frozen=True)
class TokenStatus:
    state: TokenState
    path: str
    message: str
    created_at: datetime | None = None
    expires_at: datetime | None = None
    checked_at: datetime | None = None

    @property
    def label(self) -> str:
        return LABELS[self.state]

    @property
    def seconds_remaining(self) -> float | None:
        if self.expires_at is None or self.checked_at is None:
            return None
        return (self.expires_at - self.checked_at).total_seconds()

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "label": self.label,
            "message": self.message,
            "path": self.path,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "seconds_remaining": self.seconds_remaining,
            "warning": self.state is not TokenState.CONNECTED,
        }


def _not_set_up(path: Path, why: str, now: datetime) -> TokenStatus:
    return TokenStatus(TokenState.NOT_SET_UP, str(path), why + " Click Reconnect Schwab to log in.",
                       checked_at=now)


def read_token_status(path: str | Path, now: datetime | None = None) -> TokenStatus:
    """Classify the token file. Never raises; problems become NOT_SET_UP."""
    path = Path(path)
    now = now or datetime.now(timezone.utc)
    if not path.is_file():
        return _not_set_up(path, "No Schwab login has been saved on this computer yet.", now)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _not_set_up(path, "The saved Schwab login file is unreadable.", now)
    if not isinstance(raw, dict):
        return _not_set_up(path, "The saved Schwab login file is not in the expected format.", now)
    token = raw.get("token")
    if not isinstance(token, dict) or not token.get("refresh_token"):
        # schwab-py itself refuses files without this structure.
        return _not_set_up(path, "The saved Schwab login file is incomplete or from an older version.", now)
    created = raw.get("creation_timestamp")
    if isinstance(created, bool) or not isinstance(created, (int, float)) or not math.isfinite(created):
        return _not_set_up(path, "The saved Schwab login has no valid login time, so its expiry is unknown.", now)
    try:
        created_at = datetime.fromtimestamp(created, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return _not_set_up(path, "The saved Schwab login has no valid login time, so its expiry is unknown.", now)
    if created_at > now + _CLOCK_SKEW:
        return _not_set_up(path, "The saved Schwab login time is in the future; check this computer's clock.", now)

    expires_at = created_at + REFRESH_TOKEN_LIFETIME
    remaining = expires_at - now
    if remaining <= timedelta(0):
        state, message = TokenState.EXPIRED, (
            "The Schwab login expired. Schwab requires logging in again every 7 days. "
            "Click Reconnect Schwab.")
    elif remaining < EXPIRING_SOON:
        state, message = TokenState.EXPIRING_SOON, (
            "The Schwab login expires in under 2 days. Click Reconnect Schwab before it does.")
    else:
        state, message = TokenState.CONNECTED, "Logged in to Schwab."
    return TokenStatus(state, str(path), message, created_at, expires_at, now)


def start_block_reason(status: TokenStatus, needed_until: datetime) -> str | None:
    """Why a run that talks to Schwab must not start, or None.

    ``needed_until`` is when the run last needs Schwab (session close plus a
    margin): a login that lapses mid-session would leave positions unmanaged.
    """
    if status.state in (TokenState.NOT_SET_UP, TokenState.EXPIRED):
        return f"Cannot start: Schwab is {status.label.lower()}. {status.message}"
    if status.expires_at is None or status.expires_at <= needed_until:
        when = status.expires_at.isoformat() if status.expires_at else "an unknown time"
        return ("Cannot start: the Schwab login expires at " + when + ", before this run finishes "
                "managing positions after the market close. Click Reconnect Schwab first.")
    return None
