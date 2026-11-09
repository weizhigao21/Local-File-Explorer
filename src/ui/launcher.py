"""
主入口启动器：作为各功能模块的入口界面
包含模块卡片网格，点击卡片打开对应模块窗口
"""
import os
import subprocess

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

import search_index
from resource_manager import config
from ui.search_panel import SearchPanel
from ui.theme_base import (
    ACCENT,
    ACCENT_HOVER,
    ACCENT_TINT,
    BG_MAIN,
    BG_SIDEBAR,
    CARD_BG,
    CARD_BORDER,
    CARD_BORDER_HOVER,
    CARD_HOVER,
    INPUT_BG,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)

__all__ = [
    # 从 theme_base 转出的设计令牌（保持既有 `from ui.launcher import ACCENT` 可用）
    "ACCENT", "ACCENT_HOVER", "ACCENT_TINT",
    "BG_MAIN", "BG_SIDEBAR", "CARD_BG", "CARD_HOVER",
    "CARD_BORDER", "CARD_BORDER_HOVER",
    "TEXT_PRIMARY", "TEXT_SECONDARY", "TEXT_MUTED", "TEXT_DIM",
    # 本模块对外接口
    "ModuleCard", "LauncherWindow", "MODULES",
]

# 模块定义
MODULES = [
    {
        "id": "photo",
        "icon": "\U0001F5BC",  # 🖼
        "title": "写真",
        "description": "浏览和管理本地写真资源\n按作者-作品-图片三级结构",
        "status": "ready",
    },
    {
        "id": "anime",
        "icon": "\U0001F3AC",  # 🎬
        "title": "动漫",
        "description": "浏览和管理本地动漫资源\n支持系列、剧集分类浏览",
        "status": "coming_soon",
    },
    {
        "id": "video",
        "icon": "\U0001F4FA",  # 📺
        "title": "视频",
        "description": "浏览和管理本地视频资源\n作者-作品-视频三级结构，内置播放",
        "status": "ready",
    },
    {
        "id": "audio",
        "icon": "\U0001F3B5",  # 🎵
        "title": "音频",
        "description": "浏览和管理本地音频资源\n支持歌单分类和在线播放",
        "status": "ready",
    },
]


