"""视频「继续观看」：续播列表的数据边界 + 几处防回退守卫

progress 表按文件路径记位置，作品/作者信息要 JOIN 回去；这条链路上最容易
出错的不是 SQL 语法，而是边界——孤儿记录（视频已删）、位置为 0 的空记录、
两处 duration 都可能为 0。这些错法都不会报错，只会让列表里出现空行或
「还剩 --:--」，所以逐条锁住。
"""
import ast
import os

import pytest

from resource_manager import config
from video_manager import database as db

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

V1 = "X:/甲/温泉/第01集.mp4"
V2 = "X:/甲/温泉/第02集.mp4"
V3 = "X:/乙/海边/第01集.mp4"


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VIDEO_DB_PATH", str(tmp_path / "video.db"))
    db.end_persistent()
    db.init_db()

    with db.get_conn() as conn:
        conn.execute("INSERT INTO authors (id, name) VALUES (1, '作者甲')")
        conn.execute("INSERT INTO authors (id, name) VALUES (2, '作者乙')")
        conn.execute(
            "INSERT INTO works (id, name, path, thumbnail, author_id)"
            " VALUES (1, '温泉旅行', 'X:/甲/温泉', '', 1)"
        )
        conn.execute(
            "INSERT INTO works (id, name, path, thumbnail, author_id)"
            " VALUES (2, '海边合集', 'X:/乙/海边', '', 2)"
        )
        conn.executemany(
            "INSERT INTO videos (id, work_id, path, name, duration_ms)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (1, 1, V1, "第01集", 600_000),
                (2, 1, V2, "第02集", 0),  # 没 probe 到时长
                (3, 2, V3, "第01集", 900_000),
            ],
        )
    return tmp_path


def _stamp(path, updated_at):
    """set_progress 用的是当前秒，同一秒内写多条会并列，测试里手动拉开时间"""
    with db.get_conn() as conn:
        conn.execute("UPDATE progress SET updated_at = ? WHERE path = ?", (updated_at, path))


def test_empty_db_returns_empty(seeded):
    assert db.list_resume() == []
    assert db.count_resume() == 0


def test_orders_by_last_watched(seeded):
    db.set_progress(V1, 120_000, 600_000)
    db.set_progress(V3, 300_000, 900_000)
    _stamp(V1, 1_000)
    _stamp(V3, 2_000)

    rows = db.list_resume()
    assert [r["path"] for r in rows] == [V3, V1]


def test_zero_position_is_hidden(seeded):
    """pos=0 是「刚点开就关」或「看完了被清过」的残留，不该出现在继续观看里"""
    db.set_progress(V1, 0, 600_000)
    assert db.list_resume() == []
    assert db.count_resume() == 0


def test_orphan_progress_is_hidden(seeded):
    """视频被删/改名后进度表还留着记录，INNER JOIN 要把它过滤掉"""
    db.set_progress("X:/已经/不在了.mp4", 50_000, 900_000)
    db.set_progress(V1, 10_000, 600_000)
    rows = db.list_resume()
    assert [r["path"] for r in rows] == [V1]
    assert db.count_resume() == 1


def test_duration_falls_back_to_videos_table(seeded):
    """播放时没读到时长（progress.duration_ms=0）就用扫描时 ffprobe 的结果"""
    db.set_progress(V1, 120_000, 0)
    row = db.list_resume()[0]
    assert row["duration_ms"] == 600_000


def test_duration_zero_when_nowhere_known(seeded):
    db.set_progress(V2, 45_000, 0)
    row = db.list_resume()[0]
    assert row["duration_ms"] == 0
    assert row["position_ms"] == 45_000


def test_joins_work_and_author(seeded):
    """续播卡要显示作品名/作者/封面，JOIN 少一层就会拿到 None"""
    db.set_progress(V3, 60_000, 900_000)
    row = db.list_resume()[0]
    assert row["work_id"] == 2
    assert row["work_name"] == "海边合集"
    assert row["author"] == "作者乙"
    assert row["work_path"] == "X:/乙/海边"
    assert row["video_name"] == "第01集"


def test_limit_is_respected(seeded):
    db.set_progress(V1, 10_000, 600_000)
    db.set_progress(V2, 20_000, 0)
    db.set_progress(V3, 30_000, 900_000)
    assert len(db.list_resume(limit=2)) == 2
    # 计数不受 limit 影响，侧栏上的 (n) 是全量
    assert db.count_resume() == 3


def test_missing_table_returns_empty(tmp_path, monkeypatch):
    """表还没建时（首次启动、命令行先于模块窗口）不许抛异常"""
    monkeypatch.setattr(config, "VIDEO_DB_PATH", str(tmp_path / "empty.db"))
    db.end_persistent()
    assert db.list_resume() == []
    assert db.count_resume() == 0


# ==================== 静态守卫 ====================

def _tree(filename):
    return ast.parse(open(os.path.join(SRC_DIR, filename), encoding="utf-8").read())


def test_resume_card_reuses_video_card():
    """续播卡必须继承作品卡

    另写一套卡片会复制封面懒加载那段（几十行 + 线程池信号），
    之后改配色/改圆角就得改两处，迟早长歪。
    """
    for node in _tree("ui/video_widgets.py").body:
        if isinstance(node, ast.ClassDef) and node.name == "ResumeCard":
            bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
            assert "VideoCard" in bases
            return
    pytest.fail("ui/video_widgets.py 里找不到 ResumeCard")


def test_load_works_guards_non_int_author_id():
    """选中「继续观看」时 UserRole 是标记字符串，当作 author_id 传进 SQL 会查空"""
    calls = set()
    for node in ast.walk(_tree("ui/video_view.py")):
        if isinstance(node, ast.FunctionDef) and node.name == "load_works":
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                    calls.add(call.func.id)
            break
    assert "isinstance" in calls, "load_works 必须校验 author_id 类型"


def test_visible_loader_dispatches_by_page():
    """封面懒加载要按当前页选卡片组，不能只认作品墙"""

    def symbols(func_name):
        """函数里出现的标识符：Name 的 id + Attribute 的 attr 都要算上
        （`self._resume_cards` 只有 attr 是 `_resume_cards`）"""
        found = set()
        for node in ast.walk(_tree("ui/video_view.py")):
            if isinstance(node, ast.FunctionDef) and node.name == func_name:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Name):
                        found.add(sub.id)
                    elif isinstance(sub, ast.Attribute):
                        found.add(sub.attr)
                break
        return found

    dispatcher = symbols("_load_visible_cards")
    assert "RESUME_PAGE" in dispatcher
    assert {"_resume_cards", "resume_scroll"} <= dispatcher
    # 加载动作本身复用同一个函数，别给续播页另写一份懒加载
    assert "load_thumbnail" in symbols("_load_cards_in")
