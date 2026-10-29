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
from ..rules import load_rules
from ..store import Store
from ..wiring import TokenMissing, build_engine, token_status
from .config import WebPaths
from .rules_io import RulesValidationError, read_rules_dict, settings_summary, write_rules
from .runner import EngineRunner, RunnerBusy

bp = Blueprint("ui", __name__)

# Typed to turn the Dashboard's "Real money" switch on. Deliberately awkward to
# fat-finger; LIVE_TRADING=1 in the bot's .env on the server is the other,
# independent opt-in, and the UI never sets it.
LIVE_CONFIRM_PHRASE = "ENABLE LIVE TRADING"


def _paths() -> WebPaths:
    return current_app.config["COOPA_PATHS"]


def _runner() -> EngineRunner:
    return current_app.config["COOPA_RUNNER"]


def _live_env_enabled() -> bool:
    return _truthy(os.environ.get("LIVE_TRADING"))


def _switch_on() -> bool:
    from ..live_switch import is_on
    return is_on(_paths().db_path)


def _armed() -> bool:
    return _live_env_enabled() and _switch_on()


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
    # A run reads the settings when it starts: say so, so a change isn't silently ignored.
    return jsonify(**updated, run_going=_runner().status == "running")


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
    snap["live_switch_on"] = _switch_on()
    snap["live_armed"] = snap["live_env_enabled"] and snap["live_switch_on"]
    snap["live_confirm_phrase"] = LIVE_CONFIRM_PHRASE
    snap["keep_awake"] = current_app.config["COOPA_KEEPAWAKE"].status()
    snap.update(settings_summary(_paths().config_path))
    snap["live_checks"] = _live_checks(snap)
    scheduler = current_app.config.get("COOPA_SCHEDULER")
    if scheduler is not None:
        snap["schedule"] = scheduler.snapshot()
    return jsonify(snap)


def _live_checks(snap: dict) -> list[dict]:
    """What a live run needs, each with whether it is met and how to fix it."""
    from ..calendar_svc import CalendarService
    from ..clock import fmt_display, now_utc, to_market
    from .env_io import env_path, schwab_keys_status

    restart = ("restart this bot (server: sudo systemctl restart coopa-trada@YOURNAME; "
               "Mac: quit and reopen the app)")
    checks = [{"ok": bool(snap.get("live_env_enabled")),
               "label": "This bot is allowed to trade real money (LIVE_TRADING=1, set once on the server)",
               "fix": f"On the server, edit {env_path()} so it says LIVE_TRADING=1, then {restart}."}]
    keys = schwab_keys_status()
    checks.append({"ok": keys["api_key_set"] and keys["secret_set"],
                   "label": "Your Schwab app key and secret are saved",
                   "fix": "Settings → Schwab app: paste the App Key and Secret from the Schwab Developer Portal."})
    token = token_status(config_path=_paths().config_path)
    connected = token["state"] in ("connected", "expiring_soon")
    checks.append({"ok": connected and bool(token.get("covers_next_run")),
                   "label": "Schwab is connected, and the login lasts until the bot has sold",
                   "fix": (token.get("hold_message") or token.get("message") or "")
                   + " Use Reconnect Schwab on the Dashboard."})
    now = now_utc()
    try:
        cal = CalendarService()
        today = to_market(now).date()
        open_today = cal.is_trading_day(today) and now < cal.session_close(today)
        nxt = today if open_today else cal.next_session(today)
        when = fmt_display(cal.session_open(nxt))
    except Exception as exc:  # noqa: BLE001 - e.g. past the bundled calendar
        open_today, when = False, f"unknown ({exc})"
    checks.append({"ok": open_today,
                   "label": "The market is open today (a live run starts on a trading day, before the close)",
                   "fix": f"Weekends, holidays and after the 2:00 PM MT close are closed. Next session opens {when}."})
    checks.append({"ok": snap.get("settings_error") is None, "label": "The settings file is valid",
                   "fix": snap.get("settings_error") or ""})
    return checks


@bp.post("/api/live/switch")
def api_live_switch():
    """The Dashboard's "Real money" switch: on needs the phrase, off is one click."""
    from ..live_switch import set_switch
    body = request.get_json(silent=True) or {}
    on = body.get("on") is True
    runner = _runner()
    running_live = runner.status == "running" and runner.mode == "LIVE"
    if on:
        if body.get("confirm") != LIVE_CONFIRM_PHRASE:
            return jsonify(error=f'Type "{LIVE_CONFIRM_PHRASE}" to turn real money on.'), 409
        if not _live_env_enabled():
            return jsonify(error="This bot isn't allowed to trade real money yet: on the server, set "
                                 "LIVE_TRADING=1 in its .env and restart it. The website can't do that."), 409
        set_switch(_paths().db_path, True)
        message = ("Real money is ON. The daily automatic run and the Start button now place real orders.")
        if runner.status == "running" and not running_live:
            message += " The practice run going now stays practice: press Stop, then Start, to trade for real today."
    else:
        set_switch(_paths().db_path, False)
        message = "Real money is OFF. Runs from now on are practice only."
        if running_live:
            message += (" The live run going now keeps watching what it bought; press Stop to end it "
                        "(Stop sells nothing).")
    return jsonify(live_switch_on=_switch_on(), live_armed=_armed(), message=message)


# -- Schwab app details (this bot's own .env) -------------------------------
@bp.get("/api/schwab/keys")
def api_schwab_keys():
    from .env_io import schwab_keys_status
    return jsonify(schwab_keys_status())


