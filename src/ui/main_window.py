from PyQt6.QtWidgets import (
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QLineEdit,
    QScrollArea,
    QLabel,
    QMessageBox,
    QProgressBar,
    QFrame,
    QStackedWidget,
    QDialog,
    QCheckBox,
    QFileDialog,
    QSizePolicy,
)
import threading
import os

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QPoint, QRect, QEvent
from PyQt6.QtGui import QShortcut, QKeySequence, QIcon

from resource_manager import database as db
from resource_manager import scanner
from resource_manager import config
from ui.grid_view import WorkCard
from ui.image_viewer import ImageViewer
from ui.work_images_view import WorkImagesView
from ui.flow_layout import FlowLayout


# ---- 主题配色（3D 黏土风 / 轻拟物新拟态） ----
ACCENT = "#42B4C2"                 # 强调色（青蓝）
ACCENT_HOVER = "#48B8BC"           # 强调色悬停
ACCENT_PRESSED = "#3DA0AE"         # 强调色按下
ACCENT_TINT = "rgba(66, 180, 194, 0.15)"   # 半透明 accent 背景
ACCENT_TINT_LIGHT = "rgba(66, 180, 194, 0.08)"


# 全局滚动条样式（窄条 + 圆角 + 悬停变亮）
_GLOBAL_SCROLLBAR_QSS = """
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #CCC0B0;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: #BBB0A0;
}
QScrollBar::handle:vertical:pressed {
    background: """ + ACCENT + """;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}
QScrollBar:horizontal {
    background: transparent;
    height: 0;
}
"""

