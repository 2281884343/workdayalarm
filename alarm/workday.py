# -*- coding: utf-8 -*-
"""中国工作日判定。

数据来源优先级（按“年”独立解析，找到第一个能覆盖该年的来源）：

1. 用户覆盖文件 ``holidays.local.json``（放在程序目录，便于自行修正）
2. 内置数据文件 ``data/holidays.json``（随包发布，可随时替换，无需改代码）
3. ``chinese_calendar`` 第三方包（打包进 exe；每年 pip 升级即可拿到新一年数据）
4. 兜底规则：周一至周五视为工作日（可配置为全部视为休息日），并写警告日志

判定结果分四类：
- normal   常规工作日（周一至周五）
- makeup   周末/调休补班日（必须上班）
- holiday  法定节假日（放假）
- weekend  普通周末（休息）
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

# chinese_calendar 包内的英文节假日名 -> 中文
_HOLIDAY_ZH = {
    "New Year's Day": "元旦",
    "Spring Festival": "春节",
    "Tomb-sweeping Day": "清明节",
    "Labour Day": "劳动节",
    "Dragon Boat Festival": "端午节",
    "National Day": "国庆节",
    "Mid-autumn Festival": "中秋节",
    "Anti-Fascist 70th Day": "抗战胜利纪念日",
}

KIND_NORMAL = "normal"
KIND_MAKEUP = "makeup"
KIND_HOLIDAY = "holiday"
KIND_WEEKEND = "weekend"
KIND_UNKNOWN = "unknown"

KIND_LABEL = {
    KIND_NORMAL: "工作日",
    KIND_MAKEUP: "补班工作日",
    KIND_HOLIDAY: "法定节假日",
    KIND_WEEKEND: "周末休息",
    KIND_UNKNOWN: "未知（按兜底规则）",
}

_WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def weekday_zh(d: datetime.date) -> str:
    return _WEEKDAY_ZH[d.weekday()]


@dataclass(frozen=True)
class DayInfo:
    """某一天的工作日判定结果。"""

    date: datetime.date
    is_workday: bool
    kind: str
    label: str = ""
    source: str = ""

    @property
    def kind_label(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind)

    def summary(self) -> str:
        name = self.label or self.kind_label
        state = "上班" if self.is_workday else "休息"
        return f"{self.date.isoformat()}（{weekday_zh(self.date)}）{name} → {state}"


@dataclass
class _YearData:
    holidays: dict[datetime.date, str]
    workdays: dict[datetime.date, str]
    source: str


def _parse_year_block(year: str, block: dict[str, Any], source: str) -> _YearData:
    def _to_map(raw: Any) -> dict[datetime.date, str]:
        out: dict[datetime.date, str] = {}
        if isinstance(raw, dict):
            items = raw.items()
        else:  # 兼容 ["2026-01-01", ...] 简写
            items = ((item, "") for item in (raw or []))
        for key, value in items:
            try:
                out[datetime.date.fromisoformat(str(key).strip())] = str(value or "").strip()
            except ValueError:
                log.warning("节假日数据格式错误，已忽略：%r（年 %s，来源 %s）", key, year, source)
        return out

    return _YearData(
        holidays=_to_map(block.get("holidays")),
        workdays=_to_map(block.get("workdays")),
        source=source,
    )


def load_data_file(path: Path, source: str) -> dict[int, _YearData]:
    """读取一个 JSON 数据文件，返回 {年: _YearData}。"""
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.error("读取节假日数据失败 %s：%s", path, exc)
        return {}

    years_raw = raw.get("years") if isinstance(raw, dict) else None
    if not isinstance(years_raw, dict):
        log.error("节假日数据文件缺少 years 字段：%s", path)
        return {}

    result: dict[int, _YearData] = {}
    for year_key, block in years_raw.items():
        try:
            year = int(year_key)
        except (TypeError, ValueError):
            log.warning("节假日数据出现非法年份 %r（%s）", year_key, path)
            continue
        if not isinstance(block, dict):
            log.warning("节假日数据年份 %s 结构非法（%s）", year_key, path)
            continue
        result[year] = _parse_year_block(year_key, block, source)
    return result


class WorkdayCalendar:
    """工作日判定器。"""

    def __init__(
        self,
        data_files: Iterable[tuple[Path, str]] | None = None,
        unknown_year_policy: str = "weekday",
        use_package: bool = True,
    ) -> None:
        self._sources: list[dict[int, _YearData]] = []
        for path, source in data_files or []:
            self._sources.append(load_data_file(Path(path), source))

        self._policy = unknown_year_policy if unknown_year_policy in ("weekday", "holiday") else "weekday"
        self._use_package = use_package
        self._package: Any | None = None
        self._package_checked = False
        self._package_years: set[int] = set()
        self._day_cache: dict[datetime.date, DayInfo] = {}
        self._warned_years: set[int] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 数据
    def _get_package(self) -> Any | None:
        if self._package_checked:
            return self._package
        self._package_checked = True
        if not self._use_package:
            return None
        try:
            import chinese_calendar  # type: ignore

            self._package = chinese_calendar
            log.info("已加载 chinese_calendar %s", getattr(chinese_calendar, "__version__", "?"))
        except Exception as exc:  # pragma: no cover - 依赖缺失时走内置数据
            log.info("chinese_calendar 不可用（%s），使用内置数据", exc)
            self._package = None
        return self._package

    def _year_data(self, year: int) -> _YearData | None:
        """返回第一个覆盖该年的数据源（仅用于提示来源 / 判断覆盖情况）。"""
        for source in self._sources:
            if year in source:
                return source[year]
        return None

    def _year_blobs(self, year: int) -> list[_YearData]:
        """所有覆盖该年的数据源，按优先级排列。

        覆盖粒度是“具体某一天”：高优先级文件只声明 2026-11-02 时，其余日期
        仍回落到下一个数据源，而不是整年都被覆盖，这样 holidays.local.json 才
        能只写需要修正的那几天。
        """
        return [source[year] for source in self._sources if year in source]

    def coverage(self) -> list[int]:
        """内置数据覆盖的年份（不含第三方包）。"""
        years: set[int] = set()
        for source in self._sources:
            years.update(source)
        return sorted(years)

    def supports(self, year: int) -> bool:
        if self._year_data(year) is not None:
            return True
        pkg = self._get_package()
        if pkg is None:
            return False
        try:
            pkg.is_workday(datetime.date(year, 1, 4))  # 探针：任意一天即可验证覆盖范围
            return True
        except Exception:
            return False

    # ---------------------------------------------------------------- 判定逻辑
    def info(self, day: datetime.date) -> DayInfo:
        with self._lock:
            cached = self._day_cache.get(day)
            if cached is not None:
                return cached
            result = self._compute(day)
            self._day_cache[day] = result
            return result

    def is_workday(self, day: datetime.date) -> bool:
        return self.info(day).is_workday

    def _compute(self, day: datetime.date) -> DayInfo:
        blobs = self._year_blobs(day.year)
        if blobs:
            for blob in blobs:
                if day in blob.workdays:
                    return DayInfo(day, True, KIND_MAKEUP, blob.workdays[day], blob.source)
                if day in blob.holidays:
                    return DayInfo(day, False, KIND_HOLIDAY, blob.holidays[day], blob.source)
            return self._weekday_rule(day, blobs[0].source, known_year=True)

        pkg = self._get_package()
        if pkg is not None:
            try:
                on_holiday, name = pkg.get_holiday_detail(day)
            except Exception:
                on_holiday = None  # 该年份不在包内
            if on_holiday is not None:
                self._package_years.add(day.year)
                label = _HOLIDAY_ZH.get(str(name), str(name or ""))
                if on_holiday:
                    kind = KIND_HOLIDAY if label else KIND_WEEKEND
                else:
                    kind = KIND_MAKEUP if label else KIND_NORMAL
                return DayInfo(day, not on_holiday, kind, label, "chinese_calendar")

        return self._missing_year(day)

    def _weekday_rule(self, day: datetime.date, source: str, known_year: bool) -> DayInfo:
        if day.weekday() < 5:
            if not known_year:
                return self._missing_year(day)
            return DayInfo(day, True, KIND_NORMAL, "", source)
        return DayInfo(day, False, KIND_WEEKEND, "", source)

    def _missing_year(self, day: datetime.date) -> DayInfo:
        if day.year not in self._warned_years:
            self._warned_years.add(day.year)
            log.warning(
                "%d 年的节假日数据缺失（内置数据 %s），暂按兜底规则“%s”判断；"
                "请升级 chinesecalendar 包或更新 data/holidays.json",
                day.year,
                self.coverage() or "无",
                "周一至周五上班" if self._policy == "weekday" else "全部按休息日",
            )
        if self._policy == "holiday":
            return DayInfo(day, False, KIND_UNKNOWN, "", "heuristic")
        is_work = day.weekday() < 5
        return DayInfo(day, is_work, KIND_UNKNOWN, "", "heuristic")

    # ---------------------------------------------------------------- 辅助信息
    def describe_range(self, start: datetime.date, days: int) -> list[DayInfo]:
        return [self.info(start + datetime.timedelta(days=i)) for i in range(days)]

    def next_workday(self, start: datetime.date, limit: int = 60) -> datetime.date | None:
        """下一个工作日（含 start 当天）。"""
        day = start
        for _ in range(limit):
            if self.info(day).is_workday:
                return day
            day += datetime.timedelta(days=1)
        return None
