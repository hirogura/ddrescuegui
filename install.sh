#!/bin/bash
set -e

INSTALL_DIR="/opt/ddrescuegui"
GIT_REPO="https://github.com/hirogura/ddrescuegui.git"
PORT=3327
SERVICE_NAME="ddrescuegui"

info() { printf '\033[1;32m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install]\033[0m %s\n' "$*"; }

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

# ---- Tailscale serve（Tailnet 内のみ HTTPS 公開） ----
TAILNET_URL=""
if command -v tailscale >/dev/null 2>&1 && tailscale status >/dev/null 2>&1; then
    TAILNET_DNS="$(tailscale status --json 2>/dev/null | python3 -c '
import sys, json
try:
    j = json.load(sys.stdin)
    print(((j.get("Self") or {}).get("DNSName") or "").rstrip("."))
except Exception:
    pass
' || true)"
    if [ -n "$TAILNET_DNS" ]; then
        info "Tailscale serve を設定中 (https://${TAILNET_DNS}:${PORT})..."
        if tailscale serve --bg --https=${PORT} http://127.0.0.1:${PORT} 2>/dev/null; then
            TAILNET_URL="https://${TAILNET_DNS}:${PORT}"
            info "Tailscale serve を設定しました: ${TAILNET_URL}"
            info "公開範囲: Tailnet 内のみ"
        else
            warn "tailscale serve の設定に失敗しました。手動で実行してください:"
            warn "  sudo tailscale serve --bg --https=${PORT} http://127.0.0.1:${PORT}"
        fi
    else
        warn "Tailnet の DNS 名を取得できませんでした。serve 設定は手動で行ってください。"
    fi
else
    info "Tailscale が見つかりません。HTTPS 公開しない場合はこのままで構いません。"
    info "設定する場合: sudo tailscale serve --bg --https=${PORT} http://127.0.0.1:${PORT}"
fi

echo ""
echo "=== Done! ==="
echo "ddrescueGUI is running at http://localhost:${PORT}"
if [ -n "$TAILNET_URL" ]; then
    echo "HTTPS (Tailnet only): ${TAILNET_URL}"
fi
echo ""
echo "Commands:"
echo "  systemctl status ${SERVICE_NAME}"
echo "  systemctl restart ${SERVICE_NAME}"
echo "  journalctl -u ${SERVICE_NAME} -f"
