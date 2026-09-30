#!/usr/bin/env bash
# run_cmd.sh — run a command on every host in MACHINE_NAME_LIST, one after another

# shellcheck disable=SC1091
source "$(dirname "$0")/common.sh"
SSH_USER="${SSH_USER:-root}"

if [ $# -lt 1 ]; then
  echo "Usage: $(basename "$0") <command...>"
  echo "Example: $(basename "$0") \"apt update\""
  exit 1
fi

# Safely reconstruct the command from all args
printf -v CMD_STR '%q ' "$@"
CMD_STR=${CMD_STR% }

for name in "${MACHINE_NAME_LIST[@]}"; do
  host="${PREFIX}-${name}"
  echo "[$host] running: $*"
  ssh "${SSH_USER}@${host}" bash -lc "$CMD_STR"
done