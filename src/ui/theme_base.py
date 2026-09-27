"""
UI 设计令牌（调色板）—— 单一来源

背景：`launcher.py` / `photo_theme.py` / `audio_theme.py` 曾各自复制了一份同名
配色常量。取值当前虽然一致，但已经出现漂移迹象（两处 BTN_QSS 格式分叉、
launcher 独有 CARD_BORDER 而 audio_theme 独有 PLAYER_BG），属于"改一处、漏两处"
的隐患。这里把跨模块共用的颜色收敛为唯一定义，各主题模块从本模块导入后继续
对外导出（保持 `from ui.photo_theme import ACCENT` 这类既有写法不变）。

只放「颜色/尺寸令牌」，不放 QSS 文本 —— 各模块的样式表仍留在各自主题文件里，
避免把不同模块的排版意图混在一起。
"""

# ---- 强调色（3D 黏土风 / 轻拟物新拟态） ----
ACCENT = "#42B4C2"                        # 强调色（青蓝）
ACCENT_HOVER = "#48B8BC"                  # 强调色悬停
ACCENT_TINT = "rgba(66, 180, 194, 0.15)"  # 半透明 accent 背景

# ---- 背景 ----
BG_MAIN = "#F5F0E8"
BG_SIDEBAR = "#FDF9F2"
CARD_BG = "#FFFFFF"
CARD_HOVER = "#FFFDF8"
INPUT_BG = "#EDE6DA"
PLAYER_BG = "#EDE6DA"

# ---- 文本 ----
TEXT_PRIMARY = "#555555"
TEXT_SECONDARY = "#777777"
TEXT_MUTED = "#999999"
TEXT_DIM = "#BBBBBB"

# ---- 描边 ----
BORDER_COLOR = "#D5CDC0"
CARD_BORDER = "#E8E0D5"
CARD_BORDER_HOVER = "#D5CDC0"
