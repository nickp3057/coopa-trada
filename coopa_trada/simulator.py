"""Simulator: replay a pick list on chosen dates and times, for debugging.

    python -m coopa_trada simulate REQUEST.json OUT_FOLDER

It runs the real engine, exit rules and Schwab order code (repricing included)
against a pretend Schwab on a fake clock, in a throwaway database, with this
bot's own settings. Nothing is sent to Schwab and the real trade log is never
touched. It always runs in its own process (the web page starts one), because
it swaps the clock and blocks the network while it runs.

Where the prices come from (``prices`` in the request):

* ``scenario``: made-up paths with a chosen shape per pick (rise, drop, spike
  then fade, ...). Exercises every rule; says nothing about the picks.
* ``stock``: the underlying's real one-minute prices from Schwab (the last ~48
  days), turned into an option price with Black-Scholes. The volatility is set
  so the option costs the list's LAST PRICE when the list is confirmed, then
  held constant. Schwab has no option price history, so this is an estimate.
* ``recorded``: real Schwab option quotes this bot saved while it ran (the
  ``quote_log`` table). Only days the bot watched, and only contracts it quoted.

The output is one JSON file: every pick's price path, a frame per engine tick
(positions, peaks, each rule's trigger price), every event and log line, and
the trades. The Simulator page plays it back.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import socket
import tempfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import NormalDist

import httpx

from . import clock
from .alerts import Alerter, contract_text
from .calendar_svc import CalendarService
from .contracts import ContractNotTradable, ResolvedContract, osi_symbol
from .exits import Quote
from .models import CsvSignalSource, Right, Signal

__all__ = ["SimRequest", "simulate", "SHAPES", "PRICE_SOURCES"]

UTC = timezone.utc
MT = clock.DISPLAY_TZ
PRICE_SOURCES = ("scenario", "stock", "recorded")
SHAPES = {
    "mixed": "A different shape for each pick (cycles through the list below)",
    "rise": "Climbs steadily: +10% by the close, opens +30%, +75% next morning",
    "drop": "Falls: -15% by the close, opens -35%, keeps sliding",
    "spike_fade": "Jumps +45% right after buying, then gives it all back",
    "gap_up_fade": "Flat into the close, opens +50%, fades to +10%",
    "gap_down": "Flat into the close, opens -45%",
    "flat": "Barely moves (tests the next-day sell-by time)",
    "random": "Random walk (same seed = same path)",
}
_CYCLE = ["rise", "spike_fade", "drop", "gap_up_fade", "random", "flat", "gap_down"]
RATE = 0.04  # risk-free rate for Black-Scholes
NORMAL = NormalDist()


class SimError(ValueError):
    """A request that cannot be simulated; the message is for a person."""


@dataclass
class SimRequest:
    list_csv: str
    buy_day: date
    confirm_at: time = time(13, 40)        # MT, when the list is confirmed
    start_at: time = time(13, 0)           # MT, when the buy day's run starts
    end_day: date | None = None            # default: the next trading day
    end_at: time = time(10, 0)             # MT, when the simulation stops on end_day
    prices: str = "scenario"
    shape: str = "mixed"
    shapes: dict = field(default_factory=dict)  # per pick label, e.g. {"IBIT 44P": "rise"}
    seed: int = 1
    spread_pct: float = 0.06               # bid-ask gap as a share of the mid
    first_sell_unfilled: bool = False      # the first sell order sits unfilled (tests repricing)
    shift_expiration: bool = True          # keep days-to-expiry when replaying on another date
    rule_overrides: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict) -> "SimRequest":
        def t(key, default):
            value = raw.get(key)
            if not value:
                return default
            hh, mm = str(value).split(":")[:2]
            return time(int(hh), int(mm))

        try:
            buy_day = date.fromisoformat(str(raw["buy_day"]))
            end_day = date.fromisoformat(str(raw["end_day"])) if raw.get("end_day") else None
        except (KeyError, ValueError):
            raise SimError("Pick a buy date") from None
        prices = raw.get("prices", "scenario")
        if prices not in PRICE_SOURCES:
            raise SimError(f"Prices must be one of: {', '.join(PRICE_SOURCES)}")
        shape = raw.get("shape", "mixed")
        shapes = {str(k): str(v) for k, v in (raw.get("shapes") or {}).items() if v}
        for s in [shape, *shapes.values()]:
            if s not in SHAPES:
                raise SimError(f"Unknown price shape {s!r}")
        try:
            spread = float(raw.get("spread_pct", 0.06))
            seed = int(raw.get("seed", 1))
        except (TypeError, ValueError):
            raise SimError("Spread and seed must be numbers") from None
        if not 0 <= spread <= 0.5:
            raise SimError("Spread must be between 0% and 50%")
        if not str(raw.get("list_csv", "")).strip():
            raise SimError("Pick a list or paste one")
        return cls(list_csv=str(raw["list_csv"]), buy_day=buy_day, end_day=end_day,
                   confirm_at=t("confirm_at", time(13, 40)), start_at=t("start_at", time(13, 0)),
                   end_at=t("end_at", time(10, 0)), prices=prices, shape=shape, shapes=shapes,
                   seed=seed, spread_pct=spread,
                   first_sell_unfilled=bool(raw.get("first_sell_unfilled", False)),
                   shift_expiration=bool(raw.get("shift_expiration", True)),
                   rule_overrides=dict(raw.get("rule_overrides") or {}))


def mt(day: date, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=MT).astimezone(UTC)


def pick_label(signal: Signal) -> str:
    return f"{signal.underlying} {signal.strike:g}{'C' if signal.right is Right.CALL else 'P'}"


# -- the fake clock ---------------------------------------------------------
class SimEnd(BaseException):
    """The simulation reached its end time. A BaseException, so the engine's own
    error handling (which alerts on any Exception) lets it through untouched."""


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start.astimezone(UTC)
        self.stop_at: datetime | None = None
        self.on_tick = None  # called before each sleep (one frame per engine tick)

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        if self.on_tick is not None:
            self.on_tick()
        self.now += timedelta(seconds=max(1.0, float(seconds)))
        if self.stop_at is not None and self.now >= self.stop_at:
            raise SimEnd()


# -- price models -----------------------------------------------------------
def _round_quote(mid: float, spread_pct: float) -> Quote:
    mid = max(mid, 0.01)
    half = max(0.005, mid * spread_pct / 2)
    bid = max(0.01, math.floor((mid - half) * 100) / 100)
    ask = max(bid + 0.01, math.ceil((mid + half) * 100) / 100)
    return Quote(round(bid, 2), round(ask, 2), round((bid + ask) / 2, 4))


class ScenarioMarket:
    """Made-up mids: each pick follows its shape from the list's LAST PRICE."""

    def __init__(self, picks: list["SimPick"], cal: CalendarService, req: SimRequest, days: list[date]):
        self.req = req
        self.cal = cal
        self.paths: dict[str, list[tuple[datetime, float]]] = {}
        confirm = mt(req.buy_day, req.confirm_at)
        close1 = cal.session_close(req.buy_day).astimezone(UTC)
        later = days[1:] or [days[0]]
        open2 = cal.session_open(later[0]).astimezone(UTC)
        end = max(self._end(days), open2 + timedelta(hours=1))
        for i, pick in enumerate(picks):
            shape = req.shapes.get(pick.label) or req.shape
            if shape == "mixed":
                shape = _CYCLE[i % len(_CYCLE)]
            pick.shape = shape
            base = pick.base_price
            rng = random.Random(zlib.crc32(f"{req.seed}:{pick.label}".encode()))
            start = cal.session_open(req.buy_day).astimezone(UTC)
            pts = self._shape(shape, base, start, confirm, close1, open2, end, rng)
            self.paths[pick.osi] = pts

    def _end(self, days):
        return mt(days[-1], self.req.end_at)

    @staticmethod
    def _shape(shape, base, start, confirm, close1, open2, end, rng):
        m = lambda t, x: (t, base * x)  # noqa: E731
        mins = lambda a, n: a + timedelta(minutes=n)  # noqa: E731
        if shape == "rise":
            pts = [m(start, 1), m(confirm, 1), m(close1, 1.10), m(open2, 1.30), m(mins(open2, 90), 1.75), m(end, 1.80)]
        elif shape == "drop":
            pts = [m(start, 1), m(confirm, 1), m(close1, 0.85), m(open2, 0.65), m(mins(open2, 120), 0.45), m(end, 0.40)]
        elif shape == "spike_fade":
            pts = [m(start, 1), m(confirm, 1), m(mins(confirm, 8), 1.45), m(close1, 1.0), m(open2, 0.95), m(end, 0.9)]
        elif shape == "gap_up_fade":
            pts = [m(start, 1), m(confirm, 1), m(close1, 1.0), m(open2, 1.5), m(mins(open2, 60), 1.1), m(end, 1.1)]
        elif shape == "gap_down":
            pts = [m(start, 1), m(confirm, 1), m(close1, 1.0), m(open2, 0.55), m(end, 0.5)]
        elif shape == "flat":
            pts = [m(start, 1), m(end, 1.0)]
        else:  # random walk, one point a minute in session, a gap overnight
            pts, price, t = [], base, start
            while t < end:
                pts.append((t, price))
                if close1 <= t < open2:
                    t = open2
                    price *= math.exp(rng.gauss(0, 0.15))
                    continue
                t += timedelta(minutes=1)
                price *= math.exp(rng.gauss(0, 0.012))
            pts.append((end, price))
            return pts
        # A little noise on the shaped paths, so peaks and trails behave like a real tape.
        out = []
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            t = t0
            while t < t1:
                frac = (t - t0) / (t1 - t0)
                out.append((t, (v0 + (v1 - v0) * frac) * (1 + rng.gauss(0, 0.004))))
                t += timedelta(minutes=1) if not (close1 <= t < open2) else (t1 - t)
        out.append(pts[-1])
        return out

    def mid(self, osi: str, now: datetime) -> float:
        pts = self.paths[osi]
        if now <= pts[0][0]:
            return pts[0][1]
        lo, hi = 0, len(pts) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if pts[mid][0] <= now:
                lo = mid
            else:
                hi = mid
        (t0, v0), (t1, v1) = pts[lo], pts[hi]
        if now >= t1:
            return v1
        return v0 + (v1 - v0) * (now - t0) / (t1 - t0)

    def quote(self, osi: str, now: datetime) -> Quote:
        return _round_quote(self.mid(osi, now), self.req.spread_pct)


