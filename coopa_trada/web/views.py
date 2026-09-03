"""HTTP surface for the web UI.

Every route is a thin adapter over existing ``coopa_trada`` code. No trading
logic lives here.
"""

from __future__ import annotations

import os
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request

from ..config import Mode, resolve_mode, _truthy
from ..logs import banner
from ..models import CsvSignalSource
from ..store import Store
from ..wiring import TokenMissing, build_engine, token_status
from .config import WebPaths
from .rules_io import RulesValidationError, read_rules_dict, write_rules
from .runner import EngineRunner, RunnerBusy

bp = Blueprint("ui", __name__)

# Typed into the guarded "enable live trading" control. Deliberately awkward to
# fat-finger; it is only the *first* of the two live opt-ins -- LIVE_TRADING=1
# in the environment is the second, and the UI never sets it.
LIVE_CONFIRM_PHRASE = "ENABLE LIVE TRADING"


def _paths() -> WebPaths:
    return current_app.config["COOPA_PATHS"]


def _runner() -> EngineRunner:
    return current_app.config["COOPA_RUNNER"]


def _live_env_enabled() -> bool:
    return _truthy(os.environ.get("LIVE_TRADING"))


# -- pages ------------------------------------------------------------------
@bp.get("/")
def index():
    return render_template("index.html", page="dashboard")


@bp.get("/settings")
def settings_page():
    return render_template("settings.html", page="settings")


@bp.get("/trades")
def trades_page():
    return render_template("trades.html", page="trades")


@bp.get("/reconcile")
def reconcile_page():
    return render_template("reconcile.html", page="reconcile")


# -- rules ---------------------------------------------------------------
@bp.get("/api/rules")
def api_rules_get():
    return jsonify(read_rules_dict(_paths().config_path))


@bp.post("/api/rules")
def api_rules_post():
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify(error="request body must be JSON"), 400
    try:
        updated = write_rules(_paths().config_path, payload)
    except RulesValidationError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(updated)


# -- trade log --------------------------------------------------------
@bp.get("/api/trades")
def api_trades():
    store = Store.open(_paths().db_path)
    try:
        rows = store.position_report()
    finally:
        store.close()
    closed = [r for r in rows if not r["is_open"]]
    realized = sum(r["realized_pnl"] or 0.0 for r in closed)
    return jsonify(
        {
            "positions": rows,
            "summary": {
                "total": len(rows),
                "open": len(rows) - len(closed),
                "closed": len(closed),
                "realized_pnl": round(realized, 2),
            },
        }
    )


# -- run control ----------------------------------------------------
@bp.get("/api/run/status")
def api_run_status():
    snap = _runner().snapshot()
    snap["live_env_enabled"] = _live_env_enabled()
    snap["live_confirm_phrase"] = LIVE_CONFIRM_PHRASE
    return jsonify(snap)


@bp.post("/api/run/start")
def api_run_start():
    body = request.get_json(silent=True) or {}
    live = bool(body.get("live"))
    date_str = body.get("date") or None

    if live:
        if body.get("confirm") != LIVE_CONFIRM_PHRASE:
            return (
                jsonify(error=f'type "{LIVE_CONFIRM_PHRASE}" to confirm a live run'),
                409,
            )
        if not _live_env_enabled():
            return (
                jsonify(
                    error=(
                        "LIVE_TRADING is not set in this environment. That is the "
                        "second, independent opt-in for live trading and the UI "
                        "does not set it. Start the server with LIVE_TRADING=1 to "
                        "allow a live run."
                    )
                ),
                409,
            )

    try:
        _runner().start(live=live, date_str=date_str)
    except RunnerBusy as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(_runner().snapshot()), 202


@bp.post("/api/run/stop")
def api_run_stop():
    _runner().stop()
    return jsonify(_runner().snapshot()), 202


# -- reconcile ------------------------------------------------------
@bp.post("/api/reconcile")
def api_reconcile():
    # Attempt LIVE reconcile iff LIVE_TRADING=1; otherwise it is a dry-run no-op.
    mode = resolve_mode(True)
    store = None
    try:
        engine, store = build_engine(
            mode=mode,
            signals_path=_paths().signals_path,
            config_path=_paths().config_path,
            db_path=_paths().db_path,
        )
        report = engine.reconcile()
    except TokenMissing as exc:
        return jsonify(
            {
                "mode": Mode.LIVE.value,
                "simulated": False,
                "lines": [
                    "Cannot reconcile: no valid Schwab token.",
                    str(exc),
                ],
                "divergences": [],
            }
        )
    finally:
        if store is not None:
            store.close()

    if report.simulated:
        lines = ["Dry run: Schwab was not contacted, so there is nothing to reconcile."]
    else:
        lines = [
            f"{len(report.broker_positions)} position(s) open at Schwab; "
            f"{len(report.local_open_positions)} tracked locally.",
            f"{len(report.broker_open_orders)} open order(s) at Schwab; "
            f"{len(report.local_open_orders)} tracked locally.",
        ]
        if report.divergences:
            lines.append("Divergences (Schwab is treated as the source of truth):")
            lines.extend(report.divergences)
        else:
            lines.append("Local state matches Schwab. No divergences.")

    return jsonify(
        {
            "mode": report.mode,
            "simulated": report.simulated,
            "lines": lines,
            "divergences": list(report.divergences),
        }
    )


# -- connection status ---------------------------------------------
@bp.get("/api/token")
def api_token():
    status = token_status()
    status["reconnect_hint"] = (
        "Run the one-time interactive Schwab login from the README "
        '("One-time token login"), then point SCHWAB_TOKEN_PATH at the new file.'
    )
    return jsonify(status)


# -- mode banner --------------------------------------------------
@bp.get("/api/mode")
def api_mode():
    live_capable = _live_env_enabled()
    mode = resolve_mode(live_capable)
    return jsonify(
        {
            "mode": mode.value,
            "live_env_enabled": live_capable,
            "banner": banner(mode),
        }
    )


# -- signal CSV picker ------------------------------------------
@bp.post("/api/signals")
def api_signals():
    body = request.get_json(silent=True) or {}
    raw = (body.get("path") or "").strip()
    if not raw:
        return jsonify(error="path is required"), 400
    path = Path(raw).expanduser()
    if not path.is_file():
        return jsonify(error=f"no file at {path}"), 400
    try:
        count = len(CsvSignalSource(str(path)).fetch(None))
    except Exception as exc:  # parser errors surfaced plainly
        return jsonify(error=f"could not parse as a signals CSV: {exc}"), 400
    _paths().signals_path = str(path)
    return jsonify({"signals_path": str(path), "signal_count": count})
