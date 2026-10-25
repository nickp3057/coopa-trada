#!/usr/bin/env bash
# Put the login website on the internet with HTTPS:
#   bash packaging/server/setup-https.sh yourname.duckdns.org
# Installs Caddy, which gets and renews a free Let's Encrypt certificate by itself.
# The server's firewall must allow ports 80 and 443 (docs/SERVER_SETUP.md).
set -euo pipefail
SITE="${1:?usage: setup-https.sh yourname.duckdns.org}"
sudo apt-get update -q
sudo apt-get install -y -q caddy
sudo tee /etc/caddy/Caddyfile >/dev/null <<CADDY
$SITE {
	encode gzip
	reverse_proxy 127.0.0.1:8780
}
CADDY
sudo systemctl enable caddy >/dev/null
sudo systemctl restart caddy
mkdir -p "${COOPA_DATA:-$HOME/coopa-data}"
echo "https://$SITE" > "${COOPA_DATA:-$HOME/coopa-data}/site"   # used in sign-up links
echo "Done. In a minute, open https://$SITE (Caddy is fetching the certificate)."
