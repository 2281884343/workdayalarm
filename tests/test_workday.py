# -*- coding: utf-8 -*-
"""工作日判定单元测试。"""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from alarm.workday import DayInfo, WorkdayCalendar  # noqa: E402

DATA = ROOT / "data" / "holidays.json"


class WorkdayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not DATA.is_file():
            raise unittest.SkipTest("缺少 data/holidays.json")
        cls.cal = WorkdayCalendar(
            data_files=[(DATA, "内置数据")], unknown_year_policy="weekday", use_package=False
        )

    def test_holiday(self) -> None:
        info = self.cal.info(dt.date(2026, 10, 1))
        self.assertFalse(info.is_workday)
        self.assertEqual(info.kind, "holiday")
        self.assertEqual(info.label, "国庆节")

    def test_makeup_workday(self) -> None:
        for text in ("2026-02-14", "2026-02-28", "2026-09-20", "2026-10-10", "2026-01-04"):
            info = self.cal.info(dt.date.fromisoformat(text))
            self.assertTrue(info.is_workday, text)
            self.assertEqual(info.kind, "makeup", text)

    def test_normal_weekday_and_weekend(self) -> None:
        monday = self.cal.info(dt.date(2026, 11, 2))
        self.assertTrue(monday.is_workday)
        self.assertEqual(monday.kind, "normal")
        saturday = self.cal.info(dt.date(2026, 11, 1))
        self.assertFalse(saturday.is_workday)
        self.assertEqual(saturday.kind, "weekend")

    def test_spring_festival(self) -> None:
        self.assertFalse(self.cal.info(dt.date(2026, 2, 15)).is_workday)
        self.assertTrue(self.cal.info(dt.date(2026, 2, 14)).is_workday)
        self.assertFalse(self.cal.info(dt.date(2026, 2, 23)).is_workday)

    def test_cache_returns_same_object(self) -> None:
        day = dt.date(2026, 6, 1)
        self.assertIs(self.cal.info(day), self.cal.info(day))

    def test_next_workday(self) -> None:
        # 2026-09-30 周三 → 下一个工作日是 10-08（国庆 10/1-10/7 放假）
        self.assertEqual(self.cal.next_workday(dt.date(2026, 9, 30)), dt.date(2026, 9, 30))
        self.assertEqual(self.cal.next_workday(dt.date(2026, 10, 1)), dt.date(2026, 10, 8))

    def test_missing_year_fallback_weekday(self) -> None:
        cal = WorkdayCalendar(data_files=[(DATA, "内置数据")], unknown_year_policy="weekday", use_package=False)
        friday = cal.info(dt.date(2031, 1, 3))
        saturday = cal.info(dt.date(2031, 1, 4))
        self.assertTrue(friday.is_workday)
        self.assertFalse(saturday.is_workday)
        self.assertEqual(friday.kind, "unknown")

    def test_missing_year_fallback_holiday(self) -> None:
        cal = WorkdayCalendar(data_files=[(DATA, "内置数据")], unknown_year_policy="holiday", use_package=False)
        self.assertFalse(cal.info(dt.date(2031, 1, 3)).is_workday)

    def test_coverage_and_supports(self) -> None:
        self.assertEqual(self.cal.coverage(), [2024, 2025, 2026])
        self.assertTrue(self.cal.supports(2026))
        self.assertFalse(self.cal.supports(2027))

    def test_local_override_wins(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "holidays.local.json"
            override.write_text(
                json.dumps(
                    {"years": {"2026": {"holidays": {"2026-11-02": "公司团建"}, "workdays": {}}}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            cal = WorkdayCalendar(
                data_files=[(override, "本地覆盖"), (DATA, "内置数据")], use_package=False
            )
            info = cal.info(dt.date(2026, 11, 2))
            self.assertFalse(info.is_workday)
            self.assertEqual(info.label, "公司团建")
            self.assertEqual(info.source, "本地覆盖")
            # 未在高优先级文件声明的日期仍走内置数据（例如国庆是假期而非上班日）
            holiday = cal.info(dt.date(2026, 10, 1))
            self.assertFalse(holiday.is_workday)
            self.assertEqual(holiday.label, "国庆节")
            self.assertEqual(holiday.source, "内置数据")
            # 普通工作日沿用该年第一优先来源的兜底规则
            self.assertTrue(cal.info(dt.date(2026, 11, 3)).is_workday)

    def test_describe_range(self) -> None:
        rows = self.cal.describe_range(dt.date(2026, 10, 1), 3)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(isinstance(r, DayInfo) for r in rows))
        self.assertFalse(any(r.is_workday for r in rows))


if __name__ == "__main__":
    unittest.main()
