"""
音频模块扫描器单元测试
重点覆盖 _update_all_container_track_counts 的展示曲目数聚合逻辑，
尤其是"既有直接音频又有子歌单"的混合型歌单（修复前的 bug 场景）。
使用临时数据库，避免污染真实数据。
"""
import os

import pytest

from audio_manager import database as adb
from audio_manager import scanner as ascanner
from resource_manager import fingerprint_cache as fp


@pytest.fixture
def temp_audio_db(tmp_path, monkeypatch):
    """创建临时音频数据库，避免污染真实数据库"""
    db_path = str(tmp_path / "test_audio.db")
    monkeypatch.setattr(adb, "DB_PATH", db_path)
    # 清除可能存在的长连接，确保用新的临时 DB
    adb.end_persistent()
    adb.init_db()
    yield adb
    adb.end_persistent()


def _add_playlist(db, name, path, parent_path="", track_count=0):
    """添加歌单并可选设置 track_count"""
    pid = db.add_playlist(name, path, parent_path=parent_path)
    if track_count:
        db.update_playlist(pid, track_count=track_count)
    return pid


def _add_tracks(db, playlist_id, count, prefix="track"):
    """给歌单添加 count 首曲目（title/path 自动生成）"""
    tracks = [
        (f"{prefix}_{i}", f"/fake/{playlist_id}/{prefix}_{i}.mp3", 0.0, i + 1)
        for i in range(count)
    ]
    db.add_tracks(playlist_id, tracks)


def _get_track_count(db, playlist_id):
    return db.get_playlist(playlist_id)["track_count"]


# ==================== 聚合逻辑：核心修复场景 ====================

def test_leaf_playlist_keeps_own_count(temp_audio_db, monkeypatch):
    """纯叶子歌单（无子歌单）：track_count = 自身直接曲目数"""
    monkeypatch.setattr(ascanner, "AUDIO_ROOT", "/fake/root")
    db = temp_audio_db
    pid = _add_playlist(db, "叶子", "/fake/root/leaf")
    _add_tracks(db, pid, 5)

    ascanner._update_all_container_track_counts()

    assert _get_track_count(db, pid) == 5


def test_pure_container_aggregates_children(temp_audio_db, monkeypatch):
    """纯容器歌单（无直接音频，有子）：track_count = 子歌单之和"""
    monkeypatch.setattr(ascanner, "AUDIO_ROOT", "/fake/root")
    db = temp_audio_db
    container = _add_playlist(db, "容器", "/fake/root/container", parent_path="")
    c1 = _add_playlist(db, "子1", "/fake/root/container/c1", parent_path="/fake/root/container")
    c2 = _add_playlist(db, "子2", "/fake/root/container/c2", parent_path="/fake/root/container")
    _add_tracks(db, c1, 3)
    _add_tracks(db, c2, 4)

    ascanner._update_all_container_track_counts()

    # 容器自身 0 + 子1(3) + 子2(4) = 7
    assert _get_track_count(db, container) == 7
    assert _get_track_count(db, c1) == 3
    assert _get_track_count(db, c2) == 4


def test_mixed_playlist_aggregates_children(temp_audio_db, monkeypatch):
    """混合型歌单（有直接音频 + 有子歌单）：track_count = 直接 + 子之和

    这是本次修复的核心场景。修复前，混合型歌单只保留直接曲目数，
    不聚合子歌单，导致其父歌单的聚合数也跟着错误。
    """
    monkeypatch.setattr(ascanner, "AUDIO_ROOT", "/fake/root")
    db = temp_audio_db
    # 混合型歌单：2 首直接 + 2 个子歌单（各 5 首、3 首）
    mixed = _add_playlist(db, "混合", "/fake/root/mixed", parent_path="")
    c1 = _add_playlist(db, "子1", "/fake/root/mixed/c1", parent_path="/fake/root/mixed")
    c2 = _add_playlist(db, "子2", "/fake/root/mixed/c2", parent_path="/fake/root/mixed")
    _add_tracks(db, mixed, 2)
    _add_tracks(db, c1, 5)
    _add_tracks(db, c2, 3)

    ascanner._update_all_container_track_counts()

    # 修复前 bug: mixed = 2（仅直接）
    # 修复后: mixed = 2 + 5 + 3 = 10
    assert _get_track_count(db, mixed) == 10
    assert _get_track_count(db, c1) == 5
    assert _get_track_count(db, c2) == 3


