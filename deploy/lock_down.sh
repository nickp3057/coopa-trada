#!/usr/bin/env bash
# Make Coopa Trada's secrets readable by the app user only. Safe to re-run;
# run it again after copying .env or schwab_token.json onto the server.
#
#   sudo bash /opt/coopa-trada/deploy/lock_down.sh
#
# Result:
#   /opt/coopa-trada            drwx------  coopa  (nobody else can even list it)
#   .env, schwab_token.json     -rw-------  coopa
#   trada.db*, imports/, logs/  owner only
set -euo pipefail

APP_DIR=/opt/coopa-trada
APP_USER=coopa

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo: sudo bash $0" >&2
  exit 1
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 700 "$APP_DIR"
mkdir -p "$APP_DIR/locks"
chown "$APP_USER:$APP_USER" "$APP_DIR/locks"
chmod 700 "$APP_DIR/locks"

for f in .env schwab_token.json trada.db trada.db-wal trada.db-shm trada.db-journal; do
  if [[ -e "$APP_DIR/$f" ]]; then
    chmod 600 "$APP_DIR/$f"
  fi
done
for d in imports logs config; do
  if [[ -d "$APP_DIR/$d" ]]; then
    chmod -R go-rwx "$APP_DIR/$d"
  fi
done

echo "   permissions:"
ls -la "$APP_DIR" | grep -E ' (\.env|schwab_token\.json|trada\.db)$' | sed 's/^/   /' || true
echo "   (folder: $(stat -c '%A %U' "$APP_DIR"))"
