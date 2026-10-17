"""Settings serialization. Trading values are editable; confirmation markers are file-only."""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path

import yaml

from ..rules import DeltaFilter, default_settings_metadata, load_rules, setting_confirmations

STOP_KEYS = ("cooper_drop_from_peak_pct", "cooper_drop_from_entry_pct")
DEFAULT_STOP_NOTE = (
    "Default chosen in Cooper's absence: a 10% retrace from the session peak. "
    'His wording ("exit at the high", "drops 10% between the times I said") suggests this reading. '
    "Because the observed peak is at least the entry price, its 10% trigger is at least as high: "
    "it is the earlier, tighter exit when the peak exceeds entry. Both fields are editable; "
    "blank disables a field. If both are set, both are active and the first trigger wins. "
    "The standard 25% / 40% / 60% exits remain active alongside it: adding triggers can "
    "make an exit earlier, never later. Confirm these defaults with Cooper before a live run."
)
SPENDING_COMMENT = """# Spending cap per pick. Always buys exactly 1 contract; if that contract
# costs more than this, the ticker is skipped entirely.
# DEFAULT, NOT CONFIRMED BY COOPER -- $300 is a number he mentioned once and
# never confirmed. Adjust freely.
"""
DELTA_COMMENT = """# Delta filtering. Off by default -- we assume Cooper's source already screens
# on delta and sends only the picks it wants traded.
# DEFAULT, NOT CONFIRMED BY COOPER.
# This is a curated pick list from a paid service. We assume the delta columns
# document upstream selections; filtering here would discard paid-for picks.
# TVT-DELTA appears to use 0-100, not Schwab's 0-1 scale. For source: tvt,
# both thresholds must be > 1.0 (e.g. 30 and 80); no automatic rescaling.
"""
_REQUIRED_NUMERIC = {
    "entry_minutes_before_close": int, "trailing_stop_pct": float,
    "hard_stop_loss_pct": float, "take_profit_pct": float,
    "max_concurrent_positions": int, "max_dollars_per_position": float,
    "poll_interval_seconds": int,
}


class RulesValidationError(ValueError):
    """A proposed settings payload failed validation."""


def _endpoint(value):
    s = str(value).strip()
    return int(s) if re.fullmatch(r"\d+", s) else s


def read_rules_dict(config_path) -> dict:
    path = Path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    warning = None
    try:
        load_rules(path)
    except (ValueError, TypeError, OSError) as exc:
        warning = str(exc)
    markers = setting_confirmations(raw.get("setting_confirmations"))
    out = {key: raw.get(key) for key in (*_REQUIRED_NUMERIC, *STOP_KEYS)}
    out.update(
        exit_window={key: [_endpoint(v) for v in pair] for key, pair in (raw.get("exit_window") or {}).items()},
        delta_filter={**asdict(DeltaFilter()), **(raw.get("delta_filter") or {})},
        setting_confirmations=markers,
        default_settings=default_settings_metadata(markers),
        stop_keys=list(STOP_KEYS), default_stop_note=DEFAULT_STOP_NOTE,
        warning=warning, path=str(path),
    )
    return out


def _coerce_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise RulesValidationError("expected a JSON object")
    out = {}
    for key, caster in _REQUIRED_NUMERIC.items():
        if key not in payload or payload[key] in (None, ""):
            raise RulesValidationError(f"missing value for {key}")
        try:
            out[key] = caster(payload[key])
        except (TypeError, ValueError, OverflowError):
            raise RulesValidationError(f"{key} must be a number") from None
    windows = payload.get("exit_window")
    if not isinstance(windows, dict) or not windows:
        raise RulesValidationError("exit_window must have at least one asset class")
    out["exit_window"] = {}
    for key, pair in windows.items():
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise RulesValidationError(f"exit_window[{key}] must be a [start, end] pair")
        out["exit_window"][key] = [_endpoint(v) for v in pair]
    for key in STOP_KEYS:
        value = payload.get(key)
        try:
            out[key] = None if value in (None, "", "null") else float(value)
        except (TypeError, ValueError):
            raise RulesValidationError(f"{key} must be a number or blank") from None
    try:
        out["delta_filter"] = asdict(DeltaFilter.from_config(payload.get("delta_filter")))
    except (ValueError, TypeError) as exc:
        raise RulesValidationError(str(exc)) from None
    return out


def _render_yaml(body: dict) -> str:
    comments = {
        "cooper_drop_from_peak_pct": "\n".join("# " + line for line in [DEFAULT_STOP_NOTE]) + "\n",
        "max_dollars_per_position": SPENDING_COMMENT,
        "delta_filter": DELTA_COMMENT,
        "setting_confirmations": "# File-only confirmation markers. Settings preserves these; the UI cannot dismiss them.\n",
    }
    text = "# Coopa Trada rules. Values below are adjustable; unconfirmed defaults are warnings, not live gates.\n"
    for key, value in body.items():
        text += comments.get(key, "")
        text += yaml.safe_dump({key: value}, sort_keys=False)
    return text


def write_rules(config_path, payload: dict) -> dict:
    body = _coerce_payload(payload)
    target = Path(config_path)
    current = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    # Never accept marker changes through the API, even if a forged payload sends them.
    body["setting_confirmations"] = setting_confirmations(current.get("setting_confirmations"))
    # Preserve the existing filter if an older API client omits it.
    if "delta_filter" not in payload:
        body["delta_filter"] = asdict(DeltaFilter.from_config(current.get("delta_filter")))
    fd, temporary = tempfile.mkstemp(dir=target.parent, suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_render_yaml(body))
        try:
            load_rules(temporary)
        except (ValueError, TypeError) as exc:
            raise RulesValidationError(str(exc)) from None
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return read_rules_dict(target)
