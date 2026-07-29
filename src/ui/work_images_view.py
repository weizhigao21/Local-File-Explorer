"""
图片墙：基于 QListWidget IconMode 实现。
相比 QFrame + QLabel 的方式：
- 不需要为每个缩略图创建独立 widget，性能更优（数百张图场景）
- 自定义 ItemDelegate 直接绘制，避免 widget 树开销
- 视口检测通过 visualItemRect 高效获取
"""
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QListView,
    QStyledItemDelegate,
    QStyle,
)
from PyQt6.QtGui import QPixmap, QPainter, QColor, QIcon
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QRect, QModelIndex, QTimer

from ui.image_loader import loader, get_cached


# 卡片尺寸（含外边距）
ITEM_W = 165          # 卡片宽度
ITEM_IMG_H = 200      # 图片区高度
ITEM_IDX_H = 22       # 索引文字区高度
ITEM_H = ITEM_IMG_H + ITEM_IDX_H  # 卡片总高 = 222
# 解码目标尺寸（item 内部图片区域）
DECODE_W = ITEM_W - 12
DECODE_H = ITEM_IMG_H - 6

# 强调色（与 main_window 保持一致）
ACCENT = "#42B4C2"
ACCENT_TINT = QColor(66, 180, 194, 40)        # 选中态背景
ACCENT_BORDER = QColor(ACCENT)


# 自定义数据角色
ROLE_PATH = Qt.ItemDataRole.UserRole         # 图片路径
ROLE_INDEX = Qt.ItemDataRole.UserRole + 1    # 索引
ROLE_PIXMAP = Qt.ItemDataRole.UserRole + 2   # 已加载的 pixmap


