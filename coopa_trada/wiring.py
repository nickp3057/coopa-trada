"""Shared construction of the engine dependency graph.

Both the CLI (``__main__.cmd_run``) and the local web UI need the exact same
DRY-RUN / LIVE wiring: which resolver, which broker, which quote source. That
graph lives here once so the two entrypoints can never drift apart.

Nothing in here changes trading behaviour -- it only assembles objects defined
elsewhere.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from functools import partial
from pathlib import Path
from typing import Callable

from . import clock
from .calendar_svc import CalendarService
from .config import Mode, credentials_from_env
from .engine import Engine, SyntheticQuoteSource
from .models import CsvSignalSource
from .rules import load_rules
from .schwab_token import TokenState, read_token_status, start_block_reason
from .store import Store

__all__ = [
    "TokenMissing",
    "parse_trading_date",
    "build_engine",
    "token_status",
]

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
    # Checked before schwab-py loads the file, so no request is made with a
    # missing, damaged or expired login.
    reason = start_block_reason(read_token_status(creds.token_path), clock.now_utc())
    if reason is not None:
        raise TokenMissing(reason)
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
    if mode is Mode.LIVE:
        extra["token_status"] = partial(read_token_status, creds.token_path)
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


def token_status(creds=None, now: datetime | None = None) -> dict:
    """A read-only, never-raising summary of the Schwab token file for the UI.

    Wraps :func:`coopa_trada.schwab_token.read_token_status`; it never
    refreshes the token and never contacts Schwab.
    """
    if creds is None:
        creds = credentials_from_env()
    status = read_token_status(creds.token_path, now)
    remaining = status.seconds_remaining
    out = status.to_dict()
    out.update(
        present=Path(creds.token_path).is_file(),
        creation_age_days=(round((status.checked_at - status.created_at).total_seconds() / 86400, 2)
                           if status.created_at else None),
        refresh_expires_in_days=round(remaining / 86400, 2) if remaining is not None else None,
        stale=status.state is not TokenState.CONNECTED,
        error=(None if status.created_at else
               "no token file on disk" if not Path(creds.token_path).is_file() else status.message),
        app_configured=bool(creds.api_key and creds.app_secret and creds.callback_url),
    )
    return out
