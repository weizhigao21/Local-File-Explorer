"""页面底色必须自己画，不能露出系统窗口灰。

坑的成因有三层，任意一层漏了都会在屏幕上露出 #F0F0F0（Windows 窗口色）/ #EFEFEF：
1. QMainWindow 的 QSS 管不到 centralWidget；
2. QWidget 子类（PlaylistBrowser / PlaylistDetailPage）写 `background-color` 前必须
   开 WA_StyledBackground，且选择器只能用 objectName（Python 子类的 metaObject
   类名是 QWidget，类名选择器匹配不上）；
3. QScrollArea 的 QSS 只画滚动区自身，viewport 会盖在上面用系统灰。

用户看到的「作品里面的背景太亮了，非常不舒服」就是这三处叠加：一片 #F0F0F0
冷灰夹在米色顶栏和播放条之间。
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QScrollArea  # noqa: E402

from ui.audio_browser import PlaylistBrowser  # noqa: E402
from ui.audio_detail import PlaylistDetailPage  # noqa: E402
from ui.audio_theme import BG_PAGE  # noqa: E402

SYSTEM_GRAYS = {"#F0F0F0", "#EFEFEF"}   # Windows / 离屏平台的默认窗口色


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _hex(img, x, y):
    c = img.pixelColor(x, y)
    return f"#{c.red():02X}{c.green():02X}{c.blue():02X}"


def _assert_no_system_gray(img, points, where):
    for x, y in points:
        color = _hex(img, x, y)
        assert color not in SYSTEM_GRAYS, (
            f"{where} 在 ({x},{y}) 露出系统窗口灰 {color}，说明底色没画上")


def test_detail_page_paints_its_background(app):
    page = PlaylistDetailPage()
    page.resize(900, 600)
    page.show()
    app.processEvents()
    img = page.grab().toImage()

    assert page.testAttribute(Qt.WidgetAttribute.WA_StyledBackground), (
        "QWidget 子类必须开 WA_StyledBackground，否则 QSS 底色不绘制")
    # 四角 + 中部：覆盖工具栏下沿、左侧留白、内容区、底部
    _assert_no_system_gray(img, [(2, 45), (2, 300), (897, 300), (2, 590), (700, 400)],
                           "详情页")
    assert _hex(img, 2, 45) == BG_PAGE
    page.close()


def test_detail_scroll_viewport_is_painted(app):
    """QScrollArea 的 QSS 画不到视口，视口必须单独上色。"""
    page = PlaylistDetailPage()
    page.resize(900, 600)
    page.show()
    app.processEvents()
    scrolls = page.findChildren(QScrollArea)
    assert scrolls, "详情页应有滚动区"
    for scroll in scrolls:
        # 滚到空白处时看到的就是视口自己的底色
        assert BG_PAGE.lower().lstrip("#") in scroll.viewport().styleSheet().lower()
    page.close()


def test_browser_page_paints_its_background(app):
    browser = PlaylistBrowser()
    browser.resize(900, 600)
    browser.show()
    app.processEvents()
    img = browser.grab().toImage()

    assert browser.testAttribute(Qt.WidgetAttribute.WA_StyledBackground)
    _assert_no_system_gray(img, [(2, 300), (897, 300), (450, 560)], "歌单列表页")
    browser.close()


def test_main_window_central_widget_is_painted(app, monkeypatch):
    """QMainWindow 的 QSS 管不到 centralWidget，它必须自己上色。"""
    from audio_manager import database as db
    from audio_manager import dlsite
    from ui.audio_theme import BG_MAIN
    from ui.audio_view import AudioMainWindow

    monkeypatch.setattr(db, "init_db", lambda: None)
    monkeypatch.setattr(db, "list_playlists", lambda: [])
    monkeypatch.setattr(db, "list_playlists_by_parent", lambda parent: [])
    monkeypatch.setattr(db, "count_tracks_by_playlist", lambda: {})
    monkeypatch.setattr(db, "get_child_counts_batch", lambda paths: {})
    monkeypatch.setattr(db, "get_playlist_paths", lambda: [])
    monkeypatch.setattr(dlsite, "AVAILABLE", False)
    monkeypatch.setattr(AudioMainWindow, "start_scan", lambda self: None)

    win = AudioMainWindow()
    central = win.centralWidget()
    assert central is not None
    assert central.objectName() == "audioCentral"
    assert central.testAttribute(Qt.WidgetAttribute.WA_StyledBackground), (
        "centralWidget 不开 WA_StyledBackground 会露出系统窗口灰")
    assert BG_MAIN.lower() in central.styleSheet().lower()

    win.resize(900, 640)
    win.show()
    app.processEvents()
    img = win.grab().toImage()
    _assert_no_system_gray(img, [(2, 300), (897, 300), (450, 620)], "音频主窗口")
    win.close()

