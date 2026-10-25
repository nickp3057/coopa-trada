#!/usr/bin/env bash
# Install or update Coopa Trada on Ubuntu/Debian. Run from the code folder as the
# user that will own it (not root):   bash packaging/server/install.sh
# Safe to run again after `git pull`: it never touches anyone's data folder.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/../.." && pwd)"
DATA="${COOPA_DATA:-$HOME/coopa-data}"
USER_NAME="$(id -un)"
cd "$DIR"

if ! command -v python3 >/dev/null; then
  echo "Install Python first: sudo apt update && sudo apt install -y python3 python3-venv git" >&2
  exit 1
fi
python3 -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt -e '.[web]'
mkdir -p "$DATA"
chmod 700 "$DATA"

for unit in coopa-trada@.service coopa-gateway.service; do
  sed -e "s#@USER@#${USER_NAME}#g" -e "s#@DIR@#${DIR}#g" -e "s#@DATA@#${DATA}#g" "packaging/server/$unit" \
    | sudo tee "/etc/systemd/system/$unit" >/dev/null
done
sudo systemctl daemon-reload
# Restart every person's bot and the login website so they run the new code.
for folder in "$DATA"/*/; do
  [ -f "${folder}.env" ] && sudo systemctl restart "coopa-trada@$(basename "$folder")"
done
[ -f "$DATA/gateway.yaml" ] && sudo systemctl restart coopa-gateway
echo "Installed. Add a person with: bash packaging/server/add-person.sh NAME PORT"
