# -*- coding: utf-8 -*-
"""配置读写与校验。

配置文件为纯 JSON，放在程序目录（或 %APPDATA%\\WorkdayAlarm）。用户只改文件，
无需重新打包；格式错误时自动降级到默认值并在日志中记录原因，绝不崩溃。
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

MODE_TOAST = "toast"
MODE_POPUP = "popup"
_VALID_MODES = (MODE_TOAST, MODE_POPUP)


# --------------------------------------------------------------------- 数据模型
@dataclass
class Reminder:
    """一条定时提醒。"""

    id: str
    time: str                      # "HH:MM"
    title: str
    message: str = ""
    enabled: bool = True
    mode: str | None = None        # None 表示跟随全局
    sound: str | None = None       # None 表示跟随全局

    @property
    def hour(self) -> int:
        return int(self.time.split(":")[0])

    @property
    def minute(self) -> int:
        return int(self.time.split(":")[1])

    def due_at(self, day: _dt.date) -> _dt.datetime:
        return _dt.datetime.combine(day, _dt.time(self.hour, self.minute))

    def time_label(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"


@dataclass
class AppConfig:
    enabled: bool = True
    poll_interval_seconds: int = 10
    catch_up_minutes: int = 30
    notification_mode: str = MODE_TOAST
    popup_timeout_seconds: int = 30
    notification_timeout_seconds: int = 10
    sound: str = "default"          # default / none / 自定义 wav 路径
    autostart: bool = False
    auto_update_data: bool = True   # 每年 12/30 起自动联网补齐下一年数据
    log_level: str = "INFO"
    unknown_year_policy: str = "weekday"   # weekday / holiday
    reminders: list[Reminder] = field(default_factory=list)
    # 运行期信息：配置文件里不允许出现，仅用于展示
    source_path: str = ""
    load_errors: list[str] = field(default_factory=list)     # 会导致行为不符预期的硬错误
    load_warnings: list[str] = field(default_factory=list)   # 仅提示，不影响运行

    def reminder_by_id(self, rid: str) -> Reminder | None:
        for item in self.reminders:
            if item.id == rid:
                return item
        return None

    def mode_for(self, reminder: Reminder) -> str:
        mode = reminder.mode or self.notification_mode
        return mode if mode in _VALID_MODES else MODE_TOAST

    def sound_for(self, reminder: Reminder) -> str:
        value = reminder.sound if reminder.sound is not None else self.sound
        return (value or "default").strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "poll_interval_seconds": self.poll_interval_seconds,
            "catch_up_minutes": self.catch_up_minutes,
            "notification_mode": self.notification_mode,
            "popup_timeout_seconds": self.popup_timeout_seconds,
            "notification_timeout_seconds": self.notification_timeout_seconds,
            "sound": self.sound,
            "autostart": self.autostart,
            "auto_update_data": self.auto_update_data,
            "log_level": self.log_level,
            "unknown_year_policy": self.unknown_year_policy,
            "reminders": [
                {
                    "id": r.id,
                    "enabled": r.enabled,
                    "time": r.time,
                    "title": r.title,
                    "message": r.message,
                    "mode": r.mode,
                    "sound": r.sound,
                }
                for r in self.reminders
            ],
        }


DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "poll_interval_seconds": 10,
    "catch_up_minutes": 30,
    "notification_mode": MODE_TOAST,
    "popup_timeout_seconds": 30,
    "notification_timeout_seconds": 10,
    "sound": "default",
    "autostart": False,
    "auto_update_data": True,
    "log_level": "INFO",
    "unknown_year_policy": "weekday",
    "reminders": [
        {
            "id": "morning",
            "enabled": True,
            "time": "09:30",
            "title": "上班提醒",
            "message": "先看一眼今天的待办清单，规划最重要的一件事。",
            "mode": None,
            "sound": None,
        },
        {
            "id": "standup",
            "enabled": True,
            "time": "10:00",
            "title": "站会提醒",
            "message": "10:00 站会，准备好昨天完成项与今天计划。",
            "mode": None,
            "sound": None,
        },
        {
            "id": "offwork",
            "enabled": False,
            "time": "18:30",
            "title": "下班提醒",
            "message": "收尾：提交代码、写工作日志、确认明天第一件事。",
            "mode": None,
            "sound": None,
        },
    ],
}

_SAMPLE_NOTES: dict[str, Any] = {
    "_comment": "工作日提醒配置文件。改完可在托盘菜单点“重新加载配置”，或退出后重新打开。",
    "_fields": {
        "enabled": "全局开关：false 时所有提醒都不触发",
        "poll_interval_seconds": "轮询间隔（秒），建议 10-60",
        "catch_up_minutes": "补偿窗口（分钟）：休眠/重启后若错过时间点且误差在窗口内，仍会补发提醒",
        "notification_mode": "toast=系统通知， popup=置顶弹窗",
        "popup_timeout_seconds": "置顶弹窗自动关闭秒数，0 表示不自动关闭",
        "notification_timeout_seconds": "系统通知停留秒数",
        "sound": "default=系统提示音， none=静音，也可以填 wav 文件绝对路径",
        "autostart": "是否写入开机自启（托盘菜单里也可以切换）",
        "auto_update_data": "每年 12 月 30 日之后若缺少下一年数据，是否自动联网更新；false 则需手动点托盘菜单「更新节假日数据」",
        "log_level": "DEBUG / INFO / WARNING / ERROR",
        "unknown_year_policy": "当年节假日数据缺失时的兜底规则：weekday=周一至周五上班，holiday=全部按休息日",
        "reminders": "提醒列表，可任意增删；time 为 24 小时制 HH:MM；mode/sound 填 null 表示跟随全局",
    },
    "_data": "节假日数据见 data/holidays.json，或用 pip 升级 chinesecalendar 包",
}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是", "开")
    return default


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, result))


def _normalize_time(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().replace("：", ":")
    match = _TIME_RE.match(text)
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"


def _normalize_id(value: Any, index: int, used: set[str]) -> str:
    base = re.sub(r"[^0-9A-Za-z_\-]+", "_", str(value or "").strip()).strip("_")
    if not base:
        base = f"reminder_{index + 1}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def parse_config(raw: dict[str, Any], source_path: str = "") -> AppConfig:
    """把原始 dict 解析成 AppConfig，并收集校验问题。"""
    errors: list[str] = []
    warnings: list[str] = []
    cfg = AppConfig(source_path=source_path)

    cfg.enabled = _as_bool(raw.get("enabled"), True)
    cfg.poll_interval_seconds = _as_int(raw.get("poll_interval_seconds"), 10, 5, 3600)
    cfg.catch_up_minutes = _as_int(raw.get("catch_up_minutes"), 30, 0, 1440)
    cfg.popup_timeout_seconds = _as_int(raw.get("popup_timeout_seconds"), 30, 0, 3600)
    cfg.notification_timeout_seconds = _as_int(raw.get("notification_timeout_seconds"), 10, 3, 120)
    cfg.autostart = _as_bool(raw.get("autostart"), False)
    cfg.auto_update_data = _as_bool(raw.get("auto_update_data"), True)

    mode = str(raw.get("notification_mode", MODE_TOAST)).strip().lower()
    if mode not in _VALID_MODES:
        errors.append(f"notification_mode={mode!r} 非法，已回退为 toast（可选：toast / popup）")
        mode = MODE_TOAST
    cfg.notification_mode = mode

    sound = raw.get("sound", "default")
    cfg.sound = str(sound).strip() if sound not in (None, "") else "default"

    level = str(raw.get("log_level", "INFO")).strip().upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        errors.append(f"log_level={level!r} 非法，已回退为 INFO")
        level = "INFO"
    cfg.log_level = level

    policy = str(raw.get("unknown_year_policy", "weekday")).strip().lower()
    if policy not in ("weekday", "holiday"):
        errors.append(f"unknown_year_policy={policy!r} 非法，已回退为 weekday")
        policy = "weekday"
    cfg.unknown_year_policy = policy

    reminders_raw = raw.get("reminders")
    if reminders_raw is None:
        reminders_raw = []
    if not isinstance(reminders_raw, list):
        errors.append("reminders 必须是数组，已忽略")
        reminders_raw = []

    used_ids: set[str] = set()
    for index, item in enumerate(reminders_raw):
        if not isinstance(item, dict):
            errors.append(f"第 {index + 1} 条提醒不是对象，已忽略")
            continue

        time_text = _normalize_time(item.get("time"))
        if time_text is None:
            errors.append(f"第 {index + 1} 条提醒 time={item.get('time')!r} 非法（需 HH:MM），已忽略")
            continue

        title = str(item.get("title") or "").strip()
        if not title:
            title = f"提醒 {time_text}"
            errors.append(f"第 {index + 1} 条提醒缺少 title，已使用“{title}”")

        item_mode = item.get("mode")
        if item_mode is not None:
            item_mode = str(item_mode).strip().lower()
            if item_mode not in _VALID_MODES:
                errors.append(f"提醒 {title} 的 mode={item_mode!r} 非法，已跟随全局")
                item_mode = None

        item_sound = item.get("sound")
        if item_sound is not None:
            item_sound = str(item_sound).strip()
            if not item_sound:
                item_sound = None

        cfg.reminders.append(
            Reminder(
                id=_normalize_id(item.get("id"), index, used_ids),
                time=time_text,
                title=title,
                message=str(item.get("message") or "").strip(),
                enabled=_as_bool(item.get("enabled"), True),
                mode=item_mode,
                sound=item_sound,
            )
        )

    unique_times = {}
    for reminder in cfg.reminders:
        unique_times.setdefault(reminder.time, []).append(reminder.title)
    for time_text, titles in unique_times.items():
        if len(titles) > 1:
            warnings.append(f"{time_text} 有 {len(titles)} 条提醒同时触发：{'、'.join(titles)}")

    if not cfg.reminders:
        warnings.append("reminders 为空，不会有任何提醒")

    cfg.load_errors = errors
    cfg.load_warnings = warnings
    for message in errors:
        log.warning("配置问题：%s", message)
    return cfg


def default_config_dict() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_CONFIG)


def load_config(path: Path) -> AppConfig:
    """读取配置；文件不存在时创建默认配置，文件损坏时使用默认值并备份坏文件。"""
    if not path.is_file():
        try:
            write_sample_config(path)
            log.info("未找到配置文件，已生成带说明的示例配置：%s", path)
        except OSError as exc:
            log.error("生成示例配置失败（%s），本次使用内置默认值：%s", path, exc)
            return parse_config(default_config_dict(), str(path))

    try:
        text = path.read_text(encoding="utf-8-sig")
        raw = json.loads(text)
    except (OSError, ValueError) as exc:
        log.error("配置文件解析失败（%s）：%s，已改用默认配置", path, exc)
        backup = path.with_suffix(path.suffix + ".broken")
        try:
            path.replace(backup)
            log.error("损坏的配置文件已备份为 %s", backup)
        except OSError:
            pass
        cfg = parse_config(default_config_dict(), str(path))
        cfg.load_errors.insert(0, f"配置文件解析失败：{exc}")
        save_config(path, cfg)
        return cfg

    if not isinstance(raw, dict):
        log.error("配置文件根节点必须是对象，已改用默认配置")
        cfg = parse_config(default_config_dict(), str(path))
        cfg.load_errors.insert(0, "配置文件根节点必须是对象")
        return cfg

    return parse_config(raw, str(path))


def save_config(path: Path, cfg: AppConfig) -> None:
    """原子写入配置。"""
    text = json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2) + "\n"
    _atomic_write(path, text)


def write_sample_config(path: Path, force: bool = False) -> bool:
    """写一份带注释的示例配置（JSON 里用 _comment 字段承载说明）。"""
    if path.exists() and not force:
        return False
    data = dict(_SAMPLE_NOTES)
    data.update(default_config_dict())
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return True


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except OSError as exc:
        log.error("写入文件失败 %s：%s", path, exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass
