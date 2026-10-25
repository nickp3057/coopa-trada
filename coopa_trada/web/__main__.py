"""``python -m coopa_trada.web`` -- launch the local UI on 127.0.0.1 only.

    python -m coopa_trada.web [--host 127.0.0.1] [--port 8770]

Environment:
    COOPA_TRADA_CONFIG   path to rules.yaml   (default: config/rules.yaml)
    COOPA_TRADA_DB       path to the SQLite store (default: ./trada.db)
    COOPA_TRADA_SIGNALS  path to the signals CSV  (default: data/signals.example.csv)
    LIVE_TRADING=1        second, independent opt-in required for any live run
"""

from __future__ import annotations

import argparse
import os

from ..config import load_env
from . import create_app  # noqa: F401


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(prog="coopa_trada.web", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="bind address (localhost only by default)")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--debug", action="store_true", help="Flask debug reloader")
    args = parser.parse_args(argv)

    from ..watchdog import Watchdog

    app = create_app()
    paths = app.config["COOPA_PATHS"]
    if not args.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        # Once per serving process (the debug reloader starts a second one).
        from ..autostart import AutoStarter
        watchdog = Watchdog(paths, app.config["COOPA_RUNNER"], app.config["COOPA_ALERTER"],
                            keep_awake=app.config["COOPA_KEEPAWAKE"],
                            auto_start=AutoStarter(paths, app.config["COOPA_RUNNER"])).start()
        app.config["COOPA_RUNNER"].on_change = watchdog.poke
        app.config["COOPA_WATCHDOG"] = watchdog
        from . import start_telegram
        start_telegram(app)
    print(
        f"Coopa Trada UI on http://{args.host}:{args.port}\n"
        f"  rules   : {paths.config_path}\n"
        f"  store   : {paths.db_path}\n"
        f"  signals : {paths.signals_path}",
        flush=True,
    )
    # threaded=True so the status poll is served while a run holds a worker thread.
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
