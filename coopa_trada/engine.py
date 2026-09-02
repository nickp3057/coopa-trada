"""Scheduler / reconcile / entry / exit loop.

All datetime math goes through ``clock`` and ``calendar_svc``. All broker and
client calls go through the rate limiter (inside the broker / resolver). The
engine itself holds no market-time literals.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from . import clock
from .config import Mode
from .contracts import ContractNotTradable, ContractUnderspecified
from .exits import (
    CompositeExit,
    HardTimeExit,
    PositionState,
    Quote,
    StopLoss,
    TakeProfit,
    TrailingPeak,
    update_peak,
)
from .logs import get_logger
from .rules import FixedDollarSizer
from .store import Store, position_key

__all__ = ["Engine", "SyntheticQuoteSource", "ReconcileReport"]


class SyntheticQuoteSource:
    """Deterministic offline quote feed for DRY-RUN.

    ``register(osi, mid)`` seeds a symbol; each subsequent call returns a quote
    that drifts by ``drift_per_tick`` (default: a gentle decline) so the exit
    loop has something to act on without triggering on the very first tick.
    """

    def __init__(self, *, drift_per_tick: float = -0.01, spread_pct: float = 0.03):
        self.drift_per_tick = drift_per_tick
        self.spread_pct = spread_pct
        self._base: dict[str, float] = {}
        self._ticks: dict[str, int] = {}

    def register(self, osi_symbol: str, mid: float) -> None:
        self._base.setdefault(osi_symbol, float(mid))
        self._ticks.setdefault(osi_symbol, 0)

    def __call__(self, osi_symbol: str) -> Quote:
        base = self._base.get(osi_symbol)
        if base is None:
            base = 1.0
            self._base[osi_symbol] = base
            self._ticks[osi_symbol] = 0
        n = self._ticks[osi_symbol]
        self._ticks[osi_symbol] = n + 1
        mid = max(0.01, base * (1 + self.drift_per_tick) ** n)
        half = max(0.01, mid * self.spread_pct / 2)
        return Quote(bid=round(mid - half, 2), ask=round(mid + half, 2), mid=round(mid, 4))


@dataclass
class ReconcileReport:
    mode: str
    broker_positions: list = field(default_factory=list)
    broker_open_orders: list = field(default_factory=list)
    local_open_positions: list = field(default_factory=list)
    local_open_orders: list = field(default_factory=list)
    divergences: list[str] = field(default_factory=list)
    simulated: bool = False


class Engine:
    def __init__(
        self,
        *,
        source,
        resolver,
        broker,
        store: Store,
        rules,
        cal,
        mode: Mode,
        quote_source: Callable[[str], Quote] | None = None,
        sizer=None,
        now: Callable[[], datetime] = clock.now_utc,
        sleep: Callable[[float], None] = _time.sleep,
    ):
        self.source = source
        self.resolver = resolver
        self.broker = broker
        self.store = store
        self.rules = rules
        self.cal = cal
        self.mode = mode
        self.quote_source = quote_source
        self.sizer = sizer or FixedDollarSizer(rules.max_dollars_per_position)
        self._now = now
        self._sleep = sleep
        self.log = get_logger("coopa_trada.engine", mode)

        # CompositeExit returns the FIRST trigger. The two "cooper_drop_*" strategies
        # are the ambiguous 10% stop (per Cooper 9/2) -- both left active so whichever
        # would fire first, fires. They are prepended so intent reads top-down; each
        # is optional (absent rule key -> strategy not wired). Note: the two StopLoss
        # / two TrailingPeak instances both report strategy="StopLoss"/"TrailingPeak"
        # in exit_events; the reason string embeds the pct, so they stay distinct.
        strategies: list = []
        if rules.cooper_drop_from_entry_pct is not None:
            strategies.append(StopLoss(rules.cooper_drop_from_entry_pct))
        if rules.cooper_drop_from_peak_pct is not None:
            strategies.append(TrailingPeak(rules.cooper_drop_from_peak_pct))
        strategies += [
            StopLoss(rules.hard_stop_loss_pct),
            TakeProfit(rules.take_profit_pct),
            TrailingPeak(rules.trailing_stop_pct),
            HardTimeExit(),
        ]
        self.composite = CompositeExit(strategies)

    # -- reconcile (hard rule 5) --------------------------------
    def reconcile(self) -> ReconcileReport:
        if self.mode is Mode.DRY_RUN:
            self.log.info("reconcile: simulated (dry-run no-op)")
            return ReconcileReport(mode=self.mode.value, simulated=True)

        report = ReconcileReport(mode=self.mode.value)
        report.broker_positions = self.broker.get_positions()
        report.broker_open_orders = self.broker.get_open_orders()
        report.local_open_positions = [dict(r) for r in self.store.open_positions()]
        report.local_open_orders = [dict(r) for r in self.store.orders_open()]

        broker_syms = {self._symbol_of(p) for p in report.broker_positions}
        local_syms = {r["osi_symbol"] for r in report.local_open_positions}

        for sym in sorted(local_syms - broker_syms):
            msg = f"divergence: local open position {sym} not present at Schwab (Schwab is truth; local row flagged)"
            report.divergences.append(msg)
            self.log.warning(msg)
        for sym in sorted(broker_syms - local_syms):
            msg = f"divergence: Schwab position {sym} has no local open row (untracked; adopt from Schwab)"
            report.divergences.append(msg)
            self.log.warning(msg)

        broker_order_ids = {str(o.get("orderId")) for o in report.broker_open_orders}
        local_broker_ids = {
            str(r["broker_order_id"]) for r in report.local_open_orders if r["broker_order_id"]
        }
        for oid in sorted(local_broker_ids - broker_order_ids):
            msg = f"divergence: local open order broker_id={oid} not open at Schwab"
            report.divergences.append(msg)
            self.log.warning(msg)
        for oid in sorted(broker_order_ids - local_broker_ids):
            msg = f"divergence: Schwab open order {oid} not tracked locally"
            report.divergences.append(msg)
            self.log.warning(msg)

        if not report.divergences:
            self.log.info("reconcile: local state matches Schwab")
        self.log.info(
            "reconcile: %d Schwab positions, %d Schwab open orders, %d divergences",
            len(report.broker_positions),
            len(report.broker_open_orders),
            len(report.divergences),
        )
        return report

    @staticmethod
    def _symbol_of(pos: dict) -> str:
        inst = pos.get("instrument") or {}
        return inst.get("symbol") or pos.get("symbol") or ""

    # -- main loop -------------------------------------------
    def run(self, day: date, *, once: bool = False) -> None:
        self.reconcile()

        entry_time = self.cal.entry_at(day, self.rules.entry_minutes_before_close)
        self.log.info("entry scheduled for %s", clock.to_market(entry_time).isoformat())
        if not once:
            self._wait_until(entry_time)

        self._entry_pass(day)

        if once:
            self._exit_tick(day)
            self.log.info("once=True: one entry pass + one exit tick complete")
            return

        self._exit_loop(day)

    # -- entry ----------------------------------------------
    def _entry_pass(self, day: date) -> None:
        signals = self.source.fetch(day)
        self.log.info("fetched %d signal(s) for %s", len(signals), day.isoformat())
        for signal in signals:
            self.store.record_signal(signal)
            try:
                contract = self.resolver.resolve(signal)
            except ContractUnderspecified as exc:
                self.log.warning("skip %s: underspecified (%s)", signal.id, exc)
                continue
            except ContractNotTradable as exc:
                self.log.warning("skip %s: not tradable (%s)", signal.id, exc)
                continue

            mid = contract.mid
            # Per-position dollar cap: FixedDollarSizer returns 0 when even one
            # contract would exceed max_dollars_per_position -- skip the ticker.
            qty = self.sizer.size(signal, contract, self.rules)
            if qty <= 0:
                self.log.warning(
                    "skip %s: one contract exceeds the per-position dollar cap "
                    "(mid=%.4f, max_dollars=%.2f)",
                    signal.id, mid, self.rules.max_dollars_per_position,
                )
                continue

            open_count = len(self.store.open_positions())
            if open_count >= self.rules.max_concurrent_positions:
                self.log.warning(
                    "skip %s: at max concurrent positions (%d)", signal.id, self.rules.max_concurrent_positions
                )
                continue

            result = self.broker.place_entry(
                contract, qty, contract.ask, signal_id=signal.id, day=day
            )
            if self.quote_source is not None and hasattr(self.quote_source, "register"):
                self.quote_source.register(contract.osi_symbol, mid)
            self.log.info(
                "ENTRY %s %s x%d @ ask %.2f -> status=%s broker_id=%s%s",
                signal.id, contract.osi_symbol, qty, contract.ask, result.status,
                result.broker_order_id, " (dedup)" if result.deduplicated else "",
            )

    # -- exit -----------------------------------------------
    def _positions_state(self, day: date) -> list[tuple[PositionState, str]]:
        out: list[tuple[PositionState, str]] = []
        sig_meta = {}
        for row in self.store.open_positions():
            asset_class = self._asset_class_for(row["signal_id"], sig_meta)
            _, end = self.cal.exit_window(day, asset_class, self.rules)
            entry_price = float(row["entry_price"])
            state = PositionState(
                osi_symbol=row["osi_symbol"],
                entry_price=entry_price,
                entry_at=clock.parse_iso(row["entry_at_utc"]),
                quantity=int(row["quantity"]),
                session_peak_mid=entry_price,
                asset_class=asset_class,
                hard_exit_at=end,
            )
            out.append((state, row["signal_id"]))
        return out

    def _asset_class_for(self, signal_id: str, cache: dict):
        from .models import AssetClass

        if signal_id in cache:
            return cache[signal_id]
        row = self.store.conn.execute(
            "SELECT asset_class FROM signals WHERE id = ?", (signal_id,)
        ).fetchone()
        ac = AssetClass[row["asset_class"]] if row else AssetClass.SINGLE_NAME
        cache[signal_id] = ac
        return ac

    def _exit_tick(self, day: date) -> int:
        acted = 0
        for state, signal_id in self._positions_state(day):
            quote = self._quote(state.osi_symbol)
            update_peak(state, quote)
            now = clock.to_market(self._now())
            decision = self.composite.check(state, quote, now)
            if decision is None:
                continue
            result = self.broker.place_exit(
                state, quote.bid, decision, signal_id=signal_id, day=day
            )
            self.store.record_exit_event(
                position_key=position_key(signal_id, state.osi_symbol),
                order_id=result.order_row_id,
                strategy=decision.strategy,
                reason=decision.reason,
                trigger_price=decision.price,
                peak_mid=state.session_peak_mid,
                detail={"decision_meta": decision.meta, "quote": quote.__dict__, "mode": self.mode.value},
            )
            self.log.info(
                "EXIT %s %s x%d @ bid %.2f via %s (%s) -> status=%s%s",
                signal_id, state.osi_symbol, state.quantity, quote.bid, decision.strategy,
                decision.reason, result.status, " (dedup)" if result.deduplicated else "",
            )
            acted += 1
        return acted

    def _exit_loop(self, day: date) -> None:
        states = self._positions_state(day)
        if not states:
            self.log.info("no open positions to manage")
            return
        window_end = max(s.hard_exit_at for s, _ in states)
        self.log.info("exit loop until %s", clock.to_market(window_end).isoformat())
        while True:
            self._exit_tick(day)
            if not self.store.open_positions():
                self.log.info("all positions closed")
                return
            if self._now() >= window_end:
                self.log.info("exit window ended; %d position(s) still open", len(self.store.open_positions()))
                return
            self._sleep(self.rules.poll_interval_seconds)

    # -- helpers --------------------------------------------
    def _quote(self, osi_symbol: str) -> Quote:
        if self.quote_source is None:
            raise RuntimeError("engine has no quote_source; cannot run the exit loop")
        return self.quote_source(osi_symbol)

    def _wait_until(self, target: datetime) -> None:
        target = clock.ensure_aware(target)
        while True:
            remaining = (target - self._now()).total_seconds()
            if remaining <= 0:
                return
            self._sleep(min(remaining, max(self.rules.poll_interval_seconds, 1)))
