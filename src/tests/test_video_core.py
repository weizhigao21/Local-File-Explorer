"""视频模块核心层守卫（纯 Python，不需要 Qt）

覆盖四类容易悄悄坏掉的东西：
1. 扩展名白名单 —— 扫描器认哪些文件；
2. 三级结构增量扫描 —— 新增/删除/未变化，以及取消时不许误删；
3. 续播规则 —— 看完了要归零、开头几秒不记、时长未知时的兜底；
4. 数据库键 —— 续播点按**路径**存，重新扫描（id 会变）后仍在。
"""
import os

import pytest

from resource_manager import config
from video_manager import database as db
from video_manager import progress, scanner


@pytest.fixture
def library(tmp_path, monkeypatch):
    """每个用例一套独立库：库路径/封面目录/根目录全部指向 tmp"""
    root = tmp_path / "视频"
    root.mkdir()
    monkeypatch.setattr(config, "VIDEO_ROOT", str(root))
    monkeypatch.setattr(config, "VIDEO_ROOTS", [str(root)])
    monkeypatch.setattr(config, "VIDEO_DB_PATH", str(tmp_path / "video.db"))
    monkeypatch.setattr(config, "VIDEO_THUMB_DIR", str(tmp_path / "video_thumbs"))
    # 测试不跑 ffprobe：假文件读不出时长，真跑只会拖慢用例
    monkeypatch.setattr(scanner.probe, "has_ffprobe", lambda: False)
    db.end_persistent()
    db.init_db()
    yield root
    db.end_persistent()


def _make_work(root, author, work, names):
    folder = os.path.join(str(root), author, work)
    os.makedirs(folder, exist_ok=True)
    for name in names:
        with open(os.path.join(folder, name), "wb") as f:
            f.write(b"\x00" * 16)
    return folder


# ==================== 扩展名白名单 ====================

def test_every_configured_extension_is_accepted():
    for ext in config.VIDEO_EXTENSIONS:
        assert scanner.is_video(f"a{ext}"), f"{ext} 在白名单里却判为非视频"
        assert scanner.is_video(f"a{ext.upper()}"), f"{ext.upper()} 大小写未被兼容"


def test_non_video_files_are_rejected():
    for name in ("cover.jpg", "info.txt", "subtitle.ass", "README", "a.mp3"):
        assert not scanner.is_video(name), f"{name} 不该被当成视频"


# ==================== 扫描 ====================

def test_scan_builds_author_work_video_levels(library):
    _make_work(library, "社团A", "作品一", ["第1集.mp4", "第2集.mkv"])
    _make_work(library, "社团A", "作品二", ["整部.wmv"])
    _make_work(library, "社团B", "作品三", ["01.avi"])

    stats = scanner.scan()

    assert stats["added"] == 4
    authors = {a["name"]: a for a in db.list_authors()}
    assert set(authors) == {"社团A", "社团B"}
    assert authors["社团A"]["work_count"] == 2
    assert authors["社团A"]["video_count"] == 3

    works = db.list_works()
    assert len(works) == 3
    work_one = next(w for w in works if w["name"] == "作品一")
    assert work_one["author"] == "社团A"
    assert work_one["video_count"] == 2

    names = [v["name"] for v in db.get_work_videos(work_one["id"])]
    assert names == ["第1集.mp4", "第2集.mkv"]


def test_videos_are_naturally_sorted(library):
    folder = _make_work(library, "社团", "多集", [])
    for name in ("第10集.mp4", "第2集.mp4", "第1集.mp4"):
        open(os.path.join(folder, name), "wb").close()

    scanner.scan()
    work = db.list_works()[0]
    assert [v["name"] for v in db.get_work_videos(work["id"])] == [
        "第1集.mp4", "第2集.mp4", "第10集.mp4",
    ]


def test_rescan_is_incremental(library):
    folder = _make_work(library, "社团", "作品", ["1.mp4", "2.mp4"])
    scanner.scan()

    stats = scanner.scan()
    assert stats["added"] == 0
    assert stats["skipped"] == 2
    assert stats["deleted"] == 0

    open(os.path.join(folder, "3.mp4"), "wb").close()
    stats = scanner.scan()
    assert stats["added"] == 1

    os.remove(os.path.join(folder, "1.mp4"))
    stats = scanner.scan()
    assert stats["deleted"] == 1
    assert db.count_videos() == 2


def test_work_without_videos_is_not_indexed(library):
    _make_work(library, "社团", "空作品", [])
    folder = os.path.join(str(library), "社团", "空作品")
    with open(os.path.join(folder, "说明.txt"), "w", encoding="utf-8") as f:
        f.write("no video here")

    stats = scanner.scan()

    assert stats["added"] == 0
    assert db.list_works() == []


