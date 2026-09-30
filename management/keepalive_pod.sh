#!/usr/bin/env bash
# keepalive_pod.sh <name> — keep ONE long-lived pod alive and CUDA-healthy.
#
# Built for the pre-program "connection test" pod: participants are sent a
# fixed endpoint (proxy host + the port that name owns, e.g. the first name
# in MACHINE_NAME_LIST = <proxy>:16000) and it must answer for the whole window. The proxy port
# is derived from the name's index in MACHINE_NAME_LIST, so it survives
# recreates — only the backend IP changes, which `update_proxy` re-points.
#
# Behaviour (idempotent — safe to run on a schedule):
#   1. Health check: ssh + torch.cuda.is_available(). Healthy -> exit 0, done.
#   2. Unhealthy/missing -> destroy whatever is there, burst-create a
#      replacement (A4000 first, any NVIDIA type under --max-price as
#      fallback), wait for a public IP, update_proxy, then poll the health
#      check once a minute for HEALTH_WINDOW seconds.
#   3. Still failing -> destroy it and try a fresh host, up to ATTEMPTS times.
#
# Usage:
#   keepalive_pod.sh alder              # ensure gpu-alder is up
#   MAX_PRICE=0.30 keepalive_pod.sh alder
#
# Env overrides: MAX_PRICE (0.20) ATTEMPTS (3) HEALTH_WINDOW (300)
#                CREATE_TIMEOUT (420) IP_WAIT (300)
# Log: ~/keepalive-<name>.log
set -uo pipefail

export PATH="/usr/local/bin:$HOME/.venvs/global/bin:/usr/sbin:/sbin:/usr/bin:/bin"
# shellcheck disable=SC1091
source "$(dirname "$0")/common.sh"

NAME="${1:?usage: keepalive_pod.sh <name>}"
NAME="${NAME#"$PREFIX"-}"
FULL="$PREFIX-$NAME"

MAX_PRICE="${MAX_PRICE:-0.20}"
ATTEMPTS="${ATTEMPTS:-3}"
HEALTH_WINDOW="${HEALTH_WINDOW:-300}"   # seconds to give a new pod to boot
CREATE_TIMEOUT="${CREATE_TIMEOUT:-420}" # per-attempt burst_create timeout
IP_WAIT="${IP_WAIT:-300}"               # seconds to wait for a public IP

LOG="${LOG:-$HOME/keepalive-$NAME.log}"
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date -u '+%FT%TZ')] $*" >> "$LOG"; }
say() { echo "$*"; log "$*"; }

# --- health check: one SSH round-trip proving connectivity AND CUDA ---------
healthy() {
    local out
    out=$(ssh -o ConnectTimeout=8 -o BatchMode=yes "$FULL" \
        "echo SSH_OK; $POD_PYTHON -c 'import torch; print(\"CUDA_OK\" if torch.cuda.is_available() else \"CUDA_NO\")' 2>/dev/null" 2>&1)
    if grep -qx SSH_OK <<<"$out" && grep -qx CUDA_OK <<<"$out"; then
        return 0
    fi
    LAST_CHECK="$(tr '\n' ' ' <<<"$out" | tail -c 200)"
    return 1
}

# --- does the pod have a public SSH endpoint yet? --------------------------
has_ip() {
    "$PY" - "$FULL" "$MGMT" <<'PY' 2>/dev/null
import sys, os
sys.path.insert(0, sys.argv[2])
from mydotenv import load_env
load_env()
import runpod_compat as runpod
runpod.api_key = os.environ["RUNPOD_API_KEY"]
want = sys.argv[1]
for p in runpod.get_pods():
    if p.get("name") != want:
        continue
    rt = p.get("runtime") or {}
    for port in rt.get("ports") or []:
        if port.get("privatePort") == 22 and port.get("ip") and port.get("isIpPublic"):
            print(f"{port['ip']}:{port['publicPort']}")
            sys.exit(0)
sys.exit(1)
PY
}

destroy() {
    say "destroying $FULL (if present)"
    printf 'y\ny\n' | timeout 300 "$PY" "$MGMT/kill_pods.py" --include "$FULL" >>"$LOG" 2>&1
}

# ---------------------------------------------------------------------------
say "=== keepalive $FULL (max_price=$MAX_PRICE) ==="

if healthy; then
    say "$FULL healthy (ssh + cuda) — nothing to do"
    exit 0
fi
say "$FULL NOT healthy: ${LAST_CHECK:-no response} — provisioning a replacement"

for attempt in $(seq 1 "$ATTEMPTS"); do
    say "--- attempt $attempt/$ATTEMPTS ---"
    destroy

    say "burst_create_pods $NAME --max-price $MAX_PRICE --timeout $CREATE_TIMEOUT"
    timeout $((CREATE_TIMEOUT + 60)) "$PY" "$MGMT/burst_create_pods.py" "$NAME" \
        --max-price "$MAX_PRICE" --timeout "$CREATE_TIMEOUT" >>"$LOG" 2>&1
    rc=$?
    say "burst_create_pods rc=$rc"

    # Wait for a public IP before touching the proxy (a config generated too
    # early silently omits the pod).
    endpoint=""
    waited=0
    while [ "$waited" -lt "$IP_WAIT" ]; do
        if endpoint=$(has_ip); then break; fi
        endpoint=""
        sleep 15
        waited=$((waited + 15))
    done
    if [ -z "$endpoint" ]; then
        say "no public IP after ${IP_WAIT}s — retrying on a different host"
        continue
    fi
    say "public endpoint: $endpoint (after ${waited}s)"

    say "update_proxy"
    "$REPO/proxy/update.sh" >>"$LOG" 2>&1
    say "update_proxy rc=$?"

    # Poll the health check once a minute until the window closes.
    waited=0
    while [ "$waited" -lt "$HEALTH_WINDOW" ]; do
        if healthy; then
            say "$FULL HEALTHY after ${waited}s — endpoint $endpoint, proxy port live"
            exit 0
        fi
        log "  podcheck ${waited}s: ${LAST_CHECK:-no response}"
        sleep 60
        waited=$((waited + 60))
    done
    say "$FULL still failing after ${HEALTH_WINDOW}s (${LAST_CHECK:-no response}) — killing and retrying"
done

destroy
say "FAILED to bring up $FULL after $ATTEMPTS attempts"
exit 1
