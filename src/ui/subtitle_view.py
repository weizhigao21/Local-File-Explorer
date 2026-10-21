"""
字幕滚动视图

只负责「显示 + 跟随」：
    - 当前句高亮并滚动到视口中间；
    - 其余句子淡化，长句自动换行；
    - 用户手动滚动后暂停自动跟随 3.5 秒，避免看前面台词时被强行拉回；
    - 支持字号调整与 ±0.5 秒偏移（偏移量由外部持久化）。
解析与定位全部交给 audio_manager.subtitles，本控件不碰文件。
"""
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from audio_manager.subtitles import SubtitleTrack
from ui.audio_theme import (
    ACCENT,
    BORDER_COLOR,
    CARD_BG,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
)
from ui.media_icons import glyph_icon

# 默认浅色主题（主界面米色系）；播放器窗口会传入 ui.player_theme.subtitle_theme()
_LIGHT_THEME = {
    "card_bg": CARD_BG,
    "card_edge": "#E8DECB",
    "text": TEXT_PRIMARY,
    "dim": TEXT_DIM,
    "muted": TEXT_MUTED,
    "accent": ACCENT,
    "accent_soft": (184, 92, 62, 28),
    "btn_edge": BORDER_COLOR,
    "leaf_bg": "#F0ECE5",
}


def _small_btn_qss(theme):
    return f"""
QPushButton {{
    background: transparent; color: {theme["muted"]};
    border: 1px solid {theme["btn_edge"]}; border-radius: 4px;
    padding: 2px 6px; font-size: 11px;
}}
QPushButton:hover {{ color: {theme["text"]}; border-color: {theme["accent"]}; }}
QPushButton:disabled {{ color: {theme["dim"]}; border-color: {theme["btn_edge"]}; }}
"""

_FOLLOW_RESUME_MS = 3500
_OFFSET_STEP_MS = 500
_MAX_OFFSET_MS = 60000
_FILE_LABEL_MAX = 104  # 文件名标签的省略宽（窄卡里防挤掉右侧按钮前缀）


def _elide(text, width, pixel_size=11):
    """QLabel 不会自动加省略号，超长文本必须用 QFontMetrics 截断"""
    font = QFont()
    font.setPixelSize(pixel_size)
    return QFontMetrics(font).elidedText(text or "", Qt.TextElideMode.ElideRight, width)


class _SubtitleList(QListWidget):
    """字幕列表：滚轮/拖滚动条视为用户接管，通知外部暂停跟随"""

    userScrolled = pyqtSignal()

    def wheelEvent(self, event):
        self.userScrolled.emit()
        super().wheelEvent(event)


