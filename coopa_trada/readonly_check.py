"""Read-only Schwab check: does the saved login reach accounts, balances,
positions and a real option chain?

Only four schwab-py methods can be called, through :class:`ReadOnlyClient`;
anything else (every order method included) raises before a request is made.
The report says what worked in counts and yes/no answers. It never prints an
account number, an account hash, or a dollar amount.

    python -m coopa_trada readonly-check [--symbol SPY]
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta

from . import clock

__all__ = ["ReadOnlyClient", "NotReadOnly", "CheckResult", "run_check", "ALLOWED_METHODS"]

# The only Schwab calls this check may make.
ALLOWED_METHODS = frozenset({"get_account_numbers", "get_account", "get_accounts", "get_option_chain"})


class NotReadOnly(RuntimeError):
    """Something tried to call a Schwab method outside the read-only list."""


class ReadOnlyClient:
    """Wraps a schwab-py client and refuses every method except ALLOWED_METHODS."""

    def __init__(self, client):
        self._client = client
        self.calls: list[str] = []

    def __getattr__(self, name):
        if name not in ALLOWED_METHODS:
            raise NotReadOnly(f"{name} is not a read-only Schwab call; refused")
        method = getattr(self._client, name)

        def call(*args, **kwargs):
            self.calls.append(name)
            return method(*args, **kwargs)

        return call


@dataclass
class CheckResult:
    step: str
    ok: bool
    detail: str


@dataclass
class Report:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, step, ok, detail):
        self.results.append(CheckResult(step, ok, detail))

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)

    def lines(self) -> list[str]:
        return [f"[{'OK' if r.ok else 'FAILED'}] {r.step}: {r.detail}" for r in self.results]


def _json(resp):
    from .broker import _check_response
    _check_response(resp)
    return resp.json() if hasattr(resp, "json") else resp


def _error(exc: Exception) -> str:
    # Exception text can carry URLs or numbers; keep only the type and an HTTP status.
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return type(exc).__name__ + (f" (HTTP {status})" if status else "")


def run_check(client, *, account_id: str | None = None, symbol: str = "SPY") -> Report:
    """Run the read-only checks with ``client`` (wrapped in ReadOnlyClient)."""
    ro = client if isinstance(client, ReadOnlyClient) else ReadOnlyClient(client)
    report = Report()

    account_hash = None
    try:
        data = _json(ro.get_account_numbers())
        entries = data if isinstance(data, list) else (data or {}).get("accountNumbers", [])
        hashes = [e.get("hashValue") for e in entries if isinstance(e, dict) and e.get("hashValue")]
        if account_id:
            chosen = [e.get("hashValue") for e in entries
                      if isinstance(e, dict) and str(e.get("accountNumber")) == str(account_id)]
            account_hash = chosen[0] if chosen else None
            pinned = "the SCHWAB_ACCOUNT_ID account was found" if chosen else \
                "the SCHWAB_ACCOUNT_ID account was NOT among them"
        else:
            account_hash = hashes[0] if hashes else None
            pinned = "no SCHWAB_ACCOUNT_ID set, so the first account would be used"
        report.add("account numbers", bool(account_hash),
                   f"{len(hashes)} account(s) returned; {pinned}")
    except Exception as exc:  # noqa: BLE001 - every failure is reported, none is fatal
        report.add("account numbers", False, _error(exc))

    if account_hash:
        try:
            from schwab.client import Client
            fields = [Client.Account.Fields.POSITIONS]
        except Exception:  # pragma: no cover - schwab-py is a dependency
            fields = None
        try:
            data = _json(ro.get_account(account_hash, fields=fields))
            acct = (data or {}).get("securitiesAccount", data) or {}
            balances = {k: v for k, v in acct.items() if k.endswith("Balances") and isinstance(v, dict)}
            numeric = sum(1 for b in balances.values() for v in b.values()
                          if isinstance(v, (int, float)) and not isinstance(v, bool))
            report.add("balances", bool(balances),
                       f"sections present: {', '.join(sorted(balances)) or 'none'}; "
                       f"{numeric} numeric balance field(s) (values not shown)")
            positions = acct.get("positions") or []
            options = sum(1 for p in positions
                          if (p.get("instrument") or {}).get("assetType") == "OPTION")
            report.add("positions", isinstance(positions, list),
                       f"{len(positions)} position(s), {options} of them options")
        except Exception as exc:  # noqa: BLE001
            report.add("balances", False, _error(exc))
            report.add("positions", False, "not checked (account call failed)")
    else:
        report.add("balances", False, "not checked (no account)")
        report.add("positions", False, "not checked (no account)")

    try:
        today = clock.to_market(clock.now_utc()).date()
        from schwab.client import Client
        data = _json(ro.get_option_chain(symbol, contract_type=Client.Options.ContractType.CALL,
                                         strike_count=4, from_date=today,
                                         to_date=today + timedelta(days=14)))
        contracts = [c for strikes in (data or {}).get("callExpDateMap", {}).values()
                     for cs in strikes.values() for c in (cs if isinstance(cs, list) else [cs])]
        two_sided = sum(1 for c in contracts if _pos(c.get("bid")) and _pos(c.get("ask")))
        with_delta = sum(1 for c in contracts
                         if isinstance(c.get("delta"), (int, float)) and math.isfinite(c["delta"])
                         and abs(c["delta"]) <= 1)
        report.add(f"option chain ({symbol} calls, next 2 weeks)", bool(contracts),
                   f"{len(contracts)} contract(s); {two_sided} with a two-sided bid/ask; "
                   f"{with_delta} with a usable delta")
    except Exception as exc:  # noqa: BLE001
        report.add(f"option chain ({symbol})", False, _error(exc))
    return report


def _pos(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0
