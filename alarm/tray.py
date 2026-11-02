# -*- coding: utf-8 -*-
"""纯 ctypes 实现的系统托盘图标（不依赖 pystray / Pillow）。

托盘线程拥有隐藏窗口，负责 Shell_NotifyIcon、右键菜单、气泡通知；
主线程（Tk）通过线程安全队列下发命令，避免跨线程操作 GUI。
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
from ctypes import wintypes
from pathlib import Path

from . import icon as app_icon

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------- 常量
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_COMMAND = 0x0111
WM_QUIT = 0x0012
WM_NULL = 0x0000
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_DESTROY = 0x0002

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIF_SHOWTIP = 0x00000080

NIIF_NONE = 0x00000000
NIIF_INFO = 0x00000001
NIIF_WARNING = 0x00000002
NIIF_ERROR = 0x00000003
NIIF_NOSOUND = 0x00000010

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_CHECKED = 0x00000008
MF_GRAYED = 0x00000001
MF_DISABLED = 0x00000002

TPM_LEFTALIGN = 0x0000
TPM_BOTTOMALIGN = 0x0004
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00010
LR_DEFAULTSIZE = 0x00040

HWND_MESSAGE = wintypes.HWND(-3)
WS_HIDDEN_POPUP = 0x80000000  # WS_POPUP（不带 WS_VISIBLE，即不可见）

ERROR_CLASS_ALREADY_EXISTS = 1410


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hWnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.LoadImageW.restype = wintypes.HANDLE
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.DestroyIcon.restype = wintypes.BOOL
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR]
user32.AppendMenuW.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, wintypes.HWND, ctypes.c_void_p,
]
user32.TrackPopupMenu.restype = wintypes.UINT
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT,
]
user32.PeekMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


# --------------------------------------------------------------------- 菜单项
CMD_TOGGLE = "toggle_enabled"
CMD_TEST = "test_notify"
CMD_CONFIG = "open_config"
CMD_RELOAD = "reload_config"
CMD_UPDATE = "update_data"
CMD_AUTOSTART = "toggle_autostart"
CMD_LOG = "open_log"
CMD_QUIT = "quit"
CMD_STATUS = "show_status"
CMD_ABOUT = "about"

_MENU_LAYOUT = [
    (1001, CMD_TOGGLE, "启用提醒"),
    (0, None, ""),
    (1002, CMD_TEST, "立即测试提醒"),
    (1003, CMD_STATUS, "今日工作日状态"),
    (0, None, ""),
    (1004, CMD_CONFIG, "打开配置文件"),
    (1005, CMD_RELOAD, "重新加载配置"),
    (1010, CMD_UPDATE, "更新节假日数据"),
    (1006, CMD_AUTOSTART, "开机自动启动"),
    (0, None, ""),
    (1007, CMD_LOG, "查看日志"),
    (1008, CMD_ABOUT, "关于"),
    (0, None, ""),
    (1009, CMD_QUIT, "退出"),
]


class TrayIcon:
    """托盘图标封装。所有 UI 操作都在内部线程完成。"""

    CLASS_NAME = "WorkdayAlarmTrayWindow"

    def __init__(self, tooltip: str = "工作日提醒", on_command=None) -> None:
        self._tooltip = tooltip[:127]
        self._on_command = on_command
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._hwnd: int | None = None
        self._icon_handles: dict[str, int] = {}
        self._current_kind = "enabled"
        self._enabled = True
        self._autostart = False
        self._status_text = "今日状态未知"
        self._taskbar_created_msg = 0
        self._wndproc_ref = None
        self._start_failed: str | None = None
        self._stopping = False

    # ------------------------------------------------------------------ 生命周期
    def start(self, timeout: float = 5.0) -> bool:
        self._thread = threading.Thread(target=self._run, name="TrayIcon", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        if self._start_failed:
            log.error("托盘启动失败：%s", self._start_failed)
            return False
        return self._hwnd is not None

    def stop(self) -> None:
        self._stopping = True
        self._queue.put(("quit",))
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        app_icon.cleanup()

    # ------------------------------------------------------------------ 对外 API
    def update_state(self, enabled: bool, autostart: bool, status_text: str, tooltip: str | None = None) -> None:
        self._enabled = enabled
        self._autostart = autostart
        self._status_text = status_text
        if tooltip:
            self._tooltip = tooltip[:127]
        self._queue.put(("refresh",))

    def show_balloon(self, title: str, message: str, level: str = "info", timeout_seconds: int = 10) -> None:
        self._queue.put(("balloon", title, message, level, timeout_seconds))

    # ------------------------------------------------------------------ 线程主体
    def _run(self) -> None:
        try:
            self._taskbar_created_msg = user32.RegisterWindowMessageW("TaskbarCreated")
            hinst = kernel32.GetModuleHandleW(None)
            self._wndproc_ref = WNDPROC(self._wndproc)
            wc = WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = self._wndproc_ref
            wc.cbClsExtra = 0
            wc.cbWndExtra = 0
            wc.hInstance = hinst
            wc.hIcon = None
            wc.hCursor = None
            wc.hbrBackground = None
            wc.lpszMenuName = None
            wc.lpszClassName = self.CLASS_NAME

            if not user32.RegisterClassW(ctypes.byref(wc)):
                err = ctypes.get_last_error()
                if err != ERROR_CLASS_ALREADY_EXISTS:
                    raise OSError(f"RegisterClass 失败（错误码 {err}）")

            self._hinst = hinst
            hwnd = self._create_window(message_only=True)
            if not hwnd:
                raise OSError(f"CreateWindowEx 失败（错误码 {ctypes.get_last_error()}）")
            self._hwnd = hwnd

            self._load_icons()
            if not self._add_icon():
                raise OSError(f"Shell_NotifyIcon 添加托盘图标失败（错误码 {ctypes.get_last_error()}）")

            self._ready.set()
            log.info("托盘图标已就绪")
            self._message_loop()
        except Exception as exc:
            self._start_failed = str(exc)
            log.exception("托盘线程异常")
            self._ready.set()
        finally:
            self._cleanup()

    def _create_window(self, message_only: bool):
        """创建隐藏窗口：默认用 message-only 窗口，必要时退化为普通隐藏窗口。"""
        hinst = getattr(self, "_hinst", None) or kernel32.GetModuleHandleW(None)
        parent = HWND_MESSAGE if message_only else None
        style = 0 if message_only else WS_HIDDEN_POPUP
        return user32.CreateWindowExW(
            style, self.CLASS_NAME, "WorkdayAlarm", style, 0, 0, 0, 0,
            parent, None, hinst, None,
        )

    def _recreate_window(self, plain: bool = False) -> int | None:
        """重建窗口（message-only 窗口被 shell 拒绝时改用普通隐藏窗口）。"""
        old = self._hwnd
        if old:
            try:
                user32.DestroyWindow(old)
            except Exception:
                pass
        hwnd = self._create_window(message_only=not plain)
        self._hwnd = hwnd or None
        return self._hwnd

    def _message_loop(self) -> None:
        msg = MSG()
        last_pump = 0.0
        while not self._stopping:
            handled = False
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                if msg.message == WM_QUIT:
                    return
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
                handled = True
            now = time.monotonic()
            if not handled and now - last_pump < 0.03:
                time.sleep(0.01)
                continue
            last_pump = now
            self._drain_queue()

    def _drain_queue(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            action = item[0]
            try:
                if action == "quit":
                    self._stopping = True
                elif action == "refresh":
                    self._apply_state()
                elif action == "balloon":
                    self._show_balloon_internal(*item[1:])
            except Exception:
                log.exception("托盘队列处理失败：%s", action)

    # ------------------------------------------------------------------ 图标
    def _load_icons(self) -> None:
        for kind in ("enabled", "disabled"):
            path = app_icon.icon_path(kind)
            handle = user32.LoadImageW(None, str(path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if not handle:
                handle = user32.LoadImageW(None, str(path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
            if not handle:
                raise OSError(f"加载图标失败：{path}（错误码 {ctypes.get_last_error()}）")
            self._icon_handles[kind] = handle

    def _kind(self) -> str:
        return "enabled" if self._enabled else "disabled"

    def _make_nid(self, flags: int) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._icon_handles.get(self._kind())
        nid.szTip = self._tooltip
        return nid

    def _add_icon(self) -> bool:
        nid = self._make_nid(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        ok = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
        if not ok:
            err = ctypes.get_last_error()
            log.error(
                "NIM_ADD 失败（错误码 %s）：hwnd=%s cbSize=%s 结构体=%s icon=%s tip=%r",
                err, self._hwnd, nid.cbSize, ctypes.sizeof(NOTIFYICONDATAW), nid.hIcon, nid.szTip,
            )
            # 少数环境下消息窗口会被 shell 拒绝，退化成普通隐藏窗口再试一次
            if not getattr(self, "_retried_add", False):
                self._retried_add = True
                replacement = self._recreate_window(plain=True)
                if replacement:
                    log.warning("已改用普通隐藏窗口重试添加托盘图标")
                    nid.hWnd = replacement
                    ok = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
                    log.warning("重试结果：%s（错误码 %s）", ok, ctypes.get_last_error())
        else:
            self._retried_add = False
        return ok

    def _apply_state(self) -> None:
        kind = self._kind()
        nid = self._make_nid(NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        nid.hIcon = self._icon_handles.get(kind)
        if not shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)):
            # 资源管理器重启过，重新添加
            self._add_icon()
        self._current_kind = kind

    def _show_balloon_internal(self, title: str, message: str, level: str, timeout_seconds: int) -> None:
        flags = {
            "info": NIIF_INFO,
            "warning": NIIF_WARNING,
            "error": NIIF_ERROR,
            "none": NIIF_NONE,
        }.get(str(level).lower(), NIIF_INFO)

        nid = self._make_nid(NIF_INFO)
        nid.szInfoTitle = str(title)[:63]
        nid.szInfo = str(message)[:255]
        nid.dwInfoFlags = flags | NIIF_NOSOUND   # 提示音由程序自己播放，避免双重声音
        nid.uVersion = 0
        ok = shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        if not ok:
            if self._add_icon():
                ok = shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        if not ok:
            log.warning("系统通知发送失败（错误码 %s）", ctypes.get_last_error())

    # ------------------------------------------------------------------ 窗口过程
    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                event = int(lparam) & 0xFFFF
                if event == WM_LBUTTONUP:
                    self._emit(CMD_STATUS)
                    return 0
                if event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_menu(hwnd)
                    return 0
            elif msg == WM_COMMAND:
                command = self._menu_commands.get(int(wparam) & 0xFFFF)
                if command:
                    self._emit(command)
                    return 0
            elif msg == self._taskbar_created_msg and self._taskbar_created_msg:
                log.info("检测到资源管理器重启，重新注册托盘图标")
                self._add_icon()
                self._apply_state()
                return 0
            elif msg == WM_DESTROY:
                return 0
        except Exception:
            log.exception("托盘窗口过程异常")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _emit(self, command: str) -> None:
        if self._on_command is None:
            return
        try:
            self._on_command(command)
        except Exception:
            log.exception("托盘命令回调失败：%s", command)

    _menu_commands = {cid: cmd for cid, cmd, _ in _MENU_LAYOUT if cmd}

    def _show_menu(self, hwnd) -> None:
        menu = user32.CreatePopupMenu()
        if not menu:
            log.warning("创建托盘菜单失败")
            return
        try:
            # 第一项是只读状态行
            user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 9001, f"   {self._status_text}"[:120])
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            for cid, command, text in _MENU_LAYOUT:
                if command is None:
                    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                    continue
                flags = MF_STRING
                if command == CMD_TOGGLE and self._enabled:
                    flags |= MF_CHECKED
                if command == CMD_AUTOSTART and self._autostart:
                    flags |= MF_CHECKED
                user32.AppendMenuW(menu, flags, cid, text)

            point = POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(hwnd)
            cmd = user32.TrackPopupMenu(
                menu,
                TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RETURNCMD | TPM_NONOTIFY,
                point.x,
                point.y,
                0,
                hwnd,
                None,
            )
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)
            command = self._menu_commands.get(int(cmd))
            if command:
                self._emit(command)
        finally:
            user32.DestroyMenu(menu)

    # ------------------------------------------------------------------ 清理
    def _cleanup(self) -> None:
        if self._hwnd:
            try:
                nid = self._make_nid(0)
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            except Exception:
                pass
            try:
                user32.DestroyWindow(self._hwnd)
            except Exception:
                pass
            self._hwnd = None
        for handle in self._icon_handles.values():
            try:
                user32.DestroyIcon(handle)
            except Exception:
                pass
        self._icon_handles.clear()
        log.info("托盘图标已释放")
