# -*- coding: utf-8 -*-
"""配置读写与校验单元测试。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from alarm.config import (  # noqa: E402
    MODE_POPUP,
    AppConfig,
    load_config,
    parse_config,
    save_config,
    write_sample_config,
)


class ParseConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = parse_config({})
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.notification_mode, "toast")
        self.assertEqual(cfg.reminders, [])
        self.assertEqual(cfg.load_errors, [])

    def test_full_config(self) -> None:
        cfg = parse_config(
            {
                "enabled": False,
                "poll_interval_seconds": 30,
                "catch_up_minutes": 5,
                "notification_mode": "popup",
                "popup_timeout_seconds": 15,
                "sound": "none",
                "autostart": True,
                "reminders": [
                    {"id": "morning", "time": "09:30", "title": "上班", "message": "打卡"}
                ],
            }
        )
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.poll_interval_seconds, 30)
        self.assertEqual(cfg.notification_mode, MODE_POPUP)
        self.assertTrue(cfg.autostart)
        self.assertEqual(len(cfg.reminders), 1)
        self.assertEqual(cfg.reminders[0].time, "09:30")
        self.assertEqual(cfg.load_errors, [])

    def test_bad_time_is_rejected(self) -> None:
        cfg = parse_config({"reminders": [{"id": "a", "time": "25:00", "title": "x"}]})
        self.assertEqual(cfg.reminders, [])
        self.assertTrue(any("25:00" in e for e in cfg.load_errors), cfg.load_errors)

    def test_time_is_normalized(self) -> None:
        cfg = parse_config({"reminders": [{"id": "a", "time": "9:5", "title": "x"}]})
        # 9:5 不合法（分钟必须两位）
        self.assertEqual(cfg.reminders, [])

    def test_duplicate_id_is_renamed(self) -> None:
        cfg = parse_config(
            {
                "reminders": [
                    {"id": "dup", "time": "09:00", "title": "a"},
                    {"id": "dup", "time": "10:00", "title": "b"},
                ]
            }
        )
        self.assertEqual(len(cfg.reminders), 2)
        self.assertNotEqual(cfg.reminders[0].id, cfg.reminders[1].id)

    def test_invalid_mode_falls_back(self) -> None:
        cfg = parse_config({"notification_mode": "email"})
        self.assertEqual(cfg.notification_mode, "toast")
        self.assertTrue(cfg.load_errors)

    def test_per_reminder_mode_and_sound(self) -> None:
        cfg = parse_config(
            {
                "notification_mode": "toast",
                "reminders": [
                    {"id": "a", "time": "09:00", "title": "a", "mode": "popup", "sound": "none"}
                ],
            }
        )
        reminder = cfg.reminders[0]
        self.assertEqual(cfg.mode_for(reminder), "popup")
        self.assertEqual(cfg.sound_for(reminder), "none")

    def test_invalid_reminder_entries_are_skipped(self) -> None:
        cfg = parse_config({"reminders": ["bad", 42, {"id": "ok", "time": "08:00", "title": "ok"}]})
        self.assertEqual(len(cfg.reminders), 1)
        self.assertTrue(cfg.load_errors)


class FileConfigTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = AppConfig(notification_mode="popup", poll_interval_seconds=15)
            save_config(path, cfg)
            loaded = load_config(path)
            self.assertEqual(loaded.notification_mode, "popup")
            self.assertEqual(loaded.poll_interval_seconds, 15)
            self.assertEqual(loaded.load_errors, [])

    def test_missing_file_creates_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = load_config(path)
            self.assertTrue(path.is_file())
            self.assertTrue(cfg.enabled)
            self.assertIn("_comment", json.loads(path.read_text(encoding="utf-8")))
            self.assertGreaterEqual(len(cfg.reminders), 1)
            self.assertEqual(cfg.load_errors, [])

    def test_broken_file_backs_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ this is not json", encoding="utf-8")
            cfg = load_config(path)
            self.assertTrue(cfg.load_errors)
            self.assertTrue(Path(str(path) + ".broken").is_file())

    def test_sample_config_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            self.assertTrue(write_sample_config(path))
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("_comment", raw)
            cfg = load_config(path)
            self.assertEqual(cfg.load_errors, [], cfg.load_errors)
            self.assertGreaterEqual(len(cfg.reminders), 1)
            # 示例文件只写一次，不覆盖已有配置
            self.assertFalse(write_sample_config(path))


if __name__ == "__main__":
    unittest.main()
