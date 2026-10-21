"""
Windows 全局热键

系统媒体键（播放/暂停键）走 SMTC，Qt 没有内置封装，用 ctypes 调 WinRT 既复杂又易碎；
这里先用 RegisterHotKey 提供一套稳定的全局快捷键：
    Ctrl+Alt+Space  播放 / 暂停
    Ctrl+Alt+Left   上一首
    Ctrl+Alt+Right  下一首

非 Windows 平台、或热键被其他程序占用时静默降级（register() 返回 False），
不影响主程序；窗口内的 Space / ← / → 仍然可用。
"""
import ctypes
import sys
from contextlib import suppress
from ctypes import wintypes

from PyQt6.QtCore import QAbstractNativeEventFilter
from PyQt6.QtWidgets import QApplication

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_SPACE = 0x20
VK_LEFT = 0x25
VK_RIGHT = 0x27

_HOTKEY_IDS = (0x4C01, 0x4C02, 0x4C03)


class GlobalHotkeys(QAbstractNativeEventFilter):
    def __init__(self, playback):
        super().__init__()
        self.playback = playback
        self._user32 = None
        self._registered = []
        self._installed = False
        self._actions = {
            _HOTKEY_IDS[0]: self._toggle,
            _HOTKEY_IDS[1]: self.playback.previous,
            _HOTKEY_IDS[2]: self.playback.next,
        }

    def register(self):
        """注册全局热键；返回是否全部注册成功"""
        if sys.platform != "win32":
            return False
        try:
            self._user32 = ctypes.windll.user32
        except (AttributeError, OSError):
            return False

        for hotkey_id, vk in zip(_HOTKEY_IDS, (VK_SPACE, VK_LEFT, VK_RIGHT), strict=True):
            if not self._user32.RegisterHotKey(
                None, hotkey_id, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, vk
            ):
                self.unregister()
                return False
            self._registered.append(hotkey_id)

        app = QApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self)
            self._installed = True
        return True

    def unregister(self):
        if self._user32 is not None:
            for hotkey_id in self._registered:
                with suppress(AttributeError, OSError):
                    self._user32.UnregisterHotKey(None, hotkey_id)
        self._registered = []
        if self._installed:
            app = QApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self)
            self._installed = False

    def _toggle(self):
        self.playback.toggle()

    def nativeEventFilter(self, event_type, message):
        if not self._registered:
            return False, 0
        try:
            is_windows_msg = bytes(event_type) == b"windows_generic_MSG"
        except TypeError:
            is_windows_msg = False
        if not is_windows_msg:
            return False, 0
        try:
            msg = wintypes.MSG.from_address(int(message))
        except (TypeError, ValueError):
            return False, 0
        if msg.message == WM_HOTKEY:
            action = self._actions.get(int(msg.wParam))
            if action is not None:
                action()
                return True, 0
        return False, 0


__all__ = ["GlobalHotkeys"]