class SubtitleView(QFrame):
    """字幕区：轨道加载、当前句高亮、字号与偏移控制"""

    offsetChanged = pyqtSignal(int)

    def __init__(self, parent=None, theme=None):
        super().__init__(parent)
        self._track = None
        self._offset_ms = 0
        self._last_position_ms = 0
        self._current_row = -1
        self._follow = True
        self._font_size = 15
        self._theme = dict(_LIGHT_THEME, **(theme or {}))
        t = self._theme

        self.setStyleSheet(
            f"SubtitleView {{ background-color: {t['card_bg']}; border: 1px solid {t['card_edge']};"
            f" border-radius: 8px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(8)  # 设计稿 sub-head gap: 8px

        # 设计稿 sub-head：18px 圆形 leaf 标（浅底 + 10px 赤陶图标）+ 文字
        leaf = QLabel()
        leaf.setFixedSize(18, 18)
        leaf.setAlignment(Qt.AlignmentFlag.AlignCenter)
        leaf_bg = t.get("leaf_bg", t["card_edge"])
        leaf.setStyleSheet(
            f"QLabel {{ background-color: {leaf_bg}; border: none; border-radius: 9px; }}"
        )
        leaf.setPixmap(glyph_icon("plus", t["accent"], 10).pixmap(10, 10))
        header.addWidget(leaf)

        title = QLabel("字幕同步")
        title.setStyleSheet(f"color: {t['dim']}; font-size: 11px; border: none;"
                            " background: transparent;")
        title.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        header.addWidget(title)

        self.file_label = QLabel("未找到字幕")
        self.file_label.setStyleSheet(f"color: {t['dim']}; font-size: 11px; border: none;"
                                        " background: transparent;")
        self.file_label.setMinimumWidth(0)  # 允许被压缩，省略号收尾
        header.addWidget(self.file_label, 1)
        header.addStretch(0)

        # 右侧控制簇：组内间距 4，防止窄卡总宽溢出把「-0.5s」前缀挤掉
        controls = QHBoxLayout()
        controls.setSpacing(4)
        self.offset_label = QLabel("偏移 +0.0s")
        self.offset_label.setStyleSheet(f"color: {t['dim']}; font-size: 11px; border: none;"
                                         " background: transparent;")
        self.offset_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        controls.addWidget(self.offset_label)

        self._font_minus = self._make_button("A-", "缩小字号")
        self._font_minus.clicked.connect(lambda: self.set_font_size(self._font_size - 1))
        controls.addWidget(self._font_minus)

        self._font_plus = self._make_button("A+", "放大字号")
        self._font_plus.clicked.connect(lambda: self.set_font_size(self._font_size + 1))
        controls.addWidget(self._font_plus)

        offset_minus = self._make_button("-0.5s", "字幕提前 0.5 秒")
        offset_minus.clicked.connect(lambda: self._nudge_offset(-_OFFSET_STEP_MS))
        controls.addWidget(offset_minus)

        offset_plus = self._make_button("+0.5s", "字幕延后 0.5 秒")
        offset_plus.clicked.connect(lambda: self._nudge_offset(_OFFSET_STEP_MS))
        controls.addWidget(offset_plus)

        offset_reset = self._make_button("↺", "偏移归零")
        offset_reset.clicked.connect(lambda: self.set_offset(0))
        controls.addWidget(offset_reset)
        header.addLayout(controls)

        layout.addLayout(header)

        self.list = _SubtitleList()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setWordWrap(True)
        self.list.setSpacing(2)
        self.list.setStyleSheet(
            "QListWidget { background: transparent; border: none; outline: none; }"
            "QListWidget::item { padding: 5px 8px; border-radius: 6px; }"
        )
        self.list.userScrolled.connect(self._pause_follow)
        self.list.verticalScrollBar().sliderPressed.connect(self._pause_follow)
        layout.addWidget(self.list, 1)

        self._follow_timer = QTimer(self)
        self._follow_timer.setSingleShot(True)
        self._follow_timer.setInterval(_FOLLOW_RESUME_MS)
        self._follow_timer.timeout.connect(self._resume_follow)

        self._show_placeholder("未找到字幕")

    # ── 构建/样式 ──

    def _make_button(self, text, tip):
        button = QPushButton(text)
        button.setToolTip(tip)
        button.setStyleSheet(_small_btn_qss(self._theme))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        # Fixed 策略防 HBox 压缩把「-0.5s」前缀挤掉
        button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        return button

    def _set_file_text(self, text):
        """文件名标签：超出省略宽截断（QLabel 不自动 ellipsis），全文放 tooltip"""
        self.file_label.setText(_elide(text, _FILE_LABEL_MAX))
        self.file_label.setToolTip(text)

    def resizeEvent(self, event):
        """窄卡（如 900px 窗口展开队列后）藏文件名标签，保住按钮文字完整"""
        super().resizeEvent(event)
        self.file_label.setVisible(self.width() >= 480)

    @property
    def font_size(self):
        return self._font_size

    @property
    def offset_ms(self):
        return self._offset_ms

    @property
    def has_subtitle(self):
        return bool(self._track and self._track.cues)

    # ── 轨道与定位 ──

    def set_track(self, track: SubtitleTrack | None, position_ms=0):
        """载入字幕轨道；空轨道显示占位提示"""
        self._track = track if track and track.cues else None
        self._current_row = -1
        self._follow = True
        self._follow_timer.stop()
        self.list.clear()

        if not self._track:
            self._set_file_text("未找到字幕（支持 SRT / LRC / VTT / ASS）")
            self.file_label.setToolTip("")
            self.offset_label.setVisible(False)
            self._show_placeholder("未找到字幕文件")
            return

        self.offset_label.setVisible(True)
        self._set_file_text(
            f"{self._track.name} · {self._track.format} · {self._track.encoding}"
        )
        self.file_label.setToolTip(self._track.path)
        for cue in self._track.cues:
            item = QListWidgetItem(cue.text)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.list.addItem(item)
        self._apply_font()
        self.set_position(position_ms, force=True)

    def set_position(self, position_ms, force=False):
        """按播放位置高亮当前句；句间空隙保持上一句，避免闪烁"""
        self._last_position_ms = max(0, int(position_ms))
        if not self._track:
            return
        index = self._track.index_at(self._last_position_ms - self._offset_ms)
        if index < 0:
            return
        if index == self._current_row and not force:
            return
        self._current_row = index
        self._apply_font()
        if self._follow:
            self._scroll_to(index)

    # ── 字号 / 偏移 ──

    def set_font_size(self, size):
        self._font_size = max(10, min(28, int(size)))
        self._apply_font()
        if self._follow and self._current_row >= 0:
            self._scroll_to(self._current_row)

    def set_offset(self, offset_ms, emit=True):
        offset_ms = max(-_MAX_OFFSET_MS, min(_MAX_OFFSET_MS, int(offset_ms)))
        changed = offset_ms != self._offset_ms
        self._offset_ms = offset_ms
        self.offset_label.setText(f"偏移 {offset_ms / 1000:+.1f}s")
        if changed and self._track:
            self._current_row = -1
            self.set_position(self._last_position_ms, force=True)
        if emit and changed:
            self.offsetChanged.emit(offset_ms)

    def _nudge_offset(self, delta_ms):
        self.set_offset(self._offset_ms + delta_ms)

    # ── 内部渲染 ──

    def _show_placeholder(self, text):
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setForeground(QColor(self._theme["dim"]))
        self.list.addItem(item)

    def _apply_font(self):
        for row in range(self.list.count()):
            item = self.list.item(row)
            if not item.flags():
                continue  # 占位项
            font = QFont(self.font())
            font.setPointSize(self._font_size)
            font.setBold(row == self._current_row)
            item.setFont(font)
            if row == self._current_row:
                item.setForeground(QColor(self._theme["accent"]))
                item.setBackground(QColor(*self._theme["accent_soft"]))
            else:
                item.setForeground(QColor(self._theme["muted"]))
                item.setBackground(QColor(0, 0, 0, 0))

    def _scroll_to(self, row):
        item = self.list.item(row)
        if item is not None:
            self.list.scrollToItem(
                item, QAbstractItemView.ScrollHint.PositionAtCenter
            )

    def _pause_follow(self):
        self._follow = False
        self._follow_timer.start()

    def _resume_follow(self):
        self._follow = True
        if self._current_row >= 0:
            self._scroll_to(self._current_row)


__all__ = ["SubtitleView"]
