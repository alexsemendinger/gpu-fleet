#!/usr/bin/env bash
# Regenerate the nginx stream config from live pod IPs, then reload nginx.
# Uses reload (not restart) so participants' active SSH/VSCode connections
# survive a midday proxy update; if the new config fails `nginx -t`, the
# previous config is restored and nginx keeps running on it.
set -u

CONF="$HOME/proxy.conf"   # symlinked from /etc/nginx/streams-enabled/proxy.conf
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PY:-$HOME/.venvs/global/bin/python}"
[ -x "$PY" ] || PY=python3

# nginx and systemctl live in sbin, which is NOT on the default PATH for cron
# or for any caller that sets a minimal PATH. Resolve them by absolute path so
# a missing /usr/sbin can't silently turn the `nginx -t` gate below into a
# "command not found" failure -- which looks identical to a bad config and
# skips the reload, leaving nginx serving stale pod IPs.
NGINX=""
for cand in /usr/sbin/nginx /sbin/nginx "$(command -v nginx 2>/dev/null)"; do
    [ -n "$cand" ] && [ -x "$cand" ] && { NGINX="$cand"; break; }
done
if [ -z "$NGINX" ]; then
    echo "update_proxy: nginx binary not found; nginx left untouched" >&2
    exit 1
fi
SYSTEMCTL="$(command -v systemctl 2>/dev/null || echo /usr/bin/systemctl)"

if ! "$PY" "$HERE/nginx_pods.py" > "$CONF.new" || [ ! -s "$CONF.new" ]; then
    echo "update_proxy: config generation failed; nginx left untouched" >&2
    rm -f "$CONF.new"
    exit 1
fi

[ -f "$CONF" ] && cp -f "$CONF" "$CONF.bak"
mv "$CONF.new" "$CONF"

if ! "$NGINX" -t; then
    echo "update_proxy: new config failed nginx -t; restoring previous config" >&2
    [ -f "$CONF.bak" ] && mv "$CONF.bak" "$CONF"
    exit 1
fi

"$SYSTEMCTL" reload-or-restart nginx
echo "update_proxy: nginx reloaded ($(grep -c '^upstream' "$CONF") pods routed)"
