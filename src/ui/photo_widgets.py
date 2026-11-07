"""
写真模块通用组件
设置对话框与后台扫描线程
"""
import os
import threading

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from resource_manager import config, scanner


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setFixedSize(520, 470)
        self.setStyleSheet("""
            SettingsDialog {
                background-color: #F4EFE7;
                color: #3D3830;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        # ---- 资源路径（支持多个根目录） ----
        path_section = QLabel("资源目录（支持多个）")
        path_section.setStyleSheet("color: #3D3830; font-size: 16px; font-weight: bold;")
        layout.addWidget(path_section)

        self.path_list = QListWidget()
        self.path_list.setStyleSheet(
            "QListWidget { background-color: #EDE2CE; color: #3D3830;"
            " border: 1px solid #E0D5C2; border-radius: 6px; padding: 4px; }"
        )
        for path in getattr(config, "PHOTO_ROOTS", [config.PHOTO_ROOT]):
            self.path_list.addItem(path)
        layout.addWidget(self.path_list, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        add_btn = QPushButton("添加目录")
        add_btn.setStyleSheet(
            "background-color: #B85C3E; color: #fff; padding: 6px 14px;"
            " border: none; border-radius: 13px;"
        )
        add_btn.clicked.connect(self._add_path)
        btn_row.addWidget(add_btn)

        remove_btn = QPushButton("移除选中")
        remove_btn.setStyleSheet(
            "background-color: #EDE2CE; color: #3D3830; padding: 6px 14px;"
            " border: 1px solid #E0D5C2; border-radius: 13px;"
        )
        remove_btn.clicked.connect(self._remove_path)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        path_desc = QLabel(
            "每个根目录下都是「作者 / 作品 / 图片」，可放在不同磁盘或 NAS 上。\n"
            "两棵库出现同名作者时会自动加目录后缀区分（如「某某（写真@F）」）。"
        )
        path_desc.setWordWrap(True)
        path_desc.setStyleSheet("color: #A89880; font-size: 11px;")
        layout.addWidget(path_desc)

        # ---- 分割线 ----
        sep = QLabel("─" * 50)
        sep.setStyleSheet("color: #E0D5C2;")
        layout.addWidget(sep)

        # ---- GPU 设置 ----
        gpu_section = QLabel("GPU 加速")
        gpu_section.setStyleSheet("color: #3D3830; font-size: 16px; font-weight: bold;")
        layout.addWidget(gpu_section)

        self.gpu_check = QCheckBox("启用 GPU 解码加速")
        self.gpu_check.setChecked(config.GPU_FRIENDLY_FORMAT)
        self.gpu_check.setStyleSheet("""
            QCheckBox {
                color: #3D3830;
                font-size: 13px;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
            }
        """)
        layout.addWidget(self.gpu_check)

        gpu_desc = QLabel(
            "使用 GPU 友好的像素格式 (RGBX8888) 加速图片解码和渲染。\n"
            "修改路径或 GPU 设置后需要重启程序生效。"
        )
        gpu_desc.setWordWrap(True)
        gpu_desc.setStyleSheet("color: #A89880; font-size: 11px;")
        layout.addWidget(gpu_desc)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        save_btn = QPushButton("保存")
        save_btn.setStyleSheet("""
            QPushButton {
                background-color: #EDE2CE;
                color: #3D3830;
                padding: 8px 24px;
                border: 1px solid #E0D5C2;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #F3EFE8;
                border: 1px solid #B85C3E;
            }
            QPushButton:pressed {
                background-color: #D4886A;
                color: #fff;
            }
        """)
        save_btn.clicked.connect(self.on_save)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

    def _add_path(self):
        start = self.path_list.item(self.path_list.count() - 1)
        path = QFileDialog.getExistingDirectory(
            self, "选择写真根目录", start.text() if start else ""
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
        paths = [
            self.path_list.item(i).text().strip()
            for i in range(self.path_list.count())
        ]
        paths = [p for p in paths if p]
        if not paths:
            QMessageBox.warning(self, "路径无效", "请至少保留一个写真目录。")
            return
        for path in paths:
            if not os.path.isdir(path):
                QMessageBox.warning(self, "路径无效", f"目录不存在: {path}")
                return
        # 通过 set_user_config 同步模块级变量 + _user_config
        config.set_user_config("PHOTO_ROOTS", paths)
        config.set_user_config("GPU_FRIENDLY_FORMAT", self.gpu_check.isChecked())
        config.save_user_config()
        self.accept()
        QMessageBox.information(self, "提示", "设置已保存，重启程序后生效。")


class ScanThread(QThread):
    # 不命名为 finished：会遮蔽 QThread 内建的 finished 信号（无参数），
    # 与线程自身的生命周期语义混淆
    scan_finished = pyqtSignal(dict)   # stats
    error = pyqtSignal(str)
    progress = pyqtSignal(str, str, str, int, int, int)   # stage, author, work, count, current, total
    author_done = pyqtSignal(str, int)  # author_name, work_count

    def __init__(self, total_authors=0, check_fingerprint=True):
        super().__init__()
        self.total_authors = total_authors
        self.check_fingerprint = check_fingerprint
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
                check_fingerprint=self.check_fingerprint,
            )
            self.scan_finished.emit(stats or {})
        except Exception as e:
            self.error.emit(str(e))

    def _emit_progress(self, stage, author, work, count, current, total):
        self.progress.emit(stage, author, work, count, current, total)

    def _emit_author_done(self, author_name, work_count):
        self.author_done.emit(author_name, work_count)
