"""
音频模块播放条控件
固定于主窗口底部的播放控制栏：当前曲目、上一首/播放/下一首、进度条、时间、音量
"""
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSlider
from PyQt6.QtCore import Qt, pyqtSignal

from ui.audio_theme import (
    ACCENT, ACCENT_HOVER, TEXT_PRIMARY, TEXT_DIM, format_time,
)


class AudioPlayerBar(QFrame):
    """底部播放条：发出控制信号，由主窗口连接 QMediaPlayer"""

    prevClicked = pyqtSignal()
    toggleClicked = pyqtSignal()
    nextClicked = pyqtSignal()
    seekRequested = pyqtSignal(int)  # 秒
    volumeChanged = pyqtSignal(int)  # 0-100

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(56)
        self.setStyleSheet("""
            QFrame { background-color: #EDE6DA; border-top: 1px solid #EDE6DA; }
        """)
        player_layout = QHBoxLayout(self)
        player_layout.setContentsMargins(12, 6, 12, 6)
        player_layout.setSpacing(8)

        self.now_playing = QLabel("未播放")
        self.now_playing.setMinimumWidth(160)
        self.now_playing.setStyleSheet("color: #777; font-size: 12px;")
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

        self.progress_slider = QSlider(Qt.Orientation.Horizontal)
        self.progress_slider.setFixedHeight(24)
        self.progress_slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{ background: #DDD; height: 4px; border-radius: 2px; }}
            QSlider::handle:horizontal {{ background: {ACCENT}; width: 12px; height: 12px;
                margin: -4px 0; border-radius: 6px; }}
            QSlider::sub-page:horizontal {{ background: {ACCENT}; height: 4px; border-radius: 2px; }}
        """)
        self.progress_slider.setRange(0, 0)
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
        """设置当前曲目文本"""
        self.now_playing.setText(text)

    def set_duration(self, seconds: int):
        """设置进度条总长与总时长文本"""
        self.progress_slider.setRange(0, seconds)
        self.time_total.setText(format_time(seconds))

    def set_position(self, seconds: int):
        """更新当前进度（拖动时由主窗口自行处理跳转，避免互相覆盖）"""
        self.time_current.setText(format_time(seconds))
        if not self.progress_slider.isSliderDown():
            self.progress_slider.setValue(seconds)