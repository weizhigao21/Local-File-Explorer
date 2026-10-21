"""
播放器窗口「陶色 · 温润卡片」主题（UI 方案 C）

来源：artifacts/player-ui-redesign/player-ui-3versions.html 的版本 C
—— 保留暖奶油底色，但把散落的控件收进**有边界的圆角卡片**里，
强调色从青蓝换成**赤陶**，消除"裸滑杆漂在背景上"的随意感。

与 theme_base 的米色主界面同属暖色系但强调色不同：主界面 / 相册 / 启动器
仍用 theme_base（青蓝强调），播放器窗口用这套（赤陶强调），令牌带 `P_` 前缀，
两套不得互相引用。

注意：QSS 的 background-color 不支持 rgba()（Qt 会静默丢弃整条声明），
所有半透明效果都用"叠在底色上的不透明等价色"表达。
"""
from __future__ import annotations

# ── 配色令牌（取自网页版 C 的 CSS）────────────────────────
P_BG = "#F4EFE7"            # 窗体底：暖奶油
P_HEADER = "#FBF7F0"        # 顶栏 / 卡片面
P_CARD = "#FBF7F0"
P_CARD_EDGE = "#E8DECB"
P_EDGE_STRONG = "#E0D5C2"
P_TEXT = "#3D3830"          # 正文（深咖）
P_TEXT_DIM = "#A89880"      # 次要信息
P_TEXT_FAINT = "#C4B394"    # 弱化信息（专辑行等）
P_TEXT_BTN = "#7A6E5C"      # 按钮文字
P_ACCENT = "#B85C3E"        # 赤陶（强调）
P_ACCENT_LIGHT = "#D4886A"  # 赤陶渐变亮端
P_ACCENT_RING = "#F0DDD3"   # accent 10% over card 的不透明等价
P_TAG_BG = "#F4EFE7"        # 标签底色（卡片上的浅一档）
P_TRACK = "#EDE2CE"         # 滑杆槽
P_WHITE = "#FFFFFF"

# 时间数字：等宽，进度走动时整行不抖
_TIME_FONT = "Consolas, 'Segoe UI', 'Microsoft YaHei UI'"


# ── QSS 片段 ──────────────────────────────────────────────
def window_qss() -> str:
    # 必须带选择器：裸声明会被 Qt 级联给所有子控件，
    # 把纯 QWidget 容器（meta_row / cover_wrap 等）染成底色，
    # 在米白卡片上形成横贯的浅色带（v1.8.4 细节修复）。
    return f"#playerCentral {{ background-color: {P_BG}; }}"


def header_qss() -> str:
    return (
        f"QFrame {{ background-color: {P_HEADER}; border: none;"
        f" border-bottom: 1px solid {P_CARD_EDGE}; }}"
    )


def card_qss(radius: int = 14) -> str:
    # 必须带 QFrame 选择器：裸声明会被子控件继承（标签会被描上边框）
    return (
        f"QFrame {{ background-color: {P_CARD}; border: 1px solid {P_CARD_EDGE};"
        f" border-radius: {radius}px; }}"
    )


def cover_qss() -> str:
    return (
        "QLabel { background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
        " stop:0 #EDE2CE, stop:1 #DFD0B8);"
        f" border: 1px solid {P_CARD_EDGE}; border-radius: 10px;"
        f" color: {P_TEXT_DIM}; font-size: 12px; }}"
    )


def mini_cover_qss() -> str:
    return (
        "QLabel { background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
        " stop:0 #EDE2CE, stop:1 #DFD0B8);"
        f" border: 1px solid {P_CARD_EDGE}; border-radius: 10px; }}"
    )


def pill_btn_qss(radius: int = 12) -> str:
    """胶囊按钮：白面 + 细描边；激活态整颗变赤陶

    radius 必须 ≤ 半高且取半高才是胶囊：Qt 对超过半高的 border-radius 不会
    像 CSS 那样钳成胶囊，而是直接退化成直角（实测 76px 高 38✓/39✗）。
    按钮高 24~25px → 取 12。
    """
    return f"""
QPushButton {{
    background-color: {P_WHITE}; color: {P_TEXT_BTN};
    border: 1px solid {P_EDGE_STRONG}; border-radius: {radius}px;
    padding: 5px 12px; font-size: 11px; font-weight: 500;
}}
QPushButton:hover {{ background-color: #FFFCF7; border-color: #D4C5A8; color: {P_TEXT}; }}
QPushButton:checked {{ background-color: {P_ACCENT}; border-color: {P_ACCENT}; color: {P_WHITE}; }}
QPushButton:pressed {{ background-color: {P_ACCENT_LIGHT}; border-color: {P_ACCENT_LIGHT}; }}
"""


def skip_btn_qss() -> str:
    """上一首/下一首：32px 圆形按钮（设计稿 t-btn，hover 出圆形底色）"""
    return f"""
QPushButton {{
    background: transparent; color: {P_TEXT_DIM}; border: none; padding: 0;
    border-radius: 16px;
}}
QPushButton:hover {{ background-color: {P_TAG_BG}; color: {P_TEXT}; }}
"""


def play_btn_qss() -> str:
    """播放键圆面与柔光由 GlowPlayButton 自绘，QSS 只负责透明"""
    return "QPushButton { background: transparent; border: none; }"


