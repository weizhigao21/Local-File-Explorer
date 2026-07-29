"""
主入口启动器：作为各功能模块的入口界面
包含模块卡片网格，点击卡片打开对应模块窗口
"""
from PyQt6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QPushButton,
    QSpacerItem,
    QSizePolicy,
    QMessageBox,
    QComboBox,
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize
from PyQt6.QtGui import QFont, QPixmap, QPainter, QColor, QPen

from resource_manager import config


# 配色方案（3D 黏土风 / 轻拟物新拟态）
ACCENT = "#42B4C2"
ACCENT_HOVER = "#48B8BC"
ACCENT_TINT = "rgba(66, 180, 194, 0.15)"
CARD_BG = "#FFFFFF"
CARD_HOVER = "#FFFDF8"
CARD_BORDER = "#E8E0D5"
CARD_BORDER_HOVER = "#D5CDC0"
BG_MAIN = "#F5F0E8"
BG_SIDEBAR = "#FDF9F2"
TEXT_PRIMARY = "#555555"
TEXT_SECONDARY = "#777777"
TEXT_MUTED = "#999999"
TEXT_DIM = "#BBBBBB"

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
        "description": "浏览和管理本地视频资源\n支持分类收藏和播放",
        "status": "coming_soon",
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
            "border: 1px solid #BBB;"
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

    def mousePressEvent(self, event):
        self.clicked.emit(self._module_id)

    def module_id(self):
        return self._module_id


class LauncherWindow(QMainWindow):
    """主入口启动器窗口"""

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

        scale_label = QLabel("UI缩放:")
        scale_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px; background: transparent; border: none;")
        sub_row.addWidget(scale_label)

        self.scale_selector = QComboBox()
        self.scale_selector.addItems(["50%", "75%", "100%", "125%", "150%", "175%", "200%"])
        self.scale_selector.setFixedWidth(80)
        self.scale_selector.setStyleSheet(f"""
            QComboBox {{
                background-color: #EDE6DA; color: {TEXT_PRIMARY};
                border: 1px solid #D5CDC0; border-radius: 4px;
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

        section_label = QLabel("功能模块")
        section_label.setStyleSheet(
            f"color: {TEXT_SECONDARY}; font-size: 14px; font-weight: bold; "
            f"background: transparent; border: none;"
        )
        content_layout.addWidget(section_label)
        content_layout.addSpacing(16)

        # 卡片网格（水平居中，自动换行）
        card_grid = QHBoxLayout()
        card_grid.setSpacing(24)
        card_grid.setAlignment(Qt.AlignmentFlag.AlignCenter)

        for module in MODULES:
            card = ModuleCard(module)
            card.clicked.connect(self._on_card_clicked)
            card_grid.addWidget(card)

        content_layout.addLayout(card_grid)
        content_layout.addStretch()

        main_layout.addWidget(content, 1)

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

    def _open_module(self, module):
        """打开指定模块"""
        mid = module["id"]

        # 如果窗口已打开且未被销毁，激活它
        if mid in self._module_windows:
            win = self._module_windows[mid]
            try:
                win.raise_()
                win.activateWindow()
                self.hide()
                return
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
        elif mid == "audio":
            from ui.audio_view import AudioMainWindow

            def _on_audio_closed():
                self._on_module_closed(mid)
                self.show()

            win = AudioMainWindow(on_back_to_launcher=_on_audio_closed)
            self._module_windows[mid] = win
            self.hide()
            win.show()
        else:
            QMessageBox.information(
                self, "提示", f"「{module['title']}」模块暂未实现"
            )

    def _on_module_closed(self, module_id):
        """模块窗口关闭后清理引用"""
        if module_id in self._module_windows:
            del self._module_windows[module_id]

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
