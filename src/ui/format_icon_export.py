"""
把格式图标导出成 Windows 资源管理器能直接使用的 .ico 文件

`file_format_icons` 负责"长什么样"，这里负责"怎么落到磁盘上让系统读取"：

    data/format_icons/mp3.ico      ← 16/20/24/32/40/48/64/128（DIB）+ 256（PNG）
    data/format_icons/flac.ico
    …（22 种音频格式各一个）

为什么要落盘而不是运行时内存里给 QIcon：
    资源管理器的图标由系统 shell 进程读取，它只认磁盘上的 `.ico` / `.exe`，
    程序退出后图标依然要能显示，所以必须写成实体文件并由注册表指向它。
"""
from __future__ import annotations

import os

from PyQt6.QtCore import QBuffer, QIODevice
from PyQt6.QtGui import QImage

from resource_manager import ico
from ui.file_format_icons import AUDIO_EXTENSIONS, format_pixmap, normalize_ext

# .ico 里放这些尺寸：16/24/32/48 覆盖列表/中图标/大图标，
# 20/40/64/128 兼顾 125%~200% 缩放下的取整，256 给「超大图标」视图。
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
# 这个尺寸以上用 PNG 压缩存储（256 的 DIB 单条就有 256 KB）
PNG_MIN_SIZE = 256


def _rgba_bytes(image: QImage) -> bytes:
    """取出 RGBA8888 原始像素（行序为自上而下，ICO 写入时再翻转）"""
    if image.format() != QImage.Format.Format_RGBA8888:
        image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return bytes(image.constBits().asstring(image.sizeInBytes()))


def _png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("PNG 编码失败")
    return bytes(buffer.data())


def ico_entries(ext: str, sizes: tuple[int, ...] = ICO_SIZES):
    """.ico 的各尺寸条目（DIB 为主，256 用 PNG）"""
    entries = []
    for side in sizes:
        image = format_pixmap(ext, side, dpr=1.0).toImage()
        if side >= PNG_MIN_SIZE:
            entries.append(ico.png_entry(side, _png_bytes(image)))
        else:
            entries.append(ico.bmp_entry(side, _rgba_bytes(image)))
    return entries


def ico_bytes(ext: str, sizes: tuple[int, ...] = ICO_SIZES) -> bytes:
    return ico.build_ico(ico_entries(ext, sizes))


def export_icons(icon_dir: str, extensions=None, sizes: tuple[int, ...] = ICO_SIZES):
    """把每种格式写成 `<icon_dir>/<ext>.ico`；返回 {扩展名: 路径}

    另外写一张 `audio.ico`（图标上是 AUDIO）：早期版本用单一共享 ProgID
    注册过本程序，那种 ProgID 没法按格式区分图标，用它兜底更合适。
    """
    os.makedirs(icon_dir, exist_ok=True)
    targets = sorted(AUDIO_EXTENSIONS if extensions is None else extensions)
    written = {}
    for ext in targets:
        name = normalize_ext(ext)
        path = os.path.join(icon_dir, f"{name}.ico")
        with open(path, "wb") as handle:
            handle.write(ico_bytes(name, sizes))
        written[name] = path

    generic = os.path.join(icon_dir, "audio.ico")
    with open(generic, "wb") as handle:
        handle.write(ico_bytes("audio", sizes))
    written["audio"] = generic
    return written


def icons_present(icon_dir: str, extensions=None) -> bool:
    """目录里的 .ico 是否齐全（只查存在性，不校验内容）"""
    targets = AUDIO_EXTENSIONS if extensions is None else set(extensions)
    return all(
        os.path.isfile(os.path.join(icon_dir, f"{normalize_ext(ext)}.ico"))
        for ext in targets
    )


def clear_icons(icon_dir: str) -> int:
    """删除导出的 .ico；返回删除数量"""
    if not os.path.isdir(icon_dir):
        return 0
    removed = 0
    for name in os.listdir(icon_dir):
        if name.lower().endswith(".ico"):
            try:
                os.remove(os.path.join(icon_dir, name))
                removed += 1
            except OSError:
                pass
    return removed


__all__ = [
    "ICO_SIZES", "PNG_MIN_SIZE", "ico_entries", "ico_bytes",
    "export_icons", "icons_present", "clear_icons",
]
