"""
字幕解析（纯 Python，不依赖 Qt）

用途：音频播放时显示同目录字幕/歌词。支持：
    .srt     SubRip（最常见的 Whisper / 字幕组产物）
    .vtt     WebVTT
    .lrc     歌词（含一行多时间标签的增强 LRC）
    .ass/.ssa ASS/SSA（只取 Events 里的 Dialogue 文本，丢弃样式）

设计边界：
    - 只做「时间轴 + 文本」的解析与定位，不做任何界面；
    - 编码自动嗅探：中文/日文字幕常见 GB18030、Shift-JIS，纯 UTF-8 假设一定会乱码；
    - 定位用二分查找，播放进度每 100ms 推进时不会退化成线性扫描；
    - 不强行拆分双语：一行里多句文本原样保留换行，由界面决定主/副行排版。
"""
from __future__ import annotations

import bisect
import os
import re
from dataclasses import dataclass

SUBTITLE_EXTENSIONS = (".srt", ".vtt", ".lrc", ".ass", ".ssa")

_FORMAT_LABELS = {
    ".srt": "SRT",
    ".vtt": "WebVTT",
    ".lrc": "LRC",
    ".ass": "ASS",
    ".ssa": "SSA",
}


@dataclass(frozen=True)
class Cue:
    """一条字幕：时间单位毫秒，end_ms 为 0 表示「持续到下一句」"""

    start_ms: int
    end_ms: int
    text: str


class SubtitleTrack:
    """解析后的字幕轨道：cue 列表 + 二分索引"""

    def __init__(self, path="", cues=None, encoding="utf-8", fmt=""):
        self.path = path
        cues = list(cues or [])
        # 二分定位要求 start_ms 有序。各解析器都会自己排序，但 SubtitleTrack 也
        # 可能被直接构造（测试、外部数据），乱序会让 index_at 返回错误下标，
        # 表现为高亮卡在某一句不动。这里兜一层，正常已序的列表不会触发拷贝。
        if any(cues[i].start_ms > cues[i + 1].start_ms for i in range(len(cues) - 1)):
            cues.sort(key=lambda cue: cue.start_ms)
        self.cues = cues
        self.encoding = encoding
        self.format = fmt
        self._starts = [cue.start_ms for cue in self.cues]

    def __len__(self):
        return len(self.cues)

    def __bool__(self):
        return bool(self.cues)

    @property
    def name(self):
        return os.path.basename(self.path) if self.path else ""

    def index_at(self, position_ms):
        """返回该时刻应显示的字幕下标；不在任何 cue 内时返回 -1

        二分定位 + 越过结束时间的判定：字幕之间通常有间隙（换气、停顿），
        间隙里不应继续显示上一句，否则滚动会「粘住」。
        """
        if not self.cues:
            return -1
        index = bisect.bisect_right(self._starts, max(0, int(position_ms))) - 1
        if index < 0:
            return -1
        cue = self.cues[index]
        if cue.end_ms and position_ms > cue.end_ms:
            return -1
        return index

    def cue_at(self, position_ms):
        index = self.index_at(position_ms)
        return self.cues[index] if index >= 0 else None


# ==================== 时间戳 ====================

_FULL_TIME = re.compile(r"(\d{1,3}):(\d{1,2}):(\d{1,2})[.,](\d{1,3})")
_SHORT_TIME = re.compile(r"(?:^|\s)(\d{1,2}):(\d{1,2})[.](\d{1,3})")


