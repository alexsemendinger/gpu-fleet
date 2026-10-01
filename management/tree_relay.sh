#!/usr/bin/env bash
# tree_relay.sh <source-pod> <target-pod> [target-pod ...]
# Relays an HF cache payload pod-to-pod via the proxy (tar pipe).
# Tree topology: every verified target becomes a source for remaining targets.
# Payload dirs come from $RELAY_DIRS (space-separated, relative to the HF cache).
set -uo pipefail
# shellcheck disable=SC1091
source "$(dirname "$0")/common.sh"
DIRS="${RELAY_DIRS:?set RELAY_DIRS to the space-separated cache dirs to relay, e.g. RELAY_DIRS=\"hub/models--Qwen--Qwen3-32B\"}"
CACHE=/root/.cache/huggingface

copy() {  # copy <src> <dst>
  ssh "$PREFIX-$1" "tar cf - -C $CACHE $DIRS" \
    | ssh "$PREFIX-$2" "mkdir -p $CACHE && tar xf - -C $CACHE"
}
verify() {  # verify <pod>
  ssh "$PREFIX-$1" "ok=1; for d in $DIRS; do [ -d $CACHE/\$d ] || { echo MISSING:\$d; ok=0; }; done; \
    n=\$(find $CACHE/hub -name '*.incomplete' 2>/dev/null | wc -l); [ \"\$n\" = 0 ] || { echo INCOMPLETE:\$n; ok=0; }; \
    [ \"\$ok\" = 1 ] && echo VERIFY_OK"
}

SEED="${1:?usage: RELAY_DIRS="..." tree_relay.sh <source> <target...>}"; shift
sources=("$SEED"); queue=("$@"); failed=()
declare -A tries
round=1
while [ "${#queue[@]}" -gt 0 ]; do
  n=$(( ${#sources[@]} < ${#queue[@]} ? ${#sources[@]} : ${#queue[@]} ))
  batch=("${queue[@]:0:n}"); queue=("${queue[@]:n}")
  echo "[round $round] $(date -u +%H:%M:%S) copying to: ${batch[*]} (from: ${sources[*]:0:n})"
  pids=(); dsts=()
  for i in $(seq 0 $((n-1))); do
    src="${sources[$i]}"; dst="${batch[$i]}"
    ( copy "$src" "$dst" && [ "$(verify "$dst" | tail -1)" = VERIFY_OK ] \
        && echo "[relay] $dst OK (from $src)" || { echo "[relay] $dst FAILED (from $src)"; exit 1; } ) &
    pids+=($!); dsts+=("$dst")
  done
  retry=0
  for i in $(seq 0 $((n-1))); do
    d="${dsts[$i]}"
    if wait "${pids[$i]}"; then sources+=("$d"); continue; fi
    tries[$d]=$(( ${tries[$d]:-0} + 1 ))
    if [ "${tries[$d]}" -lt 3 ]; then queue+=("$d"); retry=1; echo "[relay] will retry $d (attempt ${tries[$d]}/3 failed)"
    else failed+=("$d"); echo "[relay] giving up on $d after 3 attempts"; fi
  done
  # A failure is often a pod that isn't reachable yet: pause before retrying it.
  [ "$retry" = 1 ] && sleep 20
  round=$((round+1))
done
echo "TREE RELAY DONE: ok: ${sources[*]:1}${failed:+ | FAILED: ${failed[*]}}"
[ "${#failed[@]}" -eq 0 ]
