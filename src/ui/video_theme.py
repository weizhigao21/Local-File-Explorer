"""视频模块主题

配色令牌统一来自 ui.theme_base（与写真、音频、启动器同一套米色系）；
本文件只额外定义视频模块自己的按钮/滚动条 QSS，不再复制一份颜色取值
——`tests/test_ui_theme.py::test_palette_single_sourced` 会盯着取值漂移。

注意：QSS 的 background-color 不吃 rgba()/#AARRGGBB（Qt 静默丢弃整条声明），
所有半透明观感都用「叠在底色上的不透明等价色」表达。
"""
from ui.theme_base import (  # noqa: F401 - 转出供 `from ui.video_theme import ACCENT` 使用
    ACCENT,
    ACCENT_HOVER,
    ACCENT_TINT,
    BG_MAIN,
    BG_PAGE,
    BG_SIDEBAR,
    CARD_BG,
    CARD_BORDER,
    CARD_BORDER_HOVER,
    CARD_HOVER,
    INPUT_BG,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)

__all__ = [
    "ACCENT", "ACCENT_HOVER", "ACCENT_TINT",
    "BG_MAIN", "BG_PAGE", "BG_SIDEBAR", "CARD_BG", "CARD_HOVER",
    "CARD_BORDER", "CARD_BORDER_HOVER", "INPUT_BG",
    "TEXT_PRIMARY", "TEXT_SECONDARY", "TEXT_MUTED", "TEXT_DIM",
    "ACCENT_PRESSED", "BADGE_BG", "GLOBAL_SCROLLBAR_QSS", "BTN_QSS",
    "SEARCH_QSS", "LIST_QSS",
]

# ---- 视频模块特有档位 ----
ACCENT_PRESSED = "#9E4B30"     # 强调色按下
BADGE_BG = "#3D3830"           # 封面右下角时长角标（深咖不透明底 + 白字）

# 全局滚动条样式（窄条 + 圆角 + 悬停变亮）
GLOBAL_SCROLLBAR_QSS = """
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #CCC0B0;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: #BBB0A0;
}
QScrollBar::handle:vertical:pressed {
    background: """ + ACCENT + """;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}
QScrollBar:horizontal {
    background: transparent;
    height: 0;
}
"""

# 通用按钮（hover 变 accent 描边）
BTN_QSS = f"""
QPushButton {{
    background-color: {INPUT_BG};
    color: {TEXT_PRIMARY};
    padding: 6px 14px;
    border: 1px solid {CARD_BORDER_HOVER};
    border-radius: 4px;
    font-size: 12px;
}}
QPushButton:hover {{
    background-color: #F3EFE8;
    border: 1px solid {ACCENT};
}}
QPushButton:pressed {{
    background-color: {ACCENT_PRESSED};
    border: 1px solid {ACCENT_PRESSED};
    color: #FFFFFF;
}}
QPushButton:disabled {{
    color: {TEXT_DIM};
    border: 1px solid {CARD_BORDER};
}}
"""

# 搜索框
SEARCH_QSS = f"""
QLineEdit {{
    background-color: {INPUT_BG};
    color: {TEXT_PRIMARY};
    padding: 6px 10px;
    border: 1px solid {CARD_BORDER_HOVER};
    border-radius: 4px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus {{
    border: 1px solid {ACCENT};
    background-color: #F3EFE8;
}}
"""

# 侧栏作者列表 / 视频行列表共用
LIST_QSS = f"""
QListWidget {{
    background-color: {BG_SIDEBAR};
    color: {TEXT_PRIMARY};
    border: none;
    outline: none;
}}
QListWidget::item {{
    padding: 8px 10px;
    border-radius: 4px;
    border-left: 2px solid transparent;
}}
QListWidget::item:hover {{
    background-color: #E8DECB;
}}
QListWidget::item:selected {{
    background-color: {ACCENT_TINT};
    color: {TEXT_PRIMARY};
    border-left: 2px solid {ACCENT};
}}
"""
