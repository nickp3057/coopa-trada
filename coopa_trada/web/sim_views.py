"""Simulator and Activity pages (debugging views).

The simulator itself runs in its own process (``python -m coopa_trada simulate``)
so its fake clock and network block can never touch the real bot running in
this one. Each simulation keeps its request, result and log in
``simulations/<id>/`` next to the database; the newest ``KEEP`` are kept.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request, send_file

from .. import clock

bp = Blueprint("sim", __name__)
log = logging.getLogger("coopa_trada.web.simulator")
KEEP = 50
_ID = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{6}$")
_LIST_NAME = re.compile(r"^[A-Za-z0-9._-]+\.csv$")
_lock = threading.Lock()
_running: dict[str, subprocess.Popen] = {}
BUNDLED_SEEDS = Path(__file__).resolve().parent.parent / "sim_seeds"


def _db_path() -> Path:
    return Path(current_app.config["COOPA_PATHS"].db_path).resolve()


def _sims_dir() -> Path:
    return _db_path().parent / "simulations"


def _seed_dir() -> Path:
    return _db_path().parent / "sim-seeds"


# -- pages ---------------------------------------------------------------------
@bp.get("/simulator")
def sim_page():
    return render_template("simulator.html", page="simulator")


@bp.get("/activity")
def activity_page():
    return render_template("activity.html", page="activity")


# -- lists to start from -----------------------------------------------------------
def _list_files() -> list[tuple[str, str, Path]]:
    """(id, group, path): this bot's confirmed lists, saved seeds, bundled examples."""
    from ..picks import imports_dir
    out = []
    for group, folder in (("Confirmed lists", imports_dir(_db_path())), ("Saved seeds", _seed_dir()),
                          ("Examples (made up)", BUNDLED_SEEDS)):
        if folder.is_dir():
            for path in sorted(folder.glob("*.csv"), reverse=True):
                out.append((f"{group[:4].lower()}:{path.name}", group, path))
    return out


def _describe_list(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"Trade date:\s*(\d{4}-\d{2}-\d{2})", text)
    rows = max(0, sum(1 for line in text.splitlines() if re.match(r"^[A-Za-z]+,", line)) - 1)
    return {"trade_date": m.group(1) if m else None, "picks": rows}


@bp.get("/api/sim/lists")
def api_sim_lists():
    from ..simulator import PRICE_SOURCES, SHAPES
    lists = []
    seen = set()
    for list_id, group, path in _list_files():
        info = _describe_list(path)
        # Several confirmations of the same day: only the newest is offered.
        key = (group, info["trade_date"]) if group == "Confirmed lists" else (group, path.name)
        if key in seen:
            continue
        seen.add(key)
        name = path.stem if group != "Confirmed lists" else (info["trade_date"] or path.stem)
        lists.append({"id": list_id, "group": group, "name": name, **info})
    return jsonify(lists=lists, shapes=SHAPES, price_sources=list(PRICE_SOURCES),
                   recorded_days=_recorded_days())


def _recorded_days() -> list[str]:
    import sqlite3
    db = _db_path()
    if not db.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT DISTINCT substr(at_utc, 1, 10) FROM quote_log WHERE real = 1 "
                                "ORDER BY 1 DESC LIMIT 60").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [r[0] for r in rows]


@bp.get("/api/sim/list/<path:list_id>")
def api_sim_list(list_id):
    for lid, _group, path in _list_files():
        if lid == list_id:
            return jsonify(csv=path.read_text(encoding="utf-8", errors="replace"), **_describe_list(path))
    return jsonify(error="No such list"), 404


@bp.post("/api/sim/seeds")
def api_sim_save_seed():
    body = request.get_json(silent=True) or {}
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(body.get("name") or "")).strip("-.")[:60]
    text = str(body.get("csv") or "")
    if not name or not text.strip():
        return jsonify(error="Give the seed a name and some picks"), 400
    folder = _seed_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.csv").write_text(text, encoding="utf-8")
    log.info("simulator seed saved: %s", name)
    return jsonify(ok=True, id=f"save:{name}.csv")


