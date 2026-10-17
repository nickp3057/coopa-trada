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

__all__ = ["create_app", "WebPaths", "EngineRunner"]


def create_app(paths: WebPaths | None = None, runner: EngineRunner | None = None) -> Flask:
    import secrets
    from urllib.parse import urlsplit
    from flask import request, jsonify
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(32)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

    @app.before_request
    def same_origin_writes():
        origin = request.headers.get("Origin")
        if request.method == "POST" and origin and urlsplit(origin).netloc != request.host:
            return jsonify(error="Cross-origin writes are not allowed"), 403

    @app.errorhandler(413)
    def oversized(_error):
        return jsonify(error="File is too large; the limit is 8 MB"), 413

    app.config["COOPA_PATHS"] = paths or paths_from_env()
    app.config["COOPA_RUNNER"] = runner or EngineRunner(app.config["COOPA_PATHS"])
    app.register_blueprint(bp)
    return app
