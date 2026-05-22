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

# Copy site config
sudo cp "$ROOT_DIR/config/nginx/$DOMAIN" "/etc/nginx/sites-available/$DOMAIN"
sudo ln -sf "/etc/nginx/sites-available/$DOMAIN" "/etc/nginx/sites-enabled/$DOMAIN"
sudo rm -f /etc/nginx/sites-enabled/default

# Test config before touching anything
sudo nginx -t

# Obtain SSL certificate (also auto-patches the nginx config with SSL paths)
sudo certbot --nginx \
  --non-interactive \
  --agree-tos \
  --email "$EMAIL" \
  --domains "$DOMAIN" \
  --redirect

sudo systemctl reload nginx

echo ""
echo "Done. https://$DOMAIN is live."
echo "Certbot auto-renew is managed by the certbot systemd timer:"
echo "  systemctl status certbot.timer"
