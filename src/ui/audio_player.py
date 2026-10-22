"""
音频模块播放条控件
固定于主窗口底部的播放控制栏：当前曲目、上一首/播放/下一首、进度条、时间、音量
"""
import os
import time

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyleOptionSlider,
)

from ui.audio_theme import (
    ACCENT,
    ACCENT_HOVER,
    ACCENT_TINT,
    BG_MAIN,
    CARD_BG,
    CARD_BORDER,
    INPUT_BG,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    format_time,
)
from ui.media_icons import GlowPlayButton, MediaButton, glyph_icon

# ── 播放条样式（陶色，与播放器窗口同一套观感）────────────────
# 圆角一律 ≤ 半高才呈胶囊：Qt 对超半高的 border-radius 不钳成胶囊，
# 而是整条退化成直角（实测 76px 高 38✓ / 39✗，超 1px 即失效）。
_SKIP_BTN_QSS = f"""
QPushButton {{
    background: transparent; border: none; padding: 0;
    border-radius: 16px;
}}
QPushButton:hover {{ background-color: {ACCENT_TINT}; }}
"""

_TIME_LABEL_QSS = (
    f"color: {TEXT_MUTED}; font-size: 11px;"
    " font-family: Consolas, 'Segoe UI';"
    " background: transparent; border: none;"
)

# 陶色滑杆：暖砂槽 + 赤陶渐变已播段 + 白底赤陶描边圆柄。
# 手柄 13px + 2.5px 描边 → 总高约 18px，radius 7 在半高内成圆。
_SLIDER_QSS = f"""
QSlider::groove:horizontal {{ background: {INPUT_BG}; height: 5px; border-radius: 3px; }}
QSlider::sub-page:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {ACCENT_HOVER}, stop:1 {ACCENT});
    height: 5px; border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: #FFFFFF; border: 2.5px solid {ACCENT};
    width: 13px; height: 13px; margin: -5px 0; border-radius: 7px;
}}
"""


