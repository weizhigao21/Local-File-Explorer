"""网页端视频转码：把浏览器不认的编码（HEVC/VC-1/RMVB…）重编码成 H.264

**为什么必须有这一层**：转封装（`-c:v copy`）只换容器不换编码。把 HEVC 原样
塞进 fMP4 后，手机 Chrome / Android WebView 依然解不了 —— 用户看到「需转封装」
点了，画面照样是黑的。要真能播只有一条路：把画面重编码成 H.264（浏览器最大公约数）。

设计取舍：

1. **硬件优先**：`h264_nvenc` → `h264_qsv` → `h264_amf` → `libx264`。
   RTX 2080 Ti 上 1080p 能到 3~5 倍实时，边转边看才等得起；纯 CPU 只作兜底。
2. **边转边播**：ffmpeg 输出 fragmented mp4 直接喂 `<video>`，首次不用等整集
   转完（24 分钟一集纯 CPU 要转十几分钟，没人愿意等）。
3. **结果落盘**：正常读完的转码结果改名成 `<id>-<mtime>-<size>.mp4`，下次直接
   当普通文件发 —— 有 Content-Length、支持 Range，能拖动能续播。
4. **拖动靠重连**：fMP4 流没有总时长信息，seek 由前端重新请求 `?start=<秒>`，
   服务端用前置 `-ss` 从那个点重新转。误差在一个 GOP 内（通常 1~2 秒），
   比「完全不能拖」好得多。

本模块纯 Python、不碰 Qt，可被单测直接调用。
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from contextlib import suppress

from resource_manager import config

# 按优先级排列：先硬件，后 CPU
ENCODERS = ("h264_nvenc", "h264_qsv", "h264_amf", "libx264")

# 进程内缓存探测结果（探测一次要起 ffmpeg，别每播一集都测）
_ENCODER_CACHE: dict[str, str] = {}


def _creationflags():
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def _encoder_works(ffmpeg, encoder):
    """真起一次编码：光看 `-encoders` 列表不够，驱动没装/显卡不支持照样会失败"""
    cmd = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=d=0.2:s=320x240:r=10",
        "-c:v", encoder, "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(  # noqa: S603 - 参数为内部拼装，无 shell
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=30,
            creationflags=_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def pick_encoder(ffmpeg):
    """挑一个当前机器真能用的 H.264 编码器，都不行就返回 None"""
    if not ffmpeg:
        return None
    if ffmpeg in _ENCODER_CACHE:
        return _ENCODER_CACHE[ffmpeg] or None
    chosen = ""
    for encoder in ENCODERS:
        if _encoder_works(ffmpeg, encoder):
            chosen = encoder
            break
    _ENCODER_CACHE[ffmpeg] = chosen
    return chosen or None


def reset_encoder_cache():
    """测试用：清掉探测结果"""
    _ENCODER_CACHE.clear()


def cache_path(video, cache_dir=None):
    """转码结果路径；源文件改过（mtime/size 变了）就自动换新名字，不会串片"""
    cache_dir = cache_dir or config.TRANSCODE_DIR
    try:
        stat = os.stat(video["path"])
    except (OSError, KeyError, TypeError):
        return ""
    key = f"{video.get('id', 0)}-{int(stat.st_mtime)}-{stat.st_size}"
    return os.path.join(cache_dir, key + ".mp4")


def _video_args(encoder):
    """各编码器的质量参数：目标是「看着不比原片差，但别占太多码率」

    统一转 8bit yuv420p —— 10bit HEVC 原样转出来，老浏览器照样不认。
    """
    if encoder == "h264_nvenc":
        # -cq 是 NVENC 的恒质量模式，配合 -b:v 0 让它自己决定码率
        return ["-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0",
                "-profile:v", "high", "-pix_fmt", "yuv420p"]
    if encoder == "h264_qsv":
        return ["-preset", "veryfast", "-global_quality", "23",
                "-profile:v", "high", "-pix_fmt", "nv12"]
    if encoder == "h264_amf":
        return ["-quality", "balanced", "-rc", "cqp", "-qp_i", "23", "-qp_p", "23",
                "-profile:v", "high", "-pix_fmt", "yuv420p"]
    return ["-preset", "veryfast", "-crf", "23",
            "-profile:v", "high", "-pix_fmt", "yuv420p"]


def build_cmd(ffmpeg, src, mode, start=0.0, encoder=None):
    """拼 ffmpeg 命令：输出 fMP4 到 stdout

    `start > 0` 时 `-ss` 放在 `-i` **之前**（快速 seek，靠关键帧对齐）——
    放在后面会先解码再丢弃，转码大文件时慢得离谱。输出时间戳默认从 0 开始，
    所以前端只需要把展示时间加上 start 就行。
    """
    cmd = [ffmpeg, "-nostdin", "-loglevel", "error"]
    if start and start > 0:
        cmd += ["-ss", f"{float(start):.3f}"]
    cmd += ["-i", src, "-map", "0:v:0", "-map", "0:a:0?"]

    if mode == "remux":
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-c:v", encoder or "libx264"] + _video_args(encoder or "libx264")

    # 音频统一 AAC：DTS/AC3/FLAC 塞进 mp4 后浏览器大概率没声音
    cmd += ["-c:a", "aac", "-b:a", "160k", "-ac", "2",
            # 字幕/数据流不能进 mp4：mkv 里的 ASS 字幕会让封装直接报错
            "-sn", "-dn",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "pipe:1"]
    return cmd


def sweep(cache_dir=None, max_bytes=None):
    """缓存超限就按最后访问时间淘汰最旧的，返回删掉的文件数

    正在转的 `.part` 不碰（它还没写完，删了会把当前播放搞崩）。
    """
    cache_dir = cache_dir or config.TRANSCODE_DIR
    max_bytes = config.TRANSCODE_MAX_BYTES if max_bytes is None else max_bytes
    if not cache_dir or not os.path.isdir(cache_dir) or max_bytes <= 0:
        return 0

    entries = []
    total = 0
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return 0

    # 先扫孤儿 .part：程序被强杀（断电/任务管理器）时 shutdown 来不及跑，
    # 会留下几百 MB 的临时文件。超过一天且没有任务在写就删掉。
    active_parts = set()
    with _JOBS_LOCK:
        active_parts = {job.part_path for job in _JOBS.values() if job.running}
    now = time.time()
    for name in names:
        if not name.endswith(".part"):
            continue
        full = os.path.join(cache_dir, name)
        if full in active_parts:
            continue
        try:
            if now - os.stat(full).st_mtime > 24 * 3600:
                os.remove(full)
        except OSError:
            continue

    for name in names:
        if not name.endswith(".mp4"):
            continue
        full = os.path.join(cache_dir, name)
        try:
            stat = os.stat(full)
        except OSError:
            continue
        entries.append((stat.st_atime, stat.st_size, full))
        total += stat.st_size

    if total <= max_bytes:
        return 0

    removed = 0
    for _atime, size, full in sorted(entries):
        if total <= max_bytes:
            break
        try:
            os.remove(full)
        except OSError:
            continue
        total -= size
        removed += 1
    return removed


# ==================== 后台整集转码任务 ====================
#
# 为什么要有这一层：转码如果跟着某一次 HTTP 连接走，会有两个致命问题——
# 1. 用户从续播点接着看时 `-ss` 只转后半段，落盘的片段拼不成完整剧集；
# 2. 用户切集/退出时连接一断，ffmpeg 就被 kill，缓存永远建不起来。
# 现在转码是一次独立的后台任务（写 `.part` 文件），HTTP 请求只是「跟着读」：
# 谁断开都不影响转码，转完就 rename 成缓存，下次秒开、可拖、可续播。

_JOB_SLOTS = threading.Semaphore(1)
_JOBS: dict[str, TranscodeJob] = {}
_JOBS_LOCK = threading.Lock()


class TranscodeJob:
    """一次整集转码：后台线程跑 ffmpeg，结果落盘成正式缓存"""

    def __init__(self, key, cmd, part_path, final_path):
        self.key = key
        self.cmd = cmd
        self.part_path = part_path
        self.final_path = final_path
        self.done = threading.Event()
        self.ok = False
        self.error = ""
        self.started = time.monotonic()
        self._cancelled = False
        self._proc = None
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"transcode-{key}")

    def start(self):
        self._thread.start()
        return self

    @property
    def running(self):
        return not self.done.is_set()

    def cancel(self):
        """关服/退出时用：杀掉 ffmpeg，`.part` 由 _run 的 finally 清掉"""
        self._cancelled = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            with suppress(OSError):
                proc.kill()

    def _run(self):
        try:
            with open(self.part_path, "wb") as out:
                proc = subprocess.Popen(  # noqa: S603 - 参数为内部拼装，无 shell
                    self.cmd,
                    stdout=out,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    creationflags=_creationflags(),
                )
                self._proc = proc
                code = proc.wait()
            if self._cancelled:
                with suppress(OSError):
                    os.remove(self.part_path)
            elif code == 0 and os.path.getsize(self.part_path) > 1024:
                os.replace(self.part_path, self.final_path)
                self.ok = True
                sweep()
            else:
                self.error = f"ffmpeg 退出码 {code}"
                with suppress(OSError):
                    os.remove(self.part_path)
        except (OSError, subprocess.SubprocessError) as e:
            self.error = str(e)
            with suppress(OSError):
                os.remove(self.part_path)
        finally:
            with _JOBS_LOCK:
                if _JOBS.get(self.key) is self:
                    _JOBS.pop(self.key, None)
            _JOB_SLOTS.release()
            self.done.set()


def ensure_job(video, ffmpeg, encoder, cache_dir=None):
    """确保这一集的整集转码在后台跑着；已经有缓存或已经在转就返回现状

    返回 (job 或 None, 是否已有现成缓存)。
    """
    final = cache_path(video, cache_dir)
    if not final:
        return None, False
    if os.path.isfile(final):
        return None, True

    with _JOBS_LOCK:
        existing = _JOBS.get(final)
        if existing and existing.running:
            return existing, False
        if not _JOB_SLOTS.acquire(blocking=False):
            # GPU 已经有一路在转：不再叠加，本次只走即时管道
            return None, False
        cmd = build_cmd(ffmpeg, video["path"], "transcode", 0, encoder)
        with suppress(OSError):
            os.makedirs(os.path.dirname(final), exist_ok=True)
        job = TranscodeJob(final, cmd, final + ".part", final)
        _JOBS[final] = job

    job.start()
    return job, False


def active_jobs():
    """测试/诊断用：当前在跑的转码任务"""
    with _JOBS_LOCK:
        return list(_JOBS.values())


def reset_jobs():
    """测试用：清掉任务表（不杀进程）"""
    with _JOBS_LOCK:
        _JOBS.clear()


def shutdown():
    """关服/退出：杀掉所有在跑的转码，别让 ffmpeg 变成孤儿进程

    正在转的那一集下次会重新转——这比留下一个几百 MB 的 `.part` 强。
    """
    with _JOBS_LOCK:
        jobs = list(_JOBS.values())
    for job in jobs:
        job.cancel()
    for job in jobs:
        job.done.wait(timeout=5)
