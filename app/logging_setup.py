"""统一日志配置：控制台 + 文件，UTF-8 编码，避免 Windows 控制台中文乱码。"""
from __future__ import annotations

import logging
import sys
from typing import Optional

from .config import PROJECT_ROOT, Config


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def setup_logging(config: Config) -> None:
    level_name = str(config.get("logging.level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = config.get("logging.file", "logs/qqbot.log") or ""

    # Windows 控制台默认 GBK，强制 UTF-8 以避免中文日志报错
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    root = logging.getLogger()
    root.setLevel(level)
    # 清空已有 handler，避免重复输出
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file:
        path = PROJECT_ROOT / log_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(path, encoding="utf-8")
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except OSError as exc:
            logging.getLogger("logging_setup").error("无法创建日志文件 %s：%s", path, exc)
