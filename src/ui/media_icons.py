"""
播放控制图标（QPainter 绘制）

为什么不直接用 ▶ / ❚❚ / ◀◀ 字符：
    - 同一字符在不同字体/缩放下的实际字形大小差异很大，42px 圆里只剩一小块；
    - 这些码位会被系统 emoji 字体接管，渲染成彩色方块，颜色不受 QSS 控制。
这里按目标图标尺寸直接绘制，视觉重量稳定、边缘抗锯齿、可随主题改颜色。
"""
import math

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import (
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PyQt6.QtWidgets import QPushButton, QWidget


def _draw_shape(painter, kind, size):
    """在 size×size 的逻辑画布上绘制图形（坐标用比例，避免硬编码像素）"""
    s = float(size)
    if kind == "play":
        path = QPainterPath()
        path.moveTo(s * 0.30, s * 0.15)
        path.lineTo(s * 0.82, s * 0.50)
        path.lineTo(s * 0.30, s * 0.85)
        path.closeSubpath()
        painter.drawPath(path)
    elif kind == "pause":
        width = s * 0.17
        height = s * 0.68
        radius = s * 0.05
        painter.drawRoundedRect(QRectF(s * 0.28, s * 0.16, width, height), radius, radius)
        painter.drawRoundedRect(QRectF(s * 0.55, s * 0.16, width, height), radius, radius)
    elif kind == "prev":
        radius = s * 0.04
        painter.drawRoundedRect(QRectF(s * 0.14, s * 0.16, s * 0.13, s * 0.68), radius, radius)
        path = QPainterPath()
        path.moveTo(s * 0.32, s * 0.50)
        path.lineTo(s * 0.84, s * 0.15)
        path.lineTo(s * 0.84, s * 0.85)
        path.closeSubpath()
        painter.drawPath(path)
    elif kind == "next":
        radius = s * 0.04
        painter.drawRoundedRect(QRectF(s * 0.73, s * 0.16, s * 0.13, s * 0.68), radius, radius)
        path = QPainterPath()
        path.moveTo(s * 0.68, s * 0.50)
        path.lineTo(s * 0.16, s * 0.15)
        path.lineTo(s * 0.16, s * 0.85)
        path.closeSubpath()
        painter.drawPath(path)


def media_icon(kind, color, size=20, dpr=2):
    """返回指定图形的 QIcon；size 为逻辑像素，dpr 用于高分屏清晰度"""
    physical = max(1, int(size * dpr))
    pixmap = QPixmap(physical, physical)
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    _draw_shape(painter, kind, physical / dpr)
    painter.end()

    return QIcon(pixmap)


class MediaButton(QPushButton):
    """只显示矢量图标的按钮；支持悬停变色与切换图形（播放/暂停）"""

    def __init__(self, kind, color, hover_color=None, icon_size=20, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._color = color
        self._hover_color = hover_color or color
        self._icon_size = icon_size
        self.setIconSize(QSize(icon_size, icon_size))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._apply(self._color)

    @property
    def kind(self):
        return self._kind

    def set_kind(self, kind):
        if kind != self._kind:
            self._kind = kind
            self._apply(self._color)

    def _apply(self, color):
        self.setIcon(media_icon(self._kind, color, self._icon_size))

    def enterEvent(self, event):
        if self._hover_color != self._color:
            self._apply(self._hover_color)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._hover_color != self._color:
            self._apply(self._color)
        super().leaveEvent(event)


class GlowPlayButton(MediaButton):
    """播放/暂停键：自绘赤陶渐变圆面 + 外圈光晕（陶色卡片风格）

    光晕会画到按钮矩形之外的视觉半径内，所以按钮本体要给足留白
    （圆面直径 ≈ side*2*face_ratio，光晕半径 = side*0.5）；
    QSS 只负责透明，不画背景。
    face_ratio = 圆面半径 / 按钮边长：0.33 时圆面约占边长 2/3，
    0.4375 时 48px 按钮出 42px 圆面（贴设计稿 t-play）。
    """

    def __init__(self, kind, icon_color, face_top, face_bottom, glow_color,
                 icon_size=20, face_ratio=0.33, parent=None):
        super().__init__(kind, icon_color, icon_color, icon_size, parent)
        self._face_top = QColor(face_top)
        self._face_bottom = QColor(face_bottom)
        self._glow = QColor(glow_color)
        self._face_ratio = face_ratio
        self.setStyleSheet("QPushButton { background: transparent; border: none; }")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        center = rect.center()
        side = min(rect.width(), rect.height())

        # ① 外圈光晕（透明度渐变；衰减区间必须在圆面之外，否则光晕被圆面盖完）
        glow_radius = side * 0.5
        face_radius = side * self._face_ratio
        glow = QRadialGradient(QPointF(center), glow_radius)
        halo = QColor(self._glow)
        halo.setAlpha(90)
        glow.setColorAt(0.0, halo)
        edge = QColor(self._glow)
        edge.setAlpha(70)
        # 中段止点放在圆面边缘内侧一点，保证圆面外就有可见柔光
        glow.setColorAt(min(0.95, face_radius / glow_radius * 0.85), edge)
        fade = QColor(self._glow)
        fade.setAlpha(0)
        glow.setColorAt(1.0, fade)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QPointF(center), glow_radius, glow_radius)

        # ② 圆面（对角渐变）
        face = QLinearGradient(
            center.x() - face_radius, center.y() - face_radius,
            center.x() + face_radius, center.y() + face_radius,
        )
        face.setColorAt(0.0, self._face_top)
        face.setColorAt(1.0, self._face_bottom)
        painter.setBrush(face)
        painter.drawEllipse(QPointF(center), face_radius, face_radius)
        painter.end()

        super().paintEvent(event)  # ③ 图标


def glyph_icon(kind, color, size=13):
    """胶囊按钮里的小功能图标（队列 / 置顶 / 迷你）

    与 media_icon 同一套画法：按目标尺寸直接绘制，颜色可随状态切换。
    """
    physical = int(size * 2)
    pixmap = QPixmap(physical, physical)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(2.0, 2.0)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    s = float(size)

    if kind == "queue":      # 三行递短的列表
        for index, width in enumerate((0.78, 0.52, 0.66)):
            y = s * (0.22 + index * 0.28)
            painter.drawRoundedRect(QRectF(s * 0.14, y, s * width, s * 0.12),
                                    s * 0.06, s * 0.06)
    elif kind == "pin":      # 置顶：上箭头 + 基线
        path = QPainterPath()
        path.moveTo(s * 0.5, s * 0.18)
        path.lineTo(s * 0.72, s * 0.42)
        path.lineTo(s * 0.61, s * 0.42)
        path.lineTo(s * 0.61, s * 0.72)
        path.lineTo(s * 0.39, s * 0.72)
        path.lineTo(s * 0.39, s * 0.42)
        path.lineTo(s * 0.28, s * 0.42)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawRoundedRect(QRectF(s * 0.22, s * 0.80, s * 0.56, s * 0.11),
                                s * 0.05, s * 0.05)
    elif kind == "mini":     # 迷你：小窗 + 底部一短条
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(color), max(1.0, s * 0.1)))
        painter.drawRoundedRect(QRectF(s * 0.16, s * 0.22, s * 0.68, s * 0.52),
                                s * 0.1, s * 0.1)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(QRectF(s * 0.34, s * 0.82, s * 0.32, s * 0.1),
                                s * 0.05, s * 0.05)
    elif kind == "vol":      # 喇叭 + 声波
        path = QPainterPath()
        path.moveTo(s * 0.12, s * 0.38)
        path.lineTo(s * 0.30, s * 0.38)
        path.lineTo(s * 0.50, s * 0.20)
        path.lineTo(s * 0.50, s * 0.80)
        path.lineTo(s * 0.30, s * 0.62)
        path.lineTo(s * 0.12, s * 0.62)
        path.closeSubpath()
        painter.drawPath(path)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(color), s * 0.09, cap=Qt.PenCapStyle.RoundCap))
        painter.drawArc(QRectF(s * 0.52, s * 0.28, s * 0.36, s * 0.44), -60 * 16, 120 * 16)
    elif kind == "repeat":   # 循环：圆角矩形回环 + 箭头
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(color), s * 0.11, cap=Qt.PenCapStyle.RoundCap,
                            join=Qt.PenJoinStyle.RoundJoin))
        painter.drawRoundedRect(QRectF(s * 0.18, s * 0.22, s * 0.64, s * 0.56),
                                s * 0.16, s * 0.16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        path = QPainterPath()
        path.moveTo(s * 0.50, s * 0.10)
        path.lineTo(s * 0.70, s * 0.28)
        path.lineTo(s * 0.50, s * 0.46)
        path.closeSubpath()
        painter.drawPath(path)
    elif kind == "folder":   # 文件夹
        path = QPainterPath()
        path.moveTo(s * 0.12, s * 0.28)
        path.lineTo(s * 0.40, s * 0.28)
        path.lineTo(s * 0.48, s * 0.38)
        path.lineTo(s * 0.88, s * 0.38)
        path.lineTo(s * 0.88, s * 0.78)
        path.lineTo(s * 0.12, s * 0.78)
        path.closeSubpath()
        painter.drawPath(path)
    elif kind == "star":     # 星标（封面角标）
        path = QPainterPath()
        for index in range(10):
            angle = math.radians(-90 + index * 36)
            radius = s * (0.46 if index % 2 == 0 else 0.20)
            x = s * 0.5 + radius * math.cos(angle)
            y = s * 0.5 + radius * math.sin(angle)
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        path.closeSubpath()
        painter.drawPath(path)
    elif kind == "plus":     # 小加号（字幕头 leaf 圆标）
        painter.drawRoundedRect(QRectF(s * 0.42, s * 0.14, s * 0.16, s * 0.72),
                                s * 0.08, s * 0.08)
        painter.drawRoundedRect(QRectF(s * 0.14, s * 0.42, s * 0.72, s * 0.16),
                                s * 0.08, s * 0.08)
    painter.end()
    return QIcon(pixmap)


class EqBars(QWidget):
    """队列里"正在播放"行的三根均衡器小条（静态高度，赤陶色）"""

    def __init__(self, color="#B85C3E", parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedSize(12, 12)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        for x, height in ((1, 5), (5, 11), (9, 7)):
            painter.drawRoundedRect(QRectF(x, 12 - height, 2, height), 1, 1)
        painter.end()


__all__ = ["MediaButton", "GlowPlayButton", "EqBars", "media_icon", "glyph_icon"]
