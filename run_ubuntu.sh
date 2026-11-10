#!/bin/sh
# Optional Ubuntu (Linux) launcher for the PsygaBio Production System.
#
# Unlike run.bat / run.ps1, this script also bootstraps the environment:
# if .venv/bin/python is missing it creates the venv and installs
# requirements.txt, and if the venv exists but the app dependencies are
# missing it installs them. It then starts the app from the project
# directory (config.yaml is loaded by relative path).
#
# Usage:
#   chmod +x run_ubuntu.sh   # once, after cloning
#   ./run_ubuntu.sh
#   ./run_ubuntu.sh --background   # run detached (nohup), log in app.log
#
# Port: defaults to 5001 so this version can run alongside an earlier
# copy on the default 5000. Override with e.g. PORT=5002 ./run_ubuntu.sh
#
# Requires Python 3.11+ (python3). If `uv` is installed it is preferred
# for venv creation/installation; otherwise python3-venv + pip is used.
# On Ubuntu, python3-venv may need installing first:
#   sudo apt install python3-venv python3-full
#
# NOTE: this script is POSIX sh compatible (no bashisms like
# `set -o pipefail` or ${BASH_SOURCE[0]}), so it also works when run
# as `sh run_ubuntu.sh` — Ubuntu's /bin/sh is dash, which rejects
# bash-only options.

set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

# Background mode: relaunch detached with output in app.log, then exit.
if [ "${1:-}" = "--background" ]; then
    shift
    nohup "$SCRIPT_DIR/run_ubuntu.sh" "$@" > "$SCRIPT_DIR/app.log" 2>&1 &
    echo "Started in background (PID $!). Log: $SCRIPT_DIR/app.log — stop with: kill $!"
    exit 0
fi

# Always run from the project directory, wherever the script is called from.
cd "$SCRIPT_DIR"

VENV_PY=".venv/bin/python"

install_requirements() {
    if command -v uv >/dev/null 2>&1; then
        uv pip install --python "$VENV_PY" -r requirements.txt
    else
        "$VENV_PY" -m pip install -r requirements.txt
    fi
}

if [ ! -x "$VENV_PY" ]; then
    echo "[setup] Virtual environment not found — creating .venv ..."
    if command -v uv >/dev/null 2>&1; then
        uv venv
    elif command -v python3 >/dev/null 2>&1; then
        python3 -m venv .venv
    else
        echo "[ERROR] python3 not found. Install Python 3.11+ first:" >&2
        echo "  sudo apt install python3 python3-venv python3-full" >&2
        exit 1
    fi
    install_requirements
fi

# Venv exists but dependencies are missing (e.g. copied from another
# machine, or a partial install) — top them up before starting.
if ! "$VENV_PY" -c "import flask, yaml, flask_sqlalchemy, flask_login" 2>/dev/null; then
    echo "[setup] App dependencies missing — installing from requirements.txt ..."
    install_requirements
fi

echo "Starting PsygaBio Production System at http://127.0.0.1:${PORT:-5001}"
PORT="${PORT:-5001}" exec "$VENV_PY" app.py "$@"
