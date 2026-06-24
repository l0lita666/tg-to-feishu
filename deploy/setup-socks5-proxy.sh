#!/bin/bash
# 从 Mac 一键在新加坡服务器上部署 SOCKS5 代理
# 用法:
#   SSHPASS='你的密码' ./deploy/setup-socks5-proxy.sh
#   ./deploy/setup-socks5-proxy.sh 1080   # 可选端口，默认 1080

set -euo pipefail

SERVER_IP="43.160.239.73"
SERVER_USER="${SERVER_USER:-ubuntu}"
SOCKS_PORT="${1:-1080}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SCRIPT="${SCRIPT_DIR}/socks5/install-on-server.sh"

if ! command -v sshpass >/dev/null 2>&1; then
    echo "请先安装 sshpass: brew install hudochenkov/sshpass/sshpass"
    exit 1
fi

if [[ ! -f "$INSTALL_SCRIPT" ]]; then
    echo "找不到安装脚本: $INSTALL_SCRIPT"
    exit 1
fi

if [[ -z "${SSHPASS:-}" ]]; then
    read -rsp "SSH 密码 (${SERVER_USER}@${SERVER_IP}): " SSHPASS
    echo
    export SSHPASS
fi

SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=30 -o ServerAliveInterval=10)
SSH_CMD=(sshpass -e ssh "${SSH_OPTS[@]}" "${SERVER_USER}@${SERVER_IP}")

echo ">>> [1/3] 测试 SSH..."
if ! "${SSH_CMD[@]}" 'whoami && hostname'; then
    echo ""
    echo "SSH 连接失败，可使用腾讯云在线终端手动执行:"
    echo "  sudo bash install-on-server.sh ${SOCKS_PORT}"
    exit 1
fi

echo ">>> [2/3] 上传安装脚本..."
sshpass -e scp "${SSH_OPTS[@]}" "$INSTALL_SCRIPT" "${SERVER_USER}@${SERVER_IP}:/tmp/install-socks5.sh"

echo ">>> [3/3] 在服务器上安装 SOCKS5 (端口: ${SOCKS_PORT})..."
"${SSH_CMD[@]}" "sudo bash /tmp/install-socks5.sh '${SOCKS_PORT}'"

echo ""
echo ">>> 完成！请到腾讯云防火墙放行 TCP ${SOCKS_PORT}"
echo ">>> 凭证保存在服务器 /root/socks5-credentials.txt"