# -- running ---------------------------------------------------------------------------
def _command(req_path: Path, out: Path) -> list[str]:
    paths = current_app.config["COOPA_PATHS"]
    args = ["simulate", str(req_path), str(out), "--config", str(Path(paths.config_path).resolve()),
            "--db", str(_db_path())]
    if getattr(sys, "frozen", False):  # the packaged Mac app (see packaging/launch.py)
        return [sys.executable, *args]
    return [sys.executable, "-m", "coopa_trada", *args]


def _prune() -> None:
    folder = _sims_dir()
    runs = sorted((p for p in folder.iterdir() if p.is_dir() and _ID.match(p.name)), reverse=True)
    for old in runs[KEEP:]:
        shutil.rmtree(old, ignore_errors=True)


@bp.post("/api/sim/run")
def api_sim_run():
    from ..simulator import SimError, SimRequest
    body = request.get_json(silent=True) or {}
    try:
        SimRequest.from_dict(body)  # refuse a bad request here, before starting a process
    except SimError as exc:
        return jsonify(error=str(exc)), 400
    with _lock:
        busy = [sid for sid, proc in _running.items() if proc.poll() is None]
        if busy:
            return jsonify(error="A simulation is already running; wait for it to finish", id=busy[0]), 409
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        sim_id = f"{stamp}-{os.urandom(3).hex()}"
        out = _sims_dir() / sim_id
        out.mkdir(parents=True)
        req_path = out / "request.json"
        req_path.write_text(json.dumps(body))
        (out / "status.json").write_text(json.dumps({"state": "starting"}))
        with open(out / "process.log", "w") as plog:
            proc = subprocess.Popen(_command(req_path, out), stdout=plog, stderr=subprocess.STDOUT,
                                    cwd=os.getcwd(), env={**os.environ, "PYTHONUNBUFFERED": "1"})
        _running[sim_id] = proc
        _prune()
    log.info("simulation %s started: list for %s, buy day %s, prices %s, shape %s", sim_id,
             _describe_list_text(body.get("list_csv", "")), body.get("buy_day"), body.get("prices"),
             body.get("shape"))
    return jsonify(id=sim_id)


def _describe_list_text(text: str) -> str:
    m = re.search(r"Trade date:\s*(\d{4}-\d{2}-\d{2})", text or "")
    return m.group(1) if m else "a pasted list"


def _folder(sim_id: str) -> Path | None:
    if not _ID.match(sim_id):
        return None
    folder = _sims_dir() / sim_id
    return folder if folder.is_dir() else None


def _status(folder: Path) -> dict:
    try:
        status = json.loads((folder / "status.json").read_text())
    except (OSError, ValueError):
        status = {"state": "starting"}
    proc = _running.get(folder.name)
    if status.get("state") in ("starting", "running") and proc is not None and proc.poll() is not None:
        tail = (folder / "process.log").read_text(errors="replace")[-1500:] if (folder / "process.log").is_file() else ""
        status = {"state": "error", "error": "The simulator stopped unexpectedly.", "detail": tail}
    elif status.get("state") in ("starting", "running") and proc is None:
        # Started before this app was restarted: it can no longer be finishing.
        status = {"state": "error", "error": "Interrupted (the app restarted while it ran)."}
    return status


@bp.get("/api/sim/<sim_id>/status")
def api_sim_status(sim_id):
    folder = _folder(sim_id)
    if folder is None:
        return jsonify(error="No such simulation"), 404
    status = _status(folder)
    if status.get("state") in ("done", "error"):
        _running.pop(sim_id, None)
        if not (folder / "logged").exists():
            (folder / "logged").touch()
            if status["state"] == "done":
                log.info("simulation %s finished: %s; %s", sim_id, status.get("outcome"), status.get("summary"))
            else:
                log.warning("simulation %s failed: %s", sim_id, status.get("error"))
    return jsonify(status)


@bp.get("/api/sim/<sim_id>/result")
def api_sim_result(sim_id):
    folder = _folder(sim_id)
    if folder is None or not (folder / "result.json").is_file():
        return jsonify(error="No result for that simulation"), 404
    return send_file(folder / "result.json", mimetype="application/json")


@bp.get("/api/sim/<sim_id>/log")
def api_sim_log(sim_id):
    folder = _folder(sim_id)
    if folder is None or not (folder / "log.txt").is_file():
        return jsonify(error="No log for that simulation"), 404
    return send_file(folder / "log.txt", mimetype="text/plain", as_attachment=True,
                     download_name=f"simulation-{sim_id}.txt")


