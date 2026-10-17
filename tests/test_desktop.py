"""Double-click launcher: home folder, first run, picture reader off-Mac, live gates."""
import io
import subprocess
import sys
from pathlib import Path

import pytest

from coopa_trada import desktop, imports

ROOT = Path(__file__).resolve().parent.parent


def test_home_is_the_folder_containing_the_app(tmp_path):
    exe = tmp_path / "Apps" / "Coopa Trada.app" / "Contents" / "MacOS" / "Coopa Trada"
    assert desktop.app_home(str(exe), environ={}) == (tmp_path / "Apps").resolve()
    # A plain executable (e.g. a future Windows build): its own folder.
    assert desktop.app_home(str(tmp_path / "bin" / "coopa"), environ={}) == (tmp_path / "bin").resolve()
    assert desktop.app_home(str(exe), environ={"COOPA_TRADA_HOME": str(tmp_path / "x")}) == (tmp_path / "x").resolve()


def test_translocated_copy_is_detected():
    assert desktop.is_translocated(Path("/private/var/folders/ab/T/AppTranslocation/1234/d/Coopa Trada.app/.."))
    assert not desktop.is_translocated(Path("/Users/cooper/Documents/Coopa Trada"))


def test_first_run_copies_examples_but_never_env_and_never_overwrites(tmp_path):
    created = desktop.prepare_home(tmp_path, ROOT)
    assert sorted(created) == sorted(desktop.FIRST_RUN_FILES)
    assert not (tmp_path / ".env").exists()
    rules = tmp_path / "config" / "rules.yaml"
    rules.write_text("# Cooper's edits\n")
    assert desktop.prepare_home(tmp_path, ROOT) == []
    assert rules.read_text() == "# Cooper's edits\n"


def test_translocated_launch_refuses_with_instructions(monkeypatch, capsys):
    monkeypatch.setattr(desktop, "app_home", lambda: Path("/private/var/x/AppTranslocation/y"))
    assert desktop.main(["--no-browser"]) == 2
    assert "Move 'Coopa Trada.app'" in capsys.readouterr().out


# -- picture reader off-Mac ---------------------------------------------------------
def test_picture_reader_unavailable_off_mac_with_plain_message(monkeypatch):
    monkeypatch.setattr(imports.platform, "system", lambda: "Windows")
    status = imports.image_reader_status()
    assert status["available"] is False and "only works on a Mac" in status["reason"]
    with pytest.raises(ValueError, match="upload the CSV file instead"):
        imports.recognize_image(Path("picks.png"))


def test_mac_without_reader_says_so(monkeypatch):
    monkeypatch.setattr(imports.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(imports, "_compiled_helper", lambda: None)
    monkeypatch.setattr(imports.shutil, "which", lambda _name: None)
    assert "missing from this copy" in imports.image_reader_status()["reason"]


def test_csv_upload_still_works_off_mac(monkeypatch, tmp_path):
    pytest.importorskip("flask")
    from coopa_trada.web import create_app
    from coopa_trada.web.config import WebPaths
    from coopa_trada.web.runner import EngineRunner
    monkeypatch.setattr(imports.platform, "system", lambda: "Linux")
    paths = WebPaths(config_path="config/rules.example.yaml", db_path=str(tmp_path / "t.db"),
                     signals_path="data/signals.example.csv")
    client = create_app(paths, EngineRunner(paths)).test_client()
    assert client.get("/api/app").get_json()["image_upload"]["available"] is False
    csv_bytes = Path("data/signals.example.csv").read_bytes()
    ok = client.post("/api/signals/upload", data={"file": (io.BytesIO(csv_bytes), "picks.csv")})
    assert ok.status_code == 200 and "SYMBOL" in ok.get_json()["csv"]
    img = client.post("/api/signals/upload", data={"file": (io.BytesIO(b"\x89PNG"), "picks.png")})
    assert img.status_code == 400 and "only works on a Mac" in img.get_json()["error"]


# -- quit --------------------------------------------------------------------
def test_quit_only_from_the_app_and_never_mid_run(tmp_path):
    pytest.importorskip("flask")
    from coopa_trada.web import create_app
    from coopa_trada.web.config import WebPaths
    from coopa_trada.web.runner import EngineRunner
    paths = WebPaths(config_path="config/rules.example.yaml", db_path=str(tmp_path / "t.db"),
                     signals_path="data/signals.example.csv")
    app = create_app(paths, EngineRunner(paths))
    client = app.test_client()
    assert client.post("/api/app/quit", json={}).status_code == 400  # not launched by the app
    calls = []
    app.config["COOPA_SHUTDOWN"] = lambda: calls.append(1)
    app.config["COOPA_RUNNER"].status = "running"
    assert client.post("/api/app/quit", json={}).status_code == 409
    app.config["COOPA_RUNNER"].status = "idle"
    assert client.post("/api/app/quit", json={}).status_code == 200 and calls == [1]


# -- the packaged-build gate check, run against the source launcher --------------
def test_launcher_keeps_dry_run_default_and_every_live_gate():
    result = subprocess.run([sys.executable, str(ROOT / "packaging" / "verify_app.py"), "--source"],
                            capture_output=True, text=True, timeout=240, cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "All packaging checks passed." in result.stdout
