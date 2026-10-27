"""Domain types and signal ingestion.

``CsvSignalSource`` parses Cooper's real "FOMC Picks" list format: a title line
carrying the expiration ("... 9/4 Expiration"), then a table with columns
``SYMBOL | OPTION | LAST PRICE | TVT-DELTA | SCHWAB DELTA``. The ``OPTION`` cell
is ``{strike}{C|P}`` (e.g. ``44P``); a row that does not match is skipped with a
warning rather than guessed at. Asset class comes from an explicit hand-edited
lookup (``config/asset_classes.yaml``), never a heuristic.

Contract notation and one-contract sizing are settled. The spending cap, exit
interpretation, and delta policy still require Cooper's confirmation (see README).
"""

from __future__ import annotations

import csv
import math
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path

import yaml

from . import clock

__all__ = [
    "Right",
    "AssetClass",
    "Signal",
    "SignalSource",
    "CsvSignalSource",
    "UnrecognizedContractNotation",
    "UnknownAssetClass",
    "MixedExpirationsUnsupported",
    "load_asset_class_map",
    "DEFAULT_ASSET_CLASSES_PATH",
]

_log = logging.getLogger("coopa_trada.models")

DEFAULT_ASSET_CLASSES_PATH = "config/asset_classes.yaml"


class Right(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


class AssetClass(str, Enum):
    SINGLE_NAME = "SINGLE_NAME"
    ETF = "ETF"


@dataclass(frozen=True)
class Signal:
    id: str
    underlying: str
    right: Right
    strike: float | None
    expiration: date | None
    quantity: int
    asset_class: AssetClass
    received_at: datetime  # tz-aware
    raw: str
    # --- fields captured from Cooper's list, backward-compatible (optional) ---
    # LAST PRICE: the option's last traded price when the list was made. NOT an
    # executable limit -- entry still gets a live quote via ContractResolver.
    reference_price: float | None = None
    # TVT-DELTA / SCHWAB DELTA: recorded only. We do not filter on them yet --
    # unknown whether Cooper pre-filters upstream or we are meant to. (question out)
    signal_delta_tvt: float | None = None
    signal_delta_schwab: float | None = None

    def __post_init__(self) -> None:
        clock.ensure_aware(self.received_at)


class SignalSource(ABC):
    """Ingestion boundary. Implementations return the signals for a trading day."""

    @abstractmethod
    def fetch(self, day: date | None = None) -> list[Signal]:
        """Return signals. If ``day`` is given, restrict to that market-local date."""
        raise NotImplementedError


# --------------------------------------------------------------------------
# Exceptions raised by CsvSignalSource. (The build spec calls these siblings of
# ContractUnderspecified / ContractNotTradable; those live in contracts.py --
# these live here, where CsvSignalSource raises them.)
# --------------------------------------------------------------------------
class UnrecognizedContractNotation(ValueError):
    """An OPTION cell did not match ``^(\\d+\\.?\\d*)([CP])$``.

    ``{strike}{C|P}`` is a permanent, confirmed format -- a non-match is a data
    error to surface loudly, not a new format to support. Carries ``.raw_row``
    and the stable ``.code`` ``"E_SIGNAL_NOTATION"`` (included in ``str()`` so it
    stands out in logs). The row is skipped with a per-row ``WARNING``; the rest
    of the batch still parses. ``CsvSignalSource.fetch`` also emits one
    batch-level ``ERROR`` summary when any row hit this, and the ``resolve`` CLI
    prints the count.
    """

    code = "E_SIGNAL_NOTATION"

    def __init__(self, raw_row: str):
        super().__init__(f"[{self.code}] unrecognized OPTION notation: {raw_row!r}")
        self.raw_row = raw_row


class UnknownAssetClass(ValueError):
    """A ticker is not present in ``config/asset_classes.yaml``.

    Carries the ticker. The row is skipped with a logged warning; add the ticker
    to the YAML (it is meant to be hand-edited as Cooper's list grows).
    """

    def __init__(self, ticker: str):
        super().__init__(
            f"ticker {ticker!r} not in {DEFAULT_ASSET_CLASSES_PATH}; add it "
            f"under single_name: or etf:"
        )
        self.ticker = ticker


class MixedExpirationsUnsupported(ValueError):
    """A per-row expiration column was found.

    Expiration is a per-list header, shared by every row. Per-row expirations are
    out of scope -- we raise rather than silently take the header value.
    """


def _find_config(path: Path) -> Path:
    """A relative config path missing from the working folder falls back to the
    copy shipped with the code (a server data folder needs no copy of it)."""
    path = Path(path)
    if path.is_absolute() or path.exists():
        return path
    shipped = Path(__file__).resolve().parents[1] / path
    return shipped if shipped.exists() else path


# --------------------------------------------------------------------------
# Asset-class lookup
# --------------------------------------------------------------------------
def load_asset_class_map(path: str | Path) -> dict[str, AssetClass]:
    """Read ``config/asset_classes.yaml`` -> ``{TICKER: AssetClass}``.

    Format: two lists, ``single_name:`` and ``etf:``. Tickers are upper-cased. A
    ticker listed under both classes is an error.
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    out: dict[str, AssetClass] = {}
    for key, ac in (("single_name", AssetClass.SINGLE_NAME), ("etf", AssetClass.ETF)):
        for raw in data.get(key) or []:
            ticker = str(raw).strip().upper()
            if not ticker:
                continue
            if ticker in out and out[ticker] is not ac:
                raise ValueError(f"{path}: ticker {ticker!r} listed under both classes")
            out[ticker] = ac
    if not out:
        raise ValueError(f"{path}: no tickers found under single_name: / etf:")
    return out


# --------------------------------------------------------------------------
# CsvSignalSource
# --------------------------------------------------------------------------
_OPTION_RE = re.compile(r"^(\d+\.?\d*)([CP])$", re.IGNORECASE)

# "FOMC Picks for 9/2, 9/4 Expiration" -> (list M/D, expiration M/D)
_HEADER_FULL_RE = re.compile(
    r"for\s+(\d{1,2})/(\d{1,2})\s*,\s*(\d{1,2})/(\d{1,2})\s+expiration",
    re.IGNORECASE,
)
# "... 9/4 Expiration" -> expiration M/D only
_HEADER_EXP_RE = re.compile(r"(\d{1,2})/(\d{1,2})\s+expiration", re.IGNORECASE)

_PER_ROW_EXP_COLS = {"EXP", "EXPIRATION", "EXPIRY", "EXP DATE", "EXPIRATION DATE"}

_COL_SYMBOL = "SYMBOL"
_COL_OPTION = "OPTION"
_COL_LAST_PRICE = "LAST PRICE"
_COL_TVT_DELTA = "TVT-DELTA"
_COL_SCHWAB_DELTA = "SCHWAB DELTA"
_REQUIRED_COLS = (_COL_SYMBOL, _COL_OPTION, _COL_LAST_PRICE)


def _opt_float(value: str | None) -> float | None:
    value = (value or "").strip()
    result = float(value) if value else None
    if result is not None and not math.isfinite(result):
        raise ValueError("numeric cells must be finite")
    return result


def _resolve_year(month: int, day_: int, ref: date) -> date:
    """Pick the year for a bare ``M/D`` relative to ``ref``.

    Same year as ``ref``; roll forward if that lands the date well before ``ref``
    (a January expiration parsed from a December list).
    """
    candidate = date(ref.year, month, day_)
    if candidate < ref - timedelta(days=45):
        candidate = date(ref.year + 1, month, day_)
    return candidate


class CsvSignalSource(SignalSource):
    """Parses Cooper's "FOMC Picks" list.

    - First line matching ``... <M>/<D> Expiration`` gives the shared expiration
      (and, in the full "for <M>/<D>, <M>/<D> Expiration" form, the list date,
      used as every signal's ``received_at``).
    - The first line whose cells include ``SYMBOL`` and ``OPTION`` is the column
      header; the table runs from there.
    - ``OPTION`` -> strike + right via ``^(\\d+\\.?\\d*)([CP])$``. A non-match
      raises :class:`UnrecognizedContractNotation`; the row is skipped, batch
      continues.
    - ``SYMBOL`` -> asset class via ``config/asset_classes.yaml``. An unknown
      ticker raises :class:`UnknownAssetClass`; the row is skipped, batch
      continues.
    - ``LAST PRICE`` -> ``reference_price``; ``TVT-DELTA`` / ``SCHWAB DELTA`` ->
      ``signal_delta_tvt`` / ``signal_delta_schwab`` (recorded, unused).
    - ``quantity`` is set to 0: the list carries no size column and sizing is
      delegated to ``rules.FixedQuantitySizer``.
    """

    def __init__(
        self,
        path: str | Path,
        asset_classes_path: str | Path = DEFAULT_ASSET_CLASSES_PATH,
    ):
        self.path = Path(path)
        self.asset_classes_path = Path(asset_classes_path)
        # Count of rows skipped for unrecognized OPTION notation in the most
        # recent fetch(). Read by the CLI so a human sees a nonzero count.
        self.notation_errors = 0

    def fetch(self, day: date | None = None) -> list[Signal]:
        text = self.path.read_text(encoding="utf-8")
        ac_map = load_asset_class_map(_find_config(self.asset_classes_path))
        if self.asset_classes_path == Path(DEFAULT_ASSET_CLASSES_PATH):
            # Plus the tickers this bot learned from earlier lists (the shipped list wins).
            from .tickers import known_classes
            ac_map = {**known_classes(), **ac_map}

        ref = day or clock.to_market(clock.now_utc()).date()
        list_date, expiration = self._parse_header(text, ref)
        received_at = clock.combine_market(list_date, time(0, 0))

        signals: list[Signal] = []
        notation_errors = 0
        for raw_row, row in self._iter_table(text):
            try:
                signals.append(
                    self._row_to_signal(row, raw_row, expiration, received_at, ac_map)
                )
            except UnrecognizedContractNotation as exc:
                notation_errors += 1
                _log.warning("CsvSignalSource: skipping row -- %s", exc)
                continue
            except UnknownAssetClass as exc:
                _log.warning("CsvSignalSource: skipping row -- %s", exc)
                continue

        self.notation_errors = notation_errors
        if notation_errors:
            _log.error(
                "[%s] %d row(s) skipped this batch due to unrecognized contract "
                "notation -- see warnings above",
                UnrecognizedContractNotation.code,
                notation_errors,
            )

        if day is not None:
            signals = [
                s for s in signals if clock.to_market(s.received_at).date() == day
            ]
        return signals

    # -- header ---------------------------------------------------------
    def _parse_header(self, text: str, ref: date) -> tuple[date, date]:
        explicit = re.search(r"Trade date: (\d{4}-\d{2}-\d{2}); Expiration: (\d{4}-\d{2}-\d{2})", text)
        if explicit:
            listed, expiration = (date.fromisoformat(v) for v in explicit.groups())
            if expiration < listed:
                raise ValueError("expiration precedes trade date")
            return listed, expiration
        for line in text.splitlines():
            m = _HEADER_FULL_RE.search(line)
            if m:
                lm, ld, em, ed = (int(g) for g in m.groups())
                list_date = _resolve_year(lm, ld, ref)
                expiration = _resolve_year(em, ed, ref)
                if expiration < list_date:
                    expiration = date(
                        expiration.year + 1, expiration.month, expiration.day
                    )
                return list_date, expiration
            m = _HEADER_EXP_RE.search(line)
            if m:
                em, ed = int(m.group(1)), int(m.group(2))
                expiration = _resolve_year(em, ed, ref)
                return expiration, expiration
        raise ValueError(
            f"{self.path}: no '<M>/<D> Expiration' header line found; "
            f"expiration is required and is not a per-row field"
        )

    # -- table ---------------------------------------------------------
    def _iter_table(self, text: str):
        lines = text.splitlines()
        header_idx = None
        for i, line in enumerate(lines):
            cells = {c.strip().upper() for c in line.split(",")}
            if _COL_SYMBOL in cells and _COL_OPTION in cells:
                header_idx = i
                break
        if header_idx is None:
            raise ValueError(
                f"{self.path}: no column-header line (need SYMBOL and OPTION columns)"
            )

        reader = csv.DictReader(lines[header_idx:])
        norm = {name: (name or "").strip().upper() for name in (reader.fieldnames or [])}
        present = set(norm.values())

        bad_exp = _PER_ROW_EXP_COLS & present
        if bad_exp:
            raise MixedExpirationsUnsupported(
                f"{self.path}: per-row expiration column(s) {sorted(bad_exp)} present; "
                f"only a per-list header expiration is supported"
            )
        missing = [c for c in _REQUIRED_COLS if c not in present]
        if missing:
            raise ValueError(f"{self.path}: missing column(s): {', '.join(missing)}")

        inv = {v: k for k, v in norm.items()}  # normalized -> actual field name
        for row in reader:
            values = [(row.get(f) or "").strip() for f in reader.fieldnames]
            if not any(values):
                continue  # blank line in / after the table
            raw_row = ",".join(values)
            yield raw_row, {norm_name: row.get(actual) for norm_name, actual in inv.items()}

    # -- row ---------------------------------------------------------
    @staticmethod
    def _row_to_signal(
        row: dict[str, str | None],
        raw_row: str,
        expiration: date,
        received_at: datetime,
        ac_map: dict[str, AssetClass],
    ) -> Signal:
        symbol = (row.get(_COL_SYMBOL) or "").strip().upper()
        option_raw = (row.get(_COL_OPTION) or "").strip()

        m = _OPTION_RE.match(option_raw)
        if not m:
            raise UnrecognizedContractNotation(raw_row)
        strike = float(m.group(1))
        if not math.isfinite(strike) or strike <= 0:
            raise ValueError("strike must be positive and finite")
        right = Right.CALL if m.group(2).upper() == "C" else Right.PUT

        try:
            asset_class = ac_map[symbol]
        except KeyError:
            raise UnknownAssetClass(symbol) from None

        return Signal(
            id=f"{symbol}-{option_raw.upper()}-{expiration:%Y%m%d}",
            underlying=symbol,
            right=right,
            strike=strike,
            expiration=expiration,
            quantity=0,  # no size column; FixedQuantitySizer supplies one contract
            asset_class=asset_class,
            received_at=received_at,
            raw=raw_row,
            reference_price=_opt_float(row.get(_COL_LAST_PRICE)),
            signal_delta_tvt=_opt_float(row.get(_COL_TVT_DELTA)),
            signal_delta_schwab=_opt_float(row.get(_COL_SCHWAB_DELTA)),
        )
