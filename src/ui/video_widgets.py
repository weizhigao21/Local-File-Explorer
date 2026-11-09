"""视频模块通用组件：作品卡、封面懒生成、扫描线程、设置对话框"""
import os
import threading

from PyQt6.QtCore import QObject, QRunnable, QSize, Qt, QThread, QThreadPool, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from resource_manager import config
from ui.image_loader import get_cached, loader
from ui.text_utils import truncate_title
from ui.video_theme import ACCENT, BTN_QSS, CARD_BORDER, INPUT_BG, TEXT_DIM, TEXT_MUTED, TEXT_PRIMARY
from video_manager import probe, scanner
from video_manager.progress import format_duration

# 封面尺寸：卡片内宽 204，16:9 → 115
COVER_W = 204
COVER_H = 115
CARD_W = 220
CARD_H = 210


# ==================== 封面懒生成 ====================

class _CoverSignals(QObject):
    done = pyqtSignal(str)  # 生成的封面路径，失败为空串


class _CoverJob(QRunnable):
    """后台用 ffmpeg 抽帧；不占用 GUI 线程，卡片先显示占位块"""

    def __init__(self, video_path, out_path, signals):
        super().__init__()
        self._video_path = video_path
        self._out_path = out_path
        self._signals = signals

    def run(self):
        try:
            out = probe.make_thumbnail(self._video_path, self._out_path)
        except Exception:  # noqa: BLE001 - 抽帧失败绝不能把线程池带崩
            out = None
        self._signals.done.emit(out or "")


_pool = None
_live_signals = set()


def _thread_pool():
    global _pool
    if _pool is None:
        _pool = QThreadPool()
        _pool.setMaxThreadCount(2)  # ffmpeg 本身吃满一个核，两个并发足够
    return _pool


def generate_cover(work_path, out_path, on_done):
    """给作品抽一张封面；on_done(out_path) 在 GUI 线程回调，失败传空串

    抽帧放后台：一个 4K 视频 ffmpeg 要 0.2~1s，若在扫描/布局时同步做，
    卡片墙会整片卡住。
    """
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        on_done(out_path)
        return

    first = scanner.first_video_path(work_path)
    if not first:
        on_done("")
        return

    signals = _CoverSignals()
    _live_signals.add(signals)

    def _finish(path, _signals=signals):
        _live_signals.discard(_signals)
        on_done(path)

    signals.done.connect(_finish)
    _thread_pool().start(_CoverJob(first, out_path, signals))


# ==================== 作品卡 ====================

