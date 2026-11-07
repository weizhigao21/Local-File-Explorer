"""续播位置与时长显示的纯逻辑（无 Qt、无 IO，便于单测）

播放器窗口只负责把这里算出的毫秒数塞给 QMediaPlayer；「从哪里接着看」
「播完了要不要从头」这类规则全在本模块，改规则时不碰窗口代码。
"""

# 距结尾不足这个余量就算「看完了」，下次从头开始
FINISHED_SLACK_MS = 5000

# 开头 3 秒内不记续播点：刚点开就关掉的记录没有意义，下次还是从头
MIN_RESUME_MS = 3000

# 续播点距结尾至少保留的余量，避免一进去就立刻触发 EndOfMedia
TAIL_GUARD_MS = 1000


def format_duration(ms) -> str:
    """时长显示："12:34" / "1:02:03"；未知或非法返回 "--:--"

    时长未知（ffprobe 未装、文件损坏）时显示占位符而不是 00:00，
    否则用户分不清「0 秒」和「还没读到」。
    """
    try:
        total = int(ms)
    except (TypeError, ValueError):
        return "--:--"
    if total <= 0:
        return "--:--"
    return _clock(total)


def format_position(ms) -> str:
    """播放位置显示：0 也要显示成 "00:00"（进度条起点不该是 "--:--"）"""
    try:
        total = int(ms)
    except (TypeError, ValueError):
        return "--:--"
    if total < 0:
        return "--:--"
    return _clock(total)


def _clock(total_ms: int) -> str:
    total = int(total_ms) // 1000
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def resume_position(saved_ms, duration_ms) -> int:
    """算出这次应该从第几毫秒开始播

    - 没有记录 / 记录非正 → 0（从头）
    - 时长未知 → 先信记录，播放器拿到 duration 后会再夹一次
    - 已看到结尾（含 5 秒余量）→ 0，否则「看完了再点开」会立刻又结束
    - 记录在开头 3 秒内 → 0，避免每次点开都闪一下
    - 其余 → 记录值，但不超过 duration - 1s
    """
    try:
        saved = int(saved_ms or 0)
    except (TypeError, ValueError):
        return 0
    if saved <= MIN_RESUME_MS:
        return 0

    try:
        duration = int(duration_ms or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration <= 0:
        return saved
    if saved >= duration - FINISHED_SLACK_MS:
        return 0
    return max(0, min(saved, duration - TAIL_GUARD_MS))


def is_finished(position_ms, duration_ms) -> bool:
    """是否已播到结尾（写库时用来决定「清空续播点」还是「更新续播点」）"""
    try:
        position = int(position_ms or 0)
        duration = int(duration_ms or 0)
    except (TypeError, ValueError):
        return False
    if duration <= 0:
        return False
    return position >= duration - FINISHED_SLACK_MS


def next_index(index: int, total: int) -> int | None:
    """下一个视频下标；到末尾或参数非法返回 None（不循环）"""
    if total <= 0 or index < 0 or index >= total - 1:
        return None
    return index + 1


def prev_index(index: int, total: int) -> int | None:
    """上一个视频下标；已在第一个或参数非法返回 None"""
    if total <= 0 or index <= 0:
        return None
    return index - 1


def progress_ratio(position_ms, duration_ms) -> float:
    """播放进度 0.0~1.0（给进度条/进度列用），未知时长返回 0.0"""
    try:
        position = int(position_ms or 0)
        duration = int(duration_ms or 0)
    except (TypeError, ValueError):
        return 0.0
    if duration <= 0 or position <= 0:
        return 0.0
    return max(0.0, min(1.0, position / duration))


def decide_progress(position_ms, duration_ms):
    """决定这次落库该「记位置 / 清空 / 跳过」

    返回 (action, position_ms)，action 取值：
    - "skip"  ：时长未知或还没开始播，什么都不写（否则库里全是 00:00）
    - "clear" ：已播到结尾，清掉续播点（否则下次一打开就立刻结束）
    - "set"   ：正常记录，下次从这里接着看
    """
    try:
        position = int(position_ms or 0)
        duration = int(duration_ms or 0)
    except (TypeError, ValueError):
        return "skip", 0
    if duration <= 0 or position <= 0:
        return "skip", 0
    if is_finished(position, duration):
        return "clear", 0
    return "set", position


def should_keep_current_session(current_path, target_path, active) -> bool:
    """再次打开同一个视频时，是否应该「只把窗口提前」而不重新加载

    判据三条缺一不可：
    1. 播放器当前确实持有文件（空串不算，否则第一次打开就会被误挡）；
    2. 路径相同（调用方负责 normcase/abspath 规范化）；
    3. 播放器仍处于播放或暂停态（**不要**只看路径：换源失败、播放出错后
       路径还指着旧文件，只比路径会让这个文件再也打不开——v1.8.18 音频侧
       踩过同一个坑）。
    """
    if not current_path or not target_path:
        return False
    return bool(active) and current_path == target_path
