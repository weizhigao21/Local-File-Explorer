"""片头/片尾跳过：纯函数边界 + 两级存储（作品模板 / 逐集对齐）+ 两处静态守卫

这块最容易出的不是崩溃，而是「悄悄跳错」——跳到正片中间用户还以为文件坏了。
所以这里锁的都是边界：ED 的 end=0 什么时候能跳、对齐判定 absent 时该不该跳、
用户手动拖回片头后会不会又被弹走、模板改了旧的对齐结果有没有作废。
"""
import ast
import os

import pytest

from resource_manager import config
from video_manager import database as db
from video_manager import skip

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MIN = 60_000          # 一分钟
DURATION = 1_440_000  # 24 分钟一集

OP_TEMPLATE = {"op": {"start_ms": 12_000, "end_ms": 102_000, "source_path": "X:/甲/第01集.mp4"}}
ED_TEMPLATE = {"ed": {"start_ms": 1_380_000, "end_ms": 0, "source_path": "X:/甲/第01集.mp4"}}


# ==================== build_plan ====================

def test_template_alone_builds_plan():
    plan = skip.build_plan(OP_TEMPLATE, {}, DURATION)
    assert plan == [(skip.OP, 12_000, 102_000)]


def test_ed_end_zero_falls_back_to_duration():
    """ED 记的时候可能还不知道时长，end=0 = 「到结尾」，得用真实时长兜底"""
    plan = skip.build_plan(ED_TEMPLATE, {}, DURATION)
    assert plan == [(skip.ED, 1_380_000, DURATION)]


def test_ed_without_duration_is_not_skippable():
    """时长未知时不能猜终点——猜错了就是把正片砍掉"""
    assert skip.build_plan(ED_TEMPLATE, {}, 0) == []


def test_override_ok_wins_over_template():
    """逐集对齐过了就用对齐结果，哪怕和作品模板差几秒"""
    overrides = {"op": {"start_ms": 0, "end_ms": 90_000, "status": "ok"}}
    plan = skip.build_plan(OP_TEMPLATE, overrides, DURATION)
    assert plan == [(skip.OP, 0, 90_000)]


def test_override_absent_disables_that_kind():
    """对齐判定「这集没有片头」（第一集/最终话常见）→ 这一集不跳"""
    overrides = {"op": {"start_ms": 0, "end_ms": 0, "status": "absent"}}
    assert skip.build_plan(OP_TEMPLATE, overrides, DURATION) == []


def test_override_unknown_falls_back_to_template():
    overrides = {"op": {"start_ms": 0, "end_ms": 0, "status": "unknown"}}
    assert skip.build_plan(OP_TEMPLATE, overrides, DURATION) == [
        (skip.OP, 12_000, 102_000)
    ]


def test_plan_is_sorted_by_start():
    segments = {
        "ed": {"start_ms": 1_380_000, "end_ms": 0},
        "op": {"start_ms": 12_000, "end_ms": 102_000},
    }
    assert [kind for kind, _s, _e in skip.build_plan(segments, {}, DURATION)] == [
        skip.OP, skip.ED
    ]


def test_too_short_segments_are_dropped():
    """误标（按早了）会得到一个几秒的区间，跳它没意义还容易跳歪"""
    short = {"op": {"start_ms": 10_000, "end_ms": 12_000}}
    assert skip.build_plan(short, {}, DURATION) == []


def test_disabled_kind_is_ignored():
    assert skip.build_plan(OP_TEMPLATE, {}, DURATION, skip_op=False) == []
    assert skip.build_plan(ED_TEMPLATE, {}, DURATION, skip_ed=False) == []
    # 关掉片头不影响片尾
    both = {**OP_TEMPLATE, **ED_TEMPLATE}
    assert [k for k, _s, _e in skip.build_plan(both, {}, DURATION, skip_op=False)] == [
        skip.ED
    ]


def test_empty_inputs_are_safe():
    assert skip.build_plan(None, None, 0) == []
    assert skip.build_plan({}, {}, DURATION) == []


# ==================== pick_skip ====================

def test_pick_skip_inside_op():
    plan = [(skip.OP, 12_000, 102_000)]
    assert skip.pick_skip(30_000, plan, set(), DURATION) == (skip.OP, 102_000)


def test_pick_skip_boundaries():
    plan = [(skip.OP, 12_000, 102_000)]
    assert skip.pick_skip(12_000, plan, set(), DURATION) is not None  # 起点算在里面
    assert skip.pick_skip(102_000, plan, set(), DURATION) is None      # 终点不算
    assert skip.pick_skip(11_999, plan, set(), DURATION) is None


def test_pick_skip_once_per_kind():
    """跳过一次就不再跳，否则每帧都跳、用户永远卡在片头后面"""
    plan = [(skip.OP, 12_000, 102_000)]
    assert skip.pick_skip(30_000, plan, {skip.OP}, DURATION) is None


