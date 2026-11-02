# -*- coding: utf-8 -*-
"""提醒呈现：系统通知（托盘气泡）/ 置顶弹窗（Tk）+ 提示音。"""

from __future__ import annotations

import logging
import time
import tkinter as tk
from pathlib import Path

log = logging.getLogger(__name__)

try:
    import winsound
except ImportError:  # pragma: no cover - 仅 Windows 使用
    winsound = None  # type: ignore

_SILENT = {"", "none", "off", "silent", "静音", "mute"}


def play_sound(spec: str) -> None:
    """按配置播放提示音；任何异常都不应影响提醒本身。"""
    if winsound is None:
        return
    text = (spec or "").strip()
    if text.lower() in _SILENT:
        return
    try:
        if text.lower() in ("default", "system", "系统"):
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            return
        wav = Path(text).expanduser()
        if wav.is_file():
            winsound.PlaySound(str(wav), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        else:
            log.warning("提示音文件不存在，改用系统音：%s", text)
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception as exc:  # pragma: no cover
        log.debug("播放提示音失败：%s", exc)


SPI_GETWORKAREA = 0x0030


def work_area_right_bottom(widget: tk.Misc) -> tuple[int, int]:
    """返回屏幕工作区右下角（已排除任务栏）；失败时退回整屏尺寸。"""
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            if rect.right > 0 and rect.bottom > 0:
                return int(rect.right), int(rect.bottom)
    except Exception as exc:  # pragma: no cover
        log.debug("读取工作区失败，改用整屏：%s", exc)
    return widget.winfo_screenwidth(), widget.winfo_screenheight()


class PopupReminder:
    """单个置顶提醒窗口。"""

    WIDTH = 430
    MIN_HEIGHT = 150
    MARGIN = 16
    GAP = 12

    def __init__(
        self,
        root: tk.Misc,
        title: str,
        message: str,
        footer: str = "",
        timeout_seconds: int = 30,
        offset_index: int = 0,
        on_close=None,
    ) -> None:
        self.root = root
        self.on_close = on_close
        self._closed = False
        self._after_ids: list[str] = []
        self._created_at = time.monotonic()

        top = tk.Toplevel(root)
        self.top = top
        self.title = title
        top.withdraw()
        top.title(f"提醒 · {title}")
        top.configure(bg="#f7f8fa")
        top.resizable(False, False)
        try:
            top.attributes("-topmost", True)
        except tk.TclError:
            pass
        top.protocol("WM_DELETE_WINDOW", self.close)

        body = tk.Frame(top, bg="#f7f8fa", padx=20, pady=16)
        body.pack(fill="both", expand=True)

        head = tk.Frame(body, bg="#f7f8fa")
        head.pack(fill="x")
        tk.Label(head, text="⏰", font=("Segoe UI Emoji", 16), bg="#f7f8fa").pack(side="left", padx=(0, 8))
        tk.Label(
            head, text=title, font=("Microsoft YaHei UI", 12, "bold"),
            fg="#1f2328", bg="#f7f8fa", anchor="w", justify="left", wraplength=self.WIDTH - 110,
        ).pack(side="left", fill="x", expand=True)

        text = message or "该处理今天的事情了。"
        tk.Label(
            body, text=text, font=("Microsoft YaHei UI", 10), fg="#3d444d", bg="#f7f8fa",
            anchor="w", justify="left", wraplength=self.WIDTH - 60,
        ).pack(fill="x", pady=(12, 6))

        if footer:
            tk.Label(
                body, text=footer, font=("Microsoft YaHei UI", 8), fg="#8b949e", bg="#f7f8fa",
                anchor="w", justify="left", wraplength=self.WIDTH - 60,
            ).pack(fill="x")

        buttons = tk.Frame(body, bg="#f7f8fa")
        buttons.pack(fill="x", pady=(14, 0))

        self._countdown_label = tk.Label(
            buttons, text="", font=("Microsoft YaHei UI", 8), fg="#8b949e", bg="#f7f8fa"
        )
        self._countdown_label.pack(side="left")

        # 说明：这里用 Label 模拟按钮而不是 tk.Button。tk.Button 的类绑定在某些
        # 环境（含合成鼠标事件）下会自行 invoke 一次 command，导致弹窗刚出现就被关掉；
        # Label + <Button-1> 只响应真实按下，行为可控。
        ok = tk.Label(
            buttons, text="知道了", font=("Microsoft YaHei UI", 9),
            bg="#1e9e5a", fg="white", padx=16, pady=4, cursor="hand2",
        )
        ok.pack(side="right")
        ok.bind("<Button-1>", lambda _event: self.close())
        ok.bind("<Enter>", lambda _event: ok.configure(bg="#18854b"))
        ok.bind("<Leave>", lambda _event: ok.configure(bg="#1e9e5a"))
        top.bind("<Escape>", lambda _event: self.close())
        top.bind("<Return>", lambda _event: self.close())

        self._place(offset_index)
        log.info("显示置顶弹窗：%s", title)
        self._remaining = max(0, int(timeout_seconds))
        if self._remaining:
            self._tick()

        try:
            # 只提升层级、不抢焦点：用户正在别处输入时不会被弹窗打断
            top.lift()
        except tk.TclError:
            pass

    def _place(self, index: int) -> None:
        top = self.top
        top.update_idletasks()
        width = max(self.WIDTH, top.winfo_reqwidth())
        height = max(self.MIN_HEIGHT, top.winfo_reqheight())

        right, bottom = work_area_right_bottom(top)
        x = max(self.MARGIN, right - width - self.MARGIN)
        y = bottom - self.MARGIN - (index + 1) * height - index * self.GAP
        y = max(self.MARGIN, y)
        top.geometry(f"{width}x{height}+{x}+{y}")
        top.deiconify()
        # 窗口边框/标题栏占位会让实际底部超出工作区，按实测偏差再校正一次。
        top.update_idletasks()
        overflow = (top.winfo_rooty() + top.winfo_height()) - (bottom - self.MARGIN)
        if overflow > 0:
            top.geometry(f"{width}x{height}+{x}+{max(self.MARGIN, y - overflow)}")

    def _tick(self) -> None:
        if self._closed:
            return
        if self._remaining <= 0:
            self.close()
            return
        self._countdown_label.configure(text=f"{self._remaining} 秒后自动关闭")
        self._remaining -= 1
        self._after_ids.append(self.top.after(1000, self._tick))

    def close(self, force: bool = False) -> None:
        if self._closed:
            return
        # 防止合成事件在弹窗刚显示时把提醒直接关掉
        if not force and (time.monotonic() - self._created_at) < 1.0:
            log.debug("忽略过早的关闭请求：%s", getattr(self, "title", ""))
            return
        self._closed = True
        log.info("关闭置顶弹窗：%s", getattr(self, "title", ""))
        for after_id in self._after_ids:
            try:
                self.top.after_cancel(after_id)
            except tk.TclError:
                pass
        self._after_ids.clear()
        try:
            self.top.destroy()
        except tk.TclError:
            pass
        if self.on_close:
            try:
                self.on_close(self)
            except Exception:
                log.exception("弹窗关闭回调异常")


class PopupManager:
    """管理多个弹窗的堆叠位置。"""

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self._active: list[PopupReminder] = []

    def show(self, title: str, message: str, footer: str = "", timeout_seconds: int = 30) -> PopupReminder:
        popup = PopupReminder(
            self.root,
            title=title,
            message=message,
            footer=footer,
            timeout_seconds=timeout_seconds,
            offset_index=len(self._active),
            on_close=self._on_closed,
        )
        self._active.append(popup)
        return popup

    def _on_closed(self, popup: PopupReminder) -> None:
        if popup in self._active:
            self._active.remove(popup)
        for index, item in enumerate(list(self._active)):
            try:
                item._place(index)
            except tk.TclError:
                pass

    def close_all(self) -> None:
        for popup in list(self._active):
            popup.close()
        self._active.clear()
