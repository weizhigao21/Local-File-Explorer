"""
音频模块详情页控件
在打开歌单时展示：封面、歌单信息、可点击标签、子歌单卡片、曲目列表
"""
import os

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QPushButton, QScrollArea,
    QListWidget, QListWidgetItem, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap

from ui.flow_layout import FlowLayout
from ui.tag_widgets import TagButton, parse_tags
from ui.audio_theme import (
    ACCENT_TINT, BG_MAIN, BG_SIDEBAR,
    TEXT_PRIMARY, TEXT_MUTED, TEXT_DIM, BTN_QSS,
)
from ui.audio_widgets import SubPlaylistCard


class PlaylistDetailPage(QWidget):
    """歌单详情页：数据由 AudioMainWindow 组装后通过 display() 渲染"""

    subPlaylistClicked = pyqtSignal(int)
    tagClicked = pyqtSignal(str)
    trackDoubleClicked = pyqtSignal(int)
    backClicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background-color: {BG_MAIN};")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        detail_toolbar = QFrame()
        detail_toolbar.setFixedHeight(40)
        detail_toolbar.setStyleSheet(f"background-color: {BG_SIDEBAR}; border: none;")
        dt_layout = QHBoxLayout(detail_toolbar)
        dt_layout.setContentsMargins(8, 4, 8, 4)
        self.detail_back_btn = QPushButton("← 返回歌单列表")
        self.detail_back_btn.setStyleSheet(BTN_QSS)
        self.detail_back_btn.clicked.connect(self._back_clicked)
        dt_layout.addWidget(self.detail_back_btn)
        dt_layout.addStretch()
        layout.addWidget(detail_toolbar)

        info_scroll = QScrollArea()
        info_scroll.setWidgetResizable(True)
        info_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        info_scroll.setStyleSheet(f"QScrollArea {{ background-color: {BG_MAIN}; border: none; }}")

        info_content = QWidget()
        info_content.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.info_layout = QVBoxLayout(info_content)
        self.info_layout.setContentsMargins(16, 16, 16, 8)
        self.info_layout.setSpacing(8)

        info_row = QHBoxLayout()
        info_row.setSpacing(16)
        self.cover_label = QLabel()
        self.cover_label.setFixedSize(160, 160)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setStyleSheet("background-color: #FFFFFF; border-radius: 6px;")
        self.cover_label.setText("无封面")
        info_row.addWidget(self.cover_label)

        info_col = QVBoxLayout()
        info_col.setSpacing(6)
        self.playlist_title = QLabel()
        self.playlist_title.setWordWrap(True)
        self.playlist_title.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 20px; font-weight: bold;")
        info_col.addWidget(self.playlist_title)
        self.playlist_tags = QLabel()
        self.playlist_tags.setWordWrap(True)
        self.playlist_tags.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        info_col.addWidget(self.playlist_tags)
        # 可点击的标签按钮容器（替代纯文本显示）
        self._tag_buttons_container = QWidget()
        self._tag_buttons_layout = FlowLayout(self._tag_buttons_container, margin=0, h_spacing=6, v_spacing=4)
        self._tag_buttons_container.setContentsMargins(0, 0, 0, 4)
        self._tag_buttons_container.setLayout(self._tag_buttons_layout)
        self._tag_buttons_container.setVisible(False)
        info_col.addWidget(self._tag_buttons_container)
        self.playlist_count = QLabel()
        self.playlist_count.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")
        info_col.addWidget(self.playlist_count)
        info_col.addStretch()
        info_row.addLayout(info_col, 1)
        self.info_layout.addLayout(info_row)

        # 子歌单区域
        self.sub_header = QLabel("子歌单")
        self.sub_header.setStyleSheet(f"color: #BBB; font-size: 13px; font-weight: bold; padding-top: 8px;")
        self.sub_header.setVisible(False)
        self.info_layout.addWidget(self.sub_header)

        self.sub_scroll = QScrollArea()
        self.sub_scroll.setWidgetResizable(True)
        self.sub_scroll.setMaximumHeight(200)
        self.sub_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.sub_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sub_scroll.setStyleSheet("QScrollArea { background-color: transparent; border: none; }")
        self.sub_cards = QWidget()
        self.sub_vbox = QVBoxLayout(self.sub_cards)
        self.sub_vbox.setContentsMargins(0, 0, 0, 0)
        self.sub_vbox.setSpacing(2)
        self.sub_cards.setLayout(self.sub_vbox)
        self.sub_scroll.setWidget(self.sub_cards)
        self.sub_scroll.setVisible(False)
        self.info_layout.addWidget(self.sub_scroll)

        track_header = QLabel("曲目列表")
        track_header.setStyleSheet(f"color: #BBB; font-size: 13px; font-weight: bold; padding-top: 8px;")
        self.track_header = track_header
        self.info_layout.addWidget(self.track_header)

        self.track_list = QListWidget()
        self.track_list.setStyleSheet(f"""
            QListWidget {{
                background-color: {BG_MAIN}; color: #777;
                border: 1px solid #EDE6DA; border-radius: 4px; outline: none;
            }}
            QListWidget::item {{
                padding: 8px 12px; border-bottom: 1px solid #E8E0D5;
            }}
            QListWidget::item:hover {{ background-color: #EDE6DA; color: {TEXT_PRIMARY}; }}
            QListWidget::item:selected {{
                background-color: {ACCENT_TINT}; color: {TEXT_PRIMARY};
            }}
        """)
        self.track_list.itemDoubleClicked.connect(self._on_track_double_clicked)
        self.info_layout.addWidget(self.track_list, 1)

        info_scroll.setWidget(info_content)
        layout.addWidget(info_scroll, 1)

        # 内部状态
        self._tracks_paths = []          # 当前展示列表的曲目 dict（与 track_list 顺序一致）
        self._sub_playlists = []         # 当前展示的子歌单列表

    # ── 信号桥 ──

    def _back_clicked(self):
        self.backClicked.emit()

    def _on_track_double_clicked(self, item):
        d = item.data(Qt.ItemDataRole.UserRole)
        if d is not None:
            self.trackDoubleClicked.emit(d)

    def _on_sub_clicked(self, pl_id):
        self.subPlaylistClicked.emit(pl_id)

    def _on_tag_clicked(self, tag):
        self.tagClicked.emit(tag)

    # ── 数据渲染 ──

    def set_back_label(self, text: str):
        """更新返回按钮文字（歌单列表 / 上级）"""
        self.detail_back_btn.setText(text)

    def display(self, pl_name, cover_path, tags_str, tracks, sub_playlists):
        """渲染歌单详情

        pl_name: 歌单名称
        cover_path: 封面路径（可为 None）
        tags_str: 标签字符串
        tracks: 曲目 dict 列表 [{"title", "path"}, ...]（展示列表）
        sub_playlists: 后代歌单 dict 列表 [{id, name, track_count}, ...]
        """
        self._tracks_paths = tracks
        self._sub_playlists = sub_playlists

        self.playlist_title.setText(pl_name)
        self._rebuild_tag_buttons(tags_str)
        self._set_cover(cover_path)

        # 子歌单区域：2+ 子歌单时清空曲目区域保留布局；否则显示曲目列表
        multi_child = len(sub_playlists) >= 2
        if multi_child:
            self.track_header.setVisible(True)
            self.track_list.setVisible(True)
            self.track_list.clear()
            self.playlist_count.setText(f"共 {len(sub_playlists)} 个子歌单")
        else:
            self.track_header.setVisible(True)
            self.track_list.setVisible(True)
            self.playlist_count.setText(f"共 {len(tracks)} 首曲目")
            self.track_list.clear()
            for i, tr in enumerate(tracks):
                title = tr.get("title") or os.path.basename(tr["path"])
                item = QListWidgetItem(f"{i + 1}. {title}")
                item.setData(Qt.ItemDataRole.UserRole, i)
                self.track_list.addItem(item)

        # 子歌单区域填充
        if multi_child and sub_playlists:
            self._rebuild_sub_cards(sub_playlists)
        else:
            self._clear_sub_cards()

    def _set_cover(self, cover_path):
        got = False
        if cover_path and os.path.exists(cover_path):
            pix = QPixmap(cover_path)
            if not pix.isNull():
                self.cover_label.setPixmap(
                    pix.scaled(160, 160, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
                )
                self.cover_label.setText("")
                got = True
        if not got:
            self.cover_label.setPixmap(QPixmap())
            self.cover_label.setText("无封面")

    def _rebuild_tag_buttons(self, tags_str):
        """根据标签字符串重建可点击标签按钮"""
        # 清除旧按钮
        while self._tag_buttons_layout.count():
            item = self._tag_buttons_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        tags = parse_tags(tags_str)
        if tags:
            self.playlist_tags.setVisible(False)
            self._tag_buttons_container.setVisible(True)
            for tag in tags:
                btn = TagButton(tag)
                btn.tagClicked.connect(self._on_tag_clicked)
                self._tag_buttons_layout.addWidget(btn)
        else:
            self.playlist_tags.setVisible(False)
            self._tag_buttons_container.setVisible(False)

    def _clear_sub_cards(self):
        while self.sub_vbox.count():
            item = self.sub_vbox.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        self.sub_header.setVisible(False)
        self.sub_scroll.setVisible(False)

    def _rebuild_sub_cards(self, sub_playlists):
        """重建子歌单卡片"""
        self._clear_sub_cards()
        if not sub_playlists:
            return

        self.sub_header.setVisible(True)
        self.sub_scroll.setVisible(True)
        for child in sub_playlists:
            card = SubPlaylistCard(child)
            card.clicked.connect(self._on_sub_clicked)
            self.sub_vbox.addWidget(card)

    # ── 高亮 ──

    def set_highlight_by_path(self, playing_path):
        """按曲目路径高亮展示列表中的当前播放曲目（若存在于列表）"""
        for i in range(self.track_list.count()):
            it = self.track_list.item(i)
            if not it:
                continue
            d = it.data(Qt.ItemDataRole.UserRole)
            if d is not None and 0 <= d < len(self._tracks_paths):
                if self._tracks_paths[d].get("path") == playing_path:
                    self.track_list.setCurrentItem(it)
                    break