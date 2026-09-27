"""
写真模块主题
配色常量、全局滚动条样式、通用按钮样式

配色令牌统一来自 ui.theme_base（见该模块说明），此处继续对外导出，
保持 `from ui.photo_theme import ACCENT` 这类既有写法不变。
"""
from ui.theme_base import ACCENT, ACCENT_HOVER, ACCENT_TINT

__all__ = [
    # 从 theme_base 转出的设计令牌
    "ACCENT", "ACCENT_HOVER", "ACCENT_TINT",
    # 写真模块自有
    "ACCENT_PRESSED", "ACCENT_TINT_LIGHT", "GLOBAL_SCROLLBAR_QSS", "BTN_QSS",
]

# ---- 写真模块特有的强调色档位 ----
ACCENT_PRESSED = "#3DA0AE"         # 强调色按下
ACCENT_TINT_LIGHT = "rgba(66, 180, 194, 0.08)"

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

# 通用按钮样式（hover 时变 accent 色）
BTN_QSS = f"""
QPushButton {{
    background-color: #EDE6DA;
    color: #555555;
    padding: 6px 14px;
    border: 1px solid #D5CDC0;
    border-radius: 4px;
}}
QPushButton:hover {{
    background-color: #F3EFE8;
    border: 1px solid {ACCENT};
    color: #444;
}}
QPushButton:pressed {{
    background-color: {ACCENT_PRESSED};
    border: 1px solid {ACCENT_PRESSED};
    color: #fff;
}}
"""
