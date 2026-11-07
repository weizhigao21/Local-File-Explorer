"""「同一个音频反复打开」回归守卫

用户反馈：正在播放 A 时再从资源管理器打开 A，播放器会重建队列、从头播一遍，
窗口被重新顶起来；连着双击还可能开出两个播放窗口。期望是**不重复打开**，
只把已经开着的窗口提到前面并闪一轮强调色提示。

本项目里「打开音频」有四条入口：命令行/文件关联（main.py）、窗口内「打开
文件…」、拖放、以及单实例转发的二次启动。全部收敛到 PlayerWindow.open_tracks()。
"""
import os
import sys
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtMultimedia import QMediaPlayer  # noqa: E402
from PyQt6.QtNetwork import QLocalServer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import app_instance  # noqa: E402
from app_instance import SingleInstance  # noqa: E402
from audio_manager import database as adb  # noqa: E402
from ui.playback_controller import PlaybackController  # noqa: E402
from ui.player_window import PlayerWindow  # noqa: E402

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="module")
def shared(qapp, tmp_path_factory):
    """整份文件共用一个 PlayerWindow：离屏平台反复创建/销毁控件容易踩原生崩溃"""
    tmp = tmp_path_factory.mktemp("dup-open")
    mp = pytest.MonkeyPatch()
    mp.setattr(adb, "DB_PATH", str(tmp / "audio.db"))
    adb.end_persistent()
    adb.init_db()
    playback = PlaybackController()
    win = PlayerWindow(playback=playback)
    win._animate = False
    yield win, playback
    win.shutdown()
    adb.end_persistent()
    mp.undo()


@pytest.fixture
def scene(shared, monkeypatch):
    win, playback = shared
    win._stop_attention()
    win.hide()
    playback._queue = []
    playback._index = -1
    yield win, playback, monkeypatch
    win._stop_attention()
    win.hide()


def _track(folder, name):
    return {"title": name, "path": str(os.path.join(str(folder), name)),
            "playlist_id": None}


def _stub_play_tracks(playback, monkeypatch, calls, active=True, success=True):
    """只模拟控制器的队列状态，绝不真的初始化音频设备"""
    monkeypatch.setattr(type(playback), "is_active", property(lambda _self: active))

    def fake(tracks, index=0):
        calls.append(list(tracks))
        if success:
            playback._queue = list(tracks)
            playback._index = index
        return success

    monkeypatch.setattr(playback, "play_tracks", fake)


# ==================== 去重与高亮 ====================

def test_track_key_normalizes_case_and_relative_parts(tmp_path):
    a = _track(tmp_path, "a.mp3")
    mixed = {"title": "a", "path": a["path"].upper(), "playlist_id": None}
    dotted = {"title": "a", "path": os.path.join(str(tmp_path), ".", "a.mp3"),
              "playlist_id": None}
    key = PlayerWindow.track_key([a])
    assert key == PlayerWindow.track_key([mixed]) == PlayerWindow.track_key([dotted])
    assert key != PlayerWindow.track_key([_track(tmp_path, "b.mp3")])
    assert PlayerWindow.track_key([]) == ()
    assert PlayerWindow.track_key([{"title": "无路径"}]) == ()


def test_opening_same_audio_only_highlights(scene, tmp_path, qapp):
    win, playback, monkeypatch = scene
    calls = []
    _stub_play_tracks(playback, monkeypatch, calls)
    a = _track(tmp_path, "a.mp3")

    assert win.open_tracks([a]) is True
    assert len(calls) == 1
    assert not win._attention_on, "首次打开不该闪「已经开着」"

    again = {"title": "a", "path": os.path.join(str(tmp_path), ".", "a.mp3"),
             "playlist_id": None}
    assert win.open_tracks([again]) is True
    assert len(calls) == 1, "同一个音频再次打开绝不能重建队列 / 从头播放"
    assert win.isVisible() and win._attention_on, "要把已打开的窗口提到前面并显色提示"
    qapp.processEvents()
    assert win._attention.isVisible()
    assert win._attention_timer.isActive()


def test_different_audio_still_replaces_queue(scene, tmp_path):
    win, playback, monkeypatch = scene
    calls = []
    _stub_play_tracks(playback, monkeypatch, calls)

    win.open_tracks([_track(tmp_path, "a.mp3")])
    win.open_tracks([_track(tmp_path, "b.mp3")])
    assert len(calls) == 2, "换一首必须照常开播，不能被去重误挡"
    assert not win._attention_on


def test_inactive_playback_is_not_treated_as_already_open(scene, tmp_path):
    """clear()/stop() 之后旧队列还在，这时再打开应允许重新播放"""
    win, playback, monkeypatch = scene
    calls = []
    _stub_play_tracks(playback, monkeypatch, calls, active=False)

    a = _track(tmp_path, "a.mp3")
    win.open_tracks([a])
    win.open_tracks([a])
    assert len(calls) == 2


