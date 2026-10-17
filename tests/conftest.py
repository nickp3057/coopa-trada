import pytest


@pytest.fixture(autouse=True)
def _isolated_lock_dir(tmp_path, monkeypatch):
    """Account locks go to a per-test folder, never the real ~/.coopa_trada."""
    monkeypatch.setenv("COOPA_TRADA_LOCK_DIR", str(tmp_path / "locks"))
