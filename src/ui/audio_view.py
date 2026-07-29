"""
音频模块主窗口
歌单浏览（网格/列表双视图 + 层级导航）+ 音频播放器
"""
import os
import threading

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QPushButton, QLabel,
    QScrollArea, QFrame, QSlider, QSizePolicy, QMessageBox,
    QFileDialog, QStackedWidget, QLineEdit, QDialog,
    QProgressBar, QComboBox, QMenu,
)
from PyQt6.QtCore import Qt, QSize, QUrl, pyqtSignal, QThread, QTimer
from PyQt6.QtGui import QPixmap, QFont, QShortcut, QKeySequence, QIcon
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput

from audio_manager import database as db
from audio_manager import scanner as audio_scanner
from resource_manager import config
from ui.flow_layout import FlowLayout
from ui.tag_widgets import TagButton, TagChip, TagSelectorDialog, parse_tags


# ---- 配色（3D 黏土风 / 轻拟物新拟态） ----
ACCENT = "#42B4C2"
ACCENT_HOVER = "#48B8BC"
ACCENT_TINT = "rgba(66, 180, 194, 0.15)"
BG_MAIN = "#F5F0E8"
BG_SIDEBAR = "#FDF9F2"
CARD_BG = "#FFFFFF"
CARD_HOVER = "#FFFDF8"
TEXT_PRIMARY = "#555555"
TEXT_MUTED = "#999999"
TEXT_DIM = "#BBBBBB"
INPUT_BG = "#EDE6DA"
BORDER_COLOR = "#D5CDC0"
PLAYER_BG = "#EDE6DA"

BTN_QSS = """
QPushButton {
    background-color: #EDE6DA; color: #555;
    padding: 6px 14px; border: 1px solid #D5CDC0; border-radius: 4px;
}
QPushButton:hover {
    background-color: #F3EFE8; border: 1px solid """ + ACCENT + """; color: #444;
}
QPushButton:pressed {
    background-color: #48B8BC; border: 1px solid #48B8BC; color: #fff;
}
"""

# ---- 辅助 ----
def _format_time(seconds):
    if seconds <= 0:
        return "00:00"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


def _get_basename(path):
    """获取路径的显示名"""
    return os.path.basename(path) if path else "根目录"


