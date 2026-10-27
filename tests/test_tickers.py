"""New tickers on a pick list are looked up at Schwab and remembered."""
import pytest
import yaml

from coopa_trada import tickers
from coopa_trada.imports import preview_csv
from coopa_trada.models import AssetClass

CSV = """SYMBOL,OPTION,LAST PRICE,TVT-DELTA,SCHWAB DELTA
SOXS,43C,2.13,94.5,0.509
ORCL,133P,5.5,74.1,-0.529
TSLL,9C,0.48,73.2,0.679
"""


def fake_lookup(answers, calls):
    def lookup(symbols):
        calls.append(list(symbols))
        return {s: answers.get(s) for s in symbols}
    return lookup


def test_new_tickers_are_learned_once_and_the_list_goes_through(monkeypatch):
    calls = []
    lookup = fake_lookup({"SOXS": AssetClass.ETF, "ORCL": AssetClass.SINGLE_NAME}, calls)
    preview = preview_csv(CSV, "2026-10-27", "2026-10-30", ticker_lookup=lookup)
    assert calls == [["ORCL", "SOXS"]]                       # TSLL is already known: not looked up
    assert [r["symbol"] for r in preview["rows"]] == ["SOXS", "ORCL", "TSLL"]
    assert preview["notes"] == ["Added new tickers (stocks: ORCL; ETFs: SOXS)."]
    saved = yaml.safe_load(tickers.learned_path().read_text())
    assert saved == {"single_name": ["ORCL"], "etf": ["SOXS"]}
    again = preview_csv(CSV, "2026-10-27", "2026-10-30", ticker_lookup=lookup)
    assert calls == [["ORCL", "SOXS"]] and again["notes"] == []  # remembered


def test_a_ticker_schwab_does_not_know_is_refused_by_name_and_nothing_is_saved():
    lookup = fake_lookup({"SOXS": AssetClass.ETF}, [])
    with pytest.raises(ValueError, match="doesn't know the ticker ORCL"):
        preview_csv(CSV, "2026-10-27", "2026-10-30", ticker_lookup=lookup)
    assert not tickers.learned_path().exists()


def test_without_schwab_a_new_ticker_waits_for_a_reconnect():
    def down(symbols):
        raise tickers.TickerLookupUnavailable("not connected")
    with pytest.raises(ValueError, match="Reconnect Schwab"):
        preview_csv(CSV, "2026-10-27", "2026-10-30", ticker_lookup=down)
    assert not tickers.learned_path().exists()


def test_a_bad_option_is_named():
    with pytest.raises(ValueError, match="ORCL 133X"):
        preview_csv(CSV.replace("133P", "133X"), "2026-10-27", "2026-10-30", ticker_lookup=fake_lookup({}, []))


def test_schwab_answers_are_read_as_stock_etf_or_unknown(monkeypatch):
    class Resp:
        status_code = 200

        def json(self):
            return {"SOXS": {"assetMainType": "EQUITY", "assetSubType": "ETF"},
                    "ORCL": {"assetMainType": "EQUITY", "assetSubType": "COE"},
                    "errors": {"invalidSymbols": ["ZZZZ"]}}

    class Client:
        def get_quotes(self, symbols):
            return Resp()

    monkeypatch.setattr("coopa_trada.wiring.schwab_client", lambda: (Client(), None))
    assert tickers.schwab_lookup(["SOXS", "ORCL", "ZZZZ"]) == {
        "SOXS": AssetClass.ETF, "ORCL": AssetClass.SINGLE_NAME, "ZZZZ": None}
