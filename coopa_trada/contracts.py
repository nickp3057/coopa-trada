"""Signal -> resolved, tradable OSI option contract.

No selection heuristics live here. If a signal does not name both a strike
and an expiration, that is ``ContractUnderspecified`` and the engine skips it.
The one place schwab-py option-chain calls appear besides ``broker.py``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime

from . import clock
from .models import Right, Signal

__all__ = [
    "ResolvedContract",
    "ContractUnderspecified",
    "ContractNotTradable",
    "ContractResolver",
    "SyntheticResolver",
    "osi_symbol",
]


def osi_symbol(underlying: str, expiration: date, right: Right, strike: float) -> str:
    """OCC/OSI 21-char symbol, e.g. ``AAPL  260918C00150000``."""
    root = underlying.upper()[:6].ljust(6)
    ymd = expiration.strftime("%y%m%d")
    cp = "C" if right is Right.CALL else "P"
    strike_millis = f"{int(round(strike * 1000)):08d}"
    return f"{root}{ymd}{cp}{strike_millis}"


@dataclass
class ResolvedContract:
    osi_symbol: str
    underlying: str
    right: Right
    strike: float
    expiration: date
    bid: float
    ask: float
    mid: float
    open_interest: int
    tradable: bool
    resolved_at: datetime


class ContractUnderspecified(Exception):
    """Signal is missing a strike or an expiration. No selection rule is invented."""


class ContractNotTradable(Exception):
    """Contract not found, flagged non-tradable, or a one-sided market (bid<=0 or ask<=0)."""


class ContractResolver:
    """Resolves a :class:`Signal` against a live schwab-py client.

    The exact ``get_option_chain`` keyword names / enum types are confirmed
    against the installed ``schwab-py`` during implementation; only this class
    and ``broker.SchwabBroker`` touch them.
    """

    def __init__(self, client, ratelimiter=None):
        self.client = client
        self.ratelimiter = ratelimiter

    def resolve(self, signal: Signal) -> ResolvedContract:
        if signal.strike is None or signal.expiration is None:
            raise ContractUnderspecified(
                f"signal {signal.id}: need both strike and expiration "
                f"(strike={signal.strike!r}, expiration={signal.expiration!r})"
            )

        if self.ratelimiter is not None:
            self.ratelimiter.acquire()

        from schwab.client import Client
        from .broker import _check_response

        resp = self.client.get_option_chain(
            symbol=signal.underlying,
            contract_type=Client.Options.ContractType[signal.right.value],  # "CALL" / "PUT"
            strike=signal.strike,
            from_date=signal.expiration,
            to_date=signal.expiration,
        )
        _check_response(resp)
        payload = resp.json() if hasattr(resp, "json") else resp
        raw = self._locate(payload, signal)
        if raw is None:
            raise ContractNotTradable(
                f"signal {signal.id}: no {signal.right.value} {signal.underlying} "
                f"{signal.strike} exp {signal.expiration} in chain"
            )

        bid = float(raw.get("bid") or 0.0)
        ask = float(raw.get("ask") or 0.0)
        tradable = bool(raw.get("tradable", True)) and not raw.get("nonStandard", False)
        import math
        if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0 or ask <= 0 or bid > ask or not tradable:
            raise ContractNotTradable(
                f"signal {signal.id}: contract not two-sided/tradable (bid={bid}, ask={ask}, tradable={tradable})"
            )

        return ResolvedContract(
            osi_symbol=raw.get("symbol") or osi_symbol(
                signal.underlying, signal.expiration, signal.right, signal.strike
            ),
            underlying=signal.underlying,
            right=signal.right,
            strike=float(raw.get("strikePrice", signal.strike)),
            expiration=signal.expiration,
            bid=bid,
            ask=ask,
            mid=round((bid + ask) / 2, 4),
            open_interest=int(raw.get("openInterest") or 0),
            tradable=True,
            resolved_at=clock.now_utc(),
        )

    @staticmethod
    def _locate(payload: dict, signal: Signal) -> dict | None:
        key = "callExpDateMap" if signal.right is Right.CALL else "putExpDateMap"
        exp_map = (payload or {}).get(key) or {}
        for exp_label, strike_map in exp_map.items():
            # exp_label looks like "2026-09-18:11" (date:daysToExp)
            exp_date_str = exp_label.split(":", 1)[0]
            try:
                if date.fromisoformat(exp_date_str) != signal.expiration:
                    continue
            except ValueError:
                continue
            for strike_str, contracts in strike_map.items():
                if abs(float(strike_str) - float(signal.strike)) < 1e-6:
                    if isinstance(contracts, list) and contracts:
                        return contracts[0]
                    if isinstance(contracts, dict):
                        return contracts
        return None


class SyntheticResolver:
    """Offline resolver for DRY-RUN. Deterministic, no network, no schwab-py.

    Produces a plausible two-sided market from a hash of the contract terms so
    the engine's entry/exit paths can run end-to-end without a token.
    """

    def __init__(self, *, base_price: float = 1.10, spread_pct: float = 0.04, open_interest: int = 2500):
        self.base_price = base_price
        self.spread_pct = spread_pct
        self.open_interest = open_interest

    def resolve(self, signal: Signal) -> ResolvedContract:
        if signal.strike is None or signal.expiration is None:
            raise ContractUnderspecified(
                f"signal {signal.id}: need both strike and expiration"
            )
        seed = hashlib.sha256(
            f"{signal.underlying}|{signal.right.value}|{signal.strike}|{signal.expiration}".encode()
        ).hexdigest()
        bump = (int(seed[:8], 16) % 250) / 100.0  # 0.00 .. 2.49
        mid = round(self.base_price + bump, 2)
        half = round(max(0.02, mid * self.spread_pct / 2), 2)
        bid = round(mid - half, 2)
        ask = round(mid + half, 2)
        return ResolvedContract(
            osi_symbol=osi_symbol(signal.underlying, signal.expiration, signal.right, signal.strike),
            underlying=signal.underlying,
            right=signal.right,
            strike=float(signal.strike),
            expiration=signal.expiration,
            bid=bid,
            ask=ask,
            mid=round((bid + ask) / 2, 4),
            open_interest=self.open_interest,
            tradable=True,
            resolved_at=clock.now_utc(),
        )
