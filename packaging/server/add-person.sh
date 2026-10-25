#!/usr/bin/env bash
# Give one person their own bot and login:  bash packaging/server/add-person.sh cooper 8771
# Creates ~/coopa-data/NAME (its own .env, settings, trade log, Schwab login),
# starts coopa-trada@NAME, and adds NAME to the login website (password + 2FA).
set -euo pipefail
NAME="${1:?usage: add-person.sh NAME PORT}"
PORT="${2:?usage: add-person.sh NAME PORT  (e.g. 8771 for the first person, 8772 for the next)}"
DIR="$(cd "$(dirname "$0")/../.." && pwd)"
DATA="${COOPA_DATA:-$HOME/coopa-data}"
HOME_DIR="$DATA/$NAME"
[[ "$NAME" =~ ^[a-z][a-z0-9_]*$ ]] || { echo "Use a lower-case name, e.g. cooper" >&2; exit 1; }
[[ "$PORT" =~ ^87[0-9][0-9]$ && "$PORT" != 8780 ]] || { echo "Use a port from 8771 to 8799 (not 8780)" >&2; exit 1; }
if grep -rqs "^COOPA_TRADA_PORT=$PORT$" "$DATA"/*/.env; then
  echo "Port $PORT is already used by someone else" >&2; exit 1
fi

mkdir -p "$HOME_DIR/config"
chmod 700 "$HOME_DIR"
[ -f "$HOME_DIR/config/rules.yaml" ] || cp "$DIR/config/rules.example.yaml" "$HOME_DIR/config/rules.yaml"
if [ ! -f "$HOME_DIR/.env" ]; then
  { cat "$DIR/.env.example"; echo; echo "# This person's bot listens here (set by add-person.sh)."; echo "COOPA_TRADA_PORT=$PORT"; } \
    > "$HOME_DIR/.env"
  chmod 600 "$HOME_DIR/.env"
fi
sudo systemctl enable --now "coopa-trada@$NAME" >/dev/null
"$DIR/.venv/bin/python" -m coopa_trada.gateway --config "$DATA/gateway.yaml" add-user "$NAME" --port "$PORT"
sudo systemctl enable coopa-gateway >/dev/null
sudo systemctl restart coopa-gateway
echo
echo "$NAME is set up. Their settings: nano $HOME_DIR/.env  then: sudo systemctl restart coopa-trada@$NAME"
