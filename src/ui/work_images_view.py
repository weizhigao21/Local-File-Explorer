"""
图片墙：基于 QListWidget IconMode 实现。
相比 QFrame + QLabel 的方式：
- 不需要为每个缩略图创建独立 widget，性能更优（数百张图场景）
- 自定义 ItemDelegate 直接绘制，避免 widget 树开销
- 视口检测通过 visualItemRect 高效获取
"""
import os

from PyQt6.QtCore import QModelIndex, QRect, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

import file_ops
from resource_manager import database as db
from ui.image_loader import get_cached, loader

# 右键菜单样式：和主界面米色系一致（QMenu 默认是浅灰，和卡片墙放一起很突兀）
MENU_QSS = (
    "QMenu { background-color: #FBF7F0; border: 1px solid #E8DECB; padding: 4px; }"
    "QMenu::item { padding: 5px 22px; color: #3D3830; font-size: 12px; border-radius: 3px; }"
    "QMenu::item:selected { background-color: #EBD9CE; }"
)

# 卡片尺寸（含外边距）
ITEM_W = 165          # 卡片宽度
ITEM_IMG_H = 200      # 图片区高度
ITEM_IDX_H = 22       # 索引文字区高度
ITEM_H = ITEM_IMG_H + ITEM_IDX_H  # 卡片总高 = 222
# 解码目标尺寸（item 内部图片区域）
DECODE_W = ITEM_W - 12
DECODE_H = ITEM_IMG_H - 6

