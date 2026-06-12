"""日志配置：默认不记消息正文，支持轮转与定时清理。"""

from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

_log_verbose = False
_read_debug = False


def is_log_verbose() -> bool:
    return _log_verbose


def set_log_verbose(enabled: bool) -> None:
    global _log_verbose
    _log_verbose = enabled


def is_read_debug() -> bool:
    return _read_debug


def set_read_debug(enabled: bool) -> None:
    global _read_debug
    _read_debug = enabled


def read_log(logger: logging.Logger, msg: str, *args, **kwargs) -> None:
    """已读同步关键日志：READ_DEBUG 时 WARNING（严格模式可见），否则 INFO。"""
    trace_log(logger, "已读", msg, *args, **kwargs)


def read_skip(logger: logging.Logger, msg: str, *args, **kwargs) -> None:
    """已读同步跳过原因：仅 READ_DEBUG 时输出 WARNING。"""
    trace_skip(logger, "已读", msg, *args, **kwargs)


def forward_log(logger: logging.Logger, msg: str, *args, **kwargs) -> None:
    """转发关键日志：READ_DEBUG 时 WARNING，否则 INFO。"""
    trace_log(logger, "转发", msg, *args, **kwargs)


def forward_skip(logger: logging.Logger, msg: str, *args, **kwargs) -> None:
    """转发跳过原因：仅 READ_DEBUG 时输出 WARNING。"""
    trace_skip(logger, "转发", msg, *args, **kwargs)


def trace_log(logger: logging.Logger, tag: str, msg: str, *args, **kwargs) -> None:
    if _read_debug:
        logger.warning(f"[{tag}] " + msg, *args, **kwargs)
    else:
        logger.info(msg, *args, **kwargs)


def trace_skip(logger: logging.Logger, tag: str, msg: str, *args, **kwargs) -> None:
    if _read_debug:
        logger.warning(f"[{tag}·跳过] " + msg, *args, **kwargs)


def setup_logging(
    level: str,
    *,
    log_dir: Path | None = None,
    max_mb: int = 5,
    backup_count: int = 2,
) -> Path:
    """初始化日志：文件轮转 + 控制台输出。"""
    directory = log_dir or (BASE_DIR / "logs")
    directory.mkdir(exist_ok=True)
    log_file = directory / "listener.log"

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.WARNING))

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max(1, max_mb) * 1024 * 1024,
        backupCount=max(0, backup_count),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    return log_file


def cleanup_old_logs(
    log_dir: Path | None = None,
    retention_days: int = 3,
) -> int:
    """删除超过保留期的 listener.log 及轮转备份。"""
    if retention_days <= 0:
        return 0

    directory = log_dir or (BASE_DIR / "logs")
    if not directory.is_dir():
        return 0

    cutoff = time.time() - retention_days * 86400
    removed = 0
    for path in directory.glob("listener.log*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
