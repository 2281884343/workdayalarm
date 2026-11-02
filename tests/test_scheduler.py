# -*- coding: utf-8 -*-
"""调度触发逻辑单元测试（用固定时钟，不依赖真实时间）。"""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from alarm.config import AppConfig, Reminder  # noqa: E402
from alarm.scheduler import Scheduler  # noqa: E402
from alarm.state import TriggerState  # noqa: E402
from alarm.workday import WorkdayCalendar  # noqa: E402

DATA = ROOT / "data" / "holidays.json"


class FakeClock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


class SchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not DATA.is_file():
            raise unittest.SkipTest("缺少 data/holidays.json")
        cls.calendar = WorkdayCalendar(
            data_files=[(DATA, "内置数据")], unknown_year_policy="weekday", use_package=False
        )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmp.name) / "state.json"
        self.state = TriggerState(self.state_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _config(self, **kwargs) -> AppConfig:
        defaults = dict(
            enabled=True,
            poll_interval_seconds=10,
            catch_up_minutes=30,
            reminders=[Reminder(id="morning", time="09:30", title="上班", message="打卡")],
        )
        defaults.update(kwargs)
        return AppConfig(**defaults)

    def _scheduler(self, now: dt.datetime) -> tuple[Scheduler, FakeClock]:
        clock = FakeClock(now)
        return Scheduler(self.calendar, self.state, clock=clock), clock

    def test_fires_on_workday_and_only_once(self) -> None:
        scheduler, clock = self._scheduler(dt.datetime(2026, 11, 3, 9, 30, 5))
        cfg = self._config()
        due = scheduler.tick(cfg)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].reminder.id, "morning")
        self.assertEqual(scheduler.tick(cfg), [])
        clock.now = dt.datetime(2026, 11, 3, 9, 31, 0)
        self.assertEqual(scheduler.tick(cfg), [])

    def test_skips_holiday(self) -> None:
        scheduler, _ = self._scheduler(dt.datetime(2026, 10, 1, 9, 30, 5))
        self.assertEqual(scheduler.tick(self._config()), [])

    def test_skips_weekend(self) -> None:
        scheduler, _ = self._scheduler(dt.datetime(2026, 10, 3, 9, 30, 5))
        self.assertEqual(scheduler.tick(self._config()), [])

    def test_fires_on_makeup_saturday(self) -> None:
        scheduler, _ = self._scheduler(dt.datetime(2026, 10, 10, 9, 30, 5))
        due = scheduler.tick(self._config())
        self.assertEqual(len(due), 1)

    def test_catch_up_window(self) -> None:
        # 09:30 的提醒，09:45 才轮询到（模拟休眠唤醒），仍在 30 分钟补偿窗口内
        scheduler, _ = self._scheduler(dt.datetime(2026, 11, 3, 9, 45, 0))
        due = scheduler.tick(self._config())
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].late_seconds, 900)

    def test_beyond_window_is_dropped(self) -> None:
        scheduler, _ = self._scheduler(dt.datetime(2026, 11, 3, 12, 0, 0))
        cfg = self._config()
        self.assertEqual(scheduler.tick(cfg), [])
        # 记入“今日已触发”，避免后续轮询重复判定
        self.assertTrue(self.state.was_triggered(dt.date(2026, 11, 3), "morning"))

    def test_not_yet_due(self) -> None:
        scheduler, _ = self._scheduler(dt.datetime(2026, 11, 3, 9, 0, 0))
        self.assertEqual(scheduler.tick(self._config()), [])

    def test_disabled_globally(self) -> None:
        scheduler, _ = self._scheduler(dt.datetime(2026, 11, 3, 9, 30, 5))
        self.assertEqual(scheduler.tick(self._config(enabled=False)), [])

    def test_disabled_reminder(self) -> None:
        reminder = Reminder(id="morning", time="09:30", title="上班", enabled=False)
        scheduler, _ = self._scheduler(dt.datetime(2026, 11, 3, 9, 30, 5))
        self.assertEqual(scheduler.tick(self._config(reminders=[reminder])), [])

    def test_state_survives_restart(self) -> None:
        scheduler, _ = self._scheduler(dt.datetime(2026, 11, 3, 9, 30, 5))
        cfg = self._config()
        self.assertEqual(len(scheduler.tick(cfg)), 1)
        self.state.save()

        reloaded = TriggerState(self.state_path)
        restarted = Scheduler(self.calendar, reloaded, clock=FakeClock(dt.datetime(2026, 11, 3, 9, 31, 0)))
        self.assertEqual(restarted.tick(cfg), [])

    def test_next_run_and_status_line(self) -> None:
        scheduler, _ = self._scheduler(dt.datetime(2026, 11, 2, 12, 0, 0))
        cfg = self._config()
        self.assertEqual(scheduler.next_run(cfg), dt.datetime(2026, 11, 3, 9, 30))
        self.assertIn("09:30", scheduler.status_line(cfg))

    def test_next_run_skips_holiday_and_weekend(self) -> None:
        # 2026-09-30 18:00 → 下一条提醒在 10-08（国庆假期后）
        scheduler, _ = self._scheduler(dt.datetime(2026, 9, 30, 18, 0, 0))
        cfg = self._config(reminders=[Reminder(id="m", time="09:30", title="上班")])
        self.assertEqual(scheduler.next_run(cfg), dt.datetime(2026, 10, 8, 9, 30))


if __name__ == "__main__":
    unittest.main()
