"""The server deployment pack keeps its safety properties: restart on crash and
on reboot, dashboard on localhost only, private files, Mountain time, and setup
scripts that never write secrets. Nothing here runs the scripts."""
import configparser
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def _unit():
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    text = (DEPLOY / "coopa-trada.service").read_text()
    parser.read_string(text)
    return parser, text


def test_service_restarts_on_crash_and_on_reboot():
    unit, _ = _unit()
    assert unit["Service"]["Restart"] == "always"
    assert unit["Install"]["WantedBy"] == "multi-user.target"
    assert int(unit["Unit"]["StartLimitBurst"]) >= 1  # a broken setup can't loop forever


def test_dashboard_stays_on_localhost():
    unit, _ = _unit()
    cmd = unit["Service"]["ExecStart"]
    assert "-m coopa_trada.web" in cmd and "--host 127.0.0.1" in cmd
    assert "0.0.0.0" not in cmd


def test_files_are_private_and_time_is_mountain():
    unit, text = _unit()
    assert unit["Service"]["UMask"] == "0077"
    assert "Environment=TZ=America/Denver" in text
    assert unit["Service"]["User"] == "coopa"
    assert unit["Service"]["ProtectSystem"] == "strict"
    assert "COOPA_TRADA_LOCK_DIR=" + unit["Service"]["WorkingDirectory"] in text


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("script", ["setup_server.sh", "lock_down.sh"])
def test_scripts_parse(script):
    subprocess.run(["bash", "-n", str(DEPLOY / script)], check=True)


@pytest.mark.parametrize("script", ["setup_server.sh", "lock_down.sh"])
def test_scripts_never_write_secrets_or_turn_on_live(script):
    text = (DEPLOY / script).read_text()
    assert not re.search(r">>?\s*\S*(\.env|schwab_token\.json)", text)
    assert not re.search(r"cp\s.*\.env", text)
    assert "LIVE_TRADING" not in text and "AUTO_START_LIVE" not in text
    assert "set -euo pipefail" in text


def test_lock_down_makes_secrets_owner_only():
    text = (DEPLOY / "lock_down.sh").read_text()
    assert 'chmod 700 "$APP_DIR"' in text
    assert "chmod 600" in text and ".env schwab_token.json" in text