@bp.get("/api/sim/history")
def api_sim_history():
    folder = _sims_dir()
    out = []
    if folder.is_dir():
        for run in sorted((p for p in folder.iterdir() if p.is_dir() and _ID.match(p.name)), reverse=True)[:KEEP]:
            try:
                req = json.loads((run / "request.json").read_text())
            except (OSError, ValueError):
                continue
            status = _status(run)
            started = datetime.strptime(run.name[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            out.append({"id": run.name, "started": started.isoformat(), "state": status.get("state"),
                        "outcome": status.get("outcome") or status.get("error"), "summary": status.get("summary"),
                        "buy_day": req.get("buy_day"), "prices": req.get("prices"), "shape": req.get("shape"),
                        "list": _describe_list_text(req.get("list_csv", "")), "request": req})
    return jsonify(runs=out)


# -- activity (the bot's own log) -------------------------------------------------------
@bp.get("/api/activity/days")
def api_activity_days():
    from ..logs import log_days
    return jsonify(days=log_days(_db_path()), today=clock.to_display(clock.now_utc()).date().isoformat())


@bp.get("/api/activity")
def api_activity():
    from ..logs import read_log
    day = request.args.get("day") or clock.to_display(clock.now_utc()).date().isoformat()
    try:
        after = int(request.args.get("after", 0))
    except ValueError:
        after = 0
    try:
        return jsonify(read_log(_db_path(), day, level=request.args.get("level", ""),
                                text=request.args.get("q", ""), after=after))
    except ValueError:
        return jsonify(error="day must be a date"), 400


@bp.get("/api/activity/download")
def api_activity_download():
    from ..logs import logs_dir
    from datetime import date
    day = request.args.get("day", "")
    try:
        date.fromisoformat(day)
    except ValueError:
        return jsonify(error="day must be a date"), 400
    path = logs_dir(_db_path()) / f"{day}.log"
    if not path.is_file():
        return jsonify(error="No log for that day"), 404
    return send_file(path, mimetype="text/plain", as_attachment=True, download_name=f"coopa-trada-{day}.log")


# -- what the bot is watching right now --------------------------------------------------
@bp.get("/api/watch")
def api_watch():
    """Every open position with its latest price and each exit rule's trigger price."""
    from ..calendar_svc import CalendarService
    from ..rules import load_rules
    from ..simulator import _triggers
    from ..store import Store
    paths = current_app.config["COOPA_PATHS"]
    try:
        rules = load_rules(paths.config_path)
    except Exception as exc:  # noqa: BLE001
        return jsonify(error=f"Settings file problem: {exc}"), 500
    store = Store.open(paths.db_path)
    try:
        rows = store.open_positions()
        out = []
        cal = CalendarService()
        for row in rows:
            fill = float(row["entry_price"])
            mid_at = row["entry_mid"]
            ref = float(mid_at) if (rules.exit_reference_price == "mid_at_entry" and mid_at
                                    and 0 < mid_at <= fill) else fill
            peak = max(ref, float(row["stored_peak"]) if row["stored_peak"] is not None else ref)
            q = store.latest_quote(row["osi_symbol"])
            entry_day = row["trading_day"]
            sell_by = None
            try:
                from datetime import date
                nxt = cal.next_session(date.fromisoformat(entry_day))
                sell_by = cal.sell_by(nxt, rules.next_day_sell_by).isoformat() if rules.next_day_time_exit else None
            except Exception:  # noqa: BLE001
                pass
            from ..alerts import contract_text
            out.append({"osi": row["osi_symbol"], "name": contract_text(row["osi_symbol"]),
                        "mode": row["mode"], "qty": row["quantity"],
                        "bought_at": row["entry_at_utc"], "paid": fill, "ref": ref, "peak": peak,
                        "entry_day": entry_day, "sell_by": sell_by,
                        "mid": q["mid"] if q else None, "bid": q["bid"] if q else None,
                        "quoted_at": q["at_utc"] if q else None,
                        "triggers": _triggers(rules, ref, peak)})
    finally:
        store.close()
    return jsonify(positions=out)