def bs_price(spot: float, strike: float, years: float, vol: float, right: Right) -> float:
    years = max(years, 1 / (365 * 24 * 60))
    vol = max(vol, 0.01)
    d1 = (math.log(spot / strike) + (RATE + vol * vol / 2) * years) / (vol * math.sqrt(years))
    d2 = d1 - vol * math.sqrt(years)
    disc = math.exp(-RATE * years)
    if right is Right.CALL:
        return spot * NORMAL.cdf(d1) - strike * disc * NORMAL.cdf(d2)
    return strike * disc * NORMAL.cdf(-d2) - spot * NORMAL.cdf(-d1)


def implied_vol(price: float, spot: float, strike: float, years: float, right: Right) -> float | None:
    """The volatility at which Black-Scholes gives ``price`` (bisection), or None."""
    lo, hi = 0.01, 8.0
    if not (bs_price(spot, strike, years, lo, right) <= price <= bs_price(spot, strike, years, hi, right)):
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, years, mid, right) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def vol_from_delta(delta: float, spot: float, strike: float, years: float, right: Right) -> float | None:
    """The volatility matching a quoted delta (smaller root), or None."""
    try:
        d1 = NORMAL.inv_cdf(delta if right is Right.CALL else delta + 1)
    except Exception:  # noqa: BLE001 - delta outside (0, 1)
        return None
    a, b, c = years / 2, -d1 * math.sqrt(years), math.log(spot / strike) + RATE * years
    disc = b * b - 4 * a * c
    if disc < 0:
        return None
    roots = [r for r in ((-b - math.sqrt(disc)) / (2 * a), (-b + math.sqrt(disc)) / (2 * a)) if r > 0.01]
    return min(roots) if roots else None


