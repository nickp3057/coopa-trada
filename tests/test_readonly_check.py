"""Task 0: the read-only Schwab check never reaches an order endpoint and never prints secrets."""
import httpx
import pytest

from coopa_trada.readonly_check import ALLOWED_METHODS, NotReadOnly, ReadOnlyClient, run_check

ACCOUNT = "12345678"
HASH = "ABCDEF0123456789ABCDEF0123456789"


def response(payload, status=200):
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "https://example.test"))


class FakeSchwab:
    def __init__(self):
        self.called = []

    def __getattr__(self, name):  # any order method reaching the real client fails the test
        raise AssertionError(f"non-read-only call reached the client: {name}")

    def get_account_numbers(self):
        self.called.append("get_account_numbers")
        return response([{"accountNumber": ACCOUNT, "hashValue": HASH}])

    def get_account(self, account_hash, fields=None):
        self.called.append("get_account")
        assert account_hash == HASH
        return response({"securitiesAccount": {
            "accountNumber": ACCOUNT,
            "currentBalances": {"cashBalance": 98765.43, "liquidationValue": 123456.78},
            "positions": [{"instrument": {"assetType": "OPTION", "symbol": "SPY   261030C00600000"},
                           "longQuantity": 1, "marketValue": 4321.0}]}})

    def get_option_chain(self, symbol, **kwargs):
        self.called.append("get_option_chain")
        return response({"callExpDateMap": {"2026-10-30:8": {
            "600.0": [{"bid": 4.1, "ask": 4.2, "delta": 0.52}],
            "605.0": [{"bid": 0.0, "ask": 0.05, "delta": -999.0}]}}})


def test_check_reports_each_step_without_account_numbers_or_amounts():
    fake = FakeSchwab()
    report = run_check(fake, account_id=ACCOUNT)
    text = "\n".join(report.lines())
    assert report.ok
    assert fake.called == ["get_account_numbers", "get_account", "get_option_chain"]
    for secret in (ACCOUNT, HASH, "98765", "123456", "4321", "$"):
        assert secret not in text
    assert "1 account(s)" in text and "1 position(s), 1 of them options" in text
    assert "2 contract(s); 1 with a two-sided bid/ask; 1 with a usable delta" in text


@pytest.mark.parametrize("method", ["place_order", "replace_order", "cancel_order", "get_orders_for_account"])
def test_order_and_other_methods_are_refused_before_reaching_schwab(method):
    ro = ReadOnlyClient(FakeSchwab())
    with pytest.raises(NotReadOnly):
        getattr(ro, method)
    assert method not in ALLOWED_METHODS


def test_failures_are_reported_without_detail_that_could_leak():
    class Broken(FakeSchwab):
        def get_account_numbers(self):
            return response({"error": f"account {ACCOUNT}"}, 401)
    report = run_check(Broken())
    text = "\n".join(report.lines())
    assert not report.ok and "HTTP 401" in text and ACCOUNT not in text


def test_cli_without_a_login_says_so(tmp_path, monkeypatch, capsys):
    from coopa_trada.__main__ import main
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(tmp_path / "none.json"))
    assert main(["readonly-check"]) == 2
    assert "Schwab login" in capsys.readouterr().out
