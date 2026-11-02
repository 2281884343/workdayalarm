# -*- coding: utf-8 -*-
"""节假日数据更新。

程序本身完全离线；只有在「每年 12 月 30 日之后仍然缺少下一年数据」或用户
在托盘菜单里手动点「更新节假日数据」时，才会联网从 PyPI 取最新版
`chinesecalendar` 的数据并合并进 `<home>/data/holidays.json`。

更新失败不会破坏已有数据，只把失败原因告诉用户，方便手动处理。
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import os
import urllib.request
import zipfile
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

# 每年 12 月 30 日起检查下一年数据（国务院办公厅一般 11 月发布放假安排）
ANNUAL_MONTH = 12
ANNUAL_DAY = 30

PYPI_JSON_URL = "https://pypi.org/pypi/chinesecalendar/json"
_USER_AGENT = f"{__version__} (WorkdayAlarm)"
_CONSTANTS_MEMBER = "chinese_calendar/constants.py"
_TIMEOUT = 25


class UpdateError(RuntimeError):
    """数据更新过程中的可预期错误（消息直接展示给用户）。"""


def _download(url: str, timeout: int = _TIMEOUT) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 固定 https 地址
        return response.read()


def fetch_constants_source(timeout: int = _TIMEOUT) -> str:
    """从 PyPI 下载 chinesecalendar 的最新 wheel，返回其中 constants.py 的源码。"""
    try:
        info = json.loads(_download(PYPI_JSON_URL, timeout).decode("utf-8"))
    except Exception as exc:  # 网络、JSON 解析等
        raise UpdateError(f"无法访问 PyPI：{exc}") from exc

    urls = [item for item in info.get("urls", []) if item.get("packagetype") == "bdist_wheel"]
    if not urls:
        raise UpdateError("PyPI 上没有可用的 wheel 包")
    wheel = next(
        (item for item in urls if str(item.get("filename", "")).endswith("py3-none-any.whl")),
        urls[0],
    )
    try:
        raw = _download(str(wheel["url"]), timeout)
    except Exception as exc:
        raise UpdateError(f"下载数据包失败：{exc}") from exc

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = [name for name in zf.namelist() if name.replace("\\", "/").endswith(_CONSTANTS_MEMBER)]
            if not names:
                raise UpdateError("数据包中未找到 constants.py")
            return zf.read(names[0]).decode("utf-8")
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(f"解析数据包失败：{exc}") from exc


def parse_constants(source: str) -> tuple[dict[dt.date, str], set[dt.date]]:
    """执行 constants.py，取出 {日期: 节日中文名} 与调休上班日期集合。

    不同版本的数据格式不同：
    - 旧版（<=1.11.0）：`holidays` 的值就是 Holiday 枚举成员，取 `.chinese`；
    - 新版：`holidays` 的值是英文名（`.value`），需要借助 Holiday 枚举反查中文。
    """
    namespace: dict[str, object] = {"__name__": "chinese_calendar.constants"}
    try:
        exec(compile(source, "<chinese_calendar.constants>", "exec"), namespace)  # noqa: S102
    except Exception as exc:
        raise UpdateError(f"解析节假日数据失败：{exc}") from exc

    english_to_chinese = _english_to_chinese(namespace.get("Holiday"))
    raw_holidays = namespace.get("holidays") or {}
    raw_workdays = namespace.get("workdays") or ()

    holidays: dict[dt.date, str] = {}
    if isinstance(raw_holidays, dict):
        for day, item in raw_holidays.items():
            if not isinstance(day, dt.date):
                continue
            name = getattr(item, "chinese", None)
            if not name:
                name = english_to_chinese.get(str(item)) or str(item)
            holidays[day] = str(name)
    workdays = {day for day in raw_workdays if isinstance(day, dt.date)} if raw_workdays else set()
    return holidays, workdays


def _english_to_chinese(holiday_cls: object) -> dict[str, str]:
    """把 Holiday 枚举映射成 {英文名: 中文名}。"""
    mapping: dict[str, str] = {}
    try:
        members = list(holiday_cls)  # type: ignore[arg-type]
    except TypeError:
        return mapping
    for member in members:
        english = getattr(member, "value", None)
        chinese = getattr(member, "chinese", None)
        if isinstance(english, str) and isinstance(chinese, str):
            mapping[english] = chinese
    return mapping


def build_year(year: int, holidays: dict[dt.date, str], workdays: set[dt.date]) -> dict[str, dict[str, str]]:
    """把全量数据裁剪成 holidays.json 里某一年的结构。"""
    holiday_map = {day.isoformat(): holidays[day] for day in sorted(holidays) if day.year == year}
    workday_map = {
        day.isoformat(): "调休上班"
        for day in sorted(workdays)
        if day.year == year and day.weekday() >= 5
    }
    return {"holidays": holiday_map, "workdays": workday_map}


def read_data_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def merge_years(path: Path, payload: dict[int, dict[str, dict[str, str]]]) -> list[int]:
    """把若干年的数据合并进数据文件（保留其它年份），原子写回。"""
    document = read_data_file(path)
    years = dict(document.get("years") or {})
    for year, data in payload.items():
        years[str(year)] = data
    meta = dict(document.get("_meta") or {})
    meta.update(
        {
            "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "PyPI chinesecalendar",
            "years": sorted(int(str(key)) for key in years if str(key).isdigit()),
        }
    )
    merged = {
        "_meta": meta,
        "years": dict(sorted(years.items(), key=lambda item: str(item[0]))),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return sorted(payload)


def update_file(path: Path, years: list[int], timeout: int = _TIMEOUT) -> tuple[bool, str]:
    """联网更新指定年份数据；返回 (是否成功, 给用户看的说明)。"""
    wanted = sorted({int(year) for year in years})
    if not wanted:
        return False, "没有需要更新的年份"

    try:
        source = fetch_constants_source(timeout)
        holidays, workdays = parse_constants(source)
    except UpdateError as exc:
        return False, str(exc)

    available = sorted({day.year for day in holidays} | {day.year for day in workdays})
    latest = available[-1] if available else None
    payload: dict[int, dict[str, dict[str, str]]] = {}
    for year in wanted:
        data = build_year(year, holidays, workdays)
        if not data["holidays"] and not data["workdays"]:
            hint = f"当前可用的最新数据只到 {latest} 年" if latest else "未取到任何数据"
            return False, f"{year} 年的放假安排尚未发布（{hint}），可稍后再试"
        payload[year] = data

    try:
        done = merge_years(Path(path), payload)
    except OSError as exc:
        return False, f"写入数据文件失败：{exc}（{path}）"
    years_text = "、".join(str(year) for year in done)
    return True, f"已写入 {years_text} 年数据（{path}）"


def needs_annual_update(calendar, today: dt.date, month: int = ANNUAL_MONTH, day: int = ANNUAL_DAY) -> int | None:
    """返回需要自动补齐的年份；不需要时返回 None。

    两种情况：1) 当年数据缺失（例如跨年后很久才开机），立即补；
    2) 12 月 30 日之后，补齐下一年的放假安排。
    """
    if not calendar.supports(today.year):
        return today.year
    if (today.month, today.day) < (month, day):
        return None
    next_year = today.year + 1
    return None if calendar.supports(next_year) else next_year


def missing_years(calendar, today: dt.date) -> list[int]:
    """当前年份到明年中，日历尚未覆盖的年份。"""
    return [year for year in (today.year, today.year + 1) if not calendar.supports(year)]


def target_path_from(home: Path) -> Path:
    return Path(home) / "data" / "holidays.json"
