#!/bin/bash
# 安装 Mac 开机自启 / 后台守护（launchd）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_NAME="com.telegram.feishu.forwarder.plist"
PLIST_SRC="$SCRIPT_DIR/$PLIST_NAME"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "请先运行: ./start.sh install && ./start.sh login"
    exit 1
fi

mkdir -p "$SCRIPT_DIR/logs"
mkdir -p "$HOME/Library/LaunchAgents"

sed \
    -e "s|__SCRIPT_DIR__|$SCRIPT_DIR|g" \
    -e "s|__VENV_PYTHON__|$VENV_PYTHON|g" \
    "$PLIST_SRC" > "$PLIST_DST"

launchctl bootout "gui/$(id -u)/$PLIST_NAME" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl enable "gui/$(id -u)/$PLIST_NAME"
launchctl kickstart -k "gui/$(id -u)/$PLIST_NAME"

echo ">>> launchd 服务已安装并启动"
echo ">>> 查看状态: launchctl print gui/$(id -u)/$PLIST_NAME"
echo ">>> 停止服务: launchctl bootout gui/$(id -u)/$PLIST_NAME"
echo ">>> 卸载服务: launchctl bootout gui/$(id -u)/$PLIST_NAME && rm $PLIST_DST"