def test_pick_skip_ed_stops_before_duration():
    """直接跳到 duration 有的后端不触发 EndOfMedia，得留 200ms 给播放器收尾"""
    plan = [(skip.ED, 1_380_000, 0)]
    kind, target = skip.pick_skip(1_400_000, plan, set(), DURATION)
    assert kind == skip.ED
    assert DURATION - 500 < target <= DURATION


def test_pick_skip_ed_never_rewinds():
    """ED 起点离结尾不足 200ms 时，兜底不能把目标算到起点前面去"""
    plan = [(skip.ED, DURATION - 100, 0)]
    kind, target = skip.pick_skip(DURATION - 50, plan, set(), DURATION)
    assert kind == skip.ED
    assert target >= DURATION - 100


def test_pick_skip_zero_position_and_empty_plan():
    assert skip.pick_skip(0, [(skip.OP, 0, 90_000)], set(), DURATION) is None
    assert skip.pick_skip(30_000, [], set(), DURATION) is None


# ==================== inside_segment ====================

def test_inside_segment_reports_user_will_watch_it():
    plan = [(skip.OP, 12_000, 102_000), (skip.ED, 1_380_000, 0)]
    assert skip.inside_segment(50_000, plan, DURATION) == skip.OP
    assert skip.inside_segment(1_400_000, plan, DURATION) == skip.ED
    assert skip.inside_segment(300_000, plan, DURATION) is None


def test_label_is_chinese():
    assert skip.label(skip.OP) == "片头"
    assert skip.label(skip.ED) == "片尾"


# ==================== 数据库两级存储 ====================

@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VIDEO_DB_PATH", str(tmp_path / "video.db"))
    db.end_persistent()
    db.init_db()
    with db.get_conn() as conn:
        conn.execute("INSERT INTO authors (id, name) VALUES (1, '动漫')")
        conn.execute(
            "INSERT INTO works (id, name, path, thumbnail, author_id)"
            " VALUES (1, '大理寺日志', 'F:/动漫/大理寺日志', '', 1)"
        )
        conn.executemany(
            "INSERT INTO videos (id, work_id, path, name, duration_ms)"
            " VALUES (?, 1, ?, ?, ?)",
            [(1, "F:/动漫/大理寺日志/1.mp4", "1.mp4", DURATION),
             (2, "F:/动漫/大理寺日志/2.mp4", "2.mp4", DURATION)],
        )
    yield tmp_path
    db.end_persistent()


def test_segment_roundtrip(seeded):
    assert db.set_skip_segment(1, skip.OP, 12_000, 102_000, "F:/a.mp4")
    got = db.get_skip_segments(1)
    assert got[skip.OP]["start_ms"] == 12_000
    assert got[skip.OP]["end_ms"] == 102_000
    assert got[skip.OP]["source_path"] == "F:/a.mp4"


def test_segment_upsert_keeps_one_row(seeded):
    db.set_skip_segment(1, skip.OP, 12_000, 102_000, "F:/a.mp4")
    db.set_skip_segment(1, skip.OP, 0, 90_000, "F:/a.mp4")
    got = db.get_skip_segments(1)
    assert list(got) == [skip.OP]
    assert got[skip.OP]["start_ms"] == 0


def test_retemplate_invalidates_overrides(seeded):
    """模板改了，之前按旧模板算出来的逐集对齐结果必须作废，否则整剧都跳歪"""
    db.set_skip_segment(1, skip.OP, 12_000, 102_000, "F:/a.mp4")
    db.set_skip_override("F:/动漫/大理寺日志/2.mp4", skip.OP, 8_000, 98_000, "ok")
    assert db.get_skip_overrides("F:/动漫/大理寺日志/2.mp4")[skip.OP]["start_ms"] == 8_000

    db.set_skip_segment(1, skip.OP, 0, 90_000, "F:/a.mp4")
    assert db.get_skip_overrides("F:/动漫/大理寺日志/2.mp4") == {}


def test_clear_segment_removes_overrides(seeded):
    db.set_skip_segment(1, skip.ED, 1_380_000, 0, "F:/a.mp4")
    db.set_skip_override("F:/动漫/大理寺日志/2.mp4", skip.ED, 1_380_000, 0, "ok")
    db.clear_skip_segment(1, skip.ED)
    assert db.get_skip_segments(1) == {}
    assert db.get_skip_overrides("F:/动漫/大理寺日志/2.mp4") == {}


def test_clear_all_segments(seeded):
    db.set_skip_segment(1, skip.OP, 12_000, 102_000, "F:/a.mp4")
    db.set_skip_segment(1, skip.ED, 1_380_000, 0, "F:/a.mp4")
    db.clear_skip_segment(1)
    assert db.get_skip_segments(1) == {}


def test_delete_work_cascades(seeded):
    db.set_skip_segment(1, skip.OP, 12_000, 102_000, "F:/a.mp4")
    db.set_skip_override("F:/动漫/大理寺日志/2.mp4", skip.OP, 12_000, 102_000, "ok")
    db.delete_work(1)
    assert db.get_skip_segments(1) == {}
    assert db.get_skip_overrides("F:/动漫/大理寺日志/2.mp4") == {}


