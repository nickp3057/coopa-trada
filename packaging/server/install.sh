#!/usr/bin/env bash
# Install or update Coopa Trada as a service on Ubuntu/Debian. Run from the repo
# folder as the user that will own it (not root):  bash packaging/server/install.sh
# Safe to run again after `git pull`: it never touches .env, config/rules.yaml,
# trada.db or schwab_token.json.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/../.." && pwd)"
USER_NAME="$(id -un)"
cd "$DIR"

if ! command -v python3 >/dev/null; then
  echo "Install Python first: sudo apt update && sudo apt install -y python3 python3-venv git" >&2
  exit 1
fi
python3 -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt -e '.[web]'

[ -f config/rules.yaml ] || cp config/rules.example.yaml config/rules.yaml
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env from .env.example: fill it in (docs/SERVER_SETUP.md, step 4)."
fi

sed -e "s#@USER@#${USER_NAME}#g" -e "s#@DIR@#${DIR}#g" packaging/server/coopa-trada.service \
  | sudo tee /etc/systemd/system/coopa-trada.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable coopa-trada >/dev/null
sudo systemctl restart coopa-trada
echo "Coopa Trada is running. Status: sudo systemctl status coopa-trada   Logs: journalctl -u coopa-trada -f"
