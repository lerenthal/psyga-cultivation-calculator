#!/usr/bin/env bash
#
# Install the Server Control Panel on an Ubuntu server.
#
#   ./install_ubuntu.sh                # user-level systemd (no root needed)
#   ./install_ubuntu.sh --system       # system-level service (needs root)
#
# The script is idempotent - run it again after updating the code.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
INSTALL_DIR="${INSTALL_DIR:-$HOME/server_control_panel}"
SCOPE="user"

for arg in "$@"; do
    case "$arg" in
        --system) SCOPE="system" ;;
        --help|-h)
            sed -n '2,12p' "$0"
            exit 0
            ;;
    esac
done

echo "==> Installing the Server Control Panel into $INSTALL_DIR ($SCOPE scope)"

if [ ! -d "$INSTALL_DIR/.git" ] && [ ! -f "$INSTALL_DIR/dashboard.py" ]; then
    mkdir -p "$INSTALL_DIR"
    # Copy the code but leave the virtualenv and the registry behind.
    # deploy/ has to come along: the systemd step below reads the unit
    # template from it.
    for item in panel templates static deploy dashboard.py requirements.txt \
                apps.yaml apps.example.yaml README.md .gitignore; do
        [ -e "$PROJECT_DIR/$item" ] && cp -r "$PROJECT_DIR/$item" "$INSTALL_DIR/"
    done
fi

cd "$INSTALL_DIR"

echo "==> Creating the virtual environment"
if command -v uv >/dev/null 2>&1; then
    uv venv
    uv pip install --python .venv/bin/python -r requirements.txt
else
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt
fi

echo "==> Choosing a password"
if [ -z "${PANEL_PASSWORD:-}" ]; then
    echo "    PANEL_PASSWORD is not set - the panel will generate one and"
    echo "    print it the first time it starts."
else
    echo "    using the PANEL_PASSWORD you provided"
fi

if [ "$SCOPE" = "system" ]; then
    echo "==> Installing the system-level systemd service"
    sudo cp deploy/control-panel.system.service /etc/systemd/system/server-control-panel.service
    sudo sed -i "s|User=YOUR_USER|User=$(whoami)|" /etc/systemd/system/server-control-panel.service
    sudo sed -i "s|WorkingDirectory=/opt/server_control_panel|WorkingDirectory=$INSTALL_DIR|" \
        /etc/systemd/system/server-control-panel.service
    sudo sed -i "s|ExecStart=/opt/server_control_panel/.venv/bin/python|ExecStart=$INSTALL_DIR/.venv/bin/python|" \
        /etc/systemd/system/server-control-panel.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now server-control-panel
    echo "    systemctl status server-control-panel"
else
    echo "==> Installing the user-level systemd service"
    mkdir -p "$HOME/.config/systemd/user"
    sed "s|%h/server_control_panel|$INSTALL_DIR|g" \
        deploy/control-panel.service > "$HOME/.config/systemd/user/server-control-panel.service"
    systemctl --user daemon-reload
    systemctl --user enable --now server-control-panel
    # Let the service start at boot even when nobody is logged in.
    loginctl enable-linger "$(whoami)" 2>/dev/null || \
        echo "    note: run 'sudo loginctl enable-linger $(whoami)' to start at boot"
    echo "    systemctl --user status server-control-panel"
fi

echo ""
echo "==> Done."
echo "    The panel listens on 0.0.0.0:5050."
echo "    Open http://<server-ip>:5050 and sign in with the panel password."