def parse_timestamp(value):
    """把时间戳转成毫秒；无法解析时返回 None

    支持 00:00:01,500 / 00:00:01.500 / 0:00:01.50 / 00:01.500（VTT 省略小时）。
    """
    if not value:
        return None
    text = str(value).strip()
    match = _FULL_TIME.search(text)
    if match:
        hours, minutes, seconds, frac = match.groups()
        return (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000 \
            + int(frac.ljust(3, "0")[:3])
    match = _SHORT_TIME.search(text)
    if match:
        minutes, seconds, frac = match.groups()
        return (int(minutes) * 60 + int(seconds)) * 1000 + int(frac.ljust(3, "0")[:3])
    return None


# ==================== 文本清洗 ====================

_TAG_RE = re.compile(r"<[^>]{1,32}>")
_ASS_OVERRIDE_RE = re.compile(r"\{[^}]*\}")


def _clean_text(text):
    """去掉样式标记、还原换行与实体，保留每行内容"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    text = _ASS_OVERRIDE_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    text = (text.replace("&nbsp;", " ").replace("&lt;", "<")
            .replace("&gt;", ">").replace("&amp;", "&"))
    lines = [line.strip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


# ==================== 各格式解析 ====================

def _parse_srt(text, is_vtt=False):
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        head = lines[0].strip()
        if head.upper().startswith("WEBVTT") or head.upper().startswith("NOTE"):
            continue
        time_index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if time_index < 0:
            continue
        left, _, right = lines[time_index].partition("-->")
        start = parse_timestamp(left)
        end = parse_timestamp(right)
        if start is None:
            continue
        body = _clean_text("\n".join(lines[time_index + 1:]))
        if not body:
            continue
        cues.append(Cue(start, end if end is not None else 0, body))
    cues.sort(key=lambda cue: cue.start_ms)
    return cues


_LRC_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_DEFAULT_LRC_DURATION = 5000


def _parse_lrc(text):
    entries = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        tags = list(_LRC_TAG.finditer(line))
        if not tags:
            continue
        content = _clean_text(line[tags[-1].end():])
        if not content:
            continue
        for tag in tags:
            minutes, seconds, frac = tag.groups()
            start = (int(minutes) * 60 + int(seconds)) * 1000
            if frac:
                start += int(frac.ljust(3, "0")[:3])
            entries.append((start, content))
    entries.sort(key=lambda item: item[0])
    cues = []
    for index, (start, content) in enumerate(entries):
        end = entries[index + 1][0] if index + 1 < len(entries) else start + _DEFAULT_LRC_DURATION
        cues.append(Cue(start, end, content))
    return cues


def _parse_ass(text):
    cues = []
    fields = None
    in_events = False
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("["):
            in_events = line.lower() == "[events]"
            continue
        if not in_events:
            continue
        lower = line.lower()
        if lower.startswith("format:"):
            fields = [part.strip().lower() for part in line.split(":", 1)[1].split(",")]
            continue
        if not lower.startswith("dialogue:") or not fields:
            continue
        payload = line.split(":", 1)[1].lstrip()
        parts = payload.split(",", len(fields) - 1)  # 文本字段本身可能含逗号
        if len(parts) < len(fields):
            continue
        data = dict(zip(fields, parts, strict=False))
        start = parse_timestamp(data.get("start", ""))
        end = parse_timestamp(data.get("end", ""))
        if start is None:
            continue
        body = _clean_text(data.get("text", ""))
        if not body:
            continue
        cues.append(Cue(start, end if end is not None else 0, body))
    cues.sort(key=lambda cue: cue.start_ms)
    return cues


def _parse_plain(text, duration_ms=0):
    """无时间轴的纯文本：按行均分总时长（仅在已知音频时长时使用）"""
    lines = [_clean_text(line) for line in text.split("\n")]
    lines = [line for line in lines if line]
    if not lines or duration_ms <= 0:
        return []
    step = max(1, int(duration_ms / len(lines)))
    return [Cue(index * step, (index + 1) * step, line) for index, line in enumerate(lines)]


def detect_format(text, path=""):
    """按扩展名 + 内容判断格式，返回 srt/vtt/lrc/ass/txt"""
    ext = os.path.splitext(path or "")[1].lower()
    if ext == ".vtt":
        return "vtt"
    if ext == ".lrc":
        return "lrc"
    if ext in (".ass", ".ssa"):
        return "ass"
    if ext == ".srt":
        return "srt"
    head = text.lstrip("\ufeff")[:4096]
    if "[events]" in head.lower() and "dialogue:" in head.lower():
        return "ass"
    if re.search(r"(?m)^\[\d{1,3}:\d{1,2}", head):
        return "lrc"
    if head.lstrip().upper().startswith("WEBVTT"):
        return "vtt"
    if "-->" in head:
        return "srt"
    return "txt"


def parse_text(text, path="", duration_ms=0):
    """解析字幕文本，返回 Cue 列表"""
    fmt = detect_format(text, path)
    if fmt == "ass":
        return _parse_ass(text)
    if fmt == "lrc":
        return _parse_lrc(text)
    if fmt in ("srt", "vtt"):
        return _parse_srt(text, is_vtt=(fmt == "vtt"))
    return _parse_plain(text, duration_ms)


# ==================== 编码嗅探 ====================

def read_text_file(path):
    """读取字幕文件，返回 (文本, 编码)

    顺序：BOM → UTF-8 严格解码 → GB18030 → Shift-JIS → Latin-1 兜底。
    GB18030 放在 Shift-JIS 之前：中文 ASMR 素材占多数，且它覆盖 GBK/GB2312。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return "", "utf-8"
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace"), "utf-16"
    for encoding in ("utf-8", "gb18030", "shift_jis"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1"


def load_subtitle(path, duration_ms=0):
    """读取并解析字幕文件；失败返回空 SubtitleTrack（不抛异常）"""
    if not path or not os.path.isfile(path):
        return SubtitleTrack(path or "")
    text, encoding = read_text_file(path)
    fmt = detect_format(text, path)
    cues = parse_text(text, path, duration_ms)
    label = _FORMAT_LABELS.get(os.path.splitext(path)[1].lower(), fmt.upper())
    return SubtitleTrack(path, cues, encoding=encoding, fmt=label)


# ==================== 同目录配对 ====================

_LANG_PREFERENCE = ("zh", "chs", "sc", "cn", "chi", "cn_zh")


def _variant_rank(name):
    """语言变体优先级：中文 > 其他；同优先级按名称排序"""
    lower = name.lower()
    for index, tag in enumerate(_LANG_PREFERENCE):
        if f".{tag}." in lower or f"_{tag}." in lower:
            return index
    return len(_LANG_PREFERENCE)


def find_subtitle(audio_path):
    """在同目录寻找音频对应的字幕

    优先级：
        1) 同名同扩展（03.mp3 → 03.srt）
        2) 同名语言变体（03.zh.srt / 03.chs.srt），中文优先
        3) 目录内唯一的字幕文件（ASMR 常见：一个音频配一个 srt）
    """
    if not audio_path:
        return None
    folder = os.path.dirname(audio_path)
    base = os.path.splitext(os.path.basename(audio_path))[0]
    if not os.path.isdir(folder):
        return None

    for ext in SUBTITLE_EXTENSIONS:
        candidate = os.path.join(folder, base + ext)
        if os.path.isfile(candidate):
            return candidate

    try:
        names = os.listdir(folder)
    except OSError:
        return None
    subtitles = [name for name in names
                 if os.path.splitext(name)[1].lower() in SUBTITLE_EXTENSIONS
                 and os.path.isfile(os.path.join(folder, name))]

    variants = []
    for name in subtitles:
        stem = os.path.splitext(name)[0]
        if stem.lower().startswith(base.lower() + "."):
            variants.append(name)
    if variants:
        variants.sort(key=lambda name: (_variant_rank(name), name.lower()))
        return os.path.join(folder, variants[0])

    if len(subtitles) == 1:
        return os.path.join(folder, subtitles[0])
    return None