def test_deleted_author_is_cleaned_up(library):
    _make_work(library, "社团A", "作品", ["1.mp4"])
    _make_work(library, "社团B", "作品", ["1.mp4"])
    scanner.scan()
    assert len(db.list_authors()) == 2

    import shutil

    shutil.rmtree(os.path.join(str(library), "社团B"))
    stats = scanner.scan()

    assert stats["deleted_authors"] == 1
    assert [a["name"] for a in db.list_authors()] == ["社团A"]


def test_cancelled_scan_never_deletes(library):
    """取消 = 部分同步：磁盘作者集合不完整，绝不能拿它当删除基准"""
    import threading

    _make_work(library, "社团A", "作品", ["1.mp4"])
    _make_work(library, "社团B", "作品", ["1.mp4"])
    scanner.scan()

    event = threading.Event()
    event.set()
    stats = scanner.scan(cancel_event=event)

    assert stats["cancelled"] is True
    assert stats["deleted_authors"] == 0
    assert len(db.list_authors()) == 2


def test_missing_root_raises_friendly_error(library, monkeypatch):
    monkeypatch.setattr(config, "VIDEO_ROOT", str(library / "不存在"))
    monkeypatch.setattr(config, "VIDEO_ROOTS", [str(library / "不存在")])
    with pytest.raises(FileNotFoundError):
        scanner.scan()


# ==================== 封面路径 ====================

def test_thumbnail_path_is_stable_and_unique(library):
    first = scanner.thumbnail_path(r"E:\视频\作者\作品")
    again = scanner.thumbnail_path(r"E:\视频\作者\作品")
    other = scanner.thumbnail_path(r"E:\视频\作者\作品2")

    assert first == again
    assert first != other
    assert first.endswith(".jpg")
    assert os.path.dirname(first) == config.VIDEO_THUMB_DIR


def test_first_video_path_uses_natural_order(library):
    folder = _make_work(library, "社团", "作品", ["第10集.mp4", "第2集.mp4"])
    first = scanner.first_video_path(folder)
    assert first is not None
    assert os.path.basename(first) == "第2集.mp4"


# ==================== 续播规则 ====================

@pytest.mark.parametrize("saved,duration,expected", [
    (0, 600000, 0),                 # 没记录
    (-5, 600000, 0),                # 脏数据
    (1500, 600000, 0),              # 开头 3 秒内不记
    (120000, 600000, 120000),       # 正常续播
    (598000, 600000, 0),            # 已到结尾（5 秒余量内）→ 从头
    (600000, 600000, 0),
    (120000, 0, 120000),            # 时长未知：先信记录
    (999999, 600000, 0),            # 记录越界 → 视为看完
])
def test_resume_position_rules(saved, duration, expected):
    assert progress.resume_position(saved, duration) == expected


def test_resume_never_lands_exactly_at_end():
    position = progress.resume_position(596000, 600000)
    assert position < 600000


def test_is_finished_uses_five_second_slack():
    assert progress.is_finished(595000, 600000) is True
    assert progress.is_finished(594000, 600000) is False
    assert progress.is_finished(5000, 0) is False  # 未知时长不算看完


@pytest.mark.parametrize("ms,text", [
    (0, "--:--"),
    (-1, "--:--"),
    (None, "--:--"),
    (500, "00:00"),                 # 不足 1 秒：时长有意义但显示为 00:00
    (1000, "00:01"),
    (59_000, "00:59"),
    (60_000, "01:00"),
    (3_600_000, "1:00:00"),
    (3_723_000, "1:02:03"),
])
def test_format_duration(ms, text):
    assert progress.format_duration(ms) == text


@pytest.mark.parametrize("ms,text", [
    (0, "00:00"),      # 进度条起点必须是 00:00，不能是 --:--
    (None, "--:--"),
    (-1, "--:--"),
    (65_000, "01:05"),
])
def test_format_position(ms, text):
    assert progress.format_position(ms) == text


def test_next_and_prev_index_do_not_wrap():
    assert progress.next_index(0, 3) == 1
    assert progress.next_index(2, 3) is None
    assert progress.prev_index(2, 3) == 1
    assert progress.prev_index(0, 3) is None
    assert progress.next_index(0, 0) is None
    assert progress.prev_index(-1, 3) is None


def test_progress_ratio():
    assert progress.progress_ratio(0, 1000) == 0.0
    assert progress.progress_ratio(500, 1000) == 0.5
    assert progress.progress_ratio(2000, 1000) == 1.0   # 夹到 1
    assert progress.progress_ratio(500, 0) == 0.0


