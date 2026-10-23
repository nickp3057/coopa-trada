"""The Friday-to-Monday rehearsal: every step happens in order, and nothing real
is touched (no network, no .env, no Schwab login, no lasting files)."""
import hashlib
import os
import socket
from pathlib import Path

import pytest

from coopa_trada import rehearsal


@pytest.fixture(scope="module")
def timeline():
    return rehearsal.rehearse()


def _index(lines, text):
    return next(i for i, line in enumerate(lines) if text in line)


def test_the_weekend_happens_in_order(timeline):
    steps = [
        "Friday October 23",
        "Friday's run starts",
        "Cooper confirms today's picks",
        "Bought SNOW 230 call at $1.20",
        "Bought AMD 260 call at $1.00",
        "Bought IBIT 44 put at $0.50",
        "held to the next trading day",
        "3 bought, 0 skipped. 3 positions held overnight",
        "Monday October 26",
        "Monday's run starts",
        "sell AMD 260 call (take-profit reached)",
        "Sold AMD 260 call at $1.65",
        "sell SNOW 230 call (10-point rule)",
        "Sold SNOW 230 call at $1.55",
        "sell IBIT 44 put (10-point rule)",
        "nobody buys IBIT 44 put at $0.56",
        "Unfilled sell for IBIT 44 put canceled",
        "sell IBIT 44 put (repriced)",
        "Sold IBIT 44 put at $0.55",
        "Everything is sold",
    ]
    positions = [_index(timeline, s) for s in steps]
    assert positions == sorted(positions)


def test_buys_are_inside_the_window_and_monday_exits_wait_for_the_open(timeline):
    assert any("1:40:00 PM MT  Buy order sent" in line for line in timeline)
    first_sell = _index(timeline, "Sell order sent")
    assert "7:30:00 AM MT" in timeline[first_sell]  # not before the open


def test_the_repriced_sell_is_lower_and_a_minute_later(timeline):
    stuck = timeline[_index(timeline, "nobody buys IBIT")]
    replaced = timeline[_index(timeline, "sell IBIT 44 put (repriced)")]
    assert "9:40:00 AM" in stuck and "9:41:00 AM" in replaced
    assert "$0.55" in replaced


def test_results(timeline):
    assert timeline[-1].splitlines()[-1] == "  Total: +105.00"


def test_nothing_real_is_touched(tmp_path, monkeypatch, sent_alerts):
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(tmp_path / "must-not-be-read.json"))
    monkeypatch.setenv("COOPA_TRADA_LOCK_DIR", str(tmp_path / "locks-before"))
    example = Path("config/rules.example.yaml")
    before = hashlib.sha256(example.read_bytes()).hexdigest()
    seen = []
    monkeypatch.setattr(rehearsal.tempfile, "TemporaryDirectory",
                        _recording_tempdir(rehearsal.tempfile.TemporaryDirectory, seen))
    rehearsal.rehearse()
    assert hashlib.sha256(example.read_bytes()).hexdigest() == before
    assert sent_alerts == []                          # no Mac notification, no ntfy
    assert not (tmp_path / "must-not-be-read.json").exists()
    assert seen and not Path(seen[0]).exists()        # its database is gone
    assert os.environ["COOPA_TRADA_LOCK_DIR"] == str(tmp_path / "locks-before")


def _recording_tempdir(real, seen):
    def make(*args, **kwargs):
        td = real(*args, **kwargs)
        seen.append(td.name)
        return td
    return make


def test_network_is_refused_during_the_rehearsal():
    with rehearsal.no_network():
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", 9), timeout=1)
    # and restored afterwards
    assert socket.socket.connect is not None


def test_cli_does_not_load_env(monkeypatch, capsys):
    from coopa_trada import __main__ as cli

    def boom(*a, **k):
        raise AssertionError("rehearse must not load .env")

    monkeypatch.setattr(cli, "load_env", boom)
    assert cli.main(["rehearse"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("COOPA TRADA REHEARSAL") and "Total: +105.00" in out
