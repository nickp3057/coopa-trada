#!/usr/bin/env bash
# Give one person their own bot and login:
#   bash packaging/server/add-person.sh cooper 8771            (set the password here, now)
#   bash packaging/server/add-person.sh cooper 8771 --no-2fa   (password only, no 6-digit codes)
#   bash packaging/server/add-person.sh cooper 8771 --invite   (send them a sign-up link)
# Creates ~/coopa-data/NAME (its own .env, settings, trade log, Schwab login),
# starts coopa-trada@NAME, and adds NAME to the login website (password, plus 2FA
# unless --no-2fa; with a link, the person chooses).
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
if [ "${3:-}" = "--invite" ]; then
  "$DIR/.venv/bin/python" -m coopa_trada.gateway --config "$DATA/gateway.yaml" invite "$NAME" --port "$PORT"
else
  "$DIR/.venv/bin/python" -m coopa_trada.gateway --config "$DATA/gateway.yaml" add-user "$NAME" --port "$PORT" ${3:+"$3"}
fi
sudo systemctl enable coopa-gateway >/dev/null
sudo systemctl restart coopa-gateway
echo
echo "$NAME is set up. Their settings: nano $HOME_DIR/.env  then: sudo systemctl restart coopa-trada@$NAME"
