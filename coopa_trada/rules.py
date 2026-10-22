"""Trading rules loaded from YAML.

Every tunable lives here. ``config/rules.example.yaml`` documents each key.
The per-asset-class exit window accepts, for each endpoint, either a
``HH:MM`` wall-clock time or an integer number of minutes before the close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import yaml
import math

from .models import AssetClass

if TYPE_CHECKING:
    from .contracts import ResolvedContract
    from .models import Signal

__all__ = ["Rules", "load_rules", "PositionSizer", "FixedQuantitySizer", "DeltaFilter", "setting_confirmations", "default_settings_metadata", "unconfirmed_settings"]


SETTING_LABELS = {
    "spending_cap": "spending cap",
    "ten_percent_stop": "10% stop basis",
    "delta_filter": "delta filter",
    "profit_point_trail": "profit-point trailing exit",
    "entry_day_stops": "stops on the day of entry",
    "next_day_exits": "next-day exit start",
    "total_budget": "total budget",
    "delta_ranking": "which delta ranks picks",
    "exit_repricing": "exit repricing",
}
RANK_DELTA_SOURCES = ("schwab_live", "tvt")


def setting_confirmations(raw: dict | None = None) -> dict:
    """One registry for all hand-edited confirmation markers; missing means false."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError("setting_confirmations must be a mapping")
    if set(raw) - SETTING_LABELS.keys():
        raise ValueError("Unknown setting in setting_confirmations")
    result = {}
    for key in SETTING_LABELS:
        marker = raw.get(key, {"confirmed_by_cooper": False})
        if not isinstance(marker, dict) or type(marker.get("confirmed_by_cooper")) is not bool:
            raise ValueError(f"setting_confirmations.{key}.confirmed_by_cooper must be true or false")
        result[key] = {"confirmed_by_cooper": marker["confirmed_by_cooper"]}
    return result


def default_settings_metadata(markers: dict) -> list[dict]:
    return [{"key": key, "label": label, **markers[key]}
            for key, label in SETTING_LABELS.items()]


def unconfirmed_settings(markers: dict) -> list[str]:
    """Keys still awaiting Cooper's confirmation. Any entry blocks a live run."""
    return [key for key in SETTING_LABELS if not markers[key]["confirmed_by_cooper"]]


@dataclass(frozen=True)
class DeltaFilter:
    enabled: bool = False
    source: str = "schwab"
    min_abs_delta: float = 0.30
    max_abs_delta: float = 0.80

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("delta_filter.enabled must be true or false")
        if self.source not in ("schwab", "tvt"):
            raise ValueError('delta_filter.source must be "schwab" or "tvt"')
        for name in ("min_abs_delta", "max_abs_delta"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"delta_filter.{name} must be a finite number")
        if not 0 <= self.min_abs_delta <= self.max_abs_delta:
            raise ValueError("delta_filter requires 0 <= min_abs_delta <= max_abs_delta")
        if self.source == "tvt" and min(self.min_abs_delta, self.max_abs_delta) <= 1.0:
            raise ValueError("delta_filter source=tvt uses the apparent 0–100 scale: both thresholds must be > 1.0 (for example 30 and 80), not 0.30 and 0.80")

    @classmethod
    def from_config(cls, raw=None):
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError("delta_filter must be a mapping")
        if set(raw) - {"enabled", "source", "min_abs_delta", "max_abs_delta"}:
            raise ValueError("Unknown delta_filter setting")
        return cls(**raw)

    def skip_reason(self, signal: "Signal") -> str | None:
        if not self.enabled:
            return None
        value = getattr(signal, f"signal_delta_{self.source}")
        if value is None or not math.isfinite(value):
            return f"delta filter: {self.source} delta is missing or nonfinite"
        if not self.min_abs_delta <= abs(value) <= self.max_abs_delta:
            return (f"delta filter: abs({self.source} delta {value:g}) = {abs(value):g} "
                    f"outside inclusive band [{self.min_abs_delta:g}, {self.max_abs_delta:g}]")
        return None


