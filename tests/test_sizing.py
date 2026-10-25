"""Task 3: $300 total budget, no position-count limit, highest delta first."""
from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from coopa_trada.broker import DryRunBroker
from coopa_trada.calendar_svc import CalendarService
from coopa_trada.contracts import ContractResolver, ResolvedContract, SyntheticResolver, osi_symbol
from coopa_trada.engine import Engine
from coopa_trada.exits import Quote
from coopa_trada.idempotency import Intent
from coopa_trada.models import AssetClass, Right, Signal
from coopa_trada.rules import load_rules
from coopa_trada.store import Store

NOW = datetime(2026, 9, 2, 19, 45, tzinfo=timezone.utc)
DAY = date(2026, 9, 2)
EXP = date(2026, 9, 4)


def pick(symbol, *, schwab=None, tvt=None):
    return Signal(f"{symbol}-pick", symbol, Right.CALL, 100, EXP, 1, AssetClass.SINGLE_NAME, NOW, "x",
                  reference_price=1.0, signal_delta_tvt=tvt, signal_delta_schwab=schwab)


class Resolver:
    """Fixed asks and live deltas per ticker."""

    def __init__(self, book):
        self.book = book  # symbol -> (ask, live delta)

    def resolve(self, signal):
        ask, delta = self.book[signal.underlying]
        return ResolvedContract(osi_symbol(signal.underlying, EXP, Right.CALL, 100), signal.underlying,
                                Right.CALL, 100, EXP, round(ask - .02, 2), ask, round(ask - .01, 4),
                                100, True, NOW, delta=delta)


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "t.db") as s:
        yield s


def engine(store, picks, book, **rules):
    r = replace(load_rules("config/rules.example.yaml"), **rules)
    return Engine(source=SimpleNamespace(fetch=lambda day: picks), resolver=Resolver(book),
                  broker=DryRunBroker(store), store=store, rules=r, cal=CalendarService(),
                  mode=DryRunBroker.mode, quote_source=lambda _: Quote(1, 1.02, 1.01),
                  now=lambda: NOW, sleep=lambda _: None)


def bought(store):
    return [r["signal_id"].split("-")[0] for r in
            store.conn.execute("SELECT signal_id FROM orders WHERE intent='ENTRY' ORDER BY id")]


def skips(store):
    return {r["signal_id"].split("-")[0]: (r["category"], r["reason"]) for r in
            store.conn.execute("SELECT signal_id, category, reason FROM skip_events")}


def test_highest_delta_first_while_each_fits_the_300_budget(store):
    # Live deltas: BBB |-0.70| > CCC 0.55 > AAA 0.30.
    # BBB $1.50 -> $150 bought ($150 left). CCC $2.00 -> $200 does not fit: skipped.
    # AAA $1.00 -> $100 fits ($50 left): bought. Spent $250 of $300.
    book = {"AAA": (1.00, 0.30), "BBB": (1.50, -0.70), "CCC": (2.00, 0.55)}
    engine(store, [pick("AAA"), pick("BBB"), pick("CCC")], book)._entry_pass(DAY)
    assert bought(store) == ["BBB", "AAA"]
    category, reason = skips(store)["CCC"]
    assert category == "budget"
    assert "costs $200.00 at the ask, $150.00 of $300.00 left (rank 2 by schwab_live delta)" in reason
    assert store.committed_cents("DRY-RUN") == 25_000


def test_exactly_filling_the_budget_is_allowed(store):
    book = {"AAA": (1.00, 0.9), "BBB": (2.00, 0.8)}  # $100 + $200 = $300
    engine(store, [pick("AAA"), pick("BBB")], book)._entry_pass(DAY)
    assert bought(store) == ["AAA", "BBB"]


def test_positions_held_from_earlier_days_count_against_the_budget(store):
    # Yesterday's position cost $2.50 x 100 = $250 and is still open: $50 left today.
    eng = engine(store, [pick("NEW")], {"OLD": (2.50, 0.5), "NEW": (0.60, 0.5)})
    old = pick("OLD")
    store.record_signal(old)
    DryRunBroker(store).place_entry(eng.resolver.resolve(old), 1, 2.50, signal_id=old.id, day=date(2026, 9, 1))
    eng._entry_pass(DAY)
    assert bought(store) == ["OLD"]  # $60 > $50 left
    assert "$60.00 at the ask, $50.00 of $300.00 left" in skips(store)["NEW"][1]


def test_selling_frees_the_budget(store):
    old = pick("OLD")
    eng = engine(store, [pick("NEW")], {"OLD": (2.50, 0.5), "NEW": (0.60, 0.5)})
    store.record_signal(old)
    broker = DryRunBroker(store)
    broker.place_entry(eng.resolver.resolve(old), 1, 2.50, signal_id=old.id, day=date(2026, 9, 1))
    state, sid = eng._positions_state(DAY)[0]
    from coopa_trada.exits import ExitDecision
    broker.place_exit(state, 3.00, ExitDecision("t", "t", 3.0, NOW), signal_id=sid, day=DAY)
    assert store.committed_cents("DRY-RUN") == 0
    eng._entry_pass(DAY)
    assert bought(store) == ["OLD", "NEW"]


