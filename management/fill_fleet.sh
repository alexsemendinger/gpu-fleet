#!/usr/bin/env bash
# fill_fleet.sh — drive the WHOLE MACHINE_NAME_LIST to a podcheck-passing state.
#
# Every name in config.env gets an instance attached to it, on any GPU type
# under $MAX_PRICE, and the script keeps going until each one answers SSH with
# CUDA available. Built for the pre-cohort end-to-end test of the naming ->
# proxy-port -> ssh-config chain, where the GPU model is irrelevant and only
# "does every name resolve to a working pod" matters.
#
# Each round:
#   1. create anything MISSING (burst_create_pods; it skips names already up)
#   2. wait for public IPs (a proxy config generated before IPs settle
#      silently omits those pods)
#   3. update_proxy, then check all names in parallel (ssh + torch.cuda)
#   4. give failures a grace period to finish booting/pulling the image
#   5. destroy whatever still fails, so the next round redraws a fresh host
# Repeat until every name passes or ROUNDS is exhausted. From round
# $VAST_ROUND on, stragglers are also attempted on Vast.
#
# Usage:  fill_fleet.sh                 # all names in MACHINE_NAME_LIST
# Env:    MAX_PRICE (0.30) ROUNDS (8) IP_WAIT (600) GRACE (600)
#         CREATE_TIMEOUT (600) VAST_ROUND (3) VAST_MAX_PRICE (0.60)
#         GPU_TYPES  comma-separated allow-list passed to burst_create_pods.
#                    Use it to steer away from a GPU type whose only cheap
#                    supply is a host that keeps failing -- a redraw with no
#                    restriction usually lands back on the same machine.
# Log:    ~/fleet-fill.log
set -uo pipefail

export PATH="/usr/local/bin:$HOME/.venvs/global/bin:/usr/sbin:/sbin:/usr/bin:/bin"
# shellcheck disable=SC1091
source "$(dirname "$0")/common.sh"

MAX_PRICE="${MAX_PRICE:-0.30}"
ROUNDS="${ROUNDS:-8}"
IP_WAIT="${IP_WAIT:-600}"
GRACE="${GRACE:-600}"
CREATE_TIMEOUT="${CREATE_TIMEOUT:-600}"
VAST_ROUND="${VAST_ROUND:-3}"
VAST_MAX_PRICE="${VAST_MAX_PRICE:-0.60}"
GPU_TYPES="${GPU_TYPES:-}"

LOG="${LOG:-$HOME/fleet-fill.log}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say() { echo "[$(date -u '+%FT%TZ')] $*" | tee -a "$LOG"; }