def test_delete_author_cascades(seeded):
    """作者库里视频先被删、覆盖表再删就查不到路径了——顺序错了会留孤儿"""
    db.set_skip_segment(1, skip.OP, 12_000, 102_000, "F:/a.mp4")
    db.set_skip_override("F:/动漫/大理寺日志/2.mp4", skip.OP, 12_000, 102_000, "ok")
    db.delete_author(1)
    assert db.get_skip_segments(1) == {}
    assert db.get_skip_overrides("F:/动漫/大理寺日志/2.mp4") == {}


def test_missing_table_is_survivable(tmp_path, monkeypatch):
    """库还没建表时（首次启动 / 播放器先于模块窗口创建）只该「不跳」，不许抛"""
    monkeypatch.setattr(config, "VIDEO_DB_PATH", str(tmp_path / "empty.db"))
    db.end_persistent()
    assert db.get_skip_segments(1) == {}
    assert db.get_skip_overrides("X:/nope.mp4") == {}
    assert db.get_work_id_of_video("X:/nope.mp4") is None


def test_work_id_lookup_by_path(seeded):
    assert db.get_work_id_of_video("F:/动漫/大理寺日志/2.mp4") == 1


# ==================== 音频对齐（align）====================

def _pattern(n, seed=1):
    """确定性伪随机包络：测试里不能真去解视频，得能重现"""
    state, out = seed, []
    for _ in range(n):
        state = (state * 1103515245 + 12345) % (2 ** 31)
        out.append(state / (2 ** 31))
    return out


def test_best_offset_finds_shift():
    """真机库里实测过：第 1 集的片头在第 2 集里整体前移了 120 秒，得能找回来"""
    from video_manager import align

    ref = _pattern(40)
    target = [0.0] * 12 + ref + [0.0] * 8
    offset, score = align._best_offset(ref, target)
    assert offset == 12
    assert score > 0.99


def test_best_offset_rejects_silence():
    """目标全是静音：方差为 0，相关系数没有意义，不能返回一个高分把正片跳掉"""
    from video_manager import align

    assert align._best_offset(_pattern(20), [0.0] * 60) == (None, -2.0)


def test_best_offset_unrelated_audio_scores_low():
    """不相干的内容必须明显低于 ACCEPT —— 否则整部剧都会被跳歪"""
    from video_manager import align

    offset, score = align._best_offset(_pattern(40, seed=1), _pattern(90, seed=7))
    assert offset is not None
    assert score < align.ACCEPT_SCORE


def test_align_op_short_segment_is_unknown():
    from video_manager import align

    result = align.align_op("X:/b.mp4", "X:/a.mp4", 10_000, 11_000)
    assert result["status"] == "unknown"


def test_align_op_without_ffmpeg_is_unknown(monkeypatch):
    """没装 ffmpeg 只该「不跳」，不许抛异常把播放器带崩"""
    from video_manager import align

    monkeypatch.setattr(align.probe, "find_tool", lambda name: None)
    result = align.align_op("X:/b.mp4", "X:/a.mp4", 0, 90_000)
    assert result["status"] == "unknown"


# ==================== 静态守卫 ====================

def _src(rel):
    with open(os.path.join(SRC_DIR, rel), encoding="utf-8") as f:
        return f.read()


def _func_src(rel, name):
    """取某个函数的源码片段（用 AST 定位行号，别用字符串找 `def name`）"""
    source = _src(rel)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    pytest.fail(f"{rel} 里找不到 {name}")


def test_align_puts_ss_before_i():
    """`-ss` 必须放在 `-i` 之前 —— 放后面是先解码再丢弃，大文件慢到不可用"""
    body = _func_src("video_manager/align.py", "envelope")
    assert body.index('"-ss"') < body.index('"-i"')


def test_align_only_searches_near_template():
    """对齐窗口必须有上限：全片盲搜会把「复用的 BGM」当成片头（实测踩过）

    下限也不能太窄：真机库里有整段前移 120 秒的集，±20 秒窗口直接把这种集判成
    「没有片头」，然后模板照旧把正片前两分钟跳掉。
    """
    from video_manager import align

    assert 120_000 <= align.ALIGN_WINDOW_MS <= 300_000
    assert align.REJECT_SCORE < align.ACCEPT_SCORE <= 1.0


def test_player_guards_drag_and_manual_seek():
    """拖拽中不许跳（跟用户抢滑块），手动拖进区间要记账，否则刚拖回去就被弹走"""
    position_cb = _func_src("ui/video_player.py", "_on_position_changed")
    assert "_dragging" in position_cb
    assert "_apply_auto_skip" in position_cb

    seek_cb = _func_src("ui/video_player.py", "_on_seek_released")
    assert "inside_segment" in seek_cb
    assert "_skipped" in seek_cb
