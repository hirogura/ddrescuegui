#!/bin/bash
set -e

INSTALL_DIR="/opt/ddrescuegui"
GIT_REPO="https://github.com/hirogura/ddrescuegui.git"
PORT=3327
SERVICE_NAME="ddrescuegui"

echo "=== ddrescueGUI Installer (GitHub) ==="

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run as root." >&2
    exit 1
fi

echo "[1/5] Installing dependencies..."
apt-get update -qq
apt-get install -y -qq python3 gddrescue smartmontools fdisk git

echo "[2/5] Downloading ddrescueGUI from GitHub..."
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "  Existing installation found. Updating from GitHub..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    if [ -e "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
        echo "Error: $INSTALL_DIR already exists and is not empty." >&2
        echo "Please move it aside or remove it, then re-run this script." >&2
        exit 1
    fi
    git clone "$GIT_REPO" "$INSTALL_DIR"
fi

echo "[3/5] Creating directories..."
mkdir -p "$INSTALL_DIR/logs"

echo "[4/5] Creating systemd service..."
cat > /etc/systemd/system/${SERVICE_NAME}.service << SVCEOF
[Unit]
Description=ddrescueGUI - Web-based ddrescue interface
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

echo "[5/5] Enabling and starting service..."
systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl restart ${SERVICE_NAME}

echo ""
echo "=== Done! ==="
echo "ddrescueGUI is running at http://localhost:${PORT}"
echo ""
echo "Commands:"
echo "  systemctl status ${SERVICE_NAME}"
echo "  systemctl restart ${SERVICE_NAME}"
echo "  journalctl -u ${SERVICE_NAME} -f"
