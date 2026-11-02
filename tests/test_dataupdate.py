# -*- coding: utf-8 -*-
"""dataupdate 模块单元测试：只覆盖不联网的纯逻辑部分。"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from alarm import dataupdate


class FakeCalendar:
    def __init__(self, years):
        self.years = set(years)

    def supports(self, year: int) -> bool:
        return year in self.years


SAMPLE_SOURCE = """
import datetime
from enum import Enum


class Holiday(Enum):
    new_years_day = ("New Year's Day", "元旦")
    national_day = ("National Day", "国庆节")

    def __init__(self, english, chinese):
        self.english = english
        self.chinese = chinese


holidays = {
    datetime.date(2027, 1, 1): Holiday.new_years_day,
    datetime.date(2027, 10, 1): Holiday.national_day,
}
workdays = {
    datetime.date(2027, 1, 9),
    datetime.date(2027, 1, 5),
}
in_lieu_days = set()
"""


NEW_FORMAT_SOURCE = """
from __future__ import absolute_import, unicode_literals

import datetime
from enum import Enum


class Holiday(Enum):
    def __new__(cls, english, chinese, days):
        obj = object.__new__(cls)
        obj._value_ = english
        obj.chinese = chinese
        obj.days = days
        return obj

    new_years_day = "New Year's Day", "元旦", 1
    national_day = "National Day", "国庆节", 3