@pytest.mark.parametrize("position,duration,action,value", [
    (0, 600000, "skip", 0),          # 还没开始播
    (-1, 600000, "skip", 0),
    (120000, 0, "skip", 0),          # 时长未知
    (120000, 600000, "set", 120000),  # 正常记录
    (598000, 600000, "clear", 0),     # 已到结尾 → 清空
    (600000, 600000, "clear", 0),
])
def test_decide_progress(position, duration, action, value):
    assert progress.decide_progress(position, duration) == (action, value)


def test_should_keep_current_session():
    # 同文件 + 仍在播放/暂停 → 只提窗
    assert progress.should_keep_current_session(r"e:\a\b.mp4", r"e:\a\b.mp4", True) is True
    # 还没打开过任何文件：不能误挡（否则第一个视频永远打不开）
    assert progress.should_keep_current_session("", r"e:\a\b.mp4", True) is False
    # 播放出错/已停止：路径还在，但必须允许重新加载
    assert progress.should_keep_current_session(r"e:\a\b.mp4", r"e:\a\b.mp4", False) is False
    assert progress.should_keep_current_session(r"e:\a\c.mp4", r"e:\a\b.mp4", True) is False


# ==================== 续播点落库 ====================

def test_progress_is_keyed_by_path_and_survives_rescan(library):
    folder = _make_work(library, "社团", "作品", ["第1集.mp4"])
    scanner.scan()
    path = os.path.join(folder, "第1集.mp4")
    db.set_progress(path, 12345, 600000)

    assert db.get_progress(path) == (12345, 600000)

    # 模拟「文件被替换后重新入库」：videos.id 变了，续播点仍按路径命中
    scanner.scan()
    assert db.get_progress(path)[0] == 12345

    db.clear_progress(path)
    assert db.get_progress(path) == (0, 0)


def test_get_progress_map_reads_many_at_once(library):
    folder = _make_work(library, "社团", "作品", ["1.mp4", "2.mp4"])
    scanner.scan()
    first = os.path.join(folder, "1.mp4")
    second = os.path.join(folder, "2.mp4")
    db.set_progress(first, 1000)
    db.set_progress(second, 2000)

    mapping = db.get_progress_map([first, second, os.path.join(folder, "nope.mp4")])

    assert mapping == {first: 1000, second: 2000}


def test_update_video_duration_only_fills_blanks(library):
    folder = _make_work(library, "社团", "作品", ["1.mp4"])
    scanner.scan()
    path = os.path.join(folder, "1.mp4")

    db.update_video_duration(path, 90000)
    videos = db.get_work_videos(db.list_works()[0]["id"])
    assert videos[0]["duration_ms"] == 90000

    # 已有值不再覆盖（避免后一次错误探测把正确时长改坏）
    db.update_video_duration(path, 1000)
    videos = db.get_work_videos(db.list_works()[0]["id"])
    assert videos[0]["duration_ms"] == 90000

# ==================== 两层库：顶层文件夹本身就是作品 ====================

def _make_flat(root, name, videos):
    """根目录下直接就是一个剧集文件夹，视频躺在它里面（没有「作者」这一层）"""
    folder = os.path.join(str(root), name)
    os.makedirs(folder, exist_ok=True)
    for video in videos:
        open(os.path.join(folder, video), "wb").close()
    return folder


def test_two_level_library_treats_folder_as_work(library):
    """根/剧集/第01集.mp4 —— 老逻辑把剧集当作者、再找子文件夹当作品，视频全被丢弃"""
    _make_flat(library, "瑞克和莫蒂 第八季 (2025)", ["S08E01.mp4", "S08E02.mp4"])

    stats = scanner.scan()

    assert stats["added"] == 2
    authors = db.list_authors()
    assert [a["name"] for a in authors] == ["视频"], "两层库应由根目录名兜一个归集作者"
    assert authors[0]["work_count"] == 1

    works = db.list_works()
    assert [w["name"] for w in works] == ["瑞克和莫蒂 第八季 (2025)"]
    assert [v["name"] for v in db.get_work_videos(works[0]["id"])] == [
        "S08E01.mp4", "S08E02.mp4",
    ]


def test_mixed_two_and_three_level_library(library):
    """同一个根目录里既有三层（社团/作品）又有两层（剧集），两边都要入库"""
    _make_work(library, "社团A", "作品一", ["1.mp4"])
    _make_flat(library, "单剧集合", ["第01集.mp4"])

    scanner.scan()

    authors = {a["name"]: a for a in db.list_authors()}
    assert set(authors) == {"社团A", "视频"}
    assert authors["社团A"]["work_count"] == 1
    assert authors["视频"]["work_count"] == 1
    assert db.count_videos() == 2


