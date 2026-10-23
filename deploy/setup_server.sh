#!/usr/bin/env bash
# One-time server setup for Coopa Trada on Ubuntu 24.04 (AWS EC2). Safe to re-run.
#
#   sudo bash /opt/coopa-trada/deploy/setup_server.sh
#
# Expects the code already in /opt/coopa-trada (see docs/AWS_SETUP.md, step 4).
# It never creates or edits .env and never touches the Schwab login: you copy
# those yourself. It does NOT start trading: the service starts the dashboard,
# and runs are practice (DRY-RUN) unless you set the live opt-ins yourself.
set -euo pipefail

APP_DIR=/opt/coopa-trada
APP_USER=coopa
TZ_NAME=America/Denver

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo: sudo bash $0" >&2
  exit 1
fi
if [[ ! -f "$APP_DIR/pyproject.toml" ]]; then
  echo "The code is not in $APP_DIR yet. Copy it there first (docs/AWS_SETUP.md, step 4)." >&2
  exit 1
fi

echo "== 1. Time zone and clock =="
timedatectl set-timezone "$TZ_NAME"
timedatectl set-ntp true || true   # EC2 syncs to the Amazon Time Sync Service
timedatectl | sed 's/^/   /'

echo "== 2. System packages =="
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3 python3-venv python3-pip

echo "== 3. Swap (pandas needs headroom on a 1 GB machine) =="
mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
if [[ $mem_kb -lt 1600000 && ! -f /swapfile ]]; then
  fallocate -l 1G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "   added a 1 GB swap file"
else
  echo "   not needed (or already there)"
fi

echo "== 4. App user =="
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
  echo "   created user $APP_USER"
fi

echo "== 5. Python environment =="
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi
# PyInstaller only builds the Mac app; it is not needed on a server.
grep -v '^pyinstaller' "$APP_DIR/requirements.txt" > /tmp/coopa-requirements.txt
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r /tmp/coopa-requirements.txt
"$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR[web]"
rm -f /tmp/coopa-requirements.txt

echo "== 6. Rules file =="
if [[ ! -f "$APP_DIR/config/rules.yaml" ]]; then
  cp "$APP_DIR/config/rules.example.yaml" "$APP_DIR/config/rules.yaml"
  echo "   copied config/rules.example.yaml to config/rules.yaml (every setting unconfirmed)"
fi

echo "== 7. File permissions =="
bash "$APP_DIR/deploy/lock_down.sh"

echo "== 8. Service (restart on crash and on reboot) =="
cp "$APP_DIR/deploy/coopa-trada.service" /etc/systemd/system/coopa-trada.service
systemctl daemon-reload
systemctl enable coopa-trada >/dev/null
if [[ -f "$APP_DIR/.env" ]]; then
  systemctl restart coopa-trada
  sleep 3
  systemctl --no-pager --lines=5 status coopa-trada || true
else
  echo "   No .env yet: create $APP_DIR/.env (docs/AWS_SETUP.md, step 6), then run:"
  echo "   sudo bash $APP_DIR/deploy/lock_down.sh && sudo systemctl restart coopa-trada"
fi

echo
echo "Done. Next: Tailscale (docs/AWS_SETUP.md, step 8)."
