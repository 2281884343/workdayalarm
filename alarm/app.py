# -*- coding: utf-8 -*-
"""应用主装配：把配置、日历、状态、调度、托盘、弹窗接到一起。"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import queue
import threading
import time
from pathlib import Path
from tkinter import Tk

from . import APP_TITLE, __version__, autostart as autostart_mod, dataupdate
from .config import MODE_POPUP, AppConfig, load_config, save_config, write_sample_config
from .logging_setup import setup_logging
from .notifier import PopupManager, play_sound
from .paths import (
    bundled_data_files,
    config_path,
    data_dir,
    data_update_path,
    home_dir,
    log_dir,
    log_path,
    state_path,
)
from .scheduler import Scheduler
from .state import TriggerState
from .tray import (
    CMD_ABOUT,
    CMD_AUTOSTART,
    CMD_CONFIG,
    CMD_LOG,
    CMD_QUIT,
    CMD_RELOAD,
    CMD_STATUS,
    CMD_TEST,
    CMD_TOGGLE,
    CMD_UPDATE,
    TrayIcon,
)
from .workday import WorkdayCalendar

log = logging.getLogger(__name__)

_MIN_POLL = 5
_MAX_POLL = 300


def message_box(text: str, title: str = APP_TITLE, style: int = 0x10) -> None:
    """无控制台场景下的兜底提示（style 0x10=错误图标，0x40=信息图标）。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, style)
    except Exception:
        print(f"{title}: {text}")


def build_calendar(cfg: AppConfig) -> WorkdayCalendar:
    """按优先级装配节假日数据源。"""
    files: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    def add(path: Path, source: str) -> None:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen or not path.is_file():
            return
        seen.add(key)
        files.append((path, source))

    # 优先级：用户手写覆盖 > 自动/手动更新的数据 > 随包发布的历年数据
    add(home_dir() / "holidays.local.json", "本地覆盖")
    add(data_update_path(), "更新数据")
    for candidate in bundled_data_files():
        add(candidate, "内置数据")
    return WorkdayCalendar(data_files=files, unknown_year_policy=cfg.unknown_year_policy)


