#!/usr/bin/env bash
# run_cmd.sh <command...> — run a command on every pod that exists, one pod
# after another, in a login shell. Arguments are passed through exactly, e.g.
#   management/run_cmd.sh nvidia-smi -L
#   management/run_cmd.sh 'cd /root && ls | head'
# Prints a header per pod; exits nonzero if the command failed on any pod.

# shellcheck disable=SC1091
source "$(dirname "$0")/common.sh"

if [ $# -lt 1 ]; then
  echo "Usage: $(basename "$0") <command...>"
  echo "Example: $(basename "$0") nvidia-smi -L"
  exit 1
fi

# One string for the remote login shell, each argument quoted so it arrives
# intact (a single quoted argument like 'cd x && ls' stays one shell command).
if [ $# -eq 1 ]; then CMD="$1"; else printf -v CMD '%q ' "$@"; fi

fs=$(cd "$MGMT" && "$PY" fleet_status.py 2>&1) || { echo "Could not list pods: $(tail -1 <<<"$fs")"; exit 1; }
mapfile -t names < <(awk '$1 !~ /^#/ && $2 == "HAS_IP" {print $1}' <<<"$fs")
[ "${#names[@]}" -gt 0 ] || { echo "No reachable pods."; exit 1; }

failed=0
for name in "${names[@]}"; do
  echo "=== $PREFIX-$name"
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$PREFIX-$name" "bash -lc $(printf '%q' "$CMD")" </dev/null || failed=$((failed + 1))
done
[ "$failed" -eq 0 ] || { echo "--- failed on $failed pod(s)"; exit 1; }
