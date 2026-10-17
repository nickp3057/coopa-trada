#!/bin/bash
# Build dist/Coopa Trada.app, then verify its live-trading gates.
#   packaging/build_macos.sh            build + verify
#   packaging/build_macos.sh --no-verify
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This script builds the macOS app and must run on a Mac." >&2
  exit 1
fi

mkdir -p build/ocr
swiftc -O -o build/ocr/coopa-recognize coopa_trada/ocr/recognize.swift
"$PY" -m PyInstaller --noconfirm --clean --log-level WARN \
  --distpath dist --workpath build/pyinstaller packaging/coopa_trada.spec

echo "Built: dist/Coopa Trada.app"
if [[ "${1:-}" != "--no-verify" ]]; then
  "$PY" packaging/verify_app.py --app "dist/Coopa Trada.app"
fi