@dataclass(frozen=True)
class Rules:
    entry_minutes_before_close: int
    exit_window: dict[AssetClass, tuple[time | int, time | int]]
    trailing_stop_pct: float
    hard_stop_loss_pct: float
    take_profit_pct: float
    max_dollars_per_position: float
    poll_interval_seconds: int
    delta_filter: DeltaFilter = field(default_factory=DeltaFilter)
    setting_confirmations: dict = field(default_factory=setting_confirmations)
    # Optional existing strategies; null leaves the corresponding strategy unwired.
    cooper_drop_from_peak_pct: float | None = None
    cooper_drop_from_entry_pct: float | None = None
    # Cooper's rule: sell when profit % drops this many points below its peak
    # profit % (None = off). See exits.ProfitPointTrail.
    profit_trail_points: float | None = 10.0
    profit_trail_arm: str = "from_entry"
    # Hold logic (Cooper): take-profit sells the same day; otherwise the position
    # is held and every exit applies on the next trading day.
    # True: stop-type exits (hard stop, trailing stops, profit-point trail) also
    # protect the position on the day it was bought.
    stops_on_entry_day: bool = True
    # When held positions start being watched on the next trading day:
    # "open" (the session open), a New York HH:MM time, or minutes before the close.
    held_exit_start: object = "open"
    # Cooper: $300 is the TOTAL budget, the most that all open positions together
    # may cost (including ones held from earlier days), checked at ask x 100.
    # There is no position-count limit: picks are bought, highest delta first,
    # while each still fits.
    total_budget: float = 300.0
    # Which delta ranks the picks: "schwab_live" (from Schwab's option chain at
    # buying time) or "tvt" (the TVT-DELTA column of the pick list).
    rank_delta_source: str = "schwab_live"
    # Cooper: a sell that has not filled after this many seconds is canceled and
    # replaced at the current bid (never a market order), at most
    # exit_reprice_max_attempts times; then a person is alerted. 0 attempts = off.
    exit_reprice_after_seconds: int = 60
    exit_reprice_max_attempts: int = 5

    def live_block_reason(self) -> str | None:
        """Why a live run must not start, or None. Dry runs ignore this."""
        pending = unconfirmed_settings(self.setting_confirmations)
        if not pending:
            return None
        labels = ", ".join(SETTING_LABELS[key] for key in pending)
        return (f"Live run blocked: unconfirmed settings: {labels}. "
                "Confirm each with Cooper, then tick Confirm in Settings.")


class PositionSizer(Protocol):
    def size(self, signal: "Signal", contract: "ResolvedContract", rules: Rules) -> int:
        """Returns number of contracts to buy. 0 means skip."""


class FixedQuantitySizer:
    """Always buys exactly ``quantity`` contracts (default 1) per pick.

    ``dollars_per_position`` is a hard gate, not a sizing formula: if the cost of
    ``quantity`` contracts at the ask (the entry limit price) exceeds it, skip the
    ticker entirely (return 0).
    Otherwise return ``quantity`` unchanged -- never more, however cheap the
    contract. This is the only sizer; there is no contract-count or per-ticker
    quantity scheme.
    """

    def __init__(self, dollars_per_position: float, quantity: int = 1):
        self.dollars_per_position = float(dollars_per_position)
        self.quantity = int(quantity)

    def size(self, signal: "Signal", contract: "ResolvedContract", rules: Rules) -> int:
        if not math.isfinite(contract.ask) or contract.ask <= 0:
            return 0
        total_cost = contract.ask * 100 * self.quantity
        if total_cost > self.dollars_per_position:
            return 0  # one pick's worth of contracts exceeds the dollar gate -> skip
        return self.quantity


def _coerce_endpoint(value) -> time | int:
    if isinstance(value, bool):
        raise ValueError("exit-window endpoint may not be a bool")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("minutes-before-close endpoint must be >= 0")
        return value
    if isinstance(value, str):
        parts = value.strip().split(":")
        if len(parts) not in (2, 3):
            raise ValueError(f"bad time endpoint {value!r}; expected HH:MM")
        hh, mm = int(parts[0]), int(parts[1])
        ss = int(parts[2]) if len(parts) == 3 else 0
        return time(hh, mm, ss)
    if isinstance(value, time):
        return value
    raise ValueError(f"exit-window endpoint must be HH:MM or int minutes, got {value!r}")


def _coerce_exit_window(raw) -> dict[AssetClass, tuple[time | int, time | int]]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("exit_window must be a non-empty mapping of asset class -> [start, end]")
    out: dict[AssetClass, tuple[time | int, time | int]] = {}
    for key, pair in raw.items():
        try:
            ac = AssetClass[str(key).strip().upper()]
        except KeyError:
            raise ValueError(f"unknown asset class in exit_window: {key!r}") from None
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError(f"exit_window[{key}] must be a 2-item [start, end] list")
        out[ac] = (_coerce_endpoint(pair[0]), _coerce_endpoint(pair[1]))
    return out


def _pct(name: str, value, *, allow_zero: bool = False) -> float:
    v = float(value)
    lo = 0.0
    if not math.isfinite(v) or (v < lo) or (v == 0.0 and not allow_zero) or v >= 1.0:
        raise ValueError(f"{name} must be in (0, 1){' or 0' if allow_zero else ''}, got {value!r}")
    return v


def _pos_int(name: str, value) -> int:
    v = int(value)
    if v <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return v


