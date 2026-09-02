"""Broker boundary.

Two implementations, one code path. Both write identical ``orders`` and
``fills`` rows so a DRY-RUN log diffs cleanly against a LIVE one. The shared
``_BrokerBase._submit`` builds the idempotency key, short-circuits on a hit,
and persists request then response JSON around the implementation call.

Fills — in both modes — are buys at the ask and sells at the bid. Never mid.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Protocol, runtime_checkable

from .config import Mode
from .contracts import ResolvedContract
from .exits import ExitDecision, PositionState, Quote
from .idempotency import Intent, make_key
from .store import Store

__all__ = ["OrderResult", "Broker", "SchwabBroker", "DryRunBroker", "SchwabQuoteSource"]

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
    ) -> OrderResult:
        key = make_key(signal_id, intent, day)
        existing = self.store.find_order_by_idempotency_key(key)
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
        )

        try:
            status, broker_order_id, response = do_call()
        except Exception as exc:  # persist the failure, then re-raise
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

    def __init__(self, client, account_hash: str, store: Store, ratelimiter):
        super().__init__(store)
        self.client = client
        self.account_hash = account_hash
        self.ratelimiter = ratelimiter

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

    # -- Broker protocol -------------------------------------
    def place_entry(self, contract, quantity, limit_price, *, signal_id, day):
        from schwab.orders.options import option_buy_to_open_limit

        order_spec = option_buy_to_open_limit(contract.osi_symbol, quantity, limit_price)
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
            return _OPEN_STATUS, self._extract_order_id(resp), _response_summary(resp)

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
        )

    def place_exit(self, position, limit_price, decision, *, signal_id, day):
        from schwab.orders.options import option_sell_to_close_limit

        order_spec = option_sell_to_close_limit(position.osi_symbol, position.quantity, limit_price)
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
            return _OPEN_STATUS, self._extract_order_id(resp), _response_summary(resp)

        return self._submit(
            intent=Intent.EXIT,
            signal_id=signal_id,
            day=day,
            osi_symbol=position.osi_symbol,
            side="SELL_TO_CLOSE",
            quantity=position.quantity,
            limit_price=limit_price,
            request=request,
            do_call=do_call,
            fill_price=None,
        )

    def get_positions(self) -> list[dict]:
        self._rl()
        resp = self.client.get_account(self.account_hash, fields=self._position_fields())
        data = resp.json() if hasattr(resp, "json") else resp
        acct = (data or {}).get("securitiesAccount", data) or {}
        return list(acct.get("positions", []))

    def get_open_orders(self) -> list[dict]:
        self._rl()
        resp = self.client.get_orders_for_account(self.account_hash)
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
        quote = self._quote(position.osi_symbol)
        fill_price = quote.bid if quote is not None else limit_price  # sells fill at the bid
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
        rows = self.store.open_positions()
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
        return [dict(r) for r in self.store.orders_open()]

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

    def __init__(self, client, ratelimiter=None):
        self.client = client
        self.ratelimiter = ratelimiter

    def __call__(self, osi_symbol: str) -> Quote:
        if self.ratelimiter is not None:
            self.ratelimiter.acquire()
        resp = self.client.get_quote(osi_symbol)
        data = resp.json() if hasattr(resp, "json") else resp
        node = (data or {}).get(osi_symbol, {})
        q = node.get("quote", node)
        bid = float(q.get("bidPrice") or q.get("bid") or 0.0)
        ask = float(q.get("askPrice") or q.get("ask") or 0.0)
        mark = q.get("mark")
        mid = float(mark) if mark else round((bid + ask) / 2, 4)
        return Quote(bid=bid, ask=ask, mid=mid)

    def register(self, osi_symbol: str, mid: float) -> None:  # parity with SyntheticQuoteSource
        return None