def test_videos_directly_in_root_become_a_work(library):
    """根目录自己躺着一堆视频：没有子文件夹也不能整批丢掉"""
    for name in ("第01集.mp4", "第02集.mp4"):
        open(os.path.join(str(library), name), "wb").close()

    stats = scanner.scan()

    assert stats["added"] == 2
    works = db.list_works()
    assert [w["name"] for w in works] == ["视频（根目录）"]
    assert works[0]["video_count"] == 2


def test_flat_work_rescan_and_delete(library):
    """两层作品同样走增量：二次扫描不重复、删目录后作品跟着消失"""
    folder = _make_flat(library, "剧集", ["1.mp4"])
    scanner.scan()

    stats = scanner.scan()
    assert stats["added"] == 0
    assert stats["skipped"] == 1

    open(os.path.join(folder, "2.mp4"), "wb").close()
    assert scanner.scan()["added"] == 1

    import shutil

    shutil.rmtree(folder)
    stats = scanner.scan()
    assert stats["deleted_works"] == 1
    assert db.count_videos() == 0


def test_folder_previously_indexed_as_author_is_reclaimed(library):
    """老版本把这个文件夹记成了「作者」（0 个作品）：重扫要纠正，作者记录得清掉"""
    _make_flat(library, "剧集", ["1.mp4"])
    db.add_author("剧集")

    scanner.scan()

    assert [a["name"] for a in db.list_authors()] == ["视频"]
    assert [w["name"] for w in db.list_works()] == ["剧集"]
    assert db.count_videos() == 1


def test_count_authors_matches_scanned_units(library):
    """进度条总数（count_authors）必须等于实际扫描单元数，否则进度永远到不了 100%"""
    _make_work(library, "社团A", "作品一", ["1.mp4"])
    _make_flat(library, "剧集", ["1.mp4"])
    open(os.path.join(str(library), "根部.mp4"), "wb").close()

    assert scanner.count_authors() == 3

    seen = []
    scanner.scan(progress_callback=lambda kind, name, *_args: seen.append(name)
                 if kind == "author" else None)
    assert len(seen) == 3


# ==================== 多根目录（v1.10.0） ====================

def test_scan_covers_every_video_root(library, tmp_path, monkeypatch):
    """两个根目录各自的作品都要进库"""
    second = tmp_path / "视频2"
    second.mkdir()
    _make_work(library, "社团A", "作品一", ["01.mp4"])
    _make_work(second, "社团B", "作品二", ["02.mp4"])

    monkeypatch.setattr(config, "VIDEO_ROOTS", [str(library), str(second)])
    scanner.scan()

    assert sorted(name for _, name in db.list_all_authors()) == ["社团A", "社团B"]
    assert len(db.list_works()) == 2


def test_two_level_roots_each_get_their_own_bucket(library, tmp_path, monkeypatch):
    """两个两层库：各自的归集作者按根目录名分开，作品不会挤在一起"""
    second = tmp_path / "视频2"
    second.mkdir()
    _make_flat(library, "剧集一", ["01.mp4"])
    _make_flat(second, "剧集二", ["02.mp4"])

    monkeypatch.setattr(config, "VIDEO_ROOTS", [str(library), str(second)])
    scanner.scan()

    authors = sorted(name for _, name in db.list_all_authors())
    assert len(authors) == 2, authors
    assert len(db.list_works()) == 2


def test_same_author_name_in_two_roots_is_separated(library, tmp_path, monkeypatch):
    """两棵库都有「社团/作品」：作者名加后缀，作品各自独立"""
    second = tmp_path / "视频2"
    second.mkdir()
    _make_work(library, "社团", "作品", ["01.mp4"])
    _make_work(second, "社团", "作品", ["02.mp4"])

    monkeypatch.setattr(config, "VIDEO_ROOTS", [str(library), str(second)])
    scanner.scan()

    assert sorted(name for _, name in db.list_all_authors()) == ["社团", "社团（视频2）"]
    assert len(db.list_works()) == 2


def test_missing_video_root_keeps_records(library, tmp_path, monkeypatch):
    """根目录消失（NAS 掉线）时不许删作者记录"""
    import shutil

    second = tmp_path / "视频2"
    second.mkdir()
    _make_work(library, "社团A", "作品一", ["01.mp4"])
    _make_work(second, "社团B", "作品二", ["02.mp4"])
    monkeypatch.setattr(config, "VIDEO_ROOTS", [str(library), str(second)])
    scanner.scan()
    assert {name for _, name in db.list_all_authors()} == {"社团A", "社团B"}

    shutil.rmtree(second)
    scanner.scan()
    assert {name for _, name in db.list_all_authors()} == {"社团A", "社团B"}
