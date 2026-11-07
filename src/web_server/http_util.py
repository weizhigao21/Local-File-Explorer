"""HTTP 小工具（纯 Python，禁止 Qt）

这里只放**纯函数**，因为它们是「手机能不能正常播」的关键：
- `parse_range` 错一位，iPhone 上的视频就表现为「能播不能拖」或直接黑屏；
- `static_file_path` 松一格，就是一个目录穿越漏洞；
- `guess_content_type` 错了，`<audio>` 会静默不播。

不引第三方 Web 框架：标准库 `http.server` 足够，且打包体积不变。
"""
from __future__ import annotations

import mimetypes
import os

# 媒体分块读取大小：256KB 在千兆局域网 + 手机解码之间比较平衡
CHUNK_SIZE = 256 * 1024

# 显式映射：Windows 注册表里的 MIME 常常缺失或被别的软件改乱，
# 不能只依赖 mimetypes.guess_type。
_EXTRA_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".ts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".rmvb": "video/vnd.rn-realvideo",
    ".rm": "video/vnd.rn-realvideo",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".3gp": "video/3gpp",
    ".vob": "video/dvd",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wma": "audio/x-ms-wma",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
    ".srt": "text/plain; charset=utf-8",
    ".vtt": "text/vtt; charset=utf-8",
}

# 浏览器 <video> 直接认的容器（其余靠服务端转封装或让用户在电脑上看）
BROWSER_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".webm", ".mov", ".ogv"}

# 浏览器普遍能解的编码。HEVC 只有 Safari 认，所以不算在内。
BROWSER_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1", "theora"}

RANGE_INVALID = "invalid"


class RangeNotSatisfiable(ValueError):
    """Range 起点越过文件尾：按 RFC 应回 416 + Content-Range: bytes */size"""


def parse_range(value, size):
    """解析 `Range: bytes=...`，返回 (start, end) 闭区间

    返回 None 表示「没有 Range 或格式不合法」→ 调用方回 200 整体发送；
    起点越界抛 RangeNotSatisfiable → 调用方回 416。
    多段 Range 只取第一段（浏览器播放器不会发多段，取第一段是最稳的降级）。
    """
    if not value:
        return None
    text = str(value).strip()
    if not text.lower().startswith("bytes="):
        return None
    spec = text.split("=", 1)[1].split(",")[0].strip()
    if "-" not in spec:
        return None

    start_text, end_text = spec.split("-", 1)
    start_text, end_text = start_text.strip(), end_text.strip()
    try:
        size = int(size)
    except (TypeError, ValueError):
        return None
    if size <= 0:
        raise RangeNotSatisfiable(f"空文件不支持 Range: {value!r}")

    if not start_text:
        # 后缀式 `bytes=-500`：最后 500 字节
        # 注意 `bytes=--5`（两个横线）是格式错误，要当没传；`bytes=-0` 只是取不到
        # 字节，按 RFC 属于 unsatisfiable，回 416。
        if not end_text.isdigit():
            return None
        length = int(end_text)
        if length == 0:
            raise RangeNotSatisfiable(f"后缀长度必须为正: {value!r}")
        start = max(0, size - length)
        return (start, size - 1)

    try:
        start = int(start_text)
    except (TypeError, ValueError):
        return None
    if end_text:
        try:
            end = int(end_text)
        except (TypeError, ValueError):
            return None
    else:
        end = size - 1

    if start < 0 or start >= size or start > end:
        raise RangeNotSatisfiable(f"Range 越界: {value!r} (size={size})")
    return (start, min(end, size - 1))


def content_range_header(start, end, size):
    return f"bytes {start}-{end}/{size}"


def etag_for(path):
    """按 (mtime, size) 生成弱校验 ETag，够用且不用读文件内容"""
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    return f'"{int(stat.st_mtime)}-{stat.st_size}"'


def guess_content_type(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in _EXTRA_TYPES:
        return _EXTRA_TYPES[ext]
    guessed, _encoding = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def static_file_path(root, rel):
    """把 URL 相对路径解析成 root 下的真实文件；越界/不存在返回 None

    三道防线：去 NUL、normpath 归一化、commonpath 归属校验。
    只靠字符串里查 `..` 是不够的（`%2e%2e/`、反斜杠写法、UNC 路径都能绕过）。
    """
    rel = (rel or "").replace("\\", "/").strip("/")
    if "\x00" in rel:
        return None
    rel = rel or "index.html"

    root_abs = os.path.abspath(root)
    candidate = os.path.abspath(os.path.join(root_abs, rel))
    try:
        if os.path.commonpath([root_abs, candidate]) != root_abs:
            return None
    except ValueError:
        # 跨盘符（Windows 下 os.path.commonpath 会抛 ValueError）
        return None
    if not os.path.isfile(candidate):
        return None
    return candidate


def _vtt_timestamp(ms):
    total = max(0, int(ms))
    hours, rest = divmod(total, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def cues_to_vtt(cues):
    """把 SubtitleTrack 的 cue 列表转成 WebVTT 文本

    音频元素挂不了 <track>（那是 video 的），但转成 VTT 后前端也能直接
    用 `<track>` 之外的解析器；这里先统一成标准格式，前端只在需要时读。
    """
    lines = ["WEBVTT", ""]
    for cue in cues or []:
        if isinstance(cue, dict):
            start_ms = cue.get("start_ms", 0)
            end_ms = cue.get("end_ms", 0)
            text = cue.get("text", "")
        else:
            start_ms = getattr(cue, "start_ms", 0)
            end_ms = getattr(cue, "end_ms", 0)
            text = getattr(cue, "text", "")

        start_ms = int(start_ms or 0)
        end_ms = int(end_ms or 0)
        # end_ms 为 0 表示「持续到下一句」：给 3 秒兜底，避免生成 0 时长 cue
        if end_ms <= start_ms:
            end_ms = start_ms + 3000
        body = str(text or "").replace("\r\n", "\n").strip()
        if not body:
            continue
        lines.append(f"{_vtt_timestamp(start_ms)} --> {_vtt_timestamp(end_ms)}")
        lines.append(body)
        lines.append("")
    return "\n".join(lines)


def browser_playable(ext, codec=""):
    """容器与编码都在浏览器支持范围内时才返回 True

    编码未知（ffprobe 没装/没探到）时按容器判断，让浏览器自己试——
    宁可试一次失败，也不要因为探测不到就直接把可播的文件拦下来。
    """
    ext = (ext or "").lower()
    if ext not in BROWSER_VIDEO_EXTENSIONS:
        return False
    codec = (codec or "").lower()
    if not codec:
        return True
    return codec in BROWSER_VIDEO_CODECS


def parse_byte_size(text):
    """解析 `Content-Length` 之类的小整数，非法/负数返回 0"""
    try:
        value = int(text)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0
