"""视频模块主窗口：作者侧栏 + 作品卡墙 + 作品详情页 + 内置播放器

结构与写真模块（ui/main_window.py）一致，差别是：
- 三级结构换成 根目录/作者/作品/视频文件，卡片 16:9 封面由 ffmpeg 懒抽帧；
- 卡片点击进入「本作品的视频列表」，双击列表行播放，播放器窗口全模块只建一个。
"""
import os

from PyQt6.QtCore import QEvent, QPoint, QRect, Qt, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from resource_manager import config
from ui.flow_layout import FlowLayout
from ui.thread_guard import stop_scan_thread
from ui.video_detail import WorkVideosView
from ui.video_theme import (
    ACCENT,
    BG_MAIN,
    BG_PAGE,
    BTN_QSS,
    GLOBAL_SCROLLBAR_QSS,
    LIST_QSS,
    SEARCH_QSS,
    TEXT_MUTED,
    TEXT_PRIMARY,
)
from ui.video_widgets import ResumeCard, VideoCard, VideoScanThread, VideoSettingsDialog
from video_manager import database as db
from video_manager import scanner

# stack 的三页：卡片墙 / 作品详情 / 继续观看
WORKS_PAGE = 0
DETAIL_PAGE = 1
RESUME_PAGE = 2

# 侧栏「继续观看」项的标记（作者 id 是 int，None 表示全部作者，用字符串区分）
RESUME_MARKER = "__resume__"

# 最多列出多少条续播记录：几百条已经滚不完，再多只是浪费建卡时间
RESUME_LIMIT = 200


