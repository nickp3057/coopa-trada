"""End-to-end smoke test of the engine with fakes: --once, dry-run, no network."""

from datetime import date, datetime, timezone

import pytest

from coopa_trada.calendar_svc import CalendarService
from coopa_trada.broker import DryRunBroker
from coopa_trada.config import Mode
from coopa_trada.contracts import SyntheticResolver
from coopa_trada.engine import Engine, SyntheticQuoteSource
from coopa_trada.models import AssetClass, Right, Signal
from coopa_trada.rules import load_rules
from coopa_trada.store import Store

UTC = timezone.utc
DAY = date(2026, 9, 2)

RULES_YAML = "\n".join(
    [
        "entry_minutes_before_close: 20",
        "exit_window:",
        '  SINGLE_NAME: ["15:35", 2]',
        "  ETF: [18, 1]",
        "trailing_stop_pct: 0.25",
        "hard_stop_loss_pct: 0.40",
        "take_profit_pct: 0.60",
        "max_concurrent_positions: 5",
        "max_dollars_per_position: 1500",
        "poll_interval_seconds: 30",
    ]
)


class ListSource:
    def __init__(self, signals):
        self._signals = signals

    def fetch(self, day=None):
        return list(self._signals)


def _signal(sid, underlying, strike, qty, ac=AssetClass.SINGLE_NAME):
    return Signal(
        id=sid, underlying=underlying, right=Right.CALL, strike=strike,
        expiration=date(2026, 9, 18), quantity=qty, asset_class=ac,
        received_at=datetime(2026, 9, 2, 17, 30, tzinfo=UTC), raw="x",
    )


@pytest.fixture
def rules(tmp_path):
    p = tmp_path / "rules.yaml"
    p.write_text(RULES_YAML, encoding="utf-8")
    return load_rules(p)


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "t.db")
    yield s
    s.close()


def _engine(store, rules, signals, *, now):
    qs = SyntheticQuoteSource()
    return Engine(
        source=ListSource(signals),
        resolver=SyntheticResolver(),
        broker=DryRunBroker(store, qs),
        store=store,
        rules=rules,
        cal=CalendarService(),
        mode=Mode.DRY_RUN,
        quote_source=qs,
        now=lambda: now,
        sleep=lambda _s: None,
    )


def test_once_places_simulated_entry_at_ask_then_one_exit_tick(store, rules):
    # 14:00 ET -> well before the exit window, so HardTimeExit will not fire
    now = datetime(2026, 9, 2, 18, 0, tzinfo=UTC)
    sig = _signal("sig-1", "AAPL", 250.0, 3)
    eng = _engine(store, rules, [sig], now=now)

    eng.run(DAY, once=True)

    orders = store.conn.execute("SELECT * FROM orders ORDER BY id").fetchall()
    assert [o["intent"] for o in orders] == ["ENTRY"]
    entry = orders[0]
    assert entry["side"] == "BUY_TO_OPEN"
    assert entry["mode"] == "DRY-RUN"

    (fill,) = store.fills_for_order(entry["id"])
    resolved = SyntheticResolver().resolve(sig)
    assert fill["price"] == resolved.ask  # filled at the ask

    # position stayed open (gentle synthetic decline, no exit trigger this tick)
    assert [r["signal_id"] for r in store.open_positions()] == ["sig-1"]


def test_reconcile_is_noop_in_dry_run(store, rules):
    now = datetime(2026, 9, 2, 18, 0, tzinfo=UTC)
    eng = _engine(store, rules, [], now=now)
    report = eng.reconcile()
    assert report.simulated is True
    assert report.mode == "DRY-RUN"


def test_rerun_once_is_idempotent_no_duplicate_orders(store, rules):
    now = datetime(2026, 9, 2, 18, 0, tzinfo=UTC)
    sig = _signal("sig-1", "AAPL", 250.0, 3)

    _engine(store, rules, [sig], now=now).run(DAY, once=True)
    _engine(store, rules, [sig], now=now).run(DAY, once=True)

    entries = store.conn.execute("SELECT * FROM orders WHERE intent='ENTRY'").fetchall()
    assert len(entries) == 1


def test_dollar_cap_sizes_down_quantity(store, rules):
    # QQQ x999 requested; max_dollars_per_position=1500 forces a small size
    sig = _signal("big", "QQQ", 480.0, 999, ac=AssetClass.ETF)
    now = datetime(2026, 9, 2, 18, 0, tzinfo=UTC)
    _engine(store, rules, [sig], now=now).run(DAY, once=True)

    entry = store.conn.execute("SELECT * FROM orders WHERE intent='ENTRY'").fetchone()
    resolved = SyntheticResolver().resolve(sig)
    import math

    cap = math.floor(1500 / (resolved.mid * 100))
    assert entry is not None
    assert entry["quantity"] == cap
    assert entry["quantity"] < 999


def test_underspecified_signal_is_skipped(store, rules):
    good = _signal("good", "AAPL", 250.0, 2)
    bad = Signal(
        id="bad", underlying="MSFT", right=Right.CALL, strike=None,
        expiration=date(2026, 9, 18), quantity=2, asset_class=AssetClass.SINGLE_NAME,
        received_at=datetime(2026, 9, 2, 17, 30, tzinfo=UTC), raw="x",
    )
    now = datetime(2026, 9, 2, 18, 0, tzinfo=UTC)
    _engine(store, rules, [bad, good], now=now).run(DAY, once=True)

    syms = {o["osi_symbol"][:4].strip() for o in store.conn.execute("SELECT osi_symbol FROM orders")}
    assert "AAPL" in syms
    assert "MSFT" not in syms


def test_cooper_10pct_keys_wire_extra_exit_strategies_first(store, tmp_path):
    from coopa_trada.exits import StopLoss, TrailingPeak

    p = tmp_path / "rules.yaml"
    p.write_text(
        RULES_YAML + "\ncooper_drop_from_entry_pct: 0.10\ncooper_drop_from_peak_pct: 0.10\n",
        encoding="utf-8",
    )
    rules = load_rules(p)
    now = datetime(2026, 9, 2, 18, 0, tzinfo=UTC)
    eng = _engine(store, rules, [], now=now)

    first, second = eng.composite.strategies[0], eng.composite.strategies[1]
    assert isinstance(first, StopLoss) and first.pct == 0.10
    assert isinstance(second, TrailingPeak) and second.pct == 0.10
    # originals still present after the two prepended strategies
    assert len(eng.composite.strategies) == 6


def test_cooper_10pct_keys_are_optional(rules):
    # the shared RULES_YAML has neither key -> Rules loads, no extra strategies
    assert rules.cooper_drop_from_entry_pct is None
    assert rules.cooper_drop_from_peak_pct is None


def test_exit_loop_triggers_hard_time_exit_when_clock_is_past_window(store, rules):
    sig = _signal("sig-1", "AAPL", 250.0, 3)
    # 20:30 UTC = 16:30 ET, past the SINGLE_NAME exit window end
    now = datetime(2026, 9, 2, 20, 30, tzinfo=UTC)
    eng = _engine(store, rules, [sig], now=now)
    eng.run(DAY, once=False)

    intents = [o["intent"] for o in store.conn.execute("SELECT intent FROM orders ORDER BY id")]
    assert intents == ["ENTRY", "EXIT"]
    ev = store.conn.execute("SELECT * FROM exit_events").fetchone()
    assert ev["strategy"] == "HardTimeExit"
    assert not store.open_positions()  # closed out
