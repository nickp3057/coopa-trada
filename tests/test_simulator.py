"""Simulator: real engine on a fake clock, three price sources; quote recording; daily logs."""
import json
import logging
import sqlite3
from datetime import date, datetime, time, timedelta, timezone

import pytest

from coopa_trada import simulator as sim
from coopa_trada.models import Right
from coopa_trada.simulator import SimError, SimRequest, bs_price, implied_vol, simulate, vol_from_delta

RULES = "config/rules.example.yaml"
LIST = """Trade date: 2026-10-27; Expiration: 2026-10-30
SYMBOL,OPTION,LAST PRICE,TVT-DELTA,SCHWAB DELTA
AMD,260C,1.00,66.0,0.55
SNOW,230C,1.20,70.1,0.62
"""
TUE, WED = date(2026, 10, 27), date(2026, 10, 28)


def req(**kw):
    base = {"list_csv": LIST, "buy_day": "2026-10-27", "prices": "scenario", "shape": "flat"}
    return SimRequest.from_dict({**base, **kw})


def kinds(result, kind):
    return [e for e in result["events"] if e["kind"] == kind]


def test_flat_prices_buy_at_the_confirm_time_and_sell_by_955_next_day():
    r = simulate(req(), rules_path=RULES)
    assert r["summary"]["bought"] == 2 and r["summary"]["still_open"] == 0
    for t in r["trades"]:
        entry = datetime.fromisoformat(t["entry_at"]).astimezone(sim.MT)
        exit_ = datetime.fromisoformat(t["exit_at"]).astimezone(sim.MT)
        assert (entry.date(), entry.time()) == (TUE, time(13, 40))
        assert (exit_.date(), exit_.time()) == (WED, time(9, 55))
        assert t["why"].startswith("time exit")


def test_real_store_and_clock_are_untouched(tmp_path, monkeypatch):
    import socket
    from coopa_trada import clock
    real_now, real_connect = clock.now_utc, socket.socket.connect
    simulate(req(), rules_path=RULES)
    assert clock.now_utc is real_now and socket.socket.connect is real_connect


def test_rise_hits_the_take_profit():
    r = simulate(req(shape="rise"), rules_path=RULES)
    assert {t["why"] for t in r["trades"]} == {"take-profit"}
    assert r["summary"]["realized_pnl"] > 0


def test_first_sell_unfilled_is_repriced():
    # The bot only ever lowers a sell's price, so the bid has to fall for a reprice.
    r = simulate(req(first_sell_unfilled=True, shape="drop"), rules_path=RULES)
    texts = [e["text"] for e in r["events"]]
    assert any("sits unfilled" in t for t in texts)
    assert any("canceled" in t for t in texts)
    assert r["summary"]["still_open"] == 0


def test_frames_carry_positions_and_triggers():
    r = simulate(req(), rules_path=RULES)
    held = [f for f in r["frames"] if f["positions"]]
    assert held
    p = held[0]["positions"][0]
    assert p["plan"] == "entry_day"
    assert p["triggers"]["take_profit"] == pytest.approx(p["ref"] * 1.6, rel=1e-3)
    assert set(r["series"]) == {x["osi"] for x in r["picks"]}


def test_end_time_stops_the_run_and_leaves_positions_open():
    r = simulate(req(end_at="08:00"), rules_path=RULES)
    assert r["outcome"] == "reached the end time" and r["summary"]["still_open"] == 2


def test_another_buy_day_moves_the_expiration():
    r = simulate(req(buy_day="2026-10-20"), rules_path=RULES)
    assert {p["expiration"] for p in r["picks"]} == {"2026-10-23"}
    assert any("moves -7 days" in e["text"] for e in kinds(r, "note"))


def test_unknown_tickers_are_guessed_not_skipped():
    text = LIST + "ZZZQ,10C,0.50,60,0.5\n"
    r = simulate(req(list_csv=text), rules_path=RULES)
    assert "ZZZQ 10C" in {p["label"] for p in r["picks"]}
    assert any("ZZZQ (stock)" in e["text"] for e in kinds(r, "note"))


@pytest.mark.parametrize("change, message", [
    ({"buy_day": "2026-10-25"}, "not a trading day"),
    ({"prices": "magic"}, "Prices must be"),
    ({"shape": "zigzag"}, "Unknown price shape"),
    ({"list_csv": ""}, "Pick a list"),
])
def test_bad_requests_are_explained(change, message):
    with pytest.raises(SimError, match=message):
        simulate(req(**change), rules_path=RULES)