def test_parent_of_mixed_playlist_aggregates_correctly(temp_audio_db, monkeypatch):
    """父歌单（容器）的子歌单是混合型时，父歌单的聚合数必须包含混合型子歌单的全部后代"""
    monkeypatch.setattr(ascanner, "AUDIO_ROOT", "/fake/root")
    db = temp_audio_db
    # 结构: root_container (0 直接)
    #        ├── mixed_child (1 直接 + 2 个孙歌单: 24 + 3 = 27)  → 应为 28
    #        └── leaf_child (10 直接)                              → 10
    # 父容器期望 = 0 + 28 + 10 = 38
    parent = _add_playlist(db, "父容器", "/fake/root/parent", parent_path="")
    mixed = _add_playlist(db, "混合子", "/fake/root/parent/mixed",
                          parent_path="/fake/root/parent")
    leaf = _add_playlist(db, "叶子子", "/fake/root/parent/leaf",
                         parent_path="/fake/root/parent")
    g1 = _add_playlist(db, "孙1", "/fake/root/parent/mixed/g1",
                       parent_path="/fake/root/parent/mixed")
    g2 = _add_playlist(db, "孙2", "/fake/root/parent/mixed/g2",
                       parent_path="/fake/root/parent/mixed")
    _add_tracks(db, mixed, 1)
    _add_tracks(db, leaf, 10)
    _add_tracks(db, g1, 24)
    _add_tracks(db, g2, 3)

    ascanner._update_all_container_track_counts()

    # 修复前 bug: parent = 1 + 10 = 11（mixed 被当作叶子，丢失孙歌单 27 首）
    # 修复后: parent = 0 + 28 + 10 = 38
    assert _get_track_count(db, parent) == 38
    assert _get_track_count(db, mixed) == 28
    assert _get_track_count(db, leaf) == 10
    assert _get_track_count(db, g1) == 24
    assert _get_track_count(db, g2) == 3


def test_deep_nesting_aggregation(temp_audio_db, monkeypatch):
    """多层嵌套：容器→混合→容器→叶子，逐层聚合正确"""
    monkeypatch.setattr(ascanner, "AUDIO_ROOT", "/fake/root")
    db = temp_audio_db
    # 结构: L0 (容器, 0直接)
    #        └── L1 (混合, 2直接)
    #             └── L2 (容器, 0直接)
    #                  ├── L3a (叶子, 4直接)
    #                  └── L3b (叶子, 6直接)
    # 期望: L3a=4, L3b=6, L2=10, L1=12, L0=12
    l0 = _add_playlist(db, "L0", "/fake/root/L0", parent_path="")
    l1 = _add_playlist(db, "L1", "/fake/root/L0/L1", parent_path="/fake/root/L0")
    l2 = _add_playlist(db, "L2", "/fake/root/L0/L1/L2", parent_path="/fake/root/L0/L1")
    l3a = _add_playlist(db, "L3a", "/fake/root/L0/L1/L2/L3a",
                        parent_path="/fake/root/L0/L1/L2")
    l3b = _add_playlist(db, "L3b", "/fake/root/L0/L1/L2/L3b",
                        parent_path="/fake/root/L0/L1/L2")
    _add_tracks(db, l1, 2)
    _add_tracks(db, l3a, 4)
    _add_tracks(db, l3b, 6)

    ascanner._update_all_container_track_counts()

    assert _get_track_count(db, l3a) == 4
    assert _get_track_count(db, l3b) == 6
    assert _get_track_count(db, l2) == 10
    assert _get_track_count(db, l1) == 12  # 2 + 10
    assert _get_track_count(db, l0) == 12


def test_stale_track_count_corrected(temp_audio_db, monkeypatch):
    """track_count 字段残留旧聚合值时，函数应基于 tracks 表重新计算并修正

    模拟"指纹未变跳过扫描"路径：DB 中 track_count 是上一轮的聚合值，
    _update_all_container_track_counts 不能直接拿 track_count 当"直接数"，
    否则会重复累加。
    """
    monkeypatch.setattr(ascanner, "AUDIO_ROOT", "/fake/root")
    db = temp_audio_db
    # 容器 + 1 个子叶子
    container = _add_playlist(db, "容器", "/fake/root/container", parent_path="")
    child = _add_playlist(db, "子", "/fake/root/container/child",
                          parent_path="/fake/root/container")
    _add_tracks(db, child, 7)
    # 故意把 track_count 设成"上一轮的聚合值"（容器=7, child=7）
    db.update_playlist(container, track_count=7)
    db.update_playlist(child, track_count=7)

    ascanner._update_all_container_track_counts()

    # child 直接曲目 7，无子 → 7（不变）
    assert _get_track_count(db, child) == 7
    # container 直接 0 + child 7 = 7（不变，但函数必须从 tracks 表算出，不能依赖 track_count）
    assert _get_track_count(db, container) == 7


