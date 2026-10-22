"""Scheduler / reconcile / entry / exit loop.

All datetime math goes through ``clock`` and ``calendar_svc``. All broker and
client calls go through the rate limiter (inside the broker / resolver). The
engine itself holds no market-time literals.
"""

from __future__ import annotations

import math
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
    ExitDecision,
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
_CANCELED = {"CANCELED", "CANCELLED"}
# Statuses after which an order can no longer fill (see store._CLOSED_ORDER_STATUSES).
_DONE_STATUSES = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "REPLACED", "EXPIRED", "NOT_PLACED"}


def _canceled_to_reprice(order) -> bool:
    """A sell order this bot canceled itself, to replace it at a lower price."""
    return order["status"] in _CANCELED and order["cancel_requested_at_utc"] is not None


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
        if hasattr(broker, "now"):
            broker.now = now  # order times on the same clock as the exit rules
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
        """Buy today's picks, highest delta first, while each fits the budget.

        Every pick goes through the same gates as before (expiration, exit
        deadline, delta filter, already held, resolvable, per-pick cap, halted
        contract). The survivors are ranked by |delta| (``rank_delta_source``;
        picks without one go last, in list order) and bought in that order while
        each one's cost at ask x 100 fits what is left of ``total_budget`` after
        every open position and working buy order. A pick that does not fit is
        skipped (and logged); cheaper picks after it are still considered.
        Every skip is recorded with its reason.
        """
        signals = self.source.fetch(day)
        self.log.info("fetched %d signal(s) for %s", len(signals), day.isoformat())
        candidates = []
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
            positions = self.store.open_positions(self.mode.value)
            pending = [r for r in self.store.orders_open(self.mode.value) if r["intent"] == "ENTRY"]
            if signal.id in {r["signal_id"] for r in positions} | {r["signal_id"] for r in pending}:
                continue  # already bought or being bought
            try:
                contract = self.resolver.resolve(signal)
            except ContractUnderspecified as exc:
                self._record_skip(signal, day, "underspecified", f"underspecified ({exc})", {})
                continue
            except ContractNotTradable as exc:
                self._record_skip(signal, day, "not_tradable", f"not tradable ({exc})", {})
                continue

            # Fixed quantity per pick (1). FixedQuantitySizer returns 0 when that
            # pick's cost at the ask would exceed max_dollars_per_position -- skip it.
            qty = self.sizer.size(signal, contract, self.rules)
            if qty <= 0:
                self._record_skip(signal, day, "spending_cap",
                    f"1 contract exceeds spending cap (ask={contract.ask:.4f}, cap={self.rules.max_dollars_per_position:.2f})",
                    {"ask": contract.ask, "mid": contract.mid, "cap": self.rules.max_dollars_per_position})
                continue
            if contract.osi_symbol in self.store.halted_symbols(self.mode.value):
                self._record_skip(signal, day, "halted",
                    f"{contract.osi_symbol} is halted: an earlier order's outcome is unknown",
                    {"osi_symbol": contract.osi_symbol})
                continue
            candidates.append((signal, contract, qty, self._rank_delta(signal, contract)))

        # Highest |delta| first; stable, so ties and missing deltas keep list order.
        candidates.sort(key=lambda c: (c[3] is None, -(c[3] or 0.0)))
        budget_cents = round(self.rules.total_budget * 100)
        for rank, (signal, contract, qty, delta) in enumerate(candidates, start=1):
            cost_cents = qty * 100 * math.ceil(contract.ask * 100 - 1e-6)
            left_cents = budget_cents - self.store.committed_cents(self.mode.value)
            detail = {"rank": rank, "delta": delta, "delta_source": self.rules.rank_delta_source,
                      "ask": contract.ask, "cost": cost_cents / 100, "budget_left": left_cents / 100,
                      "total_budget": self.rules.total_budget}
            if cost_cents > left_cents:
                self._record_skip(signal, day, "budget",
                    f"does not fit the total budget: costs ${cost_cents / 100:.2f} at the ask, "
                    f"${max(left_cents, 0) / 100:.2f} of ${self.rules.total_budget:.2f} left "
                    f"(rank {rank} by {self.rules.rank_delta_source} delta)", detail)
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
                self.quote_source.register(contract.osi_symbol, contract.mid)
            self.log.info(
                "ENTRY %s %s x%d @ ask %.2f (rank %d, %s delta %s) -> status=%s broker_id=%s%s",
                signal.id, contract.osi_symbol, qty, contract.ask, rank, self.rules.rank_delta_source,
                "n/a" if delta is None else f"{delta:g}", result.status,
                result.broker_order_id, " (dedup)" if result.deduplicated else "",
            )

    def _rank_delta(self, signal, contract) -> float | None:
        """|delta| used to rank a pick, from the configured source, or None."""
        value = (contract.delta if self.rules.rank_delta_source == "schwab_live"
                 else signal.signal_delta_tvt)
        if value is None or not math.isfinite(value):
            return None
        return abs(value)

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
            exits = self.store.exit_orders(self.mode.value, state.entry_order_id)
            self._check_exit_order(state, signal_id, exits)
            if state.osi_symbol in halted:
                self.log.warning("hold %s: halted until a person resolves an unknown order", state.osi_symbol)
                continue
            quote = self._quote(state.osi_symbol)
            update_peak(state, quote)
            self.store.update_peak(state.entry_order_id, state.session_peak_mid)
            if exits:
                # The sell decision was made; only its price can still change.
                acted += self._reprice(state, signal_id, exits, quote, day)
                continue
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

    def _check_exit_order(self, state: PositionState, signal_id: str, exits=None) -> None:
        """Alert when this position's exit order failed, or is not filling."""
        if exits is None:
            exits = self.store.exit_orders(self.mode.value, state.entry_order_id)
        if not exits:
            return
        row = exits[-1]
        what = contract_text(state.osi_symbol)
        if _canceled_to_reprice(row):
            return  # canceled by the bot itself; its replacement follows
        if row["status"] in _FAILED_EXIT_STATUSES:
            self.alerter.alert(
                "exit_failed", "Sell order failed",
                f"The sell order for {what} ended as {row['status']} and the position is still open. "
                f"The bot will not send another. Check Schwab and sell it there if you want out.",
                key=f"exit:{signal_id}")
        elif row["status"] not in ("UNKNOWN", "FILLED"):
            # Measured from the first sell order: repricing does not restart the clock.
            age = self._now() - clock.parse_iso(exits[0]["created_at_utc"])
            if age >= EXIT_UNFILLED_ALERT:
                minutes = int(age.total_seconds() // 60)
                lowered = row["reprice_attempt"]
                self.alerter.alert(
                    "exit_unfilled", "Sell order not filling",
                    f"The sell order for {what} has been waiting {minutes} minutes without filling. "
                    f"The bot has made {lowered} price change{'s' if lowered != 1 else ''} "
                    f"(at most {self.rules.exit_reprice_max_attempts}, real orders only). Check it in Schwab.",
                    key=f"exit-unfilled:{signal_id}")

    # -- exit repricing ---------------------------------------
    def _reprice(self, state: PositionState, signal_id: str, exits, quote: Quote, day: date) -> int:
        """Cooper: a sell that has not filled within ``exit_reprice_after_seconds``
        is canceled and replaced at the current bid, at most
        ``exit_reprice_max_attempts`` times. Returns 1 when a replacement was sent.

        Race safety: a replacement is sent only after Schwab reports the old
        order CANCELED (no further fills are then possible), and only for the
        quantity Schwab's executions show is still held. A fill that lands while
        the cancel is in flight therefore shows up as FILLED (nothing to replace)
        or as a partial fill (only the rest is sold). Simulated sells fill at
        once, so this applies to real orders only. Never a market order.
        """
        latest = exits[-1]
        if self.mode is not Mode.LIVE or self.rules.exit_reprice_max_attempts <= 0:
            return 0
        if _canceled_to_reprice(latest):
            return self._place_replacement(state, signal_id, latest, day)  # e.g. after a restart
        if latest["status"] in _DONE_STATUSES or latest["status"] == "UNKNOWN":
            return 0
        now = self._now()
        after = timedelta(seconds=self.rules.exit_reprice_after_seconds)
        if latest["cancel_requested_at_utc"] is not None:
            if now - clock.parse_iso(latest["cancel_requested_at_utc"]) >= after:
                # Schwab never confirmed the cancel; ask again on a later tick.
                self.store.set_cancel_requested(latest["id"], None)
            return 0
        if now - clock.parse_iso(latest["created_at_utc"]) < after:
            return 0
        what = contract_text(state.osi_symbol)
        if latest["reprice_attempt"] >= self.rules.exit_reprice_max_attempts:
            self.alerter.alert(
                "exit_reprice_exhausted", "Sell order still not filling",
                f"The sell order for {what} is still not filled after {latest['reprice_attempt']} "
                f"price changes, the most the bot makes. It stays at its last price. "
                f"Check Schwab and sell it there if you want out.",
                key=f"exit-exhausted:{signal_id}")
            return 0
        if not quote.bid < latest["limit_price"] - 1e-9:
            return 0  # only ever lower the price; a bid at or above the order should fill it
        try:
            self.broker.cancel_exit_for_reprice(latest)
        except Exception as exc:  # noqa: BLE001 - the order stays as it is; retried later
            self.log.error("EXIT %s: could not cancel the sell order to reprice it: %r", signal_id, exc)
            return 0
        refreshed = self.store.get_order(latest["id"])
        if _canceled_to_reprice(refreshed):
            return self._place_replacement(state, signal_id, refreshed, day)
        self.log.info("EXIT %s: cancel requested for repricing; Schwab now shows %s",
                      signal_id, refreshed["status"])
        return 0

    def _place_replacement(self, state: PositionState, signal_id: str, canceled, day: date) -> int:
        row = next((r for r in self.store.open_positions(self.mode.value)
                    if r["order_id"] == state.entry_order_id), None)
        if row is None:
            return 0  # everything sold before the cancel landed
        remaining = int(row["quantity"])
        quote = self._quote(state.osi_symbol)  # the current price, just before sending
        attempt = int(canceled["reprice_attempt"]) + 1
        decision = ExitDecision(
            strategy="Reprice",
            reason=(f"sell order unfilled after {self.rules.exit_reprice_after_seconds}s at "
                    f"{canceled['limit_price']:.2f}; replaced at bid {quote.bid:.2f} "
                    f"(attempt {attempt} of {self.rules.exit_reprice_max_attempts})"),
            price=quote.bid, at=self._now(),
            meta={"replaces_order_id": canceled["id"], "attempt": attempt, "remaining": remaining})
        try:
            result = self.broker.place_exit(
                replace(state, quantity=remaining), quote.bid, decision, signal_id=signal_id, day=day,
                replaces_order_id=canceled["id"], reprice_attempt=attempt)
        except (OrderNotPlaced, OrderOutcomeUnknown) as exc:
            self.log.error("EXIT %s: %s", signal_id, exc)
            self._alert_order_problem("sell", state.osi_symbol, exc, key=f"exit:{signal_id}")
            return 0
        if result.deduplicated:
            return 0
        self.store.record_exit_event(
            position_key=position_key(signal_id, state.osi_symbol), order_id=result.order_row_id,
            strategy=decision.strategy, reason=decision.reason, trigger_price=quote.bid,
            peak_mid=state.session_peak_mid,
            detail={"decision_meta": decision.meta, "quote": quote.__dict__, "mode": self.mode.value})
        self.log.info("EXIT %s %s x%d repriced to bid %.2f (attempt %d) -> status=%s",
                      signal_id, state.osi_symbol, remaining, quote.bid, attempt, result.status)
        return 1

    def _next_sleep(self) -> float:
        """The poll interval, shortened so a due reprice is not up to a poll late."""
        wait = float(self.rules.poll_interval_seconds)
        if self.mode is not Mode.LIVE or self.rules.exit_reprice_max_attempts <= 0:
            return wait
        after = timedelta(seconds=self.rules.exit_reprice_after_seconds)
        for order in self.store.orders_open(self.mode.value):
            if order["intent"] == "EXIT" and order["status"] != "UNKNOWN":
                due = (clock.parse_iso(order["created_at_utc"]) + after - self._now()).total_seconds()
                wait = min(wait, max(due, 1.0))
        return wait

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
            self._sleep(self._next_sleep())

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
