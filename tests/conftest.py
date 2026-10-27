import pytest

from coopa_trada import keepawake as _keepawake

REAL_SPAWN = _keepawake._spawn  # kept for the one test that checks its arguments (with Popen faked)


@pytest.fixture(autouse=True)
def _isolated_lock_dir(tmp_path, monkeypatch):
    """Account locks go to a per-test folder, never the real ~/.coopa_trada."""
    monkeypatch.setenv("COOPA_TRADA_LOCK_DIR", str(tmp_path / "locks"))
    # Learned tickers go to a per-test file, never the repo's config folder.
    monkeypatch.setenv("COOPA_TRADA_LEARNED_TICKERS", str(tmp_path / "learned-tickers.yaml"))


class FakeCaffeinate:
    """Stands in for the caffeinate child process."""

    started: list = []

    def __init__(self, pid):
        self.pid = pid
        self.returncode = None
        FakeCaffeinate.started.append(self)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    kill = terminate


class _Outbox(list):
    """Every notification a test would have sent; nothing leaves the process."""


@pytest.fixture(autouse=True)
def sent_alerts(monkeypatch):
    """No real desktop notifications, ntfy pushes or caffeinate processes in tests."""
    from coopa_trada import alerts, keepawake

    outbox = _Outbox()

    def fake_osascript(args):
        outbox.append(("mac", args))
        import subprocess
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_post(url, body, headers):
        outbox.append(("ntfy", url, body))
        return 200

    monkeypatch.setattr(alerts, "_osascript", fake_osascript)
    monkeypatch.setattr(alerts, "_post", fake_post)
    monkeypatch.setattr(keepawake, "_spawn", FakeCaffeinate)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    # No test reaches the Claude API or Telegram; tests that need them set fakes.
    for name in ("ANTHROPIC_API_KEY", "COOPA_IMAGE_READER", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS",
                 "AUTO_START_LIVE", "DASHBOARD_PASSWORD", "COOPA_TRADA_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("NTFY_SERVER", raising=False)
    return outbox
