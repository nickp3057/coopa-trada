"""CsvSignalSource against Cooper's real "FOMC Picks" list format.

The fixture is the exact screenshot table (IBIT / TSLL / QQQ / AMD) plus four
more tickers seen on the list (SNOW / DDOG / PLTR / GDX), with the expiration in
the title line rather than per row.
"""

from datetime import date

import pytest

from coopa_trada.models import (
    AssetClass,
    CsvSignalSource,
    MixedExpirationsUnsupported,
    Right,
    UnrecognizedContractNotation,
)

TITLE = "FOMC Picks for 9/2, 9/4 Expiration"
COLS = "SYMBOL,OPTION,LAST PRICE,TVT-DELTA,SCHWAB DELTA"

SCREENSHOT_ROWS = [
    "IBIT,44P,0.71,66.4,-0.554",
    "TSLL,9C,0.48,73.2,0.679",
    "QQQ,707C,5.22,66.1,0.515",
    "AMD,460P,9.1,33.5,-0.498",
    "SNOW,220C,4.15,41.0,0.402",
    "DDOG,145P,3.80,52.5,-0.474",
    "PLTR,165C,5.60,60.1,0.523",
    "GDX,52P,1.05,38.7,-0.441",
]

ASSET_CLASSES = """
single_name:
  - AMD
  - SNOW
  - DDOG
  - PLTR
etf:
  - IBIT
  - TSLL
  - QQQ
  - GDX
"""

LIST_DAY = date(2026, 9, 2)
EXPIRY = date(2026, 9, 4)


def _source(tmp_path, rows=SCREENSHOT_ROWS, *, title=TITLE, cols=COLS,
            asset_classes=ASSET_CLASSES):
    ac = tmp_path / "asset_classes.yaml"
    ac.write_text(asset_classes, encoding="utf-8")
    csv_path = tmp_path / "signals.csv"
    csv_path.write_text(
        "\n".join([title, "", cols, *rows]) + "\n", encoding="utf-8"
    )
    return CsvSignalSource(csv_path, asset_classes_path=ac)


def _by_symbol(tmp_path, **kw):
    return {s.underlying: s for s in _source(tmp_path, **kw).fetch(day=LIST_DAY)}


# -- happy path ------------------------------------------------------------
def test_parses_every_screenshot_row(tmp_path):
    sigs = _source(tmp_path).fetch(day=LIST_DAY)
    assert [s.underlying for s in sigs] == [
        "IBIT", "TSLL", "QQQ", "AMD", "SNOW", "DDOG", "PLTR", "GDX"
    ]


def test_ibit_row_every_field(tmp_path):
    sig = _by_symbol(tmp_path)["IBIT"]
    assert sig.id == "IBIT-44P-20260904"
    assert sig.underlying == "IBIT"
    assert sig.right is Right.PUT
    assert sig.strike == 44.0
    assert sig.expiration == EXPIRY
    assert sig.asset_class is AssetClass.ETF
    assert sig.reference_price == 0.71
    assert sig.signal_delta_tvt == 66.4
    assert sig.signal_delta_schwab == -0.554
    assert sig.quantity == 0  # no size column; PositionSizer decides
    assert sig.received_at.tzinfo is not None


def test_call_rows_parse_strike_and_right(tmp_path):
    by = _by_symbol(tmp_path)
    assert (by["TSLL"].right, by["TSLL"].strike) == (Right.CALL, 9.0)
    assert (by["QQQ"].right, by["QQQ"].strike) == (Right.CALL, 707.0)
    assert (by["AMD"].right, by["AMD"].strike) == (Right.PUT, 460.0)
    assert by["AMD"].reference_price == 9.1


def test_asset_class_comes_from_the_lookup_not_a_heuristic(tmp_path):
    by = _by_symbol(tmp_path)
    assert by["GDX"].asset_class is AssetClass.ETF
    assert by["TSLL"].asset_class is AssetClass.ETF  # leveraged single-stock ETF
    assert by["SNOW"].asset_class is AssetClass.SINGLE_NAME
    assert by["PLTR"].asset_class is AssetClass.SINGLE_NAME


def test_expiration_is_shared_from_the_header(tmp_path):
    sigs = _source(tmp_path).fetch(day=LIST_DAY)
    assert {s.expiration for s in sigs} == {EXPIRY}


def test_option_letter_is_case_insensitive(tmp_path):
    by = _by_symbol(tmp_path, rows=["TSLL,9c,0.48,73.2,0.679"])
    assert by["TSLL"].right is Right.CALL


