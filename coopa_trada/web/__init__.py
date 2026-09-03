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
    app = Flask(__name__)
    app.config["COOPA_PATHS"] = paths or paths_from_env()
    app.config["COOPA_RUNNER"] = runner or EngineRunner(app.config["COOPA_PATHS"])
    app.register_blueprint(bp)
    return app
