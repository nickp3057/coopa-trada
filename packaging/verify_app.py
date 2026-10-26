"""Verify the double-click app keeps dry run as the default and every live gate.

    python packaging/verify_app.py --app "dist/Coopa Trada.app"   # the built app
    python packaging/verify_app.py --source                       # same checks, from source

Runs the app in a throwaway home folder with a scrubbed environment (no Schwab
keys, no token, HOME pointed at the temp folder), so nothing here can reach
Schwab or touch real files. The fake ``.env`` written in phase 2 contains only
``LIVE_TRADING=1``.

Checks:
  1. First run: example files created, .env NOT created, mode is DRY-RUN.
  2. Live start refused without the typed phrase, then without LIVE_TRADING=1.
  3. A dry run completes on synthetic data with no token.
  4. With LIVE_TRADING=1 in the .env next to the app: live refused because
     Schwab is not set up.
  5. Alerts: Settings lists the channels (log-only here, so nothing pops up) and
     Send test alert reports a delivery; keep-awake is reported and released
     after the dry run.
  6. Quit works and the process exits.
  7. (--app) the bundled picture reader runs without missing-library errors.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PHRASE = "ENABLE LIVE TRADING"


class Failed(AssertionError):
    pass


def check(condition, message):
    if not condition:
        raise Failed(message)
    print(f"  ok  {message}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def call(port, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


class App:
    def __init__(self, target: str, app_path: Path | None, home: Path):
        self.target, self.app_path, self.home = target, app_path, home
        self.proc = None
        self.port = None

    def command(self):
        if self.target == "app":
            return [str(self.home / self.app_path.name / "Contents" / "MacOS" / "Coopa Trada")]
        return [sys.executable, "-m", "coopa_trada.desktop"]

    def start(self):
        self.port = free_port()
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.home / "user-home"),  # isolates ~/.coopa_trada locks
            "TMPDIR": tempfile.gettempdir(),
            "COOPA_TRADA_NO_BROWSER": "1",
            "COOPA_TRADA_PORT": str(self.port),
            # Alerts are exercised, but written to the log instead of popping up.
            "COOPA_TRADA_ALERTS_LOG_ONLY": "1",
        }
        if self.target == "source":
            env["PYTHONPATH"] = str(ROOT)
        log = open(self.home / f"launcher-{self.port}.log", "w")
        self.proc = subprocess.Popen(self.command(), cwd=self.home, env=env, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise Failed(f"app exited early with {self.proc.returncode}; see {log.name}")
            try:
                status, info = call(self.port, "GET", "/api/app")
                if status == 200:
                    return info
            except OSError:
                pass
            time.sleep(0.3)
        raise Failed("app did not start within 90 seconds")

    def quit(self):
        status, body = call(self.port, "POST", "/api/app/quit", {})
        check(status == 200, "Quit button stops the app")
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise Failed("app did not exit after quitting")
        check(self.proc.returncode == 0, "process exited cleanly")

    def kill(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


def live_start(port):
    return call(port, "POST", "/api/run/start", {"live": True, "confirm": PHRASE})


def run_checks(target: str, app_path: Path | None) -> None:
    with tempfile.TemporaryDirectory(prefix="coopa-verify-") as tmp:
        home = Path(tmp).resolve()
        (home / "user-home").mkdir()
        if target == "app":
            subprocess.run(["ditto", str(app_path), str(home / app_path.name)], check=True)
        app = App(target, app_path, home)
        try:
            print(f"Phase 1: first run, no .env ({target})")
            info = app.start()
            check(Path(info["home"]).resolve() == home, "home folder is the folder next to the app")
            for name in ("config/rules.yaml", "config/asset_classes.yaml", "data/signals.example.csv",
                         "env.example.txt"):
                check((home / name).is_file(), f"first run created {name}")
            check(not (home / ".env").exists(), "first run did not create .env")
            status, mode = call(app.port, "GET", "/api/mode")
            check(mode["mode"] == "DRY-RUN" and not mode["live_env_enabled"], "mode is DRY-RUN by default")
            status, body = call(app.port, "POST", "/api/run/start", {"live": True})
            check(status == 409 and PHRASE in body["error"], "live refused without the typed phrase")
            status, body = live_start(app.port)
            check(status == 409 and "LIVE_TRADING" in body["error"], "live refused without LIVE_TRADING=1")
            status, body = call(app.port, "POST", "/api/run/start", {"date": "2026-10-15"})
            check(status == 202, "dry run starts without a Schwab login")
            for _ in range(300):
                status, run = call(app.port, "GET", "/api/run/status")
                if run["status"] == "idle":
                    break
                time.sleep(0.1)
            check(run["status"] == "idle" and run["last_error"] is None and run["mode"] == "DRY-RUN",
                  f"dry run completed ({run['last_result']})")
            for _ in range(50):  # the watchdog releases it on its own thread
                if not run["keep_awake"]["active"]:
                    break
                time.sleep(0.1)
                status, run = call(app.port, "GET", "/api/run/status")
            check(run["keep_awake"]["active"] is False and run["keep_awake"]["error"] is None,
                  "keep-awake released after the dry run")
            status, token = call(app.port, "GET", "/api/token")
            check(token["state"] == "not_set_up", "Schwab shows Not set up")
            check("covers_next_run" in token, "login check covers the next run's hold")
            status, al = call(app.port, "GET", "/api/alerts")
            check(status == 200 and al["channels"][0]["channel"] == "Log only", "alert channels listed")
            status, al = call(app.port, "POST", "/api/alerts/test", {})
            check(status == 200 and al["results"][0]["ok"], "Send test alert delivers (log only)")
            if target == "app":
                check(info["image_upload"]["available"], "picture reader available in the app")
            app.quit()

            print("Phase 2: .env next to the app sets LIVE_TRADING=1")
            (home / ".env").write_text("LIVE_TRADING=1\n", encoding="utf-8")
            app.start()
            status, mode = call(app.port, "GET", "/api/mode")
            check(mode["live_env_enabled"], "the .env next to the app is loaded")
            status, body = call(app.port, "POST", "/api/live/switch", {"on": True, "confirm": PHRASE})
            check(status == 200 and body["live_armed"], "real money switched on with the typed phrase")
            status, body = live_start(app.port)
            check(status == 409 and "Schwab is not set up" in body["error"],
                  "live refused because Schwab is not set up")
            status, run = call(app.port, "GET", "/api/run/status")
            check(run["status"] == "idle", "no run was started")
            app.quit()
        finally:
            app.kill()

        if target == "app":
            print("Bundled picture reader")
            helper = home / app_path.name / "Contents" / "Frameworks" / "ocr" / "coopa-recognize"
            if not helper.exists():
                found = list((home / app_path.name).rglob("coopa-recognize"))
                check(bool(found), "picture reader is inside the app")
                helper = found[0]
            result = subprocess.run([str(helper), str(home / "missing.png")], capture_output=True, text=True)
            check("dyld" not in result.stderr and "Library not loaded" not in result.stderr,
                  "picture reader starts (no missing libraries)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--app", type=Path, help="path to the built Coopa Trada.app")
    group.add_argument("--source", action="store_true", help="run the launcher from source")
    args = parser.parse_args(argv)
    if args.app is not None and not args.app.is_dir():
        print(f"No app at {args.app}", file=sys.stderr)
        return 2
    try:
        run_checks("source" if args.source else "app", args.app.resolve() if args.app else None)
    except Failed as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print("All packaging checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
