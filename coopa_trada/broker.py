"""Broker boundary.

Two implementations, one code path. Both write identical ``orders`` and
``fills`` rows so a DRY-RUN log diffs cleanly against a LIVE one. The shared
``_BrokerBase._submit`` builds the idempotency key, short-circuits on a hit,
and persists request then response JSON around the implementation call.

Simulated fills use the submitted ask/bid. Live fills use actual broker executions.

A live submit that fails without a definite answer from Schwab is never resent.
``SchwabBroker`` looks the order up among recently entered orders instead
(:meth:`SchwabBroker._recover`) and settles on PLACED, NOT_PLACED or UNKNOWN;
UNKNOWN halts new orders for that contract until a person resolves it.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Protocol, runtime_checkable

from . import clock

from .config import Mode
from .contracts import ResolvedContract
from .exits import ExitDecision, PositionState, Quote
from .idempotency import Intent, make_key
from .store import UNKNOWN_STATUS, Store

__all__ = ["OrderResult", "Broker", "SchwabBroker", "DryRunBroker", "SchwabQuoteSource",
           "OrderNotPlaced", "OrderOutcomeUnknown"]


class OrderNotPlaced(RuntimeError):
    """The submit failed and recovery confirmed Schwab never received the order."""


class OrderOutcomeUnknown(RuntimeError):
    """Recovery could not tell whether Schwab received the order; contract halted."""


class _AcceptedWithoutId(RuntimeError):
    """Schwab answered 2xx but gave no order ID: placed, but which order?"""

_OPEN_STATUS = "WORKING"
_FILLED_STATUS = "FILLED"


@dataclass
class OrderResult:
    idempotency_key: str
    broker_order_id: str | None
    status: str
    submitted_price: float | None
    mode: str
    raw_response: Any
    order_row_id: int | None = None
    deduplicated: bool = False


@runtime_checkable
class Broker(Protocol):
    def place_entry(
        self, contract: ResolvedContract, quantity: int, limit_price: float, *, signal_id: str, day: date
    ) -> OrderResult: ...

    def place_exit(
        self, position: PositionState, limit_price: float, decision: ExitDecision, *, signal_id: str, day: date
    ) -> OrderResult: ...

    def get_positions(self) -> list[dict]: ...

    def get_open_orders(self) -> list[dict]: ...


class _BrokerBase:
    mode: Mode

    def __init__(self, store: Store):
        self.store = store

    def _submit(
        self,
        *,
        intent: Intent,
        signal_id: str,
        day: date,
        osi_symbol: str,
        side: str,
        quantity: int,
        limit_price: float | None,
        request: Any,
        do_call: Callable[[], tuple[str, str | None, Any]],
        fill_price: float | None,
        entry_order_id: int | None = None,
        recover: Callable[[int, Exception, datetime], OrderResult | None] | None = None,
    ) -> OrderResult:
        with self.store.transaction():
            legacy_key = make_key(signal_id, intent, day)
            key = self.mode.value + ":" + legacy_key
            existing = self.store.find_order_by_idempotency_key(key)
            if existing is None:
                legacy = self.store.find_order_by_idempotency_key(legacy_key)
                if legacy is not None and legacy["mode"] == self.mode.value:
                    existing = legacy
            if intent is Intent.EXIT and entry_order_id is not None:
                prior_exit = self.store.conn.execute(
                    "SELECT * FROM orders WHERE intent='EXIT' AND mode=? AND entry_order_id=? ORDER BY id DESC LIMIT 1",
                    (self.mode.value, entry_order_id),
                ).fetchone()
                if prior_exit is not None:
                    existing = prior_exit
            if existing is not None:
                return OrderResult(
                    idempotency_key=key,
                    broker_order_id=existing["broker_order_id"],
                    status=existing["status"],
                    submitted_price=existing["limit_price"],
                    mode=existing["mode"],
                    raw_response={"deduplicated": True, "order_id": existing["id"]},
                    order_row_id=int(existing["id"]),
                    deduplicated=True,
                )

            order_id = self.store.record_order(
                signal_id=signal_id,
                intent=intent,
                idempotency_key=key,
                mode=self.mode.value,
                osi_symbol=osi_symbol,
                side=side,
                quantity=quantity,
                limit_price=limit_price,
                status="PENDING",
                request=request,
                entry_order_id=entry_order_id,
            )

        submitted_at = clock.now_utc()
        try:
            status, broker_order_id, response = do_call()
        except Exception as exc:
            # Never resubmit. An uncertain failure is looked up at the broker;
            # anything recovery declines keeps the strict ERROR path below.
            recovered = recover(order_id, exc, submitted_at) if recover is not None else None
            if recovered is not None:
                return recovered
            self.store.update_order_status(
                order_id, "ERROR", response={"error": repr(exc)}
            )
            raise

        self.store.update_order_status(
            order_id, status, broker_order_id=broker_order_id, response=response
        )

        if status == _FILLED_STATUS and fill_price is not None:
            self.store.record_fill(
                order_id=order_id,
                quantity=quantity,
                price=fill_price,
                mode=self.mode.value,
                raw=response,
            )

        return OrderResult(
            idempotency_key=key,
            broker_order_id=broker_order_id,
            status=status,
            submitted_price=limit_price,
            mode=self.mode.value,
            raw_response=response,
            order_row_id=order_id,
        )


class SchwabBroker(_BrokerBase):
    """Live orders via schwab-py.

    Entries: buy-to-open limit at the ask. Exits: sell-to-close limit at the
    bid. Order-builder / client method names are confirmed against the
    installed ``schwab-py`` — they appear only here.
    """

    mode = Mode.LIVE

    # Talks to Schwab: Engine.run refuses to start without a usable login.
    requires_schwab_token = True

    def __init__(self, client, account_hash: str, store: Store, ratelimiter, *,
                 recovery_lookups: int = 3, recovery_wait_seconds: float = 5.0,
                 sleep: Callable[[float], None] = _time.sleep):
        super().__init__(store)
        self.client = client
        self.account_hash = account_hash
        self.store.bind_live_account(account_hash)
        self.ratelimiter = ratelimiter
        # An order can take a moment to appear in Schwab's order list, so an
        # uncertain submit is looked up several times before giving up.
        self.recovery_lookups = recovery_lookups
        self.recovery_wait_seconds = recovery_wait_seconds
        self._sleep = sleep

    # -- helpers -------------------------------------------------
    def _rl(self):
        if self.ratelimiter is not None:
            self.ratelimiter.acquire()

    @staticmethod
    def _extract_order_id(response) -> str | None:
        # schwab-py returns an httpx.Response; the new order id is the last
        # path segment of the Location header.
        try:
            loc = response.headers.get("Location", "")
            return loc.rstrip("/").rsplit("/", 1)[-1] or None
        except Exception:
            return None

    # -- uncertain-submit recovery ---------------------------------
    @staticmethod
    def _classify(exc: Exception) -> str | None:
        """"rejected" (Schwab said no: keep the strict ERROR path), "not_sent"
        (the connection never opened, so no request left this computer), or
        "uncertain" (it may or may not have reached Schwab)."""
        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            return "rejected" if 400 <= code < 500 and code != 408 else "uncertain"
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return "not_sent"
        return "uncertain"

    def _matching_orders(self, order, submitted_at: datetime) -> list[dict]:
        """Schwab orders entered around the submit that match it exactly and
        are not already tracked locally."""
        self._rl()
        resp = self.client.get_orders_for_account(
            self.account_hash,
            from_entered_datetime=submitted_at - timedelta(minutes=5),
            to_entered_datetime=clock.now_utc() + timedelta(minutes=5),
        )
        _check_response(resp)
        data = resp.json()
        orders = data if isinstance(data, list) else (data or {}).get("orders", [])
        tracked = {str(r["broker_order_id"]) for r in self.store.conn.execute(
            "SELECT broker_order_id FROM orders WHERE broker_order_id IS NOT NULL")}
        matches = []
        for o in orders:
            legs = o.get("orderLegCollection") or []
            if len(legs) != 1 or str(o.get("orderId")) in tracked:
                continue
            leg = legs[0]
            try:
                same = (leg.get("instrument", {}).get("symbol") == order["osi_symbol"]
                        and leg.get("instruction") == order["side"]
                        and float(leg.get("quantity")) == order["quantity"]
                        and o.get("orderType") == "LIMIT"
                        and round(float(o.get("price")), 2) == round(order["limit_price"], 2))
            except (TypeError, ValueError):
                same = False
            if same:
                matches.append(o)
        return matches

    def _recover(self, order_id: int, exc: Exception, submitted_at: datetime) -> OrderResult | None:
        kind = "uncertain" if isinstance(exc, _AcceptedWithoutId) else self._classify(exc)
        if kind == "rejected":
            return None
        order = self.store.get_order(order_id)
        error = f"{type(exc).__name__}: {exc}"
        attempts = 1 if kind == "not_sent" else max(1, self.recovery_lookups)
        lookups: list[dict] = []
        matches: list[dict] = []
        for attempt in range(attempts):
            if attempt:
                self._sleep(self.recovery_wait_seconds)
            try:
                matches = self._matching_orders(order, submitted_at)
            except Exception as lookup_exc:  # the lookup failing proves nothing
                lookups.append({"attempt": attempt + 1, "error": repr(lookup_exc)})
                kind = "uncertain"
                continue
            lookups.append({"attempt": attempt + 1, "matches": [str(m.get("orderId")) for m in matches]})
            if matches:
                break
        detail = {"classified": kind, "lookups": lookups}

        if len(matches) == 1:
            found = matches[0]
            broker_order_id = str(found["orderId"])
            self.store.update_order_status(order_id, _OPEN_STATUS, broker_order_id=broker_order_id,
                                           response={"recovered_after": error})
            try:
                self.store.sync_order(order_id, found)
            except ValueError as sync_exc:  # the next status poll retries and reports it
                detail["sync_error"] = repr(sync_exc)
            self.store.record_incident(order_id=order_id, outcome="PLACED", error=error, detail=detail,
                                       resolution=f"found at Schwab as order {broker_order_id}")
            refreshed = self.store.get_order(order_id)
            return OrderResult(
                idempotency_key=order["idempotency_key"], broker_order_id=broker_order_id,
                status=refreshed["status"], submitted_price=order["limit_price"], mode=self.mode.value,
                raw_response=found, order_row_id=order_id)
        if not matches and kind == "not_sent":
            self.store.update_order_status(order_id, "NOT_PLACED", response={"error": error, **detail})
            self.store.record_incident(order_id=order_id, outcome="NOT_PLACED", error=error, detail=detail)
            raise OrderNotPlaced(
                f"{order['side']} {order['osi_symbol']} was not placed: the connection to Schwab never "
                f"opened ({error}). It was not resent.") from exc
        if len(matches) > 1:
            detail["ambiguous"] = True
        self.store.update_order_status(order_id, "UNKNOWN", response={"error": error, **detail})
        self.store.record_incident(order_id=order_id, outcome="UNKNOWN", error=error, detail=detail)
        raise OrderOutcomeUnknown(
            f"Cannot tell whether Schwab received {order['side']} {order['osi_symbol']} ({error}). "
            f"Trading in this contract is halted until someone checks Schwab and resolves it on the "
            f"Reconcile page.") from exc

    # -- Broker protocol -------------------------------------
    def place_entry(self, contract, quantity, limit_price, *, signal_id, day):
        from schwab.orders.options import option_buy_to_open_limit

        order_spec = option_buy_to_open_limit(contract.osi_symbol, quantity, f"{limit_price:.2f}")
        request = {
            "kind": "option_buy_to_open_limit",
            "symbol": contract.osi_symbol,
            "quantity": quantity,
            "limit_price": limit_price,
            "spec": order_spec.build() if hasattr(order_spec, "build") else str(order_spec),
        }

        def do_call():
            self._rl()
            resp = self.client.place_order(self.account_hash, order_spec)
            _check_response(resp)
            oid = self._extract_order_id(resp)
            if not oid:
                raise _AcceptedWithoutId("Schwab accepted an order without an order ID")
            return _OPEN_STATUS, oid, _response_summary(resp)

        return self._submit(
            intent=Intent.ENTRY,
            signal_id=signal_id,
            day=day,
            osi_symbol=contract.osi_symbol,
            side="BUY_TO_OPEN",
            quantity=quantity,
            limit_price=limit_price,
            request=request,
            do_call=do_call,
            fill_price=None,  # live fills arrive via reconcile / order status, not synthesized
            recover=self._recover,
        )

    def place_exit(self, position, limit_price, decision, *, signal_id, day):
        from schwab.orders.options import option_sell_to_close_limit

        order_spec = option_sell_to_close_limit(position.osi_symbol, position.quantity, f"{limit_price:.2f}")
        request = {
            "kind": "option_sell_to_close_limit",
            "symbol": position.osi_symbol,
            "quantity": position.quantity,
            "limit_price": limit_price,
            "decision": {"strategy": decision.strategy, "reason": decision.reason},
            "spec": order_spec.build() if hasattr(order_spec, "build") else str(order_spec),
        }

        def do_call():
            self._rl()
            resp = self.client.place_order(self.account_hash, order_spec)
            _check_response(resp)
            oid = self._extract_order_id(resp)
            if not oid:
                raise _AcceptedWithoutId("Schwab accepted an order without an order ID")
            return _OPEN_STATUS, oid, _response_summary(resp)

        return self._submit(
            intent=Intent.EXIT,
            entry_order_id=position.entry_order_id,
            signal_id=signal_id,
            day=day,
            osi_symbol=position.osi_symbol,
            side="SELL_TO_CLOSE",
            quantity=position.quantity,
            limit_price=limit_price,
            request=request,
            do_call=do_call,
            fill_price=None,
            recover=self._recover,
        )

    def cancel_entry(self, order) -> None:
        if order["mode"] != self.mode.value or order["intent"] != "ENTRY":
            raise ValueError("Can only cancel this mode's entry orders")
        self._rl()
        resp = self.client.cancel_order(order["broker_order_id"], self.account_hash)
        _check_response(resp)
        # Cancellation races with fills. Only the subsequent broker snapshot settles it.
        self.sync_orders()

    def sync_orders(self) -> None:
        for order in self.store.orders_open(self.mode.value):
            if order["status"] == UNKNOWN_STATUS:
                continue  # halted for a person to resolve; nothing to poll
            if not order["broker_order_id"]:
                raise RuntimeError("Unresolved local order without broker ID; reconcile manually before trading")
            self._rl()
            resp = self.client.get_order(order["broker_order_id"], self.account_hash)
            _check_response(resp)
            self.store.sync_order(order["id"], resp.json())

    def get_positions(self) -> list[dict]:
        self._rl()
        resp = self.client.get_account(self.account_hash, fields=self._position_fields())
        _check_response(resp)
        data = resp.json() if hasattr(resp, "json") else resp
        acct = (data or {}).get("securitiesAccount", data) or {}
        return list(acct.get("positions", []))

    def get_open_orders(self) -> list[dict]:
        self._rl()
        resp = self.client.get_orders_for_account(self.account_hash)
        _check_response(resp)
        data = resp.json() if hasattr(resp, "json") else resp
        orders = data if isinstance(data, list) else (data or {}).get("orders", [])
        open_statuses = {"WORKING", "PENDING_ACTIVATION", "QUEUED", "ACCEPTED", "AWAITING_MANUAL_REVIEW"}
        return [o for o in orders if str(o.get("status", "")).upper() in open_statuses]

    @staticmethod
    def _position_fields():
        try:
            from schwab.client import Client

            return Client.Account.Fields.POSITIONS
        except Exception:
            return "positions"


def _check_response(resp) -> None:
    if hasattr(resp, "raise_for_status"):
        resp.raise_for_status()
    elif hasattr(resp, "status_code") and not 200 <= resp.status_code < 300:
        raise RuntimeError(f"Schwab HTTP {resp.status_code}")


def _response_summary(resp) -> dict:
    """Best-effort JSON-able summary of an httpx.Response (or anything)."""
    out: dict[str, Any] = {}
    for attr in ("status_code",):
        if hasattr(resp, attr):
            out[attr] = getattr(resp, attr)
    if hasattr(resp, "headers"):
        try:
            out["headers"] = {k: v for k, v in resp.headers.items()}
        except Exception:
            pass
    if hasattr(resp, "json"):
        try:
            out["body"] = resp.json()
        except Exception:
            if hasattr(resp, "text"):
                out["body_text"] = resp.text
    return out or {"repr": repr(resp)}


class DryRunBroker(_BrokerBase):
    """No network. Writes the same rows as the live path.

    Buys fill at the ask, sells at the bid. The entry ask comes straight from
    the resolved contract; the exit bid comes from ``quote_source(osi_symbol)``.
    ``broker_order_id`` is synthesised with a ``DRY-`` prefix.
    """

    mode = Mode.DRY_RUN

    def __init__(self, store: Store, quote_source: Callable[[str], Quote] | None = None):
        super().__init__(store)
        self.quote_source = quote_source

    def place_entry(self, contract, quantity, limit_price, *, signal_id, day):
        fill_price = contract.ask  # buys fill at the ask, never mid
        request = {
            "kind": "DRY option_buy_to_open_limit",
            "symbol": contract.osi_symbol,
            "quantity": quantity,
            "limit_price": limit_price,
            "resolved": {
                "bid": contract.bid,
                "ask": contract.ask,
                "mid": contract.mid,
                "open_interest": contract.open_interest,
            },
        }

        def do_call():
            return (
                _FILLED_STATUS,
                None,
                {"simulated": True, "fill_price": fill_price, "rule": "buy@ask"},
            )

        result = self._submit(
            intent=Intent.ENTRY,
            signal_id=signal_id,
            day=day,
            osi_symbol=contract.osi_symbol,
            side="BUY_TO_OPEN",
            quantity=quantity,
            limit_price=limit_price,
            request=request,
            do_call=do_call,
            fill_price=fill_price,
        )
        self._stamp_dry_id(result)
        return result

    def place_exit(self, position, limit_price, decision, *, signal_id, day):
        fill_price = limit_price  # sells fill at the bid
        request = {
            "kind": "DRY option_sell_to_close_limit",
            "symbol": position.osi_symbol,
            "quantity": position.quantity,
            "limit_price": limit_price,
            "decision": {"strategy": decision.strategy, "reason": decision.reason},
        }

        def do_call():
            return (
                _FILLED_STATUS,
                None,
                {"simulated": True, "fill_price": fill_price, "rule": "sell@bid"},
            )

        result = self._submit(
            intent=Intent.EXIT,
            entry_order_id=position.entry_order_id,
            signal_id=signal_id,
            day=day,
            osi_symbol=position.osi_symbol,
            side="SELL_TO_CLOSE",
            quantity=position.quantity,
            limit_price=limit_price,
            request=request,
            do_call=do_call,
            fill_price=fill_price,
        )
        self._stamp_dry_id(result)
        return result

    def get_positions(self) -> list[dict]:
        rows = self.store.open_positions(self.mode.value)
        return [
            {
                "instrument": {"symbol": r["osi_symbol"]},
                "longQuantity": r["quantity"],
                "averagePrice": r["entry_price"],
                "source": "dry-run-local",
                "position_key": r["position_key"],
            }
            for r in rows
        ]

    def get_open_orders(self) -> list[dict]:
        return [dict(r) for r in self.store.orders_open(self.mode.value)]

    # -- helpers -------------------------------------------------
    def _quote(self, osi: str) -> Quote | None:
        if self.quote_source is None:
            return None
        return self.quote_source(osi)

    def _stamp_dry_id(self, result: OrderResult) -> None:
        if result.deduplicated or result.order_row_id is None:
            return
        dry_id = f"DRY-{result.order_row_id}"
        self.store.update_order_status(result.order_row_id, result.status, broker_order_id=dry_id)
        result.broker_order_id = dry_id


class SchwabQuoteSource:
    """Live option quotes for the exit loop, via schwab-py. Rate-limited."""

    # Talks to Schwab: Engine.run refuses to start without a usable login.
    requires_schwab_token = True

    def __init__(self, client, ratelimiter=None):
        self.client = client
        self.ratelimiter = ratelimiter

    def __call__(self, osi_symbol: str) -> Quote:
        if self.ratelimiter is not None:
            self.ratelimiter.acquire()
        resp = self.client.get_quote(osi_symbol)
        _check_response(resp)
        data = resp.json() if hasattr(resp, "json") else resp
        node = (data or {}).get(osi_symbol, {})
        q = node.get("quote", node)
        bid = float(q.get("bidPrice") or q.get("bid") or 0.0)
        ask = float(q.get("askPrice") or q.get("ask") or 0.0)
        mark = q.get("mark")
        mid = float(mark) if mark else round((bid + ask) / 2, 4)
        import math
        if not all(math.isfinite(v) and v > 0 for v in (bid, ask, mid)) or bid > ask:
            raise ValueError("Schwab returned an invalid option quote")
        return Quote(bid=bid, ask=ask, mid=mid)

    def register(self, osi_symbol: str, mid: float) -> None:  # parity with SyntheticQuoteSource
        return None
