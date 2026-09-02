"""Idempotency keys for orders.

Every order — entry or exit — carries a key derived from a canonical string.
The same (signal, intent, trading day) always yields the same key, so a retry
or a double-run collides on the ``orders.idempotency_key`` UNIQUE constraint
instead of placing a second order.
"""

from __future__ import annotations

import hashlib
from datetime import date
from enum import Enum

__all__ = ["Intent", "canonical_string", "make_key"]


class Intent(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


def _day_str(day: date | str) -> str:
    if isinstance(day, str):
        # Accept an already-canonical YYYY-MM-DD string.
        date.fromisoformat(day)  # validation only
        return day
    return day.isoformat()


def canonical_string(signal_id: str, intent: Intent, day: date | str) -> str:
    intent_value = intent.value if isinstance(intent, Intent) else str(intent)
    return f"{signal_id}|{intent_value}|{_day_str(day)}"


def make_key(signal_id: str, intent: Intent, day: date | str) -> str:
    """sha256 hex digest of ``signal_id|INTENT|YYYY-MM-DD``."""
    return hashlib.sha256(canonical_string(signal_id, intent, day).encode("utf-8")).hexdigest()
