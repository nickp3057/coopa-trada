from datetime import date

import pytest

from coopa_trada.idempotency import Intent, canonical_string, make_key


def test_canonical_string_shape():
    assert canonical_string("sig-1", Intent.ENTRY, date(2026, 9, 2)) == "sig-1|ENTRY|2026-09-02"


def test_key_is_stable_and_hex_sha256():
    k1 = make_key("sig-1", Intent.ENTRY, date(2026, 9, 2))
    k2 = make_key("sig-1", Intent.ENTRY, date(2026, 9, 2))
    assert k1 == k2
    assert len(k1) == 64 and all(c in "0123456789abcdef" for c in k1)


def test_key_varies_with_each_component():
    base = make_key("sig-1", Intent.ENTRY, date(2026, 9, 2))
    assert make_key("sig-2", Intent.ENTRY, date(2026, 9, 2)) != base
    assert make_key("sig-1", Intent.EXIT, date(2026, 9, 2)) != base
    assert make_key("sig-1", Intent.ENTRY, date(2026, 9, 3)) != base


def test_date_string_accepted_and_equivalent():
    assert make_key("s", Intent.EXIT, "2026-09-02") == make_key("s", Intent.EXIT, date(2026, 9, 2))


def test_bad_date_string_rejected():
    with pytest.raises(ValueError):
        make_key("s", Intent.ENTRY, "09/02/2026")