@bp.post("/api/schwab/keys")
def api_schwab_keys_save():
    from .env_io import EnvValidationError, save_schwab_keys
    if _runner().status == "running":
        return jsonify(error="Stop the current run before changing the Schwab app details."), 409
    try:
        status = save_schwab_keys(request.get_json(silent=True) or {})
    except EnvValidationError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(**status, message="Saved. If you changed the App Key or Secret, click Reconnect Schwab "
                                   "on the Dashboard so the login uses them.")


def _practice_real_prices() -> bool:
    try:
        return load_rules(_paths().config_path).practice_real_prices
    except (ValueError, TypeError, OSError):
        return False  # the run itself reports a broken rules file


@bp.post("/api/run/start")
def api_run_start():
    body = request.get_json(silent=True) or {}
    live = bool(body.get("live"))
    date_str = body.get("date") or None

    if live:
        if not _switch_on():
            note = "" if _live_env_enabled() else " This bot also needs LIVE_TRADING=1 in its .env on the server."
            return (jsonify(error=f'Real money is off. Turn it on first by typing "{LIVE_CONFIRM_PHRASE}".{note}'),
                    409)
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
        summary = settings_summary(_paths().config_path)
        if summary["settings_error"]:
            return jsonify(error=summary["settings_error"]), 409
    if live or _practice_real_prices():
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


@bp.post("/api/run/restart")
def api_run_restart():
    """Stop and start the run going now, so it uses the saved settings. Same mode
    as before, except that it is practice if Real money was switched off since."""
    runner = _runner()
    live = runner.mode == "LIVE" and _armed()
    if not live:
        summary = settings_summary(_paths().config_path)
        if summary["settings_error"]:
            return jsonify(error=summary["settings_error"]), 409
    try:
        runner.restart(live=live)
    except RunnerBusy as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(runner.snapshot()), 202


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
    # What runs are doing now: the run going (if any), else what the next run will be.
    live_capable = _live_env_enabled()
    runner = _runner()
    if runner.status == "running":
        mode = Mode(runner.mode)
    else:
        mode = resolve_mode(_switch_on(), {"LIVE_TRADING": "1" if live_capable else "0"})
    return jsonify(
        {
            "mode": mode.value,
            "running": runner.status == "running",
            "live_switch_on": _switch_on(),
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
    from ..clock import now_utc, to_market
    from ..imports import read_image, title_dates
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify(error="Choose a CSV, JPEG, or PNG file"), 400
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in {".csv", ".jpg", ".jpeg", ".png"}:
        return jsonify(error="Choose a CSV, JPEG, or PNG file"), 400
    try:
        trade_md = expiration_md = None
        if suffix == ".csv":
            text = upload.read().decode("utf-8-sig")
        else:
            with tempfile.TemporaryDirectory(prefix="coopa-image-") as tmp:
                path = Path(tmp) / ("image" + suffix)
                upload.save(path)
                read = read_image(path)
            text, trade_md, expiration_md = read.csv, read.trade_date, read.expiration
        # Suggested from the title for a person to check; never used without the preview.
        trade, expiration = title_dates(text, to_market(now_utc()).date(), trade_md, expiration_md)
        return jsonify(csv=text, image=suffix != ".csv",
                       date=trade.isoformat() if trade else None,
                       expiration=expiration.isoformat() if expiration else None)
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
    body = request.get_json(silent=True) or {}
    try:
        preview = _preview_signer().loads(body.get("token", ""), max_age=3600)
    except (BadSignature, TypeError):
        return jsonify(error="Preview expired or changed. Preview the picks again."), 400
    return jsonify(**confirm_picks(preview["csv"], preview["date"]))


def confirm_picks(csv_text: str, trade_date: str) -> dict:
    """Save a previewed list as the day's confirmed picks (dashboard and Telegram).

    Allowed while a run is active: that run reads confirmed lists on every tick
    and buys the picks at once if the time is inside the buying window.
    """
    from ..picks import save_confirmed_list
    runner = _runner()
    with runner._lock:
        path = save_confirmed_list(_paths().db_path, csv_text, trade_date)
        _paths().signals_path = str(path)
        running = runner.status == "running"
        mode = "practice" if runner.mode == "DRY-RUN" else "LIVE"
    if running:
        message = (f"Picks confirmed. The {mode} run in progress buys them now if it is inside the "
                   "buying window (and if the run is for this trade date).")
    else:
        message = "Picks confirmed. No run is active: start today's run to buy them."
    return {"signals_path": str(path), "date": trade_date, "message": message, "run_active": running}


# -- Telegram intake ----------------------------------------------------
def _telegram():
    return current_app.config.get("COOPA_TELEGRAM")


@bp.get("/api/telegram")
def api_telegram():
    bot = _telegram()
    if bot is None:
        return jsonify(configured=False, pending=[],
                       hint="Telegram is off. Add TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS to .env "
                            "(see docs/TELEGRAM_SETUP.md), then restart the app (or the server's coopa-trada service).")
    return jsonify(configured=True, allowed_chats=len(bot.allowed), pending=bot.pending_previews(),
                   ignored_chats=sorted(bot.ignored_chats))


@bp.post("/api/telegram/<pid>/<action>")
def api_telegram_resolve(pid: str, action: str):
    bot = _telegram()
    if bot is None or action not in ("confirm", "reject"):
        return jsonify(error="Not found"), 404
    outcome = bot.resolve(pid, confirm=action == "confirm", via="dashboard")
    status = 200 if outcome["ok"] else 409
    return jsonify(message=outcome["message"], pending=bot.pending_previews()), status
