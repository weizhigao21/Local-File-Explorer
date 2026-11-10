"""内置视频播放器（QtMultimedia：QMediaPlayer + QVideoWidget）

设计约束（与音频模块一致，避免再造第三套播放逻辑）：
- `play_list(videos, index)` 是**唯一**播放入口。重复打开同一个文件不再清空队列、
  不再从 0 重新播，只把窗口提到前面（v1.8.18 音频侧踩过的坑，这里一次做对）。
- 续播规则全在 `video_manager.progress`（纯函数），本窗口只负责读写数据库。
- 编码不支持时给「用系统播放器打开」的兜底按钮，不让用户对着黑屏发呆。
- 片头/片尾跳过的规则全在 `video_manager.skip`（纯函数），对齐在 `video_manager.align`；
  本窗口只负责按键、读库、把算出来的毫秒数塞给播放器。

播放器窗口沿用陶色卡片令牌（ui/player_theme 的 P_*），与主界面米色系分开。
"""
import os
import threading

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from resource_manager import config
from ui.media_icons import GlowPlayButton, MediaButton
from ui.player_theme import (
    P_ACCENT,
    P_ACCENT_LIGHT,
    P_BG,
    P_CARD,
    P_CARD_EDGE,
    P_TEXT,
    P_TEXT_DIM,
    P_WHITE,
    play_btn_qss,
    skip_btn_qss,
    slider_qss,
    speed_chip_qss,
    time_label_qss,
    util_btn_qss,
)
from ui.video_detail import open_with_system, reveal_in_explorer
from ui.video_theme import BTN_QSS
from video_manager import align as skip_align
from video_manager import database as db
from video_manager import skip as skip_logic
from video_manager.progress import (
    decide_progress,
    format_duration,
    format_position,
    next_index,
    prev_index,
    resume_position,
    should_keep_current_session,
)

SEEK_STEP_MS = 5000          # ←/→ 与按钮快进快退步长
SAVE_INTERVAL_MS = 5000      # 播放中每 5 秒落一次续播点（崩溃最多丢 5 秒）
AUTOHIDE_MS = 2500           # 全屏时控件自动隐藏
HINT_CLEAR_MS = 6000         # 顶栏提示（「已跳过片头」之类）多久后自动消失
ALIGN_DELAY_MS = 900         # 开播后多久开始后台对齐（先让画面出来，别抢 IO）
SPEED_OPTIONS = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]


class _OpAligner(QObject):
    """后台跑片头对齐的壳子

    只做一件事：把（这一集, 基准集, 模板区间）丢给 `skip_align.align_op`，
    再把结果用信号送回主线程。跨线程 emit 是队列投递，所以回调里动控件是安全的
    （Qt 的信号槽不会真的在子线程里执行槽函数）。
    """

    done = pyqtSignal(str, int, int, str)   # 目标路径, start_ms, end_ms, status

    def run(self, target_path, ref_path, start_ms, end_ms):
        try:
            result = skip_align.align_op(target_path, ref_path, start_ms, end_ms)
        except Exception as e:  # noqa: BLE001 - 对齐失败只该「不跳」，不能把播放器带走
            print(f"[片头对齐] 失败: {e}")
            return
        self.done.emit(
            target_path,
            int(result.get("start_ms") or 0),
            int(result.get("end_ms") or 0),
            str(result.get("status") or "unknown"),
        )


