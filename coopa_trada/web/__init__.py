"""Local web UI for Coopa Trada.

A thin Flask app that wraps the existing engine *as a library* -- it reuses
``coopa_trada.wiring.build_engine``, ``coopa_trada.store.Store`` and
``coopa_trada.rules.load_rules`` directly and never shells out to the CLI.
It reads and writes the same ``config/rules.yaml`` and the same SQLite store
the CLI uses; there is no second source of truth.

Run it with ``python -m coopa_trada.web`` (binds 127.0.0.1 only).
"""

from __future__ import annotations

from flask import Flask

from .config import WebPaths, paths_from_env
from .runner import EngineRunner
from .views import bp

__all__ = ["create_app", "start_telegram", "WebPaths", "EngineRunner"]


def create_app(paths: WebPaths | None = None, runner: EngineRunner | None = None,
               alerter=None) -> Flask:
    import secrets
    from urllib.parse import urlsplit
    from flask import request, jsonify
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(32)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

    import hmac
    import os

    password = os.environ.get("DASHBOARD_PASSWORD", "")

    @app.before_request
    def require_password():
        # Optional HTTP Basic login for a dashboard reached over a network (a
        # server). Any user name; the password is DASHBOARD_PASSWORD from .env.
        if not password:
            return None
        auth = request.authorization
        given = (auth.password or "") if auth is not None else ""
        if hmac.compare_digest(given.encode(), password.encode()):
            return None
        return app.response_class("Coopa Trada: password required", 401,
                                  {"WWW-Authenticate": 'Basic realm="Coopa Trada"'})

    @app.context_processor
    def logged_in_user():
        # Set by the login website (coopa_trada.gateway) in front of this copy; only
        # believed when the request comes from this machine.
        user = request.headers.get("X-Coopa-User") if request.remote_addr in ("127.0.0.1", "::1") else None
        return {"gateway_user": user}

    @app.before_request
    def same_origin_writes():
        origin = request.headers.get("Origin")
        hosts = {request.host}
        # Behind a proxy on this same machine (e.g. `tailscale serve`), the browser's
        # host arrives as X-Forwarded-Host. Only trusted from a local proxy.
        if request.remote_addr in ("127.0.0.1", "::1") and request.headers.get("X-Forwarded-Host"):
            hosts.add(request.headers["X-Forwarded-Host"])
        if request.method == "POST" and origin and urlsplit(origin).netloc not in hosts:
            return jsonify(error="Cross-origin writes are not allowed"), 403

    @app.errorhandler(413)
    def oversized(_error):
        return jsonify(error="File is too large; the limit is 8 MB"), 413

    from .. import alerts
    app.config["COOPA_PATHS"] = paths or paths_from_env()
    app.config["COOPA_ALERTER"] = alerter or alerts.from_env()
    runner = runner or EngineRunner(app.config["COOPA_PATHS"])
    if runner.alerter is None:
        runner.alerter = app.config["COOPA_ALERTER"]
    app.config["COOPA_RUNNER"] = runner
    from ..keepawake import KeepAwake
    # Held only once a watchdog is started (desktop app / python -m coopa_trada.web).
    app.config["COOPA_KEEPAWAKE"] = KeepAwake()
    app.register_blueprint(bp)

    # Telegram intake (optional): built here so its Confirm button saves picks exactly
    # like the dashboard; polling starts only in the real app (see start_telegram).
    from .. import telegram_bot

    def confirm_from_telegram(csv_text, trade_date):
        from .views import confirm_picks
        with app.app_context():
            return confirm_picks(csv_text, trade_date)

    app.config["COOPA_TELEGRAM"] = telegram_bot.from_env(
        confirm=confirm_from_telegram, run_status=runner.snapshot)
    return app


def start_telegram(app):
    """Start long-polling Telegram when it is configured. Returns the poller or None."""
    from ..telegram_bot import TelegramPoller
    intake = app.config.get("COOPA_TELEGRAM")
    if intake is None:
        return None
    poller = TelegramPoller(intake).start()
    app.config["COOPA_TELEGRAM_POLLER"] = poller
    return poller
