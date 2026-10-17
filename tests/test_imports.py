import io
from pathlib import Path

import pytest

from coopa_trada.imports import preview_csv, cells_to_csv
from coopa_trada.web import create_app
from coopa_trada.web.config import WebPaths

CSV = Path("data/signals.example.csv").read_text()


@pytest.fixture
def client(tmp_path):
    paths = WebPaths(db_path=str(tmp_path / "test.db"))
    return create_app(paths).test_client()


def test_screenshot_fixture_matches_all_eight_picks():
    preview = preview_csv(CSV, "2026-09-02", "2026-09-04")
    assert [r["option"] for r in preview["rows"]] == ["44P", "9C", "707C", "460P", "322.5C", "222.5C", "180C", "95C"]


def test_upload_preview_confirm_does_not_trade(client):
    before = client.application.config["COOPA_PATHS"].signals_path
    upload = client.post("/api/signals/upload", data={"file": (io.BytesIO(CSV.encode()), "picks.csv")})
    assert upload.status_code == 200
    assert client.application.config["COOPA_PATHS"].signals_path == before
    preview = client.post("/api/signals/preview", json={"csv": upload.json["csv"], "date": "2026-09-02", "expiration": "2026-09-04"})
    assert preview.status_code == 200
    assert len(preview.json["rows"]) == 8
    assert client.application.config["COOPA_PATHS"].signals_path == before
    confirmed = client.post("/api/signals/confirm", json={"token": preview.json["token"]})
    assert confirmed.status_code == 200
    assert Path(confirmed.json["signals_path"]).read_text().startswith("Trade date: 2026-09-02;")
    assert client.application.config["COOPA_RUNNER"].status == "idle"
    assert not Path(client.application.config["COOPA_PATHS"].db_path).exists()


@pytest.mark.parametrize("text", [CSV.replace("44P", "44?"), CSV.replace("IBIT", "UNKNOWN"), CSV.replace("0.71", "nan"), CSV.replace("0.71", "-1"), CSV + "IBIT,44P,0.71,66.4,-0.554\n"])
def test_bad_or_duplicate_rows_reject_entire_preview(text):
    with pytest.raises(ValueError):
        preview_csv(text, "2026-09-02", "2026-09-04")


def test_dates_required_and_expiration_cannot_precede_trade_date(client):
    for day, exp in [("", ""), ("2026-09-04", "2026-09-02")]:
        assert client.post("/api/signals/preview", json={"csv": CSV, "date": day, "expiration": exp}).status_code == 400


def test_confirm_rejects_tampering_and_changes_during_run(client):
    assert client.post("/api/signals/confirm", json={"token": "made-up"}).status_code == 400
    preview = client.post("/api/signals/preview", json={"csv": CSV, "date": "2026-09-02", "expiration": "2026-09-04"}).json
    client.application.config["COOPA_RUNNER"].status = "running"
    assert client.post("/api/signals/confirm", json={"token": preview["token"]}).status_code == 409


def test_upload_type_size_and_origin_are_checked(client):
    assert client.post("/api/signals/upload", data={"file": (io.BytesIO(b"x"), "x.exe")}).status_code == 400
    assert client.post("/api/signals/upload", data={"file": (io.BytesIO(b"x" * (8 * 1024 * 1024)), "x.csv")}).status_code == 413
    assert client.post("/api/run/start", json={}, headers={"Origin": "https://other.example"}).status_code == 403


def test_ocr_cells_are_grouped_by_coordinates():
    headers = ["SYMBOL", "OPTION", "LAST PRICE", "TVT-DELTA", "SCHWAB DELTA"]
    values = ["SNOW", "322.5C", "18.4", "79.8", "0.507"]
    cells = [{"text": text, "x": idx * .2, "y": y} for y, row in [(.2, headers), (.4, values)] for idx, text in enumerate(row)]
    result = preview_csv(cells_to_csv(cells[::-1]), "2026-09-02", "2026-09-04")
    assert result["rows"][0]["option"] == "322.5C"


def test_actual_sample_image_recognition_matches_csv_exactly():
    import json
    cells = json.loads(Path("tests/fixtures/sample_ocr.json").read_text())
    image = preview_csv(cells_to_csv(cells), "2026-09-02", "2026-09-04")
    reference = preview_csv(CSV, "2026-09-02", "2026-09-04")
    assert image["rows"] == reference["rows"]
