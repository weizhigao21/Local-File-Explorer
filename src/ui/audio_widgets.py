"""
音频模块 UI 组件
歌单卡片（网格）、子歌单卡片（紧凑列表）、设置对话框
"""
import os
from functools import lru_cache

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImageReader, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from audio_manager import database as db
from resource_manager import config
from ui.audio_theme import (
    ACCENT,
    BORDER_COLOR,
    BTN_QSS,
    CARD_BG,
    CARD_HOVER,
    INPUT_BG,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
)


class CircularProgress(QWidget):
    """顺时针闭合的进度圆环，中心显示真实百分比。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(32, 32)
        self._value = 0

    def setValue(self, value):
        value = max(0, min(100, int(value)))
        if value != self._value:
            self._value = value
            self.update()

    def value(self):
        return self._value

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(2, 2, self.width() - 4, self.height() - 4)
        pen = QPen(QColor(BORDER_COLOR), 2.5)
        painter.setPen(pen)
        painter.drawEllipse(rect)
        pen.setColor(QColor(ACCENT))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if self._value == 100:
            painter.drawEllipse(rect)
        elif self._value:
            painter.drawArc(rect, 90 * 16, -round(self._value * 360 * 16 / 100))
        font = painter.font()
        font.setPixelSize(9)
        painter.setFont(font)
        painter.setPen(QColor(TEXT_PRIMARY))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, f"{self._value}%")


class AudioTaskStatus(QWidget):
    """分页栏内共用扫描/抓取状态；两者同时运行时优先显示扫描。"""

    cancelRequested = pyqtSignal()
    logRequested = pyqtSignal()
    activityChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks = {}
        self.setFixedHeight(32)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.ring = CircularProgress()
        row.addWidget(self.ring)
        self.label = QLabel()
        self.label.setFixedWidth(96)
        self.label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        row.addWidget(self.label)
        compact_button_qss = BTN_QSS + "\nQPushButton { padding: 0; font-size: 16px; }"
        self.cancel_button = QPushButton("×")
        self.cancel_button.setFixedSize(26, 26)
        self.cancel_button.setStyleSheet(compact_button_qss)
        self.cancel_button.setToolTip("取消扫描")
        self.cancel_button.clicked.connect(lambda: self.cancelRequested.emit())
        row.addWidget(self.cancel_button)
        self.log_button = QPushButton("…")
        self.log_button.setFixedSize(26, 26)
        self.log_button.setStyleSheet(compact_button_qss)
        self.log_button.setToolTip("查看信息抓取日志")
        self.log_button.clicked.connect(lambda: self.logRequested.emit())
        row.addWidget(self.log_button)
        self.hide()

    def set_task(self, kind, percent, text, detail="", cancellable=False):
        self._tasks[kind] = (max(0, min(100, percent)), text, detail, cancellable)
        self._refresh()

    def clear_task(self, kind):
        self._tasks.pop(kind, None)
        self._refresh()

    def has_tasks(self):
        return bool(self._tasks)

    def _refresh(self):
        self.setVisible(bool(self._tasks))
        if self._tasks:
            primary = self._tasks.get("scan") or self._tasks["dlsite"]
            # 扫描已经完成时，立即让出位置给仍在拉取的信息。
            if primary[0] >= 100 and "dlsite" in self._tasks:
                primary = self._tasks["dlsite"]
            percent, text, _, _ = primary
            self.ring.setValue(percent)
            self.label.setText(text)
            self.setToolTip("\n".join(task[2] or task[1] for task in self._tasks.values()))
            scan = self._tasks.get("scan")
            self.cancel_button.setVisible(bool(scan and (scan[3] or scan[1] == "正在取消")))
            self.cancel_button.setEnabled(bool(scan and scan[3]))
            self.log_button.setVisible("dlsite" in self._tasks)
        self.activityChanged.emit()


@lru_cache(maxsize=128)
def _cover_thumbnail(path, mtime_ns, file_size):
    """按显示尺寸解码封面；文件状态参与缓存键，换图后不会复用旧图。"""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    target = QSize(160, 140)
    if size.isValid() and (size.width() > target.width() or size.height() > target.height()):
        reader.setScaledSize(size.scaled(target, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        return QPixmap()
    pix = QPixmap.fromImage(image)
    return pix.scaled(target, Qt.AspectRatioMode.KeepAspectRatio,
                      Qt.TransformationMode.SmoothTransformation)


# =============================================================
#  PlaylistCard — 歌单卡片（网格模式用）
# =============================================================
class PlaylistCard(QFrame):
    clicked = pyqtSignal(int)

    def __init__(self, pl, has_children=False, parent=None):
        super().__init__(parent)
        self.pl_id = pl["id"]
        self._pl_path = pl["path"]
        self._pl_name = pl["name"]
        self._has_children = has_children
        self.setFixedSize(180, 240)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            PlaylistCard {{
                background-color: {CARD_BG};
                border: 1px solid {BORDER_COLOR};
                border-top: 2px solid transparent;
                border-radius: 6px;
            }}
            PlaylistCard:hover {{
                background-color: {CARD_HOVER};
                border: 1px solid #D5CDC0;
                border-top: 2px solid {ACCENT};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 8)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # 封面
        self.cover_label = QLabel()
        self.cover_label.setFixedSize(160, 140)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setStyleSheet("background-color: #F5F0E8; border-radius: 4px;")
        self.cover_label.setText("...")
        layout.addWidget(self.cover_label, alignment=Qt.AlignmentFlag.AlignCenter)

        # 歌单名（自动换行）
        name = pl["name"]
        name_label = QLabel(name)
        name_label.setToolTip(name)
        name_label.setWordWrap(True)
        name_label.setFixedWidth(160)
        name_label.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px;")
        layout.addWidget(name_label)

        # 子标题行
        sub_row = QHBoxLayout()
        sub_row.setSpacing(2)
        count = QLabel(f"{pl['track_count']} 首")
        count.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px;")
        sub_row.addWidget(count)

        if has_children:
            folder_hint = QLabel("📁")
            folder_hint.setStyleSheet(f"color: {ACCENT}; font-size: 12px;")
            sub_row.addWidget(folder_hint)

        sub_row.addStretch()
        layout.addLayout(sub_row)
        layout.addStretch()

        # 加载封面
        self._cover_path = pl.get("cover")
        if self._cover_path:
            self._load_cover()

    def mouseReleaseEvent(self, event):
        # 用 release 而非 press 触发：避免双击/页面切换瞬间按压落点偏移导致误开歌单
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.pl_id)

    def _load_cover(self):
        try:
            stat = os.stat(self._cover_path)
        except OSError:
            return
        pix = _cover_thumbnail(self._cover_path, stat.st_mtime_ns, stat.st_size)
        if not pix.isNull():
            self.cover_label.setPixmap(pix)
            self.cover_label.setText("")

    def contextMenuEvent(self, event):
        from PyQt6.QtWidgets import QMenu

        from ui.audio_theme import MENU_QSS
        menu = QMenu(self)
        menu.setStyleSheet(MENU_QSS)
        copy_action = menu.addAction("复制歌单名称")
        copy_action.triggered.connect(lambda: QApplication.clipboard().setText(self._pl_name))
        menu.addSeparator()
        action = menu.addAction("打开所在文件夹")
        action.triggered.connect(self._open_folder)
        menu.exec(event.globalPos())

    def _open_folder(self):
        try:
            os.startfile(self._pl_path)
        except OSError as e:
            QMessageBox.warning(self, "打开失败", f"无法打开文件夹:\n{e}")


# =============================================================
#  SubPlaylistCard — 详情页内的子歌单小卡片
# =============================================================
class _PlaylistPathLabel(QLabel):
    """按实际宽度显示歌单路径，始终保留完整名称供悬停查看。"""

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self._full_name = name
        self.setToolTip(name)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._update_display()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_display()

    def _update_display(self):
        width = self.contentsRect().width()
        metrics = self.fontMetrics()
        name = self._full_name
        if width <= 0:
            display = ""
        elif metrics.horizontalAdvance(name) <= width:
            display = name
        else:
            parts = name.split(" > ")
            shortened = " > ".join(
                [part[:4] + "…" if len(part) > 4 else part for part in parts[:-1]]
                + parts[-1:]
            )
            if metrics.horizontalAdvance(shortened) <= width:
                display = shortened
            else:
                # 更窄时优先保留末级文件夹的开头，完整路径仍在提示中。
                leaf = parts[-1]
                prefix = "… > " if len(parts) > 1 else ""
                available = width - metrics.horizontalAdvance(prefix)
                display = prefix + metrics.elidedText(
                    leaf, Qt.TextElideMode.ElideRight, max(0, available),
                )
                if metrics.horizontalAdvance(display) > width:
                    display = metrics.elidedText(leaf, Qt.TextElideMode.ElideRight, width)
        if self.text() != display:
            self.setText(display)


class SubPlaylistCard(QFrame):
    """详情页内的子歌单——紧凑列表样式"""
    clicked = pyqtSignal(int)

    def __init__(self, pl, parent=None):
        super().__init__(parent)
        self.pl_id = pl["id"]
        self.setFixedHeight(32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("""
            SubPlaylistCard {
                background-color: transparent;
                border: 1px solid transparent;
                border-radius: 4px;
                padding: 2px 8px;
            }
            SubPlaylistCard:hover {
                background-color: #EDE6DA;
                border: 1px solid #D5CDC0;
            }
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(8)

        name = pl["name"]
        self.name_label = _PlaylistPathLabel(name)
        self.name_label.setStyleSheet(f"color: {ACCENT}; font-size: 12px;")
        layout.addWidget(self.name_label, 1)

        cnt = QLabel(f"{pl['track_count']} 首")
        cnt.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")
        layout.addWidget(cnt)

        arrow = QLabel("▶")
        arrow.setStyleSheet("color: #555; font-size: 10px;")
        layout.addWidget(arrow)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.pl_id)


