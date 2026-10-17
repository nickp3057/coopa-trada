"""HTTP surface for the web UI.

Every route is a thin adapter over existing ``coopa_trada`` code. No trading
logic lives here.
"""

from __future__ import annotations

import os
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request

from ..config import Mode, credentials_from_env, resolve_mode, _truthy
from ..logs import banner
from ..models import CsvSignalSource
from ..store import Store
from ..wiring import TokenMissing, build_engine, token_status
from .config import WebPaths
from .rules_io import RulesValidationError, live_status, read_rules_dict, write_rules
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
    snap.update(live_status(_paths().config_path))
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
        # Early, readable rejection. Engine.run enforces the same gates.
        status = live_status(_paths().config_path)
        if status["live_blocked"]:
            pending = "; ".join(f'{s["label"]} ({s["current"]})' for s in status["unconfirmed_settings"])
            return jsonify(error=status["live_block_error"] or
                           f"Live run blocked: unconfirmed settings: {pending}. Confirm them in Settings.",
                           unconfirmed_settings=status["unconfirmed_settings"]), 409
        token = token_status(config_path=_paths().config_path)
        if token["state"] in ("not_set_up", "expired"):
            return jsonify(error=f'Schwab is {token["label"].lower()}. {token["message"]}'), 409
        if not token["covers_next_run"]:
            return jsonify(error=token["hold_message"] or "The Schwab login does not last through this run."), 409

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


# -- app identity, capabilities, quit (desktop launcher) ------------
@bp.get("/api/app")
def api_app():
    from ..imports import image_reader_status
    return jsonify(app="coopa-trada", home=current_app.config.get("COOPA_HOME"),
                   can_quit=current_app.config.get("COOPA_SHUTDOWN") is not None,
                   image_upload=image_reader_status())


@bp.post("/api/app/quit")
def api_app_quit():
    shutdown = current_app.config.get("COOPA_SHUTDOWN")
    if shutdown is None:
        return jsonify(error="This server was not started by the Coopa Trada app; stop it where you started it."), 400
    if _runner().status == "running":
        return jsonify(error="A run is in progress. Press Stop and wait for it to finish before quitting."), 409
    shutdown()
    return jsonify(message="Coopa Trada has quit. You can close this tab.")


# -- orders needing a person ----------------------------------------
def _with_store(fn):
    store = Store.open(_paths().db_path)
    try:
        return fn(store)
    finally:
        store.close()


@bp.get("/api/attention")
def api_attention():
    incidents = _with_store(lambda store: store.open_incidents())
    return jsonify(incidents=incidents)


@bp.post("/api/attention/<int:incident_id>/resolve")
def api_attention_resolve(incident_id: int):
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("placed"), bool):
        return jsonify(error="Say whether the order is at Schwab (placed: true or false)"), 400
    if _runner().status == "running":
        return jsonify(error="Stop the current run before resolving orders."), 409
    try:
        _with_store(lambda store: store.resolve_incident(
            incident_id, placed=body["placed"], note=body.get("note", ""),
            broker_order_id=body.get("broker_order_id")))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(incidents=_with_store(lambda store: store.open_incidents()))


# -- run locks ------------------------------------------------------
@bp.get("/api/lock")
def api_lock():
    from ..runlock import known_locks
    locks = [lock for lock in known_locks(_paths().db_path) if lock["state"] != "free"]
    return jsonify(locks=locks, stale=[lock for lock in locks if lock["state"] == "stale"])


@bp.post("/api/lock/clear")
def api_lock_clear():
    from ..runlock import RunLocked, clear_stale_lock, known_locks
    body = request.get_json(silent=True) or {}
    # Only locks this app knows about; never an arbitrary path from the request.
    known = {lock["path"]: lock for lock in known_locks(_paths().db_path)}
    if body.get("path") not in known:
        return jsonify(error="Unknown lock"), 400
    try:
        result = clear_stale_lock(body["path"])
    except RunLocked as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(result)


# -- connection status ---------------------------------------------
@bp.get("/api/token")
def api_token():
    status = token_status(config_path=_paths().config_path)
    status["reconnect_hint"] = "Click Reconnect Schwab and follow the steps."
    status["login_in_progress"] = current_app.config.get("SCHWAB_PENDING_LOGIN") is not None
    return jsonify(status)


