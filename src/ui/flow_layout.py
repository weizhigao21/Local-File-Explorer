from PyQt6.QtCore import Qt, QPoint, QRect, QSize
from PyQt6.QtWidgets import QLayout, QSizePolicy, QStyle, QStyleOption


class FlowLayout(QLayout):
    """自动换行布局：根据容器宽度自动计算每行可容纳的控件数"""

    def __init__(self, parent=None, margin=0, h_spacing=12, v_spacing=12):
        super().__init__(parent)
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)
        self._h_space = h_spacing
        self._v_space = v_spacing
        self._items = []

    def __del__(self):
        while self._items:
            self._items.pop(0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        # 优先使用 parent 当前宽度计算实际多行高度，避免只返回单行高度
        # 导致 QScrollArea 的滚动范围不足
        parent = self.parent()
        w = 0
        if parent is not None:
            w = parent.width()
        if w <= 0 and parent is not None:
            # parent 尚未显示时，尝试向上查找可见窗口的宽度
            ancestor = parent.parent()
            while ancestor is not None:
                if ancestor.width() > 0:
                    w = ancestor.width()
                    break
                ancestor = ancestor.parent()
        if w > 0:
            min_w = self.minimumSize().width()
            # 取较大宽度确保 items 能容纳
            w = max(w, min_w)
            h = self.heightForWidth(w)
            return QSize(min_w, h)
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect, test_only):
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0

        for item in self._items:
            hint = item.widget().sizeHint() if item.widget() else item.sizeHint()
            space_x = self._h_space
            space_y = self._v_space
            next_x = x + hint.width() + space_x
            if next_x - space_x > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + space_y
                next_x = x + hint.width() + space_x
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + margins.bottom()
