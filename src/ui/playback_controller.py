"""
音频播放控制器（app 级单例）

把「播放」从音频库窗口里抽出来：队列、当前索引、循环模式、音量、播放设备
都归这里管，界面只是订阅者。独立播放器窗口、底部播放条、迷你模式
都共用同一份播放状态，关掉任意一个窗口都不会断播。

设计边界：
    - 不 import 任何 ui.* 控件，只依赖 QtMultimedia / QtCore，便于离屏测试；
    - QMediaPlayer 延迟创建：仅导入模块或构造控制器不会初始化音频设备，
      避免启动即占用声卡、也避免无音频设备的测试环境报错；
    - 对外只暴露方法与信号，不把 QMediaPlayer 泄漏给调用方。
"""
import os
import random
from contextlib import suppress

from PyQt6.QtCore import QObject, QUrl, pyqtSignal
from PyQt6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaMetaData, QMediaPlayer

# 循环模式（字符串便于直接存配置，不依赖枚举序列化）
REPEAT_LIST = "list"        # 列表循环（默认，播完最后一首回到第一首）
REPEAT_ONE = "one"          # 单曲循环
REPEAT_SHUFFLE = "shuffle"  # 随机播放

REPEAT_MODES = (REPEAT_LIST, REPEAT_ONE, REPEAT_SHUFFLE)
REPEAT_LABELS = {
    REPEAT_LIST: "列表循环",
    REPEAT_ONE: "单曲循环",
    REPEAT_SHUFFLE: "随机播放",
}

DEFAULT_VOLUME = 80


