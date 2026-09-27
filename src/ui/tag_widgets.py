"""
标签组件：可点击的标签按钮、标签芯片（带 × 移除）、标签解析工具
"""
import re

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

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
        {t.strip() for t in re.split(r"[,，\n\r\s]+", tags_str) if t.strip()},
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
#  FilterSelectorDialog — 联合筛选选择窗口（标签 / CV / 社团 三个页签）
# =============================================================
BG_SIDEBAR = "#FDF9F2"
BORDER_COLOR = "#D5CDC0"

_LIST_QSS = f"""
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
"""

_SEARCH_QSS = f"""
    QLineEdit {{
        background-color: #EDE6DA; color: {TEXT_PRIMARY};
        border: 1px solid {BORDER_COLOR}; border-radius: 4px;
        padding: 4px 8px; font-size: 12px;
    }}
    QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
"""

_BTN_QSS = f"""
    QPushButton {{
        background-color: {TAG_BG}; color: {TEXT_PRIMARY};
        border: 1px solid {BORDER_COLOR}; border-radius: 4px;
        padding: 4px 12px; font-size: 12px;
    }}
    QPushButton:hover {{ border: 1px solid {ACCENT}; }}
"""


class _CheckListTab(QWidget):
    """选择器单个页签：搜索框 + 可勾选列表（显示文本带计数，UserRole 存实际筛选值）"""

    def __init__(self, placeholder, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(placeholder)
        self.search_box.setStyleSheet(_SEARCH_QSS)
        self.search_box.textChanged.connect(self._filter)
        layout.addWidget(self.search_box)

        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet(_LIST_QSS)
        self.list_widget.itemClicked.connect(self._toggle_item)  # 点击整行即切换勾选
        layout.addWidget(self.list_widget, 1)

    def set_items(self, display_items):
        """display_items: [(显示文本, 实际值, 是否预勾选), ...]"""
        self.list_widget.clear()
        for text, value, checked in display_items:
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, value)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
            self.list_widget.addItem(item)

    def get_checked_values(self) -> list:
        """返回所有勾选项的实际值（非显示文本）"""
        return [
            self.list_widget.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.list_widget.count())
            if self.list_widget.item(i).checkState() == Qt.CheckState.Checked
        ]

    def _toggle_item(self, item):
        """单击行内任意位置切换勾选状态（无需精确点中小方框）"""
        if item.checkState() == Qt.CheckState.Checked:
            item.setCheckState(Qt.CheckState.Unchecked)
        else:
            item.setCheckState(Qt.CheckState.Checked)

    def _filter(self, text):
        text = text.strip().lower()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            item.setHidden(bool(text) and text not in item.text().lower())

    def select_all(self):
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setCheckState(Qt.CheckState.Checked)

    def clear_all(self):
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setCheckState(Qt.CheckState.Unchecked)


class FilterSelectorDialog(QDialog):
    """联合筛选选择窗口：标签 / CV / 社团 三个页签，各自独立搜索与多选。

    确定后返回 (tags, circles, cvs) 三元组；
    CV/社团条目附带作品数辅助挑选；已激活的条件会预勾选。
    """

    def __init__(self, all_tags, circle_counts, cv_counts,
                 selected_tags=None, selected_circles=None, selected_cvs=None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加筛选条件")
        self.setFixedSize(380, 480)
        self.setStyleSheet(f"QDialog {{ background-color: {BG_SIDEBAR}; color: {TEXT_PRIMARY}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        title = QLabel("选择筛选条件（同页签内多选取「或」，页签之间叠加取「与」）")
        title.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 13px; font-weight: bold; border: none;"
        )
        layout.addWidget(title)

        selected_tags = set(selected_tags or [])
        selected_circles = set(selected_circles or [])
        selected_cvs = set(selected_cvs or [])

        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: 1px solid {BORDER_COLOR}; border-radius: 4px; background: #FFFFFF;
            }}
            QTabBar::tab {{
                background: {TAG_BG}; color: {TEXT_PRIMARY};
                padding: 5px 18px; border-top-left-radius: 4px; border-top-right-radius: 4px;
                font-size: 12px;
            }}
            QTabBar::tab:selected {{ background: {ACCENT}; color: #fff; }}
        """)

        self.tag_tab = _CheckListTab("搜索标签...")
        self.tag_tab.set_items([(t, t, t in selected_tags) for t in all_tags])
        self.tabs.addTab(self.tag_tab, f"标签 {len(all_tags)}")

        self.circle_tab = _CheckListTab("搜索社团...")
        self.circle_tab.set_items([
            (f"{name}（{count}）", name, name in selected_circles)
            for name, count in circle_counts.items()
        ])
        self.tabs.addTab(self.circle_tab, f"社团 {len(circle_counts)}")

        self.cv_tab = _CheckListTab("搜索声优...")
        self.cv_tab.set_items([
            (f"{name}（{count}）", name, name in selected_cvs)
            for name, count in cv_counts.items()
        ])
        self.tabs.addTab(self.cv_tab, f"CV {len(cv_counts)}")

        layout.addWidget(self.tabs, 1)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(6)

        select_all_btn = QPushButton("全选本页")
        select_all_btn.setFixedHeight(28)
        select_all_btn.setStyleSheet(_BTN_QSS)
        select_all_btn.clicked.connect(lambda: self.tabs.currentWidget().select_all())
        btn_layout.addWidget(select_all_btn)

        clear_btn = QPushButton("清空本页")
        clear_btn.setFixedHeight(28)
        clear_btn.setStyleSheet(_BTN_QSS)
        clear_btn.clicked.connect(lambda: self.tabs.currentWidget().clear_all())
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
        cancel_btn.setStyleSheet(_BTN_QSS)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

    def get_selected(self):
        """返回 (tags, circles, cvs) 三个值列表"""
        return (
            self.tag_tab.get_checked_values(),
            self.circle_tab.get_checked_values(),
            self.cv_tab.get_checked_values(),
        )
