"""Double-click launcher: start the local web UI and open the browser.

Used by the packaged app (PyInstaller) and runnable from source with
``python -m coopa_trada.desktop``. It changes nothing about trading: the same
Flask app, the same engine, the same live gates (typed phrase, LIVE_TRADING=1,
every setting confirmed, a usable Schwab login). Dry run stays the default.

Files live in the app's *home folder*: the folder that contains
``Coopa Trada.app`` (or ``COOPA_TRADA_HOME``)::

    .env                     Schwab keys and LIVE_TRADING; never created by the app
    env.example.txt          template for .env, written on first run
    config/rules.yaml        trading settings (copied from the example on first run)
    config/asset_classes.yaml
    data/signals.example.csv
    trada.db, imports/, schwab_token.json, logs/
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

__all__ = ["app_home", "resource_dir", "is_translocated", "prepare_home", "main"]

APP_ID = "coopa-trada"
DEFAULT_PORT = 8770

# home-relative path -> bundled source; copied only when missing, never overwritten.
FIRST_RUN_FILES = {
    "config/rules.yaml": "config/rules.example.yaml",
    "config/asset_classes.yaml": "config/asset_classes.yaml",
    "data/signals.example.csv": "data/signals.example.csv",
    "env.example.txt": ".env.example",
}


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Where bundled read-only files live."""
    if frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def app_home(executable: str | None = None, environ: dict | None = None) -> Path:
    environ = os.environ if environ is None else environ
    if environ.get("COOPA_TRADA_HOME"):
        return Path(environ["COOPA_TRADA_HOME"]).expanduser().resolve()
    if executable is None and not frozen():
        return Path.cwd().resolve()
    exe = Path(executable or sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent.parent
    return exe.parent


def is_translocated(path: Path) -> bool:
    """macOS runs a downloaded, unmoved app from a hidden read-only copy."""
    return "/AppTranslocation/" in str(path)


def prepare_home(home: Path, resources: Path) -> list[str]:
    """Create missing first-run files; return what was created."""
    created = []
    for target, source in FIRST_RUN_FILES.items():
        dest = home / target
        if dest.exists() or not (resources / source).is_file():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resources / source, dest)
        created.append(target)
    return created


def _running_instance(port: int, home: Path) -> bool:
    """True when this home's app already answers on ``port``."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/app", timeout=1) as resp:
            info = json.load(resp)
    except (OSError, ValueError):
        return False
    return info.get("app") == APP_ID and info.get("home") == str(home)


def _port_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _redirect_output(home: Path) -> None:
    """A double-clicked app has no terminal: keep output in logs/."""
    logs = home / "logs"
    logs.mkdir(exist_ok=True)
    stream = open(logs / "coopa-trada.log", "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = stream


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coopa-trada-desktop")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    parser.add_argument("--port", type=int, default=int(os.environ.get("COOPA_TRADA_PORT", DEFAULT_PORT)))
    args = parser.parse_args(argv)
    no_browser = args.no_browser or os.environ.get("COOPA_TRADA_NO_BROWSER") == "1"

    home = app_home()
    if frozen():
        _redirect_output(home)
    if is_translocated(home):
        print("Coopa Trada is running from a temporary copy macOS made of the download. "
              "Move 'Coopa Trada.app' into its own folder (for example Documents/Coopa Trada) "
              "with Finder, then open it from there.", flush=True)
        return 2

    os.chdir(home)  # every default path (config/, trada.db, .env, token) is home-relative
    prepare_home(home, resources=resource_dir())

    from .config import load_env
    from .logs import setup_logging

    # Only the .env next to the app; never one found by searching elsewhere.
    load_env(home / ".env")
    setup_logging()

    if _running_instance(args.port, home):
        if not no_browser:
            webbrowser.open(f"http://127.0.0.1:{args.port}/")
        print(f"Coopa Trada is already running on port {args.port}; opened it.", flush=True)
        return 0
    port = args.port if _port_free(args.port) else _free_port()

    from werkzeug.serving import make_server
    from .web import create_app

    from .watchdog import Watchdog

    app = create_app()
    server = make_server("127.0.0.1", port, app, threaded=True)
    from .autostart import AutoStarter
    watchdog = Watchdog(app.config["COOPA_PATHS"], app.config["COOPA_RUNNER"],
                        app.config["COOPA_ALERTER"], keep_awake=app.config["COOPA_KEEPAWAKE"],
                        auto_start=AutoStarter(app.config["COOPA_PATHS"], app.config["COOPA_RUNNER"])).start()
    app.config["COOPA_RUNNER"].on_change = watchdog.poke
    app.config["COOPA_WATCHDOG"] = watchdog
    from .web import start_telegram
    telegram = start_telegram(app)
    app.config["COOPA_HOME"] = str(home)
    # Delayed so the "has quit" reply finishes sending before the server stops.
    app.config["COOPA_SHUTDOWN"] = lambda: threading.Timer(0.5, server.shutdown).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"Coopa Trada on {url} (home: {home})", flush=True)
    if not no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runner = app.config["COOPA_RUNNER"]
        runner.stop()
        deadline = time.monotonic() + 10
        while runner.status == "running" and time.monotonic() < deadline:
            time.sleep(0.1)
        watchdog.stop()
        if telegram is not None:
            telegram.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
