from PyQt6.QtWidgets import QFrame, QLabel, QVBoxLayout
from PyQt6.QtGui import QPixmap
from PyQt6.QtCore import Qt, QSize, pyqtSignal

from ui.image_loader import loader, get_cached


# 强调色（与 main_window 保持一致）
ACCENT = "#42B4C2"


class WorkCard(QFrame):
    clicked = pyqtSignal(dict)

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self.work = work
        self._thumb_loaded = False
        self._token = -1
        self.setFixedSize(220, 280)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            f"""
            WorkCard {{
                background-color: #FFFFFF;
                border: 1px solid #E8E0D5;
                border-top: 2px solid transparent;
                border-radius: 6px;
            }}
            WorkCard:hover {{
                background-color: #FFFDF8;
                border: 1px solid #D5CDC0;
                border-top: 2px solid {ACCENT};
            }}
        """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.img_label = QLabel()
        self.img_label.setFixedSize(200, 200)
        self.img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_label.setStyleSheet("background-color: #F5F0E8; border-radius: 4px;")
        self.img_label.setText("...")
        layout.addWidget(self.img_label)

        title = QLabel(work["name"])
        title.setWordWrap(True)
        title.setStyleSheet("color: #555555; font-size: 13px;")
        layout.addWidget(title)

        meta = QLabel(f"{work['author']} · {work['image_count']} 张")
        meta.setStyleSheet("color: #999999; font-size: 11px;")
        layout.addWidget(meta)

    def load_thumbnail(self):
        """由父容器视口检测主动触发，不再依赖 showEvent"""
        if self._thumb_loaded:
            return
        thumb_path = self.work.get("thumbnail") or ""
        if not thumb_path:
            self._thumb_loaded = True
            return

        target = self.img_label.size()
        if target.width() <= 0 or target.height() <= 0:
            # 兜底用固定尺寸
            target = QSize(200, 200)

        # 缓存命中则直接显示
        cached = get_cached(thumb_path, target)
        if cached is not None and not cached.isNull():
            self._apply_pixmap(cached, target)
            self._thumb_loaded = True
            return

        self._token = loader().request(thumb_path, target, self._on_loaded)

    def _on_loaded(self, path, target, pixmap, token):
        if token != self._token:
            return False
        self._apply_pixmap(pixmap, target)
        self._thumb_loaded = True
        return True

    def _apply_pixmap(self, pixmap: QPixmap, target: QSize):
        if pixmap.isNull():
            return
        # 防御：切换作者时旧卡片可能已被销毁
        try:
            # 检查底层 C++ 对象是否还存在
            _ = self.img_label.text()
        except RuntimeError:
            return
        scaled = pixmap.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        try:
            self.img_label.setPixmap(scaled)
            self.img_label.setText("")
        except RuntimeError:
            pass

    def mousePressEvent(self, event):
        self.clicked.emit(self.work)
