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
    for n in "$@"; do
        n="${n#"$PREFIX"-}"
        if ! printf '%s\n' "${MACHINE_NAME_LIST[@]}" | grep -qxF -- "$n"; then
            echo "$n is not in MACHINE_NAME_LIST (the proxy only routes listed names)."; exit 1
        fi
        names+=("$n")
    done
else
    fs=$(cd "$MGMT" && "$PY" fleet_status.py 2>&1) || { echo "Could not list pods: $(tail -1 <<<"$fs")"; exit 1; }
    mapfile -t names < <(awk '$1 !~ /^#/ && $2 != "MISSING" {print $1}' <<<"$fs")
    [ "${#names[@]}" -gt 0 ] || { echo "No pods found. Create some with create_pods first."; exit 1; }
fi
echo "Readying ${#names[@]} pod(s): ${names[*]}$( [ "$WITH_KEYS" = 1 ] && echo '  (with deploy_keys)')"

# update_proxy rewrites one shared config file; never run two at once.
proxy() { flock "${TMPDIR:-/tmp}/update_proxy.lock" update_proxy >/dev/null 2>&1; }

results="$(mktemp -d)"
trap 'rm -rf "$results"' EXIT
for p in "${names[@]}"; do
  (
    ok="" missing=0
    for _ in $(seq 1 60); do                       # up to 15 min for a public IP
      st=$(status | awk -v n="$p" '$1 == n {print $2}')
      if [ "$st" = HAS_IP ]; then ok=1; break; fi
      if [ "$st" = MISSING ]; then missing=$((missing + 1)); else missing=0; fi
      if [ "$missing" -ge 3 ]; then
        echo "[$p] FAILED no pod named $PREFIX-$p exists (create it first: create_pods $p)"; touch "$results/fail.$p"; exit 1
      fi
      sleep 15
    done
    [ -n "$ok" ] || { echo "[$p] FAILED no IP after 15 min — likely a bad host: destroy_pods $p && create_pods $p (if it repeats, add --gpu-types with a different card to land elsewhere)"; touch "$results/fail.$p"; exit 1; }
    proxy
    ok="" last=""
    for _ in $(seq 1 40); do                       # up to 10 min for sshd + CUDA
      last=$(check_pods "$p" 2>/dev/null)
      if grep -q "cuda: yes" <<<"$last"; then ok=1; break; fi
      grep -q "cuda: NO" <<<"$last" && break         # a GPU that doesn't work won't start working
      grep -q "cuda: ?" <<<"$last" && break          # SSH works but no torch: POD_PYTHON is wrong
      sleep 15; proxy
    done
    if [ -z "$ok" ]; then
      if grep -q "cuda: ?" <<<"$last"; then hint="set POD_PYTHON in config.env to the image's Python"
      else hint="likely a bad host: destroy_pods $p && create_pods $p (if it repeats, add --gpu-types with a different card)"; fi
      echo "[$p] FAILED check: $(tail -1 <<<"$last" | tr -s ' ') — $hint"; touch "$results/fail.$p"; exit 1
    fi
    if [ "$WITH_KEYS" = 1 ]; then
      if ! dk=$(deploy_keys --pod "$PREFIX-$p" 2>&1); then
        why=$(grep -E "FAIL|ERROR|rror" <<<"$dk" | tail -1 | sed 's/^ *//')
        echo "[$p] FAILED deploy_keys: ${why:-$(tail -1 <<<"$dk")}"; touch "$results/fail.$p"; exit 1
      fi
    fi
    echo "[$p] READY"
  ) &
done
wait
failed=$(find "$results" -name 'fail.*' | wc -l)
echo "--- $(( ${#names[@]} - failed ))/${#names[@]} ready$( [ "$failed" -gt 0 ] && echo ", $failed failed")"
[ "$failed" -eq 0 ]
