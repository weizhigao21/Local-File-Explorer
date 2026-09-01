"""
音频模块主题与工具函数
配色常量、通用按钮样式、时间/路径格式化工具
"""
import os


# ---- 配色（3D 黏土风 / 轻拟物新拟态） ----
ACCENT = "#42B4C2"
ACCENT_HOVER = "#48B8BC"
ACCENT_TINT = "rgba(66, 180, 194, 0.15)"
BG_MAIN = "#F5F0E8"
BG_SIDEBAR = "#FDF9F2"
CARD_BG = "#FFFFFF"
CARD_HOVER = "#FFFDF8"
TEXT_PRIMARY = "#555555"
TEXT_MUTED = "#999999"
TEXT_DIM = "#BBBBBB"
INPUT_BG = "#EDE6DA"
BORDER_COLOR = "#D5CDC0"
PLAYER_BG = "#EDE6DA"

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