class AlarmApp:
    def __init__(self, config_file: Path | None = None, console: bool = False) -> None:
        self.config_file = Path(config_file) if config_file else config_path()
        self.console = console
        self.cfg: AppConfig = load_config(self.config_file)
        self._commands: queue.Queue = queue.Queue()
        self._tray: TrayIcon | None = None
        self._tray_ok = False
        self._root: Tk | None = None
        self._popups: PopupManager | None = None
        self._tick_job: str | None = None
        self._command_job: str | None = None
        self._quitting = False
        self._last_tray_key: tuple | None = None
        self._last_state_day: _dt.date | None = None
        self._update_results: queue.Queue = queue.Queue()
        self._update_running = False
        self._annual_update_checked: _dt.date | None = None

        self._calendar = build_calendar(self.cfg)
        self._state = TriggerState(state_path())
        self._scheduler = Scheduler(self._calendar, self._state)

    # ------------------------------------------------------------------ 启动
    def run(self) -> int:
        setup_logging(log_path(), self.cfg.log_level, console=self.console)
        log.info(
            "启动 %s v%s｜配置 %s｜数据目录 %s｜日志 %s",
            APP_TITLE,
            __version__,
            self.config_file,
            home_dir(),
            log_path(),
        )
        log.info("节假日数据覆盖年份：%s", self._calendar.coverage() or "仅使用兜底规则")

        self._root = Tk()
        self._root.withdraw()
        self._root.title(APP_TITLE)
        self._popups = PopupManager(self._root)

        self._sync_autostart()
        self._start_tray()
        if not self._tray_ok:
            message_box(
                "无法创建系统托盘图标，程序退出。\n\n请检查是否有安全软件拦截，或查看日志：\n" + str(log_path())
            )
            self._shutdown()
            return 1

        self._refresh_tray(force=True)
        self._report_config_errors()
        self._schedule_tick(immediate=True)
        self._poll_commands()
        try:
            self._root.mainloop()
        except KeyboardInterrupt:  # pragma: no cover - 控制台调试
            log.info("收到 Ctrl+C，准备退出")
        finally:
            self._shutdown()
        return 0

    def _sync_autostart(self) -> None:
        if self.cfg.autostart and not autostart_mod.is_enabled():
            ok, msg = autostart_mod.set_enabled(True)
            log.info("按配置开启开机自启：%s", msg if not ok else "成功")

    def _start_tray(self) -> None:
        self._tray = TrayIcon(APP_TITLE, on_command=self._commands.put)
        self._tray_ok = self._tray.start(timeout=5.0)
        if not self._tray_ok:
            log.error("托盘图标创建失败")

    def _report_config_errors(self) -> None:
        for warning in self.cfg.load_warnings:
            log.warning("配置提示：%s", warning)
        if not self.cfg.load_errors:
            if self.cfg.load_warnings:
                self._notify("配置提示", "\n".join(self.cfg.load_warnings[:3]))
            return
        detail = "\n".join(self.cfg.load_errors[:4])
        log.warning("配置存在问题：%s", "；".join(self.cfg.load_errors))
        self._notify(
            "配置文件有问题，已按默认值处理",
            detail + f"\n\n配置文件：{self.config_file}",
            force_popup=True,
        )

    # ------------------------------------------------------------------ 定时轮询
    def _schedule_tick(self, immediate: bool = False) -> None:
        if self._quitting or self._root is None:
            return
        if self._tick_job is not None:
            try:
                self._root.after_cancel(self._tick_job)
            except Exception:
                pass
            self._tick_job = None

        if immediate:
            delay = 0.4
        else:
            interval = max(_MIN_POLL, min(_MAX_POLL, self.cfg.poll_interval_seconds))
            now = time.time()
            # 对齐到 interval 的整数倍，避免轮询漂移导致整分提醒被推迟
            delay = interval - (now % interval) + 0.02
            if delay <= 0.1:
                delay += interval
        self._tick_job = self._root.after(max(1, int(delay * 1000)), self._on_tick)

    def _on_tick(self) -> None:
        self._tick_job = None
        if self._quitting:
            return
        due = []
        try:
            due = self._scheduler.tick(self.cfg)
        except Exception:
            log.exception("轮询失败")
        for item in due:
            try:
                self._fire(item)
            except Exception:
                log.exception("提醒展示失败：[%s]", item.reminder.id)

        today = _dt.date.today()
        try:
            if due or self._last_state_day != today:
                self._state.prune(today)
                self._state.save()
                self._last_state_day = today
        except Exception:
            log.exception("保存状态失败")

        self._refresh_tray()
        self._maybe_annual_update()
        self._schedule_tick()

    def _fire(self, due) -> None:
        reminder = due.reminder
        title = reminder.title or APP_TITLE
        message = reminder.message or "该处理今天的事情了。"
        footer = f"{reminder.time_label()} · {due.day_info.summary()}"
        play_sound(self.cfg.sound_for(reminder))
        if self.cfg.mode_for(reminder) == MODE_POPUP or not self._tray_ok:
            assert self._popups is not None
            self._popups.show(title, message, footer, self.cfg.popup_timeout_seconds)
        else:
            assert self._tray is not None
            self._tray.show_balloon(title, message, "info", self.cfg.notification_timeout_seconds)

    # ------------------------------------------------------------------ 数据更新
    def _maybe_annual_update(self) -> None:
        """每年 12 月 30 日之后，如果还没有下一年的数据，自动联网补齐。"""
        if not self.cfg.auto_update_data or self._update_running:
            return
        today = _dt.date.today()
        if self._annual_update_checked == today:
            return
        year = dataupdate.needs_annual_update(self._calendar, today)
        if year is None:
            return
        self._annual_update_checked = today
        log.info("缺少 %s 年节假日数据，开始自动更新", year)
        self._start_update([year])

    def _update_data(self) -> None:
        """托盘菜单「更新节假日数据」：补齐当前年份与下一年。"""
        today = _dt.date.today()
        years = dataupdate.missing_years(self._calendar, today)
        if not years:
            years = [today.year + 1]
        self._start_update(years)

    def _start_update(self, years: list[int]) -> None:
        if self._update_running:
            self._notify("正在更新", "节假日数据更新中，请稍候…")
            return
        self._update_running = True
        self._notify(
            "开始更新节假日数据",
            f"正在从 PyPI 获取 {('、'.join(str(y) for y in years))} 年数据，结果会在稍后提示。",
        )
        thread = threading.Thread(target=self._update_worker, args=(list(years),), name="data-update", daemon=True)
        thread.start()

    def _update_worker(self, years: list[int]) -> None:
        target = data_update_path()
        try:
            ok, message = dataupdate.update_file(target, years)
        except Exception as exc:  # 兜底，避免线程静默死掉
            log.exception("节假日数据更新异常")
            ok, message = False, f"更新时发生异常：{exc}"
        log.info("节假日数据更新结果：ok=%s｜%s", ok, message)
        self._update_results.put((ok, message, years))

    def _drain_update_results(self) -> None:
        while True:
            try:
                ok, message, years = self._update_results.get_nowait()
            except queue.Empty:
                return
            self._update_running = False
            self._apply_update_result(ok, message, years)

    def _apply_update_result(self, ok: bool, message: str, years: list[int]) -> None:
        if not ok:
            years_text = " ".join(str(year) for year in years)
            hint = chr(10).join(
                [
                    message,
                    "",
                    "可手动更新：在项目目录执行",
                    f"  python tools/gen_holidays.py --years {years_text} --out data/holidays.json",
                    "或手工编写 holidays.local.json（详见 README「年度数据更新」）。",
                ]
            )
            self._notify("节假日数据更新失败", hint, force_popup=True)
            return
        self._calendar = build_calendar(self.cfg)
        self._scheduler.calendar = self._calendar
        self._annual_update_checked = _dt.date.today()
        self._refresh_tray(force=True)
        self._notify("节假日数据已更新", message)

    # ------------------------------------------------------------------ 托盘刷新
    def _refresh_tray(self, force: bool = False) -> None:
        if not self._tray_ok or self._tray is None:
            return
        status = self._scheduler.status_line(self.cfg)
        autostart_on = autostart_mod.is_enabled()
        key = (self.cfg.enabled, autostart_on, status)
        if not force and key == self._last_tray_key:
            return
        self._last_tray_key = key
        tooltip = f"{APP_TITLE}\n{status}"
        self._tray.update_state(self.cfg.enabled, autostart_on, status, tooltip=tooltip)

    def _notify(self, title: str, message: str, force_popup: bool = False) -> None:
        if force_popup or not self._tray_ok:
            if self._popups is not None:
                self._popups.show(title, message, "", self.cfg.popup_timeout_seconds)
            else:
                message_box(message, title, 0x40)
            return
        assert self._tray is not None
        self._tray.show_balloon(title, message, "info", self.cfg.notification_timeout_seconds)

    # ------------------------------------------------------------------ 托盘命令
    def _poll_commands(self) -> None:
        if self._root is None or self._quitting:
            return
        self._drain_update_results()
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle(command)
            except Exception:
                log.exception("处理托盘命令失败：%r", command)
        self._command_job = self._root.after(120, self._poll_commands)

    def _handle(self, command: str) -> None:
        HANDLERS = {
            CMD_TOGGLE: self._toggle_enabled,
            CMD_TEST: self._test_notify,
            CMD_CONFIG: self._open_config,
            CMD_RELOAD: self._reload_config,
            CMD_UPDATE: self._update_data,
            CMD_AUTOSTART: self._toggle_autostart,
            CMD_LOG: self._open_log,
            CMD_STATUS: self._show_status,
            CMD_ABOUT: self._show_about,
            CMD_QUIT: self._quit,
        }
        handler = HANDLERS.get(command)
        if handler is None:
            log.warning("未知托盘命令：%s", command)
            return
        handler()

    def _toggle_enabled(self) -> None:
        self.cfg.enabled = not self.cfg.enabled
        self._save_config()
        self._refresh_tray(force=True)
        if self.cfg.enabled:
            self._notify("提醒已启用", self._scheduler.status_line(self.cfg))
        else:
            self._notify("提醒已暂停", "工作日判定仍在后台进行，但不会再弹出提醒。")

    def _test_notify(self) -> None:
        reminder = next((r for r in self.cfg.reminders if r.enabled), None)
        if reminder is None:
            reminder = next(iter(self.cfg.reminders), None)
        today = _dt.date.today()
        info = self._calendar.info(today)
        title = reminder.title if reminder and reminder.title else "测试提醒"
        message = (reminder.message if reminder and reminder.message else "如果你看到这条消息，说明提醒通道正常。")
        message += f"\n\n今日判定：{info.summary()}"
        mode = self.cfg.mode_for(reminder) if reminder else self.cfg.notification_mode
        play_sound(self.cfg.sound_for(reminder) if reminder else self.cfg.sound)
        if mode == MODE_POPUP or not self._tray_ok:
            assert self._popups is not None
            self._popups.show(title, message, f"测试 · {info.summary()}", self.cfg.popup_timeout_seconds)
        else:
            assert self._tray is not None
            self._tray.show_balloon(title, message, "info", self.cfg.notification_timeout_seconds)

    def _open_config(self) -> None:
        try:
            if not self.config_file.is_file():
                write_sample_config(self.config_file)
            os.startfile(str(self.config_file))  # type: ignore[attr-defined]
            log.info("已打开配置文件：%s", self.config_file)
        except Exception as exc:
            log.exception("打开配置文件失败")
            self._notify("打开配置文件失败", f"{exc}\n\n路径：{self.config_file}", force_popup=True)

    def _reload_config(self) -> None:
        cfg = load_config(self.config_file)
        self.cfg = cfg
        self._calendar = build_calendar(cfg)
        self._scheduler.calendar = self._calendar
        try:
            setup_logging(log_path(), cfg.log_level, console=self.console)
        except Exception:
            log.exception("重新初始化日志失败")
        log.info("配置已重新加载：%s", self.config_file)
        self._refresh_tray(force=True)
        self._schedule_tick()
        self._report_config_errors()
        self._notify("配置已重新加载", self._scheduler.status_line(self.cfg))

    def _toggle_autostart(self) -> None:
        target = not autostart_mod.is_enabled()
        ok, msg = autostart_mod.set_enabled(target)
        if ok:
            self.cfg.autostart = target
            self._save_config()
        self._refresh_tray(force=True)
        self._notify("开机自启", msg)

    def _open_log(self) -> None:
        try:
            os.startfile(str(log_dir()))  # type: ignore[attr-defined]
        except Exception as exc:
            log.exception("打开日志目录失败")
            self._notify("打开日志目录失败", f"{exc}\n\n路径：{log_dir()}", force_popup=True)

    def _show_status(self) -> None:
        years = self._calendar.coverage()
        cover = f"{years[0]}–{years[-1]}" if years else "无内置数据（使用兜底规则）"
        info = self._calendar.info(_dt.date.today())
        lines = [
            f"今日：{info.summary()}",
            f"判定来源：{info.source}",
            f"数据覆盖：{cover}",
            f"提醒状态：{'已启用' if self.cfg.enabled else '已暂停'}",
            f"下次提醒：{self._scheduler.status_line(self.cfg)}",
            f"数据文件：{data_update_path()}",
        ]
        todo = [f"· {r.time_label()} {r.title}{'' if r.enabled else '（停用）'}" for r in self.cfg.reminders]
        text = "\n".join(lines) + ("\n\n提醒列表：\n" + "\n".join(todo) if todo else "\n\n尚未配置任何提醒")
        log.info("今日状态：%s", text.replace("\n", "｜"))
        self._notify("今日状态", text, force_popup=True)

    def _show_about(self) -> None:
        text = (
            f"{APP_TITLE} v{__version__}\n\n"
            "按国务院法定节假日与调休安排判定真实工作日，\n"
            "只在工作日触发你设定的提醒。\n\n"
            f"配置文件：{self.config_file}\n"
            f"日志目录：{log_dir()}\n"
            f"数据目录：{data_dir()}"
        )
        self._notify(f"关于 {APP_TITLE}", text, force_popup=True)

    def _quit(self) -> None:
        if self._quitting:
            return
        log.info("从托盘菜单退出")
        self._quitting = True
        self._shutdown()
        if self._root is not None:
            try:
                self._root.quit()
            except Exception:
                pass

    # ------------------------------------------------------------------ 收尾
    def _save_config(self) -> None:
        try:
            save_config(self.config_file, self.cfg)
        except Exception as exc:
            log.exception("保存配置失败")
            self._notify("保存配置失败", str(exc), force_popup=True)

    def _shutdown(self) -> None:
        if self._root is not None:
            for job in (self._tick_job, self._command_job):
                if job is not None:
                    try:
                        self._root.after_cancel(job)
                    except Exception:
                        pass
            self._tick_job = self._command_job = None
        if self._popups is not None:
            try:
                self._popups.close_all()
            except Exception:
                pass
        try:
            self._state.prune(_dt.date.today())
            self._state.save()
        except Exception:
            log.exception("退出时保存状态失败")
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                log.exception("停止托盘失败")
            self._tray = None
        self._tray_ok = False
        if self._root is not None:
            try:
                self._root.destroy()
            except Exception:
                pass
            self._root = None
        log.info("已退出")


def run(config_file: Path | None = None, console: bool = False) -> int:
    return AlarmApp(config_file=config_file, console=console).run()
