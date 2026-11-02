# -*- coding: utf-8 -*-
"""build_calendar 的数据源优先级与去重测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from alarm.app import build_calendar
from alarm.config import parse_config


def _write(path: Path, holidays: dict, workdays: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"years": {"2026": {"holidays": holidays, "workdays": workdays or {}}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


class BuildCalendarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.app_data = root / "app" / "data"
        self.app_data.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _calendar(self, local_holidays, updated_holidays, bundled_holidays):
        local = _write(self.home / "holidays.local.json", local_holidays)
        updated = _write(self.home / "data" / "holidays.json", updated_holidays)
        bundled = _write(self.app_data / "holidays.json", bundled_holidays)
        patches = [
            mock.patch("alarm.app.home_dir", return_value=self.home),
            mock.patch("alarm.app.data_update_path", return_value=updated),
            mock.patch("alarm.app.data_dir", return_value=self.app_data),
            mock.patch("alarm.app.bundled_data_files", return_value=[bundled]),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.assertTrue(local.is_file())
        return build_calendar(parse_config({}))

    def test_priority_local_then_updated_then_bundled(self):
        calendar = self._calendar(
            local_holidays={"2026-10-08": "临时放假"},
            updated_holidays={"2026-10-09": "更新放假"},
            bundled_holidays={"2026-10-12": "内置放假"},
        )
        self.assertEqual(calendar.info(__import__("datetime").date(2026, 10, 8)).source, "本地覆盖")
        self.assertEqual(calendar.info(__import__("datetime").date(2026, 10, 9)).source, "更新数据")
        self.assertEqual(calendar.info(__import__("datetime").date(2026, 10, 12)).source, "内置数据")
        self.assertFalse(calendar.is_workday(__import__("datetime").date(2026, 10, 8)))

    def test_same_file_is_not_added_twice(self):
        # 源码运行时 data_update_path() 与 app_dir()/data/holidays.json 常常是同一个文件
        updated = _write(self.home / "data" / "holidays.json", {"2026-10-01": "国庆节"})
        patches = [
            mock.patch("alarm.app.home_dir", return_value=self.home),
            mock.patch("alarm.app.data_update_path", return_value=updated),
            mock.patch("alarm.app.data_dir", return_value=updated.parent),
            mock.patch("alarm.app.bundled_data_files", return_value=[updated]),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        calendar = build_calendar(parse_config({}))
        self.assertEqual(calendar.info(__import__("datetime").date(2026, 10, 1)).label, "国庆节")


if __name__ == "__main__":
    unittest.main()
