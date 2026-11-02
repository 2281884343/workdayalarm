# -*- coding: utf-8 -*-
"""触发调度：纯逻辑实现，方便单元测试。

规则：
- 仅当“今天”是真实工作日（含调休补班日，排除法定节假日与普通周末）时检查提醒；
- 每条提醒每天最多触发一次（状态落盘，重启不重复）；
- 错过时间点但在补偿窗口（catch_up_minutes，默认 30 分钟）内，补发一次；
- 超出补偿窗口则记为“今日已错过”，不会再弹出，避免深夜打扰。
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass
from typing import Callable

from .config import AppConfig, Reminder
from .state import TriggerState
from .workday import DayInfo, WorkdayCalendar

log = logging.getLogger(__name__)


@dataclass
class DueReminder:
    reminder: Reminder
    day: _dt.date
    scheduled: _dt.datetime
    now: _dt.datetime
    late_seconds: int
    day_info: DayInfo


class Scheduler:
    def __init__(
        self,
        calendar: WorkdayCalendar,
        state: TriggerState,
        clock: Callable[[], _dt.datetime] | None = None,
    ) -> None:
        self.calendar = calendar
        self.state = state
        self._clock = clock or (lambda: _dt.datetime.now())
        self._last_reported_day: _dt.date | None = None
        self._last_reported_workday: bool | None = None

    # ------------------------------------------------------------------ 主循环
    def tick(self, config: AppConfig) -> list[DueReminder]:
        now = self._clock()
        today = now.date()
        if not config.enabled:
            return []

        day_info = self.calendar.info(today)
        self._log_day_once(today, day_info)

        if not day_info.is_workday:
            return []

        catch_up = max(0, config.catch_up_minutes) * 60
        # 补偿窗口至少覆盖一个轮询周期，否则正常轮询也会“踩空”
        window = max(catch_up, config.poll_interval_seconds + 5)
        due: list[DueReminder] = []

        for reminder in config.reminders:
            if not reminder.enabled:
                continue
            if self.state.was_triggered(today, reminder.id):
                continue

            target = reminder.due_at(today)
            late = int((now - target).total_seconds())
            if late < 0:
                continue
            if late > window:
                # 今日已经彻底错过：记录一次，避免后续轮询反复判定
                self.state.mark_triggered(today, reminder.id, now)
                log.info(
                    "提醒 [%s] %s 已错过 %d 分钟（补偿窗口 %d 分钟），今日不再触发",
                    reminder.id,
                    reminder.time_label(),
                    late // 60,
                    window // 60,
                )
                continue

            self.state.mark_triggered(today, reminder.id, now)
            due.append(DueReminder(reminder, today, target, now, late, day_info))
            log.info(
                "触发提醒 [%s] %s（%s），延迟 %d 秒",
                reminder.id,
                reminder.time_label(),
                reminder.title,
                late,
            )
        return due

    # ------------------------------------------------------------------ 辅助
    def _log_day_once(self, today: _dt.date, info: DayInfo) -> None:
        if self._last_reported_day == today and self._last_reported_workday == info.is_workday:
            return
        self._last_reported_day = today
        self._last_reported_workday = info.is_workday
        if info.is_workday:
            log.info("今日工作日判定：%s（来源 %s），提醒正常检查", info.summary(), info.source)
        else:
            log.info("今日工作日判定：%s（来源 %s），跳过所有提醒", info.summary(), info.source)

    def next_run(self, config: AppConfig, now: _dt.datetime | None = None) -> _dt.datetime | None:
        """下一次可能的触发时刻（仅用于展示）。"""
        now = now or self._clock()
        candidates: list[_dt.datetime] = []
        for offset in range(0, 21):
            day = now.date() + _dt.timedelta(days=offset)
            info = self.calendar.info(day)
            if not info.is_workday:
                continue
            for reminder in config.reminders:
                if not reminder.enabled:
                    continue
                target = reminder.due_at(day)
                if target <= now:
                    continue
                if self.state.was_triggered(day, reminder.id):
                    continue
                candidates.append(target)
        return min(candidates) if candidates else None

    def status_line(self, config: AppConfig) -> str:
        """托盘提示与左键气泡用的一行状态。"""
        now = self._clock()
        info = self.calendar.info(now.date())
        if not config.enabled:
            return "已暂停 · 今日" + ("工作日" if info.is_workday else "休息日")
        base = "今日" + ("工作日" if info.is_workday else "休息日")
        if info.label:
            base += f"（{info.label}）"
        nxt = self.next_run(config, now)
        if nxt is None:
            return base + " · 今日无待触发提醒"
        return base + f" · 下次 {nxt.strftime('%m-%d %H:%M')}"
