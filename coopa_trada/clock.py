"""Timezone helpers. Every datetime that crosses a boundary is tz-aware.

- Storage and transport: UTC, ISO-8601.
- Market logic: ``MARKET_TZ`` (US equity/option sessions).
- Human-facing output: ``DISPLAY_TZ``.

A naive datetime is a bug, not an input to coerce. ``ensure_aware`` raises.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
MARKET_TZ = ZoneInfo("America/New_York")
DISPLAY_TZ = ZoneInfo("America/Denver")

__all__ = [
    "UTC",
    "MARKET_TZ",
    "DISPLAY_TZ",
    "NaiveDatetimeError",
    "now_utc",
    "ensure_aware",
    "to_utc",
    "to_market",
    "to_display",
    "iso_utc",
    "parse_iso",
    "combine_market",
    "fmt_display",
]


class NaiveDatetimeError(ValueError):
    """Raised when a datetime without tzinfo reaches a boundary that forbids it."""


def now_utc() -> datetime:
    """Current instant, tz-aware, in UTC."""
    return datetime.now(UTC)


def ensure_aware(dt: datetime) -> datetime:
    """Return ``dt`` unchanged if tz-aware; raise ``NaiveDatetimeError`` otherwise."""
    if not isinstance(dt, datetime):
        raise TypeError(f"expected datetime, got {type(dt).__name__}")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise NaiveDatetimeError(f"naive datetime not allowed: {dt!r}")
    return dt


def to_utc(dt: datetime) -> datetime:
    return ensure_aware(dt).astimezone(UTC)


def to_market(dt: datetime) -> datetime:
    return ensure_aware(dt).astimezone(MARKET_TZ)


def to_display(dt: datetime) -> datetime:
    return ensure_aware(dt).astimezone(DISPLAY_TZ)


def iso_utc(dt: datetime) -> str:
    """Canonical persisted form: ISO-8601 in UTC."""
    return to_utc(dt).isoformat()


def parse_iso(value: str) -> datetime:
    """Inverse of :func:`iso_utc`. Rejects a parsed-but-naive result."""
    return ensure_aware(datetime.fromisoformat(value))


def combine_market(d: date, t: time) -> datetime:
    """Wall-clock ``t`` on date ``d`` in ``MARKET_TZ``, tz-aware."""
    return datetime.combine(d, t, tzinfo=MARKET_TZ)


def fmt_display(dt: datetime, *, with_day: bool = True) -> str:
    """How a time is shown to a person: Mountain time, e.g. ``Mon Oct 19, 2:29 PM MT``."""
    d = to_display(dt)
    hm = d.strftime("%I:%M %p").lstrip("0") + " MT"
    return f"{d:%a %b} {d.day}, {hm}" if with_day else hm
