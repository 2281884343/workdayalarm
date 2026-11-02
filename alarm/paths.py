# -*- coding: utf-8 -*-
"""运行目录解析。

绿色免安装：优先把配置/日志/状态放在程序所在目录；若该目录不可写
（例如被放进 Program Files），自动回落到 %APPDATA%\\WorkdayAlarm。

也可以设置环境变量 WORKDAY_ALARM_HOME 强制指定目录。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from . import APP_NAME

_ENV_HOME = "WORKDAY_ALARM_HOME"

_home_cache: Path | None = None


def app_dir() -> Path:
    """程序所在目录（源码运行时是仓库根目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def home_dir() -> Path:
    """配置、日志、状态文件的存放目录。"""
    global _home_cache
    if _home_cache is not None:
        return _home_cache

    env = os.environ.get(_ENV_HOME, "").strip()
    if env:
        _home_cache = Path(os.path.expandvars(os.path.expanduser(env)))
        _home_cache.mkdir(parents=True, exist_ok=True)
        return _home_cache

    candidate = app_dir()
    if _is_writable(candidate):
        _home_cache = candidate
        return _home_cache

    fallback = Path(os.environ.get("APPDATA", tempfile.gettempdir())) / APP_NAME
    fallback.mkdir(parents=True, exist_ok=True)
    _home_cache = fallback
    return _home_cache


def reset_home_cache() -> None:
    """测试用：清除缓存。"""
    global _home_cache
    _home_cache = None


def config_path() -> Path:
    return home_dir() / "config.json"


def state_path() -> Path:
    return home_dir() / "state.json"


def log_dir() -> Path:
    d = home_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path() -> Path:
    return log_dir() / "workday_alarm.log"


def data_dir() -> Path:
    """节假日数据目录：程序目录下的 data，其次打包进 exe 的资源目录。"""
    d = app_dir() / "data"
    if d.is_dir():
        return d
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "data"
        if bundled.is_dir():
            return bundled
    return d


def bundled_data_files() -> list[Path]:
    """随程序发布的 holidays.json 候选位置（打包运行时优先 _MEIPASS）。"""
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "data" / "holidays.json")
    candidates.append(app_dir() / "data" / "holidays.json")
    return candidates


def data_update_path() -> Path:
    """自动更新（联网/手动）写入的数据文件。

    放在 home 目录下：即使程序被放进 Program Files，也不会因为只读而更新失败。
    """
    return home_dir() / "data" / "holidays.json"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))
