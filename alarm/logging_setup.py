# -*- coding: utf-8 -*-
"""日志初始化：滚动文件 + 控制台（源码运行时）。"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_MAX_BYTES = 1024 * 1024


def setup_logging(log_file: Path, level: str = "INFO", console: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=_MAX_BYTES, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)

    if console and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(logging.DEBUG)
        stream.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(stream)

    # 第三方库降噪
    logging.getLogger("chinese_calendar").setLevel(logging.WARNING)