class StockMarket:
    """Real one-minute stock prices, an estimated option price on top."""

    def __init__(self, picks, cal, req, days, bars: dict[str, list[tuple[datetime, float]]], notes: list):
        self.req = req
        self.bars = bars
        self.picks = {p.osi: p for p in picks}
        self.vol: dict[str, float] = {}
        confirm = mt(req.buy_day, req.confirm_at)
        for pick in picks:
            series = bars.get(pick.underlying) or []
            if not series:
                notes.append(f"{pick.label}: no stock prices from Schwab for these days; it is skipped.")
                continue
            spot = self.spot(pick.underlying, confirm)
            years = self.years(pick, confirm)
            vol = None
            if pick.base_price:
                vol = implied_vol(pick.base_price, spot, pick.strike, years, pick.right)
            how = "the list's last price"
            if vol is None and pick.delta is not None:
                vol = vol_from_delta(pick.delta, spot, pick.strike, years, pick.right)
                how = "the list's Schwab delta"
            if vol is None:
                vol, how = 0.6, "a default (60%), as the list's price and delta did not fit"
            self.vol[pick.osi] = vol
            pick.shape = f"real {pick.underlying} prices, volatility {vol * 100:.0f}% (from {how})"
            notes.append(f"{pick.label}: {pick.underlying} was ${spot:.2f} at the confirm time; "
                         f"option volatility {vol * 100:.0f}% from {how}.")

    def spot(self, symbol: str, now: datetime) -> float:
        series = self.bars[symbol]
        lo, hi = 0, len(series)
        while lo < hi:
            mid = (lo + hi) // 2
            if series[mid][0] <= now:
                lo = mid + 1
            else:
                hi = mid
        return series[max(lo - 1, 0)][1]

    @staticmethod
    def years(pick, now: datetime) -> float:
        expiry = datetime.combine(pick.expiration, time(16), tzinfo=clock.MARKET_TZ)
        return max((expiry - now).total_seconds(), 60) / (365 * 24 * 3600)

    def mid(self, osi: str, now: datetime) -> float:
        pick = self.picks[osi]
        if osi not in self.vol:
            raise ContractNotTradable("no stock prices for this pick")
        spot = self.spot(pick.underlying, now)
        return bs_price(spot, pick.strike, self.years(pick, now), self.vol[osi], pick.right)

    def quote(self, osi: str, now: datetime) -> Quote:
        return _round_quote(self.mid(osi, now), self.req.spread_pct)