def test_black_scholes_round_trips():
    p = bs_price(100, 105, 30 / 365, 0.5, Right.CALL)
    assert implied_vol(p, 100, 105, 30 / 365, Right.CALL) == pytest.approx(0.5, abs=1e-4)
    from statistics import NormalDist
    import math
    years, vol = 30 / 365, 0.4
    d1 = (math.log(100 / 95) + (sim.RATE + vol * vol / 2) * years) / (vol * math.sqrt(years))
    assert vol_from_delta(NormalDist().cdf(d1), 100, 95, years, Right.CALL) == pytest.approx(vol, abs=1e-3)


def test_stock_prices_drive_the_option(monkeypatch):
    """A stock that climbs 5% after the buy lifts the call through the take-profit."""
    def bars(symbols, days, cal, cache_dir, notes):
        out = {}
        for s in set(symbols):
            series = []
            for d in days:
                t, end = cal.session_open(d), cal.session_close(d)
                while t <= end:
                    after = t.astimezone(timezone.utc) > sim.mt(TUE, time(13, 45))
                    series.append((t.astimezone(timezone.utc), 260.0 * (1.05 if after else 1.0)))
                    t += timedelta(minutes=1)
            out[s] = series
        return out
    monkeypatch.setattr(sim, "fetch_stock_bars", bars)
    text = "Trade date: 2026-10-27; Expiration: 2026-10-30\nSYMBOL,OPTION,LAST PRICE,TVT-DELTA,SCHWAB DELTA\nAMD,265C,2.00,60,0.45\n"
    r = simulate(req(list_csv=text, prices="stock"), rules_path=RULES)
    assert r["trades"] and r["trades"][0]["why"] == "take-profit"
    assert "volatility" in r["picks"][0]["shape"]


def test_recorded_quotes_are_replayed(tmp_path):
    from coopa_trada.store import Store
    db = tmp_path / "bot.db"
    osi = "AMD   261030C00260000"
    with Store.open(db) as store:
        t = sim.mt(TUE, time(13, 30))
        while t < sim.mt(WED, time(10, 30)):
            store.record_quote(osi, 0.99, 1.01, 1.0, at=t, real=True, mode="LIVE")
            t += timedelta(minutes=1)
    r = simulate(req(prices="recorded"), rules_path=RULES, db_path=db)
    assert [t["osi"] for t in r["trades"]] == [osi]  # SNOW has no recordings: skipped
    assert any("SNOW 230C" in e["text"] and "no Schwab quotes" in e["text"] for e in kinds(r, "note"))


def test_cli_writes_result_log_and_status(tmp_path):
    from coopa_trada.__main__ import main
    request = tmp_path / "req.json"
    request.write_text(json.dumps({"list_csv": LIST, "buy_day": "2026-10-27", "shape": "rise"}))
    assert main(["simulate", str(request), str(tmp_path / "out"), "--config", RULES, "--db", str(tmp_path / "none.db")]) == 0
    assert json.loads((tmp_path / "out/status.json").read_text())["state"] == "done"
    assert "take-profit" in (tmp_path / "out/log.txt").read_text()


# -- quote recording ----------------------------------------------------------
def test_engine_records_every_quote_it_reads(tmp_path):
    from test_hold_logic import buy, engine
    from coopa_trada.store import Store
    with Store.open(tmp_path / "t.db") as store:
        buy(store, date(2026, 9, 2))
        eng, _ = engine(store, datetime(2026, 9, 3, 9, tzinfo=sim.clock.MARKET_TZ), lambda now: 1.05)
        eng.run(date(2026, 9, 3))
        rows = store.conn.execute("SELECT COUNT(*), MIN(real) FROM quote_log").fetchone()
        assert rows[0] > 10 and rows[1] == 0  # synthetic quotes are marked not real


# -- daily logs ---------------------------------------------------------------
def test_daily_log_files_and_reader(tmp_path):
    from coopa_trada.logs import DailyFileHandler, log_days, read_log
    db = tmp_path / "trada.db"
    handler = DailyFileHandler(tmp_path / "logs")
    log = logging.getLogger("coopa_trada.test_daily")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        log.info("bought IBIT")
        log.warning("quote missing for IBIT")
        logging.getLogger("werkzeug").addHandler(handler)
        logging.getLogger("werkzeug").warning("kept")
        logging.getLogger("werkzeug").info("GET / 200 (dropped)")
    finally:
        log.removeHandler(handler)
        logging.getLogger("werkzeug").removeHandler(handler)
        handler.close()
    (day,) = log_days(db)
    out = read_log(db, day)
    assert [l["message"] for l in out["lines"]] == ["bought IBIT", "quote missing for IBIT", "kept"]
    assert [l["message"] for l in read_log(db, day, level="WARNING")["lines"]] == ["quote missing for IBIT", "kept"]
    assert len(read_log(db, day, text="ibit")["lines"]) == 2
    assert read_log(db, day, after=2)["lines"][0]["message"] == "kept"
    with pytest.raises(ValueError):
        read_log(db, "../../etc/passwd")