def test_fractional_strike_parses(tmp_path):
    by = _by_symbol(tmp_path, rows=["QQQ,707.5C,5.22,66.1,0.515"])
    assert by["QQQ"].strike == 707.5


# -- malformed rows don't crash the batch -------------------------------
def test_unrecognized_option_notation_is_skipped_not_fatal(tmp_path, caplog):
    rows = SCREENSHOT_ROWS + ["AMD,4-60P,1.00,10.0,0.1"]  # known ticker, bad OPTION
    src = _source(tmp_path, rows=rows)
    with caplog.at_level("WARNING"):
        sigs = src.fetch(day=LIST_DAY)
    assert len(sigs) == 8  # the 8 valid rows still parsed
    assert "unrecognized option notation" in caplog.text.lower()
    assert src.notation_errors == 1


def test_error_code_is_stable_and_in_the_message():
    assert UnrecognizedContractNotation.code == "E_SIGNAL_NOTATION"
    exc = UnrecognizedContractNotation("IBIT,weird,0.71")
    assert exc.code == "E_SIGNAL_NOTATION"
    assert "[E_SIGNAL_NOTATION]" in str(exc)
    assert exc.raw_row == "IBIT,weird,0.71"


def test_batch_level_error_line_fires_when_any_row_is_malformed(tmp_path, caplog):
    rows = SCREENSHOT_ROWS + ["IBIT,weird,0.71,66.4,-0.554"]
    with caplog.at_level("ERROR"):
        _source(tmp_path, rows=rows).fetch(day=LIST_DAY)
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("[E_SIGNAL_NOTATION]" in m and "1 row(s)" in m for m in errors)


def test_batch_level_error_line_silent_when_all_rows_clean(tmp_path, caplog):
    src = _source(tmp_path)  # the 8 clean screenshot rows
    with caplog.at_level("ERROR"):
        src.fetch(day=LIST_DAY)
    assert src.notation_errors == 0
    assert not [
        r for r in caplog.records
        if r.levelname == "ERROR" and "E_SIGNAL_NOTATION" in r.getMessage()
    ]


def test_unknown_ticker_is_skipped_not_fatal(tmp_path, caplog):
    rows = SCREENSHOT_ROWS + ["ZZZZ,10C,1.00,10.0,0.1"]
    src = _source(tmp_path, rows=rows)
    with caplog.at_level("WARNING"):
        sigs = src.fetch(day=LIST_DAY)
    assert [s.underlying for s in sigs].count("ZZZZ") == 0
    assert len(sigs) == 8
    assert "zzzz" in caplog.text.lower()
    # unknown-ticker skips are tracked separately from notation skips
    assert src.notation_errors == 0
    assert "E_SIGNAL_NOTATION" not in caplog.text


def test_blank_lines_in_the_table_are_ignored(tmp_path):
    rows = ["IBIT,44P,0.71,66.4,-0.554", "", "   ", "AMD,460P,9.1,33.5,-0.498"]
    sigs = _source(tmp_path, rows=rows).fetch(day=LIST_DAY)
    assert [s.underlying for s in sigs] == ["IBIT", "AMD"]


# -- header / structural errors ---------------------------------------
def test_missing_expiration_header_raises(tmp_path):
    src = _source(tmp_path, title="FOMC Picks (no expiration here)")
    with pytest.raises(ValueError, match="Expiration"):
        src.fetch(day=LIST_DAY)


def test_per_row_expiration_column_is_out_of_scope(tmp_path):
    src = _source(
        tmp_path,
        cols="SYMBOL,OPTION,LAST PRICE,EXPIRATION",
        rows=["IBIT,44P,0.71,2026-09-04"],
    )
    with pytest.raises(MixedExpirationsUnsupported):
        src.fetch(day=LIST_DAY)


def test_missing_required_column_raises(tmp_path):
    src = _source(tmp_path, cols="SYMBOL,LAST PRICE,TVT-DELTA", rows=["IBIT,0.71,66.4"])
    with pytest.raises(ValueError, match="OPTION"):
        src.fetch(day=LIST_DAY)


# -- day filter (list date drives received_at) -------------------------
def test_fetch_filters_by_list_date(tmp_path):
    src = _source(tmp_path)
    assert len(src.fetch(day=date(2026, 9, 2))) == 8
    assert src.fetch(day=date(2026, 9, 3)) == []
