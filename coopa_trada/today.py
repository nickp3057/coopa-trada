"""Today's picks for the Dashboard: is a list loaded, and what happened to each pick.

Reads the confirmed pick lists and the database. Never trades and never
contacts Schwab. A pick's status comes from its buy order (any mode) if it has
one, otherwise from its latest skip, otherwise it is still waiting.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from . import clock
from .alerts import contract_text
from .picks import PickListSource, latest_list
from .store import _CLOSED_ORDER_STATUSES

__all__ = ["picks_today"]


def _confirmed_at(path: Path) -> str | None:
    """The confirmation time from the list's name (``<day>-<UTC stamp>-<id>.csv``)."""
    try:
        stamp = path.name.split("-")[3]
        return datetime.strptime(stamp, "%Y%m%dT%H%M%S%f").replace(tzinfo=timezone.utc).isoformat()
    except (IndexError, ValueError):
        return None


def _pick_name(signal) -> str:
    strike = f"{signal.strike:g}" if signal.strike is not None else "?"
    return f"{signal.underlying} {strike} {signal.right.value.lower()}"


def _status(store, signal_id: str, d: str, report: dict, phase: str) -> dict:
    entry = store.conn.execute(
        """SELECT o.id, o.mode, o.status, o.limit_price,
                  EXISTS (SELECT 1 FROM fills f WHERE f.order_id = o.id) AS filled
           FROM orders o WHERE o.intent = 'ENTRY' AND o.signal_id = ? AND o.trading_day = ?
           ORDER BY o.id DESC LIMIT 1""", (signal_id, d)).fetchone()
    if entry is not None:
        out = {"mode": entry["mode"]}
        pos = report.get((signal_id, entry["mode"]))
        if pos is not None and pos["is_open"]:
            return {**out, "state": "bought", "text": f"Bought at ${pos['entry_price']:.2f}"}
        if pos is not None:
            return {**out, "state": "sold", "pnl": pos["realized_pnl"],
                    "text": f"Bought at ${pos['entry_price']:.2f}, sold at ${pos['exit_price']:.2f}"}
        if entry["status"] == "UNKNOWN":
            return {**out, "state": "problem", "text": "Buy order unclear: check the Reconcile page"}
        if not entry["filled"] and entry["status"] not in _CLOSED_ORDER_STATUSES:
            limit = f" (limit ${entry['limit_price']:.2f})" if entry["limit_price"] else ""
            return {**out, "state": "buying", "text": f"Buy order sent, waiting for a fill{limit}"}
        return {**out, "state": "skipped", "text": "Buy order did not fill"}
    skip = store.conn.execute(
        """SELECT mode, reason FROM skip_events WHERE signal_id = ? AND trading_day = ?
           ORDER BY id DESC LIMIT 1""", (signal_id, d)).fetchone()
    if skip is not None:
        return {"mode": skip["mode"], "state": "skipped", "text": f"Skipped: {skip['reason']}"}
    text = {"before": "Waiting for the buying window",
            "open": "Waiting to be bought",
            "after": "Not bought (the buying window is over)"}[phase]
    return {"mode": None, "state": "waiting" if phase != "after" else "skipped", "text": text}


def picks_today(store, folder, day: date, *, now: datetime, cal, rules=None) -> dict:
    """What the Dashboard's "Today's picks" card shows for ``day``."""
    d = day.isoformat()
    trading_day = cal.is_trading_day(day)
    window = None
    phase = "open"
    if trading_day and rules is not None:
        start, end = cal.buy_window(day, rules)
        window = {"start": start.isoformat(), "end": end.isoformat()}
        phase = "before" if now < start else "after" if now >= end else "open"

    path = latest_list(folder, day)
    signals, list_error = [], None
    if path is not None:
        try:
            signals = PickListSource(folder).fetch(day)
        except Exception as exc:  # noqa: BLE001 - shown to the person, the run reports it too
            list_error = f"The confirmed list could not be read: {exc}"

    report = {(r["signal_id"], r["mode"]): r for r in store.position_report()}
    picks = [{"name": _pick_name(s), "expiration": s.expiration.isoformat() if s.expiration else None,
              **_status(store, s.id, d, report, phase)} for s in signals]
    # Picks bought or skipped today that are not on the latest list (a replaced list).
    listed = {s.id for s in signals}
    for row in store.conn.execute(
            """SELECT DISTINCT o.signal_id, o.osi_symbol FROM orders o
               WHERE o.intent = 'ENTRY' AND o.trading_day = ? ORDER BY o.id""", (d,)):
        if row["signal_id"] not in listed:
            listed.add(row["signal_id"])
            picks.append({"name": contract_text(row["osi_symbol"]).split(",")[0], "expiration": None,
                          **_status(store, row["signal_id"], d, report, phase)})

    # The newest list confirmed for another trade date, so a list confirmed under the
    # wrong date does not look like nothing happened.
    other = None
    if path is None:
        lists = [p for p in Path(folder).glob("*.csv") if p.is_file()] if Path(folder).is_dir() else []
        newest = max(lists, key=lambda p: (p.stat().st_mtime_ns, p.name), default=None)
        confirmed = _confirmed_at(newest) if newest is not None else None
        if confirmed and clock.to_market(datetime.fromisoformat(confirmed)).date() == day:
            other = {"day": newest.name[:10], "confirmed_at": confirmed}

    held = [{"name": contract_text(r["osi_symbol"]).split(",")[0], "mode": r["mode"],
             "quantity": r["quantity"], "entry_price": r["entry_price"], "bought_on": r["trading_day"]}
            for r in store.open_positions() if r["trading_day"] and r["trading_day"] < d]

    return {
        "day": d,
        "trading_day": trading_day,
        "list_loaded": path is not None and list_error is None,
        "confirmed_at": _confirmed_at(path) if path is not None else None,
        "list_error": list_error,
        "other_list": other,
        "buy_window": window,
        "phase": phase if trading_day else None,
        "picks": picks,
        "held": held,
        "checked_at": clock.to_market(now).isoformat(),
    }
