"""SQLite trade log.

The log alone must answer: *what did we buy, at what price, when, and what
did we sell it for.* Every broker call persists its full request and response
as JSON text. A test reconstructs a complete round trip from the DB only.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import clock
from .idempotency import Intent

__all__ = ["Store", "position_key"]

_CLOSED_ORDER_STATUSES = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "REPLACED", "EXPIRED"}

_DDL = """
CREATE TABLE IF NOT EXISTS signals (
    id                TEXT PRIMARY KEY,
    underlying        TEXT NOT NULL,
    right             TEXT NOT NULL,
    strike            REAL,
    expiration        TEXT,
    quantity          INTEGER NOT NULL,
    asset_class       TEXT NOT NULL,
    received_at_utc   TEXT NOT NULL,
    raw               TEXT NOT NULL,
    ingested_at_utc   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id         TEXT NOT NULL REFERENCES signals(id),
    intent            TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL UNIQUE,
    mode              TEXT NOT NULL,
    osi_symbol        TEXT NOT NULL,
    side              TEXT NOT NULL,
    quantity          INTEGER NOT NULL,
    limit_price       REAL,
    status            TEXT NOT NULL,
    broker_order_id   TEXT,
    created_at_utc    TEXT NOT NULL,
    request_json      TEXT NOT NULL,
    response_json     TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id          INTEGER NOT NULL REFERENCES orders(id),
    quantity          INTEGER NOT NULL,
    price             REAL NOT NULL,
    filled_at_utc     TEXT NOT NULL,
    mode              TEXT NOT NULL,
    raw_json          TEXT
);

CREATE TABLE IF NOT EXISTS exit_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    position_key      TEXT NOT NULL,
    order_id          INTEGER REFERENCES orders(id),
    strategy          TEXT NOT NULL,
    reason            TEXT NOT NULL,
    trigger_price     REAL,
    peak_mid          REAL,
    created_at_utc    TEXT NOT NULL,
    detail_json       TEXT
);

CREATE TABLE IF NOT EXISTS skip_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL REFERENCES signals(id),
    trading_day TEXT NOT NULL,
    mode TEXT NOT NULL,
    category TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_orders_signal ON orders(signal_id);
