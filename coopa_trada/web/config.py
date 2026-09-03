"""Where the web UI reads and writes.

Defaults are the *exact* CLI defaults from ``coopa_trada.__main__`` so the UI
and the CLI operate on the same files. Each is overridable by an env var for
tests and alternate setups.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Mirrors coopa_trada.__main__.DEFAULT_* -- kept in sync deliberately.
_DEFAULT_CONFIG = "config/rules.yaml"
_DEFAULT_DB = "./trada.db"
_DEFAULT_SIGNALS = "data/signals.example.csv"


@dataclass
class WebPaths:
    """Mutable: ``signals_path`` can be repointed at runtime via /api/signals."""

    config_path: str = _DEFAULT_CONFIG
    db_path: str = _DEFAULT_DB
    signals_path: str = _DEFAULT_SIGNALS


def paths_from_env(env: dict[str, str] | None = None) -> WebPaths:
    e = os.environ if env is None else env
    return WebPaths(
        config_path=e.get("COOPA_TRADA_CONFIG", _DEFAULT_CONFIG),
        db_path=e.get("COOPA_TRADA_DB", _DEFAULT_DB),
        signals_path=e.get("COOPA_TRADA_SIGNALS", _DEFAULT_SIGNALS),
    )
