"""列表/卡片标题的截断工具。

音频模块与写真模块各自独立加载（不得互相 import），所以这条通用规则单独成
模块，两边都从这里取，避免各写一份、以后只改一处。

为什么必须截断：卡片宽度有限（音频 160px、写真 204px 名区），而 DLsite 那种
「双语-RJ01588371-【W乳首責め×W中出し×W爆乳バニー】…【CV大山チロル】」有
80–90 个字符，word-wrap 后占 6 行，把「N 首 / N 张」整行顶出卡片，甚至压住封面
（用户截图反馈 2026-10-22）。
"""

# 卡片标题最多显示的字符数（CJK 一字算一个），超出部分用省略号
TITLE_MAX_CHARS = 20


def truncate_title(name: str, limit: int = TITLE_MAX_CHARS) -> str:
    """按字符数截断标题，超出补省略号；完整标题由调用方放进 tooltip。"""
    name = (name or "").strip()
    if len(name) <= limit:
        return name
    return name[:limit] + "…"
