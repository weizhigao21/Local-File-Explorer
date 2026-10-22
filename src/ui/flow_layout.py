from PyQt6.QtCore import QPoint, QRect, QSize, Qt
from PyQt6.QtWidgets import QLayout


def _item_box(item) -> QSize:
    """控件在布局里真正占用的尺寸（不是裸 sizeHint）。

    只信 `sizeHint()` 会漏掉 `setFixedSize()`：PlaylistCard 固定 180x240，
    但它内容布局的 sizeHint 只有 196 高（名字两行时 202）。布局按 196 累加
    行距，控件却被 min/max 顶回 240 —— **相邻两行重叠 20~28px**，看起来像
    「两个作品压在一起」（用户截图反馈 2026-10-22）。

    ⇒ 先用 minimumSize 兜底（setFixedSize 会把 min 也设成固定值），再用
    maximumSize 夹住，保证排布尺寸 == 控件最终尺寸。
    """
    widget = item.widget()
    if widget is None:
        return item.sizeHint()
    box = widget.sizeHint().expandedTo(widget.minimumSize())
    maximum = widget.maximumSize()
    if maximum.isValid():
        box = box.boundedTo(maximum)
    return box


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
            # 跳过被显隐过滤隐藏的卡片（isHidden 只看显式隐藏标志，不受父级可见性影响）
            w = item.widget()
            if w is not None and w.isHidden():
                continue
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
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue  # 搜索过滤隐藏的卡片不占位
            # 必须按真实占位尺寸排（见 _item_box 注释）：裸 sizeHint 会让
            # setFixedSize 的卡片行距算小、两行叠在一起
            hint = _item_box(item)
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
