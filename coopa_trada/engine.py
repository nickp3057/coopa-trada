"""Scheduler / reconcile / entry / exit loop.

All datetime math goes through ``clock`` and ``calendar_svc``. All broker and
client calls go through the rate limiter (inside the broker / resolver). The
engine itself holds no market-time literals.
"""

from __future__ import annotations

import time as _time
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Callable

from . import clock
from .alerts import Alerter, contract_text, report_failure
from .config import Mode
from .broker import OrderNotPlaced, OrderOutcomeUnknown
from .contracts import ContractNotTradable, ContractUnderspecified
from .exits import (
    CompositeExit,
    HardTimeExit,
    PositionState,
    ProfitPointTrail,
    Quote,
    StopLoss,
    TakeProfit,
    TrailingPeak,
    update_peak,
)
from .logs import get_logger
from .rules import FixedQuantitySizer
from .runlock import ProcessLock, account_lock_path, database_lock_path, describe
from .schwab_token import hold_needed_until, start_block_reason
from .store import Store, position_key

__all__ = ["Engine", "SyntheticQuoteSource", "ReconcileReport"]

# An exit order still working this long after it was placed gets an alert.
EXIT_UNFILLED_ALERT = timedelta(minutes=3)
# Exit orders that ended without selling; the position is still open.
_FAILED_EXIT_STATUSES = {"REJECTED", "CANCELED", "CANCELLED", "EXPIRED", "NOT_PLACED", "ERROR"}


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