# Weekly reconnect: begin returns Schwab's login URL; finish takes the address
# the browser lands on after "Allow". See coopa_trada/schwab_login.py.
@bp.post("/api/schwab/reconnect/begin")
def api_reconnect_begin():
    from ..schwab_login import LoginError, begin_login
    if _runner().status == "running":
        return jsonify(error="Stop the current run before reconnecting Schwab."), 409
    try:
        pending = begin_login(credentials_from_env())
    except LoginError as exc:
        return jsonify(error=str(exc)), 400
    current_app.config["SCHWAB_PENDING_LOGIN"] = pending
    return jsonify(authorization_url=pending.authorization_url,
                   callback_url=credentials_from_env().callback_url)


@bp.post("/api/schwab/reconnect/finish")
def api_reconnect_finish():
    from ..schwab_login import LoginError, LoginRejected, finish_login
    if _runner().status == "running":
        return jsonify(error="Stop the current run before reconnecting Schwab."), 409
    body = request.get_json(silent=True) or {}
    try:
        finish_login(credentials_from_env(), current_app.config.get("SCHWAB_PENDING_LOGIN"),
                     body.get("redirect_url", ""))
    except LoginRejected as exc:
        current_app.config["SCHWAB_PENDING_LOGIN"] = None  # Schwab's codes are single-use
        return jsonify(error=str(exc)), 400
    except LoginError as exc:
        return jsonify(error=str(exc)), 400
    current_app.config["SCHWAB_PENDING_LOGIN"] = None
    return jsonify(token_status(config_path=_paths().config_path))


# -- alerts ---------------------------------------------------------
@bp.get("/api/alerts")
def api_alerts():
    alerter = current_app.config["COOPA_ALERTER"]
    return jsonify(channels=alerter.channels(), recent=list(alerter.recent))


@bp.post("/api/alerts/test")
def api_alerts_test():
    alerter = current_app.config["COOPA_ALERTER"]
    results = alerter.test()
    return jsonify(results=results, channels=alerter.channels(), recent=list(alerter.recent))


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
    with _runner()._lock:
        if _runner().status == "running":
            return jsonify(error="Stop the run before changing picks"), 409
        _paths().signals_path = str(path)
    return jsonify({"signals_path": str(path), "signal_count": count})

# Uploads never select picks or start trading. A signed preview must be confirmed.
@bp.post("/api/signals/upload")
def api_signals_upload():
    import tempfile
    from ..imports import recognize_image
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify(error="Choose a CSV, JPEG, or PNG file"), 400
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in {".csv", ".jpg", ".jpeg", ".png"}:
        return jsonify(error="Choose a CSV, JPEG, or PNG file"), 400
    try:
        if suffix == ".csv":
            text = upload.read().decode("utf-8-sig")
        else:
            with tempfile.TemporaryDirectory(prefix="coopa-image-") as tmp:
                path = Path(tmp) / ("image" + suffix)
                upload.save(path)
                text = recognize_image(path)
        return jsonify(csv=text, image=suffix != ".csv")
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400


def _preview_signer():
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(current_app.secret_key, salt="signal-preview")


@bp.post("/api/signals/preview")
def api_signals_preview():
    from ..imports import preview_csv
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Expected a JSON object"), 400
    try:
        preview = preview_csv(body["csv"], body["date"], body["expiration"])
        preview["token"] = _preview_signer().dumps({k: preview[k] for k in ("csv", "date", "expiration")})
        return jsonify(preview)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        return jsonify(error=f"Check the picks and dates: {exc}"), 400


@bp.post("/api/signals/confirm")
def api_signals_confirm():
    from itsdangerous import BadSignature
    from uuid import uuid4
    body = request.get_json(silent=True) or {}
    try:
        preview = _preview_signer().loads(body.get("token", ""), max_age=3600)
    except (BadSignature, TypeError):
        return jsonify(error="Preview expired or changed. Preview the picks again."), 400
    runner = _runner()
    with runner._lock:
        if runner.status == "running":
            return jsonify(error="Stop the current run before selecting new picks"), 409
        directory = Path(_paths().db_path).resolve().parent / "imports"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{preview['date']}-{uuid4().hex}.csv"
        path.write_text(preview["csv"], encoding="utf-8")
        _paths().signals_path = str(path)
    return jsonify(signals_path=str(path), date=preview["date"], message="Picks selected. Start a dry run when ready.")
