"""
音频模块「筛选视图返回」导航回归测试（离屏，无需显示器）

覆盖缺陷场景：
    从歌单详情页点击标签 / 社团胶囊进入筛选视图后——
    1) 列表页没有任何返回入口（返回按钮只认目录层级，根目录下直接隐藏）；
    2) 擦除筛选芯片不会回到来源歌单，而是退化成"全部歌单列表"。

正确行为：筛选视图记住来源歌单，返回按钮指向它；擦光筛选条件视为退出筛选视图，
同样回到来源歌单（若筛选是浏览器内自行发起的，则只清筛选、不跳页）。

用离屏 QApplication + monkeypatch 数据层验证纯导航行为，
不依赖真实数据库、真实音频文件与网络。
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 必须在导入 Qt 之前设置

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from audio_manager import database as db  # noqa: E402
from audio_manager import dlsite_db  # noqa: E402
from ui.audio_browser import PlaylistBrowser  # noqa: E402
from ui.audio_view import AudioMainWindow  # noqa: E402

# 歌单A: 标签1/标签2    歌单B: 标签2/标签3    子歌单A1 属于 歌单A
PLAYLISTS = [
    {"id": 1, "name": "歌单A", "path": r"X:\A", "parent_path": "", "track_count": 2,
     "tags": "标签1，标签2", "mtime": 1, "cover": None},
    {"id": 2, "name": "歌单B", "path": r"X:\B", "parent_path": "", "track_count": 1,
     "tags": "标签2，标签3", "mtime": 2, "cover": None},
    {"id": 3, "name": "子歌单A1", "path": r"X:\A\子歌单A1", "parent_path": r"X:\A", "track_count": 1,
     "tags": "", "mtime": 3, "cover": None},
]
TRACKS = {
    1: [{"id": 11, "title": "曲1", "path": r"X:\A\1.mp3", "duration": 100, "playlist_id": 1}],
    3: [{"id": 12, "title": "曲2", "path": r"X:\A\子歌单A1\2.mp3", "duration": 200, "playlist_id": 3}],
}
BY_ID = {pl["id"]: pl for pl in PLAYLISTS}


@pytest.fixture(scope="session")
def qapp():
    """离屏 QApplication（整个测试会话共用一个实例）"""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def patched_db(monkeypatch):
    """用内存假数据替换数据层，隔离真实数据库与文件系统"""
    monkeypatch.setattr(db, "init_db", lambda: None)
    monkeypatch.setattr(db, "list_playlists_by_parent",
                        lambda p: [pl for pl in PLAYLISTS if (pl["parent_path"] or "") == (p or "")])
    monkeypatch.setattr(db, "count_tracks_by_playlist", lambda: {1: 1, 2: 1, 3: 1})
    monkeypatch.setattr(db, "get_child_counts_batch",
                        lambda paths: {p: 1 for p in paths if p == r"X:\A"})
    monkeypatch.setattr(db, "get_playlist", lambda pl_id: BY_ID.get(pl_id))
    monkeypatch.setattr(db, "get_playlist_by_path",
                        lambda p: next((pl for pl in PLAYLISTS if pl["path"] == p), None))
    monkeypatch.setattr(db, "get_descendant_playlists",
                        lambda p: [pl for pl in PLAYLISTS if pl["path"].startswith(p + "\\")])
    monkeypatch.setattr(db, "list_tracks", lambda pl_id: TRACKS.get(pl_id, []))
    monkeypatch.setattr(db, "list_tracks_by_playlist_ids",
                        lambda ids: [t for i in ids for t in TRACKS.get(i, [])])
    monkeypatch.setattr(db, "list_playlists", lambda: PLAYLISTS)
    monkeypatch.setattr(dlsite_db, "init_db", lambda: None)
    monkeypatch.setattr(dlsite_db, "get_work", lambda rj: None)
    monkeypatch.setattr(dlsite_db, "get_cover_map", lambda: {})
    monkeypatch.setattr(dlsite_db, "get_genre_map", lambda: {})
    monkeypatch.setattr(dlsite_db, "find_rjs_by", lambda circles=None, cvs=None: set())


def _card_count(browser):
    """当前网格视图渲染出的歌单卡片数"""
    return browser.flow.count()


# ─────────────────────── 浏览器层 ───────────────────────

@pytest.fixture
def browser(qapp, patched_db):
    br = PlaylistBrowser()
    br.resize(1000, 600)
    br.show()
    qapp.processEvents()
    br.reset()
    qapp.processEvents()
    yield br
    br.close()


def test_no_back_entry_without_filter(browser):
    """初始（根目录、无筛选）：不应出现返回按钮"""
    assert _card_count(browser) == 2
    assert browser.back_btn.isHidden()


def test_back_button_returns_to_origin_playlist(browser, qapp):
    """详情页发起筛选后：出现指向来源歌单的返回按钮，点击即回到该歌单"""
    returned = []
    browser.returnToPlaylist.connect(returned.append)

    browser.set_return_target(1, "歌单A")
    browser.filter_by_tag("标签1")
    qapp.processEvents()

    assert browser.back_btn.text() == "← 返回 歌单A"
    assert not browser.back_btn.isHidden()
    assert _card_count(browser) == 1          # 筛选生效：只剩歌单A

    browser.back_btn.click()
    qapp.processEvents()

    assert returned == [1]                    # 发出返回来源歌单的请求
    assert browser.back_btn.isHidden()        # 返回目标已消费
    assert _card_count(browser) == 2          # 筛选已清理，列表还原


def test_removing_last_chip_returns_to_origin_playlist(browser, qapp):
    """擦除最后一个筛选芯片 = 退出筛选视图，应回到来源歌单"""
    returned = []
    browser.returnToPlaylist.connect(returned.append)

    browser.set_return_target(2, "歌单B")
    browser.filter_by_tag("标签3")
    qapp.processEvents()
    assert _card_count(browser) == 1

    browser._on_tag_removed("标签3")           # 芯片 × 按钮走这条路径
    qapp.processEvents()

    assert returned == [2]
    assert browser.back_btn.isHidden()
    assert _card_count(browser) == 2


def test_chip_removal_without_origin_does_not_jump(browser, qapp):
    """回归：浏览器内自行筛选时，擦除芯片只清筛选、不得触发跳页"""
    returned = []
    browser.returnToPlaylist.connect(returned.append)

    browser.filter_by_tag("标签1")
    browser._on_tag_removed("标签1")
    qapp.processEvents()

    assert returned == []
    assert _card_count(browser) == 2


def test_clear_tags_drops_return_target(browser, qapp):
    """回归：clear_tags（扫描刷新/reload 路径）不得触发返回"""
    returned = []
    browser.returnToPlaylist.connect(returned.append)

    browser.set_return_target(1, "歌单A")
    browser.filter_by_tag("标签1")
    browser.clear_tags()
    qapp.processEvents()

    assert returned == []
    assert browser.back_btn.isHidden()


# ─────────────────────── 主窗口层 ───────────────────────

@pytest.fixture
def window(qapp, patched_db, monkeypatch):
    """主窗口：屏蔽自动扫描与后台线程，只验证页面/导航状态"""
    monkeypatch.setattr(AudioMainWindow, "start_scan", lambda self: None)
    monkeypatch.setattr(AudioMainWindow, "_ensure_dlsite_thread", lambda self: None)
    win = AudioMainWindow()
    win.resize(1000, 600)
    win.show()
    qapp.processEvents()
    yield win
    win.close()
    qapp.processEvents()


def test_detail_tag_click_round_trip(window, qapp):
    """详情页点标签 → 筛选视图 → 返回：回到原歌单详情页，且筛选状态被清理"""
    def page():
        return window.page_stack.currentIndex()

    window._open_playlist(1)
    qapp.processEvents()
    assert page() == 1
    assert window.detail.playlist_title.text() == "歌单A"
    assert window.detail.detail_back_btn.text() == "← 返回歌单列表"

    window._on_tag_clicked("标签1")
    qapp.processEvents()
    assert page() == 0
    assert window._filter_origin == [1]
    assert window.browser.back_btn.text() == "← 返回 歌单A"
    assert _card_count(window.browser) == 1

    window.browser.back_btn.click()
    qapp.processEvents()
    assert page() == 1
    assert window.detail.playlist_title.text() == "歌单A"
    assert window.browser._return_pl_id is None
    assert not window.browser._active_tags
    assert _card_count(window.browser) == 2


def test_sub_playlist_history_is_restored(window, qapp):
    """子歌单详情页发起筛选：返回应还原「根→子」完整层级，而非掉回列表"""
    window._open_playlist(3, history=[1, 3])
    qapp.processEvents()
    assert window.detail.playlist_title.text() == "子歌单A1"
    assert window.detail.detail_back_btn.text() == "← 返回上级"

    window._on_tag_clicked("标签1")
    qapp.processEvents()
    assert window._filter_origin == [1, 3]
    assert window.browser.back_btn.text() == "← 返回 子歌单A1"

    window.browser.back_btn.click()
    qapp.processEvents()
    assert window.detail.playlist_title.text() == "子歌单A1"
    assert window._detail_history == [1, 3]
    assert window.detail.detail_back_btn.text() == "← 返回上级"

    # 详情页自身的返回链未被破坏
    window._detail_back()
    qapp.processEvents()
    assert window.detail.playlist_title.text() == "歌单A"
    window._detail_back()
    qapp.processEvents()
    assert window.page_stack.currentIndex() == 0


def test_dlsite_capsule_click_round_trip(window, qapp):
    """社团/CV 胶囊与标签共用同一套返回逻辑（擦掉胶囊即回到来源歌单）"""
    window._open_playlist(1)
    qapp.processEvents()
    window._on_dlsite_field_clicked("circle", "某社团")
    qapp.processEvents()
    assert window.page_stack.currentIndex() == 0
    assert window._filter_origin == [1]

    window.browser._on_dlsite_chip_removed("circle", "某社团")
    qapp.processEvents()
    assert window.page_stack.currentIndex() == 1
    assert window.detail.playlist_title.text() == "歌单A"
