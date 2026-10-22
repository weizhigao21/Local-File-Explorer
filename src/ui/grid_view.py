from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import QFrame, QLabel, QVBoxLayout

from ui.image_loader import get_cached, loader
from ui.text_utils import truncate_title

# 强调色（与 main_window 保持一致）
ACCENT = "#B85C3E"


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
                border: 1px solid #E8DECB;
                border-top: 2px solid transparent;
                border-radius: 6px;
            }}
            WorkCard:hover {{
                background-color: #FFFCF7;
                border: 1px solid #E0D5C2;
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
        self.img_label.setStyleSheet("background-color: #F4EFE7; border-radius: 4px;")
        self.img_label.setText("...")
        layout.addWidget(self.img_label)

        # 长标题不截断会换 6 行（75px），把「作者 · N 张」挤出卡片、还压住封面。
        # 与音频模块同一套规则：20 字 + 省略号，全名留在 tooltip。
        title = QLabel(truncate_title(work["name"]))
        title.setObjectName("workTitle")
        title.setToolTip(work["name"])
        title.setWordWrap(True)
        title.setStyleSheet("color: #3D3830; font-size: 13px;")
        layout.addWidget(title)

        meta = QLabel(f"{work['author']} · {work['image_count']} 张")
        meta.setStyleSheet("color: #A89880; font-size: 11px;")
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
            self._apply_image(cached, target)
            self._thumb_loaded = True
            return

        self._token = loader().request(thumb_path, target, self._on_loaded)

    def _on_loaded(self, path, target, image, token):
        if token != self._token:
            return False
        if image.isNull():
            # 解码失败：不置 _thumb_loaded，下次滚回视口时可重试
            return False
        self._apply_image(image, target)
        self._thumb_loaded = True
        return True

    def _apply_image(self, image: QImage, target: QSize):
        if image.isNull():
            return
        # QImage → QPixmap 只能在 GUI 线程做（本方法即在主线程回调中执行）
        pixmap = QPixmap.fromImage(image)
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
