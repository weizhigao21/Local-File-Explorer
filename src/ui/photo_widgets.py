"""
写真模块通用组件
设置对话框与后台扫描线程
"""
import os
import threading

from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QPushButton,
    QLineEdit,
    QMessageBox,
    QFileDialog,
    QCheckBox,
)
from PyQt6.QtCore import QThread, pyqtSignal

from resource_manager import config
from resource_manager import scanner


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setFixedSize(480, 320)
        self.setStyleSheet("""
            SettingsDialog {
                background-color: #F5F0E8;
                color: #555555;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        # ---- 资源路径 ----
        path_section = QLabel("资源目录")
        path_section.setStyleSheet("color: #555555; font-size: 16px; font-weight: bold;")
        layout.addWidget(path_section)

        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self.path_edit = QLineEdit(config.PHOTO_ROOT)
        self.path_edit.setStyleSheet(
            "background-color: #EDE6DA; color: #555555; padding: 6px; "
            "border: 1px solid #D5CDC0; border-radius: 4px;"
        )
        path_row.addWidget(self.path_edit, 1)

        browse_btn = QPushButton("浏览")
        browse_btn.setStyleSheet(
            "background-color: #EDE6DA; color: #555555; padding: 6px 12px; "
            "border: 1px solid #D5CDC0; border-radius: 4px;"
        )
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        path_desc = QLabel("写真资源的根目录路径，程序将从这里扫描作者/作品/图片。")
        path_desc.setWordWrap(True)
        path_desc.setStyleSheet("color: #999999; font-size: 11px;")
        layout.addWidget(path_desc)

        # ---- 分割线 ----
        sep = QLabel("─" * 50)
        sep.setStyleSheet("color: #D5CDC0;")
        layout.addWidget(sep)

        # ---- GPU 设置 ----
        gpu_section = QLabel("GPU 加速")
        gpu_section.setStyleSheet("color: #555555; font-size: 16px; font-weight: bold;")
        layout.addWidget(gpu_section)

        self.gpu_check = QCheckBox("启用 GPU 解码加速")
        self.gpu_check.setChecked(config.GPU_FRIENDLY_FORMAT)
        self.gpu_check.setStyleSheet("""
            QCheckBox {
                color: #555555;
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
        gpu_desc.setStyleSheet("color: #999999; font-size: 11px;")
        layout.addWidget(gpu_desc)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        save_btn = QPushButton("保存")
        save_btn.setStyleSheet("""
            QPushButton {
                background-color: #EDE6DA;
                color: #555555;
                padding: 8px 24px;
                border: 1px solid #D5CDC0;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #F3EFE8;
                border: 1px solid #42B4C2;
            }
            QPushButton:pressed {
                background-color: #48B8BC;
                color: #fff;
            }
        """)
        save_btn.clicked.connect(self.on_save)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "选择写真根目录", self.path_edit.text())
        if path:
            self.path_edit.setText(path)

    def on_save(self):
        new_path = self.path_edit.text().strip()
        if not os.path.isdir(new_path):
            QMessageBox.warning(self, "路径无效", f"目录不存在: {new_path}")
            return
        # 通过 set_user_config 同步模块级变量 + _user_config
        config.set_user_config("PHOTO_ROOT", new_path)
        config.set_user_config("GPU_FRIENDLY_FORMAT", self.gpu_check.isChecked())
        config.save_user_config()
        self.accept()
        QMessageBox.information(self, "提示", "设置已保存，重启程序后生效。")


class ScanThread(QThread):
    finished = pyqtSignal(dict)   # stats
    error = pyqtSignal(str)
    progress = pyqtSignal(str, str, str, int, int, int)   # stage, author, work, count, current, total
    author_done = pyqtSignal(str, int)  # author_name, work_count

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
            self.finished.emit(stats or {})
        except Exception as e:
            self.error.emit(str(e))

    def _emit_progress(self, stage, author, work, count, current, total):
        self.progress.emit(stage, author, work, count, current, total)

    def _emit_author_done(self, author_name, work_count):
        self.author_done.emit(author_name, work_count)