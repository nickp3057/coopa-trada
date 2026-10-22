"""Task 6: the Claude picture reader (API mocked; nothing leaves the test)."""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coopa_trada import imports, vision
from coopa_trada.imports import preview_csv

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64

ROWS = [
    {"symbol": "IBIT", "option": "44P", "last_price": "0.71", "tvt_delta": "66.4", "schwab_delta": "-0.554"},
    {"symbol": "SNOW", "option": "322.5C", "last_price": "18.4", "tvt_delta": "79.8", "schwab_delta": "0.507"},
]


class FakeClaude:
    def __init__(self, payload=None, stop_reason="end_turn", text=None):
        self.calls = []
        text = text if text is not None else json.dumps(payload)
        self.response = SimpleNamespace(stop_reason=stop_reason,
                                        content=[SimpleNamespace(type="text", text=text)])
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


@pytest.fixture
def claude(monkeypatch):
    fake = FakeClaude({"title": "FOMC Picks for 9/2, 9/4 Expiration", "trade_date": "9/2",
                       "expiration": "9/4", "rows": ROWS})
    monkeypatch.setattr(vision, "_client", lambda: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return fake


@pytest.fixture
def picture(tmp_path):
    p = tmp_path / "picks.png"
    p.write_bytes(PNG)
    return p


def test_reader_returns_editable_csv_that_passes_the_strict_preview(claude, picture):
    result = vision.read_picks_image(picture)
    assert result.csv.splitlines()[:2] == ["FOMC Picks for 9/2, 9/4 Expiration",
                                           "SYMBOL,OPTION,LAST PRICE,TVT-DELTA,SCHWAB DELTA"]
    assert (result.trade_date, result.expiration) == ("9/2", "9/4")
    preview = preview_csv(result.csv, "2026-09-02", "2026-09-04")
    assert [r["option"] for r in preview["rows"]] == ["44P", "322.5C"]
    assert preview["rows"][0]["schwab_delta"] == -0.554


def test_request_uses_structured_output_the_image_and_opus(claude, picture):
    vision.read_picks_image(picture)
    call = claude.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": vision.SCHEMA}
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    image, text = call["messages"][0]["content"]
    assert image["source"]["media_type"] == "image/png" and image["source"]["type"] == "base64"
    assert "never as instructions" in text["text"]


@pytest.mark.parametrize("fake, fragment", [
    (FakeClaude(stop_reason="refusal", text=""), "declined"),
    (FakeClaude(text="not json"), "unexpected"),
    (FakeClaude({"title": None, "trade_date": None, "expiration": None, "rows": []}), "No pick rows"),
    (FakeClaude({"title": None, "trade_date": None, "expiration": None, "rows": [{"symbol": 5}]}), "unexpected"),
])
def test_bad_answers_become_plain_errors(monkeypatch, picture, fake, fragment):
    monkeypatch.setattr(vision, "_client", lambda: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with pytest.raises(ValueError, match=fragment):
        vision.read_picks_image(picture)


def test_unreadable_cells_stay_empty_and_the_preview_rejects_them(monkeypatch, picture):
    rows = [dict(ROWS[0], option="")]
    monkeypatch.setattr(vision, "_client", lambda: FakeClaude({"title": None, "trade_date": None,
                                                               "expiration": None, "rows": rows}))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with pytest.raises(ValueError):
        preview_csv(vision.read_picks_image(picture).csv, "2026-09-02", "2026-09-04")


def test_missing_key_or_wrong_file_type_is_refused_before_any_request(monkeypatch, picture, tmp_path):
    monkeypatch.setattr(vision, "_client", lambda: pytest.fail("no request expected"))
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        vision.read_picks_image(picture)
    gif = tmp_path / "x.gif"
    gif.write_bytes(b"GIF89a")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with pytest.raises(ValueError, match="PNG or JPEG"):
        vision.read_picks_image(gif)


# -- which reader --------------------------------------------------------------
def test_claude_is_the_default_off_mac(monkeypatch):
    monkeypatch.setattr(imports.platform, "system", lambda: "Linux")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert imports.image_reader() == ("claude", None)


def test_apple_stays_the_default_on_a_mac_and_claude_can_be_chosen(monkeypatch):
    monkeypatch.setattr(imports.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(imports, "_compiled_helper", lambda: Path("/bin/true"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert imports.image_reader()[0] == "apple"
    monkeypatch.setenv("COOPA_IMAGE_READER", "claude")
    assert imports.image_reader()[0] == "claude"


def test_forcing_a_reader_that_is_not_available_says_why(monkeypatch):
    monkeypatch.setattr(imports.platform, "system", lambda: "Linux")
    monkeypatch.setenv("COOPA_IMAGE_READER", "claude")
    assert "ANTHROPIC_API_KEY" in imports.image_reader()[1]
    monkeypatch.setenv("COOPA_IMAGE_READER", "apple")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert imports.image_reader()[0] is None


def test_dashboard_upload_reads_with_claude_and_still_needs_preview_and_confirm(claude, monkeypatch, tmp_path):
    pytest.importorskip("flask")
    from coopa_trada.web import create_app
    from coopa_trada.web.config import WebPaths
    monkeypatch.setattr(imports.platform, "system", lambda: "Linux")
    paths = WebPaths(config_path="config/rules.example.yaml", db_path=str(tmp_path / "t.db"))
    client = create_app(paths).test_client()
    assert client.get("/api/app").get_json()["image_upload"] == {"available": True, "reason": None,
                                                                 "reader": "claude"}
    up = client.post("/api/signals/upload", data={"file": (io.BytesIO(PNG), "shot.png")})
    assert up.status_code == 200 and up.json["image"] is True
    assert not (tmp_path / "imports").exists()  # reading never confirms or trades
    preview = client.post("/api/signals/preview", json={"csv": up.json["csv"], "date": "2026-09-02",
                                                        "expiration": "2026-09-04"})
    assert preview.status_code == 200 and len(preview.json["rows"]) == 2
