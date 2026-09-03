"""Shared construction of the engine dependency graph.

Both the CLI (``__main__.cmd_run``) and the local web UI need the exact same
DRY-RUN / LIVE wiring: which resolver, which broker, which quote source. That
graph lives here once so the two entrypoints can never drift apart.

Nothing in here changes trading behaviour -- it only assembles objects defined
elsewhere.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from . import clock
from .calendar_svc import CalendarService
from .config import Mode, credentials_from_env
from .engine import Engine, SyntheticQuoteSource
from .models import CsvSignalSource
from .rules import load_rules
from .store import Store

__all__ = [
    "TokenMissing",
    "parse_trading_date",
    "build_engine",
    "token_status",
]

# Refresh tokens minted by the schwab-py login flow are valid for 7 days from
# creation. This is an operational trip-wire, not an API-enforced value here.
REFRESH_TOKEN_LIFETIME_DAYS = 7


class TokenMissing(RuntimeError):
    """No usable Schwab token on disk. Raised with a plain, non-traceback message."""


def parse_trading_date(value: str | None) -> date:
    """CLI/UI ``--date`` parsing: ISO date, or today in the market tz when absent."""
    if value is None:
        return clock.to_market(clock.now_utc()).date()
    return date.fromisoformat(value)


# -- Schwab client (lazy; only touched on the LIVE path) ---------------------
def schwab_client():
    creds = credentials_from_env()
    if not creds.token_exists():
        raise TokenMissing(
            f"no Schwab token at {creds.token_path}. Run the one-time interactive "
            f"login to mint it (see README), then retry."
        )
    try:
        from schwab.auth import client_from_token_file
    except ImportError as exc:  # pragma: no cover
        raise TokenMissing(f"schwab-py not importable: {exc}") from exc
    return client_from_token_file(
        token_path=str(creds.token_path),
        api_key=creds.api_key,
        app_secret=creds.app_secret,
    ), creds


def account_hash(client, creds) -> str:
    resp = client.get_account_numbers()
    data = resp.json() if hasattr(resp, "json") else resp
    entries = data if isinstance(data, list) else data.get("accountNumbers", data)
    if creds.account_id:
        for e in entries:
            if str(e.get("accountNumber")) == str(creds.account_id):
                return e["hashValue"]
        raise SystemExit(f"account {creds.account_id} not found among Schwab accounts")
    return entries[0]["hashValue"]


def build_engine(
    *,
    mode: Mode,
    signals_path: str | Path,
    config_path: str | Path,
    db_path: str | Path | None = None,
    store: Store | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[Engine, Store]:
    """Assemble a fully wired :class:`Engine` plus the :class:`Store` it uses.

    ``mode`` decides the whole dependency graph:

    * ``Mode.DRY_RUN`` -> ``SyntheticResolver`` + ``DryRunBroker`` +
      ``SyntheticQuoteSource``; no network, no token.
    * ``Mode.LIVE``    -> real ``schwab-py`` client behind ``ContractResolver`` /
      ``SchwabBroker`` / ``SchwabQuoteSource``, all rate-limited.

    Pass ``store`` to reuse an open connection (the web UI opens its own per
    background run); otherwise a fresh one is opened from ``db_path``.
    """
    if store is None:
        if db_path is None:
            raise ValueError("build_engine needs either store= or db_path=")
        store = Store.open(db_path)

    rules = load_rules(config_path)
    cal = CalendarService()
    source = CsvSignalSource(str(signals_path))

    if mode is Mode.LIVE:
        from .broker import SchwabBroker, SchwabQuoteSource
        from .contracts import ContractResolver
        from .ratelimit import RateLimiter

        client, creds = schwab_client()
        rl = RateLimiter(120)
        acct_hash = account_hash(client, creds)
        broker = SchwabBroker(client, acct_hash, store, rl)
        resolver = ContractResolver(client, rl)
        quote_source = SchwabQuoteSource(client, rl)
    else:
        from .broker import DryRunBroker
        from .contracts import SyntheticResolver

        quote_source = SyntheticQuoteSource()
        broker = DryRunBroker(store, quote_source)
        resolver = SyntheticResolver()

    extra: dict = {}
    if now is not None:
        extra["now"] = now
    if sleep is not None:
        extra["sleep"] = sleep

    engine = Engine(
        source=source,
        resolver=resolver,
        broker=broker,
        store=store,
        rules=rules,
        cal=cal,
        mode=mode,
        quote_source=quote_source,
        **extra,
    )
    return engine, store


def token_status(creds=None) -> dict:
    """A read-only, never-raising summary of the Schwab token file.

    The 7-day refresh-token expiry is a real operational risk in this project,
    so the UI needs to see it coming. This inspects the file only -- it never
    refreshes, never contacts Schwab, and degrades to ``error`` text rather
    than throwing.
    """
    if creds is None:
        creds = credentials_from_env()
    path = Path(creds.token_path)
    out: dict = {
        "present": False,
        "path": str(path),
        "creation_age_days": None,
        "refresh_expires_in_days": None,
        "stale": True,
        "error": None,
    }
    if not path.is_file():
        out["error"] = "no token file on disk"
        return out

    out["present"] = True
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        out["error"] = f"token file unreadable: {exc}"
        return out

    created = raw.get("creation_timestamp")
    if created is None:
        # Fall back to the file's own mtime -- less accurate but still a signal.
        try:
            created = path.stat().st_mtime
            out["error"] = "no creation_timestamp in token; using file mtime"
        except OSError as exc:  # pragma: no cover
            out["error"] = f"cannot stat token file: {exc}"
            return out

    try:
        created_dt = datetime.fromtimestamp(float(created), tz=timezone.utc)
    except (TypeError, ValueError, OSError) as exc:
        out["error"] = f"bad creation timestamp {created!r}: {exc}"
        return out

    age_days = (datetime.now(timezone.utc) - created_dt).total_seconds() / 86400.0
    remaining = REFRESH_TOKEN_LIFETIME_DAYS - age_days
    out["creation_age_days"] = round(age_days, 2)
    out["refresh_expires_in_days"] = round(remaining, 2)
    # Amber within a day of expiry; red once past it.
    out["stale"] = remaining <= 1.0
    return out
