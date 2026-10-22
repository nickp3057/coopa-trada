"""Settings serialization.

Trading values and Cooper-confirmation markers are both editable. Changing a
confirmed setting's value resets its marker unless the same save re-confirms it,
so a confirmation always refers to the value it was given for.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path

import yaml

from ..rules import (SETTING_LABELS, DeltaFilter, default_settings_metadata, load_rules,
                     setting_confirmations, unconfirmed_settings)

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
PROFIT_TRAIL_NOTE = (
    "Cooper's rule: sell when the profit % drops this many percentage points below its peak "
    "profit %. Example: bought at $1.00, highest price $2.00 (+100%), 10 points -> sell at "
    "$1.90 (+90%). It runs alongside every other exit; whichever fires first wins. "
    '"From entry" (the literal reading) is active immediately, so before any gain it acts '
    'like a -10% stop ($1.00 -> sells at $0.90). "Once in profit" waits until the price has '
    "been above the entry price. Blank turns it off."
)
# Optional keys with their defaults (absent from older rules files).
OPTIONAL_DEFAULTS = {
    "profit_trail_points": 10.0,
    "profit_trail_arm": "from_entry",
}
# Which rule keys each confirmation marker covers.
CONFIRMATION_KEYS = {
    "spending_cap": ("max_dollars_per_position",),
    "ten_percent_stop": STOP_KEYS,
    "delta_filter": ("delta_filter",),
    "profit_point_trail": ("profit_trail_points", "profit_trail_arm"),
}
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


def _pct_text(value) -> str:
    return "off" if value is None else f"{float(value) * 100:g}%"


def _describe(key: str, values: dict) -> str:
    """The current value a confirmation marker covers, in plain words."""
    if key == "spending_cap":
        cap = values.get("max_dollars_per_position")
        return f"${float(cap):g} per pick, checked against ask × 100" if cap is not None else "not set"
    if key == "ten_percent_stop":
        return (f"drop from peak {_pct_text(values.get('cooper_drop_from_peak_pct'))}, "
                f"drop from entry {_pct_text(values.get('cooper_drop_from_entry_pct'))}")
    if key == "profit_point_trail":
        points = values.get("profit_trail_points", OPTIONAL_DEFAULTS["profit_trail_points"])
        if points in (None, ""):
            return "off"
        arm = values.get("profit_trail_arm", OPTIONAL_DEFAULTS["profit_trail_arm"])
        return (f"sell {float(points):g} percentage points below peak profit, "
                + ("active from entry (acts as a stop before any gain)" if arm == "from_entry"
                   else "active only once in profit"))
    delta = values.get("delta_filter") or {}
    if not delta.get("enabled"):
        return "off (picks assumed screened upstream)"
    return f"on: |{delta.get('source')} delta| in [{delta.get('min_abs_delta'):g}, {delta.get('max_abs_delta'):g}]"


def live_status(config_path) -> dict:
    """Which settings still block a live run, for the dashboard."""
    try:
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        markers = setting_confirmations(raw.get("setting_confirmations"))
    except (ValueError, TypeError, OSError, yaml.YAMLError) as exc:
        return {"live_blocked": True, "unconfirmed_settings": [],
                "live_block_error": f"Rules file cannot be read: {exc}"}
    pending = [{"key": key, "label": SETTING_LABELS[key], "current": _describe(key, raw)}
               for key in unconfirmed_settings(markers)]
    return {"live_blocked": bool(pending), "unconfirmed_settings": pending, "live_block_error": None}


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
    out.update({key: raw.get(key, default) for key, default in OPTIONAL_DEFAULTS.items()})
    delta = {**asdict(DeltaFilter()), **(raw.get("delta_filter") or {})}
    metadata = [{**item, "current": _describe(item["key"], {**raw, "delta_filter": delta})}
                for item in default_settings_metadata(markers)]
    out.update(
        exit_window={key: [_endpoint(v) for v in pair] for key, pair in (raw.get("exit_window") or {}).items()},
        delta_filter=delta,
        setting_confirmations=markers,
        default_settings=metadata,
        stop_keys=list(STOP_KEYS), default_stop_note=DEFAULT_STOP_NOTE,
        profit_trail_note=PROFIT_TRAIL_NOTE,
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
    points = payload.get("profit_trail_points", OPTIONAL_DEFAULTS["profit_trail_points"])
    try:
        out["profit_trail_points"] = None if points in (None, "", "null") else float(points)
    except (TypeError, ValueError):
        raise RulesValidationError("profit_trail_points must be a number or blank") from None
    out["profit_trail_arm"] = payload.get("profit_trail_arm", OPTIONAL_DEFAULTS["profit_trail_arm"])
    try:
        out["delta_filter"] = asdict(DeltaFilter.from_config(payload.get("delta_filter")))
    except (ValueError, TypeError) as exc:
        raise RulesValidationError(str(exc)) from None
    return out


def _render_yaml(body: dict) -> str:
    comments = {
        "cooper_drop_from_peak_pct": "\n".join("# " + line for line in [DEFAULT_STOP_NOTE]) + "\n",
        "max_dollars_per_position": SPENDING_COMMENT,
        "profit_trail_points": "\n".join("# " + line for line in [PROFIT_TRAIL_NOTE]) + "\n"
                               "# NEW SETTING, NOT CONFIRMED BY COOPER (arm default: from_entry).\n",
        "delta_filter": DELTA_COMMENT,
        "setting_confirmations": ("# Cooper-confirmation markers. Any false marker blocks live runs (dry runs are\n"
                                  "# never blocked). Changing a setting's value resets its marker.\n"),
    }
    text = "# Coopa Trada rules. Values below are adjustable; unconfirmed settings block live runs.\n"
    for key, value in body.items():
        text += comments.get(key, "")
        text += yaml.safe_dump({key: value}, sort_keys=False)
    return text


def _covered_values(raw: dict, key: str):
    """Normalized values a marker covers, so formatting differences are not changes."""
    out = []
    for name in CONFIRMATION_KEYS[key]:
        value = raw.get(name)
        if name == "delta_filter":
            out.append(asdict(DeltaFilter.from_config(value)))
        elif name in OPTIONAL_DEFAULTS:
            value = raw.get(name, OPTIONAL_DEFAULTS[name])
            out.append(value if isinstance(value, str) or value is None else float(value))
        else:
            out.append(None if value in (None, "") else float(value))
    return out


def _next_confirmations(current: dict, body: dict, payload: dict) -> dict:
    before = setting_confirmations(current.get("setting_confirmations"))
    try:
        requested = setting_confirmations(payload.get("setting_confirmations", before))
    except ValueError as exc:
        raise RulesValidationError(str(exc)) from None
    result = {}
    for key in before:
        was, wants = before[key]["confirmed_by_cooper"], requested[key]["confirmed_by_cooper"]
        try:
            changed = _covered_values(current, key) != _covered_values(body, key)
        except (ValueError, TypeError):
            changed = True  # the old value was unreadable; treat it as a change
        # An untouched checkbox does not carry a confirmation over to a new value.
        result[key] = {"confirmed_by_cooper": wants and (wants != was or not changed)}
    return result


def write_rules(config_path, payload: dict) -> dict:
    body = _coerce_payload(payload)
    target = Path(config_path)
    current = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    # Preserve the existing filter if an older API client omits it.
    if "delta_filter" not in payload:
        body["delta_filter"] = asdict(DeltaFilter.from_config(current.get("delta_filter")))
    body["setting_confirmations"] = _next_confirmations(current, body, payload)
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
