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
ACCENT_PRESSED = "#9E4B30"         # 强调色按下
# 同 ACCENT_TINT：QSS 不吃带 alpha 的 background-color，这里给预混好的不透明等价色
ACCENT_TINT_LIGHT = "#EFE3D9"      # ≈ 8% accent over BG_MAIN

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
    background-color: #EDE2CE;
    color: #3D3830;
    padding: 6px 14px;
    border: 1px solid #E0D5C2;
    border-radius: 4px;
}}
QPushButton:hover {{
    background-color: #F3EFE8;
    border: 1px solid {ACCENT};
    color: #3D3830;
}}
QPushButton:pressed {{
    background-color: {ACCENT_PRESSED};
    border: 1px solid {ACCENT_PRESSED};
    color: #fff;
}}
"""
