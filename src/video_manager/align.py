"""逐集片头对齐：用音量包络把「用户标的那一集」的片头挪到这一集上（纯 Python，禁 Qt）

为什么需要它：
片头区间是**按作品**记的，但每一集的片头起点并不总在同一个位置——有的集有 20 秒冷开场，
有的集直接进 OP，有的集片头被剪短。如果死用同一条时间线，就会出现「这集跳早了，
开头正片被吃掉了」这种最招人烦的错。

做法（不引 numpy、不依赖 chromaprint）：
1. 把标记那一集的片头区间抽成 8kHz 单声道的**音量包络**（每 250ms 一个 RMS 值）当模板；
2. 目标集只解码「模板起点 ±20 秒」那一段，把模板在上面滑动，算皮尔逊相关；
3. 相关系数够高 → 用找到的位置当这一集的真实起止；
   明显对不上 → 判定这集没有片头（第一集/最终话常这样），标 absent，这一集不跳。

为什么用包络而不是谱指纹：
OP 是每集完全相同的同一段音频，包络的相关系数能到 0.99；而只在已知区间附近搜索
这个约束，把「动画复用 BGM 导致误判」的空间压掉了——全曲库盲搜才会满屏假匹配。
纯 Python 的滑动比对是几十万次乘加，零点几秒的事，真正花时间的是两次 ffmpeg 解码。

搜索窗为什么开到 ±3 分钟（实测依据）：
在真机库上按 30 秒窗口整剧比对过 `大理寺日志 3` 的第 1、2 集——第 1 集 120~240 秒那一整段
和**第 2 集 0~120 秒**的相关系数是 0.99。也就是第 1 集前面垫了两分钟导言，片头整体后移 120 秒。
窗口只开 ±20 秒的话这种集直接对不上，只能回落到模板，然后准确地把正片前两分钟跳掉。
同一批数据里，不相干内容的最高相关系数只有 0.67，和命中值（0.94~0.99）拉得很开，
所以窗口开宽是安全的——阈值比窗口更能拦住误判。
"""
import array
import math
import operator
import os
import subprocess

from video_manager import probe, skip

SR = 8000                 # 采样率：只要包络，8kHz 足够
HOP_MS = 250              # 包络步长
ALIGN_WINDOW_MS = 180000  # 在模板起点前后各 3 分钟内搜索（见文件头实测依据）
ACCEPT_SCORE = 0.82       # 命中阈值（同一段音频实测 0.94~1.00，无关内容 ≤0.67）
REJECT_SCORE = 0.60       # 低于此判为「这集没有片头」
MAX_DECODE_MS = 900000    # 单次最多解 15 分钟音频，防止畸形文件把后台线程拖死

DECODE_TIMEOUT = 120
_ENV_CACHE = {}
_ENV_CACHE_MAX = 24

_create_no_window = 0
if os.name == "nt":
    _create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def available():
    return probe.has_ffmpeg()


