#!/usr/bin/env bash
# pod_pipeline.sh <name> [name ...] — bring freshly created pods to "ready" one
# pod at a time, in parallel, so one slow pod never holds up the rest:
#   wait for an IP -> update_proxy -> podcheck (SSH + CUDA) -> deploy_keys
# Run it right after create_pods / burst_create_pods. Pass WITH_KEYS=0 to skip
# deploy_keys on days the pods don't need API keys.
set -u
# shellcheck disable=SC1091
source "$(dirname "$0")/common.sh"
WITH_KEYS="${WITH_KEYS:-1}"

for p in "$@"; do
  p="${p#"$PREFIX"-}"
  (
    for _ in $(seq 1 60); do
      row=$(list_pods 2>/dev/null | grep -E "^$p ")
      if [ -n "$row" ] && ! echo "$row" | grep -qE "N/A|loading"; then break; fi
      sleep 15
    done
    update_proxy >/dev/null 2>&1
    ok=""
    for _ in $(seq 1 20); do
      if podcheck "$p" 2>/dev/null | grep -q "cuda: yes"; then ok=1; break; fi
      sleep 15; update_proxy >/dev/null 2>&1
    done
    [ -z "$ok" ] && { echo "[$p] FAILED podcheck"; exit 1; }
    if [ "$WITH_KEYS" = 1 ]; then
      deploy_keys --pod "$PREFIX-$p" >/dev/null 2>&1 || { echo "[$p] FAILED deploy_keys"; exit 1; }
    fi
    echo "[$p] READY"
  ) &
done
wait
echo "PIPELINE COMPLETE"
