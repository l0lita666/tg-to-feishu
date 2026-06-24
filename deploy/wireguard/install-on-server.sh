#!/bin/bash
# 在新加坡服务器上安装 WireGuard VPN（手机可用）
# 可直接在服务器上运行: sudo bash install-on-server.sh [客户端名称]
# 默认客户端名: phone

set -euo pipefail

WG_PORT="${WG_PORT:-51820}"
WG_NET="10.8.0.0/24"
WG_SERVER_IP="10.8.0.1"
CLIENT_NAME="${1:-phone}"
CLIENT_IP="10.8.0.2"
CONF_DIR="/etc/wireguard"
CLIENT_DIR="/root/wireguard-clients"
PUBLIC_IP="${PUBLIC_IP:-43.160.239.73}"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "请使用 root 运行: sudo bash $0"
    exit 1
fi

echo ">>> 检测网卡..."
IFACE="$(ip -4 route show default | awk '{print $5; exit}')"
if [[ -z "$IFACE" ]]; then
    echo "无法检测默认网卡"
    exit 1
fi
echo "    使用网卡: $IFACE"

echo ">>> 安装 WireGuard..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq wireguard qrencode ufw >/dev/null

echo ">>> 开启 IP 转发..."
sysctl -w net.ipv4.ip_forward=1 >/dev/null
grep -q '^net.ipv4.ip_forward=1' /etc/sysctl.conf 2>/dev/null || \
    echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf

mkdir -p "$CONF_DIR" "$CLIENT_DIR"
chmod 700 "$CONF_DIR"

if [[ ! -f "$CONF_DIR/server_private.key" ]]; then
    echo ">>> 生成服务端密钥..."
    wg genkey | tee "$CONF_DIR/server_private.key" | wg pubkey > "$CONF_DIR/server_public.key"
    chmod 600 "$CONF_DIR/server_private.key"
fi

SERVER_PRIVATE="$(cat "$CONF_DIR/server_private.key")"
SERVER_PUBLIC="$(cat "$CONF_DIR/server_public.key")"

CLIENT_PRIVATE="$(wg genkey)"
CLIENT_PUBLIC="$(echo "$CLIENT_PRIVATE" | wg pubkey)"

# 若已有 wg0，追加 peer；否则新建
CLIENT_EXISTS=false
if [[ -f "$CONF_DIR/wg0.conf" ]] && systemctl is-active --quiet wg-quick@wg0 2>/dev/null; then
  if grep -q "# client: ${CLIENT_NAME}" "$CONF_DIR/wg0.conf" 2>/dev/null; then
    echo "客户端 ${CLIENT_NAME} 已存在，重新导出配置"
    CLIENT_EXISTS=true
  else
    # 分配下一个可用 IP
    LAST_OCTET="$(grep 'AllowedIPs = 10.8.0.' "$CONF_DIR/wg0.conf" | sed 's/.*10\.8\.0\.\([0-9]*\).*/\1/' | sort -n | tail -1)"
    CLIENT_IP="10.8.0.$(( ${LAST_OCTET:-1} + 1 ))"
    cat >> "$CONF_DIR/wg0.conf" <<EOF

# client: ${CLIENT_NAME}
[Peer]
PublicKey = ${CLIENT_PUBLIC}
AllowedIPs = ${CLIENT_IP}/32
EOF
    wg syncconf wg0 <(wg-quick strip wg0)
    echo ">>> 已追加客户端 peer: ${CLIENT_NAME} (${CLIENT_IP})"
  fi
else
  echo ">>> 写入服务端配置..."
  cat > "$CONF_DIR/wg0.conf" <<EOF
[Interface]
Address = ${WG_SERVER_IP}/24
ListenPort = ${WG_PORT}
PrivateKey = ${SERVER_PRIVATE}
PostUp = iptables -A FORWARD -i %i -j ACCEPT; iptables -A FORWARD -o %i -j ACCEPT; iptables -t nat -A POSTROUTING -o ${IFACE} -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -D FORWARD -o %i -j ACCEPT; iptables -t nat -D POSTROUTING -o ${IFACE} -j MASQUERADE

# client: ${CLIENT_NAME}
[Peer]
PublicKey = ${CLIENT_PUBLIC}
AllowedIPs = ${CLIENT_IP}/32
EOF
  chmod 600 "$CONF_DIR/wg0.conf"
  systemctl enable wg-quick@wg0 >/dev/null 2>&1 || true
  systemctl restart wg-quick@wg0
fi

CLIENT_CONF="${CLIENT_DIR}/${CLIENT_NAME}.conf"
if [[ "$CLIENT_EXISTS" == true && -f "$CLIENT_CONF" ]]; then
  echo ">>> 使用已有配置文件: ${CLIENT_CONF}"
else
cat > "$CLIENT_CONF" <<EOF
[Interface]
PrivateKey = ${CLIENT_PRIVATE}
Address = ${CLIENT_IP}/24
DNS = 8.8.8.8, 1.1.1.1

[Peer]
PublicKey = ${SERVER_PUBLIC}
Endpoint = ${PUBLIC_IP}:${WG_PORT}
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
EOF
chmod 600 "$CLIENT_CONF"
fi

echo ">>> 配置防火墙 (UDP ${WG_PORT})..."
ufw allow "${WG_PORT}/udp" >/dev/null 2>&1 || true
# 飞书回调 / Nginx 依赖 80、443，启用 UFW 前必须放行
ufw allow 80/tcp >/dev/null 2>&1 || true
ufw allow 443/tcp >/dev/null 2>&1 || true
# 确保 SSH 不被关
ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null 2>&1 || true
echo "y" | ufw enable >/dev/null 2>&1 || true

echo ""
echo "=============================================="
echo " WireGuard VPN 已就绪"
echo "=============================================="
echo " 服务器: ${PUBLIC_IP}:${WG_PORT}"
echo " 客户端配置: ${CLIENT_CONF}"
echo ""
echo " 手机连接方式:"
echo "   1. 安装 WireGuard App (iOS / Android)"
echo "   2. 扫描下方二维码，或导入配置文件"
echo ""
qrencode -t ansiutf8 < "$CLIENT_CONF"
echo ""
echo " 配置文件内容（可保存到手机）:"
echo "----------------------------------------------"
cat "$CLIENT_CONF"
echo "----------------------------------------------"
wg show wg0 2>/dev/null || true
