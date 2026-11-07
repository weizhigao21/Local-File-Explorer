"""作品详情页：一个作品下的视频列表（序号 / 名称 / 已看进度 / 时长）

交互：双击或回车播放，右键菜单可「用系统播放器打开」「在资源管理器中显示」
「重置播放进度」。列表行用 setItemWidget，所以 ::item 绝不能带 padding
（Qt 会先扣掉 item padding 再把行控件塞进剩余矩形，标签字形头脚会被裁——
v1.8.6 踩过，见 tests/test_no_visible_label_clips_glyphs）。
"""
import os
import subprocess

from PyQt6.QtCore import QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.text_utils import truncate_title
from ui.video_theme import (
    ACCENT,
    BG_MAIN,
    BTN_QSS,
    CARD_BORDER,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
)
from video_manager import database as db
from video_manager.progress import format_duration, format_position

ROW_HEIGHT = 44
ITEM_HEIGHT = 48  # 行控件 44 + 行间 4


def open_with_system(path):
    """交给 Windows 默认关联程序打开（个别编码 Qt 解不了时的兜底）"""
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def reveal_in_explorer(path):
    """在资源管理器里选中该文件（绝不用 taskkill 重启外壳）"""
    if os.name != "nt":
        return
    try:
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])  # noqa: S603,S607
    except OSError as e:
        print(f"[打开资源管理器失败] {path}: {e}")


class VideoRow(QFrame):
    """列表行：序号 + 名称 + 已看进度 + 时长"""

    def __init__(self, index, video, position_ms, parent=None):
        super().__init__(parent)
        self.video = video
        self.setObjectName("videoRow")
        self.setFixedHeight(ROW_HEIGHT)
        self.setStyleSheet(
            "#videoRow { background: transparent; border: none; }"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(10)

        order = QLabel(f"{index + 1:02d}")
        order.setFixedWidth(26)
        order.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        order.setStyleSheet(
            f"color: {TEXT_DIM}; font-size: 11px; background: transparent; border: none;"
        )
        layout.addWidget(order)

        name = QLabel(truncate_title(video.get("name", "")))
        name.setToolTip(video.get("path", ""))
        name.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 13px; background: transparent; border: none;"
        )
        layout.addWidget(name, 1)

        if position_ms:
            watched = QLabel(f"已看 {format_position(position_ms)}")
            watched.setFixedWidth(96)
            watched.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            watched.setStyleSheet(
                f"color: {ACCENT}; font-size: 11px; background: transparent; border: none;"
            )
            layout.addWidget(watched)

        duration = QLabel(format_duration(video.get("duration_ms", 0)))
        duration.setFixedWidth(58)
        duration.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        duration.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 11px; background: transparent; border: none;"
        )
        layout.addWidget(duration)


class WorkVideosView(QWidget):
    """作品详情页（主窗口 stack 的第 2 页）"""

    playRequested = pyqtSignal(list, int)   # videos, index
    backRequested = pyqtSignal()

    def __init__(self, work, videos, parent=None):
        super().__init__(parent)
        self.work = work
        self.videos = videos

        self.setObjectName("videoDetailPage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"#videoDetailPage {{ background-color: {BG_MAIN}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(10)

        back_btn = QPushButton("← 返回")
        back_btn.setStyleSheet(BTN_QSS)
        back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        back_btn.clicked.connect(self.backRequested.emit)
        header.addWidget(back_btn)

        title = QLabel(truncate_title(work.get("name", ""), 40))
        title.setToolTip(work.get("name", ""))
        title.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 16px; font-weight: bold;"
            " background: transparent; border: none;"
        )
        header.addWidget(title)

        total_ms = sum(int(v.get("duration_ms") or 0) for v in videos)
        meta = QLabel(
            f"{work.get('author') or '未分类'} · {len(videos)} 个视频"
            + (f" · 共 {format_duration(total_ms)}" if total_ms else "")
        )
        meta.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 11px; background: transparent; border: none;"
        )
        header.addWidget(meta)
        header.addStretch()

        play_btn = QPushButton("▶ 从头播放")
        play_btn.setStyleSheet(BTN_QSS)
        play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        play_btn.clicked.connect(lambda: self._play(0))
        header.addWidget(play_btn)

        layout.addLayout(header)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("videoList")
        # ::item 不设 padding：setItemWidget 时 padding 会挤压行控件（v1.8.6）
        self.list_widget.setStyleSheet(
            f"""
            #videoList {{
                background-color: #FBF7F0;
                border: 1px solid {CARD_BORDER};
                border-radius: 6px;
                outline: none;
            }}
            #videoList::item {{
                border: none;
                border-bottom: 1px solid #F0E8DA;
                background: transparent;
            }}
            #videoList::item:hover {{
                background-color: #FFFCF7;
            }}
            #videoList::item:selected {{
                background-color: #F3E9DF;
            }}
            """
        )
        self.list_widget.itemDoubleClicked.connect(self._on_item_activated)
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.list_widget, 1)

        self._progress = db.get_progress_map([v["path"] for v in videos])
        self._populate()

    # ---- 列表 ----

    def _populate(self):
        self.list_widget.clear()
        for index, video in enumerate(self.videos):
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, ITEM_HEIGHT))
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.list_widget.addItem(item)
            row = VideoRow(index, video, self._progress.get(video["path"], 0))
            self.list_widget.setItemWidget(item, row)

    def refresh_progress(self):
        """从播放器回来时刷新「已看 xx:xx」列（不重建整个列表）"""
        self._progress = db.get_progress_map([v["path"] for v in self.videos])
        self._populate()

    def _on_item_activated(self, item):
        index = item.data(Qt.ItemDataRole.UserRole)
        if index is not None:
            self._play(int(index))

    def _play(self, index):
        if 0 <= index < len(self.videos):
            self.playRequested.emit(self.videos, index)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            item = self.list_widget.currentItem()
            if item is not None:
                self._on_item_activated(item)
                return
        if event.key() == Qt.Key.Key_Escape:
            self.backRequested.emit()
            return
        super().keyPressEvent(event)

    # ---- 右键菜单 ----

    def _on_context_menu(self, pos):
        item = self.list_widget.itemAt(pos)
        if item is None:
            return
        index = int(item.data(Qt.ItemDataRole.UserRole))
        video = self.videos[index]
        path = video["path"]

        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background-color: #FBF7F0; color: {TEXT_PRIMARY};"
            f" border: 1px solid {CARD_BORDER}; padding: 4px; }}"
            f"QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}"
            f"QMenu::item:selected {{ background-color: #F3E9DF; }}"
        )
        play_action = menu.addAction("播放")
        external_action = menu.addAction("用系统播放器打开")
        reveal_action = menu.addAction("在资源管理器中显示")
        menu.addSeparator()
        reset_action = menu.addAction("重置播放进度")

        chosen = menu.exec(self.list_widget.mapToGlobal(pos))
        if chosen is play_action:
            self._play(index)
        elif chosen is external_action:
            open_with_system(path)
        elif chosen is reveal_action:
            reveal_in_explorer(path)
        elif chosen is reset_action:
            db.clear_progress(path)
            self.refresh_progress()

    # ---- 统计 ----

    def summary_text(self):
        """给主窗口标题栏/状态栏用的简述（也便于测试断言）"""
        total_ms = sum(int(v.get("duration_ms") or 0) for v in self.videos)
        parts = [f"{len(self.videos)} 个视频"]
        if total_ms:
            parts.append(f"共 {format_duration(total_ms)}")
        return " · ".join(parts)


__all__ = [
    "VideoRow", "WorkVideosView", "open_with_system", "reveal_in_explorer",
]
