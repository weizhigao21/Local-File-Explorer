"""
独立播放器窗口

与音频库窗口共享 app 级 PlaybackController：
    - 左边封面 + 曲目信息，右边滚动字幕；
    - 底部进度、上一首/播放/下一首、音量、循环模式、倍速；
    - 可折叠播放队列、迷你模式、窗口置顶、拖放文件/文件夹即播；
    - 独立模式（由文件关联 `--play` 拉起）：只开这一个小窗，关闭即退出程序。
"""
import os
from contextlib import suppress

from PyQt6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QTimer,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImageReader,
    QKeySequence,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QWIDGETSIZE_MAX,
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from audio_manager import database as db
from audio_manager import dlsite
from audio_manager import subtitles as subs
from audio_manager.scanner import AUDIO_EXTENSIONS
from resource_manager import config
from resource_manager.utils import natural_key
from ui.audio_player import SeekSlider
from ui.audio_theme import format_time
from ui.media_icons import EqBars, GlowPlayButton, MediaButton, glyph_icon
from ui.playback_controller import (
    DEFAULT_VOLUME,
    REPEAT_LABELS,
    get_playback_controller,
)
from ui.player_theme import (
    P_ACCENT,
    P_ACCENT_LIGHT,
    P_CARD_EDGE,
    P_TEXT,
    P_TEXT_BTN,
    P_TEXT_DIM,
    P_TEXT_FAINT,
    P_WHITE,
    card_qss,
    cover_qss,
    header_qss,
    mini_cover_qss,
    mini_slider_qss,
    pill_btn_qss,
    play_btn_qss,
    queue_list_qss,
    queue_panel_qss,
    skip_btn_qss,
    slider_qss,
    speed_chip_qss,
    subtitle_theme,
    tag_qss,
    time_label_qss,
    transport_pill_qss,
    util_btn_qss,
    vol_pill_qss,
    window_qss,
)
from ui.subtitle_view import SubtitleView

_SPEEDS = (0.75, 1.0, 1.25, 1.5, 2.0)
_COVER_SIZE = 208
_MINI_COVER = 44

# 文件夹里常见的封面文件名（与扩展名组合，小写比对）与图片扩展名。
# 下载站/自抓的封面命名五花八门：封面、cover、folder、front、albumart……
# 只认这些"明确就是封面"的名字，不扫任意图片——作品文件夹里常混着详情页截图。
_COVER_STEMS = ("封面", "cover", "folder", "front", "albumart", "album", "poster")
_COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
_WIN_MIN_W = 540        # 常规态窗口最小宽
_WIN_MIN_H = 140        # 常规态窗口最小高（迷你态退出时恢复）
_HEADER_H = 52          # 常规态顶栏高
_HEADER_MARGINS = (18, 0, 18, 0)
_HEADER_SPACING = 12
# 迷你态顶栏收薄：52px 里只放 25px 胶囊太空旷（用户反馈 2026-10-22）
_MINI_HEADER_H = 42
_MINI_HEADER_MARGINS = (14, 0, 14, 0)
_MINI_HEADER_SPACING = 8
_MINI_W = 560           # 迷你态窗口默认宽（横向仍可拉）
_MINI_TRANSITION_MS = 180   # 迷你↔完整态的窗口几何过渡时长
_QUEUE_NAME_MAX = 128   # 队列曲名省略宽（240 卡内刨去序号/尾标/边距）
_MINI_TITLE_MAX = 124   # 迷你态曲名省略宽（130px 信息列）


def _elide(text, width, pixel_size=12):
    """QLabel 不会自动加省略号，超长文本必须用 QFontMetrics 截断"""
    font = QFont()
    font.setPixelSize(pixel_size)
    return QFontMetrics(font).elidedText(text or "", Qt.TextElideMode.ElideRight, width)


def folder_cover(folder):
    """在文件夹里按常见命名找封面：封面.jpg / cover.png / folder.jpg …

    返回找到的绝对路径，没有返回 None。只认固定几个"明确是封面"的名字，
    不取同目录的任意图片——作品文件夹里往往混着详情页截图和特典图，
    随便挑一张当封面比"无封面"更糟。
    """
    if not folder or not os.path.isdir(folder):
        return None
    try:
        names = {name.lower(): name for name in os.listdir(folder)}
    except OSError:
        return None
    for stem in _COVER_STEMS:
        for ext in _COVER_EXTS:
            hit = names.get(stem + ext)
            if hit:
                path = os.path.join(folder, hit)
                if os.path.isfile(path):
                    return path
    return None


def dlsite_cover(track):
    """按 RJ 码找已下载的 DLsite 封面（data/dlsite_covers/<RJ>.jpg）

    查找顺序：曲目标题 → 所在文件夹名 → 完整路径。
    单文件播放（无 playlist_id）时歌单层封面拿不到，这是唯一的兜底。
    """
    path = track.get("path") or ""
    folder = os.path.dirname(path)
    for text in (track.get("title") or "", os.path.basename(folder), path):
        rj = dlsite.extract_rj_code(text)
        if rj:
            cover = os.path.join(config.DLSITE_COVER_DIR, f"{rj}.jpg")
            return cover if os.path.isfile(cover) else None
    return None

_BTN_QSS = pill_btn_qss()
_SKIP_BTN_QSS = skip_btn_qss()
_PLAY_BTN_QSS = play_btn_qss()


def tracks_from_paths(paths):
    """把文件/文件夹路径展开成播放队列条目（文件夹递归一层）"""
    tracks = []
    for path in paths:
        if os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                for name in sorted(files, key=natural_key):
                    if os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS:
                        full = os.path.join(root, name)
                        tracks.append({"title": os.path.splitext(name)[0],
                                       "path": full, "playlist_id": None})
        elif os.path.isfile(path) and os.path.splitext(path)[1].lower() in AUDIO_EXTENSIONS:
            tracks.append({"title": os.path.splitext(os.path.basename(path))[0],
                           "path": path, "playlist_id": None})
    return tracks