def test_empty_playlist_stays_zero(temp_audio_db, monkeypatch):
    """无曲目无子歌单的歌单：track_count 保持 0"""
    monkeypatch.setattr(ascanner, "AUDIO_ROOT", "/fake/root")
    db = temp_audio_db
    pid = _add_playlist(db, "空", "/fake/root/empty")

    ascanner._update_all_container_track_counts()

    assert _get_track_count(db, pid) == 0


def test_root_playlist_aggregates_top_level(temp_audio_db, monkeypatch):
    """根目录歌单聚合所有顶层子歌单

    根目录特殊：根的 parent_path="" 且其直接子歌单的 parent_path 也=""，
    需要把根本身从 children_map[""] 中排除，避免自引用。
    """
    root_path = os.path.normpath("/fake/root")
    monkeypatch.setattr(ascanner, "AUDIO_ROOT", root_path)
    db = temp_audio_db
    # 根目录歌单（自身有 1 首直接曲目）+ 2 个顶层子歌单
    root = _add_playlist(db, "音频根目录", root_path, parent_path="")
    top1 = _add_playlist(db, "顶层1", os.path.normpath("/fake/root/top1"), parent_path="")
    top2 = _add_playlist(db, "顶层2", os.path.normpath("/fake/root/top2"), parent_path="")
    _add_tracks(db, root, 1)
    _add_tracks(db, top1, 5)
    _add_tracks(db, top2, 3)

    ascanner._update_all_container_track_counts()

    # 根 = 自身 1 + top1(5) + top2(3) = 9
    assert _get_track_count(db, root) == 9
    assert _get_track_count(db, top1) == 5
    assert _get_track_count(db, top2) == 3


# ==================== dir level 指纹扫描集成测试 ====================

@pytest.fixture
def temp_audio_env(tmp_path, monkeypatch):
    """临时音频扫描环境：真实音频根目录 + 隔离 DB + 隔离指纹缓存"""
    audio_root = tmp_path / "audio_root"
    audio_root.mkdir()
    db_path = str(tmp_path / "test_audio.db")
    monkeypatch.setattr(adb, "DB_PATH", db_path)
    adb.end_persistent()
    adb.init_db()
    cache_path = str(tmp_path / ".scan_fingerprint.json")
    monkeypatch.setattr(fp, "_FINGERPRINT_PATH", cache_path)
    monkeypatch.setattr(ascanner, "AUDIO_ROOT", str(audio_root))
    yield str(audio_root)
    adb.end_persistent()


def _make_audio_file(root, relpath, content=b"fake"):
    """在临时音频根目录下创建一个假音频文件"""
    path = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return path


def _bump_dir_mtime(path, delta=10):
    """显式 bump 目录 mtime，避免文件系统精度导致测试不稳定"""
    st = os.stat(path)
    os.utime(path, (st.st_mtime + delta, st.st_mtime + delta))


def test_scan_unchanged_with_dir_level(temp_audio_env):
    """dir level: 建立指纹后再次扫描应返回 unchanged 且跳过"""
    root = temp_audio_env
    _make_audio_file(root, "歌单1/01.mp3")
    _make_audio_file(root, "歌单2/01.flac")

    # 第一次扫描：同步 DB + 建立 dir 指纹
    stats = ascanner.scan(audio_root=root)
    assert not stats.get("unchanged")
    assert stats["added_playlists"] == 2

    # 第二次扫描：dir 指纹未变，应跳过
    stats2 = ascanner.scan(audio_root=root)
    assert stats2.get("unchanged") is True
    assert stats2.get("skipped_all") is True


def test_scan_changed_after_adding_audio_file(temp_audio_env):
    """dir level: 建立指纹后新增音频文件，scan 应检测到变化并同步 DB"""
    root = temp_audio_env
    _make_audio_file(root, "歌单1/01.mp3")
    ascanner.scan(audio_root=root)  # 建立指纹

    # 新增音频文件 + bump 父目录 mtime 确保指纹变化
    _make_audio_file(root, "歌单1/02.mp3")
    _bump_dir_mtime(os.path.join(root, "歌单1"))

    stats = ascanner.scan(audio_root=root)
    assert not stats.get("unchanged")
    assert stats["added_tracks"] == 1  # 新增 1 首


def test_scan_changed_after_adding_playlist_folder(temp_audio_env):
    """dir level: 建立指纹后新增歌单文件夹，scan 应检测到并创建歌单"""
    root = temp_audio_env
    _make_audio_file(root, "歌单1/01.mp3")
    ascanner.scan(audio_root=root)  # 建立指纹

    # 新增一个歌单文件夹（含音频）
    _make_audio_file(root, "歌单2/01.mp3")
    _bump_dir_mtime(root)  # root 下新增子目录，bump root mtime

    stats = ascanner.scan(audio_root=root)
    assert not stats.get("unchanged")
    assert stats["added_playlists"] == 1