def test_failed_play_allows_retry(scene, tmp_path):
    """文件缺失导致 play_tracks 失败时不能把这份音频记成「已打开」"""
    win, playback, monkeypatch = scene
    calls = []
    _stub_play_tracks(playback, monkeypatch, calls, success=False)

    a = _track(tmp_path, "missing.mp3")
    win.open_tracks([a])
    win.open_tracks([a])
    assert len(calls) == 2


def test_is_active_covers_playing_and_paused():
    playback = PlaybackController()
    assert playback.is_active is False

    class Stub:
        def __init__(self, state):
            self._state = state

        def playbackState(self):
            return self._state

    states = QMediaPlayer.PlaybackState
    for state, expected in ((states.PlayingState, True),
                            (states.PausedState, True),
                            (states.StoppedState, False)):
        playback._player = Stub(state)
        assert playback.is_active is expected
    playback._player = None


# ==================== 高亮层本身 ====================

def test_attention_overlay_is_transparent_and_auto_hides(scene, qapp):
    win, _playback, _monkeypatch = scene
    assert win._attention.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert "#attentionOverlay" in win._attention.styleSheet(), "必须用选择器，防止级联到提示条"
    assert "rgba(" not in win._attention.styleSheet()
    assert "#attentionToast" in win._attention_toast.styleSheet()
    assert win._attention_toast.parent() is win._attention

    win.resize(760, 480)
    qapp.processEvents()
    assert win._attention.geometry() == win.centralWidget().rect()
    assert 0 <= win._attention_toast.x() <= win.centralWidget().width()

    win.flash_attention()
    assert win._attention_on
    for _ in range(20):          # 手动推进 QTimer，避免测试里等真实 1 秒
        win._pulse_attention()
    assert not win._attention_on
    assert not win._attention.isVisible(), "提示结束必须完整复原，不能留一圈赤陶边框"
    assert not win._attention_timer.isActive()


# ==================== 入口收敛（静态守卫）====================

def test_all_open_entrypoints_go_through_open_tracks():
    with open(os.path.join(SRC, "main.py"), encoding="utf-8") as f:
        main_src = f.read()
    with open(os.path.join(SRC, "ui", "player_window.py"), encoding="utf-8") as f:
        pw_src = f.read()

    assert "player.open_tracks(tracks_from_paths(files))" in main_src
    assert "playback.play_tracks(tracks, 0)" not in main_src, (
        "启动/转发链路不许绕过去重直接重建队列")
    # play_tracks 只允许在 open_tracks 里出现一次，其余入口（打开文件…、拖放）
    # 都必须调用 self.open_tracks；否则又会出现「一个入口去重、另一个入口重开」。
    assert pw_src.count("self.playback.play_tracks(") == 1
    assert pw_src.count("self.open_tracks(") >= 2
    assert "def open_tracks(self, tracks):" in pw_src


# ==================== 单实例竞态 ====================

def test_listen_failure_forwards_instead_of_opening_second_window(monkeypatch):
    """两个冷启动进程抢同一个 named pipe：输的一方必须转发参数并退出

    旧实现在 listen() 失败时「也按主实例继续运行」，于是两个进程都建界面，
    用户连着打开同一个音频就看到两个播放窗。
    """
    calls = []

    def fake_forward(self, retries=1, wait_ms=0):
        calls.append(retries)
        return len(calls) > 1     # 初始探测失败，listen 失败后的重试成功

    class FlakyServer(QLocalServer):
        def listen(self, name):
            return False

    key = f"wb-test-race-{os.getpid()}-{uuid.uuid4().hex}"
    monkeypatch.setattr(app_instance, "_acquire_primary_mutex", lambda _key: (None, True))
    monkeypatch.setattr(SingleInstance, "_forward", fake_forward)
    monkeypatch.setattr(app_instance, "QLocalServer", FlakyServer)

    secondary = SingleInstance(key)
    assert secondary.start() is False
    assert len(calls) == 2 and calls[1] >= 10, (
        "listen 失败后要带重试地把参数转给赢家，而不是自开一个窗口")
    secondary.shutdown()


@pytest.mark.skipif(sys.platform != "win32", reason="命名互斥体只在 Windows 生效")
def test_primary_mutex_is_unique_per_key():
    """命名互斥体按 key 隔离：不同 key 不该互相误判为「已有实例」"""
    key = f"wb-test-mutex-{os.getpid()}-{uuid.uuid4().hex}"
    h1, first1 = app_instance._acquire_primary_mutex(key)
    h2, first2 = app_instance._acquire_primary_mutex(key)
    h3, first3 = app_instance._acquire_primary_mutex(key + "-other")
    try:
        assert first1 is True
        assert first2 is False, "同一 key 的第二个持有者必须被告知已有实例"
        assert first3 is True
    finally:
        for handle in (h1, h2, h3):
            app_instance._close_mutex(handle)