class PlaybackController(QObject):
    """全局唯一的播放状态机

    信号：
        trackChanged(dict)   切歌后发出当前曲目（含 path/title/playlist_id 等）
        positionChanged(int) 播放进度（毫秒）
        durationChanged(int) 总时长（毫秒）
        stateChanged(bool)   True=正在播放，False=暂停/停止
        volumeChanged(int)   音量（0-100）
        queueChanged(list)   播放队列变化（换队列时发出）
        repeatModeChanged(str)
        errorOccurred(str)   文件缺失、解码失败等
    """

    trackChanged = pyqtSignal(dict)
    positionChanged = pyqtSignal(int)
    durationChanged = pyqtSignal(int)
    stateChanged = pyqtSignal(bool)
    volumeChanged = pyqtSignal(int)
    queueChanged = pyqtSignal(list)
    repeatModeChanged = pyqtSignal(str)
    metadataChanged = pyqtSignal(dict)
    playbackRateChanged = pyqtSignal(float)
    errorOccurred = pyqtSignal(str)

    def __init__(self, parent=None, rng=None):
        super().__init__(parent)
        self._player = None          # 延迟创建
        self._output = None
        self._devices = None         # QMediaDevices 需保持存活才能收到设备变化
        self._bound_device_id = None
        self._queue = []             # [{"title", "path", ...}, ...]
        self._index = -1
        self._repeat = REPEAT_LIST
        self._volume = DEFAULT_VOLUME
        self._rate = 1.0
        self._rng = rng or random.Random()

    # ==================== 只读状态 ====================
    @property
    def queue(self):
        """当前播放队列的浅拷贝（外部修改不影响内部队列）"""
        return list(self._queue)

    @property
    def current_index(self):
        return self._index

    @property
    def current_track(self):
        if 0 <= self._index < len(self._queue):
            return self._queue[self._index]
        return None

    @property
    def repeat_mode(self):
        return self._repeat

    @property
    def volume(self):
        return self._volume

    @property
    def playback_rate(self):
        return self._rate

    @property
    def is_playing(self):
        if self._player is None:
            return False
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    # ==================== 播放器（延迟初始化） ====================
    def _ensure_player(self):
        """首次真正播放时才创建 QMediaPlayer / QAudioOutput"""
        if self._player is not None:
            return self._player
        self._player = QMediaPlayer()
        self._output = QAudioOutput()
        self._player.setAudioOutput(self._output)
        self._output.setVolume(self._volume / 100.0)
        self._player.positionChanged.connect(self.positionChanged.emit)
        self._player.durationChanged.connect(self.durationChanged.emit)
        self._player.playbackStateChanged.connect(self._on_playback_state)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.errorOccurred.connect(self._on_player_error)
        self._player.metaDataChanged.connect(self._on_metadata_changed)
        self._player.setPlaybackRate(self._rate)
        # 记录当前绑定设备，并监听系统音频设备变化（切换默认设备后重绑输出）
        self._bound_device_id = QMediaDevices.defaultAudioOutput().id()
        self._devices = QMediaDevices(self)
        self._devices.audioOutputsChanged.connect(self._rebind_audio_output)
        return self._player

    def _rebind_audio_output(self, force=False):
        """把音频输出重绑到系统当前默认播放设备

        Windows 上 Qt 的 WMF/FFmpeg 后端会把音频流绑定到开播时的设备端点，
        不会跟随系统默认设备切换，导致切设备后声音仍发往旧设备（表现为无声）。
        因此设备列表变化或每次开播前显式重绑。注：不能新建 QAudioOutput 替换，
        直接对现有输出 setDevice 即可，播放中重绑不中断。
        """
        if self._player is None or self._output is None:
            return
        default_dev = QMediaDevices.defaultAudioOutput()
        if not force and default_dev.id() == self._bound_device_id:
            return
        self._output.setDevice(default_dev)
        self._bound_device_id = default_dev.id()

    # ==================== 队列与播放控制 ====================
    def set_queue(self, tracks, start_index=-1):
        """替换播放队列（不自动播放）；start_index 指定当前曲目位置"""
        self._queue = list(tracks or [])
        if self._queue and 0 <= start_index < len(self._queue):
            self._index = start_index
        else:
            self._index = -1
        self.queueChanged.emit(list(self._queue))

    def play_tracks(self, tracks, index=0):
        """设置队列并从 index 开始播放（双击曲目 / 点击子歌单曲目走这里）"""
        self.set_queue(tracks, index)
        return self.play_index(index)

    def play_index(self, index):
        """播放队列中指定位置的曲目；文件缺失时发 errorOccurred 并返回 False"""
        if not self._queue:
            return False
        if index < 0 or index >= len(self._queue):
            return False
        track = self._queue[index]
        path = track.get("path") or ""
        if not path or not os.path.exists(path):
            self.errorOccurred.emit(f"找不到文件: {path or '（空路径）'}")
            return False

        player = self._ensure_player()
        self._index = index
        self._rebind_audio_output()  # 开播前校验默认播放设备是否已切换
        player.setSource(QUrl.fromLocalFile(path))
        player.play()
        self.trackChanged.emit(dict(track))
        return True

    def toggle(self):
        """播放 / 暂停；无播放器或已停止时从当前曲目（或第一首）重新开始"""
        if self._player is None:
            if self._index >= 0:
                return self.play_index(self._index)
            return self.play_index(0) if self._queue else False
        state = self._player.playbackState()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        elif state == QMediaPlayer.PlaybackState.PausedState:
            self._player.play()
        else:
            if self._index >= 0:
                return self.play_index(self._index)
            return self.play_index(0) if self._queue else False
        return True

    def next(self, auto=False):
        """下一首；auto=True 表示由「播放结束」触发（单曲循环时重播当前曲）"""
        index = self._next_index(auto=auto)
        if index < 0:
            return False
        return self.play_index(index)

    def previous(self):
        """上一首；播到队首时回到队尾"""
        index = self._previous_index()
        if index < 0:
            return False
        return self.play_index(index)

    def seek(self, position_ms):
        if self._player is None:
            return
        self._player.setPosition(max(0, int(position_ms)))

    def position(self):
        return self._player.position() if self._player is not None else 0

    def duration(self):
        return self._player.duration() if self._player is not None else 0

    def set_volume(self, volume):
        """音量 0-100，越界自动夹紧；值未变化时不重复发信号"""
        volume = max(0, min(100, int(volume)))
        if self._output is not None:
            self._output.setVolume(volume / 100.0)
        if volume != self._volume:
            self._volume = volume
            self.volumeChanged.emit(volume)

    def set_repeat_mode(self, mode):
        if mode not in REPEAT_MODES or mode == self._repeat:
            return
        self._repeat = mode
        self.repeatModeChanged.emit(mode)

    def cycle_repeat_mode(self):
        """按 列表 → 单曲 → 随机 顺序切换，返回新模式"""
        index = REPEAT_MODES.index(self._repeat)
        self.set_repeat_mode(REPEAT_MODES[(index + 1) % len(REPEAT_MODES)])
        return self._repeat

    def set_playback_rate(self, rate):
        """倍速 0.5x - 2.0x；返回实际生效值"""
        rate = max(0.5, min(2.0, float(rate)))
        if self._player is not None:
            self._player.setPlaybackRate(rate)
        if abs(rate - self._rate) > 1e-6:
            self._rate = rate
            self.playbackRateChanged.emit(rate)
        return rate

    def meta_data(self):
        """当前媒体标签（标题/艺术家/专辑）；后端未解析出时返回空串字段"""
        if self._player is None:
            return {}
        data = self._player.metaData()

        def read(key):
            try:
                return data.stringValue(key) or ""
            except (TypeError, RuntimeError):
                return ""

        return {
            "title": read(QMediaMetaData.Key.Title),
            "artist": read(QMediaMetaData.Key.ContributingArtist)
            or read(QMediaMetaData.Key.Author),
            "album": read(QMediaMetaData.Key.AlbumTitle),
        }

    def stop(self):
        if self._player is not None:
            self._player.stop()
        self.stateChanged.emit(False)

    def clear(self):
        """清空队列（不销毁播放器，便于复用）"""
        self.stop()
        self._queue = []
        self._index = -1
        self.queueChanged.emit([])

    def shutdown(self):
        """程序退出时释放媒体资源"""
        self.stop()
        if self._player is not None:
            self._player.setSource(QUrl())
            self._player.deleteLater()
            self._player = None
        if self._output is not None:
            self._output.deleteLater()
            self._output = None

    # ==================== 索引计算（纯逻辑，可单测） ====================
    def _next_index(self, auto=False):
        count = len(self._queue)
        if count == 0:
            return -1
        if auto and self._repeat == REPEAT_ONE:
            return self._index
        if self._repeat == REPEAT_SHUFFLE and count > 1:
            candidates = [i for i in range(count) if i != self._index]
            return self._rng.choice(candidates)
        return (self._index + 1) % count

    def _previous_index(self):
        count = len(self._queue)
        if count == 0:
            return -1
        return (self._index - 1) % count

    # ==================== 播放器回调 ====================
    def _on_playback_state(self, state):
        self.stateChanged.emit(state == QMediaPlayer.PlaybackState.PlayingState)

    def _on_media_status(self, status):
        # 播完自动切下一首（单曲循环在 _next_index 里处理为「重播当前曲」）
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.next(auto=True)

    def _on_player_error(self, error, error_string):
        if error != QMediaPlayer.Error.NoError:
            self.errorOccurred.emit(error_string or "播放失败")

    def _on_metadata_changed(self):
        self.metadataChanged.emit(self.meta_data())


_instance = None


def _is_alive(obj):
    """QObject 的 Python 包装还在、但底层 C++ 对象可能已被销毁（如 QApplication 重建）

    此时访问任何属性都会抛 RuntimeError。用一个轻量属性探测比 try 整段逻辑更安全。
    """
    if obj is None:
        return False
    try:
        obj.objectName()
    except RuntimeError:
        return False
    return True


def get_playback_controller():
    """获取 app 级播放控制器（首次调用时创建）

    若底层对象已被销毁（测试中反复创建 QApplication、或程序重启事件循环），
    自动重建，避免把已析构的对象交给界面。
    """
    global _instance
    if not _is_alive(_instance):
        _instance = PlaybackController()
    return _instance


def reset_playback_controller():
    """释放单例（测试与程序退出时使用）"""
    global _instance
    if _is_alive(_instance):
        with suppress(RuntimeError):
            _instance.shutdown()
            _instance.deleteLater()
    _instance = None