class VideoMainWindow(QMainWindow):
    def __init__(self, on_back_to_launcher=None):
        super().__init__()
        self._on_back_to_launcher = on_back_to_launcher
        self.setWindowTitle(f"本地资源管理器 - 视频 v{config.APP_VERSION}")
        self.resize(1180, 760)
        self.setMinimumSize(900, 560)

        db.init_db()

        self._cards = []
        self._resume_cards = []
        self._player = None
        self._detail = None
        self._auto_scanning = True
        self.scan_thread = None

        self.setStyleSheet(GLOBAL_SCROLLBAR_QSS)

        central = QWidget()
        central.setObjectName("videoCentral")
        central.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCentralWidget(central)
        # QMainWindow 的 QSS 管不到 centralWidget，且纯 QWidget 必须挂 WA_StyledBackground
        central.setStyleSheet(f"#videoCentral {{ background-color: {BG_MAIN}; }}")

        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_sidebar())
        layout.addWidget(self._build_content(), 1)

        self.load_authors()
        self.load_works()
        # 启动落点：有续播记录就落在「继续观看」；没有就停在作品墙，
        # 别让人一进视频模块看到一张空页
        if db.count_resume():
            self.show_resume_page()
        else:
            self.author_list.blockSignals(True)
            self.author_list.setCurrentRow(1)  # 第 1 行固定是「全部作者」
            self.author_list.blockSignals(False)
        self._setup_shortcuts()

        QTimer.singleShot(400, self.start_scan)

    # ==================== 构建 ====================

    def _build_sidebar(self):
        sidebar = QWidget()
        sidebar.setObjectName("videoSidebar")
        sidebar.setFixedWidth(220)
        sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        sidebar.setStyleSheet("#videoSidebar { background-color: #FBF7F0; }")

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        label = QLabel("作者 / 分类")
        label.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 11px; padding: 2px;"
            " background: transparent; border: none;"
        )
        layout.addWidget(label)

        self.author_list = QListWidget()
        self.author_list.setObjectName("videoAuthorList")
        self.author_list.setStyleSheet(LIST_QSS)
        self.author_list.currentItemChanged.connect(self.on_author_changed)
        layout.addWidget(self.author_list, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        scan_btn = QPushButton("扫描")
        scan_btn.setStyleSheet(BTN_QSS)
        scan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        scan_btn.clicked.connect(lambda: self.start_scan(manual=True))
        settings_btn = QPushButton("设置")
        settings_btn.setStyleSheet(BTN_QSS)
        settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_btn.clicked.connect(self.open_settings)
        btn_row.addWidget(scan_btn)
        btn_row.addWidget(settings_btn)
        layout.addLayout(btn_row)

        back_btn = QPushButton("← 返回主界面")
        back_btn.setStyleSheet(BTN_QSS)
        back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        back_btn.clicked.connect(self._back_to_launcher)
        layout.addWidget(back_btn)
        return sidebar

    def _build_content(self):
        content = QWidget()
        content.setObjectName("videoContent")
        content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        content.setStyleSheet(f"#videoContent {{ background-color: {BG_MAIN}; }}")

        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        top = QHBoxLayout()
        top.setContentsMargins(12, 12, 12, 6)
        top.setSpacing(8)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("搜索作品名称…")
        self.search_box.setStyleSheet(SEARCH_QSS)
        self.search_box.textChanged.connect(self._on_search_changed)
        top.addWidget(self.search_box)
        layout.addLayout(top)

        # ---- 作品卡墙 ----
        self.works_page = QWidget()
        self.works_page.setObjectName("videoWorksPage")
        works_layout = QVBoxLayout(self.works_page)
        works_layout.setContentsMargins(0, 0, 0, 0)
        works_layout.setSpacing(6)

        self.hint_label = QLabel("")
        self.hint_label.setWordWrap(True)
        self.hint_label.setVisible(False)
        self.hint_label.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 12px; padding: 4px 16px;"
            " background: transparent; border: none;"
        )
        works_layout.addWidget(self.hint_label)

        self.works_scroll = QScrollArea()
        self.works_scroll.setObjectName("videoWorksScroll")
        self.works_scroll.setWidgetResizable(True)
        self.works_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # QScrollArea 的 QSS 画不到 viewport，视口必须单独上色（v1.8.11 白洞 bug）
        self.works_scroll.setStyleSheet(
            "#videoWorksScroll { border: none; background-color: " + BG_PAGE + "; }"
        )
        self.works_scroll.viewport().setStyleSheet(f"background-color: {BG_PAGE};")
        self.works_scroll.verticalScrollBar().valueChanged.connect(self._on_works_scroll)
        self.works_scroll.viewport().installEventFilter(self)
        works_layout.addWidget(self.works_scroll, 1)

        self.grid_container = QWidget()
        self.grid_container.setObjectName("videoGrid")
        self.grid_container.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.grid_layout = FlowLayout(self.grid_container, margin=16, h_spacing=16, v_spacing=16)
        self.works_scroll.setWidget(self.grid_container)

        self._scroll_timer = QTimer()
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(80)
        self._scroll_timer.timeout.connect(self._load_visible_cards)

        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(150)
        self._resize_timer.timeout.connect(self._load_visible_cards)

        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._apply_search_filter)

        self._scan_refresh_timer = QTimer()
        self._scan_refresh_timer.setSingleShot(True)
        self._scan_refresh_timer.setInterval(700)
        self._scan_refresh_timer.timeout.connect(self._refresh_during_scan)

        # ---- 详情页容器 ----
        self.detail_page = QWidget()
        self.detail_page.setObjectName("videoDetailHost")
        detail_layout = QVBoxLayout(self.detail_page)
        detail_layout.setContentsMargins(0, 0, 0, 0)

        # ---- 继续观看页 ----
        self.resume_page = self._build_resume_page()

        self.stack = QStackedWidget()
        self.stack.addWidget(self.works_page)
        self.stack.addWidget(self.detail_page)
        self.stack.addWidget(self.resume_page)
        layout.addWidget(self.stack, 1)

        self.scan_status = self._build_scan_status()
        layout.addWidget(self.scan_status)
        self.scan_status.setVisible(False)
        return content

    def _build_scan_status(self):
        bar = QFrame()
        bar.setObjectName("videoScanBar")
        bar.setFixedHeight(40)
        bar.setStyleSheet(
            "#videoScanBar { background-color: #FBF7F0; border: none;"
            " border-top: 1px solid #E8DECB; }"
        )
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(12)

        self.scan_label = QLabel("准备扫描…")
        self.scan_label.setFixedWidth(420)
        self.scan_label.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 12px; background: transparent; border: none;"
        )
        layout.addWidget(self.scan_label)

        self.scan_progress = QProgressBar()
        self.scan_progress.setFixedWidth(260)
        self.scan_progress.setRange(0, 100)
        self.scan_progress.setStyleSheet(
            "QProgressBar { border: none; background-color: #EDE2CE; border-radius: 3px;"
            " text-align: center; color: #3D3830; font-size: 11px; height: 14px; }"
            f"QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 3px; }}"
        )
        layout.addWidget(self.scan_progress)
        layout.addStretch()

        self.scan_cancel_btn = QPushButton("取消")
        self.scan_cancel_btn.setStyleSheet(BTN_QSS)
        self.scan_cancel_btn.setFixedWidth(64)
        self.scan_cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scan_cancel_btn.clicked.connect(self.cancel_scan)
        layout.addWidget(self.scan_cancel_btn)
        return bar

    def _build_resume_page(self):
        page = QWidget()
        page.setObjectName("videoResumePage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.resume_hint = QLabel("")
        self.resume_hint.setWordWrap(True)
        self.resume_hint.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 13px; padding: 24px 24px;"
            " background: transparent; border: none;"
        )
        layout.addWidget(self.resume_hint)

        self.resume_scroll = QScrollArea()
        self.resume_scroll.setObjectName("videoResumeScroll")
        self.resume_scroll.setWidgetResizable(True)
        self.resume_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.resume_scroll.setStyleSheet(
            "#videoResumeScroll { border: none; background-color: " + BG_PAGE + "; }"
        )
        self.resume_scroll.viewport().setStyleSheet(f"background-color: {BG_PAGE};")
        self.resume_scroll.verticalScrollBar().valueChanged.connect(self._on_works_scroll)
        self.resume_scroll.verticalScrollBar().setSingleStep(210)
        self.resume_scroll.viewport().installEventFilter(self)

        self.resume_container = QWidget()
        self.resume_container.setObjectName("videoResumeGrid")
        self.resume_container.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.resume_layout = FlowLayout(
            self.resume_container, margin=16, h_spacing=16, v_spacing=16
        )
        self.resume_scroll.setWidget(self.resume_container)
        layout.addWidget(self.resume_scroll, 1)
        return page

    def _setup_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+F"), self, self.search_box.setFocus)
        QShortcut(QKeySequence("F5"), self, self.refresh)
        QShortcut(QKeySequence("Ctrl+S"), self, lambda: self.start_scan(manual=True))
        QShortcut(QKeySequence("Esc"), self, self._handle_escape)

    # ==================== 作者 / 作品 ====================

    def load_authors(self):
        # 重建后静默恢复选中：clear() 后列表没有当前项，视图会在布局时
        # 自动选中第 0 行（「继续观看」）并发出 currentItemChanged —— 用户
        # 正看着某一集的列表，扫描同步完成时会被整页踢回续播页。
        current = self.author_list.currentItem()
        keep = current.data(Qt.ItemDataRole.UserRole) if current else None
        has_selection = current is not None

        self.author_list.blockSignals(True)
        self.author_list.clear()

        # 置顶的「继续观看」：续播点数据一直都有，之前只有翻回作品再看列表里
        # 那一条细进度条一条路，等于没有入口
        count = db.count_resume()
        resume_item = QListWidgetItem(f"⏳ 继续观看 ({count})" if count else "⏳ 继续观看")
        resume_item.setData(Qt.ItemDataRole.UserRole, RESUME_MARKER)
        resume_item.setToolTip("按最后观看时间排序，点一下就从上次的位置接着放")
        self.author_list.addItem(resume_item)

        all_item = QListWidgetItem("全部作者")
        all_item.setData(Qt.ItemDataRole.UserRole, None)
        self.author_list.addItem(all_item)

        for author in db.list_authors():
            item = QListWidgetItem(f"{author['name']} ({author['work_count']})")
            item.setData(Qt.ItemDataRole.UserRole, author["id"])
            self.author_list.addItem(item)

        row = 0
        if has_selection:
            for index in range(self.author_list.count()):
                if self.author_list.item(index).data(Qt.ItemDataRole.UserRole) == keep:
                    row = index
                    break
        self.author_list.setCurrentRow(row)
        self.author_list.blockSignals(False)

    def on_author_changed(self, current, _previous):
        if current is None:
            return
        if current.data(Qt.ItemDataRole.UserRole) == RESUME_MARKER:
            self.show_resume_page()
            return
        if self.stack.currentIndex() != WORKS_PAGE:
            self._close_detail()
            self.stack.setCurrentIndex(WORKS_PAGE)
        self.load_works()

    def load_works(self):
        self.grid_container.setUpdatesEnabled(False)
        self.grid_container.setVisible(False)

        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards = []

        current = self.author_list.currentItem()
        author_id = current.data(Qt.ItemDataRole.UserRole) if current else None
        # 选中「继续观看」时 UserRole 是标记字符串，不能当 author_id 塞进 SQL
        if not isinstance(author_id, int):
            author_id = None

        works = db.list_works(author_id)
        for work in works:
            card = VideoCard(work)
            card.clicked.connect(self.open_work)
            self.grid_layout.addWidget(card)
            self._cards.append(card)

        self.grid_container.setVisible(True)
        self.grid_container.setUpdatesEnabled(True)
        self.grid_layout.invalidate()
        self.grid_container.updateGeometry()
        self.grid_container.adjustSize()
        self.works_scroll.verticalScrollBar().setSingleStep(210)
        self._apply_search_filter()
        self._update_hint(len(works))
        QTimer.singleShot(0, self._load_visible_cards)

    def _update_hint(self, work_count):
        roots = scanner.video_roots()
        missing = [r for r in roots if not os.path.isdir(r)]
        if not roots or len(missing) == len(roots):
            detail = "、".join(roots) or "（未配置）"
            self.hint_label.setText(
                f"视频目录不存在：{detail}　—　点左下角「设置」选择你的视频根目录"
            )
            self.hint_label.setVisible(True)
        elif work_count == 0:
            self.hint_label.setText(
                "这些目录下还没有作品。结构应为：根目录 / 作者 / 作品 / 视频文件。"
            )
            self.hint_label.setVisible(True)
        else:
            hint = ""
            if missing:
                hint = "暂时读不到的目录：" + "、".join(missing)
            self.hint_label.setText(hint)
            self.hint_label.setVisible(bool(missing))

    def _on_search_changed(self, _text):
        self._search_timer.start()

    def _apply_search_filter(self):
        keyword = self.search_box.text().strip().lower()
        for card in self._cards:
            name = (card.work.get("name") or "").lower()
            author = (card.work.get("author") or "").lower()
            card.setVisible(not keyword or keyword in name or keyword in author)
        for card in self._resume_cards:
            work_name = (card.work.get("name") or "").lower()
            video_name = (card.item.get("video_name") or "").lower()
            author = (card.work.get("author") or "").lower()
            card.setVisible(
                not keyword
                or keyword in work_name
                or keyword in video_name
                or keyword in author
            )
        self.grid_layout.invalidate()
        self.grid_container.updateGeometry()
        self.resume_layout.invalidate()
        self.resume_container.updateGeometry()

    def _on_works_scroll(self, _value):
        self._scroll_timer.start()

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show) and (
            obj is self.works_scroll.viewport() or obj is self.resume_scroll.viewport()
        ):
            self._resize_timer.start()
        return super().eventFilter(obj, event)

    def _load_visible_cards(self):
        """统一入口：按当前页决定给哪一组卡片加载封面"""
        if self.stack.currentIndex() == RESUME_PAGE:
            self._load_cards_in(self._resume_cards, self.resume_scroll)
        else:
            self._load_cards_in(self._cards, self.works_scroll)

    def _load_cards_in(self, cards, scroll):
        viewport = scroll.viewport()
        vrect = viewport.rect()
        viewport_valid = vrect.width() > 0 and vrect.height() > 0
        loaded = 0
        for card in cards:
            if loaded >= 24:
                break
            if card._thumb_loaded or card.isHidden():  # noqa: SLF001 - 卡片自带幂等标记
                continue
            if viewport_valid:
                top_left = card.mapTo(viewport, QPoint(0, 0))
                if not vrect.intersects(QRect(top_left, card.size())):
                    continue
            card.load_thumbnail()
            loaded += 1

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._load_visible_cards)

    def refresh(self):
        resume = self.stack.currentIndex() == RESUME_PAGE
        self.load_authors()
        self.load_works()
        if resume:
            self._rebuild_resume()

    # ==================== 继续观看 ====================

    def show_resume_page(self):
        self._close_detail()
        self._rebuild_resume()
        self.stack.setCurrentIndex(RESUME_PAGE)

    def _rebuild_resume(self):
        self.resume_container.setUpdatesEnabled(False)
        self.resume_container.setVisible(False)
        while self.resume_layout.count():
            item = self.resume_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._resume_cards = []

        items = db.list_resume(RESUME_LIMIT)
        for item in items:
            card = ResumeCard(item)
            card.clicked.connect(self._play_resume)
            self.resume_layout.addWidget(card)
            self._resume_cards.append(card)

        self.resume_container.setVisible(True)
        self.resume_container.setUpdatesEnabled(True)
        self.resume_layout.invalidate()
        self.resume_container.updateGeometry()
        self.resume_container.adjustSize()
        self.resume_hint.setText(
            ""
            if items
            else "还没有看到一半的视频。\n看视频时进度会自动记下来，下次在这里点一下就能接着看。"
        )
        self.resume_hint.setVisible(not items)
        self._apply_search_filter()
        QTimer.singleShot(0, self._load_visible_cards)

    def _play_resume(self, item):
        """从续播点开始：取出所在作品的全部视频，定位到这一集再交给播放器

        续播位置本身不在这里算——播放器会按路径从 progress 表读，
        入口只管把「该播哪一集」备好，避免两处各算一遍。
        """
        work_id = item.get("work_id")
        videos = db.get_work_videos(work_id) if work_id is not None else []
        if not videos:
            QMessageBox.information(self, "提示", "这个视频已经不在库里了，可能被移动或删除。")
            self._rebuild_resume()
            self._refresh_resume_count()
            return
        target = item.get("path")
        index = 0
        for i, video in enumerate(videos):
            if video["path"] == target:
                index = i
                break
        self._play(videos, index)

    def _refresh_resume_count(self):
        """只改侧栏那一行的计数：重建整个作者列表会丢掉当前选中项"""
        if self.author_list.count() == 0:
            return
        entry = self.author_list.item(0)
        if entry.data(Qt.ItemDataRole.UserRole) != RESUME_MARKER:
            return
        count = db.count_resume()
        entry.setText(f"⏳ 继续观看 ({count})" if count else "⏳ 继续观看")

    # ==================== 详情页 ====================

    def open_work(self, work):
        videos = db.get_work_videos(work["id"])
        if not videos:
            QMessageBox.information(self, "提示", "该作品下没有视频文件")
            return
        self._close_detail()
        self._detail = WorkVideosView(work, videos)
        self._detail.playRequested.connect(self._play)
        self._detail.backRequested.connect(self.on_back_from_work)
        self.detail_page.layout().addWidget(self._detail)
        self.stack.setCurrentIndex(1)

    def _close_detail(self):
        if self._detail is None:
            return
        self._detail.setParent(None)
        self._detail.deleteLater()
        self._detail = None

    def on_back_from_work(self):
        # 详情页自己有「← 返回」，主界面顶栏不再放第二个返回键（避免一屏两个「返回」）
        self.stack.setCurrentIndex(0)
        self._close_detail()

    def _handle_escape(self):
        if self.stack.currentIndex() == DETAIL_PAGE:
            self.on_back_from_work()
        elif self.stack.currentIndex() == RESUME_PAGE:
            # 回到作品墙（第 1 行固定是「全部作者」）
            self.author_list.setCurrentRow(1)
        elif self.search_box.text():
            self.search_box.clear()

    # ==================== 播放 ====================

    def _ensure_player(self):
        if self._player is None:
            from ui.video_player import VideoPlayerWindow

            self._player = VideoPlayerWindow()
            self._player.closed.connect(self._on_player_closed)
        return self._player

    def _play(self, videos, index):
        self._ensure_player().play_list(videos, index)

    def _on_player_closed(self):
        # 回来时刷新「已看 xx:xx」列，不重建整页
        if self._detail is not None:
            self._detail.refresh_progress()
        self._refresh_resume_count()
        # 停在续播页时，刚看完的那条可能已被清掉/位置变了，重排一次
        if self.stack.currentIndex() == RESUME_PAGE:
            self._rebuild_resume()

    # ==================== 扫描 ====================

    def start_scan(self, manual=False):
        if self.scan_thread is not None and self.scan_thread.isRunning():
            return
        if not any(os.path.isdir(r) for r in scanner.video_roots()):
            if manual:
                QMessageBox.warning(
                    self, "目录不存在",
                    "视频目录不存在：" + "、".join(scanner.video_roots() or ["（未配置）"])
                    + "\n请先在「设置」里选择根目录。",
                )
            self._update_hint(len(self._cards))
            return

        self._auto_scanning = not manual
        self.scan_progress.setValue(0)
        self.scan_label.setText("准备扫描…")
        self.scan_cancel_btn.setEnabled(True)
        if manual:
            self.scan_status.setVisible(True)

        self.scan_thread = VideoScanThread(total_authors=scanner.count_authors())
        self.scan_thread.scan_finished.connect(self.on_scan_finished)
        self.scan_thread.error.connect(self.on_scan_error)
        self.scan_thread.progress.connect(self.on_scan_progress)
        self.scan_thread.author_done.connect(self.on_author_done)
        self.scan_thread.start()

    def cancel_scan(self):
        if self.scan_thread is not None and self.scan_thread.isRunning():
            self.scan_thread.cancel()
            self.scan_cancel_btn.setEnabled(False)
            self.scan_label.setText("正在取消…")
            self.scan_thread.wait(3000)
        self._hide_scan_status()

    def on_scan_progress(self, stage, author, work, count, current, total):
        if not self._auto_scanning and not self.scan_status.isVisible():
            self.scan_status.setVisible(True)
        if stage == "author":
            if total > 0:
                self.scan_progress.setValue(int(current / total * 100))
            self.scan_label.setText(f"正在同步作者：{author}（{current}/{total}）")
        elif stage == "work":
            self.scan_label.setText(f"作者：{author}　作品：{work}　视频数：{count}")
        elif stage == "done":
            self.scan_progress.setValue(100)
            self.scan_label.setText("正在清理孤儿封面…")

    def on_author_done(self, _author_name, _count):
        self._scan_refresh_timer.start()

    def _refresh_during_scan(self):
        if self.scan_thread is not None and self.scan_thread.isRunning():
            self.load_authors()
            current = self.author_list.currentItem()
            if current is not None and current.data(Qt.ItemDataRole.UserRole) is None:
                self.load_works()

    def on_scan_finished(self, stats):
        auto = self._auto_scanning
        self._auto_scanning = False
        self._hide_scan_status()

        if stats.get("cancelled") or stats.get("unchanged"):
            return
        self.load_authors()
        self.load_works()
        # 停在续播页时 load_authors 不会再顺带刷新它（信号被静默了），
        # 得自己重建一次，否则刚看完的那条还挂在「继续观看」里
        if self.stack.currentIndex() == RESUME_PAGE:
            self._rebuild_resume()
        if auto:
            return
        QMessageBox.information(
            self, "扫描完成",
            f"新增视频：{stats.get('added', 0)} 个\n"
            f"更新：{stats.get('updated', 0)} 个\n"
            f"删除：{stats.get('deleted', 0)} 个\n"
            f"删除作品：{stats.get('deleted_works', 0)} 个\n"
            f"未变化：{stats.get('skipped', 0)} 个\n"
            f"读取时长：{stats.get('probed', 0)} 个\n"
            f"清理封面：{stats.get('gc_thumbs', 0)} 个",
        )

    def on_scan_error(self, message):
        self._hide_scan_status()
        auto = self._auto_scanning
        self._auto_scanning = False
        self._update_hint(len(self._cards))
        if auto:
            return
        QMessageBox.critical(self, "扫描失败", message)

    def _hide_scan_status(self):
        self.scan_status.setVisible(False)
        self.scan_progress.setValue(0)
        self.scan_label.setText("准备扫描…")
        self.scan_cancel_btn.setEnabled(True)

    # ==================== 设置 / 关闭 ====================

    def open_settings(self):
        dialog = VideoSettingsDialog(self)
        if dialog.exec() and dialog.root_changed:
            self.load_authors()
            self.load_works()
            self.start_scan(manual=True)

    def _back_to_launcher(self):
        self.close()

    def closeEvent(self, event):
        self._stop_scan_thread()
        if self._player is not None:
            self._player.shutdown()
            self._player.close()
            self._player = None
        super().closeEvent(event)
        if self._on_back_to_launcher:
            callback = self._on_back_to_launcher
            self._on_back_to_launcher = None
            callback()

    def _stop_scan_thread(self):
        # 扫描线程持数据库长连接，不停就关窗会向已销毁窗口发信号
        stop_scan_thread(
            self.scan_thread,
            ("scan_finished", "error", "progress", "author_done"),
            log_name="视频扫描线程",
        )