class PlayerWindow(QMainWindow):
    """音乐播放器式窗口：只做展示与交互，播放状态全在控制器里

    standalone=True 表示这是一次「独立播放」启动（文件关联/`--play`）：
    程序里只有这一个窗口，关闭它就退出程序，不需要托盘常驻。
    """

    def __init__(self, playback=None, parent=None, standalone=False):
        super().__init__(parent)
        self.playback = playback or get_playback_controller()
        self.standalone = standalone
        self._allow_close = False
        self._mini = False
        self._positioned = False
        self._normal_size = QSize(900, 620)
        self._geo_anim = None           # 迷你↔完整态的几何过渡动画
        self._animate = True            # 测试可关掉，直接切尺寸
        self._track = None
        self._cover_from_file = False   # 当前封面是否来自磁盘文件（否则等内嵌封面）
        self._subtitle_path = None
        self._current_sec = 0
        self._total_sec = 0
        self._queue = []

        self.setWindowTitle("播放器")
        self.setMinimumSize(_WIN_MIN_W, _WIN_MIN_H)
        self.setAcceptDrops(True)
        self.resize(self._normal_size)
        # 独立模式是普通顶层窗口（无 parent、非 Qt.Tool）：Windows 会给它一个任务栏按钮，
        # 最小化后直接从任务栏点回来即可，不需要像托盘那样去右下角小图标区里找。
        icon = None
        try:
            from resource_manager import config
            icon_path = os.path.join(config.get_project_root(), "data", "ui.ico")
            if os.path.exists(icon_path):
                from PyQt6.QtGui import QIcon
                icon = QIcon(icon_path)
        except Exception:
            icon = None
        if icon is not None:
            self.setWindowIcon(icon)

        self._build_ui()
        self._bind_controller()
        self._setup_shortcuts()
        # 构造期就把置顶标志定下来（独立播放模式不经过 show_player）。
        # 只改窗口标志，不主动 show —— 何时显示由调用方决定。
        self._sync_on_top_flag()

        # 字幕跟随用独立时钟：媒体后端的 positionChanged 间隔不稳定，
        # 100ms 轮询能让高亮滚动更顺滑，也避免依赖信号频率。
        self._subtitle_timer = QTimer(self)
        self._subtitle_timer.setInterval(100)
        self._subtitle_timer.timeout.connect(self._tick_subtitle)

    # ==================== UI ====================
    def _build_ui(self):
        central = QWidget()
        central.setObjectName("playerCentral")  # window_qss 用 #playerCentral 选择器
        central.setStyleSheet(window_qss())
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 顶栏：品牌标 + 产品名 + 面包屑 + 图标胶囊 ----
        self.header = QFrame()
        self.header.setFixedHeight(_HEADER_H)
        self.header.setStyleSheet(header_qss())
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(*_HEADER_MARGINS)
        header_layout.setSpacing(_HEADER_SPACING)

        brand_mark = QLabel("▶")
        brand_mark.setFixedSize(22, 22)
        brand_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_mark.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
            f" stop:0 {P_ACCENT_LIGHT}, stop:1 {P_ACCENT});"
            f" border: none; border-radius: 6px; color: {P_WHITE};"
            " font-size: 10px; font-weight: 700;"
        )
        header_layout.addWidget(brand_mark)

        brand = QLabel("播放器")
        brand.setStyleSheet(f"color: {P_TEXT}; font-size: 13px; font-weight: 600;"
                           " background: transparent; border: none;")
        header_layout.addWidget(brand)

        # 面包屑「艺术家 / 曲名」：艺术家深咖加粗、曲名灰（设计稿 track-crumb）
        self.header_title = QLabel("")
        self.header_title.setTextFormat(Qt.TextFormat.RichText)
        self.header_title.setStyleSheet("font-size: 12px;"
                                        " background: transparent; border: none;")
        # 空闲态占位：否则顶栏中段一大片空白（用户反馈「空旷」）。
        # 开始播放后由 _on_track_changed 换成「艺术家 / 曲名」。
        self.header_title.setText(
            f'<span style="color:{P_TEXT_FAINT}">拖入音频或从资源库选择播放</span>')
        header_layout.addWidget(self.header_title)
        header_layout.addStretch()

        self.queue_btn = QPushButton("队列 0")
        self.queue_btn.setCheckable(True)
        self.queue_btn.setStyleSheet(_BTN_QSS)
        self._set_pill_icon(self.queue_btn, "queue")
        self.queue_btn.toggled.connect(self._toggle_queue)
        self.queue_btn.toggled.connect(
            lambda _on=False, b=self.queue_btn: self._set_pill_icon(b, "queue"))
        header_layout.addWidget(self.queue_btn)

        self.on_top_btn = QPushButton("置顶")
        self.on_top_btn.setCheckable(True)
        self.on_top_btn.setStyleSheet(_BTN_QSS)
        self._set_pill_icon(self.on_top_btn, "pin")
        self.on_top_btn.toggled.connect(self._toggle_on_top)
        self.on_top_btn.toggled.connect(
            lambda _on=False, b=self.on_top_btn: self._set_pill_icon(b, "pin"))
        header_layout.addWidget(self.on_top_btn)

        self.mini_btn = QPushButton("迷你")
        self.mini_btn.setCheckable(True)
        self.mini_btn.setStyleSheet(_BTN_QSS)
        self._set_pill_icon(self.mini_btn, "mini")
        self.mini_btn.toggled.connect(self._toggle_mini)
        self.mini_btn.toggled.connect(
            lambda _on=False, b=self.mini_btn: self._set_pill_icon(b, "mini"))
        header_layout.addWidget(self.mini_btn)
        root.addWidget(self.header)

        # ---- 主体：封面卡（240）+ 字幕卡 + 队列卡（240）----
        self.body = QWidget()
        body_layout = QHBoxLayout(self.body)
        body_layout.setContentsMargins(20, 20, 20, 8)
        body_layout.setSpacing(20)

        self.cover_col = QFrame()
        self.cover_col.setFixedWidth(240)
        self.cover_col.setStyleSheet(card_qss())
        self._soft_shadow(self.cover_col)
        cover_layout = QVBoxLayout(self.cover_col)
        cover_layout.setContentsMargins(16, 16, 16, 16)
        cover_layout.setSpacing(0)

        # 封面（208px）+ 右上角星标角标
        cover_wrap = QWidget()
        cover_grid = QGridLayout(cover_wrap)
        cover_grid.setContentsMargins(0, 0, 0, 0)
        self.cover_label = QLabel("无封面")
        self.cover_label.setFixedSize(_COVER_SIZE, _COVER_SIZE)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setStyleSheet(cover_qss())
        cover_grid.addWidget(self.cover_label, 0, 0)

        badge_holder = QWidget()
        badge_layout = QVBoxLayout(badge_holder)
        badge_layout.setContentsMargins(0, 10, 10, 0)
        badge = QLabel()
        badge.setFixedSize(26, 26)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet("QLabel { background-color: #FBF7F0;"
                            " border: none; border-radius: 13px; }")
        badge.setPixmap(glyph_icon("star", P_ACCENT, 13).pixmap(13, 13))
        badge_layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignRight)
        cover_grid.addWidget(badge_holder, 0, 0, Qt.AlignmentFlag.AlignTop)
        cover_layout.addWidget(cover_wrap)

        self.title_label = QLabel("未播放")
        self.title_label.setWordWrap(True)
        # 显式字体度量 + 最小高度：word-wrap 的 sizeHint 按浮点行高向下取整
        # （17px 字体只报 18px），而墨盒 ascent+descent=19px，字形头脚被裁掉 1px。
        # setFont 让 sizeHint/度量立即正确，setMinimumHeight 顶住布局压缩（v1.8.6）。
        title_font = QFont()
        title_font.setPixelSize(17)
        title_font.setWeight(QFont.Weight.DemiBold)
        self.title_label.setFont(title_font)
        self.title_label.setMinimumHeight(QFontMetrics(title_font).height())
        self.title_label.setStyleSheet(f"color: {P_TEXT}; font-size: 17px; font-weight: 600;"
                                       " background: transparent; border: none;")
        cover_layout.addSpacing(14)
        cover_layout.addWidget(self.title_label)

        self.artist_label = QLabel("—")
        self.artist_label.setWordWrap(True)
        self.artist_label.setStyleSheet(f"color: {P_TEXT_DIM}; font-size: 12px;"
                                        " background: transparent; border: none;")
        cover_layout.addSpacing(5)
        cover_layout.addWidget(self.artist_label)

        self.album_label = QLabel("")
        self.album_label.setWordWrap(True)
        self.album_label.setStyleSheet(f"color: {P_TEXT_FAINT}; font-size: 11px;"
                                       " background: transparent; border: none;")
        cover_layout.addSpacing(2)
        cover_layout.addWidget(self.album_label)

        # 虚线分隔 + 标签行（格式 / 采样率 / 队列数）
        meta_sep = QFrame()
        meta_sep.setFixedHeight(1)
        meta_sep.setStyleSheet(f"QFrame {{ background: transparent; border: none;"
                               f" border-top: 1px dashed {P_CARD_EDGE}; }}")
        cover_layout.addSpacing(12)
        cover_layout.addWidget(meta_sep)

        meta_row = QWidget()
        meta_layout = QHBoxLayout(meta_row)
        meta_layout.setContentsMargins(0, 0, 0, 0)
        meta_layout.setSpacing(6)
        self.meta_label = QLabel("")
        self.meta_label.setStyleSheet(tag_qss())
        self.meta_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        meta_layout.addWidget(self.meta_label)
        self.meta_rate_label = QLabel("")
        self.meta_rate_label.setStyleSheet(tag_qss())
        self.meta_rate_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.meta_rate_label.setVisible(False)
        meta_layout.addWidget(self.meta_rate_label)
        self.meta_queue_label = QLabel("")
        self.meta_queue_label.setStyleSheet(tag_qss())
        self.meta_queue_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        meta_layout.addWidget(self.meta_queue_label)
        meta_layout.addStretch()
        cover_layout.addSpacing(12)
        cover_layout.addWidget(meta_row)
        cover_layout.addStretch()

        body_layout.addWidget(self.cover_col)

        self.subtitle_view = SubtitleView(theme=subtitle_theme())
        self.subtitle_view.offsetChanged.connect(self._save_subtitle_offset)
        self._soft_shadow(self.subtitle_view)
        body_layout.addWidget(self.subtitle_view, 1)

        self.queue_panel = QFrame()
        self.queue_panel.setFixedWidth(240)
        self.queue_panel.setVisible(False)
        self.queue_panel.setStyleSheet(queue_panel_qss())
        self._soft_shadow(self.queue_panel)
        queue_layout = QVBoxLayout(self.queue_panel)
        queue_layout.setContentsMargins(12, 14, 12, 14)
        queue_layout.setSpacing(4)

        queue_head = QWidget()
        queue_head_layout = QHBoxLayout(queue_head)
        queue_head_layout.setContentsMargins(4, 0, 4, 0)
        queue_head_layout.setSpacing(8)
        queue_title = QLabel("播放队列")
        queue_title.setStyleSheet(f"color: {P_TEXT_DIM}; font-size: 11px; border: none;"
                                  " background: transparent;")
        queue_head_layout.addWidget(queue_title)
        queue_head_layout.addStretch()
        self.queue_count = QLabel("0")
        self.queue_count.setStyleSheet(tag_qss() + f" color: {P_ACCENT};")
        queue_head_layout.addWidget(self.queue_count)
        queue_layout.addWidget(queue_head)

        self.queue_list = QListWidget()
        self.queue_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.queue_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.queue_list.setStyleSheet(queue_list_qss())
        self.queue_list.itemDoubleClicked.connect(self._play_queue_row)
        queue_layout.addWidget(self.queue_list, 1)
        body_layout.addWidget(self.queue_panel)

        root.addWidget(self.body, 1)

        # ---- 控制区：进度卡片 + 传输胶囊 / 右侧功能簇 ----
        # 存成属性：迷你态要把整个容器（含边距）一起藏掉，
        # 否则边距仍占 32px 高，把 mini_body 压扁、播放键光晕被胶囊齐底截断。
        self.controls = QWidget()
        controls_layout = QVBoxLayout(self.controls)
        controls_layout.setContentsMargins(20, 14, 20, 18)
        controls_layout.setSpacing(14)

        self.seek_card = QFrame()
        self.seek_card.setStyleSheet(card_qss(radius=12))
        self._soft_shadow(self.seek_card)
        seek_layout = QHBoxLayout(self.seek_card)
        seek_layout.setContentsMargins(16, 12, 16, 12)
        seek_layout.setSpacing(12)

        self.time_current = QLabel("00:00")
        self.time_current.setStyleSheet(time_label_qss())
        self.time_current.setFixedWidth(38)
        seek_layout.addWidget(self.time_current)

        self.progress = SeekSlider(Qt.Orientation.Horizontal)
        self.progress.setFixedHeight(22)
        self.progress.setRange(0, 0)
        self.progress.setStyleSheet(slider_qss())
        # 一次拖动只提交一次跳转；拖动过程中只做预览（改时间/字幕），
        # 逐帧 setPosition 会让解码器反复重开音频管线，听感上是「重复音效」。
        self.progress.clickedSeek.connect(self._seek_to_second)
        self.progress.seekPreview.connect(self._on_seek_preview)
        seek_layout.addWidget(self.progress, 1)

        self.time_total = QLabel("00:00")
        self.time_total.setStyleSheet(time_label_qss())
        self.time_total.setFixedWidth(38)
        seek_layout.addWidget(self.time_total)
        controls_layout.addWidget(self.seek_card)

        self.secondary_row = QWidget()
        secondary_layout = QHBoxLayout(self.secondary_row)
        secondary_layout.setContentsMargins(0, 0, 0, 0)
        secondary_layout.setSpacing(12)

        transport = QFrame()
        # 纵向 Fixed：胶囊必须贴内容高度（60px），被拉高后半高圆角就不再是胶囊。
        transport.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        transport.setStyleSheet(transport_pill_qss())
        self._soft_shadow(transport)
        transport_layout = QHBoxLayout(transport)
        transport_layout.setContentsMargins(9, 5, 9, 5)
        transport_layout.setSpacing(6)

        self.prev_btn = MediaButton("prev", P_TEXT_DIM, P_TEXT, icon_size=18)
        self.prev_btn.setFixedSize(32, 32)
        self.prev_btn.setStyleSheet(_SKIP_BTN_QSS)
        self.prev_btn.setToolTip("上一首（←）")
        self.prev_btn.clicked.connect(self.playback.previous)
        transport_layout.addWidget(self.prev_btn)

        # 陶色播放键：赤陶渐变圆面 + 柔光（GlowPlayButton 自绘）
        # 48px 按钮出 42px 圆面（设计稿 t-play），余下 3px 给光环柔光
        self.play_btn = GlowPlayButton(
            "play", P_WHITE, P_ACCENT_LIGHT, P_ACCENT, P_ACCENT,
            icon_size=20, face_ratio=0.4375,
        )
        self.play_btn.setFixedSize(48, 48)
        self.play_btn.setStyleSheet(_PLAY_BTN_QSS)
        self.play_btn.setToolTip("播放 / 暂停（空格）")
        self.play_btn.clicked.connect(self.playback.toggle)
        transport_layout.addWidget(self.play_btn)

        self.next_btn = MediaButton("next", P_TEXT_DIM, P_TEXT, icon_size=18)
        self.next_btn.setFixedSize(32, 32)
        self.next_btn.setStyleSheet(_SKIP_BTN_QSS)
        self.next_btn.setToolTip("下一首（→）")
        self.next_btn.clicked.connect(self.playback.next)
        transport_layout.addWidget(self.next_btn)
        secondary_layout.addWidget(transport)
        secondary_layout.addStretch()

        # 右簇（设计稿顺序）：倍速胶囊 → 音量胶囊 → 功能图标按钮
        self.speed_btn = QPushButton("1.0x")
        self.speed_btn.setStyleSheet(speed_chip_qss())
        self.speed_btn.setToolTip("播放倍速")
        self.speed_btn.clicked.connect(self._cycle_speed)
        secondary_layout.addWidget(self.speed_btn)

        # 音量胶囊：Fixed 防止 HBox 把它拉宽（sizeHint 113 曾被拉到 181px）
        self.vol_pill = QFrame()
        self.vol_pill.setStyleSheet(vol_pill_qss())
        self.vol_pill.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._soft_shadow(self.vol_pill)
        vol_layout = QHBoxLayout(self.vol_pill)
        vol_layout.setContentsMargins(12, 5, 12, 5)
        vol_layout.setSpacing(8)
        vol_icon = QLabel()
        vol_icon.setPixmap(glyph_icon("vol", P_TEXT_DIM, 15).pixmap(15, 15))
        vol_icon.setStyleSheet("background: transparent; border: none;")
        vol_icon.setFixedSize(15, 15)  # 喇叭图曾被拉到 83px 宽
        vol_layout.addWidget(vol_icon)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setFixedWidth(64)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(DEFAULT_VOLUME)
        self.volume_slider.setStyleSheet(slider_qss())
        self.volume_slider.valueChanged.connect(self.playback.set_volume)
        vol_layout.addWidget(self.volume_slider)
        secondary_layout.addWidget(self.vol_pill)

        self.repeat_btn = QPushButton()
        self.repeat_btn.setFixedSize(32, 32)
        self.repeat_btn.setStyleSheet(util_btn_qss())
        self.repeat_btn.setIcon(glyph_icon("repeat", P_TEXT_DIM, 15))
        self.repeat_btn.setIconSize(QSize(15, 15))
        self.repeat_btn.clicked.connect(self.playback.cycle_repeat_mode)
        secondary_layout.addWidget(self.repeat_btn)

        self.open_btn = QPushButton()
        self.open_btn.setFixedSize(32, 32)
        self.open_btn.setStyleSheet(util_btn_qss())
        self.open_btn.setIcon(glyph_icon("folder", P_TEXT_DIM, 15))
        self.open_btn.setIconSize(QSize(15, 15))
        self.open_btn.setToolTip("打开文件…")
        self.open_btn.clicked.connect(self._open_files)
        secondary_layout.addWidget(self.open_btn)

        hint = "Esc 关闭窗口" if self.standalone else "Esc 隐藏窗口"
        self.status_label = QLabel(f"空格 播放/暂停 · ← → 切歌 · {hint}")
        self.status_label.setStyleSheet(f"color: {P_TEXT_FAINT}; font-size: 10px;"
                                        " background: transparent; border: none;")
        secondary_layout.addWidget(self.status_label)

        controls_layout.addWidget(self.secondary_row)
        root.addWidget(self.controls)

        # ---- 迷你态：44px 封面缩略 + 曲名时间 + 传输胶囊 + 圆角细进度 ----
        self.mini_body = QWidget()
        self.mini_body.setVisible(False)
        # 纵向 Fixed：过渡动画期间窗口还很高，默认 Preferred 会把迷你行拉到整窗
        # 居中（内容看起来先往下滑一下）。Fixed 让迷你行紧贴顶栏、窗口自己往下收。
        self.mini_body.setSizePolicy(QSizePolicy.Policy.Preferred,
                                     QSizePolicy.Policy.Fixed)
        mini_layout = QHBoxLayout(self.mini_body)
        # 紧凑排版：组间 8px、四周 12/10（反馈 2026-10-22，原 14/16 太空）
        mini_layout.setContentsMargins(12, 10, 12, 10)
        mini_layout.setSpacing(8)

        self.mini_cover = QLabel()
        self.mini_cover.setFixedSize(_MINI_COVER, _MINI_COVER)
        self.mini_cover.setStyleSheet(mini_cover_qss())
        mini_layout.addWidget(self.mini_cover, 0, Qt.AlignmentFlag.AlignVCenter)

        # 曲名列：只有曲名（时间已移到进度条两端），整列与进度块同中线
        mini_info = QWidget()
        mini_info.setFixedWidth(130)
        info_layout = QVBoxLayout(mini_info)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(0)
        self.mini_title = QLabel("未播放")
        # 单行曲名没有竖排拉伸余量：sizeHint 13px < 12px 字体墨盒 14px，
        # 必须用显式度量顶住，否则字形头脚被裁（守卫 test_no_visible_label_clips_glyphs）
        mini_title_font = QFont()
        mini_title_font.setPixelSize(12)
        mini_title_font.setWeight(QFont.Weight.Medium)
        self.mini_title.setFont(mini_title_font)
        self.mini_title.setMinimumHeight(QFontMetrics(mini_title_font).height())
        self.mini_title.setStyleSheet(f"color: {P_TEXT}; font-size: 12px; font-weight: 500;"
                                      " background: transparent; border: none;")
        info_layout.addWidget(self.mini_title)
        mini_layout.addWidget(mini_info, 0, Qt.AlignmentFlag.AlignVCenter)

        mini_transport = QFrame()
        # 迷你胶囊 50px 高 → 半高 25（超半高的圆角 Qt 会退化成直角）。
        # 纵向 Fixed 防止布局把胶囊拉高、圆角比例失效。
        mini_transport.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        mini_transport.setStyleSheet(transport_pill_qss(radius=25))
        mini_transport_layout = QHBoxLayout(mini_transport)
        mini_transport_layout.setContentsMargins(6, 3, 6, 3)
        mini_transport_layout.setSpacing(4)

        self.mini_prev = MediaButton("prev", P_TEXT_DIM, P_TEXT, icon_size=13)
        self.mini_prev.setFixedSize(28, 28)
        self.mini_prev.setStyleSheet(_SKIP_BTN_QSS)
        self.mini_prev.setToolTip("上一首（←）")
        self.mini_prev.clicked.connect(self.playback.previous)
        mini_transport_layout.addWidget(self.mini_prev)

        # 42px 按钮出 36px 圆面（设计稿迷你态 t-play）
        self.mini_play = GlowPlayButton(
            "play", P_WHITE, P_ACCENT_LIGHT, P_ACCENT, P_ACCENT,
            icon_size=15, face_ratio=0.4286,
        )
        self.mini_play.setFixedSize(42, 42)
        self.mini_play.setStyleSheet(_PLAY_BTN_QSS)
        self.mini_play.setToolTip("播放 / 暂停（空格）")
        self.mini_play.clicked.connect(self.playback.toggle)
        mini_transport_layout.addWidget(self.mini_play)

        self.mini_next = MediaButton("next", P_TEXT_DIM, P_TEXT, icon_size=13)
        self.mini_next.setFixedSize(28, 28)
        self.mini_next.setStyleSheet(_SKIP_BTN_QSS)
        self.mini_next.setToolTip("下一首（→）")
        self.mini_next.clicked.connect(self.playback.next)
        mini_transport_layout.addWidget(self.mini_next)
        mini_layout.addWidget(mini_transport, 0, Qt.AlignmentFlag.AlignVCenter)

        # 进度块：进度条在上，时间在下（当前进度贴头、总时长贴尾）
        seek_block = QWidget()
        seek_block_layout = QVBoxLayout(seek_block)
        seek_block_layout.setContentsMargins(0, 0, 0, 0)
        seek_block_layout.setSpacing(1)

        self.mini_seek = SeekSlider(Qt.Orientation.Horizontal)
        self.mini_seek.setFixedHeight(16)
        self.mini_seek.setRange(0, 0)
        self.mini_seek.setStyleSheet(mini_slider_qss())
        self.mini_seek.clickedSeek.connect(self._seek_to_second)
        self.mini_seek.seekPreview.connect(self._on_seek_preview)
        seek_block_layout.addWidget(self.mini_seek)

        time_row = QHBoxLayout()
        time_row.setContentsMargins(0, 0, 0, 0)
        time_row.setSpacing(0)
        time_qss = (f"color: {P_TEXT_DIM}; font-size: 10px;"
                    " font-family: Consolas, 'Segoe UI';"
                    " background: transparent; border: none;")
        self.mini_time_cur = QLabel("00:00")
        self.mini_time_cur.setStyleSheet(time_qss)
        self.mini_time_total = QLabel("00:00")
        self.mini_time_total.setStyleSheet(time_qss)
        time_row.addWidget(self.mini_time_cur)
        time_row.addStretch()
        time_row.addWidget(self.mini_time_total)
        seek_block_layout.addLayout(time_row)

        mini_layout.addWidget(seek_block, 1, Qt.AlignmentFlag.AlignVCenter)

        root.addWidget(self.mini_body)

        # 尾部弹性占位：过渡动画期间窗口还很高，而迷你态各子项都是不扩张的，
        # QBoxLayout 会把多出来的高度平摊到每一项（顶栏被推到半空、内容漂在中间）。
        # 占位项吃掉全部多余空间，顶栏钉在顶上、窗口自己往下收。常规态 body 的
        # stretch=1 会优先拿走全部空间，这里拿 0，不影响完整布局。
        root.addStretch(0)

    def _soft_shadow(self, widget, blur=12, dy=2, alpha=18):
        """卡片的柔阴影（设计稿 box-shadow: 0 2px 12px rgba(120,90,60,0.05~0.06)）"""
        shadow = QGraphicsDropShadowEffect(widget)
        shadow.setBlurRadius(blur)
        shadow.setOffset(0, dy)
        shadow.setColor(QColor(120, 90, 60, alpha))
        widget.setGraphicsEffect(shadow)

    def _set_pill_icon(self, button, kind):
        """胶囊按钮的小图标：未选中深咖字色，选中（赤陶底）换白色"""
        color = P_WHITE if button.isChecked() else P_TEXT_BTN
        button.setIcon(glyph_icon(kind, color, 13))
        button.setIconSize(QSize(13, 13))

    def _refresh_mini_time(self):
        # 时间贴在进度条两端：当前进度在头、总时长在尾
        self.mini_time_cur.setText(format_time(self._current_sec))
        self.mini_time_total.setText(format_time(self._total_sec))

    def _set_crumb(self, artist, title):
        """顶栏面包屑：艺术家（深咖加粗）/ 曲名（灰）"""

        def esc(text):
            return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        parts = []
        if artist:
            parts.append(f'<span style="color:{P_TEXT}; font-weight:500">{esc(artist)}</span>'
                         f'<span style="color:{P_TEXT_DIM}"> / </span>')
        parts.append(f'<span style="color:{P_TEXT_DIM}">{esc(title)}</span>')
        self.header_title.setText("".join(parts))

    # ==================== 绑定 ====================
    def _bind_controller(self):
        self.playback.trackChanged.connect(self._on_track_changed)
        self.playback.metadataChanged.connect(self._on_metadata)
        self.playback.positionChanged.connect(self._on_position_changed)
        self.playback.durationChanged.connect(self._on_duration_changed)
        self.playback.stateChanged.connect(self._on_state_changed)
        self.playback.volumeChanged.connect(self._on_volume_changed)
        self.playback.repeatModeChanged.connect(self._on_repeat_changed)
        self.playback.playbackRateChanged.connect(self._on_rate_changed)
        self.playback.queueChanged.connect(self._on_queue_changed)
        self.playback.errorOccurred.connect(self._on_error)

        self._on_volume_changed(self.playback.volume)
        self._on_repeat_changed(self.playback.repeat_mode)
        self._on_rate_changed(self.playback.playback_rate)
        self._on_queue_changed(self.playback.queue)
        if self.playback.duration() > 0:
            self._on_duration_changed(self.playback.duration())
        if self.playback.current_track:
            self._on_track_changed(self.playback.current_track)

    def _setup_shortcuts(self):
        QShortcut(QKeySequence("Space"), self, self.playback.toggle)
        QShortcut(QKeySequence("Left"), self, self.playback.previous)
        QShortcut(QKeySequence("Right"), self, self.playback.next)
        QShortcut(QKeySequence("Escape"), self, self._on_escape)

    def _on_escape(self):
        """独立模式直接关窗退出；嵌在主界面里的播放器只隐藏"""
        if self.standalone:
            self.close()
        else:
            self.hide()

    # ==================== 控制器回调 ====================
    def _on_track_changed(self, track):
        self._track = dict(track or {})
        title = self._track.get("title") or os.path.basename(self._track.get("path") or "")
        title = title or "未播放"
        self.title_label.setText(title)
        self.mini_title.setText(_elide(title, _MINI_TITLE_MAX))
        self.mini_title.setToolTip(title)
        self._set_crumb("", title)
        self.setWindowTitle(f"{title} · 播放器")
        self._load_cover(self._track)
        self._load_subtitle(self._track)
        self._update_meta_label()
        self._highlight_queue()

    def _on_metadata(self, data):
        artist = (data or {}).get("artist") or ""
        album = (data or {}).get("album") or ""
        self.artist_label.setText(artist or "未知艺术家")
        self.album_label.setText(album)
        if (data or {}).get("title"):
            self.title_label.setText(data["title"])
            self.mini_title.setText(_elide(data["title"], _MINI_TITLE_MAX))
            self.mini_title.setToolTip(data["title"])
            self.setWindowTitle(f"{data['title']} · 播放器")
        self._set_crumb(artist, self.title_label.text())
        # 文件夹里没找到封面时，用音频文件自带的封面（ID3 APIC / FLAC PICTURE）
        if not self._cover_from_file and getattr(config, "PLAYER_EMBEDDED_COVER", True):
            image = (data or {}).get("cover")
            if image is not None and not image.isNull():
                self._apply_cover_image(image)

    def _on_position_changed(self, position_ms):
        seconds = int(position_ms) // 1000
        self._current_sec = seconds
        self.time_current.setText(format_time(seconds))
        self._refresh_mini_time()
        if not self.progress.is_seeking:
            self.progress.setValue(seconds)
        if not self.mini_seek.is_seeking:
            self.mini_seek.setValue(seconds)

    def _on_duration_changed(self, duration_ms):
        seconds = int(duration_ms) // 1000
        self._total_sec = seconds
        self.progress.setRange(0, seconds)
        self.mini_seek.setRange(0, seconds)
        self.time_total.setText(format_time(seconds))
        self._refresh_mini_time()

    def _on_state_changed(self, playing):
        kind = "pause" if playing else "play"
        tip = "暂停（空格）" if playing else "播放（空格）"
        for button in (self.play_btn, self.mini_play):
            button.set_kind(kind)
            button.setToolTip(tip)

    def _on_volume_changed(self, volume):
        if self.volume_slider.value() == volume:
            return
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(volume)
        self.volume_slider.blockSignals(False)

    def _on_repeat_changed(self, mode):
        label = REPEAT_LABELS.get(mode, "列表循环")
        self.repeat_btn.setToolTip(f"循环模式：{label}（点击切换）")
        icon_color = P_TEXT_DIM if mode == "list" else P_ACCENT
        self.repeat_btn.setIcon(glyph_icon("repeat", icon_color, 15))

    def _on_rate_changed(self, rate):
        self.speed_btn.setText(f"{rate:g}x")

    def _on_queue_changed(self, queue):
        self._queue = list(queue or [])
        self.queue_btn.setText(f"队列 {len(self._queue)}")
        self.queue_count.setText(str(len(self._queue)))
        self._rebuild_queue_list()

    def _rebuild_queue_list(self):
        """队列行 = 序号 + 曲名 +（播放中：均衡器小条 / 否则：格式）"""
        current_path = (self.playback.current_track or {}).get("path")
        self.queue_list.clear()
        for index, track in enumerate(self._queue):
            title = track.get("title") or os.path.basename(track.get("path") or "")
            path = track.get("path")
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path or "")
            item.setSizeHint(QSize(0, 34))
            self.queue_list.addItem(item)
            self.queue_list.setItemWidget(
                item, self._queue_row_widget(index, title, path, path == current_path))

    def _queue_row_widget(self, index, title, path, playing):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        num = QLabel(f"{index + 1:02d}")
        num.setFixedWidth(18)
        num.setStyleSheet(f"color: {P_ACCENT if playing else P_TEXT_FAINT};"
                          " font-size: 10px; background: transparent; border: none;")
        layout.addWidget(num)

        name = QLabel(_elide(title, _QUEUE_NAME_MAX))
        weight = " font-weight: 500;" if playing else ""
        name.setStyleSheet(f"color: {P_TEXT if playing else P_TEXT_DIM};"
                           f" font-size: 12px;{weight}"
                           " background: transparent; border: none;")
        name.setToolTip(title)  # 截断后悬停可见全名
        layout.addWidget(name, 1)

        if playing:
            row.setStyleSheet(f"background-color: {P_WHITE}; border: none; border-radius: 8px;")
            eq = EqBars()
            eq.setStyleSheet("background: transparent; border: none;")
            layout.addWidget(eq)
        else:
            row.setStyleSheet("background: transparent; border: none;")
            ext = os.path.splitext(path or "")[1].lstrip(".").upper()
            tail = QLabel(ext)
            tail.setStyleSheet(f"color: {P_TEXT_FAINT}; font-size: 10px;"
                               " background: transparent; border: none;")
            layout.addWidget(tail)
        return row

    def _on_error(self, message):
        self.status_label.setText(f"播放错误：{message}"[:80])

    def _tick_subtitle(self):
        if self.isVisible() and self.playback.current_track:
            self.subtitle_view.set_position(self.playback.position())

    # ==================== 字幕 / 封面 ====================
    def _load_subtitle(self, track):
        path = subs.find_subtitle(track.get("path"))
        self._subtitle_path = path
        subtitle = subs.load_subtitle(path, self.playback.duration()) if path else None
        self.subtitle_view.set_track(subtitle, self.playback.position())
        offset = 0
        if path:
            try:
                offset = db.get_subtitle_offset(track.get("path"))
            except Exception:
                offset = 0
        self.subtitle_view.set_offset(offset, emit=False)
        self.subtitle_view.set_position(self.playback.position(), force=True)

    def _save_subtitle_offset(self, offset_ms):
        path = (self._track or {}).get("path")
        if not path:
            return
        try:
            db.set_subtitle_offset(path, offset_ms)
        except Exception as e:
            print(f"[字幕] 保存偏移失败: {e}")

    def _load_cover(self, track):
        """按优先级装载封面：歌单封面 → 文件夹常见命名 → DLsite 缓存

        三条都没有时先清空；**内嵌封面要等媒体元数据解析完才到**，由
        `_on_metadata` 补上（`self._cover_from_file` 标记当前是否已是文件封面，
        避免内嵌封面反过来盖掉更清晰的作品封面）。
        """
        path = track.get("path") or ""
        cover = None
        playlist_id = track.get("playlist_id")
        if playlist_id:
            try:
                playlist = db.get_playlist(playlist_id)
                cover = (playlist or {}).get("cover")
            except Exception:
                cover = None
        if not (cover and os.path.isfile(cover)):
            cover = folder_cover(os.path.dirname(path))
        if not cover:
            cover = dlsite_cover(track)

        self._cover_from_file = bool(cover) and self._apply_cover_file(cover)
        if not self._cover_from_file:
            self._clear_cover()

    def _apply_cover_file(self, cover):
        """从图片文件读封面；读不出来返回 False（调用方继续向下兜底）"""
        reader = QImageReader(cover)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid():
            reader.setScaledSize(size.scaled(_COVER_SIZE * 2, _COVER_SIZE * 2,
                                             Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if image.isNull():
            return False
        self._apply_cover_image(image)
        return True

    def _apply_cover_image(self, image):
        """把 QImage 填进主封面与迷你封面（两处同源，保证切换迷你态不闪）"""
        pixmap = QPixmap.fromImage(image).scaled(
            _COVER_SIZE, _COVER_SIZE,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.cover_label.setText("")
        self.cover_label.setPixmap(pixmap)
        # 迷你态用同一张封面缩到 44px（KeepAspectRatioByExpanding + 裁切成方块）
        self.mini_cover.setPixmap(pixmap.scaled(
            _MINI_COVER, _MINI_COVER,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def _clear_cover(self):
        self.cover_label.setPixmap(QPixmap())
        self.cover_label.setText("无封面")
        self.mini_cover.setPixmap(QPixmap())

    def _update_meta_label(self):
        path = (self._track or {}).get("path") or ""
        ext = os.path.splitext(path)[1].lstrip(".").upper()
        self.meta_label.setText(ext or "AUDIO")
        self.meta_queue_label.setText(f"队列 {len(self.playback.queue)} 首")

    def _highlight_queue(self):
        self._rebuild_queue_list()
        current_path = (self.playback.current_track or {}).get("path")
        if not current_path:
            return
        for row in range(self.queue_list.count()):
            item = self.queue_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == current_path:
                self.queue_list.setCurrentRow(row)
                self.queue_list.scrollToItem(
                    item, QAbstractItemView.ScrollHint.EnsureVisible
                )
                return

    # ==================== 交互 ====================
    def _on_seek_preview(self, seconds):
        """拖动预览：只更新时间与字幕高亮，绝不调用 seek"""
        self._current_sec = int(seconds)
        self.time_current.setText(format_time(int(seconds)))
        self._refresh_mini_time()
        self.subtitle_view.set_position(int(seconds) * 1000, force=True)

    def _seek_to_second(self, seconds):
        """手势结束后的唯一一次真实跳转"""
        self.playback.seek(int(seconds) * 1000)
        self.subtitle_view.set_position(int(seconds) * 1000, force=True)

    def _play_queue_row(self, item):
        row = self.queue_list.row(item)
        if row >= 0:
            self.playback.play_index(row)

    def _cycle_speed(self):
        current = self.playback.playback_rate
        try:
            index = _SPEEDS.index(current)
        except ValueError:
            index = 0
        self.playback.set_playback_rate(_SPEEDS[(index + 1) % len(_SPEEDS)])

    def _open_files(self):
        start = os.path.dirname((self._track or {}).get("path") or "") or os.path.expanduser("~")
        extensions = " ".join(f"*{ext}" for ext in sorted(AUDIO_EXTENSIONS))
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择音频文件", start, f"音频文件 ({extensions});;所有文件 (*.*)"
        )
        if not paths:
            return
        tracks = tracks_from_paths(paths)
        if tracks:
            self.playback.play_tracks(tracks, 0)
            self.show_player()

    def _toggle_queue(self, checked):
        self.queue_panel.setVisible(checked)

    def _toggle_on_top(self, checked):
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, checked)
        self.show()

    def _clamp_to_screen(self, rect):
        """把窗口矩形夹回屏幕可用区：缩小再放大不会把窗口挤出屏幕外"""
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return rect
        area = screen.availableGeometry()
        out = QRect(rect)
        if out.right() > area.right():
            out.moveRight(area.right())
        if out.bottom() > area.bottom():
            out.moveBottom(area.bottom())
        if out.left() < area.left():
            out.moveLeft(area.left())
        if out.top() < area.top():
            out.moveTop(area.top())
        return out

    def _animate_geometry(self, target, on_finished=None):
        """把窗口几何平滑过渡到 target（迷你↔完整态切换用）

        直接 resize 是一次硬跳变，看起来生硬（用户反馈 2026-10-22）；改成一边
        移动一边缩放，缓出曲线开头快、落点稳，视觉上像窗口「收」进顶栏。
        `_animate=False`（测试）时退化为立刻 setGeometry，保证断言同步生效。
        """
        if self._geo_anim is not None:
            with suppress(RuntimeError):
                self._geo_anim.stop()
            self._geo_anim = None
        if not self._animate or not self.isVisible() or self.geometry() == target:
            self.setGeometry(target)
            if on_finished is not None:
                on_finished()
            return
        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(_MINI_TRANSITION_MS)
        anim.setStartValue(self.geometry())
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        if on_finished is not None:
            anim.finished.connect(on_finished)
        # 必须留引用：动画对象被 GC 掉，窗口就停在起点不动了
        self._geo_anim = anim
        anim.start()

    def _toggle_mini(self, checked):
        self._mini = checked
        # 迷你态只留：顶栏 + 封面缩略/曲名时间/传输/进度（网页版 C 的迷你态）
        # controls 容器要整体隐藏（含上下边距 32px），否则 mini_body 被压扁，
        # 42px 播放键溢出 32px 高的传输胶囊，光晕被齐底截成「方形光斑」。
        # 子控件仍单独显隐：isHidden() 不跟随父容器，测试与恢复都依赖显式状态。
        cur = self.geometry()
        if checked:
            self._normal_size = self.size()
        self.body.setVisible(not checked)
        self.controls.setVisible(not checked)
        self.seek_card.setVisible(not checked)
        self.secondary_row.setVisible(not checked)
        self.mini_body.setVisible(checked)
        if checked:
            # 迷你态顶栏收薄：52px 高只放 25px 胶囊，中间大片留白显得空旷。
            # 压到 42px + 边距 14 / 间距 8，整条顶栏变紧凑的工具条。
            self.header.setFixedHeight(_MINI_HEADER_H)
            self.header.layout().setContentsMargins(*_MINI_HEADER_MARGINS)
            self.header.layout().setSpacing(_MINI_HEADER_SPACING)
            # 迷你态锁定纵向尺寸：窗高 = 顶栏 + 迷你行自然高度。不锁的话窗口能
            # 被上下拖成一大条空白（内容散落一整屏，用户反馈 2026-10-22）。
            # 横向仍可自由拉伸；高度固定后胶囊半高圆角比例也不会被破坏。
            mini_h = self.header.minimumHeight() + self.mini_body.sizeHint().height()
            # 尺寸约束分两段下：过渡期间只抬下限（下限是下界，620→mini_h 一路都
            # 合法），落点后再锁上限。若一开始就锁 max，窗口会瞬间压扁、动画失效。
            self.setMinimumHeight(mini_h)
            self.setMaximumHeight(QWIDGETSIZE_MAX)
            # 左上角为锚点：进出两趟的 x/y 不变，来回切不会横向漂移
            target = self._clamp_to_screen(
                QRect(cur.x(), cur.y(), min(cur.width(), _MINI_W), mini_h))
            self._animate_geometry(target, lambda: self.setMaximumHeight(mini_h))
        else:
            # 退出迷你态：顶栏恢复常规尺寸 + 撤销固定高度
            self.header.setFixedHeight(_HEADER_H)
            self.header.layout().setContentsMargins(*_HEADER_MARGINS)
            self.header.layout().setSpacing(_HEADER_SPACING)
            # 下限暂时留在迷你高度（112px）：立即还原 _WIN_MIN_H 会把动画起点
            # 当场顶到 140px，开头一顿；等过渡结束再还回常规最小高度。
            self.setMaximumHeight(QWIDGETSIZE_MAX)
            target = self._clamp_to_screen(
                QRect(cur.x(), cur.y(), self._normal_size.width(),
                      self._normal_size.height()))
            self._animate_geometry(target, lambda: self.setMinimumHeight(_WIN_MIN_H))

    # ==================== 窗口生命周期 ====================
    def set_compact(self, compact=True):
        """进入/退出迷你布局；独立播放默认用迷你布局（小窗口）"""
        if self.mini_btn.isChecked() != bool(compact):
            self.mini_btn.setChecked(bool(compact))
        else:
            self._toggle_mini(bool(compact))

    def _center_on_screen(self):
        """打开时把窗口摆在主屏可用区域正中（迷你态也居中，用户反馈 2026-10-22）"""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        frame = self.frameGeometry()
        frame.moveCenter(area.center())
        self.move(frame.topLeft())

    def show_player(self):
        self._apply_player_prefs()
        if self.isMinimized():
            self.showNormal()
        self.show()
        if not self._positioned:
            self._positioned = True
            self._center_on_screen()
        self.raise_()
        self.activateWindow()

    def _sync_on_top_flag(self):
        """按设置同步置顶状态（不主动显示窗口，构造期也能安全调用）"""
        want = bool(getattr(config, "PLAYER_ON_TOP", False))
        if want != bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint):
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, want)
        # 按钮状态跟着走，但别触发 _toggle_on_top（那里面会 show 窗口）
        self.on_top_btn.blockSignals(True)
        self.on_top_btn.setChecked(want)
        self.on_top_btn.blockSignals(False)
        self._set_pill_icon(self.on_top_btn, "pin")

    def _apply_player_prefs(self):
        """打开播放器前套用「音频设置 → 播放器」里的选项

        允许用户手动收起队列/取消置顶：这里只在设置要求开启时展开，
        不会因为设置为关就强行把用户已展开的队列收回去。
        """
        self._sync_on_top_flag()
        if getattr(config, "PLAYER_AUTO_QUEUE", False) and not self.queue_btn.isChecked():
            self.queue_btn.setChecked(True)

    def showEvent(self, event):
        super().showEvent(event)
        self._subtitle_timer.start()

    def hideEvent(self, event):
        self._subtitle_timer.stop()
        super().hideEvent(event)

    def shutdown(self):
        """退出程序时真正关闭窗口"""
        self._allow_close = True
        self._subtitle_timer.stop()
        for signal, slot in (
            (self.playback.trackChanged, self._on_track_changed),
            (self.playback.metadataChanged, self._on_metadata),
            (self.playback.positionChanged, self._on_position_changed),
            (self.playback.durationChanged, self._on_duration_changed),
            (self.playback.stateChanged, self._on_state_changed),
            (self.playback.queueChanged, self._on_queue_changed),
        ):
            with suppress(TypeError):
                signal.disconnect(slot)
        self.close()

    def _other_visible_window_exists(self):
        """除自己外是否还有别的可见顶层窗口（最小化也算可见）"""
        return any(
            widget is not self and widget.isVisible()
            for widget in QApplication.topLevelWidgets()
        )

    def closeEvent(self, event):
        # 独立播放：关闭窗口即结束进程（Qt 退出后 cleanup 会释放媒体）
        if self._allow_close or self.standalone:
            self._allow_close = True
            super().closeEvent(event)
            return
        # 非独立模式：主界面还在（有别的可见窗口）→ 只隐藏，后台继续播、随时再打开。
        # 但它是最后一个可见窗口时必须真关：只 hide 会让 quitOnLastWindowClosed
        # 永不触发——窗口从任务栏消失而进程驻留，单实例锁还挡住下次启动，
        # 只能进任务管理器杀进程（v1.8.5 修复的用户反馈）。
        if self._other_visible_window_exists():
            event.ignore()
            self.hide()
            return
        self._allow_close = True
        super().closeEvent(event)

    # ==================== 拖放 ====================
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        tracks = tracks_from_paths(paths)
        if tracks:
            self.playback.play_tracks(tracks, 0)
            self.show_player()
            event.acceptProposedAction()


__all__ = ["PlayerWindow", "tracks_from_paths"]
