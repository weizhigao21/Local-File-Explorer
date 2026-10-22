"""
UI 设计令牌（调色板）—— 单一来源

背景：`launcher.py` / `photo_theme.py` / `audio_theme.py` 曾各自复制了一份同名
配色常量。取值当前虽然一致，但已经出现漂移迹象（两处 BTN_QSS 格式分叉、
launcher 独有 CARD_BORDER 而 audio_theme 独有 PLAYER_BG），属于"改一处、漏两处"
的隐患。这里把跨模块共用的颜色收敛为唯一定义，各主题模块从本模块导入后继续
对外导出（保持 `from ui.photo_theme import ACCENT` 这类既有写法不变）。

只放「颜色/尺寸令牌」，不放 QSS 文本 —— 各模块的样式表仍留在各自主题文件里，
避免把不同模块的排版意图混在一起。

v1.8.5 起全应用统一为「陶色 · 温润卡片」：强调色由青蓝换赤陶，与播放器窗口
（ui/player_theme.py 的 P_* 令牌，取值同源）一套观感；暖奶油底 + 米白卡片。
"""

# ---- 强调色（陶色 · 温润卡片 / 赤陶） ----
ACCENT = "#B85C3E"                        # 强调色（赤陶，= player P_ACCENT）
ACCENT_HOVER = "#D4886A"                  # 强调色悬停（= player P_ACCENT_LIGHT）
# 半透明 accent 背景的「预混色」。
# 不能写 rgba(184,92,62,0.15)：Qt 的 QSS 会丢弃带 alpha 的 background-color，
# 整条声明失效后回落到系统高亮蓝（#389EC9），选中行会变成刺眼的蓝色。
# 这里按 15% 叠在 BG_MAIN(#F4EFE7) 上算好，得到一个不透明等价色。
ACCENT_TINT = "#EBD9CE"                   # ≈ 15% accent over BG_MAIN

# ---- 背景 ----
BG_MAIN = "#F4EFE7"                       # 暖奶油窗底（= player P_BG）
# 内容页底：比窗底再深一档。歌单详情这种「标题 + 子歌单 + 曲目表」的长页，
# 整片 #F4EFE7 在大屏上偏亮发飘，压深 4% 后与亮面卡片/表头形成层次（v1.8.11）。
BG_PAGE = "#EFE8DA"
BG_SIDEBAR = "#FBF7F0"                    # 侧栏：米白（= player P_CARD）
CARD_BG = "#FBF7F0"                       # 卡片面
CARD_HOVER = "#FFFCF7"
INPUT_BG = "#EDE2CE"                      # 输入/按钮面（暖砂，= player P_TRACK）
PLAYER_BG = "#EDE2CE"

# ---- 文本 ----
TEXT_PRIMARY = "#3D3830"                  # 正文（深咖，= player P_TEXT）
TEXT_SECONDARY = "#7A6E5C"                # 按钮/次要文字（= player P_TEXT_BTN）
TEXT_MUTED = "#A89880"                    # 弱化信息（= player P_TEXT_DIM）
TEXT_DIM = "#C4B394"                      # 最弱信息（= player P_TEXT_FAINT）

# ---- 描边 ----
BORDER_COLOR = "#E0D5C2"                  # = player P_EDGE_STRONG
CARD_BORDER = "#E8DECB"                   # = player P_CARD_EDGE
CARD_BORDER_HOVER = "#D4C5A8"
