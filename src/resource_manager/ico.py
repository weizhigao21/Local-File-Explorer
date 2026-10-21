"""
ICO 文件写入（纯 Python，无 Qt / Pillow 依赖）

Windows 资源管理器读的是标准 ICO 容器：

    ICONDIR            6 字节：reserved(0) / type(1=icon) / count
    ICONDIRENTRY × N  16 字节：宽 / 高 / 调色板数 / 保留 / 平面 / 位深 /
                              数据长度 / 数据偏移
    图像数据 × N

每个条目的数据有两种合法形式：
    - 32bpp 的 BMP(DIB)：信息头 + 自底向上的 BGRA 像素 + 1bpp AND 掩码；
    - PNG：Vista 起支持，通常只用于 256×256（体积小很多）。

Windows 10/11 对两种都认，但小尺寸上用 DIB 兼容性最好（老式列表视图、
部分第三方 shell 扩展只走 DIB 分支），所以这里按尺寸分流：128 及以下走
DIB，256 走 PNG。

本模块只处理字节，不涉及任何绘制，因此可以放在核心层并被单独测试。
"""
from __future__ import annotations

import struct
from collections.abc import Sequence

ICON_TYPE = 1
# ICONDIRENTRY 里宽/高各占 1 字节，256 必须写成 0（规范里的约定）
_MAX_DIR_BYTE = 256
_BITMAP_HEADER_SIZE = 40
_COLOR_PLANES = 1
_BIT_COUNT = 32


def bmp_entry(size: int, rgba: bytes) -> tuple[int, bytes]:
    """把 size×size 的 RGBA 像素打包成 ICO 的 DIB 条目数据"""
    if size <= 0 or size > _MAX_DIR_BYTE:
        raise ValueError(f"尺寸超出 ICO 支持范围: {size}")
    expected = size * size * 4
    if len(rgba) != expected:
        raise ValueError(f"像素数据长度不符：期望 {expected}，实际 {len(rgba)}")

    # biHeight 要写成两倍：高度里同时包含 XOR（像素）与 AND（掩码）两部分
    header = struct.pack(
        "<IiiHHIIiiII",
        _BITMAP_HEADER_SIZE,
        size,
        size * 2,
        _COLOR_PLANES,
        _BIT_COUNT,
        0,  # BI_RGB，不压缩
        size * size * 4,
        0,
        0,
        0,
        0,
    )

    # 像素按行自下而上存储，且顺序是 BGRA
    rows = []
    stride = size * 4
    for y in range(size - 1, -1, -1):
        row = rgba[y * stride:(y + 1) * stride]
        bgra = bytearray(stride)
        bgra[0::4] = row[2::4]
        bgra[1::4] = row[1::4]
        bgra[2::4] = row[0::4]
        bgra[3::4] = row[3::4]
        rows.append(bytes(bgra))

    # AND 掩码：32bpp 图靠 alpha 通道透明，掩码全 0 即可；每行按 32 位对齐
    mask_stride = ((size + 31) // 32) * 4
    mask = b"\x00" * (mask_stride * size)

    return size, header + b"".join(rows) + mask


def png_entry(size: int, png: bytes) -> tuple[int, bytes]:
    """包装一个 PNG 条目（256×256 用）"""
    if size <= 0 or size > _MAX_DIR_BYTE:
        raise ValueError(f"尺寸超出 ICO 支持范围: {size}")
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("PNG 数据缺少文件头")
    return size, png


def build_ico(entries: Sequence[tuple[int, bytes]]) -> bytes:
    """把若干 (尺寸, 图像数据) 组装成完整 ICO 文件内容"""
    if not entries:
        raise ValueError("ICO 至少需要一个尺寸")
    sizes = [size for size, _payload in entries]
    if len(set(sizes)) != len(sizes):
        raise ValueError(f"尺寸重复: {sizes}")

    entries = sorted(entries, key=lambda item: item[0])
    offset = 6 + 16 * len(entries)
    directory = bytearray()
    payloads = []
    for size, payload in entries:
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= _MAX_DIR_BYTE else size,
            0 if size >= _MAX_DIR_BYTE else size,
            0,  # 调色板颜色数：真彩色写 0
            0,  # 保留字节
            _COLOR_PLANES,
            _BIT_COUNT,
            len(payload),
            offset,
        )
        payloads.append(payload)
        offset += len(payload)

    head = struct.pack("<HHH", 0, ICON_TYPE, len(entries))
    return head + bytes(directory) + b"".join(payloads)


def write_ico(path: str, entries: Sequence[tuple[int, bytes]]) -> str:
    """写文件；返回路径（目录不存在时创建）"""
    import os

    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    data = build_ico(entries)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def read_directory(data: bytes) -> list[tuple[int, int, int]]:
    """解析 ICO 目录，返回 [(尺寸, 数据长度, 数据偏移)]（测试与校验用）"""
    if len(data) < 6:
        raise ValueError("不是合法的 ICO：文件过短")
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    if reserved != 0 or kind != ICON_TYPE:
        raise ValueError(f"不是 ICO 文件：reserved={reserved} type={kind}")
    result = []
    for index in range(count):
        base = 6 + index * 16
        width, _height, _colors, _res, _planes, _bits, length, offset = (
            struct.unpack("<BBBBHHII", data[base:base + 16])
        )
        result.append((width or _MAX_DIR_BYTE, length, offset))
    return result


__all__ = [
    "ICON_TYPE", "bmp_entry", "png_entry", "build_ico", "write_ico", "read_directory",
]
