#!/usr/bin/env bash
# install.sh [BIN_DIR] — put the shortcut commands (list_pods, create_pods, ...)
# on your PATH by symlinking bin/* into BIN_DIR (default /usr/local/bin).
#
# Safe to re-run. It never overwrites a command it didn't install: an existing
# file in BIN_DIR that isn't a symlink into this repo is left alone with a
# warning (set FORCE=1 to replace it).
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
BIN_DIR="${1:-/usr/local/bin}"
mkdir -p "$BIN_DIR"

installed=0 skipped=0
for src in "$REPO"/bin/*; do
    name="$(basename "$src")"
    dst="$BIN_DIR/$name"
    if [ -e "$dst" ] || [ -L "$dst" ]; then
        current="$(readlink -f "$dst" || true)"
        if [ "$current" != "$(readlink -f "$src")" ] && [ "${FORCE:-0}" != 1 ]; then
            echo "skip  $name: $dst already exists and isn't ours (FORCE=1 to replace)"
            skipped=$((skipped + 1))
            continue
        fi
    fi
    ln -sfn "$src" "$dst"
    installed=$((installed + 1))
done
echo "installed $installed commands into $BIN_DIR ($skipped skipped)"

[ -f "$REPO/config.env" ] || echo "next: cp $REPO/config.env.example $REPO/config.env and fill it in"