CREATE INDEX IF NOT EXISTS ix_fills_order ON fills(order_id);
"""


def position_key(signal_id: str, osi_symbol: str) -> str:
    return f"{signal_id}:{osi_symbol}"


def _iso(dt: datetime) -> str:
    return clock.iso_utc(clock.ensure_aware(dt))


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    # -- lifecycle ----------------------------------------------------
    @classmethod
    def open(cls, path: str | Path) -> "Store":
        conn = sqlite3.connect(
            str(path),
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # autocommit; we manage transactions explicitly
        )
        store = cls(conn)
        store._migrate()
        return store

    def _migrate(self) -> None:
        self.conn.executescript(_DDL)
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS position_peaks (
                order_id INTEGER PRIMARY KEY REFERENCES orders(id),
                peak_mid REAL NOT NULL
            );
        """)

        with self.transaction():
            columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(orders)")}
            if "entry_order_id" not in columns:
                self.conn.execute("ALTER TABLE orders ADD COLUMN entry_order_id INTEGER REFERENCES orders(id)")
                # Legacy logs had no entry link. Associate each exit with its most
                # recent preceding entry in the same mode, preserving prior runs.
                self.conn.execute("""
                    UPDATE orders AS x SET entry_order_id = (
                        SELECT e.id FROM orders e WHERE e.intent='ENTRY'
                        AND e.mode=x.mode AND e.signal_id=x.signal_id
                        AND e.osi_symbol=x.osi_symbol AND e.id<x.id
                        ORDER BY e.id DESC LIMIT 1
                    ) WHERE x.intent='EXIT'
                """)

    def bind_live_account(self, account_hash: str) -> None:
        self.conn.execute("CREATE TABLE IF NOT EXISTS broker_account (id INTEGER PRIMARY KEY CHECK(id=1), account_hash TEXT NOT NULL)")
        with self.transaction():
            row = self.conn.execute("SELECT account_hash FROM broker_account WHERE id=1").fetchone()
            if row is not None and row[0] != account_hash:
                raise ValueError("This database belongs to another Schwab account; use a separate database")
            if row is None:
                if self.conn.execute("SELECT 1 FROM orders WHERE mode='LIVE' LIMIT 1").fetchone():
                    raise ValueError("Legacy live database has no verified account binding; reconcile it manually before migration")
                self.conn.execute("INSERT INTO broker_account VALUES (1, ?)", (account_hash,))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes -----------------------------------------------------
    def record_signal(self, signal) -> None:
        """Idempotent on the signal id."""
        self.conn.execute(
            """
            INSERT OR IGNORE INTO signals
                (id, underlying, right, strike, expiration, quantity, asset_class,
                 received_at_utc, raw, ingested_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.id,
                signal.underlying,
                signal.right.value,
                signal.strike,
                signal.expiration.isoformat() if signal.expiration else None,
                signal.quantity,
                signal.asset_class.value,
                _iso(signal.received_at),
                signal.raw,
                _iso(clock.now_utc()),
            ),
        )

    def record_order(
        self,
        *,
        signal_id: str,
        intent: Intent,
        idempotency_key: str,
        mode: str,
        osi_symbol: str,
        side: str,
        quantity: int,
        limit_price: float | None,
        status: str,
        request: Any,
        created_at: datetime | None = None,
        entry_order_id: int | None = None,
    ) -> int:
        """Insert a PENDING (or given-status) order row with its request JSON.

        Returns the new order id. On an idempotency-key collision the existing
        row's id is returned instead — the caller should have checked first.
        """
        created = _iso(created_at or clock.now_utc())
        if intent == Intent.EXIT and entry_order_id is None:
            candidates = self.conn.execute(
                "SELECT id FROM orders WHERE intent='ENTRY' AND signal_id=? AND osi_symbol=? AND mode=? ORDER BY id DESC LIMIT 1",
                (signal_id, osi_symbol, mode),
            ).fetchone()
            entry_order_id = candidates["id"] if candidates else None
        if entry_order_id is not None:
            entry = self.get_order(entry_order_id)
            if entry is None or entry["intent"] != "ENTRY" or (entry["signal_id"], entry["osi_symbol"], entry["mode"]) != (signal_id, osi_symbol, mode):
                raise ValueError("Exit must reference its own mode, signal and contract entry")
        try:
            cur = self.conn.execute(
                """
                INSERT INTO orders
                    (signal_id, intent, idempotency_key, mode, osi_symbol, side,
                     quantity, limit_price, status, created_at_utc, request_json, entry_order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id,
                    intent.value if isinstance(intent, Intent) else str(intent),
                    idempotency_key,
                    mode,
                    osi_symbol,
                    side,
                    quantity,
                    limit_price,
                    status,
                    created,
                    _json(request),
                    entry_order_id,
                ),
            )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            existing = self.find_order_by_idempotency_key(idempotency_key)
            if existing is None:
                raise
            return int(existing["id"])

    def update_order_status(
        self,
        order_id: int,
        status: str,
        *,
        broker_order_id: str | None = None,
        response: Any | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE orders
               SET status = ?,
                   broker_order_id = COALESCE(?, broker_order_id),
                   response_json = COALESCE(?, response_json)
             WHERE id = ?
            """,
            (
                status,
                broker_order_id,
                _json(response) if response is not None else None,
                order_id,
            ),
        )

    def record_fill(
        self,
        *,
        order_id: int,
        quantity: int,
        price: float,
        mode: str,
        filled_at: datetime | None = None,
        raw: Any | None = None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO fills (order_id, quantity, price, filled_at_utc, mode, raw_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                quantity,
                price,
                _iso(filled_at or clock.now_utc()),
                mode,
                _json(raw) if raw is not None else None,
            ),
        )
        return int(cur.lastrowid)

    def record_exit_event(
        self,
        *,
        position_key: str,
        strategy: str,
        reason: str,
        order_id: int | None = None,
        trigger_price: float | None = None,
        peak_mid: float | None = None,
        detail: Any | None = None,
        created_at: datetime | None = None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO exit_events
                (position_key, order_id, strategy, reason, trigger_price, peak_mid,
                 created_at_utc, detail_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position_key,
                order_id,
                strategy,
                reason,
                trigger_price,
                peak_mid,
                _iso(created_at or clock.now_utc()),
                _json(detail) if detail is not None else None,
            ),
        )
        return int(cur.lastrowid)

    def record_skip(self, *, signal_id, day, mode, category, reason, detail) -> None:
        self.conn.execute(
            """INSERT INTO skip_events
               (signal_id, trading_day, mode, category, reason, detail_json, created_at_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (signal_id, day.isoformat(), mode, category, reason, _json(detail), _iso(clock.now_utc())),
        )

    def skip_report(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            """SELECT k.*, s.underlying FROM skip_events k
               JOIN signals s ON s.id=k.signal_id ORDER BY k.id DESC"""
        )]

    # -- reads ----------------------------------------------------
    def find_order_by_idempotency_key(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM orders WHERE idempotency_key = ?", (key,)
        ).fetchone()

    def get_order(self, order_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()

    def fills_for_order(self, order_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM fills WHERE order_id = ? ORDER BY id", (order_id,)
        ).fetchall()

    def orders_open(self, mode: str | None = None) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in _CLOSED_ORDER_STATUSES)
        return self.conn.execute(
            f"SELECT * FROM orders WHERE status NOT IN ({placeholders}) AND (? IS NULL OR mode = ?) ORDER BY id",
            (*_CLOSED_ORDER_STATUSES, mode, mode),
        ).fetchall()

    def open_positions(self, mode: str | None = None) -> list[sqlite3.Row]:
        """Remaining filled quantity, segregated by mode and entry order."""
        return self.conn.execute(
            """
            WITH totals AS (
                SELECT order_id, SUM(quantity) AS qty,
                       SUM(quantity * price) / SUM(quantity) AS price,
                       MIN(filled_at_utc) AS at_utc FROM fills GROUP BY order_id
            )
            SELECT * FROM (SELECT o.id AS order_id, o.signal_id, o.osi_symbol, o.mode,
                   o.limit_price, f.price AS entry_price, f.at_utc AS entry_at_utc,
                   f.qty - COALESCE((
                       SELECT SUM(xf.qty) FROM orders xo JOIN totals xf ON xf.order_id = xo.id
                       WHERE xo.intent = 'EXIT' AND xo.entry_order_id = o.id AND xo.mode = o.mode
                   ), 0) AS quantity,
                   o.signal_id || ':' || o.osi_symbol AS position_key,
                   COALESCE(p.peak_mid, f.price) AS peak_mid
            FROM orders o JOIN totals f ON f.order_id = o.id
            LEFT JOIN position_peaks p ON p.order_id = o.id
            WHERE o.intent = 'ENTRY' AND (? IS NULL OR o.mode = ?)) WHERE quantity > 0
            ORDER BY order_id
            """, (mode, mode)
        ).fetchall()

    def update_peak(self, order_id: int, mid: float) -> None:
        self.conn.execute(
            """INSERT INTO position_peaks VALUES (?, ?)
               ON CONFLICT(order_id) DO UPDATE SET peak_mid = MAX(peak_mid, excluded.peak_mid)""",
            (order_id, mid),
        )

    def sync_order(self, order_id: int, payload: dict) -> None:
        """Atomically replace an order's execution snapshot; repeated polls cannot duplicate fills."""
        import math
        order = self.get_order(order_id)
        legs = payload.get("orderLegCollection", [])
        if len(legs) != 1 or legs[0].get("instrument", {}).get("symbol") != order["osi_symbol"]:
            raise ValueError("broker order contract does not match local single-leg order")
        if legs[0].get("instruction") != order["side"]:
            raise ValueError("broker order instruction does not match local order")
        executions = []
        for activity in payload.get("orderActivityCollection", []):
            if activity.get("activityType") != "EXECUTION":
                continue
            for leg in activity.get("executionLegs", []):
                if leg.get("legId") != legs[0].get("legId"):
                    raise ValueError("execution leg mismatch")
                qty, price = float(leg["quantity"]), float(leg["price"])
                if not math.isfinite(qty) or not qty.is_integer() or qty <= 0 or not math.isfinite(price) or price <= 0:
                    raise ValueError("invalid execution quantity or price")
                executions.append((int(qty), price, clock.parse_iso(leg["time"]), leg))
        total = sum(e[0] for e in executions)
        if total != float(payload.get("filledQuantity", 0)) or total > order["quantity"]:
            raise ValueError("incomplete or inconsistent broker executions")
        if payload.get("status") == "FILLED" and total != order["quantity"]:
            raise ValueError("FILLED order is missing executions")
        with self.transaction():
            prior = sum(r["quantity"] for r in self.fills_for_order(order_id))
            if total < prior:
                raise ValueError("broker execution snapshot regressed")
            self.conn.execute("DELETE FROM fills WHERE order_id = ?", (order_id,))
            for qty, price, at, raw in executions:
                self.record_fill(order_id=order_id, quantity=qty, price=price,
                                 mode=order["mode"], filled_at=at, raw=raw)
            self.update_order_status(order_id, payload["status"], response=payload)

    def position_report(self) -> list[dict]:
        """One row per entry, joined to its exit (if any) -- the trade-log view.

        Read-only. Answers, per position: what was bought, the contract, entry
        time/price, exit time/price, realized P&L, and why it exited. Open
        positions have ``exit_*`` fields as ``None`` and ``is_open = True``.
        """
        rows = self.conn.execute(
            """
            WITH execution_totals AS (
                SELECT order_id, SUM(quantity) AS quantity,
                       SUM(quantity * price) / SUM(quantity) AS price,
                       MIN(filled_at_utc) AS filled_at_utc, MIN(id) AS id
                FROM fills GROUP BY order_id
            )
            SELECT
                e.signal_id          AS signal_id,
                e.osi_symbol         AS osi_symbol,
                e.mode               AS mode,
                s.underlying         AS underlying,
                ef.quantity          AS quantity,
                ef.price             AS entry_price,
                ef.filled_at_utc     AS entry_at_utc,
                xf.price             AS exit_price,
                xf.quantity          AS exited_quantity,
                xf.filled_at_utc     AS exit_at_utc,
                ev.strategy          AS exit_strategy,
                ev.reason            AS exit_reason
            FROM orders e
            JOIN execution_totals ef ON ef.order_id = e.id
            LEFT JOIN signals s ON s.id = e.signal_id
            LEFT JOIN orders xo
                   ON xo.intent = 'EXIT' AND xo.entry_order_id = e.id AND xo.mode = e.mode
            LEFT JOIN execution_totals xf ON xf.order_id = xo.id
            LEFT JOIN exit_events ev
                   ON ev.order_id = xo.id AND ev.position_key = (e.signal_id || ':' || e.osi_symbol)
                  AND ev.id = (
                        SELECT MAX(id) FROM exit_events
                        WHERE order_id = xo.id
                  )
            WHERE e.intent = 'ENTRY'
            ORDER BY ef.id
            """
        ).fetchall()

        out: list[dict] = []
        for r in rows:
            d = dict(r)
            d["position_key"] = position_key(d["signal_id"], d["osi_symbol"])
            d["remaining_quantity"] = d["quantity"] - (d["exited_quantity"] or 0)
            d["is_open"] = d["remaining_quantity"] > 0
            if d["exit_price"] is not None and d["entry_price"] is not None:
                d["realized_pnl"] = round(
                    (d["exit_price"] - d["entry_price"]) * d["exited_quantity"] * 100, 2
                )
            else:
                d["realized_pnl"] = None
            out.append(d)
        return out

    # -- transactions ---------------------------------------------
    def transaction(self):
        return _Tx(self.conn)


class _Tx:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


def iter_rows(rows: Iterable[sqlite3.Row]) -> list[dict]:
    """Convenience for tests / reports: rows -> list of plain dicts."""
    return [dict(r) for r in rows]
