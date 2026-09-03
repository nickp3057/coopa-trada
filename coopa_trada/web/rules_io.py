"""Read and write ``config/rules.yaml`` for the settings screen.

Validation is **not** duplicated here: a candidate is written to a temp file
and handed to :func:`coopa_trada.rules.load_rules`, which is the single
authority on every range. Only if that passes is ``config/rules.yaml``
rewritten -- from a fixed template (a static header + the permanent
"ambiguous pending Cooper" note), since PyYAML cannot preserve the comments
already in the file.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..rules import load_rules

__all__ = ["RulesValidationError", "read_rules_dict", "write_rules", "AMBIGUOUS_KEYS"]

# The two keys that encode the same still-unresolved "sell if it drops 10%"
# instruction (see README). Both stay live at once until Cooper confirms which
# he meant; this is surfaced in the UI as a plain sentence, not a code change.
AMBIGUOUS_KEYS = ("cooper_drop_from_peak_pct", "cooper_drop_from_entry_pct")

AMBIGUOUS_NOTE = (
    "cooper_drop_from_peak_pct and cooper_drop_from_entry_pct are two readings "
    "of the same instruction from Cooper -- \"sell if it drops 10%\" -- one "
    "measured as a retrace from the session peak, the other as a drop from the "
    "entry price. Both are active simultaneously right now; whichever triggers "
    "first wins. One of them should be removed once Cooper confirms which he "
    "meant, and whether it replaces the 25% / 40% / 60% exits."
)

_REQUIRED_NUMERIC = {
    "entry_minutes_before_close": int,
    "trailing_stop_pct": float,
    "hard_stop_loss_pct": float,
    "take_profit_pct": float,
    "max_concurrent_positions": int,
    "max_dollars_per_position": float,
    "poll_interval_seconds": int,
}

# The order keys are written back to rules.yaml (readability only; load order
# does not matter). exit_window is slotted in after entry_minutes_before_close.
_WRITE_ORDER = (
    "entry_minutes_before_close",
    "exit_window",
    "trailing_stop_pct",
    "hard_stop_loss_pct",
    "take_profit_pct",
    "cooper_drop_from_peak_pct",
    "cooper_drop_from_entry_pct",
    "max_concurrent_positions",
    "max_dollars_per_position",
    "poll_interval_seconds",
)


class RulesValidationError(ValueError):
    """A proposed rules payload failed ``load_rules`` validation."""


def _endpoint(value) -> int | str:
    """A bare integer means 'minutes before close'; anything else stays a string
    (``load_rules`` is what actually validates the ``HH:MM`` form)."""
    s = str(value).strip()
    return int(s) if re.fullmatch(r"\d+", s) else s


def read_rules_dict(config_path: str | os.PathLike) -> dict:
    """Current rules as a JSON-serialisable dict, plus UI metadata.

    Never raises for a merely-invalid on-disk file: the values are still
    returned, with a ``warning`` describing why ``load_rules`` rejected it.
    """
    path = Path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    warning = None
    try:
        load_rules(path)
    except (ValueError, OSError) as exc:
        warning = str(exc)

    exit_window = {}
    for key, pair in (raw.get("exit_window") or {}).items():
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            exit_window[str(key)] = [_endpoint(pair[0]), _endpoint(pair[1])]

    out = {
        "entry_minutes_before_close": raw.get("entry_minutes_before_close"),
        "exit_window": exit_window,
        "trailing_stop_pct": raw.get("trailing_stop_pct"),
        "hard_stop_loss_pct": raw.get("hard_stop_loss_pct"),
        "take_profit_pct": raw.get("take_profit_pct"),
        "max_concurrent_positions": raw.get("max_concurrent_positions"),
        "max_dollars_per_position": raw.get("max_dollars_per_position"),
        "poll_interval_seconds": raw.get("poll_interval_seconds"),
        "cooper_drop_from_peak_pct": raw.get("cooper_drop_from_peak_pct"),
        "cooper_drop_from_entry_pct": raw.get("cooper_drop_from_entry_pct"),
        "ambiguous_keys": list(AMBIGUOUS_KEYS),
        "ambiguous_note": AMBIGUOUS_NOTE,
        "warning": warning,
        "path": str(path),
    }
    return out


def _coerce_payload(payload: dict) -> dict:
    """Turn a settings-form payload into a plain mapping ready for YAML.

    Raises ``RulesValidationError`` for structurally broken input (missing key,
    non-numeric where a number is required). Range checks are left to
    ``load_rules``.
    """
    if not isinstance(payload, dict):
        raise RulesValidationError("expected a JSON object")

    out: dict = {}
    for key, caster in _REQUIRED_NUMERIC.items():
        if key not in payload or payload[key] in (None, ""):
            raise RulesValidationError(f"missing value for {key}")
        try:
            out[key] = caster(payload[key])
        except (TypeError, ValueError):
            raise RulesValidationError(
                f"{key} must be {'an integer' if caster is int else 'a number'}"
            ) from None

    ew_in = payload.get("exit_window")
    if not isinstance(ew_in, dict) or not ew_in:
        raise RulesValidationError("exit_window must have at least one asset class")
    ew_out: dict = {}
    for cls, pair in ew_in.items():
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise RulesValidationError(f"exit_window[{cls}] must be a [start, end] pair")
        ew_out[str(cls)] = [_endpoint(pair[0]), _endpoint(pair[1])]
    out["exit_window"] = ew_out

    for key in AMBIGUOUS_KEYS:
        val = payload.get(key)
        if val in (None, "", "null"):
            continue
        try:
            out[key] = float(val)
        except (TypeError, ValueError):
            raise RulesValidationError(f"{key} must be a number or blank") from None

    return out


def _render_yaml(body: dict) -> str:
    header = (
        "# Coopa Trada trading rules.\n"
        f"# Rewritten by the Coopa Trada web UI at {datetime.now(timezone.utc).isoformat()}.\n"
        "# The UI regenerates this whole file (comments included) on every save,\n"
        "# so hand-edited comments here will not survive the next save from the UI.\n"
        "# Every value below is validated on load -- see coopa_trada/rules.py.\n"
        "#\n"
        "# NOTE (pending Cooper): cooper_drop_from_peak_pct and\n"
        "# cooper_drop_from_entry_pct are two readings of the same \"sell if it\n"
        "# drops 10%\" instruction (retrace from the session peak vs. drop from\n"
        "# entry). Both are live at once right now; whichever triggers first wins.\n"
        "# Remove one once Cooper confirms which he meant.\n\n"
    )

    lines: list[str] = []
    for key in _WRITE_ORDER:
        if key not in body:
            continue
        if key == "exit_window":
            lines.append("exit_window:")
            for cls, pair in body["exit_window"].items():
                rendered = ", ".join(
                    repr(p) if isinstance(p, str) else str(p) for p in pair
                )
                lines.append(f"  {cls}: [{rendered}]")
        else:
            lines.append(yaml.safe_dump({key: body[key]}, default_flow_style=False).strip())
    return header + "\n".join(lines) + "\n"


def write_rules(config_path: str | os.PathLike, payload: dict) -> dict:
    """Validate ``payload`` via ``load_rules`` then atomically rewrite the file.

    Returns the fresh :func:`read_rules_dict`. Raises ``RulesValidationError``
    (never a traceback) on any bad value.
    """
    body = _coerce_payload(payload)

    fd, tmp_name = tempfile.mkstemp(suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(body, fh, sort_keys=False, default_flow_style=False)
        try:
            load_rules(tmp_name)
        except ValueError as exc:
            raise RulesValidationError(str(exc)) from None
    finally:
        os.unlink(tmp_name)

    target = Path(config_path)
    tmp_target = target.with_name(target.name + ".tmp")
    tmp_target.write_text(_render_yaml(body), encoding="utf-8")
    os.replace(tmp_target, target)

    return read_rules_dict(config_path)
