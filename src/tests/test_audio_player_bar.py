"""音频模块播放条：「展开 ↗」把当前播放交给独立播放器窗口（离屏）

设计取舍（用户 2026-10-22 决策）：模块内这条播放条只做"随手试听"，双击曲目
不弹窗、不抢焦点；要看字幕 / 排队列 / 切迷你态时，点播放条右端的「展开 ↗」
进大播放器。两条界面绑同一个 PlaybackController，切过去是接着播。
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 必须在导入 Qt 之前设置

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from ui.audio_player import AudioPlayerBar  # noqa: E402
from ui.playback_controller import PlaybackController  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def bar(qapp):
    widget = AudioPlayerBar()
    widget.resize(1000, AudioPlayerBar._BAR_H)
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()


def test_expand_button_is_true_capsule(bar):
    """「展开 ↗」是胶囊：圆角必须取半高，超半高 Qt 会整条退化成直角（坑 #13）"""
    btn = bar.expand_btn
    assert btn.text() == "展开 ↗"
    assert btn.height() == bar._EXPAND_H, "按钮高度被布局拉高了，半高圆角会失效"
    assert f"border-radius: {bar._EXPAND_H // 2}px" in btn.styleSheet(), (
        f"圆角应取半高 {bar._EXPAND_H // 2}px，否则不是胶囊")
    # 纵向 Fixed 才有硬保证（否则窗口变高时被拉伸，圆角比例就错了）
    assert btn.sizePolicy().verticalPolicy().name == "Fixed"


def test_expand_button_emits_expand_clicked(bar):
    hits = []
    bar.expandClicked.connect(lambda: hits.append(1))
    bar.expand_btn.click()
    assert hits == [1], "点击「展开 ↗」应发出 expandClicked"


def test_expand_click_requests_player_window(qapp, monkeypatch):
    """AudioMainWindow 必须把 expandClicked 接到 playerRequested（真正开大播放器）

    直接构造窗口，但把扫描线程 / DLsite 同步 / 歌单刷新全部短路：这些会起后台
    线程或读真实数据库，测试只需要"信号有没有接上"。
    """
    from ui import audio_view

    monkeypatch.setattr(audio_view.db, "init_db", lambda: None)
    monkeypatch.setattr(audio_view.db, "list_playlists_by_parent", lambda p: [])
    monkeypatch.setattr(audio_view.db, "count_tracks_by_playlist", lambda: {})
    monkeypatch.setattr(audio_view.dlsite_db, "init_db", lambda: None)
    monkeypatch.setattr(audio_view, "get_playback_controller",
                        lambda: PlaybackController())
    monkeypatch.setattr(audio_view.AudioMainWindow, "_sync_dlsite_info", lambda self: None)
    monkeypatch.setattr(audio_view.AudioMainWindow, "_refresh_playlists", lambda self: None)
    monkeypatch.setattr(audio_view.AudioMainWindow, "start_scan", lambda self: None)

    win = audio_view.AudioMainWindow()
    try:
        asked = []
        win.playerRequested.connect(lambda: asked.append(1))
        win.player_bar.expand_btn.click()
        qapp.processEvents()
        assert asked == [1], "点「展开 ↗」应请求打开独立播放器窗口"
    finally:
        win.close()
        qapp.processEvents()
