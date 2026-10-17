"""coopa_trada.wiring: the shared engine builder and the token inspector."""

import json
from datetime import datetime, timezone

import pytest

from coopa_trada.broker import DryRunBroker
from coopa_trada.config import Mode, SchwabCredentials
from coopa_trada.contracts import SyntheticResolver
from coopa_trada.engine import Engine, SyntheticQuoteSource
from coopa_trada.wiring import build_engine, parse_trading_date, token_status

RULES_YAML = "\n".join(
    [
        "entry_minutes_before_close: 20",
        "exit_window:",
        '  SINGLE_NAME: ["15:35", 2]',
        "  ETF: [18, 1]",
        "trailing_stop_pct: 0.25",
        "hard_stop_loss_pct: 0.40",
        "take_profit_pct: 0.60",
        "max_concurrent_positions: 5",
        "max_dollars_per_position: 1500",
        "poll_interval_seconds: 30",
    ]
)


@pytest.fixture
def config_path(tmp_path):
    p = tmp_path / "rules.yaml"
    p.write_text(RULES_YAML, encoding="utf-8")
    return str(p)


def test_build_engine_dry_run_wiring(tmp_path, config_path):
    engine, store = build_engine(
        mode=Mode.DRY_RUN,
        signals_path="data/signals.example.csv",
        config_path=config_path,
        db_path=str(tmp_path / "t.db"),
    )
    try:
        assert isinstance(engine, Engine)
        assert engine.mode is Mode.DRY_RUN
        assert isinstance(engine.broker, DryRunBroker)
        assert isinstance(engine.resolver, SyntheticResolver)
        assert isinstance(engine.quote_source, SyntheticQuoteSource)
        assert engine.store is store
    finally:
        store.close()


def test_build_engine_passes_now_and_sleep_hooks(tmp_path, config_path):
    fixed = datetime(2026, 9, 2, 18, 0, tzinfo=timezone.utc)
    engine, store = build_engine(
        mode=Mode.DRY_RUN,
        signals_path="data/signals.example.csv",
        config_path=config_path,
        db_path=str(tmp_path / "t.db"),
        now=lambda: fixed,
        sleep=lambda _s: None,
    )
    try:
        assert engine._now() == fixed
    finally:
        store.close()


def test_build_engine_requires_store_or_db_path(config_path):
    with pytest.raises(ValueError):
        build_engine(
            mode=Mode.DRY_RUN,
            signals_path="data/signals.example.csv",
            config_path=config_path,
        )


def test_parse_trading_date_iso_and_default():
    assert parse_trading_date("2026-09-02").isoformat() == "2026-09-02"
    assert parse_trading_date(None) is not None


def _creds(path):
    return SchwabCredentials(
        api_key="k", app_secret="s", callback_url="https://x", token_path=path
    )


def test_token_status_missing_file(tmp_path):
    st = token_status(_creds(tmp_path / "nope.json"))
    assert st["present"] is False
    assert st["stale"] is True
    assert "no token file" in st["error"]


def test_token_status_garbage_json(tmp_path):
    p = tmp_path / "schwab_token.json"
    p.write_text("not json{", encoding="utf-8")
    st = token_status(_creds(p))
    assert st["present"] is True
    assert "unreadable" in st["error"]


def test_token_status_fresh_and_stale(tmp_path):
    fresh = tmp_path / "fresh.json"
    fresh.write_text(json.dumps({"creation_timestamp": datetime.now(timezone.utc).timestamp(),
                                 "token": {"refresh_token": "fake"}}))
    st = token_status(_creds(fresh))
    assert st["present"] and st["error"] is None
    assert st["stale"] is False
    assert st["refresh_expires_in_days"] > 5

    old = tmp_path / "old.json"
    old_ts = datetime.now(timezone.utc).timestamp() - 8 * 86400
    old.write_text(json.dumps({"creation_timestamp": old_ts, "token": {"refresh_token": "fake"}}))
    st = token_status(_creds(old))
    assert st["stale"] is True
    assert st["refresh_expires_in_days"] < 0
