"""
音频格式图标（QPainter 绘制）

只服务音频模块：覆盖 `audio_manager.scanner.AUDIO_EXTENSIONS` 里的全部 22 种
扩展名，按「有损 / 无损」两档配色，格式名直接写在图标上。

为什么不用现成的图标图片或图标字体：
    - 外部图标风格与本项目「暖米底 + 青蓝强调色 + 轻拟物圆角」不一致；
    - 需要 16 / 24 / 32 / 64 / 128 全尺寸清晰，位图会糊；
    - 字符图标（♪ / ♬）会被系统 emoji 字体接管，颜色不受控。

绘制结构（自下而上）：圆角方块 → 垂直渐变（上亮下暗）→ 顶部柔光 →
细白内描边 → 白色格式名（带下沉投影）。所有坐标按边长比例计算，
因此任何尺寸下视觉重量一致。

尺寸策略：≥20px 写完整格式名（MP3 / FLAC / OPUS）；16px 这类列表视图
用的小尺寸改成 2 字符码（M3 / FL / OP），否则字高不足 6px 只会糊成白线，
而完全不写字的色块无法区分格式。
"""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
)

# ==================== 两档配色 ====================
# 有损直接沿用主题强调色，保证与界面同源；无损加深一档，同屏可区分。
COLOR_LOSSY = "#B85C3E"  # 有损：mp3 / aac / m4a / ogg / wma …
COLOR_LOSSLESS = "#904830"  # 无损：flac / wav / ape / aiff / dsf …

# 有损（含 MPEG-4 / Ogg 家族与各类编码音频）
LOSSY_EXTENSIONS = {
    "mp3", "mp2",
    "m4a", "m4b", "m4r", "aac",
    "ogg", "oga", "opus", "spx",
    "wma", "ac3", "amr",
}

# 无损 / 无压缩（mka 为 Matroska 音频容器，实际多为无损封装）
LOSSLESS_EXTENSIONS = {
    "flac", "wav", "ape", "wv",
    "aiff", "aif",
    "dsf", "dff",
    "mka",
}

# 全部支持的音频扩展名（与 scanner.AUDIO_EXTENSIONS 对齐，测试里有守卫）
AUDIO_EXTENSIONS = LOSSY_EXTENSIONS | LOSSLESS_EXTENSIONS

# 图标里最多容纳的字符数（超出则截断，避免挤成一条细线）
_MAX_LABEL = 5

# 低于这个边长，完整格式名就画不下了：16px 下 "FLAC" 字高只有 5.4px，
# 笔画黏成一片白糊（实测见 _shots/icon_16px_options.png 的 B/C 两行）。
# 但完全不画字更糟——列表视图正是 16px，所有音频会退化成同一个色块，
# 用户根本无法从图标分辨格式。所以小尺寸改用下面的 2 字符码。
_MIN_TEXT_SIZE = 20

# 小尺寸专用的 2 字符码，逐个人工分配，保证 22 种两两不同。
# 规则：优先保留能一眼认出的部分（mp3 → M3、m4a → 4A），
# 再让相近格式在同一维度上错开（aac → AC / ac3 → A3，aif → AF / aiff → AI）。
SHORT_CODES = {
    "aac": "AC", "ac3": "A3",
    "aif": "AF", "aiff": "AI",
    "amr": "AM", "ape": "AP",
    "dff": "DF", "dsf": "DS",
    "flac": "FL",
    "m4a": "4A", "m4b": "4B", "m4r": "4R",
    "mka": "MK", "mp2": "M2", "mp3": "M3",
    "oga": "OA", "ogg": "OG", "opus": "OP", "spx": "SX",
    "wav": "WA", "wma": "WM", "wv": "WV",
}

# 2 字符码的字号占边长比例：16px 下约 10px 字高，实测清晰可读
_SHORT_TEXT_RATIO = 0.62


def normalize_ext(ext: str) -> str:
    """把 '.MP3' / 'mp3' / '07 曲名.flac' 统一成 'mp3'"""
    if not ext:
        return ""
    text = str(ext).strip().lower()
    if "/" in text or "\\" in text:  # 传了完整路径时取最后一段
        text = text.replace("\\", "/").rsplit("/", 1)[-1]
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.lstrip(".")


def is_lossless(ext: str) -> bool:
    return normalize_ext(ext) in LOSSLESS_EXTENSIONS


def color_for(ext: str) -> str:
    """无损用深青，其余（含有损与未知）用主题强调色"""
    return COLOR_LOSSLESS if is_lossless(ext) else COLOR_LOSSY


def label_for(ext: str) -> str:
    """图标上显示的格式名（大写、最多 5 个字符）"""
    name = normalize_ext(ext)
    if not name:
        return "AUDIO"
    return name.upper()[:_MAX_LABEL]


def short_code_for(ext: str) -> str:
    """小尺寸图标用的 2 字符码（16px 下完整名字会糊成白线）"""
    name = normalize_ext(ext)
    if not name:
        return "AU"
    code = SHORT_CODES.get(name)
    if code:
        return code
    # 未登记的扩展名：退化成首尾两个字符，仍保持 2 字符宽度
    return (name[0] + name[-1]).upper() if len(name) > 1 else name.upper()


def _font_ratio(length: int) -> float:
    """字符数 → 字号占边长比例（越长的格式名越小，保证左右留白一致）

    4 字符档从 0.275 提到 0.29：28px 表格图标下 OPUS / AIFF 只有 7.7px 字高，
    圆字母（O / U / S）笔画挤在一起会糊；提到 0.29 后约 8.1px，配合下面的
    宽度二次收缩，既能读清又不会溢出。
    """
    if length <= 2:
        return 0.40
    if length == 3:
        return 0.335
    if length == 4:
        return 0.29
    if length == 5:
        return 0.245
    return 0.20


