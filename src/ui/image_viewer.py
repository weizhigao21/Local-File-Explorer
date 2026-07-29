from PyQt6.QtWidgets import (
    QDialog,
    QGraphicsView,
    QGraphicsScene,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
)
from PyQt6.QtGui import QPixmap, QKeySequence, QWheelEvent, QShortcut
from PyQt6.QtCore import Qt, QRectF, QTimer

from ui.image_loader import loader, get_cached


class ImageViewer(QDialog):
    def __init__(self, image_paths, start_index=0, parent=None):
        super().__init__(parent)
        self.image_paths = image_paths
        self.current_index = start_index
        self._token = -1  # 当前请求的 token
        self.setWindowTitle("图片查看")
        self.setStyleSheet("background-color: #F5F0E8; color: #555555;")

        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene)
        self.view.setStyleSheet("border: none; background-color: #F5F0E8;")
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.counter = QLabel(self)
        self.counter.setStyleSheet("color: #555555; font-size: 14px; padding: 6px;")

        prev_btn = QPushButton("上一张 (←)")
        next_btn = QPushButton("下一张 (→)")
        BTN_STYLE = "background-color: #EDE6DA; color: #555555; padding: 6px 14px; border: 1px solid #D5CDC0; border-radius: 4px;"
        BTN_HOVER = "QPushButton:hover { background-color: #F3EFE8; border: 1px solid #42B4C2; color: #444; }"
        BTN_PRESSED = "QPushButton:pressed { background-color: #48B8BC; border: 1px solid #48B8BC; color: #fff; }"
        for btn in [prev_btn, next_btn]:
            btn.setStyleSheet(BTN_STYLE + BTN_HOVER + BTN_PRESSED)
        prev_btn.clicked.connect(self.show_previous)
        next_btn.clicked.connect(self.show_next)

        controls = QHBoxLayout()
        controls.addStretch()
        controls.addWidget(prev_btn)
        controls.addWidget(self.counter)
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

        # 先显示窗口并最大化，再加载图片（让 viewport 拿到正确尺寸）
        self.setWindowState(Qt.WindowState.WindowMaximized)
        # 延迟一帧加载，确保 viewport 尺寸已经更新
        QTimer.singleShot(0, self.load_image)

    def load_image(self):
        if not self.image_paths:
            return
        path = self.image_paths[self.current_index]
        rect = self.view.viewport().rect()
        target = rect.size()

        if target.width() <= 0 or target.height() <= 0:
            return

        # 更新计数器
        self.counter.setText(f"{self.current_index + 1} / {len(self.image_paths)}")

        # 缓存命中直接显示
        cached = get_cached(path, target)
        if cached is not None and not cached.isNull():
            self._show_pixmap(cached)
            self._preload()
            return

        # 异步解码：先显示占位文本
        self.scene.clear()
        placeholder = self.scene.addText("加载中...")
        placeholder.setDefaultTextColor(Qt.GlobalColor.gray)
        self.view.setSceneRect(QRectF(0, 0, 100, 40))

        self._token = loader().request(path, target, self._on_loaded)

    def _on_loaded(self, path, target, pixmap, token):
        """异步回调：token 不匹配则丢弃"""
        if token != self._token:
            return False
        if path != self.image_paths[self.current_index]:
            return False
        if pixmap.isNull():
            return False
        self._show_pixmap(pixmap)
        self._preload()
        return True

    def _show_pixmap(self, pixmap: QPixmap):
        # 防御：窗口关闭后回调可能还在执行
        try:
            self.scene.clear()
            self.scene.addPixmap(pixmap)
            self.view.setSceneRect(QRectF(pixmap.rect()))
        except RuntimeError:
            pass

    def _preload(self):
        """预解码相邻图片（异步）"""
        indices = []
        if self.current_index > 0:
            indices.append(self.current_index - 1)
        if self.current_index < len(self.image_paths) - 1:
            indices.append(self.current_index + 1)

        rect = self.view.viewport().rect()
        target = rect.size()
        if target.width() <= 0 or target.height() <= 0:
            return

        for idx in indices:
            path = self.image_paths[idx]
            if get_cached(path, target) is None:
                # 触发异步预解码（结果会写入全局缓存，下次切到此图时直接命中）
                loader().request(path, target, lambda *_: False)

    def show_previous(self):
        if self.current_index > 0:
            self.current_index -= 1
            self.load_image()

    def show_next(self):
        if self.current_index < len(self.image_paths) - 1:
            self.current_index += 1
            self.load_image()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 防抖：拖动过程中不重新解码
        self._resize_timer.start()

    def _on_resize_stable(self):
        """窗口尺寸稳定后重新加载当前图"""
        self.load_image()

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        if delta > 0:
            self.show_previous()
        else:
            self.show_next()
