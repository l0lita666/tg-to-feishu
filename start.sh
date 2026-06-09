#!/bin/bash
# Telegram 监听程序启动脚本（Mac）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE="$SCRIPT_DIR/.listener.pid"
LOG_DIR="$SCRIPT_DIR/logs"
VENV_DIR="$SCRIPT_DIR/.venv"

usage() {
    cat <<'EOF'
用法: ./start.sh [command]

命令:
  start     后台启动监听程序（默认）
  stop      停止后台运行的程序
  restart   重启程序
  status    查看运行状态
  groups    列出所有群聊及正确 ID
  foreground 前台运行（调试用，可交互输入验证码）
  install   创建虚拟环境并安装依赖
  login     前台登录（首次使用时运行，输入验证码）

示例:
  ./start.sh install    # 首次安装
  ./start.sh login      # 首次登录
  ./start.sh start      # 后台启动
  ./start.sh status     # 查看状态
EOF
}

ensure_venv() {
    if [[ ! -d "$VENV_DIR" ]]; then
        echo "虚拟环境不存在，请先运行: ./start.sh install"
        exit 1
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
}

do_install() {
    echo ">>> 创建 Python 虚拟环境..."
    python3 -m venv "$VENV_DIR"
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip
    pip install -r requirements.txt
    echo ">>> 安装完成"
    echo ">>> 请复制 config.example.env 为 .env 并填写配置"
    echo ">>> 然后运行: ./start.sh login"
}

do_login() {
    ensure_venv
    if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
        echo "未找到 .env 文件，请先复制 config.example.env 为 .env 并填写"
        exit 1
    fi
    mkdir -p "$LOG_DIR"
    python telegram_listener.py
}

do_start() {
    ensure_venv
    if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
        echo "未找到 .env 文件，请先复制 config.example.env 为 .env 并填写"
        exit 1
    fi

    if [[ -f "$PID_FILE" ]]; then
        OLD_PID=$(cat "$PID_FILE")
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "程序已在运行 (PID: $OLD_PID)"
            exit 0
        fi
        rm -f "$PID_FILE"
    fi

    mkdir -p "$LOG_DIR"
    echo ">>> 后台启动 Telegram 监听程序..."
    nohup python telegram_listener.py >> "$LOG_DIR/nohup.out" 2>&1 &
    echo $! > "$PID_FILE"
    echo ">>> 已启动 (PID: $(cat "$PID_FILE"))"
    echo ">>> 日志: $LOG_DIR/listener.log"
}

do_stop() {
    if [[ ! -f "$PID_FILE" ]]; then
        echo "程序未运行"
        exit 0
    fi
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo ">>> 停止程序 (PID: $PID)..."
        kill "$PID"
        sleep 1
        if kill -0 "$PID" 2>/dev/null; then
            kill -9 "$PID" 2>/dev/null || true
        fi
        echo ">>> 已停止"
    else
        echo "进程不存在，清理 PID 文件"
    fi
    rm -f "$PID_FILE"
}

do_status() {
    if [[ -f "$PID_FILE" ]]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "运行中 (PID: $PID)"
            exit 0
        fi
    fi
    echo "未运行"
    exit 1
}

do_restart() {
    do_stop || true
    do_start
}

CMD="${1:-start}"

case "$CMD" in
    install)    do_install ;;
    login)      do_login ;;
    foreground) do_login ;;
    start)      do_start ;;
    stop)       do_stop ;;
    restart)    do_restart ;;
    status)     do_status ;;
    groups)     ensure_venv; python list_groups.py ;;
    -h|--help|help) usage ;;
    *)
        echo "未知命令: $CMD"
        usage
        exit 1
        ;;
esac
