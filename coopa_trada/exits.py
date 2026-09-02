"""Exit logic. Pure functions of (position, quote, now) — no I/O, no clock,
no network. This is the part that is proven against synthetic price series
before a single live quote is allowed through it.

A strategy returns an :class:`ExitDecision` when it wants out, or ``None``.
``CompositeExit`` runs a list in order and returns the first decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from .clock import ensure_aware
from .models import AssetClass

__all__ = [
    "Quote",
    "ExitDecision",
    "PositionState",
    "ExitStrategy",
    "TrailingPeak",
    "HardTimeExit",
    "StopLoss",
    "TakeProfit",
    "CompositeExit",
    "update_peak",
]


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    mid: float

    @classmethod
    def from_bid_ask(cls, bid: float, ask: float) -> "Quote":
        return cls(bid=bid, ask=ask, mid=round((bid + ask) / 2, 4))


@dataclass
class ExitDecision:
    strategy: str
    reason: str
    price: float
    at: datetime
    meta: dict = field(default_factory=dict)


@dataclass
class PositionState:
    osi_symbol: str
    entry_price: float
    entry_at: datetime
    quantity: int
    session_peak_mid: float
    asset_class: AssetClass
    hard_exit_at: datetime

    def __post_init__(self) -> None:
        ensure_aware(self.entry_at)
        ensure_aware(self.hard_exit_at)


@runtime_checkable
class ExitStrategy(Protocol):
    def check(self, pos: PositionState, quote: Quote, now: datetime) -> ExitDecision | None: ...


def update_peak(pos: PositionState, quote: Quote) -> float:
    """Raise ``pos.session_peak_mid`` to the running high. Call once per tick."""
    if quote.mid > pos.session_peak_mid:
        pos.session_peak_mid = quote.mid
    return pos.session_peak_mid


class TrailingPeak:
    """Exit when mid has retraced ``pct`` from the session peak.

    The caller is responsible for keeping ``pos.session_peak_mid`` current
    (use :func:`update_peak` before calling ``check``).
    """

    def __init__(self, pct: float):
        if not 0 < pct < 1:
            raise ValueError("TrailingPeak pct must be in (0, 1)")
        self.pct = pct

    def check(self, pos: PositionState, quote: Quote, now: datetime) -> ExitDecision | None:
        trigger = pos.session_peak_mid * (1 - self.pct)
        if quote.mid <= trigger:
            return ExitDecision(
                strategy="TrailingPeak",
                reason=f"mid {quote.mid:.4f} <= peak {pos.session_peak_mid:.4f} * (1 - {self.pct})",
                price=quote.bid,
                at=now,
                meta={"peak": pos.session_peak_mid, "trigger": round(trigger, 4), "pct": self.pct},
            )
        return None


class HardTimeExit:
    """Exit unconditionally once ``now`` reaches ``pos.hard_exit_at``.

    ``hard_exit_at`` is supplied by ``calendar_svc.exit_window`` — never a literal.
    """

    def check(self, pos: PositionState, quote: Quote, now: datetime) -> ExitDecision | None:
        if ensure_aware(now) >= pos.hard_exit_at:
            return ExitDecision(
                strategy="HardTimeExit",
                reason=f"now {now.isoformat()} >= hard_exit_at {pos.hard_exit_at.isoformat()}",
                price=quote.bid,
                at=now,
                meta={"hard_exit_at": pos.hard_exit_at.isoformat()},
            )
        return None


class StopLoss:
    """Exit when mid has fallen ``pct`` below the entry price.

    If a gap prints below the stop, the decision still fires; ``price`` reflects
    the gapped bid the fill would occur at, not the theoretical stop level.
    """

    def __init__(self, pct: float):
        if not 0 < pct < 1:
            raise ValueError("StopLoss pct must be in (0, 1)")
        self.pct = pct

    def check(self, pos: PositionState, quote: Quote, now: datetime) -> ExitDecision | None:
        trigger = pos.entry_price * (1 - self.pct)
        if quote.mid <= trigger:
            return ExitDecision(
                strategy="StopLoss",
                reason=f"mid {quote.mid:.4f} <= entry {pos.entry_price:.4f} * (1 - {self.pct})",
                price=quote.bid,
                at=now,
                meta={"entry_price": pos.entry_price, "trigger": round(trigger, 4), "pct": self.pct},
            )
        return None


class TakeProfit:
    """Exit when mid has risen ``pct`` above the entry price."""

    def __init__(self, pct: float):
        if pct <= 0:
            raise ValueError("TakeProfit pct must be > 0")
        self.pct = pct

    def check(self, pos: PositionState, quote: Quote, now: datetime) -> ExitDecision | None:
        trigger = pos.entry_price * (1 + self.pct)
        if quote.mid >= trigger:
            return ExitDecision(
                strategy="TakeProfit",
                reason=f"mid {quote.mid:.4f} >= entry {pos.entry_price:.4f} * (1 + {self.pct})",
                price=quote.bid,
                at=now,
                meta={"entry_price": pos.entry_price, "trigger": round(trigger, 4), "pct": self.pct},
            )
        return None


class CompositeExit:
    """Run strategies in order; return the first non-``None`` decision."""

    def __init__(self, strategies: list[ExitStrategy]):
        self.strategies = list(strategies)

    def check(self, pos: PositionState, quote: Quote, now: datetime) -> ExitDecision | None:
        for strategy in self.strategies:
            decision = strategy.check(pos, quote, now)
            if decision is not None:
                return decision
        return None
