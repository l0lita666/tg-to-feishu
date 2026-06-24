#!/bin/bash
# 修复 UFW 误拦 80/443 导致飞书无法回调的问题
# 用法: SSHPASS='密码' ./deploy/fix-ufw-web.sh

set -euo pipefail

SERVER_IP="43.160.239.73"
SERVER_USER="${SERVER_USER:-ubuntu}"

if [[ -z "${SSHPASS:-}" ]]; then
    read -rsp "SSH 密码 (${SERVER_USER}@${SERVER_IP}): " SSHPASS
    echo
    export SSHPASS
fi

SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=30 -o ServerAliveInterval=10)
SSH_CMD=(sshpass -e ssh "${SSH_OPTS[@]}" "${SERVER_USER}@${SERVER_IP}")

echo ">>> 放行 80/443（飞书事件回调 / Nginx）..."
"${SSH_CMD[@]}" 'sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw status numbered'

echo ""
echo ">>> 测试飞书回调地址..."
curl -s -o /dev/null -w "https://bot.99ka.life/feishu/event → HTTP %{http_code}\n" \
    --max-time 10 -X POST "https://bot.99ka.life/feishu/event" \
    -H 'Content-Type: application/json' -d '{}' || true

echo ">>> 完成。请在飞书试回复一条 TG 消息验证。"