class RecordedMarket:
    """Real Schwab option quotes the bot saved while it ran."""

    STALE = timedelta(minutes=15)

    def __init__(self, picks, req, db_path, days, cal, notes):
        import sqlite3
        self.series: dict[str, list[tuple[datetime, Quote]]] = {}
        start = cal.session_open(days[0]).astimezone(UTC) - timedelta(hours=1)
        end = mt(days[-1], req.end_at) + timedelta(hours=1)
        conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
        try:
            for pick in picks:
                try:
                    rows = conn.execute(
                        "SELECT at_utc, bid, ask, mid FROM quote_log WHERE osi_symbol = ? AND real = 1 "
                        "AND at_utc >= ? AND at_utc <= ? ORDER BY at_utc",
                        (pick.osi, start.isoformat(), end.isoformat())).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                pts = [(datetime.fromisoformat(a), Quote(b, k, m)) for a, b, k, m in rows
                       if b is not None and k is not None]
                if pts:
                    self.series[pick.osi] = pts
                    pick.shape = f"{len(pts)} recorded Schwab quotes"
                else:
                    pick.shape = "no recorded quotes"
                    notes.append(f"{pick.label}: the bot saved no Schwab quotes for it on these days; it is skipped.")
        finally:
            conn.close()

    def quote(self, osi: str, now: datetime) -> Quote:
        pts = self.series.get(osi)
        if not pts:
            raise ContractNotTradable("no recorded quotes for this contract")
        best = None
        for at, q in pts:
            if at > now:
                break
            best = (at, q)
        if best is None:
            if pts[0][0] - now > self.STALE:
                raise ContractNotTradable("no recorded quote yet at this time")
            return pts[0][1]
        if now - best[0] > self.STALE and best is not pts[-1]:
            raise ContractNotTradable("recorded quotes have a gap here")
        return best[1]

    def mid(self, osi: str, now: datetime) -> float:
        return self.quote(osi, now).mid


# -- the picks ----------------------------------------------------------------
@dataclass
class SimPick:
    signal: Signal
    label: str
    osi: str
    underlying: str
    right: Right
    strike: float
    expiration: date
    base_price: float
    delta: float | None
    shape: str = ""


# Common ETFs, for tickers this bot has not learned yet (the simulator guesses
# instead of skipping; a guess only changes which exit window applies).
_LIKELY_ETFS = {"SPY", "QQQ", "IWM", "DIA", "IBIT", "TSLL", "SOXS", "SOXL", "USO", "SLV", "GLD", "SMH",
                "SQQQ", "TQQQ", "SPXS", "SPXL", "UVXY", "XLE", "XLF", "XLK", "TLT", "ARKK", "NVDL",
                "KWEB", "FXI", "EEM", "GDX", "HYG", "LQD", "TNA", "TZA", "LABU", "LABD", "BITO"}


def _asset_classes_for(text: str, notes: list) -> str:
    """A temporary asset-class file: what the bot knows, plus a guess for the rest."""
    import yaml

    from .models import DEFAULT_ASSET_CLASSES_PATH, AssetClass, _find_config, load_asset_class_map
    from .tickers import known_classes
    try:
        known = {**known_classes(), **load_asset_class_map(_find_config(DEFAULT_ASSET_CLASSES_PATH))}
    except Exception:  # noqa: BLE001
        known = {}
    guessed = []
    header = False
    for line in text.splitlines():
        cells = [c.strip().upper() for c in line.split(",")]
        if "SYMBOL" in cells and "OPTION" in cells:
            header = True
            continue
        if header and cells and cells[0] and cells[0].isalpha() and cells[0] not in known:
            known[cells[0]] = AssetClass.ETF if cells[0] in _LIKELY_ETFS else AssetClass.SINGLE_NAME
            guessed.append(f"{cells[0]} ({'ETF' if known[cells[0]] is AssetClass.ETF else 'stock'})")
    if guessed:
        notes.append("Tickers this bot has not learned yet, guessed: " + ", ".join(guessed) + ".")
    data = {"single_name": sorted(t for t, a in known.items() if a is AssetClass.SINGLE_NAME),
            "etf": sorted(t for t, a in known.items() if a is AssetClass.ETF)}
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as handle:
        yaml.safe_dump(data, handle)
        return handle.name


def parse_picks(req: SimRequest, notes: list) -> list[SimPick]:
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as handle:
        handle.write(req.list_csv)
        path = handle.name
    classes = _asset_classes_for(req.list_csv, notes)
    try:
        source = CsvSignalSource(path, asset_classes_path=classes)
        header_date = _list_date(req.list_csv)
        signals = source.fetch(header_date or req.buy_day)
    except Exception as exc:  # noqa: BLE001
        raise SimError(f"Could not read the list: {exc}") from None
    finally:
        os.unlink(path)
        os.unlink(classes)
    if not signals:
        raise SimError("The list has no picks the bot can read (check the tickers and the OPTION column)")
    shift = timedelta(0)
    if header_date and header_date != req.buy_day and req.shift_expiration and req.prices != "recorded":
        shift = req.buy_day - header_date
        notes.append(f"The list is for {header_date:%b %-d}; replaying it on {req.buy_day:%b %-d}, so each "
                     f"expiration moves {shift.days:+d} days (same days to expiry).")
    picks = []
    received = mt(req.buy_day, req.confirm_at)
    for s in signals:
        exp = s.expiration + shift if s.expiration else None
        if exp is None:
            continue
        sig = replace(s, expiration=exp, received_at=received)
        base = s.reference_price if s.reference_price and s.reference_price > 0 else 1.0
        picks.append(SimPick(sig, pick_label(sig), osi_symbol(sig.underlying, exp, sig.right, sig.strike),
                             sig.underlying, sig.right, sig.strike, exp, float(base), sig.signal_delta_schwab))
    return picks


def _list_date(text: str) -> date | None:
    import re
    m = re.search(r"Trade date:\s*(\d{4}-\d{2}-\d{2})", text)
    return date.fromisoformat(m.group(1)) if m else None