class ModuleCard(QFrame):
    """模块卡片组件"""

    clicked = pyqtSignal(str)  # module id

    def __init__(self, module, parent=None):
        super().__init__(parent)
        self._module_id = module["id"]
        self._status = module["status"]
        self.setFixedSize(240, 260)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        base_border_style = (
            "border: 1px solid #C4B394;"
            if module["status"] == "coming_soon"
            else f"border: 1px solid {CARD_BORDER};"
        )
        base_border_style += " border-top: 2px solid transparent;"

        self.setStyleSheet(
            f"""
            ModuleCard {{
                background-color: {CARD_BG};
                {base_border_style}
                border-radius: 8px;
            }}
            ModuleCard:hover {{
                background-color: {CARD_HOVER};
                border: 1px solid {TEXT_MUTED};
                border-top: 2px solid {ACCENT};
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 20, 16, 16)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(8)

        # 图标
        icon_label = QLabel(module["icon"])
        icon_font = QFont("Segoe UI Emoji", 42)
        icon_label.setFont(icon_font)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setFixedHeight(64)

        # 开发中状态标签
        if module["status"] == "coming_soon":
            status_label = QLabel("开发中...")
            status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            status_label.setFixedHeight(20)
            status_label.setStyleSheet(
                f"color: {TEXT_DIM}; font-size: 11px; background: transparent; border: none;"
            )
        else:
            status_label = None

        # 标题
        title = QLabel(module["title"])
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 18px; font-weight: bold; "
            f"background: transparent; border: none;"
        )

        # 描述
        desc = QLabel(module["description"])
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 11px; line-height: 1.5; "
            f"background: transparent; border: none;"
        )

        layout.addStretch()
        layout.addWidget(icon_label)
        layout.addSpacing(4)
        if status_label:
            layout.addWidget(status_label)
        layout.addSpacing(4)
        layout.addWidget(title)
        layout.addSpacing(4)
        layout.addWidget(desc)
        layout.addStretch()

    def mouseReleaseEvent(self, event):
        # 仅左键在卡片内释放才进入模块（原先 mousePressEvent 不判按键，右键也会误触发）
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._module_id)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def module_id(self):
        return self._module_id


class LauncherWindow(QMainWindow):
    """主入口启动器窗口"""

    playerRequested = pyqtSignal()
    systemRequested = pyqtSignal()
    webRequested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"本地资源管理器 v{config.APP_VERSION}")
        self.setMinimumSize(1150, 600)
        self.resize(1180, 650)

        # 保存已打开的模块窗口引用
        self._module_windows = {}

        self._setup_ui()

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        central.setStyleSheet(f"background-color: {BG_MAIN};")

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # ---- 顶部标题栏 ----
        header = QFrame()
        header.setFixedHeight(160)
        header.setStyleSheet(f"background-color: {BG_SIDEBAR}; border: none;")

        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(40, 30, 40, 20)

        title = QLabel("本地资源管理器")
        title.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 28px; font-weight: bold; "
            f"background: transparent; border: none;"
        )

        version_label = QLabel(f"v{config.APP_VERSION}")
        version_label.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 13px; "
            f"background: transparent; border: none; "
            f"padding-top: 8px;"
        )

        # 标题 + 版本号水平布局
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(title)
        title_row.addWidget(version_label)
        title_row.addStretch()

        header_layout.addLayout(title_row)
        header_layout.addSpacing(4)

        # 副标题 + 缩放选择器
        sub_row = QHBoxLayout()
        subtitle = QLabel(
            "选择功能模块进入浏览 · 支持写真、动漫、视频、音频等资源管理"
        )
        subtitle.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 13px; "
            f"background: transparent; border: none;"
        )
        sub_row.addWidget(subtitle, 1)

        player_btn = QPushButton("♪ 播放器")
        player_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        player_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {CARD_BG}; color: {TEXT_PRIMARY};
                border: 1px solid {CARD_BORDER}; border-radius: 4px;
                padding: 4px 12px; font-size: 12px;
            }}
            QPushButton:hover {{ border: 1px solid {ACCENT}; background-color: {CARD_HOVER}; }}
        """)
        player_btn.clicked.connect(self.playerRequested.emit)
        sub_row.addWidget(player_btn)
        sub_row.addSpacing(8)

        system_btn = QPushButton("系统集成")
        system_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        system_btn.setToolTip("注册为 Windows 可选音乐播放器 / 打开默认应用设置")
        system_btn.setStyleSheet(player_btn.styleSheet())
        system_btn.clicked.connect(self.systemRequested.emit)
        sub_row.addWidget(system_btn)
        sub_row.addSpacing(8)

        # 手机/平板访问：不是资源模块，放顶部比塞进卡片网格更合理
        # （五张 240px 卡片加间距会超出最小窗口宽度，第四张开始就会被压扁）
        web_btn = QPushButton("📱 手机访问")
        web_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        web_btn.setToolTip("启动内置网页服务，手机/平板用浏览器听音乐、看写真、看视频")
        web_btn.setStyleSheet(player_btn.styleSheet())
        web_btn.clicked.connect(self.webRequested.emit)
        sub_row.addWidget(web_btn)
        sub_row.addSpacing(8)

        # 日志：打包成 exe 后没有控制台，出问题只能靠这个文件
        log_btn = QPushButton("日志")
        log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        log_btn.setToolTip("在资源管理器中定位 data/app.log（排障时看这里）")
        log_btn.setStyleSheet(player_btn.styleSheet())
        log_btn.clicked.connect(self._open_log)
        sub_row.addWidget(log_btn)
        sub_row.addSpacing(12)

        scale_label = QLabel("UI缩放:")
        scale_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px; background: transparent; border: none;")
        sub_row.addWidget(scale_label)

        self.scale_selector = QComboBox()
        self.scale_selector.addItems(["50%", "75%", "100%", "125%", "150%", "175%", "200%"])
        self.scale_selector.setFixedWidth(80)
        self.scale_selector.setStyleSheet(f"""
            QComboBox {{
                background-color: #EDE2CE; color: {TEXT_PRIMARY};
                border: 1px solid #E0D5C2; border-radius: 4px;
                padding: 2px 6px; font-size: 11px;
            }}
            QComboBox::drop-down {{ border: none; width: 16px; }}
            QComboBox:hover {{ border: 1px solid {ACCENT}; }}
        """)
        self.scale_selector.currentTextChanged.connect(self._on_scale_changed)
        # 设置当前值（阻止初始化信号触发）
        current_scale = getattr(config, "UI_SCALE", 125)
        self.scale_selector.blockSignals(True)
        self.scale_selector.setCurrentText(f"{current_scale}%")
        self.scale_selector.blockSignals(False)
        sub_row.addWidget(self.scale_selector)

        header_layout.addLayout(sub_row)
        header_layout.addStretch()

        main_layout.addWidget(header)

        # ---- 模块卡片区域 ----
        content = QWidget()
        content.setStyleSheet(f"background-color: {BG_MAIN}; border: none;")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(40, 30, 40, 30)

        # 全局搜索：不先选模块，直接找「那个温泉的视频」
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(
            "搜索作品 / 作者 / 歌单 / 歌曲 / 视频…    (Ctrl+K)"
        )
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setFixedHeight(34)
        self.search_box.setMaximumWidth(560)
        self.search_box.setStyleSheet(
            f"""
            QLineEdit {{
                background-color: {INPUT_BG}; color: {TEXT_PRIMARY};
                border: 1px solid {CARD_BORDER}; border-radius: 6px;
                padding: 2px 10px; font-size: 13px;
            }}
            QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
            """
        )
        self.search_box.textChanged.connect(self._on_search_text)
        self.search_box.returnPressed.connect(self._open_first_result)
        content_layout.addWidget(self.search_box)
        content_layout.addSpacing(14)

        self.section_label = QLabel("功能模块")
        self.section_label.setStyleSheet(
            f"color: {TEXT_SECONDARY}; font-size: 14px; font-weight: bold; "
            f"background: transparent; border: none;"
        )
        content_layout.addWidget(self.section_label)
        content_layout.addSpacing(16)

        # 卡片页 / 搜索结果页共用同一块空间：输入即切，清空即回
        self.content_stack = QStackedWidget()

        cards_page = QWidget()
        cards_page.setStyleSheet(f"background-color: {BG_MAIN}; border: none;")
        cards_layout = QVBoxLayout(cards_page)
        cards_layout.setContentsMargins(0, 0, 0, 0)

        # 卡片网格（水平居中，自动换行）
        card_grid = QHBoxLayout()
        card_grid.setSpacing(24)
        card_grid.setAlignment(Qt.AlignmentFlag.AlignCenter)

        for module in MODULES:
            card = ModuleCard(module)
            card.clicked.connect(self._on_card_clicked)
            card_grid.addWidget(card)

        cards_layout.addLayout(card_grid)
        cards_layout.addStretch()

        self.search_panel = SearchPanel()
        self.search_panel.resultActivated.connect(self._on_search_result)

        self.content_stack.addWidget(cards_page)
        self.content_stack.addWidget(self.search_panel)
        content_layout.addWidget(self.content_stack, 1)

        main_layout.addWidget(content, 1)

        # 输入防抖：每敲一个字就查三个库，快打时会连发十几条 SQL
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(220)
        self._search_timer.timeout.connect(self._run_search)

        for keys in ("Ctrl+K", "Ctrl+F"):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.activated.connect(self._focus_search)
        # Esc 清空搜索，回到模块卡片
        escape = QShortcut(QKeySequence("Escape"), self)
        escape.activated.connect(self._clear_search)

    def _open_first_result(self):
        first = self.search_panel.first_result()
        if first is not None:
            self._on_search_result(*first)

    def _clear_search(self):
        if self.search_box.text():
            self.search_box.clear()

    def _focus_search(self):
        self.search_box.setFocus()
        self.search_box.selectAll()

    def _on_search_text(self, text):
        if not text.strip():
            self._search_timer.stop()
            self.section_label.setText("功能模块")
            self.content_stack.setCurrentIndex(0)
            return
        self._search_timer.start()

    def _run_search(self):
        keyword = self.search_box.text().strip()
        if not keyword:
            return
        result = search_index.search(keyword)
        self.search_panel.set_results(result, keyword)
        total = result.get("total", 0)
        # 空结果时别在这里重复一遍「没有找到」——面板里已经写了
        self.section_label.setText(f"搜索结果 · {total} 个" if total else "搜索结果")
        self.content_stack.setCurrentIndex(1)

    def _on_search_result(self, module, item):
        """点搜索结果：开对应模块并直接跳到那一项"""
        kind = item.get("kind")
        payload = item.get("payload") or {}
        self.search_box.clear()

        win = self._open_module({"id": module, "title": "", "status": "ready"})
        if win is None:
            return

        try:
            if kind in ("photo_work", "video_work"):
                win.open_work(payload)
            elif kind == "audio_playlist":
                win.open_playlist(payload["id"])
            elif kind == "audio_track":
                win.open_playlist(payload["playlist_id"])
            elif kind == "video_file":
                work = {
                    "id": payload["work_id"],
                    "name": payload["work_name"],
                    "author": payload["author"],
                }
                win.open_work(work)
        except (AttributeError, KeyError, TypeError) as e:
            print(f"[全局搜索] 打开失败 {kind}: {e}")

    def _on_card_clicked(self, module_id):
        """处理卡片点击事件"""
        for m in MODULES:
            if m["id"] == module_id:
                if m["status"] == "coming_soon":
                    QMessageBox.information(
                        self,
                        "提示",
                        f"「{m['title']}」模块正在开发中，敬请期待！",
                    )
                    return
                self._open_module(m)
                return

    def open_module(self, module_id):
        """外部入口（命令行 --module）按 id 打开模块"""
        if module_id == "web":
            self._open_module({"id": "web", "title": "手机访问", "status": "ready"})
            return
        self._on_card_clicked(module_id)

    def _hide_other_modules(self, keep_mid):
        """同时只留一个模块窗口可见

        三个模块都是整屏大窗口，之前只 hide 启动器：写真、音频、视频能并排摆着，
        想回启动器还得挨个关。这里把其余模块收起来——只 hide 不 close，
        窗口状态、播放队列、滚动位置都留着，再进来还是原样。
        """
        for mid, win in list(self._module_windows.items()):
            if mid == keep_mid:
                continue
            try:
                if win.isVisible():
                    win.hide()
            except RuntimeError:
                # 底层 C++ 对象已销毁，引用留着只会在下次 raise_() 时炸
                del self._module_windows[mid]

    def _open_module(self, module):
        """打开指定模块（返回窗口实例，供搜索结果直接定位）"""
        mid = module["id"]

        self._hide_other_modules(mid)

        # 如果窗口已打开且未被销毁，激活它
        if mid in self._module_windows:
            win = self._module_windows[mid]
            try:
                # 必须 show()：切换模块时其余窗口被 hide 过，单靠 raise_()
                # 不会让隐藏的窗口重新出现（表现为「点了卡片什么都没发生」）
                if win.isMinimized():
                    win.showNormal()
                else:
                    win.show()
                win.raise_()
                win.activateWindow()
                self.hide()
                return win
            except RuntimeError:
                del self._module_windows[mid]

        if mid == "photo":
            from ui.main_window import MainWindow

            def _on_photo_closed():
                self._on_module_closed(mid)
                self.show()

            win = MainWindow(on_back_to_launcher=_on_photo_closed)
            self._module_windows[mid] = win
            self.hide()
            win.show()
            return win
        if mid == "audio":
            from ui.audio_view import AudioMainWindow

            def _on_audio_closed():
                self._on_module_closed(mid)
                self.show()

            win = AudioMainWindow(on_back_to_launcher=_on_audio_closed)
            win.playerRequested.connect(self.playerRequested.emit)
            self._module_windows[mid] = win
            self.hide()
            win.show()
            return win
        if mid == "video":
            from ui.video_view import VideoMainWindow

            def _on_video_closed():
                self._on_module_closed(mid)
                self.show()

            win = VideoMainWindow(on_back_to_launcher=_on_video_closed)
            self._module_windows[mid] = win
            self.hide()
            win.show()
            return win
        if mid == "web":
            from ui.server_window import ServerWindow

            def _on_web_closed():
                self._on_module_closed(mid)
                self.show()

            win = ServerWindow(on_back_to_launcher=_on_web_closed)
            self._module_windows[mid] = win
            self.hide()
            win.show()
            return win
        QMessageBox.information(
            self, "提示", f"「{module['title']}」模块暂未实现"
        )
        return None

    def _on_module_closed(self, module_id):
        """模块窗口关闭后清理引用"""
        if module_id in self._module_windows:
            del self._module_windows[module_id]

    def _open_log(self):
        """在资源管理器中定位 data/app.log（打包后没有控制台，排障唯一入口）"""
        from applog import log_path

        path = log_path()
        if not os.path.isfile(path):
            QMessageBox.information(self, "日志", f"还没有日志文件：\n{path}")
            return
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])  # noqa: S603,S607
        except OSError as e:
            QMessageBox.warning(self, "打不开日志", f"{path}\n\n{e}")

    def _on_scale_changed(self, text):
        """UI 缩放变更，保存配置并提示重启"""
        value = int(text.replace("%", ""))
        if value == getattr(config, "UI_SCALE", 125):
            return  # 未变化，跳过
        config.set_user_config("UI_SCALE", value)
        config.save_user_config()
        QMessageBox.information(
            self,
            "提示",
            f"UI 缩放已设置为 {text}，重启程序后生效。",
        )
