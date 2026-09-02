"""FixedDollarSizer: per-position dollar cap enforced as a hard skip.

Cooper's rule: if one contract (mid * 100) would exceed the cap, skip the ticker
entirely -- do not buy fewer contracts to squeeze under it.
"""

from types import SimpleNamespace

from coopa_trada.rules import FixedDollarSizer


def _contract(mid):
    # FixedDollarSizer.size only reads contract.mid
    return SimpleNamespace(mid=mid)


def test_skips_entirely_when_one_contract_exceeds_cap():
    sizer = FixedDollarSizer(300)
    # one contract = 3.01 * 100 = 301 > 300 -> skip, not floor(300/301)=0-ish
    assert sizer.size(None, _contract(3.01), None) == 0


def test_buys_as_many_as_fit_when_within_cap():
    sizer = FixedDollarSizer(300)
    # one contract = 100; 300 / 100 = 3
    assert sizer.size(None, _contract(1.00), None) == 3


def test_exact_boundary_fits_and_returns_one():
    sizer = FixedDollarSizer(300)
    # one contract = 300, which is not > 300 -> fits; floor(300 / 300) = 1
    assert sizer.size(None, _contract(3.00), None) == 1


def test_non_positive_mid_returns_zero():
    assert FixedDollarSizer(300).size(None, _contract(0.0), None) == 0
    assert FixedDollarSizer(300).size(None, _contract(-1.0), None) == 0


def test_larger_cap_buys_more():
    sizer = FixedDollarSizer(1500)
    # one contract = 250; 1500 / 250 = 6
    assert sizer.size(None, _contract(2.50), None) == 6