def fetch_stock_bars(symbols, days, cal, cache_dir: Path, notes) -> dict:
    """One-minute bars from Schwab, cached per symbol and day (past days never change)."""
    from .wiring import schwab_client
    out: dict[str, list] = {}
    client = None
    today = clock.to_market(clock.now_utc()).date()
    for sym in sorted(set(symbols)):
        series = []
        for day in days:
            cache = cache_dir / f"{sym}-{day.isoformat()}.json"
            if cache.is_file():
                candles = json.loads(cache.read_text())
            else:
                if client is None:
                    try:
                        client = schwab_client()
                    except Exception as exc:  # noqa: BLE001
                        raise SimError("Real stock prices need the Schwab login, and it is not working "
                                       f"({exc}). Reconnect Schwab, or use made-up prices.") from None
                start = cal.session_open(day) - timedelta(minutes=5)
                end = cal.session_close(day) + timedelta(minutes=5)
                resp = client.get_price_history_every_minute(sym, start_datetime=start, end_datetime=end,
                                                             need_extended_hours_data=False)
                if resp.status_code == 401:
                    raise SimError("Schwab refused the login (it expired). Reconnect Schwab, or use made-up prices.")
                if resp.status_code != 200:
                    notes.append(f"{sym} {day}: Schwab price history answered {resp.status_code}.")
                    continue
                candles = resp.json().get("candles") or []
                if candles and day < today:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps(candles))
            series += [(datetime.fromtimestamp(c["datetime"] / 1000, UTC), float(c["close"])) for c in candles]
        if not series:
            notes.append(f"{sym}: Schwab has no one-minute prices for these days (it keeps about 48 days).")
        out[sym] = sorted(series)
    return out


# -- pretend Schwab -------------------------------------------------------------
def _response(payload=None, status=200, **headers) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers,
                          request=httpx.Request("GET", "https://pretend-schwab.invalid"))


class SimSchwab:
    """The schwab-py calls the engine makes, answered from the simulated market.

    A limit order fills when the market reaches it: a buy when the ask is at or
    below the limit, a sell when the bid is at or above it, at the limit price.
    With ``first_sell_unfilled`` the first sell order never fills, so the
    cancel-and-reprice path runs.
    """

    def __init__(self, fake: FakeClock, market, events: "Events", first_sell_unfilled: bool):
        self.clock = fake
        self.market = market
        self.events = events
        self.stuck_left = 1 if first_sell_unfilled else 0
        self.orders: list[dict] = []

    def place_order(self, account, spec):
        built = spec.build()
        leg = built["orderLegCollection"][0]
        order = {"id": len(self.orders) + 1, "spec": built, "status": "WORKING", "filled": 0,
                 "price": float(built["price"]), "symbol": leg["instrument"]["symbol"],
                 "instruction": leg["instruction"], "qty": int(leg["quantity"]), "fill_time": None,
                 "stuck": False}
        self.orders.append(order)
        side = "Buy" if order["instruction"] == "BUY_TO_OPEN" else "Sell"
        self.events.add("order", f"{side} order sent: {contract_text(order['symbol'])} x{order['qty']}, "
                                 f"limit ${order['price']:.2f}", symbol=order["symbol"])
        if side == "Sell" and self.stuck_left:
            self.stuck_left -= 1
            order["stuck"] = True
            self.events.add("order", f"Nobody buys at ${order['price']:.2f}: the sell order sits unfilled "
                                     f"(you asked for this to test repricing)", symbol=order["symbol"])
        self._try_fill(order)
        return _response({}, 201, Location=f"https://pretend-schwab.invalid/orders/{order['id']}")

    def _try_fill(self, order) -> None:
        if order["status"] != "WORKING" or order["stuck"]:
            return
        try:
            q = self.market.quote(order["symbol"], self.clock())
        except Exception:  # noqa: BLE001 - no price, no fill
            return
        buy = order["instruction"] == "BUY_TO_OPEN"
        if (buy and q.ask <= order["price"] + 1e-9) or (not buy and q.bid >= order["price"] - 1e-9):
            order.update(status="FILLED", filled=order["qty"], fill_time=self.clock())
            self.events.add("fill", f"{'Bought' if buy else 'Sold'} {contract_text(order['symbol'])} "
                                    f"x{order['qty']} at ${order['price']:.2f}", symbol=order["symbol"])

    def get_order(self, oid, account):
        o = self.orders[int(oid) - 1]
        self._try_fill(o)
        leg = {**o["spec"]["orderLegCollection"][0], "legId": 1}
        execs = ([{"activityType": "EXECUTION", "executionLegs": [
            {"legId": 1, "quantity": o["filled"], "price": o["price"], "time": o["fill_time"].isoformat()}]}]
            if o["filled"] else [])
        return _response({"orderId": o["id"], "status": o["status"], "filledQuantity": o["filled"],
                          "orderLegCollection": [leg], "orderActivityCollection": execs,
                          "orderType": o["spec"]["orderType"], "price": o["price"]})

    def cancel_order(self, oid, account):
        o = self.orders[int(oid) - 1]
        if o["status"] == "WORKING":
            o["status"] = "CANCELED"
            self.events.add("order", f"Order for {contract_text(o['symbol'])} at ${o['price']:.2f} canceled",
                            symbol=o["symbol"])
        return _response({}, 200)

    def get_account(self, account, fields=None):
        held: dict[str, int] = {}
        for o in self.orders:
            sign = 1 if o["instruction"] == "BUY_TO_OPEN" else -1
            held[o["symbol"]] = held.get(o["symbol"], 0) + sign * o["filled"]
        positions = [{"instrument": {"symbol": s}, "longQuantity": q, "shortQuantity": 0}
                     for s, q in held.items() if q]
        return _response({"securitiesAccount": {"positions": positions}})

    def get_orders_for_account(self, account, **kwargs):
        return _response([{"orderId": o["id"], "status": o["status"]} for o in self.orders
                          if o["status"] == "WORKING"])


