"""
播放控制器单元测试（离屏，不初始化音频设备）

覆盖：
    - 队列设置不碰播放器（延迟初始化）
    - 上一首/下一首环绕
    - 列表循环 / 单曲循环（自动结束重播、手动切歌仍前进）/ 随机不重复当前曲
    - 文件缺失只发错误信号，不进入播放
    - 音量夹紧且不重复发信号
    - 音频格式白名单与新格式时长解析
    - 静态守卫：音频库窗口不得再自己持有 QMediaPlayer
"""
import os
import struct

import pytest
from PyQt6.QtWidgets import QApplication  # noqa: E402

from audio_manager import duration as aduration
from audio_manager import scanner as ascanner
from ui.playback_controller import (
    REPEAT_ONE,
    REPEAT_SHUFFLE,
    PlaybackController,
    get_playback_controller,
    reset_playback_controller,
)


class FakePlayer:
    """只实现控制器用到的最小接口，替代真实 QMediaPlayer"""

    def __init__(self):
        self.state = 0        # 0=停止 1=播放 2=暂停
        self.position_ms = 0
        self.sources = []
        self.play_calls = 0

    def setSource(self, url):
        self.sources.append(url.toLocalFile())

    def play(self):
        self.state = 1
        self.play_calls += 1

    def pause(self):
        self.state = 2

    def stop(self):
        self.state = 0

    def playbackState(self):
        return self.state

    def setPosition(self, ms):
        self.position_ms = ms

    def position(self):
        return self.position_ms

    def duration(self):
        return 0


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def controller():
    ctl = PlaybackController()
    ctl._player = FakePlayer()  # 注入假播放器，避免初始化真实音频设备
    yield ctl
    reset_playback_controller()


def _tracks(tmp_path, count=3):
    tracks = []
    for i in range(count):
        path = tmp_path / f"{i}.mp3"
        path.write_bytes(b"")
        tracks.append({"title": f"曲{i}", "path": str(path), "playlist_id": 1})
    return tracks


def test_set_queue_keeps_player_lazy(tmp_path):
    """只设队列不播放：不得创建 QMediaPlayer（窗口启动不该初始化音频设备）"""
    ctl = PlaybackController()
    ctl.set_queue(_tracks(tmp_path))
    assert ctl._player is None
    assert ctl.current_index == -1


def test_play_and_navigation_wrap(controller, tmp_path, qapp):
    tracks = _tracks(tmp_path)
    assert controller.play_tracks(tracks, 1) is True
    assert controller.current_index == 1
    assert controller.current_track["title"] == "曲1"

    controller.next()
    assert controller.current_index == 2
    controller.next()
    assert controller.current_index == 0, "列表循环应回到队首"
    controller.previous()
    assert controller.current_index == 2, "队首上一首应回到队尾"


def test_repeat_one_replays_on_auto_but_advances_manually(controller, tmp_path, qapp):
    tracks = _tracks(tmp_path)
    controller.play_tracks(tracks, 0)
    controller.set_repeat_mode(REPEAT_ONE)

    controller.next(auto=True)  # 播放结束触发
    assert controller.current_index == 0, "单曲循环下播放结束应重播当前曲"
    controller.next(auto=False)  # 用户点下一首
    assert controller.current_index == 1, "手动切歌不受单曲循环限制"


def test_shuffle_does_not_pick_current(controller, tmp_path, qapp):
    tracks = _tracks(tmp_path)
    controller.play_tracks(tracks, 1)
    controller.set_repeat_mode(REPEAT_SHUFFLE)
    for _ in range(20):
        previous = controller.current_index
        controller.next()
        assert controller.current_index != previous, "随机播放不应连续两次选中同一首"


def test_missing_file_emits_error(controller, tmp_path, qapp):
    errors = []
    controller.errorOccurred.connect(errors.append)
    assert controller.play_tracks([{"title": "缺失", "path": str(tmp_path / "nope.mp3")}], 0) is False
    assert errors and "找不到文件" in errors[0]


def test_volume_clamped_and_emits_once(controller, qapp):
    seen = []
    controller.volumeChanged.connect(seen.append)
    controller.set_volume(150)
    controller.set_volume(100)
    controller.set_volume(-5)
    assert controller.volume == 0
    assert seen == [100, 0]


def test_controller_singleton_is_stable(qapp):
    first = get_playback_controller()
    assert get_playback_controller() is first
    reset_playback_controller()
    assert get_playback_controller() is not first


def test_audio_extensions_cover_more_formats():
    expected = {
        ".mp3", ".flac", ".wav", ".aac", ".ogg", ".wma", ".m4a",
        ".opus", ".ape", ".aiff", ".aif", ".mka", ".wv", ".dsf", ".dff",
        ".m4b", ".oga", ".spx", ".ac3", ".amr", ".mp2", ".m4r",
    }
    assert expected <= ascanner.AUDIO_EXTENSIONS


def _extended80(value):
    """把整数采样率编码成 AIFF 的 80 位扩展精度浮点"""
    exponent = value.bit_length() - 1
    mantissa = value << (63 - exponent)
    return struct.pack(">H", 16383 + exponent) + mantissa.to_bytes(8, "big")


def test_aiff_duration_parses_comm_chunk(tmp_path):
    comm_payload = struct.pack(">HIH", 1, 44100, 16) + _extended80(44100)
    comm = b"COMM" + struct.pack(">I", len(comm_payload)) + comm_payload
    body = b"AIFF" + comm
    path = tmp_path / "test.aiff"
    path.write_bytes(b"FORM" + struct.pack(">I", len(body)) + body)

    assert aduration.get_duration(str(path)) == 1


def test_unknown_format_duration_is_zero(tmp_path):
    path = tmp_path / "test.xyz"
    path.write_bytes(b"not audio")
    assert aduration.get_duration(str(path)) == 0


def test_audio_view_no_longer_owns_media_player():
    """静态守卫：播放器必须归 PlaybackController，库窗口不得再直接持有 QMediaPlayer"""
    source = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "audio_view.py"
    )
    with open(source, encoding="utf-8") as f:
        text = f.read()
    assert "QMediaPlayer" not in text, "音频库窗口不应直接使用 QMediaPlayer，应通过播放控制器"
    assert "get_playback_controller" in text
