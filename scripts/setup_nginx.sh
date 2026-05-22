#!/usr/bin/env bash
set -euo pipefail

DOMAIN="data.powerofvolatility.com"
EMAIL="${CERTBOT_EMAIL:-}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "$EMAIL" ]]; then
  echo "Usage: CERTBOT_EMAIL=you@example.com bash scripts/setup_nginx.sh" >&2
  exit 1
fi

# Install nginx and certbot
sudo apt-get update -q
sudo apt-get install -y nginx certbot python3-certbot-nginx

# Deploy HTTP-only config (no SSL refs yet — certbot adds those)
sudo cp "$ROOT_DIR/config/nginx/$DOMAIN" "/etc/nginx/sites-available/$DOMAIN"
sudo ln -sf "/etc/nginx/sites-available/$DOMAIN" "/etc/nginx/sites-enabled/$DOMAIN"
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx

# Obtain SSL cert and let certbot patch the nginx config for HTTPS + redirect
sudo certbot --nginx \
  --non-interactive \
  --agree-tos \
  --email "$EMAIL" \
  --domains "$DOMAIN" \
  --redirect

sudo systemctl reload nginx

echo ""
echo "Done. https://$DOMAIN is live."
echo "Auto-renewal: systemctl status certbot.timer"
