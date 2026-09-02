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
    ) -> int:
        """Insert a PENDING (or given-status) order row with its request JSON.

        Returns the new order id. On an idempotency-key collision the existing
        row's id is returned instead — the caller should have checked first.
        """
        created = _iso(created_at or clock.now_utc())
        try:
            cur = self.conn.execute(
                """
                INSERT INTO orders
                    (signal_id, intent, idempotency_key, mode, osi_symbol, side,
                     quantity, limit_price, status, created_at_utc, request_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def orders_open(self) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in _CLOSED_ORDER_STATUSES)
        return self.conn.execute(
            f"SELECT * FROM orders WHERE status NOT IN ({placeholders}) ORDER BY id",
            tuple(_CLOSED_ORDER_STATUSES),
        ).fetchall()

    def open_positions(self) -> list[sqlite3.Row]:
        """Entry orders that have a fill and no offsetting exit fill.

        Each row exposes ``entry_price`` / ``entry_at_utc`` from the entry fill
        plus a synthetic ``position_key``.
        """
        return self.conn.execute(
            """
            SELECT
                o.id                AS order_id,
                o.signal_id         AS signal_id,
                o.osi_symbol        AS osi_symbol,
                o.quantity          AS quantity,
                o.limit_price       AS limit_price,
                o.mode              AS mode,
                f.price             AS entry_price,
                f.filled_at_utc     AS entry_at_utc,
                (o.signal_id || ':' || o.osi_symbol) AS position_key
            FROM orders o
            JOIN fills f ON f.order_id = o.id
            WHERE o.intent = 'ENTRY'
              AND NOT EXISTS (
                    SELECT 1
                    FROM orders xo
                    JOIN fills xf ON xf.order_id = xo.id
                    WHERE xo.intent = 'EXIT'
                      AND xo.signal_id = o.signal_id
                      AND xo.osi_symbol = o.osi_symbol
              )
            ORDER BY o.id
            """
        ).fetchall()

    # -- transactions ---------------------------------------------
    def transaction(self):
        return _Tx(self.conn)


class _Tx:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN")
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
