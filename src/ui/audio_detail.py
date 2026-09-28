"""
音频模块详情页控件
在打开歌单时展示：封面、歌单信息、可点击标签、子歌单卡片、曲目列表
"""
import os

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from audio_manager.dlsite_db import split_cv_names
from ui.audio_theme import (
    ACCENT_TINT,
    BG_MAIN,
    BG_SIDEBAR,
    BTN_QSS,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
)
from ui.audio_threads import DurationProbeThread
from ui.audio_widgets import SubPlaylistCard
from ui.flow_layout import FlowLayout
from ui.tag_widgets import TagButton, parse_tags


class PlaylistDetailPage(QWidget):
    """歌单详情页：数据由 AudioMainWindow 组装后通过 display() 渲染"""

    subPlaylistClicked = pyqtSignal(int)
    tagClicked = pyqtSignal(str)
    trackDoubleClicked = pyqtSignal(int)
    backClicked = pyqtSignal()
    dlsiteFieldClicked = pyqtSignal(str, str)  # (field: circle/cv, value)

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
        self.playlist_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
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
        # DLsite 信息区（层级：状态行 → 社团/CV 胶囊 → 元数据行 → 可折叠分类/简介）
        self.dlsite_status = QLabel()
        self.dlsite_status.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
        self.dlsite_status.setVisible(False)
        info_col.addWidget(self.dlsite_status)

        self.dlsite_capsules = QWidget()
        self.dlsite_caps_flow = FlowLayout(self.dlsite_capsules, margin=0, h_spacing=6, v_spacing=4)
        self.dlsite_capsules.setContentsMargins(0, 0, 0, 0)
        self.dlsite_capsules.setLayout(self.dlsite_caps_flow)
        self.dlsite_capsules.setVisible(False)
        info_col.addWidget(self.dlsite_capsules)

        self.dlsite_meta = QLabel()
        self.dlsite_meta.setWordWrap(True)
        self.dlsite_meta.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        self.dlsite_meta.setVisible(False)
        info_col.addWidget(self.dlsite_meta)

        # 可折叠"简介"区：自动换行文本（网络分类已改为顶部可点击标签按钮，不再单设分类区）
        self.dlsite_desc_label = QLabel()
        self.dlsite_desc_label.setWordWrap(True)
        self.dlsite_desc_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.dlsite_desc_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        self.dlsite_desc_sec = self._make_dlsite_section("简介", self.dlsite_desc_label)
        info_col.addWidget(self.dlsite_desc_sec)
        # 注意：这里不要 addStretch()——嵌套 stretch 会让信息行变为"可扩展"项，
        # 在无曲目表的容器页会分到多余空间导致封面垂直居中、标题与封面顶部分离
        info_row.addLayout(info_col, 1)
        self.info_layout.addLayout(info_row)

        # 子歌单区域
        self.sub_header = QLabel("子歌单")
        self.sub_header.setStyleSheet("color: #BBB; font-size: 13px; font-weight: bold; padding-top: 8px;")
        self.sub_header.setVisible(False)
        self.info_layout.addWidget(self.sub_header)

        # 子歌单容器：直接挂在主布局上（外层 info_scroll 已提供整页滚动，无需内层滚动区）
        # 横向铺满、纵向按内容高度：卡片紧贴"子歌单"标题下方，数量多时由外层滚动
        self.sub_cards = QWidget()
        self.sub_cards.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.sub_vbox = QVBoxLayout(self.sub_cards)
        self.sub_vbox.setContentsMargins(0, 0, 0, 0)
        self.sub_vbox.setSpacing(2)
        self.sub_cards.setVisible(False)
        self.info_layout.addWidget(self.sub_cards)

        # 曲目列表标题栏 + 类型筛选
        self.info_layout.addWidget(self._build_track_header())

        self.track_list = QTableWidget(0, 4)
        self.track_list.setHorizontalHeaderLabels(["#", "标题", "类型", "时长"])
        self.track_list.verticalHeader().setVisible(False)
        header = self.track_list.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.track_list.setColumnWidth(0, 44)
        self.track_list.setColumnWidth(2, 70)
        self.track_list.setColumnWidth(3, 70)
        header.setHighlightSections(False)
        header.setStyleSheet("QHeaderView::section { background: #F5F0E8; color: #999; border: none; "
                             "border-bottom: 1px solid #E8E0D5; padding: 4px 8px; font-size: 11px; }")
        self.track_list.setStyleSheet(f"""
            QTableWidget {{
                background-color: {BG_MAIN}; color: #777;
                border: 1px solid #EDE6DA; border-radius: 4px; outline: none;
                gridline-color: #EFE9DE;
            }}
            QTableWidget::item {{ padding: 4px 8px; border-bottom: 1px solid #E8E0D5; }}
            QTableWidget::item:hover {{ background-color: #EDE6DA; color: {TEXT_PRIMARY}; }}
            QTableWidget::item:selected {{
                background-color: {ACCENT_TINT}; color: {TEXT_PRIMARY};
            }}
        """)
        self.track_list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.track_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.track_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.track_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.track_list.cellDoubleClicked.connect(self._on_track_double_clicked)
        self.info_layout.addWidget(self.track_list, 1)
        # 末尾兜底空白：内容（子歌单卡片）不足一屏时收在顶部，下方留空
        self.info_layout.addStretch()

        info_scroll.setWidget(info_content)
        layout.addWidget(info_scroll, 1)

        # 内部状态
        self._tracks_paths = []          # 当前展示的笑单完整曲目 dict（原始序号）
        self._sub_playlists = []         # 当前展示的子歌单列表
        self._track_types = []           # 每首曲目的文件类型（大写扩展名）
        self._durations = {}             # 原始序 -> 秒数（数据库 + 后台探测）
        self._visible_orig = []          # 当前表行对应的原始序号（受类型筛选影响）
        self._selected_type = ""         # 当前类型筛选（""=全部）
        self._probe_thread = None        # 曲目时长后台探测线程
        self._probe_gen = 0              # 探测代数，切换歌单后丢弃过期结果

    # ── 信号桥 ──

    def _back_clicked(self):
        self.backClicked.emit()

    def _on_track_double_clicked(self, row, _col):
        """双击曲目行：发出原始曲目序号（同窗口 _tracks_data 的索引）"""
        if 0 <= row < len(self._visible_orig):
            self.trackDoubleClicked.emit(self._visible_orig[row])

    def _build_track_header(self):
        """构建"曲目列表"标题栏与类型筛选下拉框"""
        bar = QFrame()
        self._track_header_bar = bar
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 8, 0, 0)
        row.setSpacing(8)
        title = QLabel("曲目列表")
        title.setStyleSheet("color: #BBB; font-size: 13px; font-weight: bold; padding-top: 0;")
        self.track_header = title
        row.addWidget(title)
        row.addStretch()
        label = QLabel("类型:")
        label.setStyleSheet("color: #BBB; font-size: 12px;")
        self.type_filter = QComboBox()
        self.type_filter.addItem("全部类型", "")
        self.type_filter.setFixedWidth(110)
        self.type_filter.setStyleSheet(f"""
            QComboBox {{ background: #FFFFFF; color: {TEXT_PRIMARY}; border: 1px solid #E0D8CC;
                          border-radius: 4px; padding: 2px 6px; font-size: 12px; }}
            QComboBox::drop-down {{ border: none; width: 18px; }}
            QComboBox QAbstractItemView {{ background: #FFFFFF; color: {TEXT_PRIMARY};
                                           selection-background-color: {ACCENT_TINT};
                                           selection-color: {TEXT_PRIMARY}; }}
        """)
        self.type_filter.currentIndexChanged.connect(self._on_type_filter_changed)
        row.addWidget(label)
        row.addWidget(self.type_filter)
        return bar

    def _on_type_filter_changed(self, _idx):
        """类型筛选变化：重建当前显示行"""
        self._selected_type = self.type_filter.currentData() or ""
        self._rebuild_track_rows()

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
        tracks: 曲目 dict 列表 [{"title", "path", "duration"}, ...]（展示列表）
        sub_playlists: 后代歌单 dict 列表 [{id, name, track_count}, ...]
        """
        self._tracks_paths = tracks
        self._sub_playlists = sub_playlists

        self.playlist_title.setText(pl_name)
        self.playlist_title.setToolTip(pl_name)
        self._rebuild_tag_buttons(tags_str)
        self._set_cover(cover_path)
        self.clear_dlsite()

        # 主歌单自身也可能有音频；曲目与子歌单应同时显示，各自只计算一次。
        has_children = bool(sub_playlists)
        has_tracks = bool(tracks)
        if not has_tracks:
            self._track_header_bar.setVisible(False)
            self.track_list.setVisible(False)
            self.track_list.setRowCount(0)
            self._stop_probe()
            self._rebuild_type_filter(set())
            self._selected_type = ""
        else:
            self._track_header_bar.setVisible(True)
            self.track_list.setVisible(True)
            # 重建类型索引与类型筛选选项
            self._track_types = [self._file_type(tr) for tr in tracks]
            self._rebuild_type_filter(set(self._track_types))
            self._selected_type = ""
            # 时长优先取数据库已有值，缺失的交给后台探测补全
            self._durations = {}
            for i, tr in enumerate(tracks):
                secs = tr.get("duration") or 0
                if secs > 0:
                    self._durations[i] = int(secs)
            self._rebuild_track_rows()
            self._start_probe(tracks)

        if has_tracks and has_children:
            self.playlist_count.setText(f"本目录 {len(tracks)} 首曲目 · {len(sub_playlists)} 个子歌单")
        elif has_children:
            self.playlist_count.setText(f"共 {len(sub_playlists)} 个子歌单")
        else:
            self.playlist_count.setText(f"共 {len(tracks)} 首曲目")

        # 子歌单区域填充
        if has_children and sub_playlists:
            self._rebuild_sub_cards(sub_playlists)
        else:
            self._clear_sub_cards()


    @staticmethod
    def _file_type(tr):
        """从文件路径取大写扩展名作为类型（如 MP3 / FLAC）"""
        ext = os.path.splitext(tr.get("path") or "")[1]
        return ext.lstrip(".").upper()


    def _rebuild_type_filter(self, types):
        """重建类型筛选下拉框选项（保留当前选中的项若仍存在）"""
        cur = self.type_filter.currentData()
        self.type_filter.blockSignals(True)
        self.type_filter.clear()
        self.type_filter.addItem("全部类型", "")
        for t in sorted(types):
            if t:
                self.type_filter.addItem(t, t)
        # 尽量保持原选中项
        if cur and self.type_filter.findData(cur) >= 0:
            self.type_filter.setCurrentIndex(self.type_filter.findData(cur))
        else:
            self.type_filter.setCurrentIndex(0)
        self.type_filter.blockSignals(False)


    def _rebuild_track_rows(self):
        """按当前类型筛选重建表格内容（# / 标题 / 类型 / 时长）"""
        self.track_list.setRowCount(0)
        self._visible_orig = []
        shown = []
        for i, tr in enumerate(self._tracks_paths):
            if self._selected_type and self._track_types[i] != self._selected_type:
                continue
            self._visible_orig.append(i)
            title = tr.get("title") or os.path.basename(tr["path"])
            shown.append((i, title))
        self.track_list.setRowCount(len(shown))
        for row, (i, title) in enumerate(shown):
            self._set_row(row, i + 1, title, self._track_types[i], self._durations.get(i))
        self._rehighlight_current_row()


    def _set_row(self, row, number, title, ftype, secs):
        """写入表格的一行数据（# / 标题 / 类型 / 时长）"""
        cells = [str(number), title, ftype, self._fmt_time(secs)]
        for col, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if col != 1:
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.track_list.setItem(row, col, item)


    @staticmethod
    def _fmt_time(secs):
        """秒 → "mm:ss"（不足 1 秒显示为空，避免干扰列表）"""
        if not secs or secs <= 0:
            return ""
        minutes = int(secs // 60)
        seconds = int(round(secs % 60))
        if seconds >= 60:
            minutes += 1
            seconds -= 60
        return f"{minutes:02d}:{seconds:02d}"


    def _start_probe(self, tracks):
        """启动后台时长探测线程，仅对缺少时长的曲目逐条回填（按原始序）"""
        missing = [t for i, t in enumerate(tracks) if i not in self._durations]
        self._stop_probe()
        if not missing:
            return
        self._missing_orig = [i for i, t in enumerate(tracks) if i not in self._durations]
        self._probe_gen += 1
        gen = self._probe_gen
        self._probe_thread = DurationProbeThread(missing, gen=gen)
        self._probe_thread.resolved.connect(self._on_duration_resolved)
        self._probe_thread.start()


    def _on_duration_resolved(self, gen, pos, secs):
        """收到一条曲目时长：存表并更新所有可见行对应的时长列"""
        if gen != self._probe_gen or secs <= 0:
            return
        if pos >= len(self._missing_orig):
            return
        orig = self._missing_orig[pos]
        self._durations[orig] = int(secs)
        if orig in self._visible_orig:
            row = self._visible_orig.index(orig)
            self.track_list.item(row, 3).setText(self._fmt_time(secs))


    def _stop_probe(self):
        """停止并回收上一代的探测线程"""
        if self._probe_thread:
            self._probe_thread.stop()
            self._probe_thread.resolved.disconnect(self._on_duration_resolved)
            self._probe_thread.wait(500)
            self._probe_thread = None
            self._missing_orig = []


    def _rehighlight_current_row(self):
        """重新应用当前高亮（重建表格后保持选中态）"""
        self.set_highlight_by_path(getattr(self, "_highlight_path", ""))

    # ── DLsite 信息展示 ──

    def _make_dlsite_section(self, title, body):
        """构建可折叠区块：点击标题展开/收起 body（默认收起）"""
        sec = QWidget()
        v = QVBoxLayout(sec)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        btn = QToolButton()
        btn.setText(title)
        btn.setCheckable(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            "QToolButton { color: #BBB; font-size: 13px; font-weight: bold; border: none;"
            " background: transparent; text-align: left; padding: 0; }"
            "QToolButton:hover { color: #888; }"
        )
        body.setVisible(False)
        btn.toggled.connect(
            lambda checked: (body.setVisible(checked),
                             btn.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow))
        )
        v.addWidget(btn)
        v.addWidget(body)
        sec.setVisible(False)
        return sec

    @staticmethod
    def _clear_flow_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def clear_dlsite(self):
        """清空/隐藏 DLsite 信息区（切换歌单时调用）"""
        self.dlsite_status.setVisible(False)
        self.dlsite_status.setText("")
        self._clear_flow_layout(self.dlsite_caps_flow)
        self.dlsite_capsules.setVisible(False)
        self.dlsite_meta.setVisible(False)
        self.dlsite_desc_label.setText("")
        self.dlsite_desc_sec.setVisible(False)

    def show_dlsite_pending(self, rj):
        """显示"正在获取"占位（后台线程抓取完成后由主窗口回调刷新）"""
        self.dlsite_status.setText(f"正在获取 DLsite 信息（{rj}）…")
        self.dlsite_status.setVisible(True)

    def show_dlsite_error(self):
        """显示获取失败提示（失败记录 24 小时后才会自动重试）"""
        self.dlsite_status.setText("DLsite 信息获取失败")
        self.dlsite_status.setVisible(True)

    def show_dlsite_info(self, info):
        """渲染 DLsite 作品信息（层级：胶囊标签 → 元数据行 → 折叠简介）

        社团/CV 胶囊可点击：点击发出 dlsiteFieldClicked(field, value)，
        由主窗口切回列表并按该维度过滤（与标签叠加 AND）。
        """
        # 胶囊：社团 / CV（醒目主色，可点击筛选；CV 多声优拆分为多个胶囊）
        self._clear_flow_layout(self.dlsite_caps_flow)
        capsules = []
        if info.get("circle"):
            capsules.append(("circle", info["circle"], f"社团 · {info['circle']}"))
        for name in split_cv_names(info.get("cv") or ""):
            capsules.append(("cv", name, f"CV · {name}"))
        if capsules:
            for field, value, text in capsules:
                pill = QPushButton(text)
                pill.setCursor(Qt.CursorShape.PointingHandCursor)
                pill.setToolTip("点击筛选该" + ("社团" if field == "circle" else "CV"))
                pill.setStyleSheet(
                    "QPushButton {"
                    f"    background-color: {ACCENT_TINT}; color: {TEXT_PRIMARY};"
                    "    border: none; border-radius: 10px; padding: 3px 10px;"
                    "    font-size: 12px; font-weight: bold;"
                    "}"
                    "QPushButton:hover { background-color: #EFD9B8; }"
                )
                pill.clicked.connect(
                    lambda _=False, f=field, v=value: self.dlsiteFieldClicked.emit(f, v)
                )
                self.dlsite_caps_flow.addWidget(pill)
            self.dlsite_capsules.setVisible(True)
        else:
            self.dlsite_capsules.setVisible(False)

        # 元数据行（灰字）：发售日 · 年龄 · 形式 · 文件 · 容量
        meta_parts = [info.get(k) for k in ("release_date", "age_rating", "work_type", "file_type", "file_size")]
        meta_str = " · ".join(str(p) for p in meta_parts if p)
        if meta_str:
            self.dlsite_meta.setText(meta_str)
            self.dlsite_meta.setVisible(True)
        else:
            self.dlsite_meta.setVisible(False)

        # 分类标签：不再在信息区单独渲染（分类已作为顶部可点击标签按钮展示）
        self.dlsite_status.setVisible(False)

        # 简介（默认折叠）
        desc_parts = info.get("description") or []
        desc_text = "\n\n".join(
            f"{p.get('heading', '')}\n{p.get('text', '')}".strip()
            for p in desc_parts if (p.get("heading") or p.get("text"))
        )
        if desc_text:
            self.dlsite_desc_label.setText(desc_text)
            self.dlsite_desc_sec.setVisible(True)
        else:
            self.dlsite_desc_sec.setVisible(False)

        self.dlsite_status.setVisible(False)

    def try_dlsite_cover(self, cover_path):
        """当前无封面时用 DLsite 封面补位（异步抓取完成后回调）"""
        if getattr(self, "_has_cover", False):
            return
        if cover_path and os.path.exists(cover_path):
            pix = QPixmap(cover_path)
            if not pix.isNull():
                self.cover_label.setPixmap(
                    pix.scaled(160, 160, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
                )
                self.cover_label.setText("")
                self._has_cover = True

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
        self._has_cover = got
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
        self.sub_cards.setVisible(False)

    def _rebuild_sub_cards(self, sub_playlists):
        """重建子歌单卡片"""
        self._clear_sub_cards()
        if not sub_playlists:
            return

        self.sub_header.setVisible(True)
        self.sub_cards.setVisible(True)
        for child in sub_playlists:
            card = SubPlaylistCard(child)
            card.clicked.connect(self._on_sub_clicked)
            self.sub_vbox.addWidget(card)

    # ── 高亮 ──

    def set_highlight_by_path(self, playing_path):
        """按曲目路径高亮展示列表中的当前播放曲目（若存在于过滤后的列表）"""
        self._highlight_path = playing_path or ""
        if not self._highlight_path:
            return
        for row, orig in enumerate(self._visible_orig):
            if (orig < len(self._tracks_paths)
                    and self._tracks_paths[orig].get("path") == self._highlight_path):
                self.track_list.selectRow(row)
                self.track_list.scrollToItem(
                    self.track_list.item(row, 1),
                    QAbstractItemView.ScrollHint.EnsureVisible,
                )
                return
