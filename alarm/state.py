# -*- coding: utf-8 -*-
"""触发状态持久化：保证同一天同一条提醒只弹一次，重启后也不重复。"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_KEEP_DAYS = 14


class TriggerState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.triggered: dict[str, set[str]] = {}
        self.fired_at: dict[str, str] = {}   # "date|id" -> ISO 时间戳（便于排查）
        self.load()

    # ------------------------------------------------------------------ 读写
    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("状态文件读取失败（%s），按空状态处理：%s", self.path, exc)
            return
        if not isinstance(raw, dict):
            return
        triggered = raw.get("triggered")
        if isinstance(triggered, dict):
            for day, ids in triggered.items():
                if isinstance(ids, list):
                    self.triggered[str(day)] = {str(i) for i in ids}
        fired = raw.get("fired_at")
        if isinstance(fired, dict):
            self.fired_at = {str(k): str(v) for k, v in fired.items()}

    def save(self) -> None:
        payload = {
            "version": 1,
            "triggered": {day: sorted(ids) for day, ids in sorted(self.triggered.items())},
            "fired_at": dict(sorted(self.fired_at.items())),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".state_", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.error("状态文件写入失败 %s：%s", self.path, exc)

    # ------------------------------------------------------------------ 查询
    @staticmethod
    def _key(day: _dt.date, reminder_id: str) -> str:
        return f"{day.isoformat()}|{reminder_id}"

    def was_triggered(self, day: _dt.date, reminder_id: str) -> bool:
        return reminder_id in self.triggered.get(day.isoformat(), set())

    def mark_triggered(self, day: _dt.date, reminder_id: str, when: _dt.datetime) -> None:
        self.triggered.setdefault(day.isoformat(), set()).add(reminder_id)
        self.fired_at[self._key(day, reminder_id)] = when.isoformat(timespec="seconds")
        self.prune(day)
        self.save()

    def prune(self, today: _dt.date) -> None:
        cutoff = (today - _dt.timedelta(days=_KEEP_DAYS)).isoformat()
        for day in [d for d in self.triggered if d < cutoff]:
            self.triggered.pop(day, None)
        for key in [k for k in self.fired_at if k[:10] < cutoff]:
            self.fired_at.pop(key, None)

    def reset_day(self, day: _dt.date) -> None:
        self.triggered.pop(day.isoformat(), None)
        self.save()