@dataclass
class ExitPlan:
    """How one open position is managed on the run's day (see Engine._exit_plan)."""
    kind: str               # "entry_day" | "held" | "overdue"
    start: datetime         # exits are evaluated from here
    end: datetime           # the asset class's exit-window end today
    hard_exit_at: datetime  # when HardTimeExit fires, if it applies
    composite: CompositeExit
    held_overnight: bool    # True: may stay open past today's window


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
        token_status=None,
        sleep: Callable[[float], None] = _time.sleep,
        alerter: Alerter | None = None,
    ):
        self.source = source
        self.resolver = resolver
        self.broker = broker
        self.store = store
        self.rules = rules
        self.cal = cal
        self.mode = mode
        self.quote_source = quote_source
        self.sizer = sizer or FixedQuantitySizer(rules.max_dollars_per_position)
        # Callable(now) -> schwab_token.TokenStatus; required whenever a
        # component talks to Schwab (see _needs_schwab_token).
        self.token_status = token_status
        self._now = now
        self._sleep = sleep
        self.log = get_logger("coopa_trada.engine", mode)
        # Anything a person must act on is also pushed to them (see alerts).
        self.alerter = alerter if alerter is not None else Alerter()
        # "starting" until the entry pass begins; a failure before then is a refused run.
        self.phase = "starting"

        # Optional 10% readings coexist with the standard exits. First trigger wins.
        strategies: list = []
        if rules.cooper_drop_from_entry_pct is not None:
            strategies.append(StopLoss(rules.cooper_drop_from_entry_pct))
        if rules.cooper_drop_from_peak_pct is not None:
            strategies.append(TrailingPeak(rules.cooper_drop_from_peak_pct))
        if rules.profit_trail_points is not None:
            strategies.append(ProfitPointTrail(rules.profit_trail_points, rules.profit_trail_arm))
        strategies += [
            StopLoss(rules.hard_stop_loss_pct),
            TakeProfit(rules.take_profit_pct),
            TrailingPeak(rules.trailing_stop_pct),
            HardTimeExit(),
        ]
        # Every exit: positions held from an earlier day.
        self.composite = CompositeExit(strategies)

    # -- reconcile (hard rule 5) --------------------------------
    def reconcile(self) -> ReconcileReport:
        if self.mode is Mode.DRY_RUN:
            self.log.info("reconcile: simulated (dry-run no-op)")
            return ReconcileReport(mode=self.mode.value, simulated=True)

        self._sync_orders()
        report = ReconcileReport(mode=self.mode.value)
        report.broker_positions = self.broker.get_positions()
        report.broker_open_orders = self.broker.get_open_orders()
        report.local_open_positions = [dict(r) for r in self.store.open_positions(self.mode.value)]
        report.local_open_orders = [dict(r) for r in self.store.orders_open(self.mode.value)]

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

        for sym in sorted(local_syms & broker_syms):
            local_qty = sum(p["quantity"] for p in report.local_open_positions if p["osi_symbol"] == sym)
            broker_qty = sum(float(p.get("longQuantity", 0)) - float(p.get("shortQuantity", 0))
                             for p in report.broker_positions if self._symbol_of(p) == sym)
            if local_qty != broker_qty:
                report.divergences.append(f"quantity mismatch for {sym}: local={local_qty}, Schwab={broker_qty}")

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
    def _needs_schwab_token(self) -> bool:
        if self.mode is Mode.LIVE:
            return True
        parts = (self.broker, self.resolver, self.quote_source)
        return any(getattr(part, "requires_schwab_token", False) for part in parts)

    def _check_schwab_token(self, day: date) -> None:
        if not self._needs_schwab_token():
            return  # simulation with synthetic data needs no login
        if self.token_status is None:
            raise ValueError("Cannot start: this run uses Schwab but no Schwab login check is configured")
        # Today's entries are sold during the next trading day's exit window,
        # so the login has to last through that, not just today's session.
        reason = start_block_reason(self.token_status(self._now()),
                                    hold_needed_until(self.cal, self.rules, day))
        if reason is not None:
            raise ValueError(reason)

    def _run_locks(self) -> list[ProcessLock]:
        """One copy per database and per Schwab account (see runlock)."""
        locks = []
        row = self.store.conn.execute("PRAGMA database_list").fetchone()
        if row is not None and row["file"]:
            locks.append(ProcessLock(database_lock_path(row["file"]), purpose="database",
                                     mode=self.mode.value))
        account = getattr(self.broker, "account_hash", None)
        if account:
            locks.append(ProcessLock(account_lock_path(account), purpose="Schwab account",
                                     mode=self.mode.value))
        return locks

    def run(self, day: date, *, once: bool = False) -> None:
        self.phase = "starting"
        try:
            self._run_with_locks(day, once=once)
        except Exception as exc:
            report_failure(self.alerter, exc, refused=self.phase == "starting", mode=self.mode.value)
            raise

    def _run_with_locks(self, day: date, *, once: bool) -> None:
        with ExitStack() as held:
            for lock in self._run_locks():
                held.enter_context(lock)
                if lock.cleared_stale:
                    self.log.warning("previous simulation did not shut down cleanly (%s); continuing",
                                     describe(lock.cleared_stale))
            self._run_locked(day, once=once)

    def _run_locked(self, day: date, *, once: bool) -> None:
        if self.mode is Mode.LIVE:
            # Enforced here, not only in the UI, so neither the CLI nor the web
            # runner can start live trading on unconfirmed settings.
            blocked = self.rules.live_block_reason()
            if blocked is not None:
                raise ValueError(blocked)
            if once:
                raise ValueError("Live --once is disabled: orders require continuous monitoring")
            if day != clock.to_market(self._now()).date():
                raise ValueError("Live trading date must be today")
        self._check_schwab_token(day)
        report = self.reconcile()
        if report.divergences:
            raise RuntimeError("Reconciliation found divergences; resolve them before starting trading")

        entry_time = self.cal.entry_at(day, self.rules.entry_minutes_before_close)
        self.log.info("entry scheduled for %s", clock.to_market(entry_time).isoformat())
        if not once:
            # Positions held from an earlier day are managed while waiting.
            self._wait_until(entry_time, day)
            # The login may have aged past the hold while waiting for entry time.
            self._check_schwab_token(day)

        if self.mode is Mode.LIVE and self._now() >= self.cal.session_close(day):
            raise ValueError("Cannot start a live run after market close")
        self.phase = "trading"
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
            if signal.expiration is None or signal.expiration < day:
                self.log.warning("skip %s: expired or missing expiration", signal.id)
                continue
            _, end = self.cal.exit_window(day, signal.asset_class, self.rules)
            if self.mode is Mode.LIVE and self._now() >= end:
                self.log.warning("skip %s: exit deadline already reached", signal.id)
                continue
            self.store.record_signal(signal)
            delta_reason = self.rules.delta_filter.skip_reason(signal)
            if delta_reason is not None:
                self._record_skip(signal, day, "delta_filter", delta_reason, {
                    "source": self.rules.delta_filter.source,
                    "value": getattr(signal, f"signal_delta_{self.rules.delta_filter.source}"),
                    "min_abs_delta": self.rules.delta_filter.min_abs_delta,
                    "max_abs_delta": self.rules.delta_filter.max_abs_delta,
                })
                continue
            try:
                contract = self.resolver.resolve(signal)
            except ContractUnderspecified as exc:
                self.log.warning("skip %s: underspecified (%s)", signal.id, exc)
                continue
            except ContractNotTradable as exc:
                self.log.warning("skip %s: not tradable (%s)", signal.id, exc)
                continue

            mid = contract.mid
            # Fixed quantity per pick (1). FixedQuantitySizer returns 0 when that
            # pick's cost at the ask would exceed max_dollars_per_position -- skip it.
            qty = self.sizer.size(signal, contract, self.rules)
            if qty <= 0:
                self._record_skip(signal, day, "spending_cap",
                    f"1 contract exceeds spending cap (ask={contract.ask:.4f}, cap={self.rules.max_dollars_per_position:.2f})",
                    {"ask": contract.ask, "mid": mid, "cap": self.rules.max_dollars_per_position})
                continue

            positions = self.store.open_positions(self.mode.value)
            pending = [r for r in self.store.orders_open(self.mode.value) if r["intent"] == "ENTRY"]
            reserved = {r["signal_id"] for r in positions} | {r["signal_id"] for r in pending}
            if signal.id in reserved:
                continue
            open_count = len(reserved)
            if open_count >= self.rules.max_concurrent_positions:
                self.log.warning(
                    "skip %s: at max concurrent positions (%d)", signal.id, self.rules.max_concurrent_positions
                )
                continue

            if contract.osi_symbol in self.store.halted_symbols(self.mode.value):
                self._record_skip(signal, day, "halted",
                    f"{contract.osi_symbol} is halted: an earlier order's outcome is unknown",
                    {"osi_symbol": contract.osi_symbol})
                continue
            try:
                result = self.broker.place_entry(
                    contract, qty, contract.ask, signal_id=signal.id, day=day
                )
            except (OrderNotPlaced, OrderOutcomeUnknown) as exc:
                self.log.error("ENTRY %s: %s", signal.id, exc)
                self._alert_order_problem("buy", contract.osi_symbol, exc, key=f"entry:{signal.id}")
                continue  # recorded for a person; never resubmitted
            if self.quote_source is not None and hasattr(self.quote_source, "register"):
                self.quote_source.register(contract.osi_symbol, mid)
            self.log.info(
                "ENTRY %s %s x%d @ ask %.2f -> status=%s broker_id=%s%s",
                signal.id, contract.osi_symbol, qty, contract.ask, result.status,
                result.broker_order_id, " (dedup)" if result.deduplicated else "",
            )

    def _record_skip(self, signal, day, category, reason, detail):
        self.log.warning("skip %s: %s", signal.id, reason)
        self.store.record_skip(signal_id=signal.id, day=day, mode=self.mode.value,
                               category=category, reason=reason, detail=detail)

    # -- exit -----------------------------------------------
    def _positions_state(self, day: date) -> list[tuple[PositionState, str]]:
        return [(state, signal_id) for state, signal_id, _ in self._managed_positions(day)]

    def _managed_positions(self, day: date) -> list[tuple[PositionState, str, "ExitPlan"]]:
        out = []
        sig_meta = {}
        for row in self.store.open_positions(self.mode.value):
            asset_class, expiration = self._signal_meta(row["signal_id"], sig_meta)
            entry_price = float(row["entry_price"])
            entry_at = clock.parse_iso(row["entry_at_utc"])
            entry_day = (date.fromisoformat(row["trading_day"]) if row["trading_day"]
                         else clock.to_market(entry_at).date())
            plan = self._exit_plan(day, asset_class, entry_day, expiration)
            state = PositionState(
                osi_symbol=row["osi_symbol"],
                entry_price=entry_price,
                entry_at=entry_at,
                quantity=int(row["quantity"]),
                session_peak_mid=max(entry_price, float(row["peak_mid"])),
                asset_class=asset_class,
                hard_exit_at=plan.hard_exit_at,
                entry_order_id=row["order_id"],
            )
            out.append((state, row["signal_id"], plan))
        return out

    def _exit_plan(self, day: date, asset_class, entry_day: date, expiration: date | None) -> "ExitPlan":
        """Which exits apply to a position today, and from when.

        Cooper's hold rule: on the day of entry only the take-profit (plus the
        stop-type exits when ``stops_on_entry_day``) can sell; otherwise the
        position is held and rolls to the next trading day, where every exit
        applies from ``held_exit_start`` and the time exit sells at that day's
        exit-window end. An option expiring on its entry day cannot be held,
        so the time exit applies then too. A position still open after its
        planned next-day exit is overdue and is sold from the start of today's
        monitoring.
        """
        start, end = self.cal.exit_window(day, asset_class, self.rules)
        if entry_day >= day:
            expires_today = expiration is not None and expiration <= day
            keep = [s for s in self.composite.strategies
                    if isinstance(s, TakeProfit)
                    or (isinstance(s, HardTimeExit) and expires_today)
                    or (self.rules.stops_on_entry_day and not isinstance(s, (TakeProfit, HardTimeExit)))]
            return ExitPlan("entry_day", start, end, end, CompositeExit(keep), held_overnight=not expires_today)
        held_start = min(max(self.cal.resolve_time(day, self.rules.held_exit_start),
                             self.cal.session_open(day)), end)
        planned = self.cal.next_session(entry_day)
        if day > planned:
            return ExitPlan("overdue", held_start, end, held_start, self.composite, held_overnight=False)
        return ExitPlan("held", held_start, end, end, self.composite, held_overnight=False)

    def _signal_meta(self, signal_id: str, cache: dict):
        from .models import AssetClass

        if signal_id in cache:
            return cache[signal_id]
        row = self.store.conn.execute(
            "SELECT asset_class, expiration FROM signals WHERE id = ?", (signal_id,)
        ).fetchone()
        ac = AssetClass[row["asset_class"]] if row else AssetClass.SINGLE_NAME
        exp = date.fromisoformat(row["expiration"]) if row and row["expiration"] else None
        cache[signal_id] = (ac, exp)
        return cache[signal_id]

    def _asset_class_for(self, signal_id: str, cache: dict):
        return self._signal_meta(signal_id, cache)[0]

    def _exit_tick(self, day: date) -> int:
        self._sync_orders()
        acted = 0
        halted = self.store.halted_symbols(self.mode.value)
        for state, signal_id, plan in self._managed_positions(day):
            self._check_exit_order(state, signal_id)
            if state.osi_symbol in halted:
                self.log.warning("hold %s: halted until a person resolves an unknown order", state.osi_symbol)
                continue
            quote = self._quote(state.osi_symbol)
            update_peak(state, quote)
            self.store.update_peak(state.entry_order_id, state.session_peak_mid)
            now = clock.to_market(self._now())
            if now < plan.start:
                continue
            decision = plan.composite.check(state, quote, now)
            if decision is None:
                continue
            try:
                result = self.broker.place_exit(
                    state, quote.bid, decision, signal_id=signal_id, day=day
                )
            except (OrderNotPlaced, OrderOutcomeUnknown) as exc:
                self.log.error("EXIT %s: %s", signal_id, exc)
                self._alert_order_problem("sell", state.osi_symbol, exc, key=f"exit:{signal_id}")
                continue  # recorded for a person; never resubmitted
            if result.deduplicated:
                continue
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

    # -- alerts ---------------------------------------------
    def _alert_order_problem(self, side: str, osi_symbol: str, exc: Exception, *, key: str) -> None:
        what = contract_text(osi_symbol)
        if isinstance(exc, OrderOutcomeUnknown):
            self.alerter.alert(
                "order_unknown", f"Check Schwab: {side} order unclear",
                f"The {side} order for {what} may or may not have reached Schwab. It was NOT resent, "
                f"and this option is halted. Check Orders in Schwab, then say what you found on the "
                f"Reconcile page.", key=key)
        else:
            self.alerter.alert(
                "order_not_placed", f"{side.capitalize()} order not placed",
                f"The {side} order for {what} never reached Schwab and was not resent. "
                + ("The position is still open: sell it in Schwab if you want out. " if side == "sell" else "")
                + "Acknowledge it on the Reconcile page.", key=key)

    def _check_exit_order(self, state: PositionState, signal_id: str) -> None:
        """Alert when this position's exit order failed, or is not filling."""
        row = self.store.conn.execute(
            "SELECT * FROM orders WHERE intent='EXIT' AND mode=? AND entry_order_id=? ORDER BY id DESC LIMIT 1",
            (self.mode.value, state.entry_order_id)).fetchone()
        if row is None:
            return
        what = contract_text(state.osi_symbol)
        if row["status"] in _FAILED_EXIT_STATUSES:
            self.alerter.alert(
                "exit_failed", "Sell order failed",
                f"The sell order for {what} ended as {row['status']} and the position is still open. "
                f"The bot will not send another. Check Schwab and sell it there if you want out.",
                key=f"exit:{signal_id}")
        elif row["status"] not in ("UNKNOWN", "FILLED"):
            age = self._now() - clock.parse_iso(row["created_at_utc"])
            if age >= EXIT_UNFILLED_ALERT:
                minutes = int(age.total_seconds() // 60)
                self.alerter.alert(
                    "exit_unfilled", "Sell order not filling",
                    f"The sell order for {what} has been waiting {minutes} minutes without filling. "
                    f"Check it in Schwab; the bot does not lower the price by itself.",
                    key=f"exit-unfilled:{signal_id}")

    def _sync_orders(self) -> None:
        if self.mode is Mode.LIVE:
            self.broker.sync_orders()

    def _exit_loop(self, day: date) -> None:
        # Pending entries still need monitoring even before the first fill arrives.
        while True:
            self._exit_tick(day)
            pending = self.store.orders_open(self.mode.value)
            positions = self.store.open_positions(self.mode.value)
            if not positions and not pending:
                self.log.info("all positions closed and orders settled")
                return
            if not pending and self._only_overnight_holds(day):
                self.log.info("holding %d position(s) to the next trading day (take-profit not reached)",
                              len(positions))
                return
            if self.mode is Mode.LIVE:
                for order in pending:
                    if order["intent"] != "ENTRY" or order["status"] == "UNKNOWN":
                        continue  # an unknown order cannot be canceled; a person resolves it
                    ac = self._asset_class_for(order["signal_id"], {})
                    _, end = self.cal.exit_window(day, ac, self.rules)
                    if self._now() >= end:
                        self.broker.cancel_entry(order)
            if self._now() >= self.cal.session_close(day):
                raise RuntimeError("Market closed with unsettled orders or positions; check Schwab before restarting")
            self._sleep(self.rules.poll_interval_seconds)

    def _only_overnight_holds(self, day: date) -> bool:
        """Every open position was bought today, may be held overnight, and
        today's exit window for it is over."""
        now = self._now()
        managed = self._managed_positions(day)
        return bool(managed) and all(plan.held_overnight and now >= plan.end for _, _, plan in managed)

    # -- helpers --------------------------------------------
    def _quote(self, osi_symbol: str) -> Quote:
        if self.quote_source is None:
            raise RuntimeError("engine has no quote_source; cannot run the exit loop")
        return self.quote_source(osi_symbol)

    def _wait_until(self, target: datetime, day: date | None = None) -> None:
        target = clock.ensure_aware(target)
        while True:
            remaining = (target - self._now()).total_seconds()
            if remaining <= 0:
                return
            if day is not None and (self.store.open_positions(self.mode.value)
                                    or self.store.orders_open(self.mode.value)):
                self.phase = "trading"
                self._exit_tick(day)
            self._sleep(min(remaining, max(self.rules.poll_interval_seconds, 1)))
