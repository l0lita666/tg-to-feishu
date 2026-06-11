#!/bin/bash
# 一键部署到新加坡轻量机（bot.99ka.life）
# 用法: SSHPASS='Yzlh@123' ./deploy/deploy-to-server.sh
# 或:   ./deploy/deploy-to-server.sh   # 会提示输入密码（见 部署与运维手册.md）

set -euo pipefail

SERVER_IP="43.160.239.73"
SERVER_USER="${SERVER_USER:-ubuntu}"
DOMAIN="bot.99ka.life"
DIR="/opt/telegram-feishu-forwarder"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if ! command -v sshpass >/dev/null 2>&1; then
    echo "请先安装 sshpass: brew install sshpass 或 brew install hudochenkov/sshpass/sshpass"
    exit 1
fi

if [[ -z "${SSHPASS:-}" ]]; then
    read -rsp "SSH 密码 (${SERVER_USER}@${SERVER_IP}): " SSHPASS
    echo
    export SSHPASS
fi

SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=30)
RSYNC_SSH="sshpass -e ssh ${SSH_OPTS[*]}"
SSH_CMD=(sshpass -e ssh "${SSH_OPTS[@]}" "${SERVER_USER}@${SERVER_IP}")

echo ">>> [1/6] 测试 SSH..."
"${SSH_CMD[@]}" 'whoami && hostname'

echo ">>> [2/6] 上传代码..."
"${SSH_CMD[@]}" "sudo mkdir -p $DIR && sudo chown \$USER:\$USER $DIR"
"${SSH_CMD[@]}" "sudo systemctl stop telegram-feishu 2>/dev/null || true"
rsync -avz -e "$RSYNC_SSH" \
    --exclude '.venv' --exclude 'logs' --exclude '.git' \
    "$PROJECT_DIR/" "${SERVER_USER}@${SERVER_IP}:${DIR}/"
sshpass -e scp "${SSH_OPTS[@]}" \
    "$PROJECT_DIR/.env" \
    "$PROJECT_DIR/telegram_session.session" \
    "${SERVER_USER}@${SERVER_IP}:${DIR}/"
"${SSH_CMD[@]}" "chmod u+rw ${DIR}/telegram_session.session ${DIR}/data 2>/dev/null; chmod -R u+rw ${DIR}/data 2>/dev/null || true"

echo ">>> [3/6] 安装 Python 依赖..."
"${SSH_CMD[@]}" "cd $DIR && ./start.sh install"

echo ">>> [4/6] 安装 Nginx + 申请证书..."
"${SSH_CMD[@]}" bash -s <<'REMOTE'
set -euo pipefail
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx certbot python3-certbot-nginx
if [[ ! -f /etc/letsencrypt/live/bot.99ka.life/fullchain.pem ]]; then
    sudo certbot --nginx -d bot.99ka.life \
        --non-interactive --agree-tos --register-unsafely-without-email \
        --redirect || true
fi
sudo cp /opt/telegram-feishu-forwarder/deploy/nginx-bot.99ka.life.conf \
        /etc/nginx/sites-available/bot.99ka.life
sudo ln -sf /etc/nginx/sites-available/bot.99ka.life /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
REMOTE

echo ">>> [5/6] 安装 systemd 并启动..."
"${SSH_CMD[@]}" "cd $DIR && ./install_systemd.sh"

echo ">>> [6/6] 健康检查..."
sleep 3
curl -fsS "https://${DOMAIN}/feishu/health" && echo

echo ""
echo ">>> 部署完成！"
echo ">>> 请到 Lark 开放平台确认事件地址: https://${DOMAIN}/feishu/event"
echo ">>> 查看日志: ssh ${SERVER_USER}@${SERVER_IP} 'tail -f ${DIR}/logs/listener.log'"
