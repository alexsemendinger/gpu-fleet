# Shared setup for the shell tools, sourced (not run): repo paths, the Python
# that runs the management scripts, and config.env.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MGMT="$REPO/management"
PY="${PY:-$HOME/.venvs/global/bin/python}"
[ -x "$PY" ] || PY=python3
if [ ! -f "$REPO/config.env" ]; then
    echo "config.env not found in $REPO (copy config.env.example and fill it in)" >&2
    exit 1
fi
# shellcheck disable=SC1091
source "$REPO/config.env"
PREFIX="${MACHINE_NAME_PREFIX:?set MACHINE_NAME_PREFIX in config.env}"
# Python on the pods, used for the CUDA check.
POD_PYTHON="${POD_PYTHON:-python3}"