def _label_font(size: float, text: str, ratio: float | None = None):
    """按可用宽度自适应收缩的字号（AIFF / OPUS 这类 4 字符不会溢出方块）

    ratio：显式指定字号比例（小尺寸的 2 字符码要用更大的比例）。
    """
    font = QFont("Segoe UI")
    font.setBold(True)
    scale = ratio if ratio else _font_ratio(len(text))
    font.setPixelSize(max(1, int(round(size * scale))))
    metrics = QFontMetricsF(font)

    advance = metrics.horizontalAdvance(text)
    # 字体没真正加载时（例如离屏渲染且未配置 QT_QPA_FONTDIR），Qt 退回一个
    # 每字符 1em 的假度量：8px 字号下 4 个字母报出 32px 宽。照这个结果收缩
    # 会把字号压到 5px、直接糊成一团。这种情况宁可沿用比例算出的字号。
    if advance / max(1, len(text)) > font.pixelSize() * 1.5:
        return font, metrics

    # 短码只有 2 个字符，可以比完整名用更宽的可用区域：
    # W / M 是字母里最宽的，按 78% 收缩会把 16px 的 "WM" 压成一小团。
    max_width = size * (0.92 if ratio else 0.78)
    if advance > max_width > 0:
        font.setPixelSize(max(1, int(font.pixelSize() * max_width / advance)))
        metrics = QFontMetricsF(font)
    return font, metrics


def _paint_icon(painter: QPainter, size: float, color: str, text: str,
                ratio: float | None = None) -> None:
    """在 size×size 的逻辑画布上绘制一枚格式图标"""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

    base = QColor(color)
    # 方块体：下留 4% 给投影，整体略高于宽，接近 iOS 图标的观感
    body = QRectF(size * 0.045, size * 0.035, size * 0.91, size * 0.91)
    radius = size * 0.225

    # ① 落影：让图标在米色底上有"浮起来"的体积感
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(60, 48, 32, 28))
    painter.drawRoundedRect(
        QRectF(body.left(), body.top() + size * 0.035, body.width(), body.height()),
        radius,
        radius,
    )

    # ② 主体：上亮下暗的垂直渐变（黏土质感的关键）
    body_path = QPainterPath()
    body_path.addRoundedRect(body, radius, radius)
    gradient = QLinearGradient(
        QPointF(body.center().x(), body.top()),
        QPointF(body.center().x(), body.bottom()),
    )
    gradient.setColorAt(0.0, base.lighter(116))
    gradient.setColorAt(0.52, base)
    gradient.setColorAt(1.0, base.darker(112))
    painter.fillPath(body_path, gradient)

    # ③ 顶部柔光：裁剪进圆角内，上强下弱，模拟受光面
    painter.save()
    painter.setClipPath(body_path)
    gloss = QLinearGradient(
        QPointF(0, body.top()), QPointF(0, body.top() + body.height() * 0.58)
    )
    gloss.setColorAt(0.0, QColor(255, 255, 255, 78))
    gloss.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter.fillRect(
        QRectF(body.left(), body.top(), body.width(), body.height() * 0.58), gloss
    )
    painter.restore()

    # ④ 内描边：极细白边，避免在浅背景上边缘发虚
    pen = painter.pen()
    pen.setColor(QColor(255, 255, 255, 46))
    pen.setWidthF(max(0.6, size * 0.012))
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(
        body.adjusted(size * 0.012, size * 0.012, -size * 0.012, -size * 0.012),
        radius * 0.92,
        radius * 0.92,
    )
    painter.setPen(Qt.PenStyle.NoPen)

    # ⑤ 格式名：白色 + 轻微下沉投影（浮雕感），按 capHeight 做视觉垂直居中
    #   大尺寸写完整格式名；16px 这类小尺寸由调用方换成 2 字符码并放大字号，
    #   否则字高不足 6px 会糊成一道白线。
    if text:
        font, metrics = _label_font(size, text, ratio)
        painter.setFont(font)
        x = body.center().x() - metrics.horizontalAdvance(text) / 2.0
        y = body.center().y() + metrics.capHeight() / 2.0

        painter.setPen(QColor(0, 0, 0, 46))
        painter.drawText(QPointF(x, y + max(0.6, size * 0.014)), text)

        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(QPointF(x, y), text)


def format_pixmap(ext: str, size: int = 64, dpr: float = 1.0) -> QPixmap:
    """返回格式图标的 QPixmap（dpr 用于高分屏，物理尺寸自动放大）"""
    physical = max(1, int(round(size * dpr)))
    pixmap = QPixmap(physical, physical)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.scale(physical / float(size), physical / float(size))
    short = size < _MIN_TEXT_SIZE
    text = short_code_for(ext) if short else label_for(ext)
    _paint_icon(painter, float(size), color_for(ext), text,
                _SHORT_TEXT_RATIO if short else None)
    painter.end()

    pixmap.setDevicePixelRatio(dpr)
    return pixmap


def format_icon(
    ext: str,
    sizes: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256),
) -> QIcon:
    """返回多尺寸 QIcon：表格/树按实际行高挑最清晰的一档"""
    icon = QIcon()
    for side in sizes:
        icon.addPixmap(format_pixmap(ext, side, dpr=1.0))
    return icon


def save_png(ext: str, path: str, size: int = 256) -> str:
    """导出单张 PNG（用于资源集或对外预览）"""
    format_pixmap(ext, size).save(path, "PNG")
    return path