# 通用按钮样式（hover 时变 accent 色）
_BTN_QSS = f"""
QPushButton {{
    background-color: #EDE6DA;
    color: #555555;
    padding: 6px 14px;
    border: 1px solid #D5CDC0;
    border-radius: 4px;
}}
QPushButton:hover {{
    background-color: #F3EFE8;
    border: 1px solid {ACCENT};
    color: #444;
}}
QPushButton:pressed {{
    background-color: {ACCENT_PRESSED};
    border: 1px solid {ACCENT_PRESSED};
    color: #fff;
}}
"""


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setFixedSize(480, 320)
        self.setStyleSheet("""
            SettingsDialog {
                background-color: #F5F0E8;
                color: #555555;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        # ---- 资源路径 ----
        path_section = QLabel("资源目录")
        path_section.setStyleSheet("color: #555555; font-size: 16px; font-weight: bold;")
        layout.addWidget(path_section)

        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self.path_edit = QLineEdit(config.PHOTO_ROOT)
        self.path_edit.setStyleSheet(
            "background-color: #EDE6DA; color: #555555; padding: 6px; "
            "border: 1px solid #D5CDC0; border-radius: 4px;"
        )
        path_row.addWidget(self.path_edit, 1)

        browse_btn = QPushButton("浏览")
        browse_btn.setStyleSheet(
            "background-color: #EDE6DA; color: #555555; padding: 6px 12px; "
            "border: 1px solid #D5CDC0; border-radius: 4px;"
        )
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        path_desc = QLabel("写真资源的根目录路径，程序将从这里扫描作者/作品/图片。")
        path_desc.setWordWrap(True)
        path_desc.setStyleSheet("color: #999999; font-size: 11px;")
        layout.addWidget(path_desc)

        # ---- 分割线 ----
        sep = QLabel("─" * 50)
        sep.setStyleSheet("color: #D5CDC0;")
        layout.addWidget(sep)

        # ---- GPU 设置 ----
        gpu_section = QLabel("GPU 加速")
        gpu_section.setStyleSheet("color: #555555; font-size: 16px; font-weight: bold;")
        layout.addWidget(gpu_section)

        self.gpu_check = QCheckBox("启用 GPU 解码加速")
        self.gpu_check.setChecked(config.GPU_FRIENDLY_FORMAT)
        self.gpu_check.setStyleSheet("""
            QCheckBox {
                color: #555555;
                font-size: 13px;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
            }
        """)
        layout.addWidget(self.gpu_check)

        gpu_desc = QLabel(
            "使用 GPU 友好的像素格式 (RGBX8888) 加速图片解码和渲染。\n"
            "修改路径或 GPU 设置后需要重启程序生效。"
        )
        gpu_desc.setWordWrap(True)
        gpu_desc.setStyleSheet("color: #999999; font-size: 11px;")
        layout.addWidget(gpu_desc)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        save_btn = QPushButton("保存")
        save_btn.setStyleSheet("""
            QPushButton {
                background-color: #EDE6DA;
                color: #555555;
                padding: 8px 24px;
                border: 1px solid #D5CDC0;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #F3EFE8;
                border: 1px solid #42B4C2;
            }
            QPushButton:pressed {
                background-color: #48B8BC;
                color: #fff;
            }
        """)
        save_btn.clicked.connect(self.on_save)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "选择写真根目录", self.path_edit.text())
        if path:
            self.path_edit.setText(path)

    def on_save(self):
        new_path = self.path_edit.text().strip()
        if not os.path.isdir(new_path):
            QMessageBox.warning(self, "路径无效", f"目录不存在: {new_path}")
            return
        # 通过 set_user_config 同步模块级变量 + _user_config
        config.set_user_config("PHOTO_ROOT", new_path)
        config.set_user_config("GPU_FRIENDLY_FORMAT", self.gpu_check.isChecked())
        config.save_user_config()
        self.accept()
        QMessageBox.information(self, "提示", "设置已保存，重启程序后生效。")


class ScanThread(QThread):
    finished = pyqtSignal(dict)  # stats
    error = pyqtSignal(str)
    progress = pyqtSignal(str, str, str, int, int, int)  # stage, author, work, count, current, total
    author_done = pyqtSignal(str, int)  # author_name, work_count

    def __init__(self, total_authors=0):
        super().__init__()
        self.total_authors = total_authors
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            stats = scanner.scan(
                progress_callback=self._emit_progress,
                author_done_callback=self._emit_author_done,
                cancel_event=self.cancel_event,
                total_authors=self.total_authors,
            )
            self.finished.emit(stats or {})
        except Exception as e:
            self.error.emit(str(e))

    def _emit_progress(self, stage, author, work, count, current, total):
        self.progress.emit(stage, author, work, count, current, total)

    def _emit_author_done(self, author_name, work_count):
        self.author_done.emit(author_name, work_count)


class MainWindow(QMainWindow):
    def __init__(self, on_back_to_launcher=None):
        super().__init__()
        self._on_back_to_launcher = on_back_to_launcher
        self.setWindowTitle(f"本地资源管理器 - 写真 v{config.APP_VERSION}")
        self.resize(1200, 800)

        # 初始化数据库
        db.init_db()

        # 全局滚动条样式（影响所有子 widget 的 QScrollBar）
        self.setStyleSheet(_GLOBAL_SCROLLBAR_QSS)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)

        # 左侧栏
        sidebar = QWidget()
        sidebar.setFixedWidth(220)
        sidebar.setStyleSheet("background-color: #FDF9F2; color: #555555;")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(10, 10, 10, 10)

        self.author_list = QListWidget()
        self.author_list.setStyleSheet(
            f"""
            QListWidget {{
                background-color: #FDF9F2;
                color: #555555;
                border: none;
                outline: none;
            }}
            QListWidget::item {{
                padding: 8px 10px;
                border-radius: 4px;
                border-left: 2px solid transparent;
            }}
            QListWidget::item:hover {{
                background-color: #E8E0D5;
            }}
            QListWidget::item:selected {{
                background-color: {ACCENT_TINT};
                color: #444444;
                border-left: 2px solid {ACCENT};
            }}
        """
        )
        self.author_list.currentItemChanged.connect(self.on_author_changed)

        all_item = QListWidgetItem("全部作者")
        all_item.setData(Qt.ItemDataRole.UserRole, None)
        self.author_list.addItem(all_item)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        scan_btn = QPushButton("扫描")
        scan_btn.setStyleSheet(_BTN_QSS)
        scan_btn.clicked.connect(self.start_scan)

        settings_btn = QPushButton("设置")
        settings_btn.setStyleSheet(_BTN_QSS)
        settings_btn.clicked.connect(self.open_settings)

        btn_row.addWidget(scan_btn)
        btn_row.addWidget(settings_btn)

        sidebar_label = QLabel("作者列表")
        sidebar_label.setStyleSheet("color: #999999; font-size: 11px; padding: 4px 2px;")
        back_launcher_btn = QPushButton("← 返回主界面")
        back_launcher_btn.setStyleSheet(_BTN_QSS)
        back_launcher_btn.clicked.connect(self._back_to_launcher)

        sidebar_layout.addWidget(sidebar_label)
        sidebar_layout.addWidget(self.author_list)
        sidebar_layout.addLayout(btn_row)
        sidebar_layout.addWidget(back_launcher_btn)

        # 右侧内容
        content = QWidget()
        content.setStyleSheet("background-color: #F5F0E8;")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        top.setContentsMargins(12, 12, 12, 0)
        self.back_btn = QPushButton("← 返回")
        self.back_btn.setStyleSheet(_BTN_QSS)
        self.back_btn.setVisible(False)
        self.back_btn.clicked.connect(self.on_back_clicked)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("搜索作品名称...")
        self.search_box.setStyleSheet(
            f"""
            QLineEdit {{
                background-color: #EDE6DA;
                color: #555555;
                padding: 6px 10px;
                border: 1px solid #D5CDC0;
                border-radius: 4px;
                selection-background-color: {ACCENT};
            }}
            QLineEdit:focus {{
                border: 1px solid {ACCENT};
                background-color: #F3EFE8;
            }}
            """
        )
        self.search_box.textChanged.connect(self.load_works)
        top.addWidget(self.back_btn)
        top.addWidget(self.search_box)
        content_layout.addLayout(top)

        # 作品列表页
        self.works_page = QWidget()
        works_page_layout = QVBoxLayout(self.works_page)
        works_page_layout.setContentsMargins(0, 0, 0, 0)
        self.works_scroll = QScrollArea()
        self.works_scroll.setWidgetResizable(True)
        self.works_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.works_scroll.setStyleSheet("QScrollArea { border: none; background-color: #F5F0E8; }")
        self.works_scroll.verticalScrollBar().valueChanged.connect(self._on_works_scroll)
        self.works_scroll.viewport().installEventFilter(self)

        self.grid_container = QWidget()
        # 垂直方向用 Preferred，让 QScrollArea 调用 heightForWidth 计算实际多行高度
        # 避免内容高度被限制为单行，导致可滚动范围不准
        self.grid_container.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.grid_layout = FlowLayout(self.grid_container, margin=16, h_spacing=16, v_spacing=16)
        self.works_scroll.setWidget(self.grid_container)
        works_page_layout.addWidget(self.works_scroll)

        # 滚动条步进：按一行卡片高度滚动，避免一次跳太多
        self._work_card_h = 280 + 16  # WorkCard 高度 280 + v_spacing 16

        # 滚动加载防抖
        self._works_scroll_timer = QTimer()
        self._works_scroll_timer.setSingleShot(True)
        self._works_scroll_timer.setInterval(80)
        self._works_scroll_timer.timeout.connect(self._load_visible_work_cards)

        # 窗口尺寸变化防抖
        self._works_resize_timer = QTimer()
        self._works_resize_timer.setSingleShot(True)
        self._works_resize_timer.setInterval(150)
        self._works_resize_timer.timeout.connect(self._load_visible_work_cards)

        self._work_cards = []

        # 作品页（缩略图墙）
        self.work_page = QWidget()
        work_page_layout = QVBoxLayout(self.work_page)
        work_page_layout.setContentsMargins(0, 0, 0, 0)
        self.work_page_content = QStackedWidget()
        work_page_layout.addWidget(self.work_page_content)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.works_page)
        self.stack.addWidget(self.work_page)
        content_layout.addWidget(self.stack)

        # 底部扫描状态条（非模态，扫描期间仍可正常使用主窗口）
        self.scan_status_bar = self._build_scan_status_bar()
        content_layout.addWidget(self.scan_status_bar)
        self.scan_status_bar.setVisible(False)

        layout.addWidget(sidebar)
        layout.addWidget(content)

        self.load_authors()
        self.load_works()

        self._setup_shortcuts()

        # 自动后台扫描（延迟 500ms，让 UI 先渲染）
        self._auto_scanning = True
        QTimer.singleShot(500, self.start_scan)

    def _build_scan_status_bar(self):
        """构建底部扫描状态条：进度条 + 状态文本 + 取消按钮"""
        bar = QFrame()
        bar.setStyleSheet(
            f"""
            QFrame {{
                background-color: #FDF9F2;
                border-top: 1px solid #E8E0D5;
            }}
            QLabel {{
                color: #555555;
                font-size: 12px;
                padding: 2px;
            }}
            QProgressBar {{
                border: none;
                background-color: #EDE6DA;
                border-radius: 3px;
                text-align: center;
                color: #555555;
                font-size: 11px;
                height: 14px;
            }}
            QProgressBar::chunk {{
                background-color: {ACCENT};
                border-radius: 3px;
            }}
            """
        )
        bar.setFixedHeight(40)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(12)

        self.scan_label = QLabel("准备扫描...")
        self.scan_label.setFixedWidth(360)

        self.scan_progress = QProgressBar()
        self.scan_progress.setFixedWidth(260)
        self.scan_progress.setRange(0, 100)
        self.scan_progress.setValue(0)

        self.scan_cancel_btn = QPushButton("取消")
        self.scan_cancel_btn.setStyleSheet(_BTN_QSS)
        self.scan_cancel_btn.setFixedWidth(60)
        self.scan_cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scan_cancel_btn.clicked.connect(self.cancel_scan)

        layout.addWidget(self.scan_label)
        layout.addWidget(self.scan_progress)
        layout.addStretch()
        layout.addWidget(self.scan_cancel_btn)
        return bar

    def load_authors(self):
        self.author_list.clear()

        all_item = QListWidgetItem("全部作者")
        all_item.setData(Qt.ItemDataRole.UserRole, None)
        self.author_list.addItem(all_item)

        for author in db.list_authors():
            item = QListWidgetItem(f"{author['name']} ({author['work_count']})")
            item.setData(Qt.ItemDataRole.UserRole, author["id"])
            self.author_list.addItem(item)

    def on_author_changed(self, current, previous):
        if current is None:
            return
        # 如果当前在作品图片页，切回作品列表
        if self.stack.currentIndex() == 1:
            self.stack.setCurrentIndex(0)
            self.back_btn.setVisible(False)
            if self.work_page_content.count() > 0:
                old = self.work_page_content.widget(0)
                self.work_page_content.removeWidget(old)
                old.deleteLater()
        self.load_works()

    def load_works(self):
        # 重建期间禁用布局更新，避免每添加一个 widget 就重排
        self.grid_container.setUpdatesEnabled(False)
        self.grid_container.setVisible(False)

        # 清空网格
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._work_cards = []

        current = self.author_list.currentItem()
        author_id = current.data(Qt.ItemDataRole.UserRole) if current else None
        keyword = self.search_box.text().strip().lower()

        works = db.list_works(author_id)
        if keyword:
            works = [w for w in works if keyword in w["name"].lower()]

        for work in works:
            card = WorkCard(work)
            card.clicked.connect(self.open_work)
            self.grid_layout.addWidget(card)
            self._work_cards.append(card)

        # 重建完成，恢复可见并一次性刷新
        self.grid_container.setVisible(True)
        self.grid_container.setUpdatesEnabled(True)
        # 强制让 FlowLayout 重新计算所有卡片的位置
        self.grid_layout.invalidate()
        self.grid_container.updateGeometry()
        # 通知 QScrollArea 重新计算 widget 高度（触发 heightForWidth）
        self.grid_container.adjustSize()
        # 设置滚动条步进为单行卡片高度
        self.works_scroll.verticalScrollBar().setSingleStep(self._work_card_h)
        # 延迟触发首次懒加载，确保布局已经计算完毕
        QTimer.singleShot(0, self._load_visible_work_cards)

    def _on_works_scroll(self, _value):
        self._works_scroll_timer.start()

    def eventFilter(self, obj, event):
        # 监听作品列表的视口 resize/show，触发懒加载
        if obj is self.works_scroll.viewport():
            if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
                self._works_resize_timer.start()
        return super().eventFilter(obj, event)

    def _load_visible_work_cards(self):
        """视口检测：仅加载可见区域内的作品卡片缩略图"""
        scroll = self.works_scroll
        if scroll is None:
            return
        viewport = scroll.viewport()
        vrect = viewport.rect()
        # 兜底：窗口未显示时 viewport 是 0×0，直接加载前 N 张
        viewport_valid = vrect.width() > 0 and vrect.height() > 0
        loaded = 0
        for card in self._work_cards:
            if loaded >= 32:
                break
            if card._thumb_loaded:
                continue
            if viewport_valid:
                top_left = card.mapTo(viewport, QPoint(0, 0))
                card_rect = QRect(top_left, card.size())
                if not vrect.intersects(card_rect):
                    continue
            card.load_thumbnail()
            loaded += 1

    def showEvent(self, event):
        super().showEvent(event)
        # 首次显示时触发一次懒加载（__init__ 中调用 load_works 时窗口尚未显示）
        QTimer.singleShot(0, self._load_visible_work_cards)

    def _setup_shortcuts(self):
        """注册全局键盘快捷键"""
        # F11：全屏切换
        QShortcut(QKeySequence("F11"), self, self.toggle_fullscreen)
        # Ctrl+F：聚焦搜索框
        QShortcut(QKeySequence("Ctrl+F"), self, self.search_box.setFocus)
        # Esc：返回（在作品图片墙页）
        QShortcut(QKeySequence("Esc"), self, self._handle_escape)
        # Backspace：返回（备选）
        QShortcut(QKeySequence("Backspace"), self, self._handle_backspace)
        # Ctrl+S：扫描
        QShortcut(QKeySequence("Ctrl+S"), self, self.start_scan)
        # Ctrl+R：刷新列表
        QShortcut(QKeySequence("Ctrl+R"), self, self._refresh)
        # F5：刷新列表（Windows 习惯）
        QShortcut(QKeySequence("F5"), self, self._refresh)

    def toggle_fullscreen(self):
        """全屏切换"""
        if self.isFullScreen():
            self.showMaximized()
        else:
            self.showFullScreen()

    def _handle_escape(self):
        """Esc：从作品图片墙返回作品列表，全屏退出，或清除搜索"""
        if self.isFullScreen():
            self.showMaximized()
            return
        if self.stack.currentIndex() == 1:
            self.on_back_from_work()
            return
        # 作品列表页：清除搜索框
        if self.search_box.text():
            self.search_box.clear()

    def _handle_backspace(self):
        """Backspace：返回（在作品图片墙页）"""
        # 注意：仅在 stack 不在搜索框焦点时生效，避免删除搜索文本
        if self.search_box.hasFocus():
            return
        if self.stack.currentIndex() == 1:
            self.on_back_from_work()

    def _refresh(self):
        """刷新作品列表"""
        self.load_authors()
        self.load_works()

    def open_work(self, work):
        images = db.get_images_by_work(work["id"])
        if not images:
            QMessageBox.information(self, "提示", "该作品下没有图片")
            return
        # 切换到作品页
        if self.work_page_content.count() > 0:
            old = self.work_page_content.widget(0)
            self.work_page_content.removeWidget(old)
            old.deleteLater()
        view = WorkImagesView(
            work,
            images,
            on_image_clicked=lambda idx: self.open_image_at(images, idx),
            on_back=self.on_back_from_work,
        )
        self.work_page_content.addWidget(view)
        self.work_page_content.setCurrentIndex(0)
        self.stack.setCurrentIndex(1)
        self.back_btn.setVisible(True)

    def open_image_at(self, images, index):
        paths = [img["path"] for img in images]
        viewer = ImageViewer(paths, start_index=index, parent=self)
        viewer.exec()

    def on_back_from_work(self):
        self.stack.setCurrentIndex(0)
        self.back_btn.setVisible(False)

    def _back_to_launcher(self):
        """返回主启动器界面"""
        self.close()

    def closeEvent(self, event):
        """窗口关闭时通知启动器恢复显示"""
        super().closeEvent(event)
        if self._on_back_to_launcher:
            cb = self._on_back_to_launcher
            self._on_back_to_launcher = None  # 防止重入
            cb()

    def on_back_clicked(self):
        if self.stack.currentIndex() == 1:
            self.on_back_from_work()
        else:
            self.search_box.clear()
            self.author_list.setCurrentItem(self.author_list.item(0))

    def open_settings(self):
        dialog = SettingsDialog(self)
        dialog.exec()

    def start_scan(self):
        # 防止重复启动扫描
        if getattr(self, "scan_thread", None) and self.scan_thread.isRunning():
            return

        total = scanner.count_authors()
        self.scan_status_bar.setVisible(True)
        self.scan_progress.setValue(0)
        self.scan_label.setText("准备扫描...")
        self.scan_cancel_btn.setEnabled(True)

        self.scan_thread = ScanThread(total_authors=total)
        self.scan_thread.finished.connect(self.on_scan_finished)
        self.scan_thread.error.connect(self.on_scan_error)
        self.scan_thread.progress.connect(self.on_scan_progress)
        self.scan_thread.author_done.connect(self.on_author_done)
        self.scan_thread.start()

    def cancel_scan(self):
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.cancel()
            self.scan_cancel_btn.setEnabled(False)
            self.scan_label.setText("正在取消...")
            self.scan_thread.wait(3000)
        self._hide_scan_status_bar()

    def on_scan_progress(self, stage, author, work, count, current, total):
        if stage == "author":
            if total > 0:
                pct = int(current / total * 100)
                self.scan_progress.setValue(pct)
            self.scan_label.setText(f"正在同步作者: {author}  ({current}/{total})")
        elif stage == "work":
            self.scan_label.setText(f"作者: {author}  作品: {work}  图片数: {count}")
        elif stage == "done":
            self.scan_progress.setValue(100)
            self.scan_label.setText("正在清理孤儿缩略图...")

    def on_author_done(self, author_name, work_count):
        # 每扫完一个作者，实时刷新左侧作者列表和右侧网格
        self.load_authors()
        current = self.author_list.currentItem()
        author_id = current.data(Qt.ItemDataRole.UserRole) if current else None
        if author_id is None:
            self.load_works()

    def on_scan_finished(self, stats):
        auto = getattr(self, "_auto_scanning", False)
        self._auto_scanning = False

        if not stats:
            self._hide_scan_status_bar()
            if not auto:
                QMessageBox.information(self, "完成", "扫描完成")
            return

        # 指纹未变化，跳过扫描
        if stats.get("unchanged"):
            self.scan_progress.setValue(100)
            self.scan_label.setText("目录无变化，已跳过扫描")
            # 延迟 1.5 秒后自动隐藏状态条
            QTimer.singleShot(1500, self._hide_scan_status_bar)
            return

        self._hide_scan_status_bar()
        self.load_authors()
        self.load_works()
        if auto:
            return  # 自动扫描，静默完成

        # 显示增量扫描统计
        msg = (
            f"扫描完成\n\n"
            f"新增图片: {stats.get('added', 0)} 张\n"
            f"更新图片: {stats.get('updated', 0)} 张\n"
            f"删除图片: {stats.get('deleted', 0)} 张\n"
            f"删除作品: {stats.get('deleted_works', 0)} 个\n"
            f"删除作者: {stats.get('deleted_authors', 0)} 个\n"
            f"未变化:   {stats.get('skipped', 0)} 张\n"
            f"清理缩略图: {stats.get('gc_thumbs', 0)} 个"
        )
        QMessageBox.information(self, "扫描完成", msg)

    def on_scan_error(self, msg):
        self._hide_scan_status_bar()
        auto = getattr(self, "_auto_scanning", False)
        self._auto_scanning = False
        if auto:
            return  # 自动扫描，静默处理错误
        QMessageBox.critical(self, "扫描失败", msg)

    def _hide_scan_status_bar(self):
        """隐藏扫描状态条并重置进度"""
        self.scan_status_bar.setVisible(False)
        self.scan_progress.setValue(0)
        self.scan_label.setText("准备扫描...")
        self.scan_cancel_btn.setEnabled(True)
