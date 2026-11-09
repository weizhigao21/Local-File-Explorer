"""RJ 码识别（纯正则，零依赖）

单独成模块的原因：
    `extract_rj_code` 原本住在 `audio_manager/dlsite.py`，而该模块顶层
    import 了 `curl_cffi` + `bs4`（实测约 220ms）。`ui/player_window.py`
    只是想从曲目名里认一个 RJ 码去配封面，却把整个网络抓取栈拖进了
    **每一条**启动路径（启动器、--module photo/video 都会被连带）。

    把纯函数挪到这里，播放器只导本模块即可；`dlsite.py` 仍从本模块
    re-export 同名符号，保持 `dlsite.extract_rj_code` 的既有用法
    （catalog / audio_view / 测试里的 monkeypatch 全部不受影响）。
"""
import re

__all__ = ["RJ_RE", "extract_rj_code"]

# RJ 码后必须不是数字：RJ01155419 后面紧跟汉字/横杠/空格都算结束（`RJ01155419作品名`）
RJ_RE = re.compile(r"(RJ\d{6,10})(?!\d)", re.IGNORECASE)

# 兼容旧名
_RJ_RE = RJ_RE


def extract_rj_code(name: str | None) -> str | None:
    """从歌单名/文件夹名中提取 RJ 码，无则返回 None（统一大写）"""
    if not name:
        return None
    m = RJ_RE.search(str(name))
    return m.group(1).upper() if m else None
