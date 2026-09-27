"""
音频模块主题与工具函数
配色常量、通用按钮样式、时间/路径格式化工具

配色令牌统一来自 ui.theme_base（见该模块说明），此处继续对外导出，
保持 `from ui.audio_theme import ACCENT` 这类既有写法不变。
"""
import os

from ui.theme_base import (
    ACCENT,
    ACCENT_HOVER,
    ACCENT_TINT,
    BG_MAIN,
    BG_SIDEBAR,
    BORDER_COLOR,
    CARD_BG,
    CARD_HOVER,
    INPUT_BG,
    PLAYER_BG,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
)

__all__ = [
    # 从 theme_base 转出的设计令牌
    "ACCENT", "ACCENT_HOVER", "ACCENT_TINT",
    "BG_MAIN", "BG_SIDEBAR", "CARD_BG", "CARD_HOVER", "INPUT_BG", "PLAYER_BG",
    "TEXT_PRIMARY", "TEXT_MUTED", "TEXT_DIM", "BORDER_COLOR",
    # 本模块自有的样式与工具
    "BTN_QSS", "MENU_QSS", "format_time", "get_basename",
]

BTN_QSS = """
QPushButton {
    background-color: #EDE6DA; color: #555;
    padding: 6px 14px; border: 1px solid #D5CDC0; border-radius: 4px;
}
QPushButton:hover {
    background-color: #F3EFE8; border: 1px solid """ + ACCENT + """; color: #444;
}
QPushButton:pressed {
    background-color: #48B8BC; border: 1px solid #48B8BC; color: #fff;
}
"""

# 右键菜单样式：悬停时背景与文字颜色同时变化，避免浅底浅字看不清
MENU_QSS = f"""
QMenu {{
    background-color: {BG_SIDEBAR}; color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_COLOR}; border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 24px; border-radius: 4px; font-size: 12px;
}}
QMenu::item:selected {{
    background-color: {ACCENT}; color: #FFFFFF;
}}
QMenu::separator {{
    height: 1px; background-color: {BORDER_COLOR}; margin: 4px 8px;
}}
"""


def format_time(seconds):
    """将秒数格式化为 MM:SS"""
    if seconds <= 0:
        return "00:00"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


def get_basename(path):
    """获取路径的显示名"""
    return os.path.basename(path) if path else "根目录"