mapfile -t NAMES < <(cd "$MGMT" && "$PY" - <<'PY'
import ast, os
from mydotenv import load_env
load_env()
print("\n".join(ast.literal_eval(os.environ["MACHINE_NAME_LIST"])))
PY
)
TOTAL=${#NAMES[@]}
[ "$TOTAL" -gt 0 ] || { say "could not read MACHINE_NAME_LIST"; exit 1; }

status()  { (cd "$MGMT" && "$PY" fleet_status.py "$@"); }

# All names checked concurrently: one SSH round-trip each, proving both
# connectivity through the proxy and CUDA via POD_PYTHON.
check_all() {
    local n
    rm -f "$WORK/pass" "$WORK/fail" "$WORK/wedged"
    touch "$WORK/pass" "$WORK/fail" "$WORK/wedged"
    for n in "${NAMES[@]}"; do
        (
            # One round-trip: SSH reachability, CUDA availability, and -- if
            # CUDA is down -- whether /dev/nvidia-uvm is readable. A pod whose
            # nvidia-smi works but whose UVM device returns EIO is on a host
            # with a wedged nvidia-uvm module: terminal, not slow, and not
            # fixable from inside the container. Distinguishing that from
            # "still pulling the image" is the difference between redrawing
            # immediately and burning the whole grace window on a dead pod.
            out=$(ssh -o ConnectTimeout=8 -o BatchMode=yes "$PREFIX-$n" \
                'echo SSH_OK
                 if '"$POD_PYTHON"' -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
                     echo CUDA_OK
                 elif [ -e /dev/nvidia-uvm ] && ! dd if=/dev/nvidia-uvm of=/dev/null bs=1 count=1 >/dev/null 2>&1; then
                     echo UVM_WEDGED
                 fi' 2>&1)
            if grep -qx SSH_OK <<<"$out" && grep -qx CUDA_OK <<<"$out"; then
                echo "$n" >> "$WORK/pass"
            else
                echo "$n" >> "$WORK/fail"
                grep -qx UVM_WEDGED <<<"$out" && echo "$n" >> "$WORK/wedged"
            fi
        ) &
    done
    wait
    sort -o "$WORK/pass" "$WORK/pass"; sort -o "$WORK/fail" "$WORK/fail"
    sort -o "$WORK/wedged" "$WORK/wedged"
}

# Every current failure is a wedged host -> no point waiting out the grace.
all_failures_terminal() {
    [ -s "$WORK/fail" ] && [ -z "$(comm -23 "$WORK/fail" "$WORK/wedged")" ]
}

hourly() { (cd "$MGMT" && "$PY" list_pods.py 2>/dev/null | grep -oE 'Current hourly spend: \$[0-9.]+' | head -1); }

say "=== fill_fleet: driving $TOTAL names to podcheck-pass (max_price=$MAX_PRICE) ==="

for round in $(seq 1 "$ROUNDS"); do
    say "--- round $round/$ROUNDS ---"

    # 1. create anything with no instance attached
    missing=$(status --missing)
    if [ -n "$missing" ]; then
        say "creating $(wc -w <<<"$missing") missing: $missing"
        # shellcheck disable=SC2086
        gpu_args=()
        [ -n "$GPU_TYPES" ] && gpu_args=(--gpu-types "$GPU_TYPES")
        timeout $((CREATE_TIMEOUT + 120)) "$PY" "$MGMT/burst_create_pods.py" $missing \
            --max-price "$MAX_PRICE" --timeout "$CREATE_TIMEOUT" \
            "${gpu_args[@]+"${gpu_args[@]}"}" >>"$LOG" 2>&1
        say "burst_create_pods rc=$?"

        still=$(status --missing)
        if [ -n "$still" ] && [ "$round" -ge "$VAST_ROUND" ]; then
            say "RunPod short by $(wc -w <<<"$still") — trying Vast: $still"
            # shellcheck disable=SC2086
            timeout 900 "$PY" "$MGMT/create_vast_pods.py" $still \
                --max-price "$VAST_MAX_PRICE" >>"$LOG" 2>&1 </dev/null
            say "create_vast_pods rc=$?"
        fi
    else
        say "every name already has an instance"
    fi

    # 2. wait for public IPs before regenerating the proxy config
    waited=0
    while [ "$waited" -lt "$IP_WAIT" ]; do
        noip=$(status --no-ip)
        [ -z "$noip" ] && break
        say "waiting on IPs (${waited}s): $(wc -w <<<"$noip") pending"
        sleep 30
        waited=$((waited + 30))
    done

    # 3. proxy, then check everything
    say "update_proxy"
    "$REPO/proxy/update.sh" >>"$LOG" 2>&1
    say "update_proxy rc=$?  |  $(hourly)"

    check_all
    say "check: $(wc -l < "$WORK/pass")/$TOTAL passing"

    # 4. grace period — a fresh pod pulling the 15 GB image is slow, not broken
    waited=0
    while [ -s "$WORK/fail" ] && [ "$waited" -lt "$GRACE" ]; do
        if all_failures_terminal; then
            say "  all $(wc -l < "$WORK/fail") failures are wedged-host (nvidia-uvm EIO) — skipping the rest of the grace window"
            break
        fi
        say "  grace ${waited}s, still failing: $(tr '\n' ' ' < "$WORK/fail")$([ -s "$WORK/wedged" ] && echo " [wedged: $(tr '\n' ' ' < "$WORK/wedged")]")"
        sleep 60
        waited=$((waited + 60))
        "$REPO/proxy/update.sh" >>"$LOG" 2>&1   # pick up late IPs
        check_all
    done

    if [ ! -s "$WORK/fail" ]; then
        say "=== ALL $TOTAL NAMES PASS (ssh + cuda) ==="
        say "$(hourly)"
        (cd "$MGMT" && "$PY" fleet_status.py) | tee -a "$LOG"
        exit 0
    fi

    # 5. destroy the stragglers so the next round redraws a different host
    bad=$(tr '\n' ' ' < "$WORK/fail")
    say "round $round leaves $(wc -w <<<"$bad") failing: $bad — destroying to redraw"
    say "failure hosts (a repeated IP = one bad machine, not N bad pods):"
    status | awk 'NR==FNR{f[$1];next} $1 in f && $3!="-"{split($3,a,":"); print "    "$1" "a[1]" "$5" "$6}' \
        "$WORK/fail" - | tee -a "$LOG"
    # shellcheck disable=SC2086
    inc=""; for n in $bad; do inc="$inc $PREFIX-$n"; done
    # shellcheck disable=SC2086
    printf 'y\ny\n' | timeout 420 "$PY" "$MGMT/kill_pods.py" --include $inc >>"$LOG" 2>&1
    # shellcheck disable=SC2086
    printf 'y\n' | timeout 300 "$PY" "$MGMT/kill_vast_pods.py" --include $inc >>"$LOG" 2>&1
done

say "=== INCOMPLETE after $ROUNDS rounds: $(wc -l < "$WORK/fail") still failing: $(tr '\n' ' ' < "$WORK/fail") ==="
(cd "$MGMT" && "$PY" fleet_status.py) | tee -a "$LOG"
exit 1
