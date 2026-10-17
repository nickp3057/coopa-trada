"""FixedQuantitySizer: always 1 contract per pick; the dollar figure is a gate.

Cooper's rule: buy exactly 1 contract per pick, always. If 1 contract would cost
more than the cap, skip the ticker entirely; otherwise buy 1 -- never more,
however cheap the contract.
"""

from types import SimpleNamespace

from coopa_trada.rules import FixedQuantitySizer


def _contract(ask):
    # FixedQuantitySizer.size reads contract.ask, the entry limit price
    return SimpleNamespace(mid=ask, ask=ask)


def test_skips_entirely_when_one_contract_exceeds_cap():
    sizer = FixedQuantitySizer(300)
    # 1 contract = 3.01 * 100 = 301 > 300 -> skip
    assert sizer.size(None, _contract(3.01), None) == 0


def test_within_cap_returns_exactly_one():
    sizer = FixedQuantitySizer(300)
    # 1 contract = 100, within the 300 cap -> exactly 1 (not 3)
    assert sizer.size(None, _contract(1.00), None) == 1


def test_exact_boundary_fits_and_returns_one():
    sizer = FixedQuantitySizer(300)
    # 1 contract = 300, which is not > 300 -> fits -> 1
    assert sizer.size(None, _contract(3.00), None) == 1


def test_cap_is_checked_against_ask_not_mid():
    sizer = FixedQuantitySizer(300)
    # mid 2.99 fits, but the order is placed at the 3.02 ask = 302 > 300 -> skip
    assert sizer.size(None, SimpleNamespace(mid=2.99, ask=3.02), None) == 0
    # a mid above the cap does not matter when the ask fits
    assert sizer.size(None, SimpleNamespace(mid=3.10, ask=3.00), None) == 1


def test_non_finite_ask_returns_zero():
    assert FixedQuantitySizer(300).size(None, _contract(float("nan")), None) == 0


def test_non_positive_ask_returns_zero():
    assert FixedQuantitySizer(300).size(None, _contract(0.0), None) == 0
    assert FixedQuantitySizer(300).size(None, _contract(-1.0), None) == 0


def test_very_cheap_contract_still_returns_one():
    # the branch that actually changed: no flooring up to fill the cap
    sizer = FixedQuantitySizer(1500)
    assert sizer.size(None, _contract(0.10), None) == 1