def test_per_pick_cap_still_applies(store):
    # $3.01 x 100 = $301 > $300 per-pick cap, even with a bigger total budget.
    engine(store, [pick("BIG")], {"BIG": (3.01, 0.9)}, total_budget=1000)._entry_pass(DAY)
    assert bought(store) == [] and skips(store)["BIG"][0] == "spending_cap"


def test_no_fixed_position_count(store):
    # Ten picks at $0.20 ($20 each, $200 total): all ten are bought (the old limit was 5).
    names = [f"T{i}" for i in range(10)]
    book = {n: (0.20, 0.5) for n in names}
    engine(store, [pick(n) for n in names], book)._entry_pass(DAY)
    assert len(bought(store)) == 10


def test_tvt_ranking_uses_the_list_column(store):
    book = {"AAA": (1.00, 0.9), "BBB": (1.00, 0.1)}
    picks = [pick("AAA", tvt=40.0), pick("BBB", tvt=-75.0)]
    engine(store, picks, book, rank_delta_source="tvt", total_budget=100)._entry_pass(DAY)
    assert bought(store) == ["BBB"]  # |-75| beats 40 although its live delta is lower
    assert skips(store)["AAA"][0] == "budget"


def test_picks_without_a_delta_rank_last_in_list_order(store):
    book = {"AAA": (1.00, None), "BBB": (1.00, None), "CCC": (1.00, 0.2)}
    engine(store, [pick("AAA"), pick("BBB"), pick("CCC")], book, total_budget=200)._entry_pass(DAY)
    assert bought(store) == ["CCC", "AAA"]
    assert skips(store)["BBB"][0] == "budget"


def test_unknown_or_working_buy_orders_reserve_their_cost(store):
    sig = pick("AAA")
    store.record_signal(sig)
    store.record_order(signal_id=sig.id, intent=Intent.ENTRY, idempotency_key="k1", mode="LIVE",
                       osi_symbol="X", side="BUY_TO_OPEN", quantity=1, limit_price=1.23, status="UNKNOWN",
                       request={})
    assert store.committed_cents("LIVE") == 12_300
    assert store.committed_cents("DRY-RUN") == 0


def test_fill_above_the_limit_and_fractional_cents_round_up(store):
    sig = pick("AAA")
    store.record_signal(sig)
    oid = store.record_order(signal_id=sig.id, intent=Intent.ENTRY, idempotency_key="k1", mode="LIVE",
                             osi_symbol="X", side="BUY_TO_OPEN", quantity=1, limit_price=1.20,
                             status="FILLED", request={})
    store.record_fill(order_id=oid, quantity=1, price=1.2051, mode="LIVE")
    assert store.committed_cents("LIVE") == 12_100  # $1.2051 -> 121 cents x 100


def test_live_delta_is_read_from_the_chain_and_bad_values_dropped():
    from coopa_trada.contracts import _valid_delta
    assert _valid_delta(-0.554) == -0.554
    assert _valid_delta(-999.0) is None and _valid_delta(None) is None and _valid_delta("nan") is None
    assert SyntheticResolver().resolve(pick("AMD", schwab=-0.4)).delta == -0.4


def test_budget_and_ranking_settings_validate(tmp_path):
    base = open("config/rules.example.yaml").read()
    r = load_rules("config/rules.example.yaml")
    assert (r.total_budget, r.rank_delta_source) == (300, "schwab_live")
    assert not hasattr(r, "max_concurrent_positions")
    for bad, key in (("total_budget: 0", "total_budget"), ("rank_delta_source: random", "rank_delta_source")):
        p = tmp_path / "r.yaml"
        p.write_text("\n".join(l for l in base.splitlines() if not l.startswith(key + ":")) + "\n" + bad)
        with pytest.raises(ValueError, match=key):
            load_rules(p)
    p = tmp_path / "legacy.yaml"
    p.write_text(base + "\nmax_concurrent_positions: 5\n")  # older files still load
    assert load_rules(p).total_budget == 300


def test_more_contracts_per_pick_count_against_the_cap_and_budget(store):
    # 2 contracts: AAA $1.00 -> $200, BBB $0.60 -> $120. Budget $300: AAA fits, BBB does not.
    book = {"AAA": (1.00, 0.9), "BBB": (0.60, 0.8)}
    engine(store, [pick("AAA"), pick("BBB")], book, contracts_per_pick=2)._entry_pass(DAY)
    assert bought(store) == ["AAA"]
    assert store.conn.execute("SELECT quantity FROM orders").fetchone()[0] == 2
    assert "$120.00 at the ask, $100.00 of $300.00 left" in skips(store)["BBB"][1]
    # $1.60 x 2 = $320 > the $300 per-pick cap.
    with Store.open(":memory:") as other:
        engine(other, [pick("CCC")], {"CCC": (1.60, 0.9)}, contracts_per_pick=2,
               total_budget=1000)._entry_pass(DAY)
        assert skips(other)["CCC"][0] == "spending_cap"
