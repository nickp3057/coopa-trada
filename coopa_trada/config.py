"""Environment loading and run-mode resolution.

Mode is the single most important decision the program makes. It is
DRY-RUN unless the operator asks for LIVE twice, independently:

    * ``--live`` on the command line, and
    * ``LIVE_TRADING=1`` in the environment.

Either one alone leaves the program in DRY-RUN.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = ["Mode", "SchwabCredentials", "load_env", "resolve_mode", "credentials_from_env"]


class Mode(str, Enum):
    DRY_RUN = "DRY-RUN"
    LIVE = "LIVE"


def load_env(dotenv_path: str | os.PathLike[str] | None = None) -> None:
    """Load ``.env`` in development. A missing file is not an error.

    ``python-dotenv`` is a runtime dependency; if it is somehow absent we
    degrade to the real process environment rather than crash.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is a pinned dependency
        return
    # On a server running one copy of the bot per person, each copy names its own
    # file, and no other .env is read, so one person's keys never leak into another's.
    dotenv_path = dotenv_path or os.environ.get("COOPA_TRADA_ENV_FILE") or None
    if dotenv_path is not None:
        load_dotenv(dotenv_path)
    else:
        load_dotenv()


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_mode(live_flag: bool, env: dict[str, str] | None = None) -> Mode:
    """LIVE only when the flag is set *and* ``LIVE_TRADING`` is truthy."""
    environ = os.environ if env is None else env
    if live_flag and _truthy(environ.get("LIVE_TRADING")):
        return Mode.LIVE
    return Mode.DRY_RUN


@dataclass(frozen=True)
class SchwabCredentials:
    api_key: str
    app_secret: str
    callback_url: str
    token_path: Path
    account_id: str | None = None

    def token_exists(self) -> bool:
        return self.token_path.is_file()


def credentials_from_env(env: dict[str, str] | None = None) -> SchwabCredentials:
    """Collect Schwab settings from the environment.

    Does not validate that secrets are present — callers that actually reach
    the network are responsible for a loud, non-traceback failure when the
    token file is missing (see ``__main__``).
    """
    e = os.environ if env is None else env
    return SchwabCredentials(
        api_key=e.get("SCHWAB_API_KEY", ""),
        app_secret=e.get("SCHWAB_APP_SECRET", ""),
        callback_url=e.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182"),
        token_path=Path(e.get("SCHWAB_TOKEN_PATH", "./schwab_token.json")).expanduser(),
        account_id=e.get("SCHWAB_ACCOUNT_ID") or None,
    )