class VideoCard(QFrame):
    """作品卡：16:9 封面 + 时长角标 + 标题 + 作者/集数

    尺寸固定 220×210（FlowLayout 按 setFixedSize 的真实尺寸排布，见
    tests/test_flow_layout.py 的重叠守卫）。标题走 truncate_title，
    否则 80 字的长标题会换 5 行把 meta 行顶出卡片。
    """

    clicked = pyqtSignal(dict)

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self.work = work
        self._thumb_loaded = False
        self._thumb_requested = False
        self._token = -1

        self.setObjectName("videoCard")
        self.setFixedSize(CARD_W, CARD_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            f"""
            #videoCard {{
                background-color: #FBF7F0;
                border: 1px solid {CARD_BORDER};
                border-top: 2px solid transparent;
                border-radius: 6px;
            }}
            #videoCard:hover {{
                background-color: #FFFCF7;
                border: 1px solid #E0D5C2;
                border-top: 2px solid {ACCENT};
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # 封面（时长角标是它的子控件，靠右下角绝对定位；封面尺寸固定所以不必跟 resize）
        self.cover = QLabel(self)
        self.cover.setObjectName("videoCover")
        self.cover.setFixedSize(COVER_W, COVER_H)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setStyleSheet(
            f"#videoCover {{ background-color: #EFE8DA; border: none;"
            f" border-radius: 4px; color: {TEXT_DIM}; font-size: 12px; }}"
        )
        self.cover.setText("载入封面…")

        self.badge = QLabel(self.cover)
        self.badge.setObjectName("videoBadge")
        # 角标高 18 → 圆角取半高 9 才是真胶囊（Qt 超过半高会退化成直角）
        self.badge.setFixedHeight(18)
        self.badge.setStyleSheet(
            "#videoBadge { background-color: #3D3830; color: #FFFFFF; border: none;"
            " border-radius: 9px; padding: 0 6px; font-size: 10px; }"
        )
        self.badge.move(COVER_W - 56, COVER_H - 24)
        self.badge.hide()

        layout.addWidget(self.cover)

        title = QLabel(truncate_title(work.get("name", "")))
        title.setObjectName("videoTitle")
        title.setToolTip(work.get("name", ""))
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        title.setFixedHeight(36)  # 两行 13px 字：47 会撑破卡片，36 刚好两行
        title.setStyleSheet(
            f"#videoTitle {{ color: {TEXT_PRIMARY}; font-size: 13px;"
            f" background: transparent; border: none; }}"
        )
        layout.addWidget(title)

        meta = QLabel(self._meta_text(work))
        meta.setObjectName("videoMeta")
        meta.setFixedHeight(16)
        meta.setStyleSheet(
            f"#videoMeta {{ color: {TEXT_MUTED}; font-size: 11px;"
            f" background: transparent; border: none; }}"
        )
        layout.addWidget(meta)
        self.meta = meta  # 子类（ResumeCard）要改这行文案

        self._apply_duration_badge(work.get("total_duration_ms", 0), work.get("video_count", 0))

    @staticmethod
    def _meta_text(work):
        author = work.get("author") or "未分类"
        count = work.get("video_count", 0)
        return f"{author} · {count} 个视频"

    def _apply_duration_badge(self, total_ms, count):
        """角标显示整部作品的总时长（单片太短时角标会挤，所以用合计）"""
        if count and total_ms:
            self.badge.setText(format_duration(total_ms))
            self.badge.adjustSize()
            self.badge.setFixedHeight(18)
            self.badge.move(COVER_W - self.badge.width() - 6, COVER_H - 24)
            self.badge.show()

    def load_thumbnail(self):
        """由父容器视口检测主动触发，避免一次性解码上百张封面"""
        if self._thumb_loaded:
            return
        thumb_path = self.work.get("thumbnail") or ""
        if not thumb_path:
            self.cover.setText("无封面")
            self._thumb_loaded = True
            return

        target = QSize(COVER_W, COVER_H)
        cached = get_cached(thumb_path, target)
        if cached is not None and not cached.isNull():
            self._apply_image(cached)
            self._thumb_loaded = True
            return

        if not os.path.exists(thumb_path):
            # 首次浏览到该作品：后台抽帧，成功后走正常解码链路
            if self._thumb_requested:
                return
            self._thumb_requested = True
            work_path = self.work.get("path") or ""
            generate_cover(work_path, thumb_path, self._on_cover_ready)
            return

        self._token = loader().request(thumb_path, target, self._on_loaded)

    def _on_cover_ready(self, path):
        if not path:
            try:
                self.cover.setText("无封面")
            except RuntimeError:
                return
            self._thumb_loaded = True
            return
        self._token = loader().request(path, QSize(COVER_W, COVER_H), self._on_loaded)

    def _on_loaded(self, _path, _target, image, token):
        if token != self._token:
            return False
        if image.isNull():
            return False
        self._apply_image(image)
        self._thumb_loaded = True
        return True

    def _apply_image(self, image: QImage):
        try:
            _ = self.cover.text()  # 底层 C++ 对象已被销毁时会抛 RuntimeError
        except RuntimeError:
            return
        pixmap = QPixmap.fromImage(image)
        # 封面铺满 16:9 区域：先按比例放大到覆盖，再居中裁切，避免上下留黑边
        scaled = pixmap.scaled(
            QSize(COVER_W, COVER_H),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = max(0, (scaled.width() - COVER_W) // 2)
        y = max(0, (scaled.height() - COVER_H) // 2)
        cropped = scaled.copy(x, y, min(COVER_W, scaled.width()), min(COVER_H, scaled.height()))
        self.cover.setPixmap(cropped)
        self.cover.setText("")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.work)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ResumeCard(VideoCard):
    """「继续观看」卡片：作品封面 + 底部进度条 + 还剩多久 + 集数角标

    复用 VideoCard 的封面懒加载和样式，只换角标与 meta 文案——
    另起一套卡片会和作品卡很快长出两套视觉，之后改配色要改两处。
    """

    def __init__(self, item, parent=None):
        self.item = item
        # 封面按作品取（同一作品的续播记录会显示同一张图，这是对的：
        # 用户认的是「那部片子」，不是某一次播放）
        work = {
            "name": item.get("work_name") or item.get("video_name") or "",
            "author": item.get("author") or "",
            "thumbnail": item.get("thumbnail") or "",
            "path": item.get("work_path") or "",
            "video_count": 0,
            "total_duration_ms": 0,
        }
        super().__init__(work, parent)
        self.meta.setText(self._resume_text(item))
        self.meta.setToolTip(
            f"{item.get('video_name') or ''}\n看到 {format_duration(item.get('position_ms'))}"
        )
        self._apply_resume_badge(item)
        self._add_progress_bar(item)

    @staticmethod
    def _resume_text(item):
        position = int(item.get("position_ms") or 0)
        duration = int(item.get("duration_ms") or 0)
        author = item.get("author") or "未分类"
        if duration > position:
            return f"{author} · 还剩 {format_duration(duration - position)}"
        return f"{author} · 已看 {format_duration(position)}"

    def _apply_resume_badge(self, item):
        """角标换成集数/文件名——续播页里「看到哪一集」比总时长重要"""
        name = (item.get("video_name") or "").strip()
        if not name:
            return
        if len(name) > 10:
            name = name[:10] + "…"
        self.badge.setText(name)
        self.badge.adjustSize()
        self.badge.setFixedHeight(18)
        width = min(self.badge.width(), COVER_W - 12)
        self.badge.setGeometry(COVER_W - width - 6, COVER_H - 24, width, 18)
        self.badge.show()

    def _add_progress_bar(self, item):
        """封面底部 4px 进度条：先放浅色底槽，再放强调色实条（后创建的在上层）"""
        position = int(item.get("position_ms") or 0)
        duration = int(item.get("duration_ms") or 0)
        ratio = 0.0 if duration <= 0 else max(0.0, min(1.0, position / duration))
        width = max(2, int(COVER_W * ratio))

        self.track = QFrame(self.cover)
        self.track.setObjectName("resumeTrack")
        self.track.setGeometry(0, COVER_H - 4, COVER_W, 4)
        self.track.setStyleSheet("#resumeTrack { background-color: #D8CBB4; border: none; }")

        self.progress = QFrame(self.cover)
        self.progress.setObjectName("resumeProgress")
        self.progress.setGeometry(0, COVER_H - 4, width, 4)
        self.progress.setStyleSheet(
            f"#resumeProgress {{ background-color: {ACCENT}; border: none; }}"
        )


# ==================== 扫描线程 ====================

class VideoScanThread(QThread):
    # 不叫 finished：会遮蔽 QThread 内建的无参 finished 信号
    scan_finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    progress = pyqtSignal(str, str, str, int, int, int)  # stage, author, work, count, cur, total
    author_done = pyqtSignal(str, int)

    def __init__(self, total_authors=0):
        super().__init__()
        self.total_authors = total_authors
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            stats = scanner.scan(
                progress_callback=self._emit_progress,
                author_done_callback=self._emit_author_done,
                cancel_event=self.cancel_event,
                total_authors=self.total_authors,
            )
            self.scan_finished.emit(stats or {})
        except Exception as e:  # noqa: BLE001 - 线程里任何异常都要转成信号，不能静默
            self.error.emit(str(e))

    def _emit_progress(self, stage, author, work, count, current, total):
        self.progress.emit(stage, author, work, count, current, total)

    def _emit_author_done(self, author_name, work_count):
        self.author_done.emit(author_name, work_count)


# ==================== 设置 ====================

class VideoSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.root_changed = False
        self.setWindowTitle("视频设置")
        self.setFixedSize(520, 470)
        # 先挂 objectName 再上样式：#objectName 选择器要求对象名已就位，
        # 否则首帧会按系统灰画一次（v1.8.4 同类问题）。
        self.setObjectName("videoSettings")
        self.setStyleSheet("#videoSettings { background-color: #F4EFE7; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        section = QLabel("视频目录（支持多个）")
        section.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 15px; font-weight: bold;"
            " background: transparent; border: none;"
        )
        layout.addWidget(section)

        self.path_list = QListWidget()
        self.path_list.setStyleSheet(
            f"QListWidget {{ background-color: {INPUT_BG}; color: {TEXT_PRIMARY};"
            f" border: 1px solid #E0D5C2; border-radius: 6px; padding: 4px; }}"
        )
        for path in getattr(config, "VIDEO_ROOTS", [config.VIDEO_ROOT]):
            self.path_list.addItem(path)
        layout.addWidget(self.path_list, 1)

        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        add_btn = QPushButton("添加目录")
        add_btn.setStyleSheet(
            f"background-color: {ACCENT}; color: #fff; padding: 6px 14px;"
            " border: none; border-radius: 13px;"
        )
        add_btn.clicked.connect(self._add_path)
        path_row.addWidget(add_btn)

        remove_btn = QPushButton("移除选中")
        remove_btn.setStyleSheet(
            f"background-color: {INPUT_BG}; color: {TEXT_PRIMARY}; padding: 6px 14px;"
            " border: 1px solid #E0D5C2; border-radius: 13px;"
        )
        remove_btn.clicked.connect(self._remove_path)
        path_row.addWidget(remove_btn)
        path_row.addStretch()
        layout.addLayout(path_row)

        desc = QLabel(
            "根目录下每个文件夹是一位作者，作者目录下的每个子文件夹是一部作品，\n"
            "作品目录内的所有视频文件按名称自然排序（第 1 集、第 2 集…）。\n"
            "两棵库出现同名作者时会自动加目录后缀区分。"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 11px; background: transparent; border: none;"
        )
        layout.addWidget(desc)

        self.resume_check = QCheckBox("记住播放进度（下次打开接着看）")
        self.autoplay_check = QCheckBox("一集播完自动播下一集")
        self.resume_check.setChecked(bool(getattr(config, "VIDEO_RESUME", True)))
        self.autoplay_check.setChecked(bool(getattr(config, "VIDEO_AUTOPLAY_NEXT", True)))
        checkbox_qss = (
            f"QCheckBox {{ color: {TEXT_PRIMARY}; font-size: 13px; spacing: 8px;"
            " background: transparent; border: none; }"
            "QCheckBox::indicator { width: 16px; height: 16px; }"
        )
        self.resume_check.setStyleSheet(checkbox_qss)
        self.autoplay_check.setStyleSheet(checkbox_qss)
        layout.addWidget(self.resume_check)
        layout.addWidget(self.autoplay_check)

        hint = QLabel(
            f"封面由 ffmpeg 抽帧生成，缓存于 data/video_thumbs。\n"
            f"未检测到 ffmpeg：{'是' if not probe.has_ffmpeg() else '否'}"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"color: {TEXT_DIM}; font-size: 11px; background: transparent; border: none;"
        )
        layout.addWidget(hint)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        save_btn = QPushButton("保存")
        save_btn.setStyleSheet(BTN_QSS)
        save_btn.clicked.connect(self.on_save)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def _add_path(self):
        start = self.path_list.item(self.path_list.count() - 1)
        path = QFileDialog.getExistingDirectory(
            self, "选择视频根目录", start.text() if start else ""
        )
        if not path:
            return
        existing = {
            os.path.normcase(os.path.normpath(self.path_list.item(i).text()))
            for i in range(self.path_list.count())
        }
        if os.path.normcase(os.path.normpath(path)) in existing:
            QMessageBox.information(self, "提示", "这个目录已经在列表里了。")
            return
        self.path_list.addItem(path)

    def _remove_path(self):
        row = self.path_list.currentRow()
        if row >= 0:
            self.path_list.takeItem(row)

    def on_save(self):
        roots = [
            self.path_list.item(i).text().strip()
            for i in range(self.path_list.count())
        ]
        roots = [p for p in roots if p]
        if not roots:
            QMessageBox.warning(self, "路径无效", "请至少保留一个视频目录。")
            return
        for path in roots:
            if not os.path.isdir(path):
                QMessageBox.warning(self, "路径无效", f"目录不存在: {path}")
                return
        old_roots = list(getattr(config, "VIDEO_ROOTS", [config.VIDEO_ROOT]))
        self.root_changed = [os.path.normcase(p) for p in roots] != [
            os.path.normcase(p) for p in old_roots
        ]
        if self.root_changed:
            config.set_user_config("VIDEO_ROOTS", roots)
        config.set_user_config("VIDEO_RESUME", self.resume_check.isChecked())
        config.set_user_config("VIDEO_AUTOPLAY_NEXT", self.autoplay_check.isChecked())
        config.save_user_config()
        self.accept()
