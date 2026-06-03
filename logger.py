#!/usr/bin/env python3
"""足球自媒体 — 结构化日志模块

用法:
    from logger import log
    log.info("正在采集比赛数据...")
    log.warning("无比赛数据，启用回退模式")
    log.error("API调用失败", exc_info=True)

日志同时输出到控制台和 output/logs/ 目录。
"""

import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

# Determine log directory
LOG_DIR = Path(os.environ.get("OUTPUT_DIR", Path(__file__).parent / "output")) / "logs"

# Ensure log directory exists
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Create logger
log = logging.getLogger("football_auto_publish")
log.setLevel(logging.DEBUG)

# Console handler — INFO and above
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_fmt = logging.Formatter("%(asctime)s [%(levelname)-5s] %(message)s", datefmt="%H:%M:%S")
console_handler.setFormatter(console_fmt)
log.addHandler(console_handler)

# File handler — DEBUG and above, daily rotating (new file per day)
today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
file_handler = logging.FileHandler(LOG_DIR / f"orchestrator_{today}.log", encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_fmt = logging.Formatter("%(asctime)s [%(levelname)-5s] %(name)s:%(lineno)d — %(message)s")
file_handler.setFormatter(file_fmt)
log.addHandler(file_handler)

# Suppress noisy third-party loggers
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("playwright").setLevel(logging.WARNING)

# Convenience: log startup info
log.info(f"日志系统已初始化，文件输出: {file_handler.baseFilename}")