def envelope(path, start_ms=0, length_ms=0):
    """抽音量包络：每 HOP_MS 一个 RMS。失败返回 None（调用方按「未知」处理）

    start_ms/length_ms 是解码窗口——只取需要的那一段，别整集解码。
    `-ss` 必须放在 `-i` 之前，否则是先解码再丢弃，大文件慢到不可用。
    """
    ffmpeg = probe.find_tool("ffmpeg")
    if not ffmpeg or not path:
        return None
    cmd = [ffmpeg, "-nostdin", "-v", "error"]
    if start_ms > 0:
        cmd += ["-ss", f"{start_ms / 1000.0:.3f}"]
    if length_ms > 0:
        cmd += ["-t", f"{length_ms / 1000.0:.3f}"]
    cmd += ["-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]

    try:
        proc = subprocess.run(  # noqa: S603 - 内部拼装的绝对路径，无 shell
            cmd, capture_output=True, timeout=DECODE_TIMEOUT,
            creationflags=_create_no_window, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None

    samples = array.array("h")
    try:
        samples.frombytes(proc.stdout)
    except ValueError:
        return None
    hop = int(SR * HOP_MS / 1000)
    env = []
    for i in range(0, len(samples) - hop + 1, hop):
        chunk = samples[i:i + hop]
        total = sum(map(operator.mul, chunk, chunk))
        env.append(math.sqrt(total / hop))
    return env or None


def _cached_envelope(path, start_ms, length_ms):
    """同一集重复对齐时别再解码一遍（缓存键带 mtime，文件被替换会失效）"""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0
    key = (os.path.normcase(path), int(mtime), int(start_ms), int(length_ms))
    if key in _ENV_CACHE:
        return _ENV_CACHE[key]
    env = envelope(path, start_ms, length_ms)
    if env:
        if len(_ENV_CACHE) >= _ENV_CACHE_MAX:
            _ENV_CACHE.pop(next(iter(_ENV_CACHE)))
        _ENV_CACHE[key] = env
    return env


def _zscore(seq):
    n = len(seq)
    if n == 0:
        return None
    mean = sum(seq) / n
    var = sum((x - mean) ** 2 for x in seq) / n
    sd = math.sqrt(var)
    if sd < 1e-9:
        return None
    return [(x - mean) / sd for x in seq]


def correlation(a, b):
    """两段等长序列的皮尔逊相关；任一段是静音（方差≈0）时返回 -1"""
    if len(a) != len(b) or not a:
        return -1.0
    za, zb = _zscore(a), _zscore(b)
    if za is None or zb is None:
        return -1.0
    return sum(map(operator.mul, za, zb)) / len(za)


def _best_offset(ref_env, target_env):
    """把 ref_env 在 target_env 上滑动，返回 (最佳偏移, 皮尔逊相关)

    增量实现：ref 只做一次 z 标准化，目标窗口的均值/方差用前缀和 O(1) 求出，
    每个偏移只剩一次点积。±3 分钟窗口（1440 个偏移 × 360 点）也就几十万次乘加，
    换成「每个偏移重算一次 z 分数」要慢 3 倍以上。
    """
    n = len(ref_env)
    count = len(target_env) - n + 1
    if n < 4 or count <= 0:
        return None, -2.0

    ref_z = _zscore(ref_env)
    if ref_z is None:
        return None, -2.0

    prefix = [0.0] * (len(target_env) + 1)
    prefix_sq = [0.0] * (len(target_env) + 1)
    for i, value in enumerate(target_env):
        prefix[i + 1] = prefix[i] + value
        prefix_sq[i + 1] = prefix_sq[i] + value * value

    best_offset, best_score = None, -2.0
    for offset in range(count):
        total = prefix[offset + n] - prefix[offset]
        total_sq = prefix_sq[offset + n] - prefix_sq[offset]
        mean_y = total / n
        var_y = total_sq / n - mean_y * mean_y
        if var_y <= 1e-9:
            continue  # 静音窗口谈不上相关，别让浮点噪声刷出假的最高分
        dot = sum(map(operator.mul, ref_z, target_env[offset:offset + n]))
        score = dot / (n * math.sqrt(var_y))
        if score > best_score:
            best_offset, best_score = offset, score
        if best_score >= 0.999:
            break  # 已经完全一致，再算也没意义
    return best_offset, best_score


def align_op(target_path, ref_path, ref_start_ms, ref_end_ms,
             window_ms=ALIGN_WINDOW_MS):
    """把 [ref_start, ref_end] 这段片头对齐到 target_path 上

    返回 {"status": "ok"/"absent"/"unknown", "start_ms", "end_ms", "score"}
    - ok      ：找到了，start/end 是这一集的真实起止
    - absent  ：这段在这集明显不存在（第一集/最终话/总集篇）
    - unknown ：没 ffmpeg、解码失败、区间太短、分数夹在中间——调用方回落到作品级模板
    """
    start = max(0, int(ref_start_ms))
    end = int(ref_end_ms)
    if end - start < skip.MIN_SEGMENT_MS:
        return {"status": "unknown", "start_ms": start, "end_ms": end, "score": 0.0}

    ref_env = _cached_envelope(ref_path, start, end - start)
    if not ref_env or len(ref_env) < 4:
        return {"status": "unknown", "start_ms": start, "end_ms": end, "score": 0.0}

    win = len(ref_env)
    win_ms = win * HOP_MS
    base = max(0, start - window_ms)
    span = min(win_ms + window_ms * 2, MAX_DECODE_MS)
    target_env = _cached_envelope(target_path, base, span)
    if not target_env or len(target_env) < win:
        # 目标集比片头还短 / 解码不出来，别乱下结论
        return {"status": "unknown", "start_ms": start, "end_ms": end, "score": 0.0}

    best_offset, best_score = _best_offset(ref_env, target_env)
    if best_offset is None:
        return {"status": "unknown", "start_ms": start, "end_ms": end, "score": 0.0}

    found_start = base + best_offset * HOP_MS
    found_end = found_start + win_ms
    if best_score >= ACCEPT_SCORE:
        return {"status": "ok", "start_ms": found_start, "end_ms": found_end,
                "score": round(best_score, 3)}
    if best_score < REJECT_SCORE:
        return {"status": "absent", "start_ms": start, "end_ms": end,
                "score": round(best_score, 3)}
    # 介于两者之间：不推翻用户标的模板，也不判定为缺失
    return {"status": "unknown", "start_ms": start, "end_ms": end,
            "score": round(best_score, 3)}
