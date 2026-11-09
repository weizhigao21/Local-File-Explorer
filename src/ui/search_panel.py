"""启动器里的跨模块搜索结果面板

启动器只有一个搜索框：用户在写「那个温泉的视频」时不该先猜它在哪个模块。
这里只负责把 `search_index.search()` 的结果画成可点的行，点击后发
`resultActivated(module, item)` 交给启动器去开对应模块——面板自己不 import
任何模块窗口，保持能独立加载（与卡片组件同一约定）。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ui.text_utils import truncate_title
from ui.theme_base import (
    ACCENT,
    ACCENT_TINT,
    CARD_BG,
    CARD_BORDER,
    CARD_HOVER,
    INPUT_BG,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)

MODULE_LABEL = {"photo": "写真", "audio": "音频", "video": "视频"}


class _ResultRow(QFrame):
    activated = pyqtSignal(dict)

    def __init__(self, module, item, parent=None):
        super().__init__(parent)
        self.item = item
        self._module = module
        # QSS 想给 QFrame 上色必须自己负责绘制背景（见 theme 约定）
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("searchRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            f"""
            #searchRow {{
                background-color: {CARD_BG};
                border: 1px solid {CARD_BORDER};
                border-radius: 6px;
            }}
            #searchRow:hover {{
                background-color: {CARD_HOVER};
                border: 1px solid {ACCENT};
            }}
            """
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        badge = QLabel(MODULE_LABEL.get(module, module))
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedWidth(44)
        badge.setStyleSheet(
            f"background-color: {ACCENT_TINT}; color: {ACCENT};"
            f" border-radius: 4px; font-size: 11px; padding: 2px 0;"
        )
        layout.addWidget(badge)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        text_col.setContentsMargins(0, 0, 0, 0)
        title = QLabel(truncate_title(item.get("title") or ""))
        title.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 13px; font-weight: 600;"
            f" background: transparent;"
        )
        subtitle = QLabel(truncate_title(item.get("subtitle") or "", 34))
        subtitle.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 11px; background: transparent;"
        )
        text_col.addWidget(title)
        text_col.addWidget(subtitle)
        layout.addLayout(text_col, 1)

        arrow = QLabel("›")
        arrow.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 16px; background: transparent;")
        layout.addWidget(arrow)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.item)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class SearchPanel(QScrollArea):
    """搜索结果列表：分组标题 + 可点行，空结果给一句话"""

    resultActivated = pyqtSignal(str, dict)  # (module, item)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(f"QScrollArea {{ background-color: {INPUT_BG}; border: none; }}")

        self._container = QWidget()
        self._container.setStyleSheet(f"background-color: {INPUT_BG};")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(12, 10, 12, 10)
        self._layout.setSpacing(6)
        self.setWidget(self._container)

    def _clear(self):
        # takeAt 只是把控件从布局里摘掉，它仍然是子控件、仍占着上次的几何位置；
        # 必须 setParent(None) 立刻脱离父级，否则新结果会和旧结果叠在一起
        # （deleteLater 要等事件循环空闲，中间这一帧用户能看到两批结果）。
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def first_result(self):
        """回车直接开第一条（键盘党不用摸鼠标）；没有结果时返回 None"""
        rows = self.findChildren(_ResultRow)
        if not rows:
            return None
        row = rows[0]
        return row._module, row.item  # noqa: SLF001 - 面板自己的行，取模块名

    def set_results(self, result, keyword):
        self._clear()

        groups = result.get("groups") or []
        if not groups:
            empty = QLabel(f"没有找到「{keyword}」")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(
                f"color: {TEXT_SECONDARY}; font-size: 14px; padding: 40px 0;"
                f" background: transparent;"
            )
            hint = QLabel("试试作品名、作者名、歌单名或文件名的一部分")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px; background: transparent;")
            self._layout.addWidget(empty)
            self._layout.addWidget(hint)
            self._layout.addStretch(1)
            return

        for group in groups:
            shown = len(group["items"])
            more = f"（显示前 {shown}）" if group["count"] > shown else ""
            header = QLabel(f"{group['title']} · {group['count']} 个结果{more}")
            header.setStyleSheet(
                f"color: {TEXT_SECONDARY}; font-size: 12px; font-weight: bold;"
                f" background: transparent; padding: 4px 2px 2px 2px;"
            )
            self._layout.addWidget(header)

            for entry in group["items"]:
                row = _ResultRow(group["module"], entry)
                row.activated.connect(
                    lambda item, module=group["module"]: self.resultActivated.emit(module, item)
                )
                self._layout.addWidget(row)

        self._layout.addStretch(1)
