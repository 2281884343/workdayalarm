# -*- coding: utf-8 -*-
"""打包脚本：用 PyInstaller 生成单文件 exe。

    python tools/build.py            # 生成 dist/WorkdayAlarm.exe
    python tools/build.py --clean    # 先清理 build/dist

前置条件：
    pip install pyinstaller chinesecalendar
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_NAME = "WorkdayAlarm"
ICON = ROOT / "assets" / "app.ico"
DATA = ROOT / "data" / "holidays.json"


def _ensure_icon() -> Path:
    if ICON.is_file():
        return ICON
    from alarm.icon import GREEN, export_app_ico

    ICON.parent.mkdir(parents=True, exist_ok=True)
    export_app_ico(ICON, GREEN)
    print(f"已生成图标：{ICON}")
    return ICON


def _ensure_data() -> Path:
    if not DATA.is_file():
        print("未找到 data/holidays.json，正在用 chinese_calendar 生成…")
        subprocess.run([sys.executable, str(ROOT / "tools" / "gen_holidays.py")], check=True)
    return DATA


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包单文件 exe")
    parser.add_argument("--clean", action="store_true", help="先删除 build/ 与 dist/")
    parser.add_argument("--onedir", action="store_true", help="打目录版（启动更快，便于调试）")
    parser.add_argument("--console", action="store_true", help="保留控制台窗口（排查问题用）")
    args = parser.parse_args(argv)

    if args.clean:
        for folder in ("build", "dist"):
            target = ROOT / folder
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
                print(f"已删除 {target}")

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("未安装 PyInstaller，请先执行：pip install pyinstaller")
        return 1

    icon = _ensure_icon()
    data = _ensure_data()

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir" if args.onedir else "--onefile",
        "--console" if args.console else "--noconsole",
        "--name",
        APP_NAME,
        "--icon",
        str(icon),
        # 内置节假日数据随包发布，用户可自行替换 exe 旁边的 data/holidays.json
        "--add-data",
        f"{data}{';' if sys.platform == 'win32' else ':'}data",
        # 打包后不再依赖 chinese_calendar 包：数据已固化为 JSON，exe 更小更可控
        "--exclude-module",
        "chinese_calendar",
        "--exclude-module",
        "numpy",
        "--exclude-module",
        "pandas",
        "--exclude-module",
        "PIL",
        str(ROOT / "main.py"),
    ]
    print("执行：" + " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("打包失败")
        return result.returncode

    exe = ROOT / "dist" / APP_NAME / f"{APP_NAME}.exe" if args.onedir else ROOT / "dist" / f"{APP_NAME}.exe"
    if exe.is_file():
        print(f"\n打包完成：{exe}（{exe.stat().st_size / 1024 / 1024:.1f} MB）")
        print("提示：首次运行会在 exe 同目录生成 config.json、state.json 与 logs/。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
