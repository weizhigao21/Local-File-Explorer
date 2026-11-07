"""ffprobe / ffmpeg 封装：读视频元信息 + 抽首帧当封面（纯 Python，无 Qt）

两个能力都是「有就用、没有就降级」：
- 拿不到 ffprobe → 时长/分辨率留 0，界面显示 `--:--`，播放器读到真实时长后回填；
- 拿不到 ffmpeg → 不生成封面，卡片显示占位块，不影响浏览与播放。

Windows 上必须带 CREATE_NO_WINDOW，否则每次探测都会闪一个黑框
（扫描几百个视频时尤其明显）。
"""
import json
import os
import shutil
import subprocess
import sys
from contextlib import suppress

# 单次探测超时（秒）：损坏文件或网络盘挂起时不能把扫描线程卡死
PROBE_TIMEOUT = 15
THUMB_TIMEOUT = 30

# 抽帧位置：默认第 5 秒（片头黑场/厂标太多时取略后面一些）
DEFAULT_THUMB_AT_MS = 5000

_create_no_window = 0
if os.name == "nt":
    _create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_cache = {}


def _candidates(name):
    """按优先级列出可执行文件候选路径"""
    exe = f"{name}.exe" if os.name == "nt" else name
    paths = []

    env_value = os.environ.get(f"{name.upper()}_PATH", "").strip()
    if env_value:
        paths.append(env_value if env_value.lower().endswith(".exe") else
                     os.path.join(env_value, exe))

    found = shutil.which(name)
    if found:
        paths.append(found)

    # 项目部署机常见位置（C:\DIY\ffmpeg\bin）+ 打包后 exe 同级
    roots = [r"C:\DIY\ffmpeg\bin", r"C:\ffmpeg\bin"]
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        roots = [os.path.join(base, "ffmpeg", "bin"), os.path.join(base, "bin"), *roots]
    paths.extend(os.path.join(root, exe) for root in roots)
    return paths


def find_tool(name):
    """定位 ffmpeg / ffprobe，找不到返回 None（结果按进程缓存）"""
    key = f"tool:{name}"
    if key in _cache:
        return _cache[key]
    resolved = None
    for path in _candidates(name):
        if path and os.path.isfile(path):
            resolved = path
            break
    _cache[key] = resolved
    return resolved


def has_ffprobe():
    return find_tool("ffprobe") is not None


def has_ffmpeg():
    return find_tool("ffmpeg") is not None


def _run(cmd, timeout):
    try:
        proc = subprocess.run(  # noqa: S603 - 参数为内部拼装的绝对路径，无 shell
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=_create_no_window,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc


def probe(path):
    """读视频元信息，返回 {"duration_ms", "width", "height", "codec"}

    任一步失败都返回空字典——调用方按「未知」处理即可，不要中断扫描。
    """
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        return {}
    proc = _run(
        [
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name,duration",
            "-show_entries", "format=duration",
            "-of", "json", path,
        ],
        PROBE_TIMEOUT,
    )
    if proc is None or proc.returncode != 0 or not proc.stdout:
        return {}
    try:
        data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return {}

    streams = data.get("streams") or []
    stream = streams[0] if streams else {}
    fmt = data.get("format") or {}

    duration = _to_float(fmt.get("duration")) or _to_float(stream.get("duration"))
    return {
        "duration_ms": int(duration * 1000) if duration else 0,
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "codec": str(stream.get("codec_name") or ""),
    }


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def make_thumbnail(video_path, out_path, at_ms=DEFAULT_THUMB_AT_MS):
    """抽一帧存成 JPEG 封面，成功返回 out_path，失败返回 None

    已存在则直接复用（重新扫描时封面不会重复生成）；写临时文件再改名，
    避免程序被强杀留下半张图，下次永远读到坏封面。
    """
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return None

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp_path = f"{out_path}.tmp.jpg"
    seconds = max(0, int(at_ms)) / 1000.0
    proc = _run(
        [
            ffmpeg, "-nostdin", "-y",
            "-ss", f"{seconds:.3f}", "-i", video_path,
            "-frames:v", "1", "-vf", "scale=480:-2",
            "-q:v", "4", tmp_path,
        ],
        THUMB_TIMEOUT,
    )
    if proc is None or proc.returncode != 0 or not os.path.exists(tmp_path):
        _safe_remove(tmp_path)
        # 第 5 秒可能落在片头静止/黑场之外，退一步用第 1 秒再试一次
        if at_ms != 1000:
            return make_thumbnail(video_path, out_path, at_ms=1000)
        return None

    try:
        os.replace(tmp_path, out_path)
    except OSError:
        _safe_remove(tmp_path)
        return None
    return out_path


def _safe_remove(path):
    with suppress(OSError):
        os.remove(path)
