#!/bin/bash
# 从 Mac 一键在新加坡服务器上部署 WireGuard VPN
# 用法:
#   SSHPASS='你的密码' ./deploy/setup-wireguard-vpn.sh
#   ./deploy/setup-wireguard-vpn.sh phone        # 可选：客户端名称，默认 phone
#   ./deploy/setup-wireguard-vpn.sh phone 51820  # 可选：端口，默认 51820

set -euo pipefail

SERVER_IP="43.160.239.73"
SERVER_USER="${SERVER_USER:-ubuntu}"
CLIENT_NAME="${1:-phone}"
WG_PORT="${2:-51820}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SCRIPT="${SCRIPT_DIR}/wireguard/install-on-server.sh"

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
    echo "SSH 连接失败。你也可以："
    echo "  1. 登录腾讯云控制台 -> 轻量服务器 -> 在线终端"
    echo "  2. 上传 deploy/wireguard/install-on-server.sh 后执行:"
    echo "     sudo bash install-on-server.sh ${CLIENT_NAME}"
    echo "  3. 在腾讯云防火墙放行 UDP ${WG_PORT}"
    exit 1
fi

echo ">>> [2/3] 上传安装脚本..."
sshpass -e scp "${SSH_OPTS[@]}" "$INSTALL_SCRIPT" "${SERVER_USER}@${SERVER_IP}:/tmp/install-wireguard.sh"

echo ">>> [3/3] 在服务器上安装 WireGuard (客户端: ${CLIENT_NAME}, 端口: ${WG_PORT})..."
"${SSH_CMD[@]}" "sudo PUBLIC_IP='${SERVER_IP}' WG_PORT='${WG_PORT}' bash /tmp/install-wireguard.sh '${CLIENT_NAME}'"

echo ""
echo ">>> 完成！请用手机 WireGuard App 扫描上方二维码。"
echo ">>> 若手机连不上，请到腾讯云控制台 -> 防火墙 -> 放行 UDP ${WG_PORT}"
