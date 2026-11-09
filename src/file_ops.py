"""文件系统操作（纯 Python，不含 Qt）：资源管理器定位、默认程序打开、移入回收站

为什么单独一层：UI 只该负责弹菜单和确认框，真正碰系统的代码集中在这里，
既能被桌面端复用，将来网页端要加「删掉这张图」也能直接调用；而且不依赖
Qt，可以在 pytest 里直接测（不建 QWidget，避开离屏崩溃）。

**删除一律走回收站**，不提供「彻底删除」：这是管理本地收藏的工具，
一次误点就把原图毁掉是不可接受的，删错了至少还能找回来。
"""
from __future__ import annotations

import os
import subprocess
import sys

IS_WINDOWS = sys.platform.startswith("win")

# SHFileOperationW 的常量（shellapi.h）
_FO_DELETE = 3
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040  # 关键：走回收站而不是直接抹掉
_FOF_NOERRORUI = 0x0400


def reveal(path) -> bool:
    """在资源管理器里定位 path：目录 → 打开它；文件 → 打开所在文件夹并选中它

    返回 False 表示路径不存在或系统调用失败。UI 必须据此给出提示——
    点了菜单没任何反应，用户只会以为程序坏了。
    """
    if not path:
        return False
    target = os.path.normpath(os.path.abspath(str(path)))
    if not os.path.exists(target):
        return False

    if not IS_WINDOWS:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        where = target if os.path.isdir(target) else os.path.dirname(target)
        try:
            subprocess.Popen([opener, where])
            return True
        except OSError:
            return False

    try:
        if os.path.isdir(target):
            subprocess.Popen(["explorer", target])
        else:
            # 逗号是 /select 参数的一部分，必须紧贴参数名
            subprocess.Popen(["explorer", "/select,", target])
        return True
    except OSError:
        return False


def open_with_default(path) -> bool:
    """用系统默认程序打开（图片会进系统看图器，目录会进资源管理器）"""
    if not path:
        return False
    target = os.path.normpath(os.path.abspath(str(path)))
    if not os.path.exists(target):
        return False
    try:
        if IS_WINDOWS:
            os.startfile(target)  # noqa: S606 - Windows 专用，路径来自本地数据库
        else:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, target])
        return True
    except OSError:
        return False


def move_to_recycle_bin(paths):
    """把文件/目录移入回收站（可恢复）

    返回 (成功数, 失败路径列表)。批处理用一次 SHFileOperationW，
    但要逐个复查文件是否真的消失——文件被别的程序占用时系统返回值
    仍可能是 0，只看返回值会误报成功。

    非 Windows 或没有任何存在的路径时成功数为 0，由调用方提示。
    """
    if isinstance(paths, (str, bytes, os.PathLike)):
        paths = [paths]
    targets = [os.path.normpath(os.path.abspath(str(p))) for p in paths]
    targets = [p for p in targets if os.path.exists(p)]
    if not targets:
        return 0, []
    if not IS_WINDOWS:
        return 0, targets

    import ctypes
    from ctypes import wintypes

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        # 字段顺序与 shellapi.h 一致；fFlags 是 WORD，后面 BOOL 的 2 字节
        # 空洞由默认对齐补上，不要设 _pack_
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    # pFrom 要求「每个路径各以 \0 结尾，整体再加一个 \0」
    source = "\0".join(targets) + "\0\0"
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = _FO_DELETE
    op.pFrom = source
    op.pTo = None
    op.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
    op.fAnyOperationsAborted = False
    op.hNameMappings = None
    op.lpszProgressTitle = None

    try:
        ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except (OSError, AttributeError):
        return 0, targets

    failed = [p for p in targets if os.path.exists(p)]
    return len(targets) - len(failed), failed
