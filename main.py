# -*- coding: utf-8 -*-
"""工作日提醒 —— 程序入口。

用法：
    python main.py                 启动托盘程序
    python main.py --check         查看最近两周的工作日判定
    python main.py --check 2026-10-10   查看指定日期
    python main.py --test          自检：节假日数据、配置、托盘可用性
    python main.py --update-data   联网更新节假日数据（--years 2027 指定年份）
    python main.py --config PATH   指定配置文件
    python main.py --home PATH     指定数据目录（配置/日志/状态）
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import io
import os
import sys
from pathlib import Path

from alarm import APP_TITLE, __version__
from alarm.config import load_config
from alarm.paths import (
    config_path,
    data_dir,
    data_update_path,
    home_dir,
    log_path,
    reset_home_cache,
    state_path,
)

MUTEX_NAME = "Local\\WorkdayAlarm.SingleInstance"
MUTEX_ALREADY_EXISTS = 183

_mutex_handle = None
_cli_output: "io.StringIO | None" = None


def _enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _set_app_id() -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WorkdayAlarm.Tray")
    except Exception:
        pass


def _acquire_single_instance() -> bool:
    """命名互斥体保证只有一个实例在跑。"""
    global _mutex_handle
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            return True
        if ctypes.get_last_error() == MUTEX_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _mutex_handle = handle
        return True
    except Exception:
        return True


def _release_single_instance() -> None:
    global _mutex_handle
    if _mutex_handle:
        try:
            ctypes.WinDLL("kernel32").CloseHandle(_mutex_handle)
        except Exception:
            pass
        _mutex_handle = None


def _ensure_output() -> bool:
    """打包成 --noconsole 时 sys.stdout 为 None，为命令行子命令找回输出通道。

    返回 True 表示输出可直接看到；False 表示已缓存，稍后用弹窗展示。
    """
    global _cli_output
    if sys.stdout is not None:
        _cli_output = None
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        if kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            kernel32.SetConsoleOutputCP(65001)
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
            sys.stderr = sys.stdout
            return True
    except Exception:
        pass
    _cli_output = io.StringIO()
    sys.stdout = _cli_output
    sys.stderr = _cli_output
    return False


def _flush_output(console_ok: bool) -> None:
    """没有控制台时（例如从资源管理器双击 exe 带参数运行），用弹窗展示结果。"""
    if console_ok or _cli_output is None:
        return
    text = _cli_output.getvalue().strip()
    if not text:
        return
    from alarm.app import message_box

    note = ""
    try:
        import tempfile

        dump = Path(tempfile.gettempdir()) / "WorkdayAlarm-output.txt"
        dump.write_text(text + "\n", encoding="utf-8")
        note = f"\n\n（同样的内容已保存到 {dump}）"
    except Exception:
        pass
    message_box(text + note, f"{APP_TITLE} v{__version__}", 0x40)


def _parse_date(text: str) -> dt.date:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%m-%d"):
        try:
            value = dt.datetime.strptime(text.strip(), fmt)
            if fmt == "%m-%d":
                value = value.replace(year=dt.date.today().year)
            return value.date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"无法识别的日期：{text}（示例：2026-10-01）")


def _print_check(calendar, target: dt.date | None, days: int, config_file: Path) -> None:
    years = calendar.coverage()
    cover = f"{years[0]}–{years[-1]}" if years else "无（使用兜底规则）"
    print(f"{APP_TITLE} v{__version__}")
    print(f"配置文件：{config_file}（存在：{config_file.is_file()}）")
    print(f"数据目录：{home_dir()}")
    print(f"内置数据覆盖年份：{cover}")
    print()

    today = dt.date.today()
    start = target or today
    rows = calendar.describe_range(start, days)
    header = f"{'日期':<12}{'星期':<6}{'工作日':<8}{'类型':<8}{'说明':<24}来源"
    print(header)
    print("-" * 84)
    for info in rows:
        from alarm.workday import weekday_zh

        print(
            f"{info.date.isoformat():<12}{weekday_zh(info.date):<6}"
            f"{'是' if info.is_workday else '否':<9}{info.kind_label:<8}"
            f"{info.summary():<24}{info.source}"
        )
    nxt = calendar.next_workday(today + dt.timedelta(days=1), limit=60)
    if nxt is not None:
        print()
        print(f"下一个工作日：{nxt.isoformat()}（{weekday_zh(nxt)}）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="workday_alarm", description=f"{APP_TITLE} v{__version__}", add_help=True
    )
    parser.add_argument("--version", action="store_true", help="显示版本号后退出")
    parser.add_argument(
        "--check", nargs="?", const="", metavar="DATE", help="查看工作日判定（可带日期 2026-10-01）"
    )
    parser.add_argument("--test", action="store_true", help="自检节假日数据与配置后退出")
    parser.add_argument(
        "--update-data",
        action="store_true",
        help="联网从 PyPI 更新节假日数据（默认补当前缺失的年份）",
    )
    parser.add_argument(
        "--years",
        metavar="YEARS",
        help="配合 --update-data 指定年份，例如 2027 或 2026,2027",
    )
    parser.add_argument("--days", type=int, default=14, help="--check 连续查看的天数（默认 14）")
    parser.add_argument("--config", metavar="PATH", help="指定配置文件路径")
    parser.add_argument("--home", metavar="PATH", help="指定数据目录（配置/日志/状态所在目录）")
    parser.add_argument("--no-tray", action="store_true", help="调试用：不创建托盘（会立即退出）")
    args = parser.parse_args(argv)

    if args.home:
        os.environ["WORKDAY_ALARM_HOME"] = str(Path(args.home).expanduser().resolve())
        reset_home_cache()

    if args.version:
        ok = _ensure_output()
        print(f"{APP_TITLE} v{__version__}")
        _flush_output(ok)
        return 0

    config_file = Path(args.config).expanduser().resolve() if args.config else config_path()

    if args.check is not None:
        from alarm.app import build_calendar

        ok = _ensure_output()
        cfg = load_config(config_file)
        calendar = build_calendar(cfg)
        target = _parse_date(args.check) if args.check else None
        _print_check(calendar, target, max(1, args.days), config_file)
        _flush_output(ok)
        return 0

    if args.test:
        from alarm.app import build_calendar

        ok = _ensure_output()
        cfg = load_config(config_file)
        calendar = build_calendar(cfg)
        print(f"{APP_TITLE} v{__version__} 自检")
        print(f"  配置：{config_file}")
        print(f"  状态：{state_path()}")
        print(f"  日志：{log_path()}")
        print(f"  数据目录：{data_dir()}")
        print(f"  数据覆盖年份：{calendar.coverage() or '无'}")
        print(f"  提醒条数：{len(cfg.reminders)}（启用 {sum(1 for r in cfg.reminders if r.enabled)}）")
        if cfg.load_errors:
            print("  配置问题：")
            for err in cfg.load_errors:
                print(f"    - {err}")
        today = dt.date.today()
        info = calendar.info(today)
        print(f"  今日判定：{info.summary()}（来源 {info.source}）")
        try:
            import tkinter

            root = tkinter.Tk()
            root.withdraw()
            root.destroy()
            print("  Tk：可用")
        except Exception as exc:
            print(f"  Tk：不可用（{exc}）")
        from alarm.tray import TrayIcon

        tray = TrayIcon(APP_TITLE, on_command=lambda _cmd: None)
        ok = tray.start(timeout=5.0)
        print(f"  托盘：{'可用' if ok else '创建失败'}")
        tray.stop()
        _flush_output(ok)
        return 0 if not cfg.load_errors else 2

    if args.update_data:
        from alarm import dataupdate
        from alarm.app import build_calendar

        console_ok = _ensure_output()
        years: list[int] = []
        if args.years:
            for chunk in str(args.years).replace(",", " ").split():
                try:
                    years.append(int(chunk))
                except ValueError:
                    print(f"--years 里的 {chunk!r} 不是年份")
                    _flush_output(console_ok)
                    return 2
        if not years:
            calendar = build_calendar(load_config(config_file))
            years = dataupdate.missing_years(calendar, dt.date.today()) or [dt.date.today().year + 1]

        target = data_update_path()
        print(f"更新节假日数据：{('、'.join(str(y) for y in years))} → {target}")
        done, message = dataupdate.update_file(target, years)
        print(("成功：" if done else "失败：") + message)
        _flush_output(console_ok)
        return 0 if done else 1

    if args.no_tray:
        ok = _ensure_output()
        print("--no-tray 仅用于调试，已退出。")
        _flush_output(ok)
        return 0

    _enable_dpi_awareness()
    _set_app_id()

    if not _acquire_single_instance():
        from alarm.app import message_box

        message_box(f"{APP_TITLE} 已在运行。\n\n请查看系统托盘图标（可能在隐藏区域）。", APP_TITLE, 0x40)
        return 0

    try:
        from alarm.app import AlarmApp

        console = sys.stderr is not None and sys.stdout is not None
        return AlarmApp(config_file=config_file, console=console).run()
    finally:
        _release_single_instance()


if __name__ == "__main__":
    sys.exit(main())
