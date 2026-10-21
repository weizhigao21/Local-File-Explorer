"""格式图标导出：.ico 结构、尺寸齐全、shell 真的能读出来"""
import ctypes
import os
import pathlib
import sys
import uuid
from contextlib import suppress

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

import system_integration as si  # noqa: E402
from resource_manager import ico  # noqa: E402
from ui import format_icon_export as export  # noqa: E402
from ui.file_format_icons import AUDIO_EXTENSIONS  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="module")
def exported(tmp_path_factory, qapp):
    """只导出一次：22 种格式各一张 + audio.ico，模块内共用（依赖 qapp 才能画图）"""
    target = tmp_path_factory.mktemp("icons")
    return export.export_icons(str(target))


# ==================== 文件层面 ====================
def test_export_covers_all_formats_and_generic(exported):
    names = set(exported)
    assert {ext.lstrip(".") for ext in AUDIO_EXTENSIONS} <= names
    assert "audio" in names, "旧版共享 ProgID 需要一个通用兜底图标"
    assert len(names) == len(AUDIO_EXTENSIONS) + 1


def test_every_ico_has_all_sizes(exported):
    for name, path in exported.items():
        entries = ico.read_directory(pathlib.Path(path).read_bytes())
        sizes = [size for size, _length, _offset in entries]
        assert sizes == sorted(export.ICO_SIZES), f"{name} 尺寸不全：{sizes}"


def test_small_entries_are_dib_and_256_is_png(exported):
    data = pathlib.Path(exported["mp3"]).read_bytes()
    for size, length, offset in ico.read_directory(data):
        chunk = data[offset:offset + length]
        if size >= export.PNG_MIN_SIZE:
            assert chunk.startswith(b"\x89PNG"), "256 应该是 PNG（DIB 单条就 256 KB）"
        else:
            assert chunk[:4] == (40).to_bytes(4, "little"), f"{size}px 应该是 32bpp DIB"


def test_icons_are_not_empty(exported):
    for name, path in exported.items():
        assert os.path.getsize(path) > 2048, f"{name}.ico 太小，可能没画上内容"


def test_icons_present_helper(exported):
    directory = os.path.dirname(exported["mp3"])
    assert export.icons_present(directory) is True
    assert export.icons_present(directory, extensions=[".mp3"]) is True

    os.remove(exported["flac"])
    try:
        assert export.icons_present(directory) is False
    finally:
        pathlib.Path(exported["flac"]).write_bytes(export.ico_bytes("flac"))


def test_clear_icons(tmp_path):
    directory = str(tmp_path / "icons")
    export.export_icons(directory, extensions=["mp3", "flac"])
    assert export.clear_icons(directory) == 3  # mp3 + flac + audio
    assert not any(name.endswith(".ico") for name in os.listdir(directory))


# ==================== Windows 实测：shell 是否认这个 .ico ====================
class _SHFILEINFO(ctypes.Structure):
    _fields_ = [("hIcon", ctypes.c_void_p), ("iIcon", ctypes.c_int),
                ("dwAttributes", ctypes.c_uint),
                ("szDisplayName", ctypes.c_wchar * 260),
                ("szTypeName", ctypes.c_wchar * 80)]


class _ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", ctypes.c_int), ("xHotspot", ctypes.c_uint),
                ("yHotspot", ctypes.c_uint),
                ("hbmMask", ctypes.c_void_p), ("hbmColor", ctypes.c_void_p)]


class _BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                ("bmPlanes", ctypes.c_ushort), ("bmBitsPixel", ctypes.c_ushort),
                ("bmBits", ctypes.c_void_p)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_ushort),
                ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_uint),
                ("biSizeImage", ctypes.c_uint), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint),
                ("biClrImportant", ctypes.c_uint)]


def _handle(value):
    return ctypes.c_void_p(value & 0xFFFFFFFFFFFFFFFF) if value else ctypes.c_void_p(0)


