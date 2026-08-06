"""
音频模块歌单浏览器
层级导航（路径栈 + 面包屑）、搜索、标签过滤、网格/列表双视图、分页
"""
import os

from PyQt6.QtWidgets import (
    QApplication,
    QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QPushButton, QLabel,
    QScrollArea, QFrame, QSizePolicy, QMessageBox,
    QStackedWidget, QLineEdit, QDialog, QComboBox, QMenu,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer

from audio_manager import database as db
from resource_manager import config
from ui.flow_layout import FlowLayout
from ui.tag_widgets import TagChip, TagSelectorDialog, parse_tags
from ui.audio_theme import (
    ACCENT, ACCENT_TINT, BG_MAIN, BG_SIDEBAR,
    TEXT_PRIMARY, TEXT_MUTED, TEXT_DIM, BTN_QSS, get_basename,
)
from ui.audio_widgets import PlaylistCard


class PlaylistBrowser(QWidget):
    """歌单浏览器：支持进入子文件夹、面包屑导航"""
    openPlaylist = pyqtSignal(int)  # 打开歌单的曲目详情页

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background-color: {BG_MAIN};")
        self._path_stack = [""]  # 路径栈，栈顶为当前 parent_path
        self._all_playlists = []  # 当前层级全部歌单（未过滤）
        self._filtered_cache = []  # 当前筛选/排序后的歌单缓存
        self._page_size = 30  # 每页歌单数
        self._current_page = 0  # 当前页码（0-based）
        self._active_tags: set = set()  # 当前活动的标签过滤
        self._search_timer = QTimer()  # 搜索防抖
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply_filter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- 导航栏 ----
        nav_bar = QFrame()
        nav_bar.setStyleSheet(f"background-color: {BG_SIDEBAR}; border: none;")
        nav_layout = QHBoxLayout(nav_bar)
        nav_layout.setContentsMargins(8, 4, 8, 4)
        nav_layout.setSpacing(6)

        self.back_btn = QPushButton("← 返回上级")
        self.back_btn.setStyleSheet(BTN_QSS)
        self.back_btn.setFixedWidth(100)
        self.back_btn.clicked.connect(self._go_up)
        self.back_btn.setVisible(False)
        nav_layout.addWidget(self.back_btn)

        # 面包屑
        self._breadcrumb_widget = QWidget()
        self._breadcrumb_layout = QHBoxLayout(self._breadcrumb_widget)
        self._breadcrumb_layout.setContentsMargins(0, 0, 0, 0)
        self._breadcrumb_layout.setSpacing(2)
        nav_layout.addWidget(self._breadcrumb_widget)

        # 标签芯片（水平排列，无弹性）
        self._tag_bar = QWidget()
        self._tag_bar_layout = QHBoxLayout(self._tag_bar)
        self._tag_bar_layout.setContentsMargins(0, 0, 0, 0)
        self._tag_bar_layout.setSpacing(4)
        self._tag_bar.setVisible(False)
        nav_layout.addWidget(self._tag_bar)

        # 弹性间隔（始终存在，确保右侧控件位置固定）
        nav_layout.addStretch(1)

        # 搜索框
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("搜索歌单...")
        self.search_box.setFixedWidth(180)
        self.search_box.setStyleSheet(f"""
            QLineEdit {{
                background-color: #EDE6DA; color: {TEXT_PRIMARY};
                border: 1px solid #D5CDC0; border-radius: 4px;
                padding: 4px 8px; font-size: 12px;
            }}
            QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
        """)
        self.search_box.textChanged.connect(self._on_search_changed)
        nav_layout.addWidget(self.search_box)

        # 标签选择器按钮
        self.tag_selector_btn = QPushButton("+")
        self.tag_selector_btn.setFixedSize(26, 26)
        self.tag_selector_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tag_selector_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {ACCENT}; color: #fff;
                border: none; border-radius: 13px;
                font-size: 16px; font-weight: bold;
            }}
            QPushButton:hover {{ background-color: #48B8BC; }}
        """)
        self.tag_selector_btn.setToolTip("按标签筛选")
        self.tag_selector_btn.clicked.connect(self._open_tag_selector)
        nav_layout.addWidget(self.tag_selector_btn)

        # 排序
        sort_label = QLabel("排序:")
        sort_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        nav_layout.addWidget(sort_label)
        self.sort_selector = QComboBox()
        self.sort_selector.addItems(["名称 (A-Z)", "名称 (Z-A)", "时间 (新→旧)", "时间 (旧→新)"])
        self.sort_selector.setFixedWidth(140)
        self.sort_selector.setStyleSheet(f"""
            QComboBox {{
                background-color: #EDE6DA; color: {TEXT_PRIMARY};
                border: 1px solid #D5CDC0; border-radius: 4px;
                padding: 4px 8px; font-size: 12px;
            }}
            QComboBox::drop-down {{ border: none; width: 20px; }}
            QComboBox:hover {{ border: 1px solid {ACCENT}; }}
        """)
        self.sort_selector.currentIndexChanged.connect(self._apply_filter)
        self.sort_selector.currentIndexChanged.connect(self._save_sort_pref)
        nav_layout.addWidget(self.sort_selector)

        nav_layout.addStretch()

        # 歌单数量
        self.count_label = QLabel()
        self.count_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px; padding-right: 8px;")
        nav_layout.addWidget(self.count_label)

        # 显示方式
        view_label = QLabel("显示方式:")
        view_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        nav_layout.addWidget(view_label)
        self.view_selector = QComboBox()
        self.view_selector.addItems(["网格视图", "列表视图"])
        self.view_selector.setFixedWidth(110)
        self.view_selector.setStyleSheet(f"""
            QComboBox {{
                background-color: #EDE6DA; color: {TEXT_PRIMARY};
                border: 1px solid #D5CDC0; border-radius: 4px;
                padding: 4px 8px;
            }}
            QComboBox::drop-down {{ border: none; width: 20px; }}
            QComboBox:hover {{ border: 1px solid {ACCENT}; }}
        """)
        self.view_selector.currentIndexChanged.connect(self._switch_view)
        self.view_selector.currentIndexChanged.connect(self._save_view_pref)
        nav_layout.addWidget(self.view_selector)
        layout.addWidget(nav_bar)

        # ---- 视图容器 ----
        self._view_stack = QStackedWidget()
        layout.addWidget(self._view_stack, 1)

        # — 网格页
        self._grid_page = QWidget()
        grid_layout = QVBoxLayout(self._grid_page)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"QScrollArea {{ background-color: {BG_MAIN}; border: none; }}")
        container = QWidget()
        container.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.flow = FlowLayout(container, margin=16, h_spacing=16, v_spacing=16)
        container.setLayout(self.flow)
        scroll.setWidget(container)
        self._grid_scroll = scroll
        grid_layout.addWidget(scroll)
        self._view_stack.addWidget(self._grid_page)  # 0

        # — 列表页
        self._list_page = QWidget()
        list_layout = QVBoxLayout(self._list_page)
        list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_widget = QListWidget()
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._on_list_context_menu)
        self.list_widget.setStyleSheet(f"""
            QListWidget {{
                background-color: {BG_MAIN}; color: {TEXT_PRIMARY};
                border: none; outline: none;
            }}
            QListWidget::item {{
                padding: 10px 16px; border-bottom: 1px solid #E8E0D5;
            }}
            QListWidget::item:hover {{ background-color: #E8E0D5; }}
            QListWidget::item:selected {{
                background-color: {ACCENT_TINT}; color: {TEXT_PRIMARY};
            }}
        """)
        self.list_widget.itemDoubleClicked.connect(self._on_list_item_clicked)
        list_layout.addWidget(self.list_widget)
        self._view_stack.addWidget(self._list_page)  # 1

        self._view_stack.setCurrentIndex(0)

        # ---- 分页栏 ----
        page_bar = QFrame()
        page_bar.setFixedHeight(40)
        page_bar.setStyleSheet(f"background-color: {BG_SIDEBAR}; border: none;")
        page_layout = QHBoxLayout(page_bar)
        page_layout.setContentsMargins(8, 4, 8, 4)
        page_layout.setSpacing(6)

        page_layout.addStretch()

        self._prev_btn = QPushButton("上一页")
        self._prev_btn.setStyleSheet(BTN_QSS)
        self._prev_btn.setFixedWidth(80)
        self._prev_btn.clicked.connect(lambda: self._go_page(-1))
        page_layout.addWidget(self._prev_btn)

        self._page_label = QLabel("第 1 页 / 共 1 页")
        self._page_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_label.setFixedWidth(140)
        page_layout.addWidget(self._page_label)

        self._next_btn = QPushButton("下一页")
        self._next_btn.setStyleSheet(BTN_QSS)
        self._next_btn.setFixedWidth(80)
        self._next_btn.clicked.connect(lambda: self._go_page(1))
        page_layout.addWidget(self._next_btn)

        page_layout.addStretch()
        layout.addWidget(page_bar)

        # 恢复上次使用的配置
        self._restore_preferences()

    # ── 偏好 ──

    def _restore_preferences(self):
        """恢复上次保存的排序和视图偏好"""
        saved_sort = getattr(config, "AUDIO_SORT_INDEX", 0)
        saved_view = getattr(config, "AUDIO_VIEW_MODE", 0)
        self.sort_selector.blockSignals(True)
        self.sort_selector.setCurrentIndex(saved_sort)
        self.sort_selector.blockSignals(False)
        self.view_selector.blockSignals(True)
        self.view_selector.setCurrentIndex(saved_view)
        self.view_selector.blockSignals(False)

    def _save_sort_pref(self, index):
        """保存当前排序偏好到配置文件"""
        config.set_user_config("AUDIO_SORT_INDEX", index)
        config.save_user_config()

    def _save_view_pref(self, index):
        """保存当前视图偏好到配置文件"""
        config.set_user_config("AUDIO_VIEW_MODE", index)
        config.save_user_config()

    def _switch_view(self, index):
        self._view_stack.setCurrentIndex(index)
        # 视图切换时重新渲染（当前只渲染了之前的可见视图）
        if self._filtered_cache:
            self._render(self._filtered_cache)

    # ── 导航 ──

    @property
    def current_path(self):
        return self._path_stack[-1] if self._path_stack else ""

    def _go_up(self):
        """返回上级目录"""
        if len(self._path_stack) > 1:
            self._path_stack.pop()
            self._load_current()

    def navigate_to(self, path):
        """导航到指定路径（作为当前 parent_path）"""
        self._path_stack.append(path)
        self._load_current()

    def refresh(self):
        """刷新当前层级"""
        self.search_box.clear()
        self.clear_tags()
        self._load_current()

    def reload_current(self):
        """重新加载当前层级数据（保留导航/搜索/标签状态）

        供后台 mtime 迁移完成后静默刷新用，避免重置用户已导航的位置。
        """
        self._all_playlists = db.list_playlists_by_parent(self.current_path)
        self._apply_filter()

    def reset(self):
        """回到根目录"""
        self._path_stack = [""]
        self.search_box.clear()
        self.clear_tags()
        self._load_current()

    def _load_current(self):
        """根据当前路径加载歌单"""
        parent = self.current_path
        self._all_playlists = db.list_playlists_by_parent(parent)
        self._apply_filter()

        # 更新导航栏
        self.back_btn.setVisible(len(self._path_stack) > 1)
        self._rebuild_breadcrumb()

    def _on_search_changed(self, text):
        """搜索框文本变化时防抖过滤"""
        self._search_timer.start()  # 每次变化重启300ms定时器

    def _apply_filter(self):
        """根据搜索框文本 / 活动标签过滤并按排序后渲染歌单"""
        text = self.search_box.text().strip().lower() if hasattr(self, 'search_box') else ""
        if text:
            filtered = [pl for pl in self._all_playlists if text in pl["name"].lower()]
        else:
            filtered = self._all_playlists

        # 标签过滤（与搜索框互斥，使用标签时搜索框自动隐藏）
        if self._active_tags:
            filtered = [pl for pl in filtered if self._playlist_matches_tags(pl)]

        # 排序
        idx = self.sort_selector.currentIndex() if hasattr(self, 'sort_selector') else 0
        if idx <= 1:
            # 名称排序
            reverse = idx == 1
            self._filtered_cache = sorted(filtered, key=lambda pl: pl["name"].lower(), reverse=reverse)
        else:
            # 时间排序（按文件夹修改时间，仅使用数据库已缓存的 mtime）
            # mtime=0 的旧数据由 MtimeMigrationThread 后台迁移，避免 UI 线程执行文件 IO
            reverse = idx == 2  # 新→旧: reverse=True
            self._filtered_cache = sorted(
                filtered, key=lambda pl: pl.get("mtime", 0), reverse=reverse
            )

        self._current_page = 0
        self._render(self._filtered_cache)

        total = len(self._all_playlists)
        shown = len(self._filtered_cache)
        if self._active_tags:
            self.count_label.setText(f"共 {shown} 个歌单" if shown else "")
        elif text:
            self.count_label.setText(f"搜索: {shown}/{total} 个歌单")
        else:
            self.count_label.setText(f"共 {shown} 个歌单" if shown else "")

    def _rebuild_breadcrumb(self):
        """重建面包屑导航"""
        # 清除原有部件
        while self._breadcrumb_layout.count():
            item = self._breadcrumb_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        # 生成路径分段
        segments = []
        # 根目录
        segments.append(("歌单列表", ""))

        # 中间路径段
        parts = self._path_stack[1:]  # 跳过栈底 ""
        for p in parts:
            name = get_basename(p)
            segments.append((name, p))

        # 构建面包屑
        for i, (name, path) in enumerate(segments):
            is_last = (i == len(segments) - 1)

            btn = QPushButton(name)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            if is_last:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: transparent; color: {ACCENT};
                        border: none; font-size: 12px; padding: 2px 4px;
                        font-weight: bold;
                    }}
                """)
                btn.setEnabled(False)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: transparent; color: {TEXT_MUTED};
                        border: none; font-size: 12px; padding: 2px 4px;
                    }}
                    QPushButton:hover {{ color: {TEXT_PRIMARY}; }}
                """)
                btn.clicked.connect(lambda checked, p=path: self._jump_to(p))

            self._breadcrumb_layout.addWidget(btn)

            if not is_last:
                sep = QLabel(">")
                sep.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
                self._breadcrumb_layout.addWidget(sep)

        self._breadcrumb_layout.addStretch()

    def _jump_to(self, path):
        """跳转到指定路径（从面包屑点击）"""
        # 重建栈：找到 path 在栈中的位置
        if path == "":
            self._path_stack = [""]
        else:
            idx = -1
            for i, p in enumerate(self._path_stack):
                if p == path:
                    idx = i
                    break
            if idx >= 0:
                self._path_stack = self._path_stack[:idx + 1]
            else:
                self._path_stack = [path]
        self._load_current()

    # ── 标签过滤 ──

    def filter_by_tags(self, tags: set):
        """批量设置标签过滤并刷新"""
        self._active_tags = set(tags)
        self._sync_tag_ui()
        self._apply_filter()

    def filter_by_tag(self, tag: str):
        """添加单个标签过滤并刷新"""
        self.filter_by_tags(self._active_tags | {tag})

    def _on_tag_removed(self, tag):
        """移除单个标签过滤"""
        self._active_tags.discard(tag)
        if not self._active_tags:
            self.search_box.clear()
        self._sync_tag_ui()
        self._apply_filter()

    def clear_tags(self):
        """清除所有标签过滤"""
        self._active_tags.clear()
        self.search_box.setVisible(True)
        self.search_box.clear()
        self._sync_tag_ui()
        self._apply_filter()

    def _open_tag_selector(self):
        """打开标签选择器窗口"""
        all_tags = db.get_all_tags()
        if not all_tags:
            return
        dialog = TagSelectorDialog(all_tags, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected = dialog.get_selected_tags()
            if selected:
                self.filter_by_tags(set(selected))

    def _sync_tag_ui(self):
        """同步标签芯片 UI 与 _active_tags 状态"""
        # 清除旧芯片
        while self._tag_bar_layout.count():
            item = self._tag_bar_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        if self._active_tags:
            for tag in sorted(self._active_tags, key=str.lower):
                chip = TagChip(tag)
                chip.removed.connect(self._on_tag_removed)
                self._tag_bar_layout.addWidget(chip)
            self._tag_bar_layout.addStretch()
            self._tag_bar.setVisible(True)
        else:
            self._tag_bar.setVisible(False)

        self.search_box.setVisible(not bool(self._active_tags))

    def _playlist_matches_tags(self, pl):
        """检查歌单是否匹配所有活动标签（AND 逻辑）"""
        if not self._active_tags:
            return True
        pl_tags = set(parse_tags(pl.get("tags", "") or ""))
        return all(tag in pl_tags for tag in self._active_tags)

    # ── 渲染 ──

    def _render(self, all_playlists):
        """按当前页码分页，仅渲染当前可见视图"""
        total_pages = max(1, (len(all_playlists) + self._page_size - 1) // self._page_size)
        if self._current_page >= total_pages:
            self._current_page = total_pages - 1

        start = self._current_page * self._page_size
        playlists = all_playlists[start:start + self._page_size]

        # 批量查询子歌单数量（一次查询替代 N 次 get_child_count）
        child_counts = db.get_child_counts_batch([pl["path"] for pl in playlists])

        if self._view_stack.currentIndex() == 0:
            self._render_grid(playlists, child_counts)
        else:
            self._render_list(playlists, child_counts)

        # 更新分页控件
        self._update_pagination(len(all_playlists))

    def _render_grid(self, playlists, child_counts):
        """仅渲染网格视图"""
        while self.flow.count():
            item = self.flow.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        for pl in playlists:
            has_children = child_counts.get(pl["path"], 0) > 0
            card = PlaylistCard(pl, has_children=has_children)
            card.clicked.connect(self._on_card_click)
            self.flow.addWidget(card)

        self._grid_page.update()

    def _render_list(self, playlists, child_counts):
        """仅渲染列表视图（不加载封面图标，避免翻页 IO 卡顿）"""
        self.list_widget.clear()
        for pl in playlists:
            has_children = child_counts.get(pl["path"], 0) > 0
            suffix = " 📁" if has_children else ""
            text = f"{pl['name']}    {pl['track_count']} 首曲目{suffix}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, pl["id"])
            item.setData(Qt.ItemDataRole.UserRole + 1, has_children)
            item.setData(Qt.ItemDataRole.UserRole + 2, pl["path"])
            item.setData(Qt.ItemDataRole.UserRole + 3, pl["name"])
            self.list_widget.addItem(item)

    def _go_page(self, delta):
        """翻页：delta 为 -1（上一页）或 1（下一页）"""
        total_pages = max(1, (len(self._filtered_cache) + self._page_size - 1) // self._page_size)
        new_page = self._current_page + delta
        if 0 <= new_page < total_pages:
            self._current_page = new_page
            self._render(self._filtered_cache)
            # 翻页后滚动到顶部
            self._grid_scroll.verticalScrollBar().setValue(0)
            self.list_widget.verticalScrollBar().setValue(0)

    def _update_pagination(self, total_count):
        """更新分页控件状态"""
        total_pages = max(1, (total_count + self._page_size - 1) // self._page_size)
        self._page_label.setText(f"第 {self._current_page + 1} 页 / 共 {total_pages} 页")
        self._prev_btn.setEnabled(self._current_page > 0)
        self._next_btn.setEnabled(self._current_page < total_pages - 1)

    def _on_card_click(self, pl_id):
        """点击歌单卡片 → 打开详情页"""
        self.openPlaylist.emit(pl_id)

    def _on_list_item_clicked(self, item):
        pl_id = item.data(Qt.ItemDataRole.UserRole)
        if pl_id is not None:
            self.openPlaylist.emit(pl_id)

    def _on_list_context_menu(self, pos):
        item = self.list_widget.itemAt(pos)
        if item is None:
            return
        pl_path = item.data(Qt.ItemDataRole.UserRole + 2)
        pl_name = item.data(Qt.ItemDataRole.UserRole + 3) or ""
        if not pl_path:
            return
        menu = QMenu(self)
        copy_action = menu.addAction("复制歌单名称")
        copy_action.triggered.connect(lambda: QApplication.clipboard().setText(pl_name))
        menu.addSeparator()
        action = menu.addAction("打开所在文件夹")

        def _open_folder():
            try:
                os.startfile(pl_path)
            except OSError as e:
                QMessageBox.warning(self, "打开失败", f"无法打开文件夹:\n{e}")

        action.triggered.connect(_open_folder)
        menu.exec(self.list_widget.viewport().mapToGlobal(pos))