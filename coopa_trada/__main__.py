"""Command-line entrypoint.

    coopa_trada run       [--live] [--once] [--date YYYY-MM-DD] [--signals P] [--config P] [--db P]
    coopa_trada reconcile [--db P] [--config P]
    coopa_trada resolve   --signals P [--date YYYY-MM-DD]
    coopa_trada unlock    [--db P] [--force-other-host]

Every subcommand loads ``.env``, resolves the run mode, and prints the banner.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from .calendar_svc import CalendarService
from .config import Mode, load_env, resolve_mode
from .logs import banner, get_logger, setup_logging
from .models import CsvSignalSource, UnrecognizedContractNotation
from .rules import load_rules
from .wiring import (
    TokenMissing,
    account_hash as _account_hash,
    build_engine,
    parse_trading_date as _parse_date,
    schwab_client as _schwab_client,
)

DEFAULT_CONFIG = "config/rules.yaml"
DEFAULT_SIGNALS = "data/signals.example.csv"
DEFAULT_DB = "./trada.db"

__all__ = ["TokenMissing", "build_parser", "main"]


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

    unl = sub.add_parser("unlock", help="clear a stale lock left by a run that did not shut down cleanly")
    unl.add_argument("--db", default=DEFAULT_DB)
    unl.add_argument("--force-other-host", action="store_true",
                     help="also clear a lock recorded by another computer (shared folders only)")

    return p


# -- subcommands --------------------------------------------------------------
def cmd_run(args) -> int:
    mode = resolve_mode(args.live)
    setup_logging()
    print(banner(mode), flush=True)
    log = get_logger("coopa_trada.cli", mode)

    day = _parse_date(args.date)
    if not CalendarService().is_trading_day(day):
        log.error("%s is not an XNYS trading day", day.isoformat())
        return 2

    from . import alerts

    alerter = alerts.from_env()
    # Same dependency graph the web UI builds -- see coopa_trada/wiring.py.
    try:
        engine, store = build_engine(
            mode=mode,
            signals_path=args.signals,
            config_path=args.config,
            db_path=args.db,
            alerter=alerter,
        )
    except Exception as exc:
        alerts.report_failure(alerter, exc, refused=True, mode=mode.value)
        raise
    from .keepawake import KeepAwake

    awake = KeepAwake()
    awake.set(True, "a command-line run is active")
    try:
        engine.run(day, once=args.once)  # alerts for its own refusals and errors
    finally:
        awake.release()
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


def cmd_unlock(args) -> int:
    from .runlock import RunLocked, clear_stale_lock, known_locks

    rc = 0
    for lock in known_locks(args.db):
        if lock["state"] == "free":
            continue
        if lock["state"] == "held":
            print(f"{lock['purpose']} lock is in use by {lock['description']}; left alone.")
            rc = 1
            continue
        try:
            print(clear_stale_lock(lock["path"], force_other_host=args.force_other_host)["message"])
        except RunLocked as exc:
            print(str(exc))
            rc = 1
    if rc == 0:
        print("No stale locks remain.")
    return rc


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env()
    handler = {
        "run": cmd_run,
        "reconcile": cmd_reconcile,
        "resolve": cmd_resolve,
        "unlock": cmd_unlock,
    }[args.command]
    from .runlock import RunLocked, StaleLock

    try:
        return handler(args)
    except (FileNotFoundError, RunLocked, StaleLock) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
