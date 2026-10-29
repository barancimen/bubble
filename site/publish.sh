#!/bin/sh
# Uploads the latest dashboard.html to the bubble-watch Worker (gestaltschaft.com/bubble).
# Run by cron after each bubble_watch.py run; the token stays in ~/.cloudflare-token.
set -eu
PATH="$HOME/.local/share/pi-node/current/bin:$PATH"  # cron's PATH has no node
cd "$(dirname "$0")"
mkdir -p public
cp ../dashboard.html public/dashboard.html
CLOUDFLARE_API_TOKEN="$(cat "$HOME/.cloudflare-token")" WRANGLER_SEND_METRICS=false \
  ./node_modules/.bin/wrangler deploy
