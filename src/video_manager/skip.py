"""片头 / 片尾自动跳过的纯逻辑（无 Qt、无 IO，便于单测）

为什么按「作品」记而不是按文件记：
同一部连续动画每一集的 OP/ED 是同一段画面，位置也几乎不变，标一次就该全剧通用。
逐集的细微差异（有的集有冷开场、有的集片头提前几秒）交给 `skip_align` 用音频对齐微调。

区间语义：
- `kind="op"`：start/end 都是绝对毫秒，`end - start` 是片头长度；
- `kind="ed"`：start 是片尾起点，**end 允许为 0**，表示「一直到文件结尾」。
  标记时可能还不知道真实时长（ffprobe 没装 / 还没读到 duration），
  写 0 而不是写一个猜的时长，等播放时用真实 duration 兜底。

本模块只回答两个问题：这一集该跳哪几段、当前位置该跳到哪。跳不跳由调用方决定。
"""

OP = "op"
ED = "ed"
KINDS = (OP, ED)

# 短于 5 秒的区间没有跳过价值，多半是误标（按早了/按晚了）
MIN_SEGMENT_MS = 5000

# 跳到结尾时留一点余量：直接 setPosition(duration) 有些后端不触发 EndOfMedia
SKIP_TO_END_GUARD_MS = 200


def _int(value, default=0):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def effective_end(kind, end_ms, duration_ms):
    """算出一段的实际终点

    ED 的 end 为 0 表示「到结尾」，用真实时长兜底；时长未知返回 0（调用方据此判为不可跳）。
    """
    end = _int(end_ms)
    if end > 0:
        return end
    if kind == ED:
        duration = _int(duration_ms)
        return duration if duration > 0 else 0
    return 0


def build_plan(segments, overrides, duration_ms=0, skip_op=True, skip_ed=True):
    """把「作品级模板」与「本集对齐结果」整理成本集真正要跳的区间

    segments  : {kind: {"start_ms", "end_ms"}}           作品级（用户标的那次）
    overrides : {kind: {"start_ms", "end_ms", "status"}} 本集对齐结果，status 为
                "ok"（已对齐，用它的时间）/ "absent"（这集没有该段，别跳）
    duration_ms: 本集时长，用于给 ED 的 end=0 兜底；未知传 0

    返回按起点排序的 [(kind, start, end)]，其中 end 可能是 0（ED 且时长未知）。
    调用方若拿到 end=0，应当视为「这一段暂时不可跳」——宁可这次不跳，
    也不能猜一个终点把正片砍掉。
    """
    plan = []
    enabled = {OP: bool(skip_op), ED: bool(skip_ed)}
    segments = segments or {}
    overrides = overrides or {}

    for kind in KINDS:
        if not enabled.get(kind):
            continue
        template = segments.get(kind)
        if not template:
            continue

        override = overrides.get(kind) or {}
        if (override.get("status") or "") == "absent":
            # 对齐判定这集没有这一段（第一集/最终话常见），保持不跳
            continue

        source = override if (override.get("status") or "") == "ok" else template
        start = _int(source.get("start_ms"))
        end = effective_end(kind, source.get("end_ms"), duration_ms)
        if start < 0 or end <= 0 or end - start < MIN_SEGMENT_MS:
            continue
        plan.append((kind, start, end))

    plan.sort(key=lambda item: item[1])
    return plan


def pick_skip(position_ms, plan, skipped, duration_ms=0):
    """当前位置落在哪一段没跳过的区间里 → 返回 (kind, target_ms)，否则 None

    skipped 是「本集已经处理过的 kind 集合」：跳过一次就不再跳，
    用户手动拖回片头里看的时候也不会被立刻弹走（撤销由调用方清这个集合）。
    """
    position = _int(position_ms)
    if position <= 0 or not plan:
        return None
    skipped = skipped or set()
    for kind, start, end in plan:
        if kind in skipped:
            continue
        resolved_end = effective_end(kind, end, duration_ms)
        if resolved_end <= 0:
            continue
        if start <= position < resolved_end:
            target = resolved_end
            if kind == ED:
                duration = _int(duration_ms)
                # 直接跳到 duration 有的后端不触发 EndOfMedia，留 200ms 余量
                target = max(start, resolved_end - SKIP_TO_END_GUARD_MS)
                if duration > 0:
                    target = min(target, max(start, duration - SKIP_TO_END_GUARD_MS))
            return kind, target
    return None


def inside_segment(position_ms, plan, duration_ms=0):
    """当前位置是否落在任一段里（不管跳过没跳过），用户拖动时用来判「他主动要看这段」"""
    position = _int(position_ms)
    if position <= 0:
        return None
    for kind, start, end in plan:
        resolved_end = effective_end(kind, end, duration_ms)
        if resolved_end > 0 and start <= position < resolved_end:
            return kind
    return None


def segment_duration(kind, start_ms, end_ms, duration_ms=0):
    """区间长度（毫秒）；ED 的 end=0 用时长兜底。给界面显示「跳过了 xx 秒」用"""
    start = _int(start_ms)
    end = effective_end(kind, end_ms, duration_ms)
    return max(0, end - start)


def label(kind):
    """区间种类的中文名（提示条文案统一从这里取，别在界面里散写字面量）"""
    return {OP: "片头", ED: "片尾"}.get(kind, "片段")
