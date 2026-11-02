# -*- coding: utf-8 -*-
"""程序化生成托盘图标（纯标准库，不依赖 Pillow / 外部 .ico 文件）。

同一份代码既能给托盘用，也能在打包时导出 exe 图标，保证“单文件 + 零资源依赖”。
图标造型：彩色圆底 + 白色时钟指针；启用为绿色，暂停为灰色。
"""

from __future__ import annotations

import math
import os
import struct
import tempfile
from pathlib import Path

RGB = tuple[int, int, int]

GREEN: RGB = (0x1E, 0x9E, 0x5A)
GRAY: RGB = (0x8B, 0x93, 0x9E)
BLUE: RGB = (0x2B, 0x6C, 0xB0)  # 备用

_SIZES = (16, 20, 24, 32, 40, 48, 64)


def _dist_to_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    length_sq = vx * vx + vy * vy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / length_sq))
    dx, dy = px - (ax + t * vx), py - (ay + t * vy)
    return math.hypot(dx, dy)


def _render_rgba(size: int, color: RGB, supersample: int = 4) -> list[tuple[int, int, int, int]]:
    """渲染 size×size 的 RGBA 像素（行优先，自上而下）。"""
    s = size * supersample
    cx = cy = s / 2.0
    radius = s / 2.0 - supersample * 0.5
    ring_r = radius * 0.70
    ring_w = max(supersample * 0.9, radius * 0.10)
    hand_w = max(supersample * 0.85, radius * 0.085)

    # 时钟指针（12 点方向为 -y）
    def end(angle_deg: float, length: float) -> tuple[float, float]:
        rad = math.radians(angle_deg - 90.0)
        return cx + math.cos(rad) * length, cy + math.sin(rad) * length

    minute_end = end(0.0, radius * 0.52)      # 指向 12 点（分针竖直）
    hour_end = end(-120.0, radius * 0.34)     # 指向 8 点方向

    accum = [[[0, 0, 0, 0] for _ in range(size)] for _ in range(size)]
    ss = supersample
    for ys in range(s):
        py = ys + 0.5
        for xs in range(s):
            px = xs + 0.5
            dx, dy = px - cx, py - cy
            if dx * dx + dy * dy > radius * radius:
                continue
            white = 0.0
            dist_ring = abs(math.hypot(dx, dy) - ring_r)
            if dist_ring <= ring_w / 2:
                white = 1.0
            elif math.hypot(dx, dy) <= radius * 0.13:
                white = 1.0
            else:
                dmin = min(
                    _dist_to_segment(px, py, cx, cy, minute_end[0], minute_end[1]),
                    _dist_to_segment(px, py, cx, cy, hour_end[0], hour_end[1]),
                )
                if dmin <= hand_w / 2:
                    white = 1.0
            if white:
                r, g, b = 255, 255, 255
            else:
                r, g, b = color
            cell = accum[ys // ss][xs // ss]
            cell[0] += r
            cell[1] += g
            cell[2] += b
            cell[3] += 255

    total = ss * ss
    out: list[tuple[int, int, int, int]] = []
    for row in accum:
        for cell in row:
            out.append(
                (
                    cell[0] // total,
                    cell[1] // total,
                    cell[2] // total,
                    cell[3] // total,
                )
            )
    return out


def _bmp_payload(size: int, pixels: list[tuple[int, int, int, int]]) -> bytes:
    """生成 ICO 内的 BMP 数据（32bpp，像素自下而上，含 AND 掩码）。"""
    header = struct.pack(
        "<IiiHHIIiiII",
        40,          # biSize
        size,        # biWidth
        size * 2,    # biHeight（XOR + AND 两份）
        1,           # biPlanes
        32,          # biBitCount
        0,           # biCompression = BI_RGB
        size * size * 4,
        0, 0, 0, 0,
    )
    body = bytearray()
    for y in range(size - 1, -1, -1):
        for x in range(size):
            r, g, b, a = pixels[y * size + x]
            body += bytes((b, g, r, a))
    # AND 掩码：每行按 4 字节对齐，全 0 表示“不透明区域由 alpha 决定”
    mask_row = ((size + 31) // 32) * 4
    body += b"\x00" * (mask_row * size)
    return header + bytes(body)


def build_ico(color: RGB, sizes: tuple[int, ...] = _SIZES) -> bytes:
    images = []
    for size in sizes:
        pixels = _render_rgba(size, color)
        images.append((size, _bmp_payload(size, pixels)))

    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries = bytearray()
    blobs = bytearray()
    for size, payload in images:
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0, 0, 1, 32, len(payload), offset,
        )
        blobs += payload
        offset += len(payload)
    return header + bytes(entries) + bytes(blobs)


_temp_files: list[str] = []


def write_ico(color: RGB, filename: str) -> Path:
    """把图标写进临时目录，返回路径（供 LoadImageW 使用）。"""
    directory = Path(tempfile.gettempdir()) / "WorkdayAlarm"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    data = build_ico(color)
    try:
        if path.read_bytes() == data:
            return path
    except OSError:
        pass
    path.write_bytes(data)
    _temp_files.append(str(path))
    return path


def icon_path(kind: str = "enabled") -> Path:
    color = GREEN if kind == "enabled" else GRAY
    return write_ico(color, f"tray_{kind}.ico")


def export_app_ico(path: Path, color: RGB = GREEN) -> Path:
    """导出 exe 图标（打包脚本使用）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_ico(color))
    return path


def cleanup() -> None:
    for name in list(_temp_files):
        try:
            os.unlink(name)
        except OSError:
            pass
        _temp_files.remove(name)
