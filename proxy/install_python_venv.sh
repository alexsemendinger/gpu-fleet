#!/usr/bin/env bash
# One-time: create ~/.venvs/global (the Python every shortcut command uses) and
# auto-activate it in interactive shells. Run as root. Safe to re-run.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
[ "$(id -u)" -eq 0 ] || { echo "Run this as root (e.g. sudo -i first)." >&2; exit 1; }

apt-get update
apt-get install -y python3-venv python3-full

VENV_DIR="$HOME/.venvs/global"
mkdir -p "$(dirname "$VENV_DIR")"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel

# Auto-activate in interactive shells, once (the marker makes re-runs no-ops).
ACTIVATE='if [ -z "$VIRTUAL_ENV" ] && [ -f "$HOME/.venvs/global/bin/activate" ]; then . "$HOME/.venvs/global/bin/activate"; fi'
for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  [ "$rc" = "$HOME/.zshrc" ] && [ ! -f "$rc" ] && continue   # only touch zsh if it's in use
  grep -qF '.venvs/global/bin/activate' "$rc" 2>/dev/null || printf '\n# Auto-activate global Python venv\n%s\n' "$ACTIVATE" >> "$rc"
done

echo "Done. Open a new shell or: source ~/.bashrc"
