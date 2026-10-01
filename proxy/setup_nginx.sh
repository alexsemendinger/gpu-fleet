#!/usr/bin/env bash
# One-time proxy setup, run as root: install nginx, install this repo's
# nginx.conf (which adds a `stream {}` block including
# /etc/nginx/streams-enabled/*.conf), and point that include at ~/proxy.conf —
# the file update_proxy regenerates.
set -euo pipefail
cd "$(dirname "$0")"
[ "$(id -u)" -eq 0 ] || { echo "Run this as root (e.g. sudo -i first)." >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y nginx-full git

cp ./nginx.conf /etc/nginx/nginx.conf
mkdir -p /etc/nginx/streams-enabled
touch "$HOME/proxy.conf"
# The link must point FROM /etc/nginx TO ~/proxy.conf: update.sh replaces
# ~/proxy.conf with a fresh file each run, which would break a link the other
# way round.
ln -sfn "$HOME/proxy.conf" /etc/nginx/streams-enabled/proxy.conf
nginx -t
if [ -d /run/systemd/system ]; then
    systemctl enable --now nginx
    systemctl reload-or-restart nginx
elif [ -s /run/nginx.pid ] && kill -0 "$(cat /run/nginx.pid)" 2>/dev/null; then
    nginx -s reload
else
    nginx    # no systemd (container/WSL): start it directly
fi
echo "nginx ready. Next: create pods, then ready_pods (which runs update_proxy)."