# 强调色（与 main_window 保持一致）
ACCENT = "#B85C3E"
ACCENT_TINT = QColor(184, 92, 62, 40)        # 选中态背景
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
            painter.fillRect(rect, QColor("#FFFCF7"))
            painter.setPen(QColor("#E0D5C2"))
        else:
            painter.fillRect(rect, QColor("#FFFFFF"))
            painter.setPen(QColor("#E8DECB"))
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
            painter.setPen(QColor("#A89880"))
            painter.drawText(
                QRect(rect.x(), rect.y() + ITEM_IMG_H // 2 - 10, rect.width(), 20),
                Qt.AlignmentFlag.AlignCenter, "..."
            )

        # 索引
        painter.setPen(QColor("#A89880"))
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
            "QListWidget { background-color: #F4EFE7; border: none; }"
            "QListWidget::item { background: transparent; border: none; padding: 0; }"
        )
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 垂直滚动条：内容不足一屏时自动隐藏，滚动按行步进
        self.list_widget.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # singleStep = 一个网格高度（gridSize 含 spacing），让滚动按行而非按视口比例
        self.list_widget.verticalScrollBar().setSingleStep(ITEM_H + 12)
        # 单击即打开
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        # 右键：定位文件 / 复制路径 / 移入回收站
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_item_menu)
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
        bottom_widget.setStyleSheet("background-color: #FBF7F0; border-top: 1px solid #E8DECB;")
        bottom = QHBoxLayout(bottom_widget)
        bottom.setContentsMargins(16, 6, 16, 6)
        self.info_label = QLabel(self._info_text())
        self.info_label.setStyleSheet("color: #3D3830; font-size: 13px;")
        bottom.addWidget(self.info_label)
        bottom.addStretch()
        hint = QLabel("点击图片查看 · 右键可定位文件 · 返回请按 Esc 或顶栏 ← 返回")
        hint.setStyleSheet("color: #A89880; font-size: 11px;")
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

    # ---------- 右键文件操作 ----------

    def _info_text(self):
        return f"{self.work['author']} / {self.work['name']} · 共 {len(self.images)} 张"

    def _show_item_menu(self, pos):
        item = self.list_widget.itemAt(pos)
        if item is None:
            return
        path = item.data(ROLE_PATH)
        menu = QMenu(self)
        menu.setStyleSheet(MENU_QSS)
        act_view = menu.addAction("查看大图")
        act_reveal = menu.addAction("在资源管理器中显示")
        act_default = menu.addAction("用默认程序打开")
        menu.addSeparator()
        act_copy_path = menu.addAction("复制文件路径")
        act_copy_name = menu.addAction("复制文件名")
        menu.addSeparator()
        act_delete = menu.addAction("移入回收站…")

        chosen = menu.exec(self.list_widget.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_view:
            self._on_image_clicked(item.data(ROLE_INDEX))
        elif chosen is act_reveal:
            if not file_ops.reveal(path):
                QMessageBox.information(self, "打开失败", f"找不到这个文件：\n{path}")
        elif chosen is act_default:
            if not file_ops.open_with_default(path):
                QMessageBox.information(self, "打开失败", f"找不到这个文件：\n{path}")
        elif chosen is act_copy_path:
            QApplication.clipboard().setText(path)
        elif chosen is act_copy_name:
            QApplication.clipboard().setText(os.path.basename(path))
        elif chosen is act_delete:
            self._delete_image(path)

    def _confirm_delete(self, name):
        """删除前确认：按钮写「移入回收站」而不是「确定」，
        让用户清楚这不是永久删除，但也别指望人人都读提示"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("移入回收站")
        box.setText(f"把这张图片移入回收站？\n\n{name}")
        box.setInformativeText("文件会进入系统回收站，需要时可以还原。")
        confirm = box.addButton("移入回收站", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is confirm

    def _delete_image(self, path):
        """移入回收站（不提供永久删除：原图删错了找不回来）

        删完要同步三处，否则界面和磁盘会不一致：
        1. 磁盘：SHFileOperationW 走回收站；
        2. 数据库：images 表删掉这条，下次扫描不会以为它丢了又重新加；
        3. 界面：从 self.images 原地移除并重建索引（ROLE_INDEX 是下标，
           不重建会让后面每张图的「第几张」全部错位）。
        """
        name = os.path.basename(path)
        if not self._confirm_delete(name):
            return

        ok_count, _failed = file_ops.move_to_recycle_bin([path])
        if not ok_count:
            QMessageBox.warning(
                self, "删除失败",
                f"没能移入回收站（文件可能正被别的程序占用）：\n{name}",
            )
            return

        db.delete_images_by_paths([path])
        # 原地删除：open_image_at 的闭包持有的是同一个 list 对象，
        # 换成新列表会让看图器还按旧下标翻到已删除的图
        for i, img in enumerate(self.images):
            if img.get("path") == path:
                del self.images[i]
                break
        self._rebuild_items()

    def _rebuild_items(self):
        """重建列表项：删除后下标全部前移，必须重新编号"""
        self.list_widget.clear()
        for i, img in enumerate(self.images):
            self.list_widget.addItem(_make_item(img["path"], i))
        self.info_label.setText(self._info_text())
        QTimer.singleShot(0, self._load_visible)

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

        self._release_offscreen(viewport_valid, vrect)

    def _release_offscreen(self, viewport_valid, vrect):
        """释放滚出视口 item 的 pixmap，限制图片墙内存驻留

        数百张图的 QPixmap 全部驻留可达数百 MB；滚回时
        _ensure_item_loaded 会从 image_loader 的 LRU 缓存快速恢复（仅 QImage→QPixmap 转换）。
        上下各留一屏缓冲，避免小幅滚动引起反复释放/重建。
        """
        if not viewport_valid:
            return
        margin = vrect.height()
        keep = vrect.adjusted(0, -margin, 0, margin)
        list_widget = self.list_widget
        for i in range(list_widget.count()):
            item = list_widget.item(i)
            if item is None or not getattr(item, "_loaded", False):
                continue
            if not keep.intersects(list_widget.visualItemRect(item)):
                item.setData(ROLE_PIXMAP, None)
                item._loaded = False

    def _ensure_item_loaded(self, item: QListWidgetItem):
        """触发单个 item 的异步加载"""
        if getattr(item, "_loaded", False):
            return
        target = QSize(DECODE_W, DECODE_H)
        # 缓存命中直接设置
        cached = get_cached(item._image_path, target)
        if cached is not None and not cached.isNull():
            self._apply_image(item, cached)
            item._loaded = True
            return
        item._token = loader().request(
            item._image_path, target,
            lambda path, t, img, tk, it=item: self._on_loaded(it, path, t, img, tk),
        )

    def _on_loaded(self, item, path, target, image, token):
        """异步回调：token 不匹配或 item 已销毁则丢弃"""
        if token != getattr(item, "_token", -1):
            return False
        if path != item._image_path:
            return False
        if image.isNull():
            # 解码失败：不置 _loaded，下次滚回视口时可重试
            return False
        self._apply_image(item, image)
        item._loaded = True
        return True

    def _apply_image(self, item: QListWidgetItem, image: QImage):
        """把解码结果写入 item 数据，触发重绘（QImage → QPixmap 只能在 GUI 线程做）"""
        if image.isNull():
            return
        pixmap = QPixmap.fromImage(image)
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
