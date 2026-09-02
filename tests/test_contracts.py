from datetime import date, datetime, timezone

import pytest

from coopa_trada.contracts import (
    ContractNotTradable,
    ContractResolver,
    ContractUnderspecified,
    SyntheticResolver,
    osi_symbol,
)
from coopa_trada.models import AssetClass, Right, Signal

UTC = timezone.utc


def _signal(strike=250.0, expiration=date(2026, 9, 18), right=Right.CALL, sid="sig-1"):
    return Signal(
        id=sid, underlying="AAPL", right=right, strike=strike, expiration=expiration,
        quantity=3, asset_class=AssetClass.SINGLE_NAME,
        received_at=datetime(2026, 9, 2, 17, 30, tzinfo=UTC), raw="x",
    )


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeClient:
    """Returns a canned option-chain payload and records the call kwargs."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_option_chain(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResp(self.payload)


def _chain(bid=1.20, ask=1.30, oi=4321):
    return {
        "callExpDateMap": {
            "2026-09-18:11": {
                "250.0": [
                    {
                        "symbol": "AAPL  260918C00250000",
                        "strikePrice": 250.0,
                        "bid": bid,
                        "ask": ask,
                        "openInterest": oi,
                        "tradable": True,
                    }
                ],
                "255.0": [
                    {"symbol": "AAPL  260918C00255000", "strikePrice": 255.0, "bid": 0.9, "ask": 1.0}
                ],
            }
        },
        "putExpDateMap": {},
    }


def test_osi_symbol_format():
    assert osi_symbol("AAPL", date(2026, 9, 18), Right.CALL, 250) == "AAPL  260918C00250000"
    assert osi_symbol("SPY", date(2026, 9, 18), Right.PUT, 555) == "SPY   260918P00555000"


def test_underspecified_when_strike_missing():
    r = ContractResolver(FakeClient(_chain()))
    with pytest.raises(ContractUnderspecified):
        r.resolve(_signal(strike=None))


def test_underspecified_when_expiration_missing():
    r = ContractResolver(FakeClient(_chain()))
    with pytest.raises(ContractUnderspecified):
        r.resolve(_signal(expiration=None))


def test_resolves_exact_contract_with_two_sided_market():
    client = FakeClient(_chain(bid=1.20, ask=1.30, oi=4321))
    rc = ContractResolver(client).resolve(_signal())
    assert rc.osi_symbol == "AAPL  260918C00250000"
    assert rc.bid == 1.20 and rc.ask == 1.30
    assert rc.mid == pytest.approx(1.25)
    assert rc.open_interest == 4321
    assert rc.tradable is True
    assert rc.resolved_at.tzinfo is not None
    # filtered the chain call by underlying / type / strike / expiration
    assert client.calls[0]["symbol"] == "AAPL"
    assert client.calls[0]["strike"] == 250.0


def test_one_sided_market_is_not_tradable():
    payload = _chain()
    payload["callExpDateMap"]["2026-09-18:11"]["250.0"][0]["bid"] = 0.0
    with pytest.raises(ContractNotTradable):
        ContractResolver(FakeClient(payload)).resolve(_signal())


def test_missing_contract_is_not_tradable():
    with pytest.raises(ContractNotTradable):
        ContractResolver(FakeClient(_chain())).resolve(_signal(strike=999.0))


def test_synthetic_resolver_is_offline_deterministic_and_two_sided():
    r = SyntheticResolver()
    a = r.resolve(_signal())
    b = r.resolve(_signal())
    assert (a.bid, a.ask) == (b.bid, b.ask)
    assert a.bid > 0 and a.ask > a.bid
    assert a.osi_symbol == "AAPL  260918C00250000"
    with pytest.raises(ContractUnderspecified):
        r.resolve(_signal(strike=None))