class _ImageDelegate(QStyledItemDelegate):
    """自定义绘制：背景 + 缩略图 + 底部索引"""

    def paint(self, painter: QPainter, option, index: QModelIndex):
        painter.save()
        rect = option.rect

        # 背景
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, ACCENT_TINT)
            painter.setPen(ACCENT_BORDER)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(rect, QColor("#FFFDF8"))
            painter.setPen(QColor("#D5CDC0"))
        else:
            painter.fillRect(rect, QColor("#FFFFFF"))
            painter.setPen(QColor("#E8E0D5"))
        painter.drawRect(rect.adjusted(0, 0, -1, -1))

        # 绘制 pixmap
        pix_variant = index.data(ROLE_PIXMAP)
        if pix_variant is not None and not pix_variant.isNull():
            target = QRect(
                rect.x() + 6, rect.y() + 6,
                rect.width() - 12, ITEM_IMG_H - 6,
            )
            # 用 FastTransformation 提速（缩略图不需要高质量）
            scaled = pix_variant.scaled(
                target.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            x = target.x() + (target.width() - scaled.width()) // 2
            y = target.y() + (target.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            # 占位
            painter.setPen(QColor("#999999"))
            painter.drawText(
                QRect(rect.x(), rect.y() + ITEM_IMG_H // 2 - 10, rect.width(), 20),
                Qt.AlignmentFlag.AlignCenter, "..."
            )

        # 索引
        painter.setPen(QColor("#999999"))
        font = painter.font()
        font.setPointSize(9)
        painter.setFont(font)
        idx = index.data(ROLE_INDEX)
        if idx is not None:
            text_rect = QRect(
                rect.x(), rect.bottom() - ITEM_IDX_H,
                rect.width(), ITEM_IDX_H - 2,
            )
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, f"#{idx + 1}")

        painter.restore()

    def sizeHint(self, option, index: QModelIndex) -> QSize:
        return QSize(ITEM_W, ITEM_H)


def _make_item(image_path: str, index: int) -> QListWidgetItem:
    item = QListWidgetItem()
    item.setData(ROLE_PATH, image_path)
    item.setData(ROLE_INDEX, index)
    # 加载状态附加在 item 上（通过 data/setData 也可，但用 Python 属性更高效）
    item._loaded = False
    item._token = -1
    item._image_path = image_path
    return item


class WorkImagesView(QWidget):
    """图片墙视图：QListWidget + IconMode + 自定义 delegate"""

    def __init__(self, work, images, on_image_clicked, on_back, parent=None):
        super().__init__(parent)
        self.work = work
        self.images = images
        self._on_image_clicked = on_image_clicked

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # QListWidget IconMode：自适应换行
        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListView.ViewMode.IconMode)
        self.list_widget.setResizeMode(QListView.ResizeMode.Adjust)  # 关键：窗口 resize 时自动重排
        self.list_widget.setMovement(QListView.Movement.Static)  # 禁止拖动
        self.list_widget.setUniformItemSizes(True)  # 启用 sizeHint 优化
        self.list_widget.setSpacing(12)
        self.list_widget.setGridSize(QSize(ITEM_W + 12, ITEM_H + 12))
        self.list_widget.setItemDelegate(_ImageDelegate(self.list_widget))
        self.list_widget.setStyleSheet(
            "QListWidget { background-color: #F5F0E8; border: none; }"
            "QListWidget::item { background: transparent; border: none; padding: 0; }"
        )
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 垂直滚动条：内容不足一屏时自动隐藏，滚动按行步进
        self.list_widget.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # singleStep = 一个网格高度（gridSize 含 spacing），让滚动按行而非按视口比例
        self.list_widget.verticalScrollBar().setSingleStep(ITEM_H + 12)
        # 单击即打开
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        # 滚动防抖
        self.list_widget.verticalScrollBar().valueChanged.connect(self._on_scroll)
        # resize 防抖
        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(150)
        self._resize_timer.timeout.connect(self._load_visible)

        # 填充 items
        for i, img in enumerate(images):
            self.list_widget.addItem(_make_item(img["path"], i))

        layout.addWidget(self.list_widget, 1)

        # 底部信息栏（独立于滚动区域）
        bottom_widget = QWidget()
        bottom_widget.setStyleSheet("background-color: #FDF9F2; border-top: 1px solid #E8E0D5;")
        bottom = QHBoxLayout(bottom_widget)
        bottom.setContentsMargins(16, 6, 16, 6)
        info = QLabel(f"{work['author']} / {work['name']} · 共 {len(images)} 张")
        info.setStyleSheet("color: #555555; font-size: 13px;")
        bottom.addWidget(info)
        bottom.addStretch()
        hint = QLabel("点击图片查看 · 返回请按 Esc 或顶栏 ← 返回")
        hint.setStyleSheet("color: #999999; font-size: 11px;")
        bottom.addWidget(hint)
        bottom_widget.setFixedHeight(40)
        layout.addWidget(bottom_widget)

        # 滚动触发懒加载
        self._scroll_timer = QTimer()
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(80)
        self._scroll_timer.timeout.connect(self._load_visible)

        # 首次加载
        QTimer.singleShot(0, self._load_visible)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 窗口大小变化，防抖后重新加载可见区域
        self._resize_timer.start()

    def _on_scroll(self, _value):
        self._scroll_timer.start()

    def _on_item_clicked(self, item):
        idx = item.data(ROLE_INDEX)
        self._on_image_clicked(idx)

    def _load_visible(self):
        """加载可见区域内的缩略图"""
        list_widget = self.list_widget
        viewport = list_widget.viewport()
        vrect = viewport.rect()
        # 兜底：首次显示 viewport 是 0×0，直接加载前 N 个
        viewport_valid = vrect.width() > 0 and vrect.height() > 0

        loaded = 0
        max_load = 32
        for i in range(list_widget.count()):
            if loaded >= max_load:
                break
            item = list_widget.item(i)
            if item is None or getattr(item, "_loaded", False):
                continue
            if viewport_valid:
                rect = list_widget.visualItemRect(item)
                if not vrect.intersects(rect):
                    continue
            self._ensure_item_loaded(item)
            loaded += 1

    def _ensure_item_loaded(self, item: QListWidgetItem):
        """触发单个 item 的异步加载"""
        if getattr(item, "_loaded", False):
            return
        target = QSize(DECODE_W, DECODE_H)
        # 缓存命中直接设置
        cached = get_cached(item._image_path, target)
        if cached is not None and not cached.isNull():
            self._apply_pixmap(item, cached)
            item._loaded = True
            return
        item._token = loader().request(
            item._image_path, target,
            lambda path, t, pix, tk, it=item: self._on_loaded(it, path, t, pix, tk),
        )

    def _on_loaded(self, item, path, target, pixmap, token):
        """异步回调：token 不匹配或 item 已销毁则丢弃"""
        if token != getattr(item, "_token", -1):
            return False
        if path != item._image_path:
            return False
        self._apply_pixmap(item, pixmap)
        item._loaded = True
        return True

    def _apply_pixmap(self, item: QListWidgetItem, pixmap: QPixmap):
        """把 pixmap 写入 item 数据，触发重绘"""
        if pixmap.isNull():
            return
        try:
            item.setData(ROLE_PIXMAP, pixmap)
            row = self.list_widget.row(item)
            idx = self.list_widget.model().index(row, 0)
            self.list_widget.update(idx)
        except RuntimeError:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        # 首次显示时触发懒加载
        QTimer.singleShot(0, self._load_visible)
