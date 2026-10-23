"""守卫：字幕高亮必须跟着播放位置走（不能停在第一句）。

用户反馈（2026-10-24）：「播放音频的时候字幕没有自动滚动」，追问后确认
**连高亮都不动，停在第一句** —— 这不是滚动问题，是驱动链断了。

原实现只有一个 100ms 的 QTimer 轮询，而且带 `self.isVisible()` 条件：
一旦窗口在某个时刻 `isVisible()` 为 False（刚 show 尚未映射、或窗口被
隐藏后未重启定时器），字幕就再也不会更新，永远停在载入时那一句。

现在两条路一起走：
1. `PlaybackController.positionChanged` 信号（后端回调，最可靠）
2. 100ms 轮询（补充，去掉 isVisible 判定）
外加「开始播放时自愈定时器」和「字幕卡显示句数」便于自查。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from audio_manager import database as adb  # noqa: E402
from audio_manager import subtitles as subs  # noqa: E402
from ui.playback_controller import PlaybackController  # noqa: E402
from ui.player_window import PlayerWindow  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def temp_audio_db(tmp_path, monkeypatch):
    monkeypatch.setattr(adb, "DB_PATH", str(tmp_path / "audio.db"))
    adb.end_persistent()
    adb.init_db()
    yield adb
    adb.end_persistent()


@pytest.fixture
def window(qapp, temp_audio_db):
    playback = PlaybackController()
    win = PlayerWindow(playback=playback)
    win._animate = False
    yield win, playback
    win.shutdown()


def _track(count=10, step_ms=3000):
    cues = [
        subs.Cue(i * step_ms, i * step_ms + step_ms - 200, f"第 {i + 1} 句")
        for i in range(count)
    ]
    return subs.SubtitleTrack("test.srt", cues, fmt="SRT")


def _bind_track(playback, track, position=0):
    """让播放器认为正在播放某曲目，并把字幕装进字幕区"""
    playback.set_queue([{"title": "测试", "path": "/no/such/file.mp3"}], 0)
    return track


# ==================== 驱动链 ====================

def test_tick_updates_subtitle_even_when_window_hidden(window, monkeypatch):
    """轮询不许依赖 isVisible：窗口隐藏时定时器已停，多这层会漏更新"""
    win, pc = window
    _bind_track(pc, None)
    win.subtitle_view.set_track(_track(), 0)
    assert win.isVisible() is False  # 没 show 过

    monkeypatch.setattr(pc, "position", lambda: 12000)  # 第 5 句（每句 3 秒）
    win._tick_subtitle()
    assert win.subtitle_view._current_row == 4


def test_position_signal_drives_subtitle(window):
    """播放位置信号也要推进字幕（后端回调比轮询可靠）"""
    win, pc = window
    _bind_track(pc, None)
    win.subtitle_view.set_track(_track(), 0)
    win._on_position_changed(9200)   # 第 4 句
    assert win.subtitle_view._current_row == 3


def test_playing_state_restarts_stalled_timer(window):
    """定时器被停掉后（如 hide 过），重新开始播放应自愈"""
    win, pc = window
    win._subtitle_timer.stop()
    assert win._subtitle_timer.isActive() is False

    win.show()
    try:
        win._on_state_changed(True)
        assert win._subtitle_timer.isActive() is True
    finally:
        win.hide()


def test_tick_does_nothing_without_current_track(window, monkeypatch):
    """没有当前曲目时不乱更新（避免用了上一首的位置）"""
    win, pc = window
    win.subtitle_view.set_track(_track(), 0)
    before = win.subtitle_view._current_row

    monkeypatch.setattr(pc, "position", lambda: 21000)
    pc.set_queue([], -1)
    win._tick_subtitle()
    assert win.subtitle_view._current_row == before


# ==================== 高亮与滚动 ====================

def test_position_advances_row_and_scroll(window):
    """连续推进位置：行号递增、滚动条跟着下移（真实"自动滚动"）"""
    win, pc = window
    win.show()
    try:
        sv = win.subtitle_view
        sv.resize(320, 200)
        sv.set_track(_track(40), 0)
        bar = sv.list.verticalScrollBar()
        seen = []
        for ms in (0, 12000, 30000, 60000, 90000):
            sv.set_position(ms)
            QApplication.processEvents()
            seen.append((sv._current_row, bar.value()))

        rows = [row for row, _ in seen]
        assert rows == sorted(rows), f"行号应单调前进，实际 {rows}"
        assert seen[-1][1] > seen[0][1], "滚动条应随高亮下移"
    finally:
        win.hide()


def test_cue_gap_keeps_previous_highlight(window):
    """句间间隙保持上一句（不闪回无高亮）"""
    win, _pc = window
    sv = win.subtitle_view
    track = subs.SubtitleTrack("t.srt", [
        subs.Cue(0, 1000, "第一句"),
        subs.Cue(5000, 6000, "第二句"),
    ], fmt="SRT")
    sv.set_track(track, 500)
    assert sv._current_row == 0
    sv.set_position(3000)          # 落在 1000~5000 的间隙里
    assert sv._current_row == 0    # 保持第一句


# ==================== 字幕卡自述信息 ====================

def test_file_label_shows_cue_count(window):
    """标题带句数：一眼分清「没加载」和「加载了没跟进」"""
    win, _pc = window
    win.subtitle_view.set_track(_track(37), 0)
    text = win.subtitle_view.file_label.text()
    assert "37 句" in text


def test_file_label_says_not_found(window):
    win, _pc = window
    win.subtitle_view.set_track(None, 0)
    assert "未找到字幕" in win.subtitle_view.file_label.text()


# ==================== 时间轴兜底 ====================

def test_track_sorts_unordered_cues():
    """乱序字幕会让二分返回错误下标（高亮卡住），构造时必须排好序"""
    cues = [
        subs.Cue(9000, 9500, "第三句"),
        subs.Cue(1000, 1500, "第一句"),
        subs.Cue(5000, 5500, "第二句"),
    ]
    track = subs.SubtitleTrack("t.srt", cues, fmt="SRT")
    assert [c.text for c in track.cues] == ["第一句", "第二句", "第三句"]
    assert track.index_at(5500) == 1
    assert track.index_at(9200) == 2


def test_tick_source_has_no_isvisible_guard():
    """静态兜底：轮询里不许再出现 isVisible 判断"""
    import pathlib

    source = pathlib.Path(__file__).with_name("..").resolve()
    body = (source / "ui" / "player_window.py").read_text(encoding="utf-8")
    tick = body.split("def _tick_subtitle", 1)[1].split("\n    def ", 1)[0]
    assert "self.isVisible()" not in tick, "轮询不能靠窗口可见性决定是否更新字幕"