def _shell_icon_rgb(path):
    """用 shell 自己的解析结果取图标平均色（真正的"资源管理器会怎么显示"）"""
    shell32 = ctypes.windll.shell32
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    shell32.SHGetFileInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint,
                                       ctypes.POINTER(_SHFILEINFO), ctypes.c_uint,
                                       ctypes.c_uint]
    user32.GetIconInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ICONINFO)]
    gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_uint]
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.GetDC.restype = ctypes.c_void_p

    info = _SHFILEINFO()
    # SHGFI_ICON | SHGFI_LARGEICON
    if not shell32.SHGetFileInfoW(ctypes.c_wchar_p(path), 0x80, ctypes.byref(info),
                                  ctypes.sizeof(info), 0x100):
        return None
    icon_info = _ICONINFO()
    if not user32.GetIconInfo(_handle(info.hIcon), ctypes.byref(icon_info)):
        return None
    bitmap = _BITMAP()
    gdi32.GetObjectW(_handle(icon_info.hbmColor), ctypes.sizeof(bitmap),
                     ctypes.byref(bitmap))
    width, height = bitmap.bmWidth, bitmap.bmHeight
    header = _BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    header.biWidth, header.biHeight = width, -height
    header.biPlanes, header.biBitCount = 1, 32
    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(user32.GetDC(None), _handle(icon_info.hbmColor), 0, height,
                    buffer, ctypes.byref(header), 0)
    user32.DestroyIcon(_handle(info.hIcon))

    raw = buffer.raw
    red = green = blue = count = 0
    for y in range(0, height, 2):
        for x in range(0, width, 2):
            index = (y * width + x) * 4
            b, g, r, a = raw[index], raw[index + 1], raw[index + 2], raw[index + 3]
            if a > 100:
                red, green, blue, count = red + r, green + g, blue + b, count + 1
    if not count:
        return None
    return red // count, green // count, blue // count


@pytest.mark.skipif(os.name != "nt", reason="shell 图标解析是 Windows 专有")
def test_windows_shell_reads_our_icon(qapp, tmp_path):
    """端到端守卫：把 .ico 挂到一个全新扩展名，shell 必须读成青蓝色

    真机验证过的坑：只有 ProgID 的 DefaultIcon 会被采用，扩展名上的
    DefaultIcon 在 ProgID 已有图标时会被忽略（见 system_integration 模块注释）。
    """
    import winreg

    unique = f"lrm{uuid.uuid4().hex[:8]}"
    ext = f".{unique}"
    prog_id = f"LrmVerify.{unique}"
    icon = os.path.join(str(tmp_path), f"{unique}.ico")
    pathlib.Path(icon).write_bytes(export.ico_bytes("mp3"))

    root = winreg.HKEY_CURRENT_USER
    classes = r"Software\Classes"
    sample = os.path.join(str(tmp_path), f"sample{ext}")
    pathlib.Path(sample).write_bytes(b"\x00" * 64)

    try:
        with winreg.CreateKeyEx(root, rf"{classes}\{prog_id}\DefaultIcon", 0,
                                winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"{icon},0")
        with winreg.CreateKeyEx(root, rf"{classes}\{ext}", 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, prog_id)

        rgb = _shell_icon_rgb(sample)
        assert rgb is not None, "shell 没有返回图标"
        red, green, blue = rgb
        assert red > green + 20 and red > blue + 20, (
            f"shell 读到的不是我们的赤陶图标：rgb{rgb}"
        )
    finally:
        si._delete_tree(root, rf"{classes}\{prog_id}")
        si._delete_tree(root, rf"{classes}\{ext}")
        for path in (icon, sample):
            with suppress(OSError):
                os.remove(path)


def test_ico_paths_follow_data_dir():
    """图标目录要跟数据库、缩略图缓存同一套约定（打包后是 exe 同级的 data/）"""
    assert si.format_icon_dir().replace("\\", "/").endswith("/data/format_icons")
    assert si.icon_path("MP3").endswith("mp3.ico")
    assert si.generic_icon_path().endswith("audio.ico")


def test_export_bytes_are_deterministic(qapp):
    """同样输入必须得到同样字节，否则每次安装都会让图标缓存全失效"""
    assert export.ico_bytes("flac") == export.ico_bytes("flac")
    assert export.ico_bytes("mp3") != export.ico_bytes("flac")


if sys.platform != "win32":  # pragma: no cover
    pytestmark = pytest.mark.skip(reason="图标注册仅 Windows 可用")
