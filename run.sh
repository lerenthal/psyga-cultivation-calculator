#!/usr/bin/env bash
# Launcher for the PsygaBio Production System (git-bash / WSL / MSYS).
# Bypasses the activate-script dance by invoking the venv Python directly.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$SCRIPT_DIR/.venv/Scripts/python.exe"
VENV_PY_LINUX="$SCRIPT_DIR/.venv/bin/python"

if [[ -f "$VENV_PY" ]]; then
    PY="$VENV_PY"
elif [[ -f "$VENV_PY_LINUX" ]]; then
    PY="$VENV_PY_LINUX"
else
    echo "[ERROR] Virtual environment not found." >&2
    echo "Run:  uv venv && uv pip install -r requirements.txt" >&2
    exit 1
fi

exec "$PY" "$SCRIPT_DIR/app.py" "$@"