class SeekSlider(QSlider):
    """可点击跳转的进度条

    信号语义（一次拖动 = 一次跳转）：
        seekPreview(int)  拖动过程中的位置预览（单位秒）。消费方只更新时间和字幕，
                          绝不能调用 QMediaPlayer.setPosition —— 拖动中逐帧 seek 会
                          让解码器反复重开音频管线，听感上就是「同一小段被重复播放」。
        clickedSeek(int)  手势结束时提交的唯一一次跳转（单位秒）。

    无论是「点轨道」（自定义实现）还是「拖滑块」（原生实现），
    都只会在松手瞬间发出一次 clickedSeek。
    """

    clickedSeek = pyqtSignal(int)  # 手势结束后提交一次跳转，单位：秒
    seekPreview = pyqtSignal(int)  # 拖动中的位置预览，单位：秒

    _PREVIEW_INTERVAL = 0.05  # 预览信号节流（仅刷新 UI，不触发解码）

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self._seeking = False        # 是否处于"按在轨道上"的拖动
        self._handle_drag = False    # 是否处于"按住滑块"的原生拖动
        self._last_preview = 0.0
        # 原生拖滑块路径：sliderMoved 只做预览，sliderReleased 才提交
        self.sliderPressed.connect(self._on_slider_pressed)
        self.sliderMoved.connect(self._on_slider_moved)
        self.sliderReleased.connect(self._on_slider_released)

    @property
    def is_seeking(self):
        """用户是否正按着进度条（此时外部不应回写位置，否则滑块会跳回去）"""
        return self._seeking or self._handle_drag

    def _value_from_pos(self, x: float) -> int:
        """把鼠标横坐标换算为滑块值（秒）"""
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), int(x), self.width())

    def _emit_preview(self, value, force=False):
        now = time.monotonic()
        if force or now - self._last_preview >= self._PREVIEW_INTERVAL:
            self._last_preview = now
            self.seekPreview.emit(int(value))

    def mousePressEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and self.maximum() > 0
                and not self._on_handle(event.position().toPoint())):
            # 点击在轨道上 → 滑块跳到该位置；不调用 super()，避免默认 pageStep 逻辑覆盖
            self._seeking = True
            self.setValue(self._value_from_pos(event.position().x()))
            self._emit_preview(self.value(), force=True)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # 按住轨道拖动：滑块跟随鼠标，只发预览，不发 seek
        if self._seeking and (event.buttons() & Qt.MouseButton.LeftButton):
            self.setValue(self._value_from_pos(event.position().x()))
            self._emit_preview(self.value())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._seeking:
            self._seeking = False
            self.clickedSeek.emit(self.value())  # 松手时才真正跳转一次
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ---- 原生滑块拖动 ----
    def _on_slider_pressed(self):
        self._handle_drag = True
        self._emit_preview(self.value(), force=True)

    def _on_slider_moved(self, value):
        if self._handle_drag:
            self._emit_preview(value)

    def _on_slider_released(self):
        if self._handle_drag:
            self._handle_drag = False
            self.clickedSeek.emit(self.value())

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
    expandClicked = pyqtSignal()      # 「展开 ↗」：在大播放器里继续（字幕/队列/迷你）

    # ── 尺寸常量 ──
    # 胶囊圆角必须取半高：Qt 对超半高的 border-radius 退化成直角（见模块头注释）。
    _BAR_H = 64
    _TRANSPORT_H = 50   # 播放键 42 + 边距 3×2 + 描边 2 → 半高 25
    _VOL_H = 30         # 滑杆 20 + 边距 4×2 + 描边 2 → 半高 15
    _EXPAND_H = 26      # 「展开 ↗」胶囊（半高 13）
    _TITLE_WIDTH = 200  # 曲名列定宽（超宽走省略号，防挤掉进度条）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self._BAR_H)
        # 底色必须带类型选择器：裸 background-color 会被 Qt 级联给所有子控件。
        # AudioPlayerBar 是 Python 子类，Qt 类型选择器可直接匹配（QLabel 不会被命中）。
        self.setStyleSheet(f"""
            AudioPlayerBar {{
                background-color: {BG_MAIN};
                border-top: 1px solid {CARD_BORDER};
            }}
        """)
        player_layout = QHBoxLayout(self)
        player_layout.setContentsMargins(12, 6, 12, 6)
        player_layout.setSpacing(8)

        self.now_playing = ClickableLabel("未播放")
        self.now_playing.setFixedWidth(self._TITLE_WIDTH)
        # 注意：QSS 里不能把「裸声明」与「选择器块」混写。Qt 一旦遇到顶层裸声明
        # （形如 `color: #7A6E5C;`）就会判定整张表非法并**整表丢弃**，报
        # "Could not parse stylesheet" —— 结果是这里所有样式（含 hover）都不生效。
        # 必须全部写成 选择器 { ... } 形式。QLabel 作为类型选择器同样匹配其子类。
        self.now_playing.setStyleSheet(f"""
            QLabel {{
                color: {TEXT_SECONDARY}; font-size: 12px; padding: 2px 4px;
                border-radius: 4px; background: transparent; border: none;
            }}
            QLabel:hover {{
                color: {TEXT_PRIMARY}; background-color: {ACCENT_TINT};
            }}
        """)
        self.now_playing.set_clickable(False)
        self.now_playing.clicked.connect(self.nowPlayingClicked.emit)
        player_layout.addWidget(self.now_playing)

        # ── 传输胶囊（50px 高 → 半高 25；objectName 选择器防级联进内部）──
        transport = QFrame()
        transport.setObjectName("transportPill")
        # 纵向 Fixed：胶囊被拉高后半高圆角就不再是胶囊。
        transport.setFixedHeight(self._TRANSPORT_H)
        transport.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        transport.setStyleSheet(f"""
            QFrame#transportPill {{
                background-color: {CARD_BG};
                border: 1px solid {CARD_BORDER};
                border-radius: {self._TRANSPORT_H // 2}px;
            }}
        """)
        transport_layout = QHBoxLayout(transport)
        transport_layout.setContentsMargins(10, 3, 10, 3)
        transport_layout.setSpacing(4)

        self.prev_btn = MediaButton("prev", TEXT_MUTED, TEXT_PRIMARY, icon_size=15)
        self.prev_btn.setFixedSize(32, 32)
        self.prev_btn.setStyleSheet(_SKIP_BTN_QSS)
        self.prev_btn.setToolTip("上一首")
        self.prev_btn.clicked.connect(self.prevClicked.emit)
        transport_layout.addWidget(self.prev_btn)

        # 陶色播放键：赤陶渐变圆面 + 柔光（GlowPlayButton 自绘，QSS 只管透明）
        self.play_btn = GlowPlayButton(
            "play", "#FFFFFF", ACCENT_HOVER, ACCENT, ACCENT,
            icon_size=15, face_ratio=0.4286,
        )
        self.play_btn.setFixedSize(42, 42)
        self.play_btn.setStyleSheet("QPushButton { background: transparent; border: none; }")
        self.play_btn.setToolTip("播放 / 暂停")
        self.play_btn.clicked.connect(self.toggleClicked.emit)
        transport_layout.addWidget(self.play_btn)

        self.next_btn = MediaButton("next", TEXT_MUTED, TEXT_PRIMARY, icon_size=15)
        self.next_btn.setFixedSize(32, 32)
        self.next_btn.setStyleSheet(_SKIP_BTN_QSS)
        self.next_btn.setToolTip("下一首")
        self.next_btn.clicked.connect(self.nextClicked.emit)
        transport_layout.addWidget(self.next_btn)
        player_layout.addWidget(transport)

        self.time_current = QLabel("00:00")
        self.time_current.setStyleSheet(_TIME_LABEL_QSS)
        self.time_current.setFixedWidth(42)
        player_layout.addWidget(self.time_current)

        self.progress_slider = SeekSlider(Qt.Orientation.Horizontal)
        self.progress_slider.setFixedHeight(24)
        self.progress_slider.setStyleSheet(_SLIDER_QSS)
        self.progress_slider.setRange(0, 0)
        # 拖动中只预览（更新时间文本），松手才提交一次跳转，避免重复音效
        self.progress_slider.clickedSeek.connect(self.seekRequested.emit)
        self.progress_slider.seekPreview.connect(self._on_seek_preview)
        player_layout.addWidget(self.progress_slider, 1)

        self.time_total = QLabel("00:00")
        self.time_total.setStyleSheet(_TIME_LABEL_QSS)
        self.time_total.setFixedWidth(42)
        player_layout.addWidget(self.time_total)

        # ── 音量胶囊（30px 高 → 半高 15；Fixed 防止被拉宽拉高）──
        self.vol_pill = QFrame()
        self.vol_pill.setObjectName("volPill")
        self.vol_pill.setFixedHeight(self._VOL_H)
        self.vol_pill.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.vol_pill.setStyleSheet(f"""
            QFrame#volPill {{
                background-color: {CARD_BG};
                border: 1px solid {CARD_BORDER};
                border-radius: {self._VOL_H // 2}px;
            }}
        """)
        vol_layout = QHBoxLayout(self.vol_pill)
        vol_layout.setContentsMargins(12, 4, 12, 4)
        vol_layout.setSpacing(8)

        vol_icon = QLabel()
        vol_icon.setPixmap(glyph_icon("vol", TEXT_MUTED, 15).pixmap(15, 15))
        vol_icon.setStyleSheet("background: transparent; border: none;")
        vol_icon.setFixedSize(15, 15)  # 喇叭图标曾被 HBox 拉宽到 83px，必须定死
        vol_layout.addWidget(vol_icon)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setFixedSize(64, 20)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setStyleSheet(_SLIDER_QSS)
        self.volume_slider.valueChanged.connect(self.volumeChanged.emit)
        vol_layout.addWidget(self.volume_slider)
        player_layout.addWidget(self.vol_pill)

        # ── 「展开 ↗」：把当前播放交给独立播放器窗口（字幕 / 队列 / 迷你）──
        # 模块内这条播放条只负责"随手试听"；要沉浸看字幕、排队列时才进大播放器。
        # 两条界面绑的是同一个 PlaybackController，切过去是接着播、不是重放。
        # 高度 26 → 圆角必须取半高 13，否则整条退化成直角（坑见模块头）。
        self.expand_btn = QPushButton("展开 ↗")
        self.expand_btn.setObjectName("expandPlayerBtn")
        self.expand_btn.setFixedHeight(self._EXPAND_H)
        self.expand_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.expand_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.expand_btn.setToolTip("在大播放器里打开（字幕 / 队列 / 迷你模式）")
        self.expand_btn.setStyleSheet(f"""
            QPushButton#expandPlayerBtn {{
                background-color: {CARD_BG};
                border: 1px solid {CARD_BORDER};
                border-radius: {self._EXPAND_H // 2}px;
                color: {TEXT_SECONDARY};
                font-size: 11px;
                padding: 0 10px;
            }}
            QPushButton#expandPlayerBtn:hover {{
                background-color: {ACCENT_TINT};
                border-color: {ACCENT};
                color: {ACCENT};
            }}
        """)
        self.expand_btn.clicked.connect(self.expandClicked.emit)
        player_layout.addWidget(self.expand_btn)

    # ── 内部信号桥 ──

    def _on_seek_preview(self, seconds):
        """拖动中的预览：只改时间文本，不动播放位置"""
        self.time_current.setText(format_time(int(seconds)))

    # ── 与播放控制器绑定 ──

    def bind_controller(self, controller):
        """把播放条接到 PlaybackController

        条上的按钮 → 控制器方法；控制器信号 → 条上的显示状态。
        这样播放条不再关心 QMediaPlayer，独立播放器窗口也能复用同一份状态。
        """
        self._controller = controller
        self.prevClicked.connect(controller.previous)
        self.nextClicked.connect(controller.next)
        self.toggleClicked.connect(controller.toggle)
        self.seekRequested.connect(lambda secs: controller.seek(secs * 1000))
        self.volumeChanged.connect(controller.set_volume)

        controller.trackChanged.connect(self._on_track_changed)
        controller.positionChanged.connect(self._on_position_changed)
        controller.durationChanged.connect(self._on_duration_changed)
        controller.stateChanged.connect(self.set_playing)
        controller.volumeChanged.connect(self._apply_volume)
        controller.errorOccurred.connect(self._on_playback_error)

        # 打开窗口时同步控制器已有状态（例如从另一个入口接着听）
        self._apply_volume(controller.volume)
        self.set_playing(controller.is_playing)
        if controller.duration() > 0:
            self._on_duration_changed(controller.duration())
        if controller.current_track:
            self._on_track_changed(controller.current_track)

    def _on_track_changed(self, track):
        """切歌：显示曲名（超宽省略号，避免挤掉进度条）"""
        title = (track or {}).get("title") or os.path.basename((track or {}).get("path") or "")
        if not title:
            self.set_now_playing("未播放")
            return
        self.set_now_playing(title)

    def _on_position_changed(self, position_ms):
        self.set_position(int(position_ms) // 1000)

    def _on_duration_changed(self, duration_ms):
        self.set_duration(int(duration_ms) // 1000)

    def _apply_volume(self, volume):
        """控制器音量为准（外部改音量时同步滑块，不反向触发信号）"""
        if self.volume_slider.value() == volume:
            return
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(volume)
        self.volume_slider.blockSignals(False)

    def _on_playback_error(self, message):
        text = str(message)
        self.set_now_playing(f"播放错误: {text[:24]}")
        # 错误文案不是曲目，不该挂「点击打开所属歌单」
        self.now_playing.set_clickable(False)

    # ── 对外状态更新接口 ──

    def set_playing(self, playing: bool):
        """同步播放按钮图形（播放 ↔ 暂停）"""
        self.play_btn.set_kind("pause" if playing else "play")

    def set_now_playing(self, text: str):
        """设置当前曲目文本；播放中可点击跳转所属歌单，未播放时禁用

        超宽文本用 QFontMetrics 省略号截断（QLabel 不会自动加省略号）。
        """
        self.now_playing.setText(self._elide(text))
        # set_clickable 会写 tooltip，全名 tooltip 要放在它之后才不被覆盖
        self.now_playing.set_clickable(bool(text) and text != "未播放")
        if text and text != "未播放":
            self.now_playing.setToolTip(f"{text}\n点击打开所属歌单")

    def _elide(self, text: str) -> str:
        """按曲名列定宽截断文本，超出部分显示 …"""
        if not text:
            return ""
        metrics = QFontMetrics(self.now_playing.font())
        return metrics.elidedText(text, Qt.TextElideMode.ElideRight, self._TITLE_WIDTH - 12)

    def set_duration(self, seconds: int):
        """设置进度条总长与总时长文本"""
        self.progress_slider.setRange(0, seconds)
        self.time_total.setText(format_time(seconds))

    def set_position(self, seconds: int):
        """更新当前进度（用户正在拖动时跳过，避免滑块被播放位置回拽）"""
        self.time_current.setText(format_time(seconds))
        if not self.progress_slider.is_seeking:
            self.progress_slider.setValue(seconds)
