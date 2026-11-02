# -*- coding: utf-8 -*-
"""从 chinese_calendar 包生成内置节假日数据 data/holidays.json。

节假日数据每年由国务院办公厅发文公布（一般在上一年 11 月），chinese_calendar
维护者在通知发布后更新 PyPI 包。升级数据只需：

    pip install -U chinesecalendar
    python tools/gen_holidays.py --years 2024-2027

然后把生成的 data/holidays.json 一起打包/提交即可。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# chinese_calendar 的 Holiday 枚举值是英文，落盘时转成中文，便于日志与界面直接展示
_ZH_NAMES = {
    "New Year's Day": "元旦",
    "Spring Festival": "春节",
    "Tomb-sweeping Day": "清明节",
    "Labour Day": "劳动节",
    "Dragon Boat Festival": "端午节",
    "National Day": "国庆节",
    "Mid-autumn Festival": "中秋节",
    "Anti-Fascist 70th Day": "抗战胜利纪念日",
}


def _zh(name) -> str:
    raw = str(getattr(name, "value", name))
    return _ZH_NAMES.get(raw, raw)


def _parse_years(text: str) -> list[int]:
    years: list[int] = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            years.extend(range(int(start), int(end) + 1))
        else:
            years.append(int(part))
    return sorted(set(years))


def generate(years: list[int]) -> dict:
    import chinese_calendar as cc

    payload: dict = {"_meta": {}, "years": {}}
    for year in years:
        holidays: dict[str, str] = {}
        workdays: dict[str, str] = {}
        day = dt.date(year, 1, 1)
        while day.year == year:
            try:
                workday = cc.is_workday(day)
                is_official_holiday, name = cc.get_holiday_detail(day)
            except NotImplementedError:
                raise SystemExit(
                    f"chinese_calendar 未覆盖 {year} 年，请先升级：pip install -U chinesecalendar"
                )
            key = day.isoformat()
            if not workday and name:
                # 法定节假日（含调休放假日）；普通周末 get_holiday_detail 的 name 为 None，不写入
                holidays[key] = _zh(name)
            elif workday and day.weekday() >= 5:
                # 周末补班：weekday>=5 却要上班，属于调休上班
                workdays[key] = "调休上班"
            day += dt.timedelta(days=1)
        payload["years"][str(year)] = {"holidays": holidays, "workdays": workdays}

    try:
        from importlib.metadata import version

        pkg_version = version("chinesecalendar")
    except Exception:
        pkg_version = "unknown"
    payload["_meta"] = {
        "source": f"chinesecalendar {pkg_version}",
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "由 tools/gen_holidays.py 生成，请勿手工编辑；可用 holidays.local.json 覆盖。",
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成内置节假日数据")
    parser.add_argument("--years", default="2024-2026", help="年份范围，如 2024-2026 或 2024,2025")
    parser.add_argument("--out", default=str(ROOT / "data" / "holidays.json"), help="输出文件")
    args = parser.parse_args(argv)

    years = _parse_years(args.years)
    payload = generate(years)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    total_h = sum(len(y["holidays"]) for y in payload["years"].values())
    total_w = sum(len(y["workdays"]) for y in payload["years"].values())
    print(f"已写入 {out}")
    print(f"年份 {years[0]}–{years[-1]}：法定节假日 {total_h} 天，调休上班 {total_w} 天")
    return 0


if __name__ == "__main__":
    sys.exit(main())
