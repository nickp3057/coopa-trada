import pytest


@pytest.fixture(autouse=True)
def _isolated_lock_dir(tmp_path, monkeypatch):
    """Account locks go to a per-test folder, never the real ~/.coopa_trada."""
    monkeypatch.setenv("COOPA_TRADA_LOCK_DIR", str(tmp_path / "locks"))


class _Outbox(list):
    """Every notification a test would have sent; nothing leaves the process."""


@pytest.fixture(autouse=True)
def sent_alerts(monkeypatch):
    """No real desktop notifications or ntfy pushes in tests."""
    from coopa_trada import alerts

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
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("NTFY_SERVER", raising=False)
    return outbox