class SimResolver:
    def __init__(self, picks: list[SimPick], market, fake: FakeClock):
        self.by_id = {p.signal.id: p for p in picks}
        self.market = market
        self.clock = fake

    def resolve(self, signal: Signal) -> ResolvedContract:
        pick = self.by_id[signal.id]
        q = self.market.quote(pick.osi, self.clock())
        return ResolvedContract(pick.osi, pick.underlying, pick.right, pick.strike, pick.expiration,
                                q.bid, q.ask, q.mid, 5000, True, self.clock(), pick.delta)


class ListArrives:
    """The list appears at the confirm time on the buy day (as a Telegram confirm would)."""

    def __init__(self, picks, req, fake, events):
        self.picks = picks
        self.req = req
        self.clock = fake
        self.events = events
        self.announced = False

    def fetch(self, day=None):
        at = mt(self.req.buy_day, self.req.confirm_at)
        if day != self.req.buy_day or self.clock() < at:
            return []
        if not self.announced:
            self.announced = True
            self.events.add("picks", f"The list is confirmed: {len(self.picks)} pick(s): "
                                     + ", ".join(p.label for p in self.picks), at=at)
        return [p.signal for p in self.picks]


# -- what happened ---------------------------------------------------------------
class Events:
    def __init__(self, fake: FakeClock):
        self.clock = fake
        self.items: list[dict] = []

    def add(self, kind: str, text: str, *, at: datetime | None = None, level: str = "INFO", symbol=None):
        self.items.append({"t": _iso(at or self.clock()), "kind": kind, "level": level, "text": text,
                           "symbol": symbol})


class _CaptureLogs(logging.Handler):
    """Every log line the engine writes, stamped with the simulated time."""

    def __init__(self, events: Events):
        super().__init__(logging.DEBUG)
        self.events = events

    def emit(self, record):
        try:
            # The pretend Schwab runs the engine's real-order path, whose lines say [LIVE].
            message = record.getMessage().replace("[LIVE] ", "[SIM] ", 1)
            self.events.add("log", message, level=record.levelname,
                            symbol=None if record.name.startswith("coopa_trada") else record.name)
            self.events.items[-1]["source"] = record.name
        except Exception:  # noqa: BLE001
            pass


class _SimAlerts:
    name = "simulator (shown, not sent)"

    def __init__(self, events: Events):
        self.events = events

    def send(self, alert) -> None:
        self.events.add("alert", f"Alert (not sent): {alert.title}: {alert.message}", level="WARNING")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


_REASON = {
    "TakeProfit": "take-profit", "ProfitPointTrail": "10-point rule", "TrailingPeak": "dropped from its high",
    "StopLoss": "stop-loss", "HardTimeExit": "time exit (sell-by time / end of window)",
    "Reprice": "reprice of an unfilled sell",
}


def _triggers(rules, ref: float, peak: float) -> dict:
    """Each exit rule's trigger price for one position right now (None = not active)."""
    out = {"take_profit": ref * (1 + rules.take_profit_pct),
           "hard_stop": ref * (1 - rules.hard_stop_loss_pct),
           "trailing_stop": peak * (1 - rules.trailing_stop_pct)}
    if rules.profit_trail_points is not None:
        armed = rules.profit_trail_arm == "from_entry" or peak > ref
        out["ten_point"] = (peak - rules.profit_trail_points / 100 * ref) if armed else None
    if rules.cooper_drop_from_peak_pct is not None:
        out["drop_from_peak"] = peak * (1 - rules.cooper_drop_from_peak_pct)
    if rules.cooper_drop_from_entry_pct is not None:
        out["drop_from_entry"] = ref * (1 - rules.cooper_drop_from_entry_pct)
    return {k: (round(v, 4) if v is not None else None) for k, v in out.items()}


# -- running it ----------------------------------------------------------------------
@contextmanager
def no_network():
    real = socket.socket.connect

    def refuse(self, *args, **kwargs):
        raise ConnectionRefusedError("the simulator never uses the network while it runs")

    socket.socket.connect = refuse
    try:
        yield
    finally:
        socket.socket.connect = real


@contextmanager
def fake_now(fake: FakeClock):
    """Every timestamp the app writes (store rows, logs) on the simulated clock."""
    real = clock.now_utc
    clock.now_utc = fake
    try:
        yield
    finally:
        clock.now_utc = real