# =============================================================
#  设置对话框
# =============================================================
class AudioSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("音频设置")
        self.setFixedSize(540, 430)
        self.setStyleSheet(f"AudioSettingsDialog {{ background-color: #FDF9F2; color: {TEXT_PRIMARY}; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        section = QLabel("音频目录（支持多个）")
        section.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 16px; font-weight: bold;")
        layout.addWidget(section)

        self.path_list = QListWidget()
        self.path_list.setStyleSheet(f"""
            QListWidget {{
                background-color: {INPUT_BG}; color: {TEXT_PRIMARY};
                border: 1px solid {BORDER_COLOR}; border-radius: 4px;
                padding: 4px;
            }}
        """)
        for p in config.AUDIO_ROOTS:
            self.path_list.addItem(p)
        layout.addWidget(self.path_list, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        add_btn = QPushButton("添加目录")
        add_btn.setStyleSheet(f"background-color: {ACCENT}; color: #fff; padding: 6px 14px; border-radius: 4px;")
        add_btn.clicked.connect(self._add_path)
        btn_row.addWidget(add_btn)
        remove_btn = QPushButton("移除选中")
        remove_btn.setStyleSheet(f"background-color: {BORDER_COLOR}; color: {TEXT_PRIMARY}; padding: 6px 14px; border-radius: 4px;")
        remove_btn.clicked.connect(self._remove_path)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        desc = QLabel("每个音频根目录独立扫描，子文件夹作为一个歌单。\n修改后需要重启程序生效。")
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(desc)

        btn_layout = QHBoxLayout()
        delete_btn = QPushButton("删除当前数据库")
        delete_btn.setStyleSheet("""
            QPushButton { background-color: #D4736E; color: #fff;
                padding: 8px 16px; border-radius: 4px; }
            QPushButton:hover { background-color: #E08580; }
        """)
        delete_btn.clicked.connect(self._delete_db)
        btn_layout.addWidget(delete_btn)
        btn_layout.addStretch()
        save_btn = QPushButton("保存")
        save_btn.setStyleSheet("""
            QPushButton { background-color: #CCC0B0; color: #fff;
                padding: 8px 24px; border-radius: 4px; }
            QPushButton:hover { background-color: #BBB0A0; }
        """)
        save_btn.clicked.connect(self.on_save)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

    def _delete_db(self):
        reply = QMessageBox.question(
            self, "确认删除",
            "确定要删除当前音频数据库吗？\n所有歌单和曲目数据将被清空。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        db.clear_all()
        QMessageBox.information(self, "提示", "数据库已清空。\n请重新扫描以重建歌单。")

    def _add_path(self):
        path = QFileDialog.getExistingDirectory(self, "选择音频根目录",
                                                config.AUDIO_ROOT)
        if not path:
            return
        path = path.strip()
        existing = [self.path_list.item(i).text() for i in range(self.path_list.count())]
        if path in existing:
            return
        self.path_list.addItem(path)

    def _remove_path(self):
        row = self.path_list.currentRow()
        if row >= 0:
            self.path_list.takeItem(row)

    def on_save(self):
        paths = [self.path_list.item(i).text().strip()
                 for i in range(self.path_list.count())]
        paths = [p for p in paths if p]
        if not paths:
            QMessageBox.warning(self, "路径无效", "请至少保留一个音频目录。")
            return
        for p in paths:
            if not os.path.isdir(p):
                QMessageBox.warning(self, "路径无效", f"目录不存在: {p}")
                return
        config.set_user_config("AUDIO_ROOTS", paths)
        config.save_user_config()
        self.accept()
        QMessageBox.information(self, "提示", "设置已保存，重启程序后生效。")
