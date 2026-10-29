"""Practice runs on real Schwab prices (``practice_real_prices``), and the exit
loop surviving quotes Schwab can't give."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coopa_trada.broker import DryRunBroker, SchwabQuoteSource
from coopa_trada.config import Mode
from coopa_trada.contracts import ContractResolver
from coopa_trada.engine import QUOTE_FAILURES_BEFORE_ALERT
from coopa_trada.exits import Quote
from coopa_trada.rules import load_rules
from coopa_trada import wiring
from coopa_trada.web import create_app
from coopa_trada.web.config import WebPaths
from coopa_trada.web.rules_io import read_rules_dict, settings_summary

from test_exit_repricing import DAY, exits, setup, store  # noqa: F401 - store is a fixture

EXAMPLE = Path("config/rules.example.yaml")


def _rules(tmp_path, real: bool) -> Path:
    p = tmp_path / "rules.yaml"
    p.write_text(EXAMPLE.read_text().replace("practice_real_prices: false",
                                             f"practice_real_prices: {str(real).lower()}"))
    return p


def test_the_setting_is_off_unless_turned_on(tmp_path):
    assert load_rules(EXAMPLE).practice_real_prices is False
    assert load_rules(_rules(tmp_path, True)).practice_real_prices is True
    bad = tmp_path / "bad.yaml"
    bad.write_text(EXAMPLE.read_text().replace("practice_real_prices: false", "practice_real_prices: yes please"))
    with pytest.raises(ValueError, match="practice_real_prices"):
        load_rules(bad)


def test_settings_page_and_dashboard_show_it(tmp_path):
    path = _rules(tmp_path, True)
    assert read_rules_dict(path)["practice_real_prices"] is True
    summary = {s["label"]: s["current"] for s in settings_summary(path)["settings_summary"]}
    assert summary["Practice prices"].startswith("real Schwab prices")
    summary = {s["label"]: s["current"] for s in settings_summary(EXAMPLE)["settings_summary"]}
    assert summary["Practice prices"].startswith("made-up prices")


def test_practice_on_real_prices_uses_schwab_prices_and_pretend_orders(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, "schwab_client", lambda: (object(), wiring.credentials_from_env()))
    engine, store = wiring.build_engine(mode=Mode.DRY_RUN, signals_path="data/signals.example.csv",
                                        config_path=_rules(tmp_path, True), db_path=tmp_path / "t.db")
    try:
        assert isinstance(engine.broker, DryRunBroker)      # orders never reach Schwab
        assert isinstance(engine.resolver, ContractResolver)
        assert isinstance(engine.quote_source, SchwabQuoteSource)
        assert engine.broker.quote_source is engine.quote_source
        assert engine._needs_schwab_token() and engine.token_status is not None
    finally:
        store.close()


def test_practice_on_real_prices_needs_the_schwab_login(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(tmp_path / "missing.json"))
    paths = WebPaths(config_path=str(_rules(tmp_path, True)), db_path=str(tmp_path / "trada.db"),
                     signals_path="data/signals.example.csv")
    client = create_app(paths).test_client()
    rv = client.post("/api/run/start", json={"date": "2026-09-02"})
    assert rv.status_code == 409 and "Schwab" in rv.get_json()["error"]
    assert client.get("/api/run/status").get_json()["status"] == "idle"


def test_a_quote_schwab_cant_give_skips_the_tick_and_alerts_if_it_keeps_failing(store):  # noqa: F811
    eng, schwab, notifier, clock, bid = setup(store)
    real_quote, failing = eng.quote_source, [True]

    def quote(osi):
        if failing[0]:
            raise ValueError("Schwab returned an invalid option quote")
        return real_quote(osi)

    eng.quote_source = quote
    for _ in range(QUOTE_FAILURES_BEFORE_ALERT):
        assert eng._exit_tick(DAY) == 0                     # no crash, nothing sold
    assert exits(store) == []
    assert [a.kind for a in notifier.sent] == ["quote_unavailable"]
    assert "keeps trying" in notifier.sent[0].message
    eng._exit_tick(DAY)
    assert len(notifier.sent) == 1                           # one alert, not one per tick
    failing[0] = False
    assert eng._exit_tick(DAY) == 1                          # +70%: the take-profit sells once prices return
    assert len(exits(store)) == 1
