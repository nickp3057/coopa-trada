"""Trading rules loaded from YAML.

Every tunable lives here. ``config/rules.example.yaml`` documents each key.
The per-asset-class exit window accepts, for each endpoint, either a
``HH:MM`` wall-clock time or an integer number of minutes before the close.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import yaml

from .models import AssetClass

if TYPE_CHECKING:
    from .contracts import ResolvedContract
    from .models import Signal

__all__ = ["Rules", "load_rules", "PositionSizer", "FixedDollarSizer"]


@dataclass(frozen=True)
class Rules:
    entry_minutes_before_close: int
    exit_window: dict[AssetClass, tuple[time | int, time | int]]
    trailing_stop_pct: float
    hard_stop_loss_pct: float
    take_profit_pct: float
    max_concurrent_positions: int
    max_dollars_per_position: float
    poll_interval_seconds: int
    # Ambiguous per Cooper 9/2 ("drops 10% between the times I said"). Optional:
    # absent -> the corresponding extra exit strategy is simply not wired.
    cooper_drop_from_peak_pct: float | None = None
    cooper_drop_from_entry_pct: float | None = None


class PositionSizer(Protocol):
    def size(self, signal: "Signal", contract: "ResolvedContract", rules: Rules) -> int:
        """Returns number of contracts to buy. 0 means skip."""


class FixedDollarSizer:
    """Per-position dollar cap, enforced as a hard skip (per Cooper).

    If one contract (``contract.mid * 100``) would exceed ``dollars_per_position``,
    return 0 -- skip the ticker entirely rather than buying fewer contracts to
    fit. Otherwise buy as many as fit: ``floor(dollars_per_position / (mid * 100))``.
    This is the only sizer; there is no contract-count variant.
    """

    def __init__(self, dollars_per_position: float):
        self.dollars_per_position = float(dollars_per_position)

    def size(self, signal: "Signal", contract: "ResolvedContract", rules: Rules) -> int:
        one_contract_cost = contract.mid * 100
        if one_contract_cost <= 0:
            return 0
        if one_contract_cost > self.dollars_per_position:
            return 0  # even one contract exceeds the per-position dollar cap -> skip
        return math.floor(self.dollars_per_position / one_contract_cost)


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
    if (v < lo) or (v == 0.0 and not allow_zero) or v >= 1.0:
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
        "max_concurrent_positions",
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
    if max_dollars <= 0:
        raise ValueError("max_dollars_per_position must be positive")

    cooper_peak = data.get("cooper_drop_from_peak_pct")
    cooper_entry = data.get("cooper_drop_from_entry_pct")

    return Rules(
        entry_minutes_before_close=entry_minutes,
        exit_window=_coerce_exit_window(data["exit_window"]),
        trailing_stop_pct=_pct("trailing_stop_pct", data["trailing_stop_pct"]),
        hard_stop_loss_pct=_pct("hard_stop_loss_pct", data["hard_stop_loss_pct"]),
        take_profit_pct=_pct("take_profit_pct", data["take_profit_pct"]),
        max_concurrent_positions=_pos_int("max_concurrent_positions", data["max_concurrent_positions"]),
        max_dollars_per_position=max_dollars,
        poll_interval_seconds=_pos_int("poll_interval_seconds", data["poll_interval_seconds"]),
        cooper_drop_from_peak_pct=(
            _pct("cooper_drop_from_peak_pct", cooper_peak) if cooper_peak is not None else None
        ),
        cooper_drop_from_entry_pct=(
            _pct("cooper_drop_from_entry_pct", cooper_entry) if cooper_entry is not None else None
        ),
    )
