"""
标签组件：可点击的标签按钮、标签芯片（带 × 移除）、标签解析工具
"""
import re

from PyQt6.QtWidgets import (
    QPushButton, QFrame, QHBoxLayout, QVBoxLayout, QLabel, QWidget,
    QDialog, QListWidget, QListWidgetItem, QLineEdit,
)
from PyQt6.QtCore import Qt, pyqtSignal

from ui.flow_layout import FlowLayout


# ---- 配色（与主窗口黏土风统一） ----
ACCENT = "#42B4C2"
ACCENT_TINT = "rgba(66, 180, 194, 0.15)"
TEXT_PRIMARY = "#555555"
TEXT_MUTED = "#999999"
TEXT_DIM = "#BBBBBB"
TAG_BG = "#EDE6DA"


def parse_tags(tags_str: str) -> list[str]:
    """将标签字符串拆分为标签列表，支持空格、逗号（中英文）、换行分隔，去重后按字母排序。"""
    if not tags_str or not tags_str.strip():
        return []
    return sorted(
        set(t.strip() for t in re.split(r"[,，\n\r\s]+", tags_str) if t.strip()),
        key=str.lower,
    )


# =============================================================
#  TagButton — 详情页中的可点击标签
# =============================================================
class TagButton(QPushButton):
    """点击后触发标签搜索的胶囊按钮"""

    tagClicked = pyqtSignal(str)

    def __init__(self, tag: str, parent=None):
        super().__init__(tag, parent)
        self._tag = tag
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(26)
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: {TAG_BG}; color: {TEXT_PRIMARY};
                border: 1px solid transparent; border-radius: 13px;
                padding: 2px 14px; font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {ACCENT}; color: #fff;
                border: 1px solid {ACCENT};
            }}
            QPushButton:pressed {{
                background-color: {ACCENT_TINT}; color: {TEXT_PRIMARY};
            }}
        """)
        self.clicked.connect(self._on_click)

    def _on_click(self):
        self.tagClicked.emit(self._tag)

    def get_tag(self) -> str:
        return self._tag


# =============================================================
#  TagChip — 导航栏中的活动标签芯片（带 × 移除按钮）
# =============================================================
class TagChip(QFrame):
    """显示活动标签，右侧带 × 按钮可移除"""

    removed = pyqtSignal(str)

    def __init__(self, tag: str, parent=None):
        super().__init__(parent)
        self._tag = tag
        self.setFixedHeight(24)
        self.setStyleSheet(f"""
            TagChip {{
                background-color: {ACCENT_TINT};
                border: 1px solid {ACCENT};
                border-radius: 12px;
                padding: 0px;
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 4, 0)
        layout.setSpacing(4)

        label = QLabel(tag)
        label.setStyleSheet(
            f"color: {ACCENT}; font-size: 11px; background: transparent; border: none;"
        )
        layout.addWidget(label)

        close_btn = QPushButton("×")
        close_btn.setFixedSize(16, 16)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT_DIM};
                border: none; font-size: 14px; padding: 0; margin: 0;
            }}
            QPushButton:hover {{ color: {ACCENT}; }}
        """)
        close_btn.clicked.connect(self._on_remove)
        layout.addWidget(close_btn)

    def _on_remove(self):
        self.removed.emit(self._tag)

    def get_tag(self) -> str:
        return self._tag


# =============================================================
#  TagFilterWidget — 浏览器导航栏的标签过滤区域
# =============================================================
class TagFilterWidget(QWidget):
    """封装活动标签芯片的容器，自动换行布局，支持添加/移除标签"""

    tagRemoved = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = FlowLayout(self, margin=0, h_spacing=4, v_spacing=2)
        self.setContentsMargins(0, 0, 0, 2)
        self.setLayout(self._layout)
        self.setVisible(False)

    def set_tags(self, tags: set):
        """设置并显示标签芯片"""
        # 清除旧芯片
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        for tag in sorted(tags, key=str.lower):
            chip = TagChip(tag)
            chip.removed.connect(self._on_chip_removed)
            self._layout.addWidget(chip)

        self.setVisible(bool(tags))

    def _on_chip_removed(self, tag):
        self.tagRemoved.emit(tag)


# =============================================================
#  TagSelectorDialog — 全量标签选择窗口（多选）
# =============================================================
BG_SIDEBAR = "#FDF9F2"
BORDER_COLOR = "#D5CDC0"


class TagSelectorDialog(QDialog):
    """显示所有歌单标签，支持勾选多选，确定后返回选中标签"""

    def __init__(self, all_tags: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择标签")
        self.setFixedSize(320, 420)
        self.setStyleSheet(f"QDialog {{ background-color: {BG_SIDEBAR}; color: {TEXT_PRIMARY}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        title = QLabel("选择要过滤的标签（可多选）")
        title.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 14px; font-weight: bold; border: none;"
        )
        layout.addWidget(title)

        # 搜索输入框
        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("搜索标签...")
        self._search_box.setStyleSheet(f"""
            QLineEdit {{
                background-color: #EDE6DA; color: {TEXT_PRIMARY};
                border: 1px solid {BORDER_COLOR}; border-radius: 4px;
                padding: 4px 8px; font-size: 12px;
            }}
            QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
        """)
        self._search_box.textChanged.connect(self._filter_tags)
        layout.addWidget(self._search_box)

        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet(f"""
            QListWidget {{
                background-color: #FFFFFF; color: {TEXT_PRIMARY};
                border: 1px solid {BORDER_COLOR}; border-radius: 4px;
                outline: none;
            }}
            QListWidget::item {{
                padding: 6px 8px; border-bottom: 1px solid #E8E0D5;
            }}
            QListWidget::item:hover {{ background-color: {ACCENT_TINT}; }}
            QListWidget::item:selected {{
                background-color: {ACCENT_TINT}; color: {TEXT_PRIMARY};
            }}
        """)

        self._all_tags = all_tags
        for tag in all_tags:
            item = QListWidgetItem(tag)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.list_widget.addItem(item)

        layout.addWidget(self.list_widget, 1)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(6)

        select_all_btn = QPushButton("全选")
        select_all_btn.setFixedHeight(28)
        select_all_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {TAG_BG}; color: {TEXT_PRIMARY};
                border: 1px solid {BORDER_COLOR}; border-radius: 4px;
                padding: 4px 12px; font-size: 12px;
            }}
            QPushButton:hover {{ border: 1px solid {ACCENT}; }}
        """)
        select_all_btn.clicked.connect(self._select_all)
        btn_layout.addWidget(select_all_btn)

        clear_btn = QPushButton("清除")
        clear_btn.setFixedHeight(28)
        clear_btn.setStyleSheet(select_all_btn.styleSheet())
        clear_btn.clicked.connect(self._clear_all)
        btn_layout.addWidget(clear_btn)

        btn_layout.addStretch()

        ok_btn = QPushButton("确定")
        ok_btn.setFixedHeight(28)
        ok_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {ACCENT}; color: #fff;
                border: none; border-radius: 4px;
                padding: 4px 20px; font-size: 12px;
            }}
            QPushButton:hover {{ background-color: #48B8BC; }}
        """)
        ok_btn.clicked.connect(self.accept)
        btn_layout.addWidget(ok_btn)

        cancel_btn = QPushButton("取消")
        cancel_btn.setFixedHeight(28)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {TAG_BG}; color: {TEXT_PRIMARY};
                border: 1px solid {BORDER_COLOR}; border-radius: 4px;
                padding: 4px 12px; font-size: 12px;
            }}
            QPushButton:hover {{ border: 1px solid {ACCENT}; }}
        """)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

    def get_selected_tags(self) -> list[str]:
        """返回所有勾选的标签"""
        result = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                result.append(item.text())
        return result

    def _select_all(self):
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setCheckState(Qt.CheckState.Checked)

    def _clear_all(self):
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setCheckState(Qt.CheckState.Unchecked)

    def _filter_tags(self, text: str):
        """按搜索文本过滤标签列表"""
        text = text.strip().lower()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            item.setHidden(bool(text) and text not in item.text().lower())