def session_days(cal: CalendarService, req: SimRequest) -> list[date]:
    if not cal.is_trading_day(req.buy_day):
        raise SimError(f"{req.buy_day:%a %b %-d, %Y} is not a trading day")
    end = req.end_day or cal.next_session(req.buy_day)
    if end < req.buy_day:
        raise SimError("The end date is before the buy date")
    days, d = [req.buy_day], req.buy_day
    while d < end:
        d = cal.next_session(d)
        if d <= end:
            days.append(d)
    if len(days) > 6:
        raise SimError("Pick at most 6 trading days")
    return days


def simulate(req: SimRequest, *, rules_path, db_path=None, cache_dir=None, progress=None) -> dict:
    """Run one simulation; returns the result (also what the page plays back)."""
    from .broker import SchwabBroker
    from .engine import Engine
    from .rules import load_rules
    from .schwab_token import TokenState, TokenStatus
    from .store import Store

    cal = CalendarService()
    notes: list[str] = []
    days = session_days(cal, req)
    rules = load_rules(rules_path)
    if req.rule_overrides:
        rules = replace(rules, **req.rule_overrides)
    picks = parse_picks(req, notes)
    if req.prices == "scenario":
        market = ScenarioMarket(picks, cal, req, days)
    elif req.prices == "stock":
        bars = fetch_stock_bars([p.underlying for p in picks], days, cal,
                                Path(cache_dir or tempfile.gettempdir()), notes)
        market = StockMarket(picks, cal, req, days, bars, notes)
    else:
        if not db_path:
            raise SimError("Recorded prices need this bot's database")
        market = RecordedMarket(picks, req, db_path, days, cal, notes)

    start = mt(req.buy_day, req.start_at)
    stop = mt(days[-1], req.end_at)
    if stop <= start:
        raise SimError("The end time is before the start time")
    fake = FakeClock(start)
    fake.stop_at = stop
    events = Events(fake)
    for n in notes:
        events.add("note", n, at=start)
    frames: list[dict] = []
    series: dict[str, list] = {p.osi: [] for p in picks}

    capture = _CaptureLogs(events)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(capture)
    root.setLevel(logging.INFO)
    tmp = tempfile.TemporaryDirectory(prefix="coopa-sim-")
    old_lock = os.environ.get("COOPA_TRADA_LOCK_DIR")
    os.environ["COOPA_TRADA_LOCK_DIR"] = str(Path(tmp.name) / "locks")
    store = Store.open(Path(tmp.name) / "sim.db")
    outcome = "finished"
    try:
        with no_network(), fake_now(fake):
            schwab = SimSchwab(fake, market, events, req.first_sell_unfilled)
            broker = SchwabBroker(schwab, "simulated-account", store, None, sleep=fake.sleep)
            login = start - timedelta(days=1)

            def token(now):
                return TokenStatus(TokenState.CONNECTED, "(simulated)", "Logged in to Schwab.",
                                   login, login + timedelta(days=30), now)

            engine = Engine(source=ListArrives(picks, req, fake, events), resolver=SimResolver(picks, market, fake),
                            broker=broker, store=store, rules=rules, cal=cal, mode=broker.mode,
                            quote_source=lambda osi: market.quote(osi, fake()), now=fake, token_status=token,
                            sleep=fake.sleep, alerter=Alerter([_SimAlerts(events)], now=fake))
            record_exit = store.record_exit_event

            def narrate_exit(**kw):
                osi = kw["position_key"].split(":", 1)[1]
                why = _REASON.get(kw["strategy"], kw["strategy"])
                events.add("decision", f"Decision: sell {contract_text(osi)} ({why}) at bid "
                                       f"${kw['trigger_price']:.2f}", symbol=osi)
                return record_exit(**kw)

            store.record_exit_event = narrate_exit
            record_skip = store.record_skip

            def narrate_skip(**kw):
                events.add("skip", f"Skipped {kw['signal_id']}: {kw['reason']}", level="WARNING")
                return record_skip(**kw)

            store.record_skip = narrate_skip

            def tick():
                now = fake()
                for p in picks:
                    try:
                        q = market.quote(p.osi, now)
                        series[p.osi].append([_iso(now), q.bid, q.ask, round(q.mid, 4)])
                    except Exception:  # noqa: BLE001
                        pass
                frame = {"t": _iso(now), "status": engine.status_note, "positions": []}
                for state, signal_id, plan in engine._managed_positions(cal_day[0]):
                    try:
                        q = market.quote(state.osi_symbol, now)
                    except Exception:  # noqa: BLE001
                        q = None
                    frame["positions"].append({
                        "osi": state.osi_symbol, "qty": state.quantity, "ref": round(state.entry_price, 4),
                        "peak": round(state.session_peak_mid, 4), "mid": q and round(q.mid, 4),
                        "bid": q and q.bid, "plan": plan.kind, "watch_from": _iso(plan.start),
                        "sell_by": _iso(plan.hard_exit_at),
                        "rules": [type(s).__name__ for s in plan.composite.strategies],
                        "triggers": _triggers(rules, state.entry_price, state.session_peak_mid)})
                frames.append(frame)
                if progress and len(frames) % 200 == 0:
                    progress(_iso(now))

            fake.on_tick = tick
            cal_day = [days[0]]
            for i, day in enumerate(days):
                cal_day[0] = day
                if i:
                    fake.now = max(fake.now, mt(day, rules.auto_start_time))
                if fake.now >= stop:
                    break
                events.add("run", f"{day:%A %b %-d}: the run starts")
                try:
                    engine.run(day)
                    events.add("run", f"{day:%A %b %-d}: the run ends ({engine.status_note})")
                except SimEnd:
                    events.add("run", "The simulation reached its end time")
                    tick()
                    outcome = "reached the end time"
                    break
                except Exception as exc:  # noqa: BLE001 - a run that stops is what we are debugging
                    events.add("error", f"The run stopped with an error: {exc}", level="ERROR")
                    outcome = f"stopped with an error: {exc}"
                    break
            trades = [_trade_row(r) for r in store.position_report()]
    finally:
        root.removeHandler(capture)
        root.setLevel(old_level)
        store.close()
        if old_lock is None:
            os.environ.pop("COOPA_TRADA_LOCK_DIR", None)
        else:
            os.environ["COOPA_TRADA_LOCK_DIR"] = old_lock
        tmp.cleanup()

    # A decision is recorded after the order it led to; show it first.
    prio = {"note": 0, "run": 1, "picks": 2, "decision": 3, "order": 4, "fill": 5}
    events.items = [e for _, _, _, e in sorted(
        (e["t"], prio.get(e["kind"], 4), i, e) for i, e in enumerate(events.items))]
    realized = sum(t["pnl"] or 0 for t in trades if not t["open"])
    return {
        "request": _request_json(req),
        "outcome": outcome,
        "start": _iso(start), "end": _iso(stop),
        "days": [d.isoformat() for d in days],
        "sessions": [{"day": d.isoformat(), "open": _iso(cal.session_open(d)), "close": _iso(cal.session_close(d)),
                      "buy_from": _iso(cal.buy_window(d, rules)[0]), "buy_until": _iso(cal.buy_window(d, rules)[1])}
                     for d in days],
        "rules": _rules_json(rules),
        "picks": [{"label": p.label, "osi": p.osi, "name": contract_text(p.osi), "shape": p.shape,
                   "list_price": p.base_price, "delta": p.delta, "expiration": p.expiration.isoformat()}
                  for p in picks],
        "series": series,
        "frames": frames,
        "events": events.items,
        "trades": trades,
        "summary": {"bought": len(trades), "sold": sum(1 for t in trades if not t["open"]),
                    "still_open": sum(1 for t in trades if t["open"]), "realized_pnl": round(realized, 2)},
    }


