from datetime import date, datetime, timezone

import pytest

from coopa_trada.clock import NaiveDatetimeError
from coopa_trada.models import AssetClass, Right, Signal

# CSV parsing is covered in tests/test_signal_source.py (real "FOMC Picks" format).

UTC = timezone.utc


def test_signal_rejects_naive_received_at():
    with pytest.raises(NaiveDatetimeError):
        Signal(
            id="s", underlying="AAPL", right=Right.CALL, strike=100.0,
            expiration=date(2026, 9, 18), quantity=1, asset_class=AssetClass.SINGLE_NAME,
            received_at=datetime(2026, 9, 2, 13, 30), raw="x",
        )


def test_signal_new_fields_default_to_none():
    sig = Signal(
        id="s", underlying="AAPL", right=Right.CALL, strike=100.0,
        expiration=date(2026, 9, 18), quantity=1, asset_class=AssetClass.SINGLE_NAME,
        received_at=datetime(2026, 9, 2, 13, 30, tzinfo=UTC), raw="x",
    )
    assert sig.reference_price is None
    assert sig.signal_delta_tvt is None
    assert sig.signal_delta_schwab is None
