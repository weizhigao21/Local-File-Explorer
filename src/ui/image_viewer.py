"""全屏图片查看器：翻页 / 缩放 / 平移

三件事值得说明：

1. **解码尺寸不是视口尺寸**。旧实现按视口大小解码，画面永远是「刚好塞进窗口」的
   一张缩图，想放大看细节根本没那个像素。现在按 `min(原图, 视口 × VIEWER_OVERSCAN)`
   解码，放大两倍以内都是真图元。

2. **缩放基数是「适应窗口」= 100%**，而不是原图 1:1。解码产物上限 2560px，
   对 6000px 宽的扫描件说「100% 是 1:1」是假的；不如给一个用户能验证的基准：
   100% 正好铺满窗口，200% 就是两倍大。

3. **滚轮仍然翻页**（沿用既有习惯），缩放走 `Ctrl+滚轮` / `+` `-` / 双击。
   放大后左键拖拽平移。
"""
import time

from PyQt6.QtCore import QEvent, QRectF, QSize, Qt, QTimer
from PyQt6.QtGui import (
    QImage,
    QImageReader,
    QKeySequence,
    QPixmap,
    QShortcut,
    QTransform,
    QWheelEvent,
)
from PyQt6.QtWidgets import (
    QDialog,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ui.image_loader import get_cached, loader

# 看图解码上限：2K 屏放大 2 倍也够清晰，再大只是白吃内存
# （单张 2560×1440 RGBX ≈ 15MB，内存缓存 256MB 能放十几张）
VIEWER_MAX_DIM = 2560

# 解码尺寸相对视口的倍数：放大到 2 倍以内仍然是真像素
VIEWER_OVERSCAN = 2

# 缩放范围（100% = 适应窗口）
ZOOM_MIN = 1.0
ZOOM_MAX = 8.0
ZOOM_DOUBLE_CLICK = 2.0

# 滚轮翻页的最短间隔：快速滚动会连发十几个 wheel 事件，
# 不节流会一路翻过头（用户滚一格只想翻一张）
WHEEL_FLIP_INTERVAL = 0.12


def clamp_decode_size(orig: QSize, want: QSize, max_dim: int = VIEWER_MAX_DIM) -> QSize:
    """实际解码尺寸：不超过 want，也不超过 max_dim；读不到原图尺寸时直接用 want

    纯函数，方便单测——这里的边界（超大图必须夹住、小图不能放大）出错的代价
    是 OOM 或者糊图，靠手工点很难保证每次都覆盖到。
    """
    if orig.isValid() and orig.width() > 0 and orig.height() > 0:
        # 原图本身就比目标小：按原尺寸解码，不放大
        # （QSize.scaled 是「缩放到不超过给定矩形」，小图也会被拉大，不能直接用）
        if orig.width() <= want.width() and orig.height() <= want.height():
            target = orig
        else:
            target = orig.scaled(want, Qt.AspectRatioMode.KeepAspectRatio)
        # 仍要夹上限：want 本身可能大于 max_dim（如 4K 视口的 2 倍）
        if max(target.width(), target.height()) > max_dim:
            target = target.scaled(
                QSize(max_dim, max_dim), Qt.AspectRatioMode.KeepAspectRatio
            )
        return target
    return want


def fit_scale(viewport_w: int, viewport_h: int, pix_w: int, pix_h: int, zoom: float) -> float:
    """视图缩放倍率：zoom=1 时正好适应窗口，且小图不放大（最多 1:1）

    小图不放大是刻意的：一张 200px 的图标被拉成满屏只会得到一片马赛克，
    远不如按原尺寸居中显示。
    """
    if viewport_w <= 0 or viewport_h <= 0 or pix_w <= 0 or pix_h <= 0:
        return 1.0
    fit = min(viewport_w / pix_w, viewport_h / pix_h)
    return min(fit, 1.0) * zoom


class ImageViewer(QDialog):
    def __init__(self, image_paths, start_index=0, parent=None):
        super().__init__(parent)
        self.image_paths = image_paths
        self.current_index = start_index
        self._token = -1  # 当前请求的 token
        self._pixmap: QPixmap | None = None
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._zoom = ZOOM_MIN  # 1.0 = 适应窗口
        self._last_flip = 0.0
        self.setWindowTitle("图片查看")
        self.setStyleSheet("background-color: #F4EFE7; color: #3D3830;")

        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene)
        self.view.setStyleSheet("border: none; background-color: #F4EFE7;")
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 缩放锚点：以鼠标位置为中心放大，而不是以窗口中心
        self.view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.view.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        # 滚轮与双击交给本窗口统一处理（QGraphicsView 默认会自己滚动/缩放）
        self.view.viewport().installEventFilter(self)

        self.counter = QLabel(self)
        self.counter.setStyleSheet("color: #3D3830; font-size: 14px; padding: 6px;")

        self.zoom_label = QLabel("100%", self)
        self.zoom_label.setToolTip("Ctrl+滚轮或 + / - 缩放，双击在 100% 与 200% 间切换")
        self._update_zoom_label()

        prev_btn = QPushButton("上一张 (←)")
        next_btn = QPushButton("下一张 (→)")
        BTN_STYLE = "background-color: #EDE2CE; color: #3D3830; padding: 6px 14px; border: 1px solid #E0D5C2; border-radius: 4px;"
        BTN_HOVER = "QPushButton:hover { background-color: #F3EFE8; border: 1px solid #B85C3E; color: #3D3830; }"
        BTN_PRESSED = "QPushButton:pressed { background-color: #D4886A; border: 1px solid #D4886A; color: #fff; }"
        for btn in [prev_btn, next_btn]:
            btn.setStyleSheet(BTN_STYLE + BTN_HOVER + BTN_PRESSED)
        prev_btn.clicked.connect(self.show_previous)
        next_btn.clicked.connect(self.show_next)

        controls = QHBoxLayout()
        controls.addStretch()
        controls.addWidget(prev_btn)
        controls.addWidget(self.counter)
        controls.addWidget(self.zoom_label)
        controls.addWidget(next_btn)
        controls.addStretch()

        layout = QVBoxLayout(self)
        layout.addWidget(self.view)
        layout.addLayout(controls)

        # resize 防抖：拖动窗口时只在松手后 150ms 重新解码
        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(150)
        self._resize_timer.timeout.connect(self._on_resize_stable)

        QShortcut(QKeySequence("Left"), self).activated.connect(self.show_previous)
        QShortcut(QKeySequence("Right"), self).activated.connect(self.show_next)
        QShortcut(QKeySequence("Esc"), self).activated.connect(self.close)
        # 缩放快捷键：+ / = 放大，- / _ 缩小，0 复位
        for key in ("+", "="):
            QShortcut(QKeySequence(key), self).activated.connect(lambda: self._zoom_by(1.25))
        for key in ("-", "_"):
            QShortcut(QKeySequence(key), self).activated.connect(lambda: self._zoom_by(1 / 1.25))
        QShortcut(QKeySequence("0"), self).activated.connect(self._reset_zoom)

        # 先显示窗口并最大化，再加载图片（让 viewport 拿到正确尺寸）
        self.setWindowState(Qt.WindowState.WindowMaximized)
        # 延迟一帧加载，确保 viewport 尺寸已经更新
        QTimer.singleShot(0, self.load_image)

    # ---------------- 尺寸与解码 ----------------

    def _decode_target(self) -> QSize:
        """解码目标尺寸：min(原图, 视口 × 2)，再夹到 VIEWER_MAX_DIM

        比视口大一倍是为了「放大看得清」；仍要夹上限，否则一张 8000×6000 的
        扫描件会解出 200MB 的 QImage，翻两张就把内存打满。
        """
        rect = self.view.viewport().rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return QSize()
        want = QSize(rect.width() * VIEWER_OVERSCAN, rect.height() * VIEWER_OVERSCAN)

        try:
            reader = QImageReader(self.image_paths[self.current_index])
            reader.setAutoTransform(True)
            orig = reader.size()
        except Exception:  # noqa: BLE001 - 读不到头信息就按视口算，解码时再兜底
            orig = QSize()

        if orig.isValid() and orig.width() > 0 and orig.height() > 0:
            return clamp_decode_size(orig, want)
        return want

    # ---------------- 加载 ----------------

    def load_image(self):
        if not self.image_paths:
            return
        path = self.image_paths[self.current_index]
        target = self._decode_target()
        if target.width() <= 0 or target.height() <= 0:
            return

        # 更新计数器
        self.counter.setText(f"{self.current_index + 1} / {len(self.image_paths)}")

        # 缓存命中直接显示
        cached = get_cached(path, target)
        if cached is not None and not cached.isNull():
            self._show_image(cached)
            self._preload()
            return

        # 异步解码：先显示占位文本
        self.scene.clear()
        self._pixmap = None
        self._pixmap_item = None
        placeholder = self.scene.addText("加载中...")
        placeholder.setDefaultTextColor(Qt.GlobalColor.gray)
        self.view.setSceneRect(QRectF(0, 0, 100, 40))

        self._token = loader().request(path, target, self._on_loaded)

    def _on_loaded(self, path, target, image, token):
        """异步回调：token 不匹配则丢弃"""
        if token != self._token:
            return False
        if path != self.image_paths[self.current_index]:
            return False
        if image.isNull():
            return False
        self._show_image(image)
        self._preload()
        return True

    def _show_image(self, image: QImage):
        # QImage → QPixmap 只能在 GUI 线程做（本方法即在主线程回调中执行）
        pixmap = QPixmap.fromImage(image)
        # 防御：窗口关闭后回调可能还在执行
        try:
            self.scene.clear()
            self._pixmap = pixmap
            self._pixmap_item = self.scene.addPixmap(pixmap)
            self._pixmap_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            self.view.setSceneRect(QRectF(pixmap.rect()))
            self._apply_view()
        except RuntimeError:
            pass

    def _preload(self):
        """预解码相邻图片（异步）"""
        indices = []
        if self.current_index > 0:
            indices.append(self.current_index - 1)
        if self.current_index < len(self.image_paths) - 1:
            indices.append(self.current_index + 1)

        target = self._decode_target()
        if target.width() <= 0 or target.height() <= 0:
            return

        for idx in indices:
            path = self.image_paths[idx]
            if get_cached(path, target) is None:
                # 触发异步预解码（结果会写入全局缓存，下次切到此图时直接命中）
                loader().request(path, target, lambda *_: False)

    # ---------------- 缩放与平移 ----------------

    def _apply_view(self):
        """按 _zoom 设置视图变换：1.0 正好适应窗口，>1 放大并可拖拽平移"""
        # 先刷标签：图还没解码完（_pixmap 为空）时缩放也要有反馈，
        # 否则用户按了 + 一点动静都没有，会以为快捷键坏了
        self._update_zoom_label()
        if self._pixmap is None or self._pixmap.isNull():
            return
        viewport = self.view.viewport().rect()
        if viewport.width() <= 0 or viewport.height() <= 0:
            return

        pw = max(self._pixmap.width(), 1)
        ph = max(self._pixmap.height(), 1)
        scale = fit_scale(viewport.width(), viewport.height(), pw, ph, self._zoom)

        self.view.setTransform(QTransform.fromScale(scale, scale))
        # 放大后可拖动；100% 时禁用，否则单击会被当成拖拽起点
        self.view.setDragMode(
            QGraphicsView.DragMode.ScrollHandDrag
            if self._zoom > ZOOM_MIN + 1e-3
            else QGraphicsView.DragMode.NoDrag
        )
        self._update_zoom_label()

    def _update_zoom_label(self):
        self.zoom_label.setText(f"{int(round(self._zoom * 100))}%")
        color = "#B85C3E" if self._zoom > ZOOM_MIN + 1e-3 else "#8A7F6E"
        self.zoom_label.setStyleSheet(
            f"color: {color}; font-size: 12px; padding: 6px 2px;"
        )

    def _zoom_by(self, factor):
        new_zoom = max(ZOOM_MIN, min(ZOOM_MAX, self._zoom * factor))
        if abs(new_zoom - self._zoom) < 1e-3:
            return
        self._zoom = new_zoom
        self._apply_view()

    def _reset_zoom(self):
        self._zoom = ZOOM_MIN
        self._apply_view()

    def _toggle_zoom(self):
        self._zoom = ZOOM_MIN if self._zoom > ZOOM_MIN + 1e-3 else ZOOM_DOUBLE_CLICK
        self._apply_view()

    # ---------------- 事件 ----------------

    def eventFilter(self, obj, event):
        """接管视口上的滚轮与双击

        不接管的话：滚轮会被 QGraphicsView 当成滚动（图比视口大时被静默吃掉，
        翻页就失灵），双击也不会有任何反应。
        """
        if obj is self.view.viewport():
            if event.type() == QEvent.Type.Wheel:
                self._on_wheel(event)
                return True
            if (event.type() == QEvent.Type.MouseButtonDblClick
                    and event.button() == Qt.MouseButton.LeftButton):
                self._toggle_zoom()
                return True
        return super().eventFilter(obj, event)

    def _on_wheel(self, event: QWheelEvent):
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        delta = event.angleDelta().y()
        if delta == 0:
            return
        if ctrl:
            self._zoom_by(1.15 if delta > 0 else 1 / 1.15)
            return

        # 翻页节流：一次物理滚动会连发多个事件，不挡会一口气翻好几张
        now = time.monotonic()
        if now - self._last_flip < WHEEL_FLIP_INTERVAL:
            return
        self._last_flip = now
        if delta > 0:
            self.show_previous()
        else:
            self.show_next()

    def wheelEvent(self, event: QWheelEvent):
        # 视口没拿到事件时的兜底（例如焦点在按钮上）
        self._on_wheel(event)

    def show_previous(self):
        if self.current_index > 0:
            self.current_index -= 1
            self._zoom = ZOOM_MIN
            self.load_image()

    def show_next(self):
        if self.current_index < len(self.image_paths) - 1:
            self.current_index += 1
            self._zoom = ZOOM_MIN
            self.load_image()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 防抖：拖动过程中不重新解码
        self._resize_timer.start()

    def _on_resize_stable(self):
        """窗口尺寸稳定后重新加载当前图（解码上限随视口变化）"""
        self.load_image()