def _trade_row(r: dict) -> dict:
    return {"osi": r.get("osi_symbol"), "name": contract_text(r.get("osi_symbol") or ""),
            "qty": r.get("quantity"), "entry_at": r.get("entry_at_utc"), "entry_price": r.get("entry_price"),
            "exit_at": r.get("exit_at_utc"), "exit_price": r.get("exit_price"),
            "pnl": r.get("realized_pnl"), "why": _REASON.get(r.get("exit_strategy"), r.get("exit_strategy")),
            "open": bool(r.get("is_open"))}


def _request_json(req: SimRequest) -> dict:
    out = {}
    for k, v in req.__dict__.items():
        out[k] = v.isoformat()[:5] if isinstance(v, time) else v.isoformat() if isinstance(v, date) else v
    return out


def _rules_json(rules) -> dict:
    keep = ("take_profit_pct", "hard_stop_loss_pct", "trailing_stop_pct", "profit_trail_points", "profit_trail_arm",
            "cooper_drop_from_peak_pct", "cooper_drop_from_entry_pct", "stops_on_entry_day", "next_day_time_exit",
            "total_budget", "max_dollars_per_position", "contracts_per_pick", "exit_reference_price")
    out = {k: getattr(rules, k) for k in keep}
    out["next_day_sell_by"] = rules.next_day_sell_by.strftime("%H:%M")
    return out


# -- command line (the web page runs this in its own process) -----------------------
def main(request_path: str, out_dir: str, *, rules_path: str, db_path: str | None) -> int:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    status = out / "status.json"

    def write_status(**kw):
        tmp = status.with_suffix(".part")
        tmp.write_text(json.dumps(kw))
        os.replace(tmp, status)

    write_status(state="running", at=None)
    try:
        req = SimRequest.from_dict(json.loads(Path(request_path).read_text()))
        result = simulate(req, rules_path=rules_path, db_path=db_path,
                          cache_dir=out.parent / "price-cache",
                          progress=lambda at: write_status(state="running", at=at))
    except SimError as exc:
        write_status(state="error", error=str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).exception("simulation failed")
        write_status(state="error", error=f"The simulator failed: {exc}")
        return 1
    (out / "result.json").write_text(json.dumps(result, default=str))
    lines = [f"{clock.to_display(datetime.fromisoformat(e['t'])):%a %b %d %I:%M:%S %p} MT  "
             f"{e['level']:<7} {e['kind']:<8} {e['text']}" for e in result["events"]]
    (out / "log.txt").write_text("\n".join(lines) + "\n")
    write_status(state="done", outcome=result["outcome"], summary=result["summary"])
    return 0