def transport_pill_qss(radius: int = 30) -> str:
    """传输按钮组的胶囊容器（QFrame 选择器，防止边框级联到按钮上）

    radius 用半高才成胶囊；Qt 对超半高 border-radius 退化成直角，
    绝不能写 100px 这类 CSS 式取值。完整态 60px 高 → 30；迷你态 50px 高 → 25。
    """
    return (
        f"QFrame {{ background-color: {P_CARD}; border: 1px solid {P_CARD_EDGE};"
        f" border-radius: {radius}px; }}"
    )


def vol_pill_qss(radius: int = 13) -> str:
    """音量组的胶囊容器（27px 高 → 半高 13）"""
    return transport_pill_qss(radius)


def slider_qss() -> str:
    """进度 / 音量：暖色槽 + 赤陶渐变已播段 + 白底赤陶描边圆柄"""
    return f"""
QSlider::groove:horizontal {{ background: {P_TRACK}; height: 5px; border-radius: 3px; }}
QSlider::sub-page:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {P_ACCENT_LIGHT}, stop:1 {P_ACCENT});
    height: 5px; border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: {P_WHITE}; border: 2.5px solid {P_ACCENT};
    width: 13px; height: 13px; margin: -5px 0; border-radius: 7px;
}}
"""


def mini_slider_qss() -> str:
    """迷你态进度：4px 圆角细条、无可见手柄（设计稿 m-seek），点按仍可跳转"""
    return f"""
QSlider::groove:horizontal {{ background: {P_TRACK}; height: 4px; border-radius: 2px; }}
QSlider::sub-page:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {P_ACCENT_LIGHT}, stop:1 {P_ACCENT});
    height: 4px; border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: transparent; border: none;
    width: 12px; height: 12px; margin: -4px 0;
}}
"""


def speed_chip_qss(radius: int = 12) -> str:
    """倍速胶囊：卡片底 + 细描边（设计稿 speed-chip，24px 高 → 半高 12）"""
    return f"""
QPushButton {{
    background-color: {P_CARD}; color: {P_TEXT_BTN};
    border: 1px solid {P_CARD_EDGE}; border-radius: {radius}px;
    padding: 5px 11px; font-size: 11px; font-weight: 500;
}}
QPushButton:hover {{ color: {P_TEXT}; border-color: #D4C5A8; }}
"""


def util_btn_qss() -> str:
    """功能图标按钮（循环 / 打开）：32px 圆角方，hover 出卡片底色"""
    return f"""
QPushButton {{
    background: transparent; color: {P_TEXT_DIM};
    border: none; border-radius: 8px;
}}
QPushButton:hover {{ background-color: {P_CARD}; color: {P_TEXT}; }}
QPushButton:checked {{ background-color: {P_ACCENT_RING}; color: {P_ACCENT}; }}
"""


def time_label_qss(color: str = P_TEXT_DIM) -> str:
    return (
        f"color: {color}; font-size: 11px; font-family: {_TIME_FONT};"
        " background: transparent; border: none;"
    )


def tag_qss(radius: int = 8) -> str:
    """格式/队列数这类元信息标签：浅底胶囊（高 16~17px → 半高 8）"""
    return (
        f"QLabel {{ background-color: {P_TAG_BG}; color: {P_TEXT_DIM};"
        f" border: none; border-radius: {radius}px; padding: 3px 9px; font-size: 10px; }}"
    )


def queue_panel_qss() -> str:
    return card_qss()


def queue_list_qss() -> str:
    # ::item 绝不能带 padding：Qt 会把 item padding 从 item 矩形里扣掉，再把
    # setItemWidget 的行控件塞进剩余的内容区（34px − 7×2 = 20px），行内 margin
    # 再吃掉 12px，标签只剩 8px——12px 字体墨盒 14px，字形头脚被裁掉（v1.8.6）。
    # 行间距/内边距由行控件自己的 contentsMargins 负责。
    return f"""
QListWidget {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{ border-radius: 8px; color: {P_TEXT_DIM}; }}
QListWidget::item:hover {{ background-color: {P_TAG_BG}; color: {P_TEXT}; }}
QListWidget::item:selected {{ background-color: {P_WHITE}; color: {P_TEXT};
    border: 1px solid {P_CARD_EDGE}; }}
"""


def subtitle_theme() -> dict:
    """传给 SubtitleView(theme=...) 的陶色配色"""
    return {
        "card_bg": P_CARD,
        "card_edge": P_CARD_EDGE,
        "text": P_TEXT,
        "dim": P_TEXT_DIM,
        "muted": P_TEXT_FAINT,
        "accent": P_ACCENT,
        "accent_soft": (184, 92, 62, 28),
        "btn_edge": P_EDGE_STRONG,
        "leaf_bg": P_TAG_BG,  # sub-head 的18px 圆标底（设计稿 #F4EFE7）
    }


__all__ = [
    "P_BG", "P_HEADER", "P_CARD", "P_CARD_EDGE", "P_EDGE_STRONG",
    "P_TEXT", "P_TEXT_DIM", "P_TEXT_FAINT", "P_TEXT_BTN",
    "P_ACCENT", "P_ACCENT_LIGHT", "P_ACCENT_RING", "P_TAG_BG",
    "P_TRACK", "P_WHITE",
    "window_qss", "header_qss", "card_qss", "cover_qss", "mini_cover_qss",
    "pill_btn_qss", "skip_btn_qss", "play_btn_qss",
    "mini_slider_qss", "speed_chip_qss", "util_btn_qss",
    "transport_pill_qss", "vol_pill_qss", "slider_qss", "time_label_qss",
    "tag_qss", "queue_panel_qss", "queue_list_qss", "subtitle_theme",
]
