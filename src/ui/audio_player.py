"""
音频模块播放条控件
固定于主窗口底部的播放控制栏：当前曲目、上一首/播放/下一首、进度条、时间、音量
"""
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSlider, QStyle, QStyleOptionSlider,
)
from PyQt6.QtCore import Qt, pyqtSignal

from ui.audio_theme import (
    ACCENT, ACCENT_HOVER, TEXT_PRIMARY, TEXT_DIM, format_time,
)


class SeekSlider(QSlider):
    """可点击跳转的进度条：点击轨道任意位置直接跳到该位置，拖动 / 点击后拖动均可用"""

    clickedSeek = pyqtSignal(int)  # 点击跳转，单位：秒

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self._seeking = False  # 当前是否处于"点击轨道"模式

    def _value_from_pos(self, x: float) -> int:
        """把鼠标横坐标换算为滑块值（秒）"""
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), int(x), self.width())

    def mousePressEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and self.maximum() > 0
                and not self._on_handle(event.position().toPoint())):
            # 点击在轨道上 → 直接跳到点击位置；不调用 super()，避免默认 pageStep 逻辑覆盖
            self._seeking = True
            self.setValue(self._value_from_pos(event.position().x()))
            self.clickedSeek.emit(self.value())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # 点击轨道后按住拖动：持续跟随鼠标位置跳转
        if self._seeking and (event.buttons() & Qt.MouseButton.LeftButton):
            self.setValue(self._value_from_pos(event.position().x()))
            self.clickedSeek.emit(self.value())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._seeking = False
        super().mouseReleaseEvent(event)

    def _on_handle(self, pos) -> bool:
        """判断点击点是否落在滑块 handle 上（是则走原生拖动逻辑）"""
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, opt,
            QStyle.SubControl.SC_SliderHandle, self)
        return rect.contains(pos)


class ClickableLabel(QLabel):
    """可点击的文本标签：左键点击发出 clicked 信号"""

    clicked = pyqtSignal()

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self._clickable = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_clickable(self, enabled: bool):
        """切换可点击状态：控制手型光标与 tooltip 反馈"""
        self._clickable = enabled
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor)
        self.setToolTip("点击打开所属歌单" if enabled else "")

    def mousePressEvent(self, event):
        if self._clickable and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class AudioPlayerBar(QFrame):
    """底部播放条：发出控制信号，由主窗口连接 QMediaPlayer"""

    prevClicked = pyqtSignal()
    toggleClicked = pyqtSignal()
    nextClicked = pyqtSignal()
    seekRequested = pyqtSignal(int)  # 秒
    volumeChanged = pyqtSignal(int)  # 0-100
    nowPlayingClicked = pyqtSignal()  # 点击当前曲目名（跳转所属歌单）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(56)
        self.setStyleSheet("""
            QFrame { background-color: #EDE6DA; border-top: 1px solid #EDE6DA; }
        """)
        player_layout = QHBoxLayout(self)
        player_layout.setContentsMargins(12, 6, 12, 6)
        player_layout.setSpacing(8)

        self.now_playing = ClickableLabel("未播放")
        self.now_playing.setMinimumWidth(160)
        self.now_playing.setStyleSheet(f"""
            color: #777; font-size: 12px; padding: 2px 4px; border-radius: 4px;
            QLabel:hover {{ color: {TEXT_PRIMARY}; background-color: rgba(66, 180, 194, 0.15); }}
        """)
        self.now_playing.set_clickable(False)
        self.now_playing.clicked.connect(self.nowPlayingClicked.emit)
        player_layout.addWidget(self.now_playing)

        self.prev_btn = QPushButton("⏮")
        self.prev_btn.setFixedSize(32, 28)
        self.prev_btn.setStyleSheet(
            "background: transparent; color: #BBB; border: none; font-size: 16px; padding: 0; "
            f"QPushButton:hover {{ color: {TEXT_PRIMARY}; }}"
        )
        self.prev_btn.clicked.connect(self.prevClicked.emit)
        player_layout.addWidget(self.prev_btn)

        self.play_btn = QPushButton("▶")
        self.play_btn.setFixedSize(36, 32)
        self.play_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {ACCENT}; color: #fff; border: none;
                border-radius: 16px; font-size: 14px; padding: 0;
            }}
            QPushButton:hover {{ background-color: {ACCENT_HOVER}; }}
        """)
        self.play_btn.clicked.connect(self.toggleClicked.emit)
        player_layout.addWidget(self.play_btn)

        self.next_btn = QPushButton("⏭")
        self.next_btn.setFixedSize(32, 28)
        self.next_btn.setStyleSheet(
            "background: transparent; color: #BBB; border: none; font-size: 16px; padding: 0; "
            f"QPushButton:hover {{ color: {TEXT_PRIMARY}; }}"
        )
        self.next_btn.clicked.connect(self.nextClicked.emit)
        player_layout.addWidget(self.next_btn)

        self.time_current = QLabel("00:00")
        self.time_current.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")
        player_layout.addWidget(self.time_current)

        self.progress_slider = SeekSlider(Qt.Orientation.Horizontal)
        self.progress_slider.setFixedHeight(24)
        self.progress_slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{ background: #DDD; height: 4px; border-radius: 2px; }}
            QSlider::handle:horizontal {{ background: {ACCENT}; width: 12px; height: 12px;
                margin: -4px 0; border-radius: 6px; }}
            QSlider::sub-page:horizontal {{ background: {ACCENT}; height: 4px; border-radius: 2px; }}
        """)
        self.progress_slider.setRange(0, 0)
        self.progress_slider.clickedSeek.connect(self._emit_seek)
        self.progress_slider.sliderMoved.connect(self._emit_seek)
        self.progress_slider.sliderReleased.connect(self._emit_seek_on_release)
        player_layout.addWidget(self.progress_slider, 1)

        self.time_total = QLabel("00:00")
        self.time_total.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")
        player_layout.addWidget(self.time_total)

        volume_btn = QPushButton("🔊")
        volume_btn.setFixedSize(28, 28)
        volume_btn.setStyleSheet("background: transparent; color: #BBB; border: none;")
        player_layout.addWidget(volume_btn)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setFixedWidth(80)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setStyleSheet(self.progress_slider.styleSheet())
        self.volume_slider.valueChanged.connect(self.volumeChanged.emit)
        player_layout.addWidget(self.volume_slider)

    # ── 内部信号桥 ──

    def _emit_seek(self, pos):
        self.seekRequested.emit(pos)

    def _emit_seek_on_release(self):
        self.seekRequested.emit(self.progress_slider.value())

    # ── 对外状态更新接口 ──

    def set_playing(self, playing: bool):
        """同步播放按钮图标"""
        self.play_btn.setText("⏸" if playing else "▶")

    def set_now_playing(self, text: str):
        """设置当前曲目文本；播放中可点击跳转所属歌单，未播放时禁用"""
        self.now_playing.setText(text)
        self.now_playing.set_clickable(bool(text) and text != "未播放")

    def set_duration(self, seconds: int):
        """设置进度条总长与总时长文本"""
        self.progress_slider.setRange(0, seconds)
        self.time_total.setText(format_time(seconds))

    def set_position(self, seconds: int):
        """更新当前进度（拖动时由主窗口自行处理跳转，避免互相覆盖）"""
        self.time_current.setText(format_time(seconds))
        if not self.progress_slider.isSliderDown():
            self.progress_slider.setValue(seconds)