"""Local image recognition and strict, reviewable signal imports. No broker access."""
from __future__ import annotations

import csv
import io
import json
import os
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
                   "built-in text recognition). On this computer, upload the CSV file instead.")
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


def image_reader_status() -> dict:
    available = image_reader_command() is not None
    reason = None if available else (NOT_MAC_MESSAGE if platform.system() != "Darwin" else NO_READER_MESSAGE)
    return {"available": available, "reason": reason}


def recognize_image(path: Path) -> str:
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


def preview_csv(text: str, trading_date: str, expiration_date: str) -> dict:
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
        "csv": canonical, "date": str(day), "expiration": str(expiration),
        "rows": [{"symbol": s.underlying, "option": f"{s.strike:g}{s.right.value[0]}",
                  "last_price": s.reference_price, "tvt_delta": s.signal_delta_tvt,
                  "schwab_delta": s.signal_delta_schwab} for s in signals],
    }