def load_rules(path: str | Path) -> Rules:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")

    required = {
        "entry_minutes_before_close",
        "exit_window",
        "trailing_stop_pct",
        "hard_stop_loss_pct",
        "take_profit_pct",
        "max_dollars_per_position",
        "poll_interval_seconds",
    }
    missing = required - data.keys()
    if missing:
        raise ValueError(f"{path}: missing rule keys: {', '.join(sorted(missing))}")

    entry_minutes = int(data["entry_minutes_before_close"])
    if entry_minutes <= 0:
        raise ValueError("entry_minutes_before_close must be positive")

    max_dollars = float(data["max_dollars_per_position"])
    if not math.isfinite(max_dollars) or max_dollars <= 0:
        raise ValueError("max_dollars_per_position must be positive")

    cooper_peak = data.get("cooper_drop_from_peak_pct")
    cooper_entry = data.get("cooper_drop_from_entry_pct")

    trail_points = data.get("profit_trail_points", 10.0)
    if trail_points is not None:
        trail_points = float(trail_points)
        if not math.isfinite(trail_points) or not 0 < trail_points < 100:
            raise ValueError("profit_trail_points must be between 0 and 100 (percentage points), or blank")
    trail_arm = data.get("profit_trail_arm", "from_entry")
    from .exits import PROFIT_TRAIL_ARMS
    if trail_arm not in PROFIT_TRAIL_ARMS:
        raise ValueError(f"profit_trail_arm must be one of: {', '.join(PROFIT_TRAIL_ARMS)}")

    stops_today = data.get("stops_on_entry_day", True)
    if type(stops_today) is not bool:
        raise ValueError("stops_on_entry_day must be true or false")
    held_start = data.get("held_exit_start", "open")
    if not (isinstance(held_start, str) and held_start.strip().lower() == "open"):
        try:
            held_start = _coerce_endpoint(held_start)
            # An unquoted 14:00 in YAML reads as the number 840 (base 60); a full
            # session is 390 minutes, so larger numbers are refused, not guessed at.
            if isinstance(held_start, time) and not time(4) <= held_start <= time(20):
                raise ValueError
            if isinstance(held_start, int) and held_start > 390:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError('held_exit_start must be "open", a New York HH:MM time, '
                             "or whole minutes before the close") from None
    else:
        held_start = "open"

    # max_concurrent_positions (a fixed position count) was removed at Cooper's
    # request; older files may still carry it and it is ignored.
    budget = float(data.get("total_budget", 300.0))
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("total_budget must be a positive dollar amount")
    rank_source = data.get("rank_delta_source", "schwab_live")
    if rank_source not in RANK_DELTA_SOURCES:
        raise ValueError(f"rank_delta_source must be one of: {', '.join(RANK_DELTA_SOURCES)}")

    reprice_after = data.get("exit_reprice_after_seconds", 60)
    reprice_max = data.get("exit_reprice_max_attempts", 5)
    if isinstance(reprice_after, bool) or not isinstance(reprice_after, int) or not 10 <= reprice_after <= 3600:
        raise ValueError("exit_reprice_after_seconds must be a whole number from 10 to 3600")
    if isinstance(reprice_max, bool) or not isinstance(reprice_max, int) or not 0 <= reprice_max <= 20:
        raise ValueError("exit_reprice_max_attempts must be a whole number from 0 to 20")

    return Rules(
        exit_reprice_after_seconds=reprice_after,
        exit_reprice_max_attempts=reprice_max,
        total_budget=budget,
        rank_delta_source=rank_source,
        stops_on_entry_day=stops_today,
        held_exit_start=held_start,
        profit_trail_points=trail_points,
        profit_trail_arm=trail_arm,
        delta_filter=DeltaFilter.from_config(data.get("delta_filter")),
        setting_confirmations=setting_confirmations(data.get("setting_confirmations")),
        entry_minutes_before_close=entry_minutes,
        exit_window=_coerce_exit_window(data["exit_window"]),
        trailing_stop_pct=_pct("trailing_stop_pct", data["trailing_stop_pct"]),
        hard_stop_loss_pct=_pct("hard_stop_loss_pct", data["hard_stop_loss_pct"]),
        take_profit_pct=_pct("take_profit_pct", data["take_profit_pct"]),
        max_dollars_per_position=max_dollars,
        poll_interval_seconds=_pos_int("poll_interval_seconds", data["poll_interval_seconds"]),
        cooper_drop_from_peak_pct=(
            _pct("cooper_drop_from_peak_pct", cooper_peak) if cooper_peak is not None else None
        ),
        cooper_drop_from_entry_pct=(
            _pct("cooper_drop_from_entry_pct", cooper_entry) if cooper_entry is not None else None
        ),
    )