holidays = {
    datetime.date(year=2027, month=1, day=1): Holiday.new_years_day.value,
    datetime.date(year=2027, month=1, day=2): Holiday.new_years_day.value,
    datetime.date(year=2027, month=10, day=1): Holiday.national_day.value,
}
workdays = {
    datetime.date(year=2027, month=1, day=9): Holiday.new_years_day.value,
}
in_lieu_days = {
    datetime.date(year=2027, month=1, day=4): Holiday.new_years_day.value,
}
"""


class ParseConstantsTest(unittest.TestCase):
    def test_parses_holidays_and_workdays(self):
        holidays, workdays = dataupdate.parse_constants(SAMPLE_SOURCE)
        self.assertEqual(holidays[dt.date(2027, 1, 1)], "元旦")
        self.assertEqual(holidays[dt.date(2027, 10, 1)], "国庆节")
        self.assertIn(dt.date(2027, 1, 9), workdays)

    def test_new_format_maps_english_names_to_chinese(self):
        holidays, workdays = dataupdate.parse_constants(NEW_FORMAT_SOURCE)
        self.assertEqual(holidays[dt.date(2027, 1, 1)], "元旦")
        self.assertEqual(holidays[dt.date(2027, 10, 1)], "国庆节")
        self.assertIn(dt.date(2027, 1, 9), workdays)

    def test_new_format_build_year_uses_chinese_labels(self):
        holidays, workdays = dataupdate.parse_constants(NEW_FORMAT_SOURCE)
        data = dataupdate.build_year(2027, holidays, workdays)
        self.assertEqual(data["holidays"]["2027-10-01"], "国庆节")
        self.assertEqual(data["workdays"], {"2027-01-09": "调休上班"})

    def test_invalid_source_raises_update_error(self):
        with self.assertRaises(dataupdate.UpdateError):
            dataupdate.parse_constants("this is not python(")


class BuildYearTest(unittest.TestCase):
    def setUp(self):
        self.holidays, self.workdays = dataupdate.parse_constants(SAMPLE_SOURCE)

    def test_only_weekend_workdays_become_makeup(self):
        data = dataupdate.build_year(2027, self.holidays, self.workdays)
        self.assertEqual(data["holidays"], {"2027-01-01": "元旦", "2027-10-01": "国庆节"})
        # 2027-01-05 是周二（普通工作日），不应写进 workdays
        self.assertEqual(data["workdays"], {"2027-01-09": "调休上班"})

    def test_other_year_is_empty(self):
        data = dataupdate.build_year(2030, self.holidays, self.workdays)
        self.assertEqual(data, {"holidays": {}, "workdays": {}})


class NeedsUpdateTest(unittest.TestCase):
    def test_before_december_30_does_nothing(self):
        cal = FakeCalendar([2026])
        self.assertIsNone(dataupdate.needs_annual_update(cal, dt.date(2026, 12, 29)))
        self.assertIsNone(dataupdate.needs_annual_update(cal, dt.date(2026, 6, 1)))

    def test_after_december_30_reports_missing_year(self):
        cal = FakeCalendar([2026])
        self.assertEqual(dataupdate.needs_annual_update(cal, dt.date(2026, 12, 30)), 2027)
        # 2027 年初已经有 2027 的数据，且还没到下一个年度更新窗口
        cal_2027 = FakeCalendar([2026, 2027])
        self.assertIsNone(dataupdate.needs_annual_update(cal_2027, dt.date(2027, 1, 5)))

    def test_missing_current_year_is_updated_immediately(self):
        cal = FakeCalendar([2025])
        self.assertEqual(dataupdate.needs_annual_update(cal, dt.date(2026, 3, 2)), 2026)

    def test_already_covered_returns_none(self):
        cal = FakeCalendar([2026, 2027])
        self.assertIsNone(dataupdate.needs_annual_update(cal, dt.date(2026, 12, 30)))

    def test_missing_years_scans_current_and_next(self):
        cal = FakeCalendar([2026])
        self.assertEqual(dataupdate.missing_years(cal, dt.date(2026, 11, 2)), [2027])
        cal2 = FakeCalendar([])
        self.assertEqual(dataupdate.missing_years(cal2, dt.date(2026, 11, 2)), [2026, 2027])


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "data" / "holidays.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_file_and_writes_year(self):
        payload = {2027: {"holidays": {"2027-01-01": "元旦"}, "workdays": {}}}
        done = dataupdate.merge_years(self.path, payload)
        self.assertEqual(done, [2027])
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["years"]["2027"]["holidays"]["2027-01-01"], "元旦")
        self.assertEqual(document["_meta"]["years"], [2027])
        self.assertIn("source", document["_meta"])

    def test_keeps_other_years_and_order(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps({"years": {"2026": {"holidays": {"2026-10-01": "国庆节"}, "workdays": {}}}}),
            encoding="utf-8",
        )
        dataupdate.merge_years(self.path, {2027: {"holidays": {"2027-01-01": "元旦"}, "workdays": {}}})
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(document["years"]), ["2026", "2027"])
        self.assertEqual(document["_meta"]["years"], [2026, 2027])
        self.assertEqual(document["years"]["2026"]["holidays"]["2026-10-01"], "国庆节")

    def test_reads_broken_file_as_empty(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{ not json", encoding="utf-8")
        dataupdate.merge_years(self.path, {2027: {"holidays": {"2027-01-01": "元旦"}, "workdays": {}}})
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(document["years"]), ["2027"])

    def test_no_tmp_file_left(self):
        dataupdate.merge_years(self.path, {2027: {"holidays": {}, "workdays": {}}})
        leftovers = [p.name for p in self.path.parent.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class UpdateFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "holidays.json"
        self._original_fetch = dataupdate.fetch_constants_source

    def tearDown(self):
        dataupdate.fetch_constants_source = self._original_fetch
        self.tmp.cleanup()

    def test_success_writes_requested_years(self):
        dataupdate.fetch_constants_source = lambda timeout=0: SAMPLE_SOURCE
        ok, message = dataupdate.update_file(self.path, [2027])
        self.assertTrue(ok, message)
        self.assertIn("2027", message)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["years"]["2027"]["holidays"]["2027-10-01"], "国庆节")

    def test_unpublished_year_is_reported(self):
        dataupdate.fetch_constants_source = lambda timeout=0: SAMPLE_SOURCE
        ok, message = dataupdate.update_file(self.path, [2029])
        self.assertFalse(ok)
        self.assertIn("2029", message)
        self.assertIn("2027", message)  # 提示当前最新年份
        self.assertFalse(self.path.exists())

    def test_network_failure_is_reported(self):
        def boom(timeout=0):
            raise dataupdate.UpdateError("无法访问 PyPI：连接超时")

        dataupdate.fetch_constants_source = boom
        ok, message = dataupdate.update_file(self.path, [2027])
        self.assertFalse(ok)
        self.assertIn("无法访问 PyPI", message)

    def test_empty_years(self):
        ok, message = dataupdate.update_file(self.path, [])
        self.assertFalse(ok)
        self.assertIn("没有需要更新", message)


if __name__ == "__main__":
    unittest.main()
