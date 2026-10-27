"""New tickers on a pick list are learned automatically.

Every ticker needs an asset class (stock or ETF): it decides when a position
is watched on the day it is bought (Settings -> Advanced -> Exit windows).
``config/asset_classes.yaml`` ships with the code; a ticker that isn't in it is
looked up at Schwab when a list is previewed, and the answer is saved to this
bot's own ``config/asset_classes.learned.yaml`` so the run knows it too.

A ticker Schwab doesn't know is refused by name (usually a misread or a typo).
If Schwab can't be asked (not connected), the list waits for a reconnect: a
ticker is never guessed, so a misread can't be remembered as real.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from .logs import get_logger
from .models import DEFAULT_ASSET_CLASSES_PATH, AssetClass, _find_config, load_asset_class_map

__all__ = ["learned_path", "known_classes", "learn_tickers", "schwab_lookup", "TickerLookupUnavailable"]

log = get_logger("coopa_trada.tickers", "APP")
_KEYS = {AssetClass.SINGLE_NAME: "single_name", AssetClass.ETF: "etf"}


class TickerLookupUnavailable(RuntimeError):
    """Schwab could not be asked (no login, network down)."""


def learned_path() -> Path:
    return Path(os.environ.get("COOPA_TRADA_LEARNED_TICKERS") or "config/asset_classes.learned.yaml")


def _read_learned(path: Path) -> dict[str, AssetClass]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = {}
    for ac, key in _KEYS.items():
        for raw in (data.get(key) or []) if isinstance(data, dict) else []:
            out[str(raw).strip().upper()] = ac
    return out


def known_classes() -> dict[str, AssetClass]:
    """The shipped list plus what this bot has learned (the shipped list wins)."""
    return {**_read_learned(learned_path()),
            **load_asset_class_map(_find_config(Path(DEFAULT_ASSET_CLASSES_PATH)))}


def _save(learned: dict[str, AssetClass]) -> None:
    path = learned_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {key: sorted(t for t, a in learned.items() if a is ac) for ac, key in _KEYS.items()}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tickers.", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("# Tickers this bot learned from pick lists (looked up at Schwab).\n"
                         "# Safe to edit: move a ticker between the lists to change its class.\n")
            yaml.safe_dump(body, handle, sort_keys=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def schwab_lookup(symbols: list[str]) -> dict[str, AssetClass | None]:
    """{ticker: class} from Schwab's quotes; None for a ticker Schwab doesn't know."""
    from .wiring import schwab_client
    try:
        client, _ = schwab_client()
        resp = client.get_quotes(symbols)
        if getattr(resp, "status_code", 200) != 200:
            raise TickerLookupUnavailable(f"Schwab answered {resp.status_code}")
        data = resp.json() if hasattr(resp, "json") else resp
    except TickerLookupUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - no login, network: fall back below
        raise TickerLookupUnavailable(str(exc)) from exc
    out: dict[str, AssetClass | None] = {}
    for symbol in symbols:
        node = (data or {}).get(symbol)
        if not isinstance(node, dict) or "assetMainType" not in node:
            out[symbol] = None
            continue
        sub = str(node.get("assetSubType") or "").upper()
        out[symbol] = AssetClass.ETF if sub in ("ETF", "ETN") else AssetClass.SINGLE_NAME
    return out


def learn_tickers(symbols, lookup=schwab_lookup) -> list[str]:
    """Learn every ticker in ``symbols`` not known yet. Returns notes for the person.

    Raises ValueError naming any ticker Schwab says doesn't exist.
    """
    known = known_classes()
    new = sorted({s.strip().upper() for s in symbols if s and s.strip()} - set(known))
    if not new:
        return []
    plural = "s" if len(new) > 1 else ""
    try:
        found = lookup(new)
    except TickerLookupUnavailable as exc:
        # Not guessed: a misread ticker must not be remembered as real. The bot can't
        # trade without Schwab anyway, so this only waits for a reconnect.
        log.warning("could not look up %s at Schwab: %s", ", ".join(new), exc)
        raise ValueError(f"{', '.join(new)}: unknown ticker{plural} that couldn't be checked with Schwab "
                         "(it isn't connected). Reconnect Schwab on the Dashboard, then preview again.") from None
    unknown = [s for s in new if found.get(s) is None]
    if unknown:
        raise ValueError(f"Schwab doesn't know the ticker{'s' if len(unknown) > 1 else ''} "
                         f"{', '.join(unknown)} (unknown ticker): check the spelling against the picture.")
    _save({**_read_learned(learned_path()), **{s: found[s] for s in new}})
    etfs = [s for s in new if found[s] is AssetClass.ETF]
    stocks = [s for s in new if found[s] is AssetClass.SINGLE_NAME]
    parts = ([f"stocks: {', '.join(stocks)}"] if stocks else []) + ([f"ETFs: {', '.join(etfs)}"] if etfs else [])
    log.info("learned tickers: %s", ", ".join(f"{s}={found[s].name}" for s in new))
    return [f"Added new ticker{plural} (" + "; ".join(parts) + ")."]
