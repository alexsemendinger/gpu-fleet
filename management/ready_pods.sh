#!/usr/bin/env bash
# ready_pods [name ...] — take freshly created pods to "ready", each pod
# independently and in parallel so one slow pod never holds up the rest:
#   wait for an IP -> update_proxy -> check_pods (SSH + CUDA) -> deploy_keys
# No names = every name in MACHINE_NAME_LIST that currently has a pod.
# deploy_keys runs only if keys/ holds key CSVs (set WITH_KEYS=0 to skip it).
# Prints "[name] READY" or "[name] FAILED <step>" per pod, then a summary.
set -u
# shellcheck disable=SC1091
source "$(dirname "$0")/common.sh"
export PATH="$REPO/bin:$PATH"

if [ "${WITH_KEYS:-1}" = 1 ] && ls "$REPO"/keys/*_api_keys.csv >/dev/null 2>&1; then
    WITH_KEYS=1
else
    WITH_KEYS=0
fi

status() { (cd "$MGMT" && "$PY" fleet_status.py 2>/dev/null); }

if [ $# -gt 0 ]; then
    names=()
    for n in "$@"; do names+=("${n#"$PREFIX"-}"); done
else
    mapfile -t names < <(status | awk '$1 !~ /^#/ && $2 != "MISSING" {print $1}')
    [ "${#names[@]}" -gt 0 ] || { echo "No pods found. Create some with create_pods first."; exit 1; }
fi
echo "Readying ${#names[@]} pod(s): ${names[*]}$( [ "$WITH_KEYS" = 1 ] && echo '  (with deploy_keys)')"

# update_proxy rewrites one shared config file; never run two at once.
proxy() { flock "${TMPDIR:-/tmp}/update_proxy.lock" update_proxy >/dev/null 2>&1; }

results="$(mktemp -d)"
trap 'rm -rf "$results"' EXIT
for p in "${names[@]}"; do
  (
    ok=""
    for _ in $(seq 1 60); do                       # up to 15 min for a public IP
      if status | awk -v n="$p" '$1 == n && $2 == "HAS_IP" {f=1} END {exit !f}'; then ok=1; break; fi
      sleep 15
    done
    [ -n "$ok" ] || { echo "[$p] FAILED no IP after 15 min (bad host? destroy_pods $p && create_pods $p)"; touch "$results/fail.$p"; exit 1; }
    proxy
    ok="" last=""
    for _ in $(seq 1 40); do                       # up to 10 min for sshd + CUDA
      last=$(check_pods "$p" 2>/dev/null)
      if grep -q "cuda: yes" <<<"$last"; then ok=1; break; fi
      grep -q "cuda: NO" <<<"$last" && break         # a GPU that doesn't work won't start working
      sleep 15; proxy
    done
    [ -n "$ok" ] || { echo "[$p] FAILED check: $(tail -1 <<<"$last" | tr -s ' ') — destroy_pods $p && create_pods $p"; touch "$results/fail.$p"; exit 1; }
    if [ "$WITH_KEYS" = 1 ]; then
      deploy_keys --pod "$PREFIX-$p" >/dev/null 2>&1 || { echo "[$p] FAILED deploy_keys"; touch "$results/fail.$p"; exit 1; }
    fi
    echo "[$p] READY"
  ) &
done
wait
failed=$(find "$results" -name 'fail.*' | wc -l)
echo "--- $(( ${#names[@]} - failed ))/${#names[@]} ready$( [ "$failed" -gt 0 ] && echo ", $failed failed")"
[ "$failed" -eq 0 ]
