#!/bin/bash
# 在新加坡服务器上安装 SOCKS5 代理（与 WireGuard 独立，互不影响）
# 用法: sudo bash install-on-server.sh [端口]
# 默认端口: 1080

set -euo pipefail

SOCKS_PORT="${1:-1080}"
CRED_FILE="/root/socks5-credentials.txt"
SERVICE_NAME="microsocks"
BIN="/usr/local/bin/microsocks"
UNIT="/etc/systemd/system/microsocks.service"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "请使用 root 运行: sudo bash $0"
    exit 1
fi

echo ">>> 安装依赖..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq build-essential git ufw >/dev/null

if [[ ! -x "$BIN" ]]; then
    echo ">>> 编译 microsocks..."
    TMP="$(mktemp -d)"
    git clone -q --depth 1 https://github.com/rofl0r/microsocks.git "$TMP/microsocks"
    make -C "$TMP/microsocks" -s
    install -m 755 "$TMP/microsocks/microsocks" "$BIN"
    rm -rf "$TMP"
fi

if [[ -f "$CRED_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$CRED_FILE"
    SOCKS_USER="${SOCKS_USER:-}"
    SOCKS_PASS="${SOCKS_PASS:-}"
    echo ">>> 使用已有账号: ${SOCKS_USER}"
else
    SOCKS_USER="proxy"
    SOCKS_PASS="$(openssl rand -base64 12 | tr -d '/+=' | head -c 16)"
    cat > "$CRED_FILE" <<EOF
SOCKS_USER=${SOCKS_USER}
SOCKS_PASS=${SOCKS_PASS}
SOCKS_PORT=${SOCKS_PORT}
PUBLIC_IP=43.160.239.73
EOF
    chmod 600 "$CRED_FILE"
    echo ">>> 已生成新账号密码"
fi

ENV_FILE="/etc/microsocks.env"
echo ">>> 写入环境变量与 systemd 服务..."
cat > "$ENV_FILE" <<EOF
SOCKS_PORT=${SOCKS_PORT}
SOCKS_USER=${SOCKS_USER}
SOCKS_PASS=${SOCKS_PASS}
EOF
chmod 600 "$ENV_FILE"

# systemd 会直接吞掉 ExecStart 里的 -P 参数，需用 shell 包装
cat > "$UNIT" <<'EOF'
[Unit]
Description=SOCKS5 Proxy (microsocks)
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/microsocks.env
ExecStart=/bin/sh -c 'exec /usr/local/bin/microsocks -i 0.0.0.0 -p "$SOCKS_PORT" -u "$SOCKS_USER" -P "$SOCKS_PASS"'
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
systemctl restart "$SERVICE_NAME"

echo ">>> 配置防火墙 (TCP ${SOCKS_PORT})..."
ufw allow "${SOCKS_PORT}/tcp" >/dev/null 2>&1 || true
ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null 2>&1 || true
echo "y" | ufw enable >/dev/null 2>&1 || true

# 更新凭证文件中的端口
sed -i "s/^SOCKS_PORT=.*/SOCKS_PORT=${SOCKS_PORT}/" "$CRED_FILE"

echo ""
echo "=============================================="
echo " SOCKS5 代理已就绪（与 WireGuard 独立运行）"
echo "=============================================="
echo " 服务器: 43.160.239.73"
echo " 端口:   ${SOCKS_PORT}"
echo " 用户名: ${SOCKS_USER}"
echo " 密码:   ${SOCKS_PASS}"
echo ""
echo " Telegram 配置:"
echo "   设置 → 数据和存储 → 代理 → 添加代理 → SOCKS5"
echo ""
echo " 凭证已保存: ${CRED_FILE}"
echo "=============================================="
systemctl --no-pager status "$SERVICE_NAME" | head -5
