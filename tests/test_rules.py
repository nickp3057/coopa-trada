"""FixedQuantitySizer: always 1 contract per pick; the dollar figure is a gate.

Cooper's rule: buy exactly 1 contract per pick, always. If 1 contract would cost
more than the cap, skip the ticker entirely; otherwise buy 1 -- never more,
however cheap the contract.
"""

from types import SimpleNamespace

from coopa_trada.rules import FixedQuantitySizer


def _contract(mid):
    # FixedQuantitySizer.size only reads contract.mid
    return SimpleNamespace(mid=mid, ask=mid)


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


def test_non_positive_mid_returns_zero():
    assert FixedQuantitySizer(300).size(None, _contract(0.0), None) == 0
    assert FixedQuantitySizer(300).size(None, _contract(-1.0), None) == 0


def test_very_cheap_contract_still_returns_one():
    # the branch that actually changed: no flooring up to fill the cap
    sizer = FixedQuantitySizer(1500)
    assert sizer.size(None, _contract(0.10), None) == 1
