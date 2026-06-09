#!/bin/bash
# 安装 Linux systemd 服务（腾讯云 / Ubuntu / CentOS 等）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="telegram-feishu"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CURRENT_USER="$(whoami)"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "请先运行: ./start.sh install && ./start.sh login"
    exit 1
fi

if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    echo "未找到 .env 文件，请先配置"
    exit 1
fi

if [[ ! -f "$SCRIPT_DIR/telegram_session.session" ]]; then
    echo "未找到 telegram_session.session，请先在 Mac 或本机完成 ./start.sh login"
    exit 1
fi

mkdir -p "$SCRIPT_DIR/logs"

sudo tee "$UNIT_FILE" > /dev/null <<EOF
[Unit]
Description=Telegram to Feishu Forwarder
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${VENV_PYTHON} ${SCRIPT_DIR}/telegram_listener.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo ">>> systemd 服务已安装并启动"
echo ">>> 查看状态: sudo systemctl status ${SERVICE_NAME}"
echo ">>> 查看日志: journalctl -u ${SERVICE_NAME} -f"
echo ">>> 重启服务: sudo systemctl restart ${SERVICE_NAME}"
echo ">>> 停止服务: sudo systemctl stop ${SERVICE_NAME}"
