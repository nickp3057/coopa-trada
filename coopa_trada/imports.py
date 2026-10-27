"""Local image recognition and strict, reviewable signal imports. No broker access."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

from .models import CsvSignalSource

COLUMNS = ["SYMBOL", "OPTION", "LAST PRICE", "TVT-DELTA", "SCHWAB DELTA"]


NOT_MAC_MESSAGE = ("Reading a picture of the picks table only works on a Mac (it uses Apple's "
                   "built-in text recognition) or with the Claude picture reader, which needs "
                   "ANTHROPIC_API_KEY in the .env file. On this computer, upload the CSV file instead.")
CLAUDE_KEY_MESSAGE = ("The Claude picture reader is selected but ANTHROPIC_API_KEY is not in the .env "
                      "file. Add it, or upload the CSV file instead.")
# COOPA_IMAGE_READER in .env: auto (default), claude, or apple.
READER_CHOICES = ("auto", "claude", "apple")
NO_READER_MESSAGE = ("The picture reader is missing from this copy of Coopa Trada. Upload the CSV "
                     "file instead, or reinstall the app.")


def _compiled_helper() -> Path | None:
    """The helper compiled at build time (packaging/build_macos.sh)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    for candidate in (base / "ocr" / "coopa-recognize", Path(__file__).with_name("ocr") / "coopa-recognize"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def image_reader_command() -> list[str] | None:
    """How to run Apple Vision here, or None when this computer cannot."""
    if platform.system() != "Darwin":
        return None
    helper = _compiled_helper()
    if helper is not None:
        return [str(helper)]
    if shutil.which("swift"):  # development: interpret the source (needs Xcode tools)
        script = Path(__file__).with_name("ocr") / "recognize.swift"
        return ["swift", "-module-cache-path", str(Path(tempfile.gettempdir()) / "coopa-swift-cache"), str(script)]
    return None


def image_reader() -> tuple[str | None, str | None]:
    """(reader, reason): which picture reader to use here, or (None, why not).

    ``COOPA_IMAGE_READER=auto`` (default): Apple Vision on a Mac that has it,
    otherwise the Claude reader when ANTHROPIC_API_KEY is set. ``claude`` or
    ``apple`` force one (Apple only exists on a Mac).
    """
    from .vision import claude_configured
    choice = (os.environ.get("COOPA_IMAGE_READER") or "auto").strip().lower()
    if choice not in READER_CHOICES:
        choice = "auto"
    apple = image_reader_command() is not None
    if choice == "apple" or (choice == "auto" and apple):
        if apple:
            return "apple", None
        return None, NOT_MAC_MESSAGE if platform.system() != "Darwin" else NO_READER_MESSAGE
    if claude_configured():
        return "claude", None
    if choice == "claude":
        return None, CLAUDE_KEY_MESSAGE
    return None, NOT_MAC_MESSAGE if platform.system() != "Darwin" else NO_READER_MESSAGE


def image_reader_status() -> dict:
    reader, reason = image_reader()
    return {"available": reader is not None, "reason": reason, "reader": reader}


def read_image(path: Path):
    """Read a picks picture with the selected reader. Returns a vision.ReadResult."""
    from .vision import ReadResult, read_picks_image
    reader, reason = image_reader()
    if reader is None:
        raise ValueError(reason)
    if reader == "claude":
        return read_picks_image(path)
    text = _recognize_apple(path)
    return ReadResult(text, None, None, None)


def recognize_image(path: Path) -> str:
    return read_image(path).csv


def _recognize_apple(path: Path) -> str:
    command = image_reader_command()
    if command is None:
        raise ValueError(image_reader_status()["reason"])
    try:
        result = subprocess.run(
            [*command, str(path)],
            capture_output=True, text=True, timeout=90, check=True,
        )
        return cells_to_csv(json.loads(result.stdout))
    except (subprocess.SubprocessError, ValueError) as exc:
        raise ValueError("Could not read the image. Try a clearer image or upload a CSV.") from exc


_TITLE_FOR = re.compile(r"\bfor\s+(\d{1,2})/(\d{1,2})\b", re.IGNORECASE)
_TITLE_EXP = re.compile(r"(\d{1,2})/(\d{1,2})\s*expiration", re.IGNORECASE)
_MONTH_DAY = re.compile(r"^\s*(\d{1,2})/(\d{1,2})\s*$")


def closest_date(month: int, day: int, today: date) -> date:
    """The ``month``/``day`` nearest to ``today`` (last year, this year or next)."""
    options = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            options.append(date(year, month, day))
        except ValueError:
            continue
    if not options:
        raise ValueError(f"{month}/{day} is not a date")
    return min(options, key=lambda d: abs((d - today).days))


def title_dates(text: str, today: date, trade_md: str | None = None,
                expiration_md: str | None = None) -> tuple[date | None, date | None]:
    """The list date and expiration from the picture's title, e.g. "FOMC Picks for 9/2"
    and "9/4 Expiration" (one line or two). Years are the ones nearest ``today``.
    Either may be None when the title doesn't show it; a person checks both."""
    lines = []
    for line in (text or "").splitlines():
        if "SYMBOL" in line.upper() and "OPTION" in line.upper():
            break  # the title is above the table
        lines.append(line)
    head = "\n".join(lines)

    def pick(given, pattern):
        m = _MONTH_DAY.match(given or "") or pattern.search(head)
        if m is None:
            return None
        try:
            return closest_date(int(m.group(1)), int(m.group(2)), today)
        except ValueError:
            return None

    trade, expiration = pick(trade_md, _TITLE_FOR), pick(expiration_md, _TITLE_EXP)
    if trade and expiration and expiration < trade:
        expiration = expiration.replace(year=expiration.year + 1)
    return trade, expiration


def cells_to_csv(cells: list[dict]) -> str:
    """Reconstruct rows using recognition coordinates, preserving unrecognized text for review."""
    groups = []
    for cell in sorted(cells, key=lambda c: c["y"]):
        if groups and abs(cell["y"] - groups[-1][0]["y"]) < 0.018:
            groups[-1].append(cell)
        else:
            groups.append([cell])
    out = io.StringIO()
    writer = csv.writer(out)
    header = None
    for group in groups:
        group.sort(key=lambda c: c["x"])
        texts = [c["text"] for c in group]
        if "SYMBOL" in [t.upper() for t in texts] and len(group) == 5:
            header = [c["x"] for c in group]
            writer.writerow(COLUMNS)
        elif header is None:
            writer.writerow([" ".join(texts)])
        else:
            row = [""] * 5
            for cell in group:
                idx = min(range(5), key=lambda i: abs(header[i] - cell["x"]))
                row[idx] = (row[idx] + " " + cell["text"]).strip()
            writer.writerow(row)
    if header is None:
        raise ValueError("Could not identify the five-column picks table")
    return out.getvalue()


def preview_csv(text: str, trading_date: str, expiration_date: str, *, ticker_lookup=None) -> dict:
    day, expiration = date.fromisoformat(trading_date), date.fromisoformat(expiration_date)
    if expiration < day:
        raise ValueError("Expiration must be on or after the trading date")
    lines = text.lstrip("\ufeff").splitlines()
    start = next((i for i, line in enumerate(lines)
                  if {"SYMBOL", "OPTION"} <= {v.strip().upper() for v in next(csv.reader([line]))}), None)
    if start is None:
        raise ValueError("CSV needs SYMBOL and OPTION column headers")
    table = list(csv.reader(lines[start:]))
    width = len(table[0])
    rows = [r for r in table[1:] if any(c.strip() for c in r)]
    if not rows or len(rows) > 100:
        raise ValueError("Provide between 1 and 100 picks")
    if any(len(r) != width for r in rows):
        raise ValueError("Each pick must have the same number of columns as the header")
    header = [c.strip().upper() for c in table[0]]
    sym_i, opt_i = header.index("SYMBOL"), header.index("OPTION")
    bad = [f"{r[sym_i].strip() or '?'} {r[opt_i].strip() or '?'}" for r in rows
           if not re.fullmatch(r"\d+\.?\d*[CP]", r[opt_i].strip(), re.IGNORECASE)]
    if bad:
        raise ValueError(f"These options don't read right: {', '.join(bad)}. Each one should look like "
                         "43C or 49.5P (strike, then C or P). Fix them in the table.")
    # New tickers are looked up at Schwab (stock or ETF) and remembered.
    from .tickers import learn_tickers
    notes = learn_tickers([r[sym_i] for r in rows], **({} if ticker_lookup is None else {"lookup": ticker_lookup}))
    canonical = f"Trade date: {day}; Expiration: {expiration}\n" + "\n".join(lines[start:]) + "\n"
    with tempfile.TemporaryDirectory(prefix="coopa-preview-") as tmp:
        path = Path(tmp) / "picks.csv"
        path.write_text(canonical, encoding="utf-8")
        signals = CsvSignalSource(path).fetch(day)
    if len(signals) != len(rows):
        raise ValueError("Some rows have an invalid option or unknown ticker. Correct every row before confirming.")
    if len({s.id for s in signals}) != len(signals):
        raise ValueError("Duplicate option picks: remove duplicates before confirming")
    if any(s.reference_price is None or s.reference_price <= 0 for s in signals):
        raise ValueError("Every pick needs a positive LAST PRICE")
    return {
        "notes": notes,
        "csv": canonical, "date": str(day), "expiration": str(expiration),
        "rows": [{"symbol": s.underlying, "option": f"{s.strike:g}{s.right.value[0]}",
                  "last_price": s.reference_price, "tvt_delta": s.signal_delta_tvt,
                  "schwab_delta": s.signal_delta_schwab} for s in signals],
    }