# =============================================================
#  PlaylistCard — 歌单卡片（网格模式用）
# =============================================================
class PlaylistCard(QFrame):
    clicked = pyqtSignal(int)

    def __init__(self, pl, has_children=False, parent=None):
        super().__init__(parent)
        self.pl_id = pl["id"]
        self._pl_path = pl["path"]
        self._has_children = has_children
        self.setFixedSize(180, 240)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            PlaylistCard {{
                background-color: {CARD_BG};
                border: 1px solid {BORDER_COLOR};
                border-top: 2px solid transparent;
                border-radius: 6px;
            }}
            PlaylistCard:hover {{
                background-color: {CARD_HOVER};
                border: 1px solid #D5CDC0;
                border-top: 2px solid {ACCENT};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 8)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # 封面
        self.cover_label = QLabel()
        self.cover_label.setFixedSize(160, 140)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setStyleSheet("background-color: #F5F0E8; border-radius: 4px;")
        self.cover_label.setText("...")
        layout.addWidget(self.cover_label, alignment=Qt.AlignmentFlag.AlignCenter)

        # 歌单名（自动换行）
        name = pl["name"]
        name_label = QLabel(name)
        name_label.setWordWrap(True)
        name_label.setFixedWidth(160)
        name_label.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px;")
        layout.addWidget(name_label)

        # 子标题行
        sub_row = QHBoxLayout()
        sub_row.setSpacing(2)
        count = QLabel(f"{pl['track_count']} 首")
        count.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px;")
        sub_row.addWidget(count)

        if has_children:
            folder_hint = QLabel("📁")
            folder_hint.setStyleSheet(f"color: {ACCENT}; font-size: 12px;")
            sub_row.addWidget(folder_hint)

        sub_row.addStretch()
        layout.addLayout(sub_row)
        layout.addStretch()

        # 加载封面
        cover_path = pl.get("cover")
        if cover_path and os.path.exists(cover_path):
            pix = QPixmap(cover_path)
            if not pix.isNull():
                scaled = pix.scaled(160, 140, Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
                self.cover_label.setPixmap(scaled)
                self.cover_label.setText("")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.pl_id)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        action = menu.addAction("打开所在文件夹")
        action.triggered.connect(lambda: os.startfile(self._pl_path))
        menu.exec(event.globalPos())


# =============================================================
#  SubPlaylistCard — 详情页内的子歌单小卡片
# =============================================================
class SubPlaylistCard(QFrame):
    """详情页内的子歌单——紧凑列表样式"""
    clicked = pyqtSignal(int)

    def __init__(self, pl, parent=None):
        super().__init__(parent)
        self.pl_id = pl["id"]
        self.setFixedHeight(32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            SubPlaylistCard {{
                background-color: transparent;
                border: 1px solid transparent;
                border-radius: 4px;
                padding: 2px 8px;
            }}
            SubPlaylistCard:hover {{
                background-color: #EDE6DA;
                border: 1px solid #D5CDC0;
            }}
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(8)

        name = pl["name"]
        name_label = QLabel(name)
        name_label.setStyleSheet(f"color: {ACCENT}; font-size: 12px;")
        layout.addWidget(name_label)

        cnt = QLabel(f"{len(db.list_tracks(pl['id']))} 首")
        cnt.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")
        layout.addWidget(cnt)

        layout.addStretch()

        arrow = QLabel("▶")
        arrow.setStyleSheet("color: #555; font-size: 10px;")
        layout.addWidget(arrow)

    def mousePressEvent(self, event):
        self.clicked.emit(self.pl_id)


# =============================================================
#  PlaylistBrowser — 歌单浏览器（层级导航 + 网格/列表切换）
# =============================================================
class PlaylistBrowser(QWidget):
    """支持进入子文件夹、面包屑导航"""
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
            # 时间排序（按文件夹修改时间）
            reverse = idx == 2  # 新→旧: reverse=True
            self._filtered_cache = sorted(filtered, key=lambda pl: os.path.getmtime(pl["path"]), reverse=reverse)

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
            name = _get_basename(p)
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
        """渲染歌单列表到两种视图（按当前页码分页）"""
        total_pages = max(1, (len(all_playlists) + self._page_size - 1) // self._page_size)
        if self._current_page >= total_pages:
            self._current_page = total_pages - 1

        start = self._current_page * self._page_size
        playlists = all_playlists[start:start + self._page_size]

        # 清除网格
        while self.flow.count():
            item = self.flow.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        for pl in playlists:
            child_cnt = db.get_child_count(pl["path"])
            card = PlaylistCard(pl, has_children=(child_cnt > 0))
            card.clicked.connect(self._on_card_click)
            self.flow.addWidget(card)

        self._grid_page.update()

        # 列表视图
        self.list_widget.clear()
        for pl in playlists:
            child_cnt = db.get_child_count(pl["path"])
            suffix = " 📁" if child_cnt > 0 else ""
            text = f"{pl['name']}    {pl['track_count']} 首曲目{suffix}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, pl["id"])
            item.setData(Qt.ItemDataRole.UserRole + 1, child_cnt > 0)
            item.setData(Qt.ItemDataRole.UserRole + 2, pl["path"])
            if pl.get("cover") and os.path.exists(pl["cover"]):
                pix = QPixmap(pl["cover"])
                if not pix.isNull():
                    icon_pix = pix.scaled(40, 40, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation)
                    item.setIcon(QIcon(icon_pix))
            self.list_widget.addItem(item)

        # 更新分页控件
        self._update_pagination(len(all_playlists))

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
        if not pl_path:
            return
        menu = QMenu(self)
        action = menu.addAction("打开所在文件夹")
        action.triggered.connect(lambda: os.startfile(pl_path))
        menu.exec(self.list_widget.viewport().mapToGlobal(pos))


# =============================================================
#  设置对话框
# =============================================================
class AudioSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("音频设置")
        self.setFixedSize(480, 320)
        self.setStyleSheet(f"AudioSettingsDialog {{ background-color: {BG_SIDEBAR}; color: {TEXT_PRIMARY}; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        section = QLabel("音频目录")
        section.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 16px; font-weight: bold;")
        layout.addWidget(section)

        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self.path_edit = QLineEdit(config.AUDIO_ROOT)
        self.path_edit.setStyleSheet(
            f"background-color: {INPUT_BG}; color: {TEXT_PRIMARY}; padding: 6px; "
            f"border: 1px solid {BORDER_COLOR}; border-radius: 4px;"
        )
        path_row.addWidget(self.path_edit, 1)

        browse_btn = QPushButton("浏览")
        browse_btn.setStyleSheet(f"background-color: {BORDER_COLOR}; color: {TEXT_PRIMARY}; padding: 6px 12px; border-radius: 4px;")
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        desc = QLabel("音频资源的根目录路径，每个子文件夹作为一个歌单。\n修改后需要重启程序生效。")
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(desc)
        layout.addStretch()

        btn_layout = QHBoxLayout()
        delete_btn = QPushButton("删除当前数据库")
        delete_btn.setStyleSheet("""
            QPushButton { background-color: #D4736E; color: #fff;
                padding: 8px 16px; border-radius: 4px; }
            QPushButton:hover { background-color: #E08580; }
        """)
        delete_btn.clicked.connect(self._delete_db)
        btn_layout.addWidget(delete_btn)
        btn_layout.addStretch()
        save_btn = QPushButton("保存")
        save_btn.setStyleSheet("""
            QPushButton { background-color: #CCC0B0; color: #fff;
                padding: 8px 24px; border-radius: 4px; }
            QPushButton:hover { background-color: #BBB0A0; }
        """)
        save_btn.clicked.connect(self.on_save)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

    def _delete_db(self):
        reply = QMessageBox.question(
            self, "确认删除",
            "确定要删除当前音频数据库吗？\n所有歌单和曲目数据将被清空。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        db.clear_all()
        QMessageBox.information(self, "提示", "数据库已清空。\n请重新扫描以重建歌单。")

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "选择音频根目录", self.path_edit.text())
        if path:
            self.path_edit.setText(path)

    def on_save(self):
        new_path = self.path_edit.text().strip()
        if not os.path.isdir(new_path):
            QMessageBox.warning(self, "路径无效", f"目录不存在: {new_path}")
            return
        config.set_user_config("AUDIO_ROOT", new_path)
        config.save_user_config()
        self.accept()
        QMessageBox.information(self, "提示", "设置已保存，重启程序后生效。")


# =============================================================
#  扫描线程
# =============================================================
class AudioScanThread(QThread):
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)

    def __init__(self, audio_root):
        super().__init__()
        self.audio_root = audio_root
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            stats = audio_scanner.scan(
                audio_root=self.audio_root,
                progress_callback=self._emit_progress,
                cancel_event=self.cancel_event,
            )
            self.finished.emit(stats or {})
        except Exception as e:
            self.error.emit(str(e))

    def _emit_progress(self, current, total, msg):
        self.progress.emit(current, total, msg)


# =============================================================
#  AudioMainWindow — 音频主窗口
# =============================================================
class AudioMainWindow(QMainWindow):
    def __init__(self, on_back_to_launcher=None):
        super().__init__()
        self._on_back_to_launcher = on_back_to_launcher
        self.setWindowTitle("本地资源管理器 - 音频")
        self.resize(1080, 700)

        self._current_playlist_id = None
        self._current_track_index = -1
        self._detail_history = []  # 详情页导航栈
        self._tracks_data = []
        self._seeking = False  # 是否正在手动拖动进度条

        db.init_db()

        # 播放器
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(self._on_player_error)
        self.audio_output.setVolume(0.8)

        self.setStyleSheet(f"QMainWindow {{ background-color: {BG_MAIN}; }}")
        self._setup_ui()
        self._setup_shortcuts()
        self._refresh_playlists()

        # 自动后台扫描（延迟 500ms，让 UI 先渲染）
        self._auto_scanning = True
        QTimer.singleShot(500, self.start_scan)

    # ==================== UI 构建 ====================
    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- 顶部栏 ----
        toolbar = QFrame()
        toolbar.setFixedHeight(44)
        toolbar.setStyleSheet(f"background-color: {BG_SIDEBAR}; border: none;")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(8, 4, 8, 4)
        toolbar_layout.setSpacing(8)

        back_btn = QPushButton("← 返回主界面")
        back_btn.setStyleSheet(BTN_QSS)
        back_btn.clicked.connect(self._back_to_launcher)
        toolbar_layout.addWidget(back_btn)
        toolbar_layout.addSpacing(12)

        scan_btn = QPushButton("扫描")
        scan_btn.setStyleSheet(BTN_QSS)
        scan_btn.clicked.connect(self.start_scan)
        toolbar_layout.addWidget(scan_btn)

        settings_btn = QPushButton("设置")
        settings_btn.setStyleSheet(BTN_QSS)
        settings_btn.clicked.connect(self.open_settings)
        toolbar_layout.addWidget(settings_btn)
        toolbar_layout.addStretch()
        layout.addWidget(toolbar)

        # ---- 页面切换（歌单浏览 / 曲目详情） ----
        self._page_stack = QStackedWidget()
        layout.addWidget(self._page_stack, 1)

        # — 页面 0: 带层级导航的歌单浏览器
        self.browser = PlaylistBrowser()
        self.browser.openPlaylist.connect(self._open_playlist)
        self._page_stack.addWidget(self.browser)  # index 0

        # — 页面 1: 曲目详情页
        self._detail_page = QWidget()
        self._detail_page.setStyleSheet(f"background-color: {BG_MAIN};")
        detail_layout = QVBoxLayout(self._detail_page)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(0)

        detail_toolbar = QFrame()
        detail_toolbar.setFixedHeight(40)
        detail_toolbar.setStyleSheet(f"background-color: {BG_SIDEBAR}; border: none;")
        dt_layout = QHBoxLayout(detail_toolbar)
        dt_layout.setContentsMargins(8, 4, 8, 4)
        self.detail_back_btn = QPushButton("← 返回歌单列表")
        self.detail_back_btn.setStyleSheet(BTN_QSS)
        self.detail_back_btn.clicked.connect(self._detail_back)
        dt_layout.addWidget(self.detail_back_btn)
        dt_layout.addStretch()
        detail_layout.addWidget(detail_toolbar)

        info_scroll = QScrollArea()
        info_scroll.setWidgetResizable(True)
        info_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        info_scroll.setStyleSheet(f"QScrollArea {{ background-color: {BG_MAIN}; border: none; }}")

        info_content = QWidget()
        info_content.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        info_layout = QVBoxLayout(info_content)
        info_layout.setContentsMargins(16, 16, 16, 8)
        info_layout.setSpacing(8)

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
        info_layout.addLayout(info_row)

        # 子歌单区域（动态填充，Type 1 歌单显示）
        self.sub_header = QLabel("子歌单")
        self.sub_header.setStyleSheet(f"color: #BBB; font-size: 13px; font-weight: bold; padding-top: 8px;")
        self.sub_header.setVisible(False)
        info_layout.addWidget(self.sub_header)

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
        info_layout.addWidget(self.sub_scroll)

        track_header = QLabel("曲目列表")
        track_header.setStyleSheet(f"color: #BBB; font-size: 13px; font-weight: bold; padding-top: 8px;")
        self.track_header = track_header
        info_layout.addWidget(self.track_header)

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
        info_layout.addWidget(self.track_list, 1)

        info_scroll.setWidget(info_content)
        detail_layout.addWidget(info_scroll, 1)
        self._page_stack.addWidget(self._detail_page)  # index 1

        # ---- 播放栏 ----
        self.player_bar = QFrame()
        self.player_bar.setFixedHeight(56)
        self.player_bar.setStyleSheet("""
            QFrame { background-color: #EDE6DA; border-top: 1px solid #EDE6DA; }
        """)
        player_layout = QHBoxLayout(self.player_bar)
        player_layout.setContentsMargins(12, 6, 12, 6)
        player_layout.setSpacing(8)

        self.now_playing = QLabel("未播放")
        self.now_playing.setMinimumWidth(160)
        self.now_playing.setStyleSheet("color: #777; font-size: 12px;")
        player_layout.addWidget(self.now_playing)

        self.prev_btn = QPushButton("⏮")
        self.prev_btn.setFixedSize(32, 28)
        self.prev_btn.setStyleSheet(f"background: transparent; color: #BBB; border: none; font-size: 16px; padding: 0; QPushButton:hover {{ color: {TEXT_PRIMARY}; }}")
        self.prev_btn.clicked.connect(self.play_previous)
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
        self.play_btn.clicked.connect(self.toggle_play)
        player_layout.addWidget(self.play_btn)

        self.next_btn = QPushButton("⏭")
        self.next_btn.setFixedSize(32, 28)
        self.next_btn.setStyleSheet(f"background: transparent; color: #BBB; border: none; font-size: 16px; padding: 0; QPushButton:hover {{ color: {TEXT_PRIMARY}; }}")
        self.next_btn.clicked.connect(self.play_next)
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
        self.progress_slider.sliderMoved.connect(self._seek)
        self.progress_slider.sliderReleased.connect(self._on_slider_released)
        self.progress_slider.sliderPressed.connect(self._on_slider_pressed)
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
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        player_layout.addWidget(self.volume_slider)

        layout.addWidget(self.player_bar)
        self.player_bar.setVisible(False)

        # 扫描状态栏
        self._scan_bar = self._build_scan_bar()
        layout.addWidget(self._scan_bar)
        self._scan_bar.setVisible(False)

    def _build_scan_bar(self):
        bar = QFrame()
        bar.setFixedHeight(36)
        bar.setStyleSheet(f"""
            QFrame {{ background-color: {BG_SIDEBAR}; border-top: 1px solid #EDE6DA; }}
            QLabel {{ color: #777; font-size: 11px; padding: 2px; }}
            QProgressBar {{
                border: none; background: #EDE6DA; border-radius: 2px;
                text-align: center; color: {TEXT_PRIMARY}; font-size: 10px; height: 12px;
            }}
            QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 2px; }}
        """)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)
        self.scan_label = QLabel("准备扫描...")
        self.scan_label.setFixedWidth(340)
        self.scan_progress = QProgressBar()
        self.scan_progress.setFixedWidth(260)
        self.scan_progress.setRange(0, 100)
        self.scan_progress.setValue(0)
        self.scan_cancel = QPushButton("取消")
        self.scan_cancel.setStyleSheet(BTN_QSS)
        self.scan_cancel.setFixedWidth(50)
        self.scan_cancel.clicked.connect(self._cancel_scan)
        layout.addWidget(self.scan_label)
        layout.addWidget(self.scan_progress)
        layout.addStretch()
        layout.addWidget(self.scan_cancel)
        return bar

    # ==================== 数据加载 ====================
    def _refresh_playlists(self):
        self.browser.reset()  # 回到根目录刷新

    def _detail_back(self):
        """详情页返回按钮：返回上一级详情或歌单列表"""
        if len(self._detail_history) > 1:
            self._detail_history.pop()
            self._open_playlist(self._detail_history[-1])
        else:
            self._detail_history = []
            self._page_stack.setCurrentIndex(0)

    @staticmethod
    def _collect_tracks_recursive(pl_id):
        """递归收集歌单及其所有子歌单的所有曲目"""
        result = []
        pl = db.get_playlist(pl_id)
        if not pl:
            return result
        result.extend(db.list_tracks(pl_id))
        for child in db.list_playlists_by_parent(pl["path"]):
            result.extend(AudioMainWindow._collect_tracks_recursive(child["id"]))
        return result

    @staticmethod
    def _find_first_cover(pl):
        """递归查找歌单或其子歌单的第一个可用封面"""
        cover = pl.get("cover")
        if cover and os.path.exists(cover):
            return cover
        for child in db.list_playlists_by_parent(pl["path"]):
            child_pl = db.get_playlist(child["id"])
            if child_pl:
                found = AudioMainWindow._find_first_cover(child_pl)
                if found:
                    return found
        return None

    def _open_playlist(self, pl_id):
        # 导航历史：首次从浏览器进入时重置，子歌单点击时追加
        if self._page_stack.currentIndex() != 1:
            self._detail_history = [pl_id]
        elif pl_id != self._detail_history[-1]:
            self._detail_history.append(pl_id)

        self._current_playlist_id = pl_id
        self.player.stop()
        self.player_bar.setVisible(False)
        self._current_track_index = -1

        pl = db.get_playlist(pl_id)
        if not pl:
            return

        # 更新返回按钮文字
        if len(self._detail_history) <= 1:
            self.detail_back_btn.setText("\u2190 返回歌单列表")
        else:
            self.detail_back_btn.setText("\u2190 返回上级")

        self.playlist_title.setText(pl["name"])

        # 标签和封面：子歌单使用主歌单的，主歌单用自己的
        main_pl = db.get_playlist(self._detail_history[0]) if len(self._detail_history) > 1 else pl
        tags_str = main_pl.get("tags", "") or ""
        self._rebuild_tag_buttons(tags_str)

        cover_path = main_pl.get("cover")
        if not (cover_path and os.path.exists(cover_path)):
            cover_path = self._find_first_cover(main_pl)
        got_cover = False
        if cover_path and os.path.exists(cover_path):
            pix = QPixmap(cover_path)
            if not pix.isNull():
                self.cover_label.setPixmap(
                    pix.scaled(160, 160, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
                )
                self.cover_label.setText("")
                got_cover = True
        if not got_cover:
            self.cover_label.setPixmap(QPixmap())
            self.cover_label.setText("\u65e0\u5c01\u9762")

        # 加载曲目：有直接音频则用自身的，否则递归聚合所有子歌单曲目
        direct_tracks = db.list_tracks(pl_id)
        if direct_tracks:
            self._tracks_data = direct_tracks
        else:
            raw = self._collect_tracks_recursive(pl_id)
            self._tracks_data = []
            seen = set()
            for tr in raw:
                if tr["path"] not in seen:
                    seen.add(tr["path"])
                    self._tracks_data.append(tr)

        # 收集后代歌单，决定显示策略
        descendants = self._collect_descendant_playlists(pl["path"])
        single_child = len(descendants) == 1 and not direct_tracks
        multi_child = len(descendants) >= 2

        if multi_child:
            # 2+ 子歌单：保留曲目区域可见（防止布局塌陷），但清空并显示提示
            self.track_header.setVisible(True)
            self.track_list.setVisible(True)
            self.track_list.clear()
            self.playlist_count.setText(f"共 {len(descendants)} 个子歌单")
        else:
            # 0 或 1 子歌单：显示曲目列表
            self.track_header.setVisible(True)
            self.track_list.setVisible(True)
            track_count = len(self._tracks_data)
            self.playlist_count.setText(f"共 {track_count} 首曲目")
            self.track_list.clear()
            for i, tr in enumerate(self._tracks_data):
                title = tr["title"] or os.path.basename(tr["path"])
                item = QListWidgetItem(f"{i + 1}. {title}")
                item.setData(Qt.ItemDataRole.UserRole, i)
                self.track_list.addItem(item)

        # 子歌单区域
        if multi_child:
            self._rebuild_sub_playlists(pl_id)
        else:
            # 单子歌单或无子歌单：隐藏子歌单卡片
            while self.sub_vbox.count():
                item = self.sub_vbox.takeAt(0)
                if item and item.widget():
                    item.widget().deleteLater()
            self.sub_header.setVisible(False)
            self.sub_scroll.setVisible(False)

        self._page_stack.setCurrentIndex(1)

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

    def _on_tag_clicked(self, tag):
        """点击标签按钮 → 返回歌单浏览器并追加标签过滤（与已有标签叠加 AND 逻辑）"""
        self._page_stack.setCurrentIndex(0)
        self.browser.filter_by_tag(tag)

    def _rebuild_sub_playlists(self, pl_id):
        """重建详情页中的子歌单卡片。递归收集所有后代歌单（非仅直接子歌单）。"""
        pl = db.get_playlist(pl_id)
        if not pl:
            self.sub_header.setVisible(False)
            self.sub_scroll.setVisible(False)
            return

        # 递归收集所有后代歌单
        all_descendants = self._collect_descendant_playlists(pl["path"])
        # 清除旧的子歌单卡片
        while self.sub_vbox.count():
            item = self.sub_vbox.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        if not all_descendants:
            self.sub_header.setVisible(False)
            self.sub_scroll.setVisible(False)
            return

        self.sub_header.setVisible(True)
        self.sub_scroll.setVisible(True)
        for child in all_descendants:
            card = SubPlaylistCard(child)
            card.clicked.connect(self._open_sub_playlist)
            self.sub_vbox.addWidget(card)

    def _collect_descendant_playlists(self, path):
        """递归收集指定路径下的所有后代歌单（深度优先平铺）"""
        result = []
        children = db.list_playlists_by_parent(path)
        for child in children:
            result.append(child)
            result.extend(self._collect_descendant_playlists(child["path"]))
        return result

    def _open_sub_playlist(self, pl_id):
        """点击详情页内的子歌单卡片"""
        self._open_playlist(pl_id)

    # ==================== 播放控制 ====================
    def _on_track_double_clicked(self, item):
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is not None:
            self._play_track(idx)

    def _play_track(self, index):
        if index < 0 or index >= len(self._tracks_data):
            return
        self._current_track_index = index
        track = self._tracks_data[index]
        path = track["path"]
        if not os.path.exists(path):
            QMessageBox.warning(self, "文件不存在", f"找不到文件: {path}")
            return
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()
        self.play_btn.setText("⏸")
        title = track["title"] or os.path.basename(path)
        # 截断：从头开始最多 10 个字符
        display = title[:10] + "..." if len(title) > 10 else title
        self.now_playing.setText(f"♪ {display}")
        self.player_bar.setVisible(True)
        for i in range(self.track_list.count()):
            it = self.track_list.item(i)
            if it and it.data(Qt.ItemDataRole.UserRole) == index:
                self.track_list.setCurrentItem(it)
                break

    def toggle_play(self):
        state = self.player.playbackState()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.play_btn.setText("▶")
        elif state == QMediaPlayer.PlaybackState.PausedState:
            self.player.play()
            self.play_btn.setText("⏸")
        else:
            if self._tracks_data and self._current_track_index >= 0:
                self._play_track(self._current_track_index)
            elif self._tracks_data:
                self._play_track(0)

    def play_next(self):
        if not self._tracks_data:
            return
        idx = self._current_track_index + 1
        if idx >= len(self._tracks_data):
            idx = 0
        self._play_track(idx)

    def play_previous(self):
        if not self._tracks_data:
            return
        idx = self._current_track_index - 1
        if idx < 0:
            idx = len(self._tracks_data) - 1
        self._play_track(idx)

    def _on_position_changed(self, pos_ms):
        if not self.progress_slider.isSliderDown():
            sec = pos_ms // 1000
            self.progress_slider.setValue(int(sec))
            self.time_current.setText(_format_time(sec))

    def _on_slider_pressed(self):
        pass  # slider 按下瞬间无需额外操作

    def _on_slider_released(self):
        """松开进度条时跳转到当前滑块位置（点击跳转）"""
        pos = self.progress_slider.value()
        self._seek(pos)

    def _on_duration_changed(self, dur_ms):
        sec = dur_ms // 1000
        self.progress_slider.setRange(0, int(sec))
        self.time_total.setText(_format_time(sec))

    def _on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.play_next()

    def _on_player_error(self, error, error_string):
        if error != QMediaPlayer.Error.NoError:
            self.now_playing.setText(f"播放错误: {error_string}")

    def _seek(self, pos):
        """拖动/点击进度条时跳转并刷新时间显示"""
        self.time_current.setText(_format_time(pos))
        self.player.setPosition(pos * 1000)

    def _on_volume_changed(self, val):
        self.audio_output.setVolume(val / 100.0)

    # ==================== 扫描 ====================
    def start_scan(self):
        if getattr(self, "scan_thread", None) and self.scan_thread.isRunning():
            return
        self._scan_bar.setVisible(True)
        self.scan_progress.setValue(0)
        self.scan_label.setText("准备扫描...")
        self.scan_cancel.setEnabled(True)

        self.scan_thread = AudioScanThread(config.AUDIO_ROOT)
        self.scan_thread.finished.connect(self._on_scan_finished)
        self.scan_thread.error.connect(self._on_scan_error)
        self.scan_thread.progress.connect(self._on_scan_progress)
        self.scan_thread.start()

    def _cancel_scan(self):
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.cancel()
            self.scan_cancel.setEnabled(False)
            self.scan_label.setText("正在取消...")
        self._scan_bar.setVisible(False)

    def _on_scan_progress(self, current, total, msg):
        if total > 0:
            self.scan_progress.setValue(int(current / total * 100))
        # 截断过长的文件/文件夹名称，保持标签宽度稳定
        if len(msg) > 42:
            msg = msg[:39] + "..."
        self.scan_label.setText(msg)

    def _on_scan_finished(self, stats):
        auto = getattr(self, "_auto_scanning", False)
        self._auto_scanning = False

        # 指纹未变化，跳过扫描
        if stats.get("unchanged"):
            self.scan_progress.setValue(100)
            self.scan_label.setText("目录无变化，已跳过扫描")
            QTimer.singleShot(2000, self._scan_bar.hide)
            return

        self._scan_bar.setVisible(False)
        self._refresh_playlists()
        if auto:
            return  # 自动扫描有变化时也静默完成
        msg_parts = []
        for k, v in [("added_playlists", "新增歌单"), ("removed_playlists", "删除歌单"),
                     ("added_tracks", "新增曲目"), ("removed_tracks", "删除曲目")]:
            if stats.get(k):
                msg_parts.append(f"{v}: {stats[k]}")
        msg = "扫描完成\n\n" + "\n".join(msg_parts) if msg_parts else "扫描完成\n\n无变化"
        QMessageBox.information(self, "扫描完成", msg)

    def _on_scan_error(self, msg):
        self._scan_bar.setVisible(False)
        auto = getattr(self, "_auto_scanning", False)
        self._auto_scanning = False
        if auto:
            return  # 自动扫描，静默处理错误
        QMessageBox.critical(self, "扫描失败", msg)

    # ==================== 设置 / 返回 ====================
    def open_settings(self):
        AudioSettingsDialog(self).exec()

    def _back_to_launcher(self):
        self.player.stop()
        self.close()

    def closeEvent(self, event):
        self.player.stop()
        super().closeEvent(event)
        if self._on_back_to_launcher:
            cb = self._on_back_to_launcher
            self._on_back_to_launcher = None
            cb()

    def _setup_shortcuts(self):
        QShortcut(QKeySequence("Space"), self, self.toggle_play)
        QShortcut(QKeySequence("Left"), self, self.play_previous)
        QShortcut(QKeySequence("Right"), self, self.play_next)
        QShortcut(QKeySequence("Ctrl+S"), self, self.start_scan)
