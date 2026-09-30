#!/usr/bin/env bash
# One-time proxy setup: install nginx, install this repo's nginx.conf (which
# adds a `stream {}` block including /etc/nginx/streams-enabled/*.conf), and
# point that include at ~/proxy.conf — the file update_proxy regenerates.
set -euo pipefail
cd "$(dirname "$0")"

sudo apt-get update
sudo apt-get install -y nginx-full git

sudo cp ./nginx.conf /etc/nginx/nginx.conf
sudo mkdir -p /etc/nginx/streams-enabled
touch "$HOME/proxy.conf"
# The link must point FROM /etc/nginx TO ~/proxy.conf: update.sh replaces
# ~/proxy.conf with a fresh file each run, which would break a link the other
# way round.
sudo ln -sfn "$HOME/proxy.conf" /etc/nginx/streams-enabled/proxy.conf
sudo nginx -t && sudo systemctl reload-or-restart nginx
echo "nginx ready. Next: update_proxy (after creating pods)."