class VideoPlayerWindow(QWidget):
    closed = pyqtSignal()

    # 「按 O 标记片头」的引导只在一次运行里提示一次，别每集都弹
    _tip_shown = False

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName("videoPlayerRoot")
        self.setWindowTitle("视频播放")
        self.resize(1024, 660)
        self.setMinimumSize(720, 480)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # 裸声明会被级联给所有子控件（QFrame{} 甚至命中 QLabel），必须带 #objectName
        self.setStyleSheet(f"#videoPlayerRoot {{ background-color: {P_BG}; }}")

        self._videos = []
        self._index = -1
        self._current_path = ""
        self._current_key = ""  # 规范化后的路径，供「同一个视频」判重
        self._work_id = 0       # 当前视频所属作品：片头/片尾模板按作品记
        self._skip_plan = []    # 本集要跳的区间 [(kind, start, end)]
        self._skipped = set()   # 本集已经处理过的 kind（跳过一次就不再跳）
        self._mark_start = {}   # 两次按键标记片头时的中间态 {"op": ms}
        self._pending_resume = 0
        self._dragging = False
        self._fullscreen = False

        # 播放器可能先于模块窗口被创建（命令行/二次启动转发），自己保证建表；
        # 建表失败只影响续播，不许挡住播放。
        try:
            db.init_db()
        except Exception as e:  # noqa: BLE001
            print(f"[视频库] 初始化失败，续播不可用: {e}")

        self._build_ui()
        self._build_player()
        self._build_shortcuts()

        self._save_timer = QTimer(self)
        self._save_timer.setInterval(SAVE_INTERVAL_MS)
        self._save_timer.timeout.connect(self._save_progress)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(AUTOHIDE_MS)
        self._hide_timer.timeout.connect(self._autohide_bars)

        self._hint_timer = QTimer(self)
        self._hint_timer.setSingleShot(True)
        self._hint_timer.setInterval(HINT_CLEAR_MS)
        self._hint_timer.timeout.connect(self._clear_hint)

        # 片头对齐在后台线程跑，结果用信号回来（见 _OpAligner 的说明）
        self._aligner = _OpAligner(self)
        self._aligner.done.connect(self._on_aligned)
        self._align_thread = None

        self._apply_stored_settings()

    # ==================== UI ====================

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 顶栏：文件名 + 兜底入口 ----
        self.header = QFrame()
        self.header.setObjectName("videoHeader")
        self.header.setFixedHeight(40)
        # 选择器必须是 #objectName：QFrame{} 会级联到内部 QLabel（QLabel 是 QFrame 子类）
        self.header.setStyleSheet(
            f"#videoHeader {{ background-color: {P_CARD}; border: none;"
            f" border-bottom: 1px solid {P_CARD_EDGE}; }}"
        )
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(14, 6, 12, 6)
        header_layout.setSpacing(8)

        self.title_label = QLabel("未在播放")
        self.title_label.setStyleSheet(
            f"color: {P_TEXT}; font-size: 12px; background: transparent; border: none;"
        )
        header_layout.addWidget(self.title_label, 1)

        self.hint_label = QLabel("")
        self.hint_label.setStyleSheet(
            f"color: {P_ACCENT}; font-size: 11px; background: transparent; border: none;"
        )
        header_layout.addWidget(self.hint_label)

        self.external_btn = QPushButton("用系统播放器打开")
        self.external_btn.setStyleSheet(BTN_QSS)
        self.external_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.external_btn.setToolTip("个别编码 Qt 解不了时，交给系统默认播放器")
        self.external_btn.clicked.connect(self._open_external)
        header_layout.addWidget(self.external_btn)

        self.reveal_btn = QPushButton("所在文件夹")
        self.reveal_btn.setStyleSheet(BTN_QSS)
        self.reveal_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reveal_btn.clicked.connect(self._reveal_current)
        header_layout.addWidget(self.reveal_btn)

        root.addWidget(self.header)

        # ---- 画面区：外层黑底容器，避免 QVideoWidget 未出画时露出米色 ----
        self.stage = QFrame()
        self.stage.setObjectName("videoStage")
        self.stage.setStyleSheet("#videoStage { background-color: #000000; border: none; }")
        stage_layout = QVBoxLayout(self.stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(0)

        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumSize(320, 180)
        self.video_widget.installEventFilter(self)  # 双击切全屏
        stage_layout.addWidget(self.video_widget)

        self.error_label = QLabel("", self.stage)
        self.error_label.setObjectName("videoError")
        self.error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(
            "#videoError { color: #F0DDD3; font-size: 13px; background-color: #2A2320;"
            " border: none; border-radius: 8px; padding: 14px 20px; }"
        )
        self.error_label.hide()

        root.addWidget(self.stage, 1)

        # ---- 控制条 ----
        self.controls = QFrame()
        self.controls.setObjectName("videoControls")
        self.controls.setFixedHeight(72)
        self.controls.setStyleSheet(
            "#videoControls { background-color: " + P_CARD + "; border: none;"
            " border-top: 1px solid " + P_CARD_EDGE + "; }"
        )
        controls_layout = QHBoxLayout(self.controls)
        controls_layout.setContentsMargins(12, 10, 12, 10)
        controls_layout.setSpacing(8)

        self.prev_btn = MediaButton("prev", P_TEXT_DIM, P_TEXT, icon_size=16)
        self.prev_btn.setFixedSize(32, 32)
        self.prev_btn.setStyleSheet(skip_btn_qss())
        self.prev_btn.setToolTip("上一集（PgUp）")
        self.prev_btn.clicked.connect(self.play_previous)
        controls_layout.addWidget(self.prev_btn)

        # 48px 按钮出 42px 圆面（与音频播放器同一套设计稿参数）
        self.play_btn = GlowPlayButton(
            "play", P_WHITE, P_ACCENT_LIGHT, P_ACCENT, P_ACCENT,
            icon_size=20, face_ratio=0.4375,
        )
        self.play_btn.setFixedSize(48, 48)
        self.play_btn.setStyleSheet(play_btn_qss())
        self.play_btn.setToolTip("播放 / 暂停（空格）")
        self.play_btn.clicked.connect(self.toggle_play)
        controls_layout.addWidget(self.play_btn)

        self.next_btn = MediaButton("next", P_TEXT_DIM, P_TEXT, icon_size=16)
        self.next_btn.setFixedSize(32, 32)
        self.next_btn.setStyleSheet(skip_btn_qss())
        self.next_btn.setToolTip("下一集（PgDn）")
        self.next_btn.clicked.connect(self.play_next)
        controls_layout.addWidget(self.next_btn)

        self.time_label = QLabel("--:--")
        self.time_label.setFixedWidth(56)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.time_label.setStyleSheet(time_label_qss(P_TEXT))
        controls_layout.addWidget(self.time_label)

        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, 0)
        self.seek.setStyleSheet(slider_qss())
        self.seek.sliderPressed.connect(self._on_seek_pressed)
        self.seek.sliderReleased.connect(self._on_seek_released)
        self.seek.sliderMoved.connect(self._on_seek_moved)
        controls_layout.addWidget(self.seek, 1)

        self.total_label = QLabel("--:--")
        self.total_label.setFixedWidth(56)
        self.total_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.total_label.setStyleSheet(time_label_qss(P_TEXT_DIM))
        controls_layout.addWidget(self.total_label)

        self.speed_box = QComboBox()
        self.speed_box.setFixedSize(72, 24)
        for speed in SPEED_OPTIONS:
            self.speed_box.addItem(f"{speed:g}×", speed)
        self.speed_box.setStyleSheet(speed_chip_qss(12))
        self.speed_box.setToolTip("播放速度")
        self.speed_box.currentIndexChanged.connect(self._on_speed_changed)
        controls_layout.addWidget(self.speed_box)

        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setFixedSize(84, 24)
        self.volume.setRange(0, 100)
        self.volume.setStyleSheet(slider_qss())
        self.volume.setToolTip("音量")
        self.volume.valueChanged.connect(self._on_volume_changed)
        controls_layout.addWidget(self.volume)

        self.fullscreen_btn = QPushButton("全屏")
        self.fullscreen_btn.setFixedSize(52, 32)
        self.fullscreen_btn.setStyleSheet(util_btn_qss())
        self.fullscreen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fullscreen_btn.setToolTip("全屏（双击画面 / F11）")
        self.fullscreen_btn.clicked.connect(self.toggle_fullscreen)
        controls_layout.addWidget(self.fullscreen_btn)

        root.addWidget(self.controls)

    def _build_player(self):
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)

        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.playbackStateChanged.connect(self._on_playback_state)
        self.player.errorOccurred.connect(self._on_error)

    def _build_shortcuts(self):
        def bind(keys, slot):
            for key in keys:
                shortcut = QShortcut(QKeySequence(key), self)
                shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
                shortcut.activated.connect(slot)

        bind(["Space"], self.toggle_play)
        bind(["Right"], lambda: self._seek_by(SEEK_STEP_MS))
        bind(["Left"], lambda: self._seek_by(-SEEK_STEP_MS))
        bind(["PgDown"], self.play_next)
        bind(["PgUp"], self.play_previous)
        bind(["F11", "F"], self.toggle_fullscreen)
        bind(["Esc"], self._on_escape)
        bind(["Up"], lambda: self.volume.setValue(self.volume.value() + 5))
        bind(["Down"], lambda: self.volume.setValue(self.volume.value() - 5))
        # 片头/片尾标记与回看（键位避开所有播放控制）
        bind(["O"], self._mark_op)
        bind(["E"], self._mark_ed)
        bind(["B"], self._replay_op)

    def _apply_stored_settings(self):
        volume = int(getattr(config, "VIDEO_VOLUME", 80) or 0)
        self.volume.setValue(max(0, min(100, volume)))
        self.audio_output.setVolume(self.volume.value() / 100.0)

        stored_speed = float(getattr(config, "VIDEO_SPEED", 1.0) or 1.0)
        for i in range(self.speed_box.count()):
            if abs(self.speed_box.itemData(i) - stored_speed) < 1e-6:
                self.speed_box.blockSignals(True)
                self.speed_box.setCurrentIndex(i)
                self.speed_box.blockSignals(False)
                break
        self.player.setPlaybackRate(stored_speed)

    # ==================== 播放入口 ====================

    def play_list(self, videos, index=0):
        """唯一的播放入口：换集/换作品都走这里

        同一个文件且播放器仍处于播放/暂停态 → 只把窗口提到前面，
        不重置队列、不从 0 开始（否则用户每次双击都丢进度）。
        """
        videos = list(videos or [])
        if not videos:
            return False
        index = max(0, min(int(index), len(videos) - 1))
        target = os.path.normcase(os.path.abspath(videos[index]["path"]))

        active = self.player.playbackState() != QMediaPlayer.PlaybackState.StoppedState
        if should_keep_current_session(self._current_key, target, active):
            self.show_player()
            return True

        self._videos = videos
        self._load(index)
        return True

    def _load(self, index, auto_play=True):
        if not (0 <= index < len(self._videos)):
            return
        # 换集前先把当前进度落库（暂停态也存，避免关窗前的进度丢失）
        self._save_progress()

        video = self._videos[index]
        self._index = index
        self._current_path = video.get("path", "")
        self._current_key = (
            os.path.normcase(os.path.abspath(self._current_path))
            if self._current_path else ""
        )
        self._hint_timer.stop()
        self.hint_label.setText("")
        self.error_label.hide()

        self.title_label.setText(video.get("name", os.path.basename(self._current_path)))
        self.title_label.setToolTip(self._current_path)
        self.setWindowTitle(f"{video.get('name', '')} · 视频播放")

        self._pending_resume = 0
        if getattr(config, "VIDEO_RESUME", True):
            saved, saved_duration = db.get_progress(self._current_path)
            duration = int(video.get("duration_ms") or saved_duration or 0)
            self._pending_resume = resume_position(saved, duration)

        # 换集：上一集的「已跳过」状态、标到一半的起点都作废
        self._work_id = self._resolve_work_id(video)
        self._skipped = set()
        self._mark_start.clear()

        self.seek.setRange(0, 0)
        self.seek.setValue(0)
        # 起点显示 00:00 而不是 --:--：文件已经载入，"--:--" 只用于「时长未知」
        self.time_label.setText(format_position(0))
        self.total_label.setText(format_duration(video.get("duration_ms", 0)))
        self._update_skip_buttons()

        # 先停旧的再换源：否则旧文件的 positionChanged 会写进新一集的进度条
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(self._current_path))
        # 此刻播放器时长还是 0/旧值，用扫描时探到的时长兜底，ED 才有终点可用
        self._load_skip_plan(video.get("duration_ms") or 0)
        if auto_play:
            self.player.play()
        # 画面先出来再跑去重：对齐要解两次音频，别跟开播抢 IO
        QTimer.singleShot(ALIGN_DELAY_MS, self._maybe_align_op)

    def show_player(self):
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def play_previous(self):
        target = prev_index(self._index, len(self._videos))
        if target is not None:
            self._load(target)

    def play_next(self):
        target = next_index(self._index, len(self._videos))
        if target is not None:
            self._load(target)

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            if not self._current_path and self._videos:
                self._load(0)
                return
            self.player.play()

    def _seek_by(self, delta_ms):
        if self.player.duration() <= 0:
            return
        position = max(0, min(self.player.duration(), self.player.position() + delta_ms))
        self.player.setPosition(position)

    # ==================== 播放器回调 ====================

    def _on_position_changed(self, position):
        if not self._dragging:
            if not self.seek.isSliderDown():
                self.seek.setValue(int(position))
            self.time_label.setText(format_position(position))
        if self._dragging:
            # 拖拽期间不触发自动跳过：用户正在找位置，别跟他抢滑块
            return
        self._apply_auto_skip(position)

    def _on_duration_changed(self, duration):
        self.seek.setRange(0, max(0, int(duration)))
        self.total_label.setText(format_duration(duration))
        if duration > 0:
            db.update_video_duration(self._current_path, duration)
        self._apply_pending_resume()
        # 真实时长出来后重算一次：ED 的「到结尾」要靠它才能确定终点
        self._load_skip_plan()

    def _apply_pending_resume(self):
        if self._pending_resume > 0:
            position = self._pending_resume
            self._pending_resume = 0
            self.player.setPosition(position)
            self.time_label.setText(format_position(position))
            self._flash_hint(f"从 {format_position(position)} 继续")

    def _on_media_status(self, status):
        if status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            self._apply_pending_resume()
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._on_finished()

    def _on_finished(self):
        self._save_progress()
        target = next_index(self._index, len(self._videos))
        if getattr(config, "VIDEO_AUTOPLAY_NEXT", True) and target is not None:
            self._load(target)
        else:
            self.play_btn.set_kind("play")
            self.hint_label.setText("已播完")

    def _on_playback_state(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_btn.set_kind("pause" if playing else "play")
        if playing:
            self._save_timer.start()
        else:
            self._save_timer.stop()
            self._save_progress()

    def _on_error(self, _error, message):
        self._save_timer.stop()
        text = (message or "").strip() or "未知错误"
        self.error_label.setText(
            f"无法播放：{text}\n\n"
            "可能是编码不受支持（如 HEVC / AV1 缺少解码器）。\n"
            "可点右上角「用系统播放器打开」。"
        )
        self.error_label.setFixedWidth(max(320, min(560, self.width() - 120)))
        self.error_label.adjustSize()
        self._center_error_label()
        self.error_label.show()
        self._hint_timer.stop()
        self.hint_label.setText("播放失败")

    def _center_error_label(self):
        x = max(0, (self.stage.width() - self.error_label.width()) // 2)
        y = max(0, (self.stage.height() - self.error_label.height()) // 2)
        self.error_label.move(x, y)

    # ==================== 续播点 ====================

    def _save_progress(self):
        if not getattr(config, "VIDEO_RESUME", True):
            return
        path = self._current_path
        if not path:
            return
        position = int(self.player.position() or 0)
        duration = int(self.player.duration() or 0)
        action, value = decide_progress(position, duration)
        if action == "clear":
            # 看完了就清掉续播点，下次从头上开始，否则一进去又立刻结束
            db.clear_progress(path)
        elif action == "set":
            db.set_progress(path, value, duration)

    # ==================== 片头 / 片尾跳过 ====================
    #
    # 分工：规则在 video_manager.skip（纯函数），对齐在 video_manager.align（纯 Python），
    # 这里只负责按键、读库、把算出来的毫秒塞给播放器。
    #
    # 交互：O 两次按键标片头（起点 / 终点），E 一次按键标片尾（按第二次则取消），
    # B 回看刚跳过的片头。标记落到**作品**上，同一部动画后面每一集都自动跳过。

    def _resolve_work_id(self, video):
        """取当前视频所属作品 id：dict 里有就用，没有按路径回查（续播页/搜索入口）"""
        try:
            work_id = int((video or {}).get("work_id") or 0)
        except (TypeError, ValueError):
            work_id = 0
        if work_id:
            return work_id
        return int(db.get_work_id_of_video(self._current_path) or 0)

    def _load_skip_plan(self, duration_ms=None):
        """按「作品模板 + 本集对齐结果」重算这一集要跳的区间

        duration_ms 传 None 表示从播放器读（时长变更时用）；载入新集时播放器时长
        还是旧值/0，调用方必须显式传扫描时探到的时长。
        """
        self._skip_plan = []
        if not self._current_path or not self._work_id:
            return
        segments = db.get_skip_segments(self._work_id)
        if not segments:
            self._show_mark_tip()
            return
        overrides = db.get_skip_overrides(self._current_path)
        duration = (int(self.player.duration() or 0) if duration_ms is None
                    else int(duration_ms or 0))
        self._skip_plan = skip_logic.build_plan(
            segments,
            overrides,
            duration,
            skip_op=bool(getattr(config, "VIDEO_SKIP_OP", True)),
            skip_ed=bool(getattr(config, "VIDEO_SKIP_ED", True)),
        )

    def _show_mark_tip(self):
        """第一次遇到「还没标过片头」的作品时提示一次怎么标（一次运行只提示一次）"""
        if VideoPlayerWindow._tip_shown or not self._current_path:
            return
        VideoPlayerWindow._tip_shown = True
        self._flash_hint("按 O 标片头、E 标片尾，全剧通用")

    def _apply_auto_skip(self, position):
        """播放位置进入片头/片尾区间就往前跳；每段每集只跳一次"""
        if not self._skip_plan:
            return False
        duration = int(self.player.duration() or 0)
        hit = skip_logic.pick_skip(position, self._skip_plan, self._skipped, duration)
        if not hit:
            return False
        kind, target = hit
        self._skipped.add(kind)
        self.player.setPosition(int(target))
        self.seek.setValue(int(target))
        self.time_label.setText(format_position(target))
        self._flash_hint(
            f"已跳过{skip_logic.label(kind)} "
            f"{format_position(position)} → {format_position(target)} · B 回看"
        )
        return True

    def _mark_op(self):
        """按 O：第一次记起点，第二次记终点并写进作品模板"""
        if not self._current_path or not self._work_id:
            return
        position = int(self.player.position() or 0)
        start = self._mark_start.get(skip_logic.OP)
        if start is None:
            self._mark_start[skip_logic.OP] = position
            self._flash_hint(
                f"片头起点 {format_position(position)} 已记 —— 播到片头结束再按 O"
            )
            return
        if position - start < skip_logic.MIN_SEGMENT_MS:
            self._flash_hint("这段太短了，往后播一点再按 O")
            return
        self._mark_start.pop(skip_logic.OP, None)
        if db.set_skip_segment(
            self._work_id, skip_logic.OP, start, position, self._current_path
        ):
            self._skipped.discard(skip_logic.OP)
            self._load_skip_plan()
            self._flash_hint(
                f"片头 {format_position(start)}–{format_position(position)} 已记，全剧通用"
            )

    def _mark_ed(self):
        """按 E：记片尾起点（终点到文件结尾）；已有记录时再按一次则取消"""
        if not self._current_path or not self._work_id:
            return
        existing = db.get_skip_segments(self._work_id).get(skip_logic.ED)
        if existing:
            db.clear_skip_segment(self._work_id, skip_logic.ED)
            self._skipped.discard(skip_logic.ED)
            self._load_skip_plan()
            self._flash_hint("已取消本作品的片尾跳过")
            return

        position = int(self.player.position() or 0)
        duration = int(self.player.duration() or 0)
        if duration > 0 and duration - position < skip_logic.MIN_SEGMENT_MS:
            self._flash_hint("离结尾太近了，往前一点再按 E")
            return
        # end 传真实时长；时长未知就传 0（表示「到结尾」，播放时再用真实时长兜底）
        if db.set_skip_segment(
            self._work_id, skip_logic.ED, position, duration, self._current_path
        ):
            self._skipped.discard(skip_logic.ED)
            self._load_skip_plan()
            self._flash_hint(
                f"片尾从 {format_position(position)} 起跳过，全剧通用"
            )

    def _replay_op(self):
        """按 B：回到这一集的片头起点，并把片头标成「已处理」，免得又被弹走"""
        for kind, start, _end in self._skip_plan:
            if kind == skip_logic.OP:
                self._skipped.add(skip_logic.OP)
                self.player.setPosition(int(start))
                self.seek.setValue(int(start))
                self.time_label.setText(format_position(start))
                self._flash_hint(f"已回到{skip_logic.label(kind)}起点 {format_position(start)}")
                return
        self._flash_hint("本作品还没标记片头（按 O 标一次）")

    def _maybe_align_op(self):
        """后台把作品模板对齐到这一集：每集的片头位置常差几秒，死用模板会跳早/跳晚"""
        if not self._current_path or not self._work_id:
            return
        if not skip_align.available():
            return
        op = db.get_skip_segments(self._work_id).get(skip_logic.OP)
        if not op:
            return
        if db.get_skip_overrides(self._current_path).get(skip_logic.OP):
            return  # 已经算过（ok 或 absent），别重复解码

        ref_path = op.get("source_path") or ""
        if ref_path and os.path.normcase(os.path.abspath(ref_path)) == self._current_key:
            return  # 当前就是基准那一集，模板本身就准

        if self._align_thread is not None and self._align_thread.is_alive():
            return
        target = self._current_path
        self._align_thread = threading.Thread(
            target=self._aligner.run,
            args=(target, ref_path, int(op.get("start_ms") or 0), int(op.get("end_ms") or 0)),
            daemon=True,
            name="video-op-align",
        )
        self._align_thread.start()

    def _on_aligned(self, target_path, start_ms, end_ms, status):
        """对齐结果回到主线程：写库 + 就地更新本集计划"""
        if status not in ("ok", "absent"):
            return
        if not target_path:
            return
        db.set_skip_override(target_path, skip_logic.OP, start_ms, end_ms, status)
        if target_path != self._current_path:
            return  # 算完时用户已经换集了，只落库，别改当前这集的计划
        self._load_skip_plan()
        if status == "ok":
            self._flash_hint(
                f"片头已对齐到 {format_position(start_ms)}–{format_position(end_ms)}"
            )

    def _flash_hint(self, text):
        self.hint_label.setText(text)
        self._hint_timer.start()

    def _clear_hint(self):
        self.hint_label.setText("")

    # ==================== 进度条 / 音量 / 倍速 ====================

    def _on_seek_pressed(self):
        self._dragging = True

    def _on_seek_released(self):
        self._dragging = False
        value = int(self.seek.value())
        # 用户主动拖进片头/片尾里看 → 这段本次不再自动跳，否则刚拖回去就被弹走
        kind = skip_logic.inside_segment(
            value, self._skip_plan, int(self.player.duration() or 0)
        )
        if kind:
            self._skipped.add(kind)
        self.player.setPosition(value)
        self.time_label.setText(format_position(value))

    def _on_seek_moved(self, value):
        self.time_label.setText(format_position(value))

    def _on_volume_changed(self, value):
        self.audio_output.setVolume(value / 100.0)
        config.set_user_config("VIDEO_VOLUME", int(value))

    def _on_speed_changed(self, _index):
        speed = float(self.speed_box.currentData() or 1.0)
        self.player.setPlaybackRate(speed)
        config.set_user_config("VIDEO_SPEED", speed)

    def _update_skip_buttons(self):
        total = len(self._videos)
        self.prev_btn.setEnabled(prev_index(self._index, total) is not None)
        self.next_btn.setEnabled(next_index(self._index, total) is not None)

    # ==================== 全屏 ====================

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            self._fullscreen = False
            self.header.show()
            self.controls.show()
            self.fullscreen_btn.setText("全屏")
        else:
            self.showFullScreen()
            self._fullscreen = True
            self.fullscreen_btn.setText("退出全屏")
            self._hide_timer.start()

    def _on_escape(self):
        if self.isFullScreen():
            self.toggle_fullscreen()

    def _autohide_bars(self):
        if self.isFullScreen():
            self.header.hide()
            self.controls.hide()
            self.setCursor(Qt.CursorShape.BlankCursor)

    def _show_bars_temporarily(self):
        if not self.isFullScreen():
            return
        self.header.show()
        self.controls.show()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._hide_timer.start()

    def mouseMoveEvent(self, event):
        self._show_bars_temporarily()
        super().mouseMoveEvent(event)

    def eventFilter(self, obj, event):
        """画面双击切全屏；全屏下移动鼠标唤出控制条"""
        if obj is self.video_widget:
            if event.type() == QEvent.Type.MouseButtonDblClick:
                self.toggle_fullscreen()
                return True
            if event.type() == QEvent.Type.MouseMove:
                self._show_bars_temporarily()
        return super().eventFilter(obj, event)

    # ==================== 外部入口 ====================

    def _open_external(self):
        if self._current_path:
            open_with_system(self._current_path)

    def _reveal_current(self):
        if self._current_path:
            reveal_in_explorer(self._current_path)

    def current_path(self):
        return self._current_path

    def current_index(self):
        return self._index

    # ==================== 生命周期 ====================

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.error_label.isVisible():
            self.error_label.setFixedWidth(max(320, min(560, self.width() - 120)))
            self.error_label.adjustSize()
            self._center_error_label()

    def closeEvent(self, event):
        self._save_progress()
        config.set_user_config("VIDEO_VOLUME", int(self.volume.value()))
        config.save_user_config()
        self._save_timer.stop()
        self._hide_timer.stop()
        self.player.stop()
        self.player.setSource(QUrl())
        self._current_key = ""
        super().closeEvent(event)
        self.closed.emit()

    def shutdown(self):
        """程序退出清理（main.cleanup 调用）"""
        self._save_timer.stop()
        self._hide_timer.stop()
        self.player.stop()
