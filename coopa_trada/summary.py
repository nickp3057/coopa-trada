"""End-of-day summary: one alert after the day's exit windows close.

Counts only: picks received, bought, skipped, positions held overnight, and
anything still waiting for a person. No account numbers, no dollar amounts,
no contract details (the alert scrubber removes amounts anyway; this text never
contains them in the first place).

Reads the database, the confirmed pick lists and the lock files. Never trades
and never contacts Schwab.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

__all__ = ["DaySummary", "ModeCounts", "collect", "MODES"]

MODES = (("LIVE", "Real trading"), ("DRY-RUN", "Practice"))


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


@dataclass
class ModeCounts:
    mode: str
    received: int = 0
    bought: int = 0
    skipped: int = 0
    not_filled: int = 0
    held: int = 0
    working_orders: int = 0
    incidents: int = 0

    @property
    def active(self) -> bool:
        return any((self.bought, self.skipped, self.not_filled, self.held, self.working_orders,
                    self.incidents))

    def line(self, label: str) -> str:
        parts = [_plural(self.received, "pick") + " received", f"{self.bought} bought",
                 f"{self.skipped} skipped"]
        if self.not_filled:
            parts.append(f"{self.not_filled} not filled")
        held = _plural(self.held, "position") + " held overnight"
        return f"{label}: {', '.join(parts)}. {held}."


@dataclass
class DaySummary:
    day: date
    received: int
    counts: list[ModeCounts]
    issues: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return f"Coopa Trada: {self.day:%a %b} {self.day.day} summary"

    @property
    def message(self) -> str:
        labels = dict(MODES)
        lines = [c.line(labels[c.mode]) for c in self.counts if c.active]
        if not lines:
            lines = [f"{_plural(self.received, 'pick')} received. Nothing bought, nothing held."]
        issues = "none." if not self.issues else " ".join(f"- {i}" for i in self.issues)
        return "\n".join(lines) + f"\nOpen issues: {issues}"


def _list_ids(folder, day: date) -> set[str]:
    from .picks import PickListSource
    try:
        return {s.id for s in PickListSource(folder).fetch(day)}
    except Exception:  # an unreadable list is reported by the run itself
        return set()


def _ids(store, sql: str, *args) -> set[str]:
    return {row[0] for row in store.conn.execute(sql, args)}


def mode_counts(store, mode: str, day: date, list_ids: set[str]) -> ModeCounts:
    d = day.isoformat()
    entries = _ids(store, "SELECT signal_id FROM orders WHERE intent='ENTRY' AND mode=? AND trading_day=?",
                   mode, d)
    bought = _ids(store, """SELECT DISTINCT o.signal_id FROM orders o JOIN fills f ON f.order_id=o.id
                            WHERE o.intent='ENTRY' AND o.mode=? AND o.trading_day=?""", mode, d)
    skipped = _ids(store, "SELECT signal_id FROM skip_events WHERE mode=? AND trading_day=?", mode, d) - bought
    open_ids = {r["id"] for r in store.orders_open(mode)}
    not_filled = _ids(store, """SELECT o.signal_id FROM orders o WHERE o.intent='ENTRY' AND o.mode=?
                                AND o.trading_day=? AND NOT EXISTS (SELECT 1 FROM fills f WHERE f.order_id=o.id)
                                AND o.id NOT IN (%s)""" % (",".join("?" * len(open_ids)) or "NULL"),
                      mode, d, *open_ids) - bought - skipped
    return ModeCounts(
        mode=mode,
        received=len(list_ids | entries | skipped),
        bought=len(bought),
        skipped=len(skipped),
        not_filled=len(not_filled),
        held=len(store.open_positions(mode)),
        working_orders=len(open_ids),
        incidents=len(store.open_incidents(mode)),
    )


def collect(store, day: date, *, picks_folder, runner=None, autostart=None, token=None,
            locks=(), autostart_enabled: bool = False) -> DaySummary:
    """Everything the summary says, from local state only.

    ``runner``: the EngineRunner snapshot (dict) or None. ``autostart``: the
    scheduler's ``last_attempt`` or None. ``token``: ``wiring.token_status()``
    or None. ``locks``: ``runlock.known_locks()``.
    """
    list_ids = _list_ids(picks_folder, day)
    counts = [mode_counts(store, mode, day, list_ids) for mode, _ in MODES]
    issues: list[str] = []
    labels = dict(MODES)
    for c in counts:
        prefix = "" if c.mode == "LIVE" else "Practice: "
        if c.incidents:
            issues.append(f"{prefix}{_plural(c.incidents, 'order')} to check on the Reconcile page.")
        if c.working_orders:
            issues.append(f"{prefix}{_plural(c.working_orders, 'order')} still working after the "
                          f"selling window; check Schwab.")
    live = counts[0]
    if live.held and not autostart_enabled:
        issues.append(f"{labels['LIVE']}: start tomorrow's run before the open to watch the "
                      f"{_plural(live.held, 'held position')}.")
    if runner and runner.get("last_error") and (runner.get("last_started") or "")[:10] >= day.isoformat():
        issues.append("Today's run stopped with an error; see the Dashboard.")
    if autostart and autostart.get("day") == day.isoformat() and not autostart.get("ok"):
        issues.append("The automatic start was skipped today.")
    for lock in locks:
        if lock.get("state") == "stale" and (lock.get("holder") or {}).get("mode") == "LIVE":
            issues.append("A real-money run did not shut down cleanly; new runs are blocked.")
            break
    if token and token.get("state") != "not_set_up" and not token.get("covers_next_run", True):
        issues.append("Reconnect Schwab before the next run.")
    return DaySummary(day, len(list_ids), counts, issues)
