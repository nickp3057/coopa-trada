"""Command-line entrypoint.

    coopa_trada run       [--live] [--once] [--date YYYY-MM-DD] [--signals P] [--config P] [--db P]
    coopa_trada reconcile [--db P] [--config P]
    coopa_trada resolve   --signals P [--date YYYY-MM-DD]

Every subcommand loads ``.env``, resolves the run mode, and prints the banner.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from . import clock
from .calendar_svc import CalendarService
from .config import Mode, credentials_from_env, load_env, resolve_mode
from .logs import banner, get_logger, setup_logging
from .models import CsvSignalSource, UnrecognizedContractNotation
from .rules import load_rules

DEFAULT_CONFIG = "config/rules.yaml"
DEFAULT_SIGNALS = "data/signals.example.csv"
DEFAULT_DB = "./trada.db"


class TokenMissing(RuntimeError):
    pass


def _parse_date(value: str | None) -> date:
    if value is None:
        return clock.to_market(clock.now_utc()).date()
    return date.fromisoformat(value)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coopa_trada", description="Schwab options automation skeleton")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="reconcile, wait for entry, place entries, manage exits")
    run.add_argument("--live", action="store_true", help="request LIVE mode (also needs LIVE_TRADING=1)")
    run.add_argument("--once", action="store_true", help="one entry pass + one exit tick, then return")
    run.add_argument("--date", help="trading day YYYY-MM-DD (default: today, market tz)")
    run.add_argument("--signals", default=DEFAULT_SIGNALS)
    run.add_argument("--config", default=DEFAULT_CONFIG)
    run.add_argument("--db", default=DEFAULT_DB)

    rec = sub.add_parser("reconcile", help="print Schwab vs local positions/orders")
    rec.add_argument("--live", action="store_true", help="request LIVE mode (also needs LIVE_TRADING=1)")
    rec.add_argument("--db", default=DEFAULT_DB)
    rec.add_argument("--config", default=DEFAULT_CONFIG)

    res = sub.add_parser("resolve", help="resolve each signal to an OSI contract (read-only, no orders)")
    res.add_argument("--signals", default=DEFAULT_SIGNALS)
    res.add_argument("--date", help="restrict to signals received on this market date")

    return p


# -- Schwab wiring (lazy; only when actually going live / hitting the chain) --
def _schwab_client():
    creds = credentials_from_env()
    if not creds.token_exists():
        raise TokenMissing(
            f"no Schwab token at {creds.token_path}. Run the one-time interactive login "
            f"to mint it (see README), then retry."
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


def _account_hash(client, creds) -> str:
    resp = client.get_account_numbers()
    data = resp.json() if hasattr(resp, "json") else resp
    entries = data if isinstance(data, list) else data.get("accountNumbers", data)
    if creds.account_id:
        for e in entries:
            if str(e.get("accountNumber")) == str(creds.account_id):
                return e["hashValue"]
        raise SystemExit(f"account {creds.account_id} not found among Schwab accounts")
    return entries[0]["hashValue"]


# -- subcommands --------------------------------------------------------------
def cmd_run(args) -> int:
    mode = resolve_mode(args.live)
    setup_logging()
    print(banner(mode), flush=True)
    log = get_logger("coopa_trada.cli", mode)

    day = _parse_date(args.date)
    rules = load_rules(args.config)
    cal = CalendarService()
    if not cal.is_trading_day(day):
        log.error("%s is not an XNYS trading day", day.isoformat())
        return 2

    from .store import Store

    store = Store.open(args.db)
    source = CsvSignalSource(args.signals)

    if mode is Mode.LIVE:
        from .broker import SchwabBroker, SchwabQuoteSource
        from .contracts import ContractResolver
        from .ratelimit import RateLimiter

        client, creds = _schwab_client()
        rl = RateLimiter(120)
        acct_hash = _account_hash(client, creds)
        broker = SchwabBroker(client, acct_hash, store, rl)
        resolver = ContractResolver(client, rl)
        quote_source = SchwabQuoteSource(client, rl)
    else:
        from .broker import DryRunBroker
        from .contracts import SyntheticResolver
        from .engine import SyntheticQuoteSource

        quote_source = SyntheticQuoteSource()
        broker = DryRunBroker(store, quote_source)
        resolver = SyntheticResolver()

    from .engine import Engine

    engine = Engine(
        source=source,
        resolver=resolver,
        broker=broker,
        store=store,
        rules=rules,
        cal=cal,
        mode=mode,
        quote_source=quote_source,
    )
    try:
        engine.run(day, once=args.once)
    finally:
        store.close()
    return 0


def cmd_reconcile(args) -> int:
    mode = resolve_mode(getattr(args, "live", False))
    setup_logging()
    print(banner(mode), flush=True)
    log = get_logger("coopa_trada.cli", mode)

    rules = load_rules(args.config)
    cal = CalendarService()
    from .store import Store

    store = Store.open(args.db)

    if mode is Mode.LIVE:
        from .broker import SchwabBroker
        from .ratelimit import RateLimiter

        try:
            client, creds = _schwab_client()
        except TokenMissing as exc:
            log.error(str(exc))
            return 2
        rl = RateLimiter(120)
        broker = SchwabBroker(client, _account_hash(client, creds), store, rl)
        resolver = None
    else:
        from .broker import DryRunBroker

        broker = DryRunBroker(store)
        resolver = None

    from .engine import Engine

    engine = Engine(
        source=CsvSignalSource(DEFAULT_SIGNALS),
        resolver=resolver,
        broker=broker,
        store=store,
        rules=rules,
        cal=cal,
        mode=mode,
    )
    report = engine.reconcile()
    _print_reconcile(report)
    store.close()
    return 0


def _print_reconcile(report) -> None:
    print(f"\n-- reconcile ({report.mode}) --")
    if report.simulated:
        print("  dry-run: no-op (simulated). Schwab is not contacted.")
        return
    print(f"  Schwab positions   : {len(report.broker_positions)}")
    print(f"  Schwab open orders : {len(report.broker_open_orders)}")
    print(f"  local open positions: {len(report.local_open_positions)}")
    print(f"  local open orders   : {len(report.local_open_orders)}")
    if report.divergences:
        print("  divergences (Schwab treated as truth):")
        for d in report.divergences:
            print(f"    - {d}")
    else:
        print("  no divergences")


def cmd_resolve(args) -> int:
    load_env()
    mode = resolve_mode(False)
    setup_logging()
    print(banner(mode), flush=True)
    log = get_logger("coopa_trada.cli", mode)

    from .contracts import (
        ContractNotTradable,
        ContractResolver,
        ContractUnderspecified,
        SyntheticResolver,
    )
    from .ratelimit import RateLimiter

    source = CsvSignalSource(args.signals)
    day = date.fromisoformat(args.date) if args.date else None
    signals = source.fetch(day)
    notation_errors = getattr(source, "notation_errors", 0)

    try:
        client, _creds = _schwab_client()
        resolver = ContractResolver(client, RateLimiter(120))
        header = f"-- resolve: {len(signals)} signal(s) --"
    except TokenMissing as exc:
        log.warning("%s -- falling back to offline SyntheticResolver", exc)
        resolver = SyntheticResolver()
        header = f"-- resolve: {len(signals)} signal(s) (offline SyntheticResolver, no token) --"

    print(f"\n{header}")
    if notation_errors:
        print(
            f"[{UnrecognizedContractNotation.code}] {notation_errors} row(s) had "
            f"unrecognized notation and were skipped"
        )
    rc = 0
    for s in signals:
        try:
            c = resolver.resolve(s)
            print(
                f"  {s.id:<20} {c.osi_symbol}  bid={c.bid:.2f} ask={c.ask:.2f} "
                f"mid={c.mid:.2f} OI={c.open_interest}"
            )
        except (ContractUnderspecified, ContractNotTradable) as exc:
            print(f"  {s.id:<20} SKIP: {exc}")
            rc = 1
    return rc


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env()
    handler = {
        "run": cmd_run,
        "reconcile": cmd_reconcile,
        "resolve": cmd_resolve,
    }[args.command]
    try:
        return handler(args)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
