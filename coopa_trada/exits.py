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
    "ProfitPointTrail",
    "PROFIT_TRAIL_ARMS",
    "CompositeExit",
    "update_peak",
]


# Float tolerance so a price exactly at a trigger counts as reaching it
# (0.90 x 1.6 is 1.4400000000000002 in floating point; a $1.44 mid must count).
_EPS = 1e-9


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
    entry_order_id: int | None = None

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
        if quote.mid <= trigger + _EPS:
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
        if quote.mid <= trigger + _EPS:
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
        if quote.mid >= trigger - _EPS:
            return ExitDecision(
                strategy="TakeProfit",
                reason=f"mid {quote.mid:.4f} >= entry {pos.entry_price:.4f} * (1 + {self.pct})",
                price=quote.bid,
                at=now,
                meta={"entry_price": pos.entry_price, "trigger": round(trigger, 4), "pct": self.pct},
            )
        return None


PROFIT_TRAIL_ARMS = ("from_entry", "in_profit")


class ProfitPointTrail:
    """Cooper's rule: sell when the profit % falls ``points`` percentage points
    below its peak profit %.

    profit % = (mid - entry) / entry * 100. With entry $1.00 and peak $2.00
    (peak profit +100%), 10 points means selling at +90%, i.e. mid <= $1.90.
    In prices: trigger = peak - points/100 * entry. The drop is measured in
    points of the *entry* price, not as a percentage of the peak.

    ``arm``:
      * ``"from_entry"`` (literal reading): active from the moment of entry. The
        peak starts at the entry price, so before any gain it acts as a
        -``points``% stop (entry $1.00 -> sells at $0.90).
      * ``"in_profit"``: active only once the peak has been above the entry
        price (the position has been in profit at least once).
    """

    def __init__(self, points: float, arm: str = "from_entry"):
        if not 0 < points < 100:
            raise ValueError("ProfitPointTrail points must be in (0, 100)")
        if arm not in PROFIT_TRAIL_ARMS:
            raise ValueError(f"ProfitPointTrail arm must be one of {PROFIT_TRAIL_ARMS}")
        self.points = points
        self.arm = arm

    def trigger(self, pos: PositionState) -> float | None:
        """The mid at or below which this exit fires, or None while unarmed."""
        peak = max(pos.session_peak_mid, pos.entry_price)
        if self.arm == "in_profit" and peak <= pos.entry_price:
            return None
        return peak - self.points / 100 * pos.entry_price

    def check(self, pos: PositionState, quote: Quote, now: datetime) -> ExitDecision | None:
        trigger = self.trigger(pos)
        if trigger is None or quote.mid > trigger + _EPS:
            return None
        peak = max(pos.session_peak_mid, pos.entry_price)
        peak_pct = (peak / pos.entry_price - 1) * 100
        now_pct = (quote.mid / pos.entry_price - 1) * 100
        return ExitDecision(
            strategy="ProfitPointTrail",
            reason=(f"profit {now_pct:+.1f}% is {peak_pct - now_pct:.1f} points below peak profit "
                    f"{peak_pct:+.1f}% (limit {self.points:g} points)"),
            price=quote.bid,
            at=now,
            meta={"entry_price": pos.entry_price, "peak": peak, "trigger": round(trigger, 4),
                  "points": self.points, "arm": self.arm},
        )


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
