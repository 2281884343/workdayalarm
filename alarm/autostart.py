# -*- coding: utf-8 -*-
"""开机自启：写入 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run（免管理员权限）。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "WorkdayAlarm"


def _registry():
    import winreg  # 延迟导入，便于在非 Windows 环境做静态检查

    return winreg


def command_line() -> str:
    """当前进程对应的启动命令行。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'

    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    runner = pythonw if pythonw.is_file() else exe
    script = Path(__file__).resolve().parent.parent / "main.py"
    return f'"{runner}" "{script}"'


def is_enabled() -> bool:
    try:
        winreg = _registry()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(str(value).strip())
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.debug("读取开机自启状态失败：%s", exc)
        return False


def set_enabled(flag: bool) -> tuple[bool, str]:
    """设置开机自启，返回 (是否成功, 说明)。"""
    try:
        winreg = _registry()
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if flag:
                cmd = command_line()
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, cmd)
                log.info("已开启开机自启：%s", cmd)
                return True, "已开启开机自启"
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
            log.info("已关闭开机自启")
            return True, "已关闭开机自启"
    except OSError as exc:
        log.error("设置开机自启失败：%s", exc)
        return False, f"设置开机自启失败：{exc}"